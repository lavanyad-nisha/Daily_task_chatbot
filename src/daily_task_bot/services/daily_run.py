"""Orchestrates one daily run: fetch → match → summarise → deliver.

Dependencies are passed in, so the run can be tested with fakes and no network access.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, Protocol

from daily_task_bot.domain.leave import leave_state
from daily_task_bot.domain.matcher import match_messages
from daily_task_bot.domain.models import ChatMessage, Employee, LeaveWindow, WorkPackage
from daily_task_bot.domain.rendering import render_summary
from daily_task_bot.domain.summary import build_summary
from daily_task_bot.domain.task_window import is_scheduled_on

Outcome = Literal["dry_run", "sent", "failed", "skipped"]


class OpenProjectReader(Protocol):
    async def get_statuses(self) -> dict: ...
    async def get_open_work_packages(self, statuses: dict, project_ids: list[int] | None = None) -> list[WorkPackage]: ...


class ChatWriter(Protocol):
    async def find_direct_message(self, chat_user_id: str) -> str: ...
    async def post_message(self, space: str, text: str) -> dict: ...


class ChatReader(Protocol):
    async def list_messages(self, space: str, since: datetime) -> list[ChatMessage]: ...


@dataclass
class EmployeeResult:
    email: str
    outcome: Outcome
    text: str = ""
    error: str | None = None


@dataclass
class RunReport:
    run_date: date
    results: list[EmployeeResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        return counts


async def run_daily_summary(
    *,
    run_date: date,
    now: datetime,
    employees: list[Employee],
    leave_by_email: dict[str, list[LeaveWindow]],
    holidays: set[date],
    openproject: OpenProjectReader,
    project_ids: list[int] | None = None,
    chat_reader: ChatReader | None = None,
    read_spaces: list[str] | None = None,
    lookback_hours: int = 24,
    chat_writer: ChatWriter | None = None,
    dry_run: bool = True,
) -> RunReport:
    if not dry_run and chat_writer is None:
        # A live run must never be reported as a dry run. Fail before touching anything.
        raise ValueError("live run requires a Chat writer; none is configured")

    report = RunReport(run_date=run_date)

    statuses = await openproject.get_statuses()
    work_packages = await openproject.get_open_work_packages(statuses, project_ids)
    by_assignee: dict[int, list[WorkPackage]] = {}
    for wp in work_packages:
        if wp.assignee_id is not None and is_scheduled_on(wp, run_date):
            by_assignee.setdefault(wp.assignee_id, []).append(wp)

    chat_messages: list[ChatMessage] = []
    if chat_reader and read_spaces:
        since = now - timedelta(hours=lookback_hours)
        for space in read_spaces:
            chat_messages.extend(await chat_reader.list_messages(space, since))

    for employee in employees:
        email = employee.email.lower()
        try:
            windows = leave_by_email.get(email, [])
            state = leave_state(run_date, windows, holidays)

            # Only the employee's own messages count as their Chat updates.
            own_messages = [
                m for m in chat_messages
                if employee.chat_user_id and m.sender_id == employee.chat_user_id
            ]
            known_ids = {wp.id for wp in by_assignee.get(employee.openproject_user_id, [])}
            updates = match_messages(own_messages, known_ids)

            summary = build_summary(
                employee=employee,
                run_date=run_date,
                work_packages=by_assignee.get(employee.openproject_user_id, []),
                leave=state,
                chat_updates=updates,
            )
            text = render_summary(summary)
        except Exception as exc:  # isolate per employee
            report.results.append(EmployeeResult(email, "failed", error=f"build: {exc}"))
            continue

        if dry_run:
            report.results.append(EmployeeResult(email, "dry_run", text=text))
            continue

        if not employee.chat_user_id:
            report.results.append(EmployeeResult(email, "skipped", text=text, error="no chat_user_id"))
            continue

        try:
            space = await chat_writer.find_direct_message(employee.chat_user_id)
            await chat_writer.post_message(space, text)
            report.results.append(EmployeeResult(email, "sent", text=text))
        except Exception as exc:  # isolate per employee
            report.results.append(EmployeeResult(email, "failed", text=text, error=f"deliver: {exc}"))

    return report
