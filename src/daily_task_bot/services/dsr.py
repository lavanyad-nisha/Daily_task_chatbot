"""DSR drafts and confirmation. Confirmation enqueues the OpenProject time records.

Transaction contract: the caller owns the transaction. confirm_submission locks the submission
row, sets it to confirmed, and enqueues its records in the same transaction. Either both happen,
or neither does.

Scope: this module is channel-agnostic. The internal API uses it now, and Google Chat will use it
later, so nothing here knows about Chat.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee
from daily_task_bot.domain.sync_mapping import MAX_DESCRIPTION_LENGTH, MAX_HOURS
from daily_task_bot.services.openproject_time_sync import (
    AuthorizationError,
    enqueue_for_submission,
)


class DsrValidationError(ValueError):
    """The DSR content breaks a rule. Nothing was saved."""


class DsrConflictError(Exception):
    """A current DSR already exists, or the submission is in a state that forbids the action."""


class DsrNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class EntryInput:
    work_package_id: int
    hours_spent: Decimal
    work_done: str
    remaining_work: str | None = None
    new_status_id: int | None = None


def validate_entries(entries: list[EntryInput], allowed_status_ids: frozenset[int] = frozenset()) -> None:
    if not entries:
        raise DsrValidationError("a DSR needs at least one entry")
    seen: set[int] = set()
    total = Decimal("0")
    for e in entries:
        if e.work_package_id <= 0:
            raise DsrValidationError("work_package_id must be positive")
        if e.work_package_id in seen:
            raise DsrValidationError(f"work package {e.work_package_id} appears twice")
        seen.add(e.work_package_id)
        if not (Decimal("0") < e.hours_spent <= MAX_HOURS):
            raise DsrValidationError(f"hours for work package {e.work_package_id} must be above 0 and at most {MAX_HOURS}")
        text = (e.work_done or "").strip()
        if not text:
            raise DsrValidationError(f"work description for work package {e.work_package_id} is required")
        if len(text) > MAX_DESCRIPTION_LENGTH:
            raise DsrValidationError(f"work description for work package {e.work_package_id} is too long")
        if e.new_status_id is not None and e.new_status_id not in allowed_status_ids:
            raise DsrValidationError(
                f"status {e.new_status_id} is not an allowed choice; status updates are "
                f"{'off' if not allowed_status_ids else 'limited to the configured list'}"
            )
        total += e.hours_spent
    if total > MAX_HOURS:
        raise DsrValidationError(f"total hours for the day ({total}) exceed {MAX_HOURS}")


def create_draft(
    session: Session,
    *,
    employee_id: int,
    dsr_date: date,
    entries: list[EntryInput],
    allowed_status_ids: frozenset[int] = frozenset(),
) -> DsrSubmission:
    validate_entries(entries, allowed_status_ids)
    employee = session.get(Employee, employee_id)
    if employee is None or not employee.is_active:
        raise AuthorizationError("employee is missing or inactive")

    current = session.scalar(
        select(DsrSubmission.id).where(
            DsrSubmission.employee_id == employee_id,
            DsrSubmission.dsr_date == dsr_date,
            DsrSubmission.is_current.is_(True),
        )
    )
    if current is not None:
        raise DsrConflictError("a current DSR already exists for this employee and date")

    submission = DsrSubmission(
        employee_id=employee_id,
        dsr_date=dsr_date,
        version=1,
        status="draft",
        is_current=True,
    )
    session.add(submission)
    session.flush()
    for e in entries:
        session.add(DsrEntry(
            submission_id=submission.id,
            work_package_id=e.work_package_id,
            hours_spent=e.hours_spent,
            work_done=e.work_done.strip(),
            remaining_work=(e.remaining_work or None),
            new_status_id=e.new_status_id,
        ))
    session.flush()
    return submission


@dataclass(frozen=True)
class ConfirmResult:
    submission_id: int
    status: str
    enqueued: int
    already_confirmed: bool


def _check_employee_can_sync(session: Session, submission: DsrSubmission) -> None:
    employee = session.get(Employee, submission.employee_id)
    if employee is None or not employee.is_active:
        raise AuthorizationError("employee is missing or inactive")
    if employee.openproject_user_id is None:
        raise AuthorizationError("employee has no OpenProject user ID")


def confirm_submission(
    session: Session, submission_id: int, *, now: datetime, go_live: date | None = None
) -> ConfirmResult:
    """Confirm a draft and enqueue its OpenProject time records, atomically.

    Confirming an already confirmed DSR is a no-op that returns the same status. This makes
    repeated clicks or retries safe.
    """
    submission = session.scalar(
        select(DsrSubmission).where(DsrSubmission.id == submission_id).with_for_update()
    )
    if submission is None:
        raise DsrNotFoundError(f"submission {submission_id} not found")
    if submission.status == "confirmed":
        return ConfirmResult(submission.id, "confirmed", enqueued=0, already_confirmed=True)
    if submission.status != "draft" or not submission.is_current:
        raise DsrConflictError(f"submission is {submission.status}; only a current draft can be confirmed")
    if not submission.entries:
        raise DsrValidationError("cannot confirm a DSR with no entries")

    # Check the employee before changing any state. enqueue_for_submission re-checks the full
    # authorization after the status change, within the same transaction.
    _check_employee_can_sync(session, submission)

    submission.status = "confirmed"
    submission.confirmed_at = now
    session.flush()
    enqueued = enqueue_for_submission(session, submission, go_live=go_live)
    return ConfirmResult(submission.id, "confirmed", enqueued=enqueued, already_confirmed=False)
