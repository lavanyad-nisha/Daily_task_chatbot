"""Daily routed posting: refresh roles, summarise each pilot employee's saved entries, post per space.

- Dry run (the default) logs what would be posted. Nothing is sent and nothing is recorded.
- A live post is recorded in routed_posts, so the same employee, space and day is never posted twice.
  Live runs repeat every background cycle after routed_post_time, so DSRs saved later in the evening
  are posted too. Entries for spaces already posted today are not summarised again (no extra AI calls).
  A correction made after the employee's post for that space is not posted again.
- Only pilot employees with saved entries for the day are handled.
- Reads OpenProject (memberships, tasks) and writes only to the bot database, plus Chat posts when live.
"""

import asyncio
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from daily_task_bot.config import Settings
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee, RoutedPost
from daily_task_bot.services.chat_events import local_today
from daily_task_bot.services.dsr_preview import PreviewLine
from daily_task_bot.services.routed_posting import plan_routed_posts, send_plans
from daily_task_bot.services.routing_sync import load_routes, roles_by_project, sync_employee_roles
from daily_task_bot.services.summarizer import (
    AI_METHOD,
    AI_TIMEOUT_SECONDS,
    make_haiku_summarizer,
    summarize,
)

SUMMARY_TIMEOUT_SECONDS = AI_TIMEOUT_SECONDS  # one slow AI call must not hold up the background loop


def is_live(settings: Settings) -> bool:
    return settings.allow_external_writes and not settings.dry_run


def routing_done_for_today(settings: Settings, result: dict[str, Any]) -> bool:
    """Whether the background loop can stop running the routing job for the rest of the local day.

    Before routed_post_time: not done. A live run: never done, so later DSRs are posted on the next
    cycle (routed_posts prevents duplicates). Dry run or skipped: done after one run, so the summaries
    are not rebuilt every cycle.
    """
    if result.get("status") == "waiting":
        return False
    if result.get("status") == "ran" and is_live(settings):
        return False
    return True


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_due(settings: Settings, now: datetime) -> bool:
    """True once the routed-posting time has passed, in the configured timezone."""
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    return local_now.time() >= _clock(settings.routed_post_time)


async def _lines_for(session: Session, op_client: Any, employee: Employee, day, statuses) -> list[PreviewLine]:
    submission = session.scalar(select(DsrSubmission).where(
        DsrSubmission.employee_id == employee.id,
        DsrSubmission.dsr_date == day,
        DsrSubmission.is_current.is_(True),
    ))
    if submission is None:
        return []
    entries = session.scalars(select(DsrEntry).where(DsrEntry.submission_id == submission.id)
                              .order_by(DsrEntry.id)).all()
    lines: list[PreviewLine] = []
    for entry in entries:
        detail = await op_client.get_work_package(entry.work_package_id, statuses)
        if detail is None:
            lines.append(PreviewLine(entry.work_package_id, None, entry.hours_spent, entry.work_done,
                                     note="work package not visible in OpenProject"))
        else:
            lines.append(PreviewLine(entry.work_package_id, detail.project_id, entry.hours_spent,
                                     entry.work_done))
    return lines


async def run_routing_cycle(session_factory: sessionmaker, settings: Settings, op_client: Any, gchat: Any,
                            *, now: datetime, summarizer: Any | None) -> dict[str, Any]:
    day = local_today(settings, now)
    report: dict[str, Any] = {"day": day.isoformat(), "roles": None, "employees": 0, "skipped": 0,
                              "posted": 0, "would_post": 0, "failed": 0, "flagged": 0, "already_posted": 0}

    memberships = await op_client.list_user_memberships()
    with session_factory() as session:
        report["roles"] = sync_employee_roles(session, memberships)
        session.commit()

    statuses = await op_client.get_statuses()
    pilot = set(settings.pilot_chat_user_ids)
    with session_factory() as session:
        employees = [
            e for e in session.scalars(select(Employee).where(
                Employee.is_active.is_(True), Employee.chat_user_id.is_not(None),
                Employee.openproject_user_id.is_not(None))).all()
            if e.chat_user_id in pilot
        ]
        routes = load_routes(session)
        for employee in employees:
            lines = await _lines_for(session, op_client, employee, day, statuses)
            if not lines:
                report["skipped"] += 1
                continue
            done_spaces = set(session.scalars(select(RoutedPost.space_id).where(
                RoutedPost.employee_id == employee.id, RoutedPost.post_date == day)))
            roles = roles_by_project(session, employee.id)
            # Plan first without summaries, so entries for spaces already posted are never summarised.
            draft = plan_routed_posts(employee_name=employee.name, dsr_date=day, lines=lines,
                                      roles_by_project=roles, routes=routes)
            pending_ids = {wp for p in draft.posts if p.space_id not in done_spaces for wp in p.work_package_ids}
            if not pending_ids:
                report["already_posted"] += 1
                continue
            report["employees"] += 1
            summaries = {}
            method_of = {}
            for line in lines:
                if line.work_package_id in pending_ids:
                    # AI first for each summary; a failed one falls back to rule-based on its own.
                    # The SDK call is blocking; run it in a thread so the web server keeps answering.
                    result = await asyncio.to_thread(summarize, line.work_done, summarizer)
                    summaries[line.work_package_id] = result.text
                    method_of[line.work_package_id] = result.method
            plan = plan_routed_posts(
                employee_name=employee.name, dsr_date=day, lines=lines,
                roles_by_project=roles, routes=routes, summaries=summaries,
            )
            report["flagged"] += len(plan.flagged)
            posts = [p for p in plan.posts if p.space_id not in done_spaces]
            outcomes = await send_plans(gchat, posts, allow_writes=settings.allow_external_writes,
                                        dry_run=settings.dry_run)
            for outcome in outcomes:
                action = outcome["action"]
                if action == "posted":
                    tasks = outcome.get("tasks") or []
                    ai_count = sum(1 for wp in tasks if method_of.get(wp) == AI_METHOD)
                    session.add(RoutedPost(employee_id=employee.id, space_id=outcome["space"],
                                           post_date=day, message_name=outcome.get("message"),
                                           ai_summaries=ai_count, rule_based_summaries=len(tasks) - ai_count))
                    report["posted"] += 1
                elif action == "would_post":
                    report["would_post"] += 1
                else:
                    report["failed"] += 1
            session.commit()
    return report


async def run_routing_once(settings: Settings, session_factory: sessionmaker, *, now: datetime) -> dict[str, Any]:
    """Build clients from settings and run one routing cycle. Skips quietly when not configured or not due."""
    if not is_due(settings, now):
        return {"status": "waiting", "reason": "routed post time not reached"}
    if settings.gchat_service_account_file is None or not settings.gchat_service_account_file.is_file():
        return {"status": "skipped", "reason": "Google Chat service account file is not configured"}
    if settings.openproject_api_key is None:
        return {"status": "skipped", "reason": "OpenProject is not configured"}

    from daily_task_bot.clients.gchat import GChatClient
    from daily_task_bot.clients.openproject import OpenProjectClient

    op_client = OpenProjectClient(settings.openproject_base_url,
                                  settings.openproject_api_key.get_secret_value(), writes_enabled=False)
    gchat = GChatClient(settings.gchat_service_account_file)
    summarizer = None
    if settings.anthropic_api_key is not None:
        summarizer = make_haiku_summarizer(settings.anthropic_api_key.get_secret_value())
    try:
        report = await run_routing_cycle(session_factory, settings, op_client, gchat, now=now, summarizer=summarizer)
        return {"status": "ran", **report}
    finally:
        await gchat.aclose()
        await op_client.aclose()
