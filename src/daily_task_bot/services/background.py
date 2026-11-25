"""Automatic background sync: OpenProject and Jibble, one cycle at a time.

Rules:
- A cycle does nothing while DTB_ALLOW_EXTERNAL_WRITES is false. No records are claimed.
- OpenProject runs when its API key is configured. Jibble runs only when its configuration is
  complete; otherwise the cycle reports the missing setting names and skips Jibble.
- Jibble records held for a mapping that is now configured go back to pending.
- Each target runs in its own try block, so one failure does not stop the other.
- Logs contain counts and exception type names only. No tokens, personal data or DSR text.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from daily_task_bot.clients.jibble import JibbleAuth, JibbleClient
from daily_task_bot.clients.openproject import OpenProjectClient
from daily_task_bot.config import Settings, jibble_configuration_problems
from daily_task_bot.db.models import Employee, ExternalSyncRecord
from daily_task_bot.services.chat_events import local_today
from daily_task_bot.services.jibble_hour_sync import run_jibble_sync
from daily_task_bot.services.openproject_time_sync import run_time_entry_sync
from daily_task_bot.services.routing_job import run_routing_once

logger = logging.getLogger("daily_task_bot.background")

# The routed project posts run once per local day, after routed_post_time. Reset on restart; live posts
# are also recorded in routed_posts, so a restart cannot post the same message twice.
_routing_day_done = None


def _mark_routing_done(day) -> None:
    global _routing_day_done
    _routing_day_done = day

NO_MAPPING = "employee has no Jibble mapping"
NO_PROJECT = "Jibble project and activity are not configured"


def requeue_resolved_jibble_reviews(session: Session, settings: Settings) -> int:
    """Move Jibble records held only for a mapping that is now configured back to pending."""
    records = session.scalars(
        select(ExternalSyncRecord).where(
            ExternalSyncRecord.target == "jibble_time",
            ExternalSyncRecord.status == "needs_review",
        )
    ).all()
    project_ready = bool(settings.jibble_hour_project_id and settings.jibble_hour_activity_id)
    released = 0
    for record in records:
        reason = record.last_error or ""
        employee = session.get(Employee, record.employee_id)
        mapping_now_set = reason == NO_MAPPING and employee is not None and bool(employee.jibble_person_id)
        project_now_set = reason == NO_PROJECT and project_ready
        if not (mapping_now_set or project_now_set):
            continue
        record.status = "pending"
        record.last_error = None
        record.last_error_class = None
        released += 1
    session.flush()
    return released


async def run_sync_cycle(settings: Settings, session_factory: sessionmaker,
                         *, now: datetime | None = None) -> dict:
    """One pass over both targets. Returns counts and reasons only."""
    if not settings.allow_external_writes:
        return {"status": "skipped", "reason": "external writes disabled"}
    when = now or datetime.now(UTC)
    report: dict = {"status": "ran"}

    if settings.openproject_api_key is not None:
        op_client = OpenProjectClient(settings.openproject_base_url,
                                      settings.openproject_api_key.get_secret_value(),
                                      writes_enabled=True)
        try:
            summary = await run_time_entry_sync(
                session_factory, op_client, now=when, worker_id="background-openproject",
                writes_allowed=True, time_activity_id=settings.openproject_time_activity_id,
                status_choices=frozenset(settings.dsr_status_choices),
            )
            report["openproject"] = summary.counts()
        except Exception as exc:  # per-target isolation; only the type name is kept
            report["openproject"] = {"error": type(exc).__name__}
        finally:
            await op_client.aclose()
    else:
        report["openproject"] = {"skipped": "DTB_OPENPROJECT_API_KEY is not set"}

    problems = jibble_configuration_problems(settings)
    if problems:
        report["jibble"] = {"skipped": "configuration incomplete", "problems": problems}
        return report

    auth = JibbleAuth(settings.jibble_token_url, settings.jibble_client_id.get_secret_value(),
                      settings.jibble_client_secret.get_secret_value())
    jb_client = JibbleClient(settings.jibble_people_base_url, auth,
                             time_entries_base_url=settings.jibble_time_entries_base_url,
                             writes_enabled=True)
    try:
        with session_factory() as session:
            released = requeue_resolved_jibble_reviews(session, settings)
            session.commit()
        summary = await run_jibble_sync(
            session_factory, jb_client, now=when, worker_id="background-jibble", writes_allowed=True,
            hour_project_id=settings.jibble_hour_project_id,
            hour_activity_id=settings.jibble_hour_activity_id,
        )
        report["jibble"] = {"released_from_review": released, **summary.counts()}
    except Exception as exc:
        report["jibble"] = {"error": type(exc).__name__}
    finally:
        await jb_client.aclose()
        await auth.aclose()
    return report


async def run_reminders_once(settings: Settings, session_factory: sessionmaker) -> dict:
    """Send any due morning plans or evening reminders. Off unless external writes are allowed."""
    if not settings.allow_external_writes:
        return {"status": "skipped", "reason": "external writes disabled"}
    if settings.gchat_service_account_file is None or not settings.gchat_service_account_file.is_file():
        return {"status": "skipped", "reason": "Google Chat service account file is not configured"}
    if settings.openproject_api_key is None:
        return {"status": "skipped", "reason": "OpenProject is not configured"}
    from datetime import UTC
    from daily_task_bot.clients.gchat import GChatClient
    from daily_task_bot.services.chat_reminders import run_reminder_cycle

    op_client = OpenProjectClient(settings.openproject_base_url,
                                  settings.openproject_api_key.get_secret_value(), writes_enabled=True)
    gchat = GChatClient(settings.gchat_service_account_file)
    try:
        return await run_reminder_cycle(session_factory, settings, op_client, gchat,
                                        now=datetime.now(UTC))
    finally:
        await gchat.aclose()
        await op_client.aclose()


async def background_sync_loop(settings: Settings, session_factory: sessionmaker,
                               stop: asyncio.Event) -> None:
    """Run cycles until stop is set. A failed cycle is logged by type and the loop continues."""
    interval = settings.background_sync_interval_seconds
    while not stop.is_set():
        try:
            report = await run_sync_cycle(settings, session_factory)
            logger.info("background sync cycle: %s", report)
        except Exception as exc:
            logger.warning("background sync cycle failed: %s", type(exc).__name__)
        try:
            reminders = await run_reminders_once(settings, session_factory)
            logger.info("background reminder cycle: %s", reminders)
        except Exception as exc:
            logger.warning("background reminder cycle failed: %s", type(exc).__name__)
        try:
            now = datetime.now(UTC)
            today = local_today(settings, now)
            if _routing_day_done != today:
                routing = await run_routing_once(settings, session_factory, now=now)
                logger.info("background routing cycle: %s", {k: v for k, v in routing.items() if k != "day"})
                if routing.get("status") != "waiting":
                    _mark_routing_done(today)
        except Exception as exc:
            logger.warning("background routing cycle failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
