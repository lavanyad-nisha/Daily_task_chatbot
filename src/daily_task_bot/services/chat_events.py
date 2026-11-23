"""Google Chat inbound events: verify the sender, then handle messages and form submissions.

Security:
- Every request must carry a Google-signed bearer token whose audience equals the configured
  endpoint URL and whose issuer is the Chat service. Anything else is refused before parsing.
- The employee is identified only by the Chat user ID in the event, which must match a
  registered employee. Nothing is trusted from form contents about identity.
- Each submitted task must be open and assigned to that employee in OpenProject. Otherwise the
  whole submission is refused and nothing is saved.
"""

from datetime import date, datetime
from typing import Any

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.config import Settings
from daily_task_bot.db.models import Employee
from daily_task_bot.domain.chat_commands import HELP_TEXT, parse_command
from daily_task_bot.services.dsr_updates import TaskChange, apply_task_change
from daily_task_bot.domain.chat_cards import (
    SUBMIT_FUNCTION,
    CardTask,
    StatusChoice,
    build_dsr_card,
    parse_dsr_form,
)
from daily_task_bot.services.dsr import (
    DsrConflictError,
    DsrValidationError,
    EntryInput,
    confirm_submission,
    create_draft,
)

CHAT_ISSUER = "chat@system.gserviceaccount.com"


class ChatAuthError(Exception):
    """The request did not carry a valid Google Chat token for this endpoint."""


def verify_chat_token(token: str, audience: str | None) -> dict[str, Any]:
    """Verify a Google-signed token from Chat. Raises ChatAuthError on any problem."""
    if not audience:
        raise ChatAuthError("endpoint audience is not configured")
    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)
    except Exception as exc:  # signature, audience or expiry failure
        raise ChatAuthError(type(exc).__name__) from exc
    if claims.get("iss") != CHAT_ISSUER and claims.get("email") != CHAT_ISSUER:
        raise ChatAuthError("token was not issued to Google Chat")
    return claims


def local_today(settings: Settings, now: datetime | None = None) -> date:
    from zoneinfo import ZoneInfo
    base = now or datetime.now(ZoneInfo(settings.timezone))
    return base.astimezone(ZoneInfo(settings.timezone)).date()


def find_employee(session: Session, event: dict[str, Any]) -> Employee | None:
    user = event.get("user") or {}
    chat_user = user.get("name")
    if not chat_user:
        return None
    return session.scalar(select(Employee).where(Employee.chat_user_id == chat_user, Employee.is_active.is_(True)))


async def open_tasks_for(op_client: Any, employee: Employee, settings: Settings) -> list[CardTask]:
    statuses = await op_client.get_statuses()
    packages = await op_client.get_open_work_packages(statuses, settings.openproject_project_ids or None)
    return [
        CardTask(wp.id, wp.subject, wp.status_name)
        for wp in sorted(packages, key=lambda w: w.id)
        if wp.assignee_id == employee.openproject_user_id
    ]


async def status_choices_for(op_client: Any, settings: Settings) -> list[StatusChoice]:
    statuses = await op_client.get_statuses()
    return [StatusChoice(sid, statuses[sid].name) for sid in sorted(settings.dsr_status_choices) if sid in statuses]


async def dsr_card_for(session: Session, op_client: Any, settings: Settings, employee: Employee,
                       today: date) -> dict[str, Any]:
    return build_dsr_card(
        employee_name=employee.name,
        dsr_date=today,
        tasks=await open_tasks_for(op_client, employee, settings),
        status_choices=await status_choices_for(op_client, settings),
    )


def _text(message: str) -> dict[str, Any]:
    return {"text": message}


async def _validate_tasks(op_client: Any, employee: Employee, work_package_ids: list[int]) -> list[str]:
    statuses = await op_client.get_statuses()
    problems: list[str] = []
    for wp_id in work_package_ids:
        detail = await op_client.get_work_package(wp_id, statuses)
        if detail is None:
            problems.append(f"#{wp_id} is not visible in OpenProject")
        elif detail.status_is_closed:
            problems.append(f"#{wp_id} is closed")
        elif detail.assignee_id != employee.openproject_user_id:
            problems.append(f"#{wp_id} is not assigned to you")
    return problems


async def handle_chat_event(
    event: dict[str, Any],
    *,
    session: Session,
    op_client: Any,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the Chat response body. The caller commits the session on success."""
    kind = event.get("type")
    today = local_today(settings, now)

    if kind == "ADDED_TO_SPACE":
        return _text("Hi! I send your daily task summary and collect your daily status report here. "
                     "Type DSR to open today's form.")

    employee = find_employee(session, event)
    if employee is not None and employee.chat_user_id not in set(settings.pilot_chat_user_ids):
        return _text("The DSR bot is not enabled for your account yet.")
    if employee is None:
        return _text("Your Chat account is not linked to a task profile. Please contact your administrator.")

    if kind == "MESSAGE":
        command = parse_command((event.get("message") or {}).get("text") or "")
        if command.error:
            return _text(f"{command.error}\n\n{HELP_TEXT}")
        if command.name == "DSR":
            return await dsr_card_for(session, op_client, settings, employee, today)
        if command.name == "HELP":
            return _text(HELP_TEXT)
        tasks = await open_tasks_for(op_client, employee, settings)
        if command.name == "TASKS":
            if not tasks:
                return _text("You have no open assigned tasks.")
            lines = [f"{i}. #{t.work_package_id} {t.subject} ({t.status_name})"
                     for i, t in enumerate(tasks, start=1)]
            return _text("Your open tasks:\n" + "\n".join(lines))
        if command.name in ("UPDATE", "STATUS"):
            number = command.args["task"]
            if number > len(tasks):
                return _text("That task number is not on your list. Type TASKS to see the numbers.")
            wp_id = tasks[number - 1].work_package_id
            if command.name == "UPDATE":
                change = TaskChange(wp_id, hours=command.args["hours"], work_done=command.args["work"])
            else:
                change = TaskChange(wp_id, status_id=command.args["status_id"])
            message = apply_task_change(
                session, employee=employee, today=today, change=change,
                allowed_status_ids=frozenset(settings.dsr_status_choices),
                go_live=settings.dsr_go_live_date, now=now or datetime.now(),
            )
            return _text(message)
        return _text(HELP_TEXT)

    if kind == "CARD_CLICKED":
        common = event.get("common") or {}
        if common.get("invokedFunction") != SUBMIT_FUNCTION:
            return _text("That action is not available.")
        params = common.get("parameters") or {}
        if params.get("dsr_date") != today.isoformat():
            return _text("This form is for a different day. Type DSR to open today's form.")
        parsed = parse_dsr_form(common.get("formInputs") or {}, frozenset(settings.dsr_status_choices))
        if parsed.errors:
            return _text("Please fix these and submit again: " + "; ".join(parsed.errors))
        if not parsed.entries:
            return _text("No hours were entered, so nothing was recorded.")
        problems = await _validate_tasks(op_client, employee, [e.work_package_id for e in parsed.entries])
        if problems:
            return _text("Nothing was recorded. " + "; ".join(problems))
        try:
            submission = create_draft(
                session, employee_id=employee.id, dsr_date=today,
                entries=[EntryInput(e.work_package_id, e.hours, e.work_done, e.remaining_work, e.new_status_id)
                         for e in parsed.entries],
                allowed_status_ids=frozenset(settings.dsr_status_choices),
            )
            confirm_submission(session, submission.id, now=now or datetime.now(), go_live=settings.dsr_go_live_date)
        except DsrConflictError:
            return _text("Your status report for today is already recorded.")
        except DsrValidationError as exc:
            return _text(f"Nothing was recorded: {exc}")
        return _text(f"Thanks {employee.name}. {len(parsed.entries)} task(s) recorded for {today.isoformat()}.")

    return _text("Sorry, I did not understand that.")
