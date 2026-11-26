"""Google Chat inbound events: verify the sender, then handle messages and form submissions.

Security:
- Every request must carry a Google-signed bearer token whose audience equals the configured
  endpoint URL and whose issuer is the Chat service. Anything else is refused before parsing.
- The employee is identified only by the Chat user ID in the event, which must match a
  registered employee. Nothing is trusted from form contents about identity.
- Each submitted task must be open and assigned to that employee in OpenProject. Otherwise the
  whole submission is refused and nothing is saved.
"""

import logging
from datetime import date, datetime
from typing import Any

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.config import Settings
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee
from daily_task_bot.domain.chat_cards import (
    SUBMIT_FUNCTION,
    CardTask,
    StatusChoice,
    build_dsr_card,
    parse_dsr_form,
)
from daily_task_bot.domain.chat_commands import HELP_TEXT, parse_command
from daily_task_bot.domain.task_window import TaskRules, is_completed, is_eligible, task_flag
from daily_task_bot.services.dsr import (
    DsrConflictError,
    DsrValidationError,
    EntryInput,
    confirm_submission,
    create_draft,
)
from daily_task_bot.services.dsr_preview import PreviewLine, build_preview_text
from daily_task_bot.services.dsr_updates import TaskChange, apply_task_change
from daily_task_bot.services.openproject_time_sync import push_status_now
from daily_task_bot.services.routing_sync import load_routes, roles_by_project
from daily_task_bot.services.space_links import describe_link, link_space, unlink_space

CHAT_ISSUER = "chat@system.gserviceaccount.com"
log = logging.getLogger("uvicorn.error")


class ChatAuthError(Exception):
    """The request did not carry a valid Google Chat token for this endpoint."""


def verify_chat_token(token: str, audience: str | None,
                      addon_service_account: str | None = None) -> dict[str, Any]:
    """Verify a Google-signed token from Chat. Raises ChatAuthError on any problem.

    Accepted signers: Google Chat itself (plain Chat apps), or this project's own Workspace
    add-on service account (Chat apps built as add-ons). Any other signer is refused, even with
    the right audience, because another project's add-on could point at this URL.
    """
    if not audience:
        raise ChatAuthError("endpoint audience is not configured")
    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)
    except Exception as exc:  # signature, audience or expiry failure
        raise ChatAuthError(type(exc).__name__) from exc
    if claims.get("iss") == CHAT_ISSUER or claims.get("email") == CHAT_ISSUER:
        return claims
    if (addon_service_account and claims.get("email") == addon_service_account
            and claims.get("email_verified") is True):
        return claims
    raise ChatAuthError("token was not issued to Google Chat or this project's add-on")


def local_today(settings: Settings, now: datetime | None = None) -> date:
    from zoneinfo import ZoneInfo
    base = now or datetime.now(ZoneInfo(settings.timezone))
    return base.astimezone(ZoneInfo(settings.timezone)).date()


def find_employee(session: Session, event: dict[str, Any]) -> Employee | None:
    """Find the employee by Chat user ID, or link them once by their Google-verified email.

    The event body is trusted only after the Google-signed token was verified. The email link is
    made only when that employee has no Chat user ID yet, so an existing link is never replaced.
    """
    user = event.get("user") or {}
    chat_user = user.get("name")
    if not chat_user:
        return None
    employee = session.scalar(
        select(Employee).where(Employee.chat_user_id == chat_user, Employee.is_active.is_(True)))
    if employee is not None:
        return employee
    email = str(user.get("email") or "").strip().lower()
    if not email:
        return None
    employee = session.scalar(
        select(Employee).where(Employee.email == email, Employee.is_active.is_(True)))
    if employee is None or employee.chat_user_id is not None:
        return None
    employee.chat_user_id = chat_user
    session.flush()
    return employee


async def open_tasks_for(op_client: Any, employee: Employee, settings: Settings, today: date) -> list[CardTask]:
    """Tasks for the DSR card and TASKS: every open task assigned to the employee, on any date."""
    statuses = await op_client.get_statuses()
    packages = await op_client.get_open_work_packages(statuses, settings.openproject_project_ids or None)
    rules = TaskRules.from_settings(settings)
    return [
        CardTask(wp.id, wp.subject, _status_label(wp, today))
        for wp in sorted(packages, key=lambda w: w.id)
        if wp.assignee_id == employee.openproject_user_id and is_eligible(wp, rules)
    ]


def _status_label(wp: Any, today: date) -> str:
    flag = task_flag(wp, today)
    return f"{wp.status_name}, {flag}" if flag else wp.status_name


async def status_choices_for(op_client: Any, settings: Settings) -> list[StatusChoice]:
    statuses = await op_client.get_statuses()
    return [StatusChoice(sid, statuses[sid].name) for sid in sorted(settings.dsr_status_choices) if sid in statuses]


async def dsr_card_for(session: Session, op_client: Any, settings: Settings, employee: Employee,
                       today: date) -> dict[str, Any]:
    return build_dsr_card(
        employee_name=employee.name,
        dsr_date=today,
        tasks=await open_tasks_for(op_client, employee, settings, today),
        status_choices=await status_choices_for(op_client, settings),
    )


async def preview_text_for(session: Session, op_client: Any, employee: Employee, today: date) -> str:
    """Where today's saved entries would be routed. Reads only; nothing is sent or written."""
    submission = session.scalar(select(DsrSubmission).where(
        DsrSubmission.employee_id == employee.id,
        DsrSubmission.dsr_date == today,
        DsrSubmission.is_current.is_(True),
    ))
    if submission is None:
        return f"No entries saved for {today.isoformat()} yet."
    entries = session.scalars(select(DsrEntry).where(DsrEntry.submission_id == submission.id)
                              .order_by(DsrEntry.id)).all()
    statuses = await op_client.get_statuses()
    lines: list[PreviewLine] = []
    for entry in entries:
        detail = await op_client.get_work_package(entry.work_package_id, statuses)
        if detail is None:
            lines.append(PreviewLine(entry.work_package_id, None, entry.hours_spent, entry.work_done,
                                     note="work package not visible in OpenProject"))
        else:
            lines.append(PreviewLine(entry.work_package_id, detail.project_id,
                                     entry.hours_spent, entry.work_done))
    return build_preview_text(
        dsr_date=today, lines=lines,
        roles_by_project=roles_by_project(session, employee.id),
        routes=load_routes(session),
    )


def immediate_status_enabled(settings: Settings) -> bool:
    """A STATUS change goes to OpenProject at once only when live writes are on (not dry run)."""
    return settings.allow_external_writes and not settings.dry_run


async def _push_status_for(session: Session, op_client: Any, settings: Settings, employee: Employee,
                           today: date, work_package_id: int, now: datetime) -> str | None:
    """Push today's status change for one task now. None when there is nothing to push."""
    entry = session.scalar(select(DsrEntry).join(DsrSubmission).where(
        DsrSubmission.employee_id == employee.id,
        DsrSubmission.dsr_date == today,
        DsrSubmission.is_current.is_(True),
        DsrEntry.work_package_id == work_package_id,
    ))
    if entry is None or entry.new_status_id is None:
        return None
    statuses = await op_client.get_statuses()
    result = await push_status_now(
        session, entry=entry, client=op_client, statuses=statuses,
        status_choices=frozenset(settings.dsr_status_choices), now=now,
    )
    if result is None:
        return None
    task = f"#{work_package_id}"
    if result.action in ("updated", "reused"):
        return f"Updated in OpenProject: {task} is now status {entry.new_status_id}."
    if result.action == "retry_wait":
        return f"Saved. OpenProject was unavailable, so {task} will be retried automatically."
    if result.action == "needs_review":
        return f"Saved, but {task} needs review in OpenProject: {result.detail}"
    if result.action == "failed_permanent":
        return f"Saved, but OpenProject did not accept the status for {task}: {result.detail}"
    return f"Saved. Status for {task}: {result.action}."


PRIVATE_ONLY_COMMANDS = frozenset({"TASKS", "DSR", "PREVIEW", "UPDATE", "STATUS"})
SPACE_ADMIN_COMMANDS = frozenset({"LINK", "UNLINK"})
SPACE_COMMANDS = SPACE_ADMIN_COMMANDS | {"LINKED"}


def message_text(event: dict[str, Any]) -> str:
    """The command text without the bot @mention.

    In a space the message must @mention the bot, and message.text then starts with
    "@Daily Task Bot". Chat puts the text without the mention in message.argumentText.
    """
    message = event.get("message") or {}
    argument_text = message.get("argumentText")
    if isinstance(argument_text, str) and argument_text.strip():
        return argument_text.strip()
    return (message.get("text") or "").strip()


def is_private_chat(event: dict[str, Any]) -> bool:
    """True only for a direct message with the bot.

    Chat sends space.spaceType (current) and may also send the older space.type. If neither says
    "direct message", the event is treated as a space, so private commands are refused rather than
    showing someone's tasks to a whole space.
    """
    space = event.get("space") or {}
    if space.get("spaceType") == "DIRECT_MESSAGE" or space.get("type") == "DM":
        return True
    if space.get("singleUserBotDm") is True:
        return True
    if "spaceType" not in space and "type" not in space:
        log.warning("chat event has no space type; treating it as a space: %s", space.get("name"))
    return False


def _handle_space_rules(event: dict[str, Any], *, session: Session, settings: Settings) -> dict[str, Any] | None:
    """Space linking and private-only commands. Returns a reply, or None to continue normally."""
    text = message_text(event)
    command = parse_command(text)
    first_word = text.split()[0].upper() if text else ""

    if is_private_chat(event):
        if first_word in SPACE_COMMANDS:
            return _text("LINK, UNLINK and LINKED work inside the client's Chat space, not in a private chat.")
        return None

    # Inside a Chat space
    if first_word in SPACE_COMMANDS:
        space_id = (event.get("space") or {}).get("name")
        if not space_id:
            return _text("I could not read this space's ID, so nothing was changed.")
        if first_word == "LINKED":  # anyone in the space may check
            return _text(describe_link(session, space_id=space_id))
        user = (event.get("user") or {}).get("name")
        if user not in set(settings.bot_admin_chat_user_ids):
            return _text("Only bot admins can link or unlink this space.")
        if command.error:
            return _text(command.error)
        if command.name == "LINK":
            return _text(link_space(session, space_id=space_id, client=command.args["client"],
                                    linked_by=user, clients=settings.clients))
        return _text(unlink_space(session, space_id=space_id))
    if command.name in PRIVATE_ONLY_COMMANDS:
        return _text(f"Please message me privately to use {command.name}.")
    return {}  # other messages in a space get no reply


def _text(message: str) -> dict[str, Any]:
    return {"text": message}


async def _validate_tasks(op_client: Any, employee: Employee, work_package_ids: list[int], today: date,
                         rules: TaskRules | None = None) -> list[str]:
    rules = rules or TaskRules()
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
        elif is_completed(detail, rules):
            problems.append(f"#{wp_id} is already completed")
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
        if not is_private_chat(event):
            return _text("Hi! I post updates here once this space is linked to a client. "
                         "Ask a bot admin to type @Daily Task Bot LINK <client> here. "
                         "For your own tasks and DSR, message me privately.")
        return _text("Hi! I send your daily task summary and collect your daily status report here. "
                     "Type DSR to open today's form.")

    if kind == "MESSAGE":
        chat_reply = _handle_space_rules(event, session=session, settings=settings)
        if chat_reply is not None:
            return chat_reply

    employee = find_employee(session, event)
    if employee is not None and employee.chat_user_id not in set(settings.pilot_chat_user_ids):
        log.info("chat user not in pilot list: %s (%s)", employee.chat_user_id, employee.email)
        return _text("The DSR bot is not enabled for your account yet.")
    if employee is None:
        user = event.get("user") or {}
        log.info("chat user not linked: %s (%s)", user.get("name"), user.get("email"))
        return _text("Your Chat account is not linked to a task profile. Please contact your administrator.")

    if kind == "MESSAGE":
        command = parse_command(message_text(event))
        if command.error:
            return _text(f"{command.error}\n\n{HELP_TEXT}")
        if command.name == "DSR":
            return await dsr_card_for(session, op_client, settings, employee, today)
        if command.name == "HELP":
            return _text(HELP_TEXT)
        if command.name == "PREVIEW":
            return _text(await preview_text_for(session, op_client, employee, today))
        tasks = await open_tasks_for(op_client, employee, settings, today)
        if command.name == "TASKS":
            if not tasks:
                return _text("You have no open tasks for today. A task shows up here when it is assigned to "
                             "you in OpenProject, not completed, and its start date has come.")
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
            if command.name == "STATUS" and immediate_status_enabled(settings):
                pushed = await _push_status_for(session, op_client, settings, employee, today,
                                                wp_id, now or datetime.now())
                if pushed is not None:
                    return _text(pushed)
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
        problems = await _validate_tasks(op_client, employee, [e.work_package_id for e in parsed.entries], today,
                                        TaskRules.from_settings(settings))
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
