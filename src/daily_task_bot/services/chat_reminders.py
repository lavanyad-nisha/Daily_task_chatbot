"""Scheduled Chat messages: the morning plan and the evening DSR reminder, sent once per day.

- Morning: the next-day plan, from open OpenProject assignments and the previous confirmed DSR.
- Evening: the DSR form card, skipped when a submission already exists for the day.
- Each send is logged in reminders_sent. The unique key prevents duplicates across restarts.
- Each employee is handled on its own, so one failure does not stop the others.
- Nothing is sent while external writes are disabled.
"""

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from daily_task_bot.config import Settings
from daily_task_bot.db.models import DsrSubmission, Employee, ReminderSent
from daily_task_bot.domain.task_window import is_scheduled_on
from daily_task_bot.services.chat_events import dsr_card_for, local_today
from daily_task_bot.services.next_day import build_next_day_plan, load_previous_entries, render_next_day_plan


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _already_sent(session: Session, employee_id: int, kind: str, day: date) -> bool:
    return session.scalar(select(ReminderSent.id).where(
        ReminderSent.employee_id == employee_id, ReminderSent.kind == kind, ReminderSent.plan_date == day,
    )) is not None


def _mark(session: Session, employee_id: int, kind: str, day: date) -> None:
    session.add(ReminderSent(employee_id=employee_id, kind=kind, plan_date=day))
    session.flush()


def _has_submission(session: Session, employee_id: int, day: date) -> bool:
    return session.scalar(select(DsrSubmission.id).where(
        DsrSubmission.employee_id == employee_id, DsrSubmission.dsr_date == day,
    ).limit(1)) is not None


async def _send_to(gchat: Any, chat_user_id: str, body: dict[str, Any] | str) -> None:
    space = await gchat.find_direct_message(chat_user_id)
    if isinstance(body, str):
        await gchat.post_message(space, body)
    else:
        await gchat.post_card(space, body)


async def send_morning_plans(session_factory: sessionmaker, settings: Settings, op_client: Any,
                             gchat: Any, *, now: datetime) -> dict[str, int]:
    today = local_today(settings, now)
    counts = {"sent": 0, "skipped": 0, "failed": 0}
    statuses = await op_client.get_statuses()
    packages = await op_client.get_open_work_packages(statuses, settings.openproject_project_ids or None)
    with session_factory() as session:
        employees = session.scalars(select(Employee).where(
            Employee.is_active.is_(True), Employee.chat_user_id.is_not(None),
            Employee.openproject_user_id.is_not(None))).all()
        employees = [e for e in employees if e.chat_user_id in set(settings.pilot_chat_user_ids)]
        for employee in employees:
            if _already_sent(session, employee.id, "morning", today):
                counts["skipped"] += 1
                continue
            try:
                previous_day, previous = load_previous_entries(session, employee.id, today)
                mine = [w for w in packages
                        if w.assignee_id == employee.openproject_user_id and is_scheduled_on(w, today)]
                plan = build_next_day_plan(run_date=today, open_assignments=mine,
                                           previous_dsr_date=previous_day, previous_entries=previous,
                                           status_names={sid: st.name for sid, st in statuses.items()})
                await _send_to(gchat, employee.chat_user_id, render_next_day_plan(plan, employee.name))
                _mark(session, employee.id, "morning", today)
                session.commit()
                counts["sent"] += 1
            except Exception:
                session.rollback()
                counts["failed"] += 1
    return counts


async def send_evening_reminders(session_factory: sessionmaker, settings: Settings, op_client: Any,
                                 gchat: Any, *, now: datetime) -> dict[str, int]:
    today = local_today(settings, now)
    counts = {"sent": 0, "skipped": 0, "failed": 0}
    with session_factory() as session:
        employees = session.scalars(select(Employee).where(
            Employee.is_active.is_(True), Employee.chat_user_id.is_not(None),
            Employee.openproject_user_id.is_not(None))).all()
        employees = [e for e in employees if e.chat_user_id in set(settings.pilot_chat_user_ids)]
        for employee in employees:
            if _already_sent(session, employee.id, "evening", today) or _has_submission(session, employee.id, today):
                counts["skipped"] += 1
                continue
            try:
                card = await dsr_card_for(session, op_client, settings, employee, today)
                await _send_to(gchat, employee.chat_user_id, card)
                _mark(session, employee.id, "evening", today)
                session.commit()
                counts["sent"] += 1
            except Exception:
                session.rollback()
                counts["failed"] += 1
    return counts


async def run_reminder_cycle(session_factory: sessionmaker, settings: Settings, op_client: Any,
                             gchat: Any, *, now: datetime) -> dict[str, Any]:
    """Send whichever reminders are due now. Does nothing while external writes are disabled."""
    if not settings.allow_external_writes:
        return {"status": "skipped", "reason": "external writes disabled"}
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    local_time = local_now.time().replace(tzinfo=None)
    report: dict[str, Any] = {"status": "ran"}
    if local_time >= _clock(settings.morning_send_time):
        report["morning"] = await send_morning_plans(session_factory, settings, op_client, gchat, now=now)
    if local_time >= _clock(settings.evening_reminder_time):
        report["evening"] = await send_evening_reminders(session_factory, settings, op_client, gchat, now=now)
    return report
