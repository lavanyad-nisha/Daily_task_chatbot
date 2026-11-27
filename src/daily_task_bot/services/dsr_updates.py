"""Change one task in today's DSR from a chat command. Changes are saved and queued for sync.

Rules:
- The first change of the day creates today's submission and confirms it, so sync is queued.
- Later changes update the same task's entry. New tasks are added to the day's submission.
- A task whose records have already reached OpenProject or Jibble cannot be changed yet. The
  employee is told so, and nothing is altered.
- The day's total hours may not exceed 24.
- Nothing is written to OpenProject or Jibble here. The scheduled sync does that.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.domain.sync_mapping import MAX_HOURS, payload_hash, status_change_payload
from daily_task_bot.services.dsr import (
    DsrConflictError,
    DsrValidationError,
    EntryInput,
    confirm_submission,
    create_draft,
)
from daily_task_bot.services.jibble_hour_sync import JIBBLE_TARGET
from daily_task_bot.services.openproject_time_sync import (
    STATUS_TARGET,
    TARGET,
    build_entry_payload,
    enqueue_for_submission,
)

SENT_STATES = ("sent", "in_progress")
LOCKED_MESSAGE = (
    "That task has already been sent to OpenProject or Jibble. Changes to sent entries "
    "are not supported yet, so your update was not saved."
)


@dataclass(frozen=True)
class TaskChange:
    work_package_id: int
    hours: Decimal | None = None
    work_done: str | None = None
    status_id: int | None = None


def _todays_submission(session: Session, employee: Employee, day: date) -> DsrSubmission | None:
    return session.scalar(select(DsrSubmission).where(
        DsrSubmission.employee_id == employee.id,
        DsrSubmission.dsr_date == day,
        DsrSubmission.is_current.is_(True),
    ))


def _records_for(session: Session, entry_id: int) -> list[ExternalSyncRecord]:
    return list(session.scalars(select(ExternalSyncRecord).where(ExternalSyncRecord.dsr_entry_id == entry_id)))


def _refresh_hashes(session: Session, submission: DsrSubmission, entry: DsrEntry) -> None:
    """Keep pending records' fingerprints in step with the entry, so the sync sends the new values."""
    for record in _records_for(session, entry.id):
        if record.status in SENT_STATES:
            continue
        if record.target == TARGET:
            record.payload_hash = payload_hash(build_entry_payload(entry, submission))
        elif record.target == STATUS_TARGET and entry.new_status_id is not None:
            record.payload_hash = payload_hash(status_change_payload(
                work_package_id=entry.work_package_id, status_id=entry.new_status_id))
        elif record.target == JIBBLE_TARGET:
            from daily_task_bot.domain.sync_mapping import jibble_fingerprint
            record.payload_hash = payload_hash(jibble_fingerprint(entry, submission))


def apply_task_change(
    session: Session,
    *,
    employee: Employee,
    today: date,
    change: TaskChange,
    allowed_status_ids: frozenset[int],
    go_live: date | None,
    now: datetime,
) -> str:
    """Apply one change. Returns the message to show the employee. The caller commits."""
    if change.status_id is not None and change.status_id not in allowed_status_ids:
        return "That status is not on the approved list. Use a status number from the approved choices."

    submission = _todays_submission(session, employee, today)
    if submission is None:
        if change.hours is None or change.work_done is None:
            return "Record hours and work first with UPDATE [task] [hours] [work done]."
        try:
            submission = create_draft(
                session, employee_id=employee.id, dsr_date=today,
                entries=[EntryInput(change.work_package_id, change.hours, change.work_done, None,
                                    change.status_id)],
                allowed_status_ids=allowed_status_ids,
            )
            confirm_submission(session, submission.id, now=now, go_live=go_live)
        except (DsrValidationError, DsrConflictError) as exc:
            return f"Nothing was saved: {exc}"
        return f"Saved task #{change.work_package_id} for {today.isoformat()}. It will sync within a few minutes."

    entry = next((e for e in submission.entries if e.work_package_id == change.work_package_id), None)
    if entry is not None and any(r.status in SENT_STATES for r in _records_for(session, entry.id)):
        return LOCKED_MESSAGE

    if change.hours is not None:
        others = sum((e.hours_spent for e in submission.entries
                      if e.work_package_id != change.work_package_id), Decimal("0"))
        if others + change.hours > MAX_HOURS:
            return f"Nothing was saved: the day's total would be more than {MAX_HOURS} hours."

    if entry is None:
        if change.hours is None or change.work_done is None:
            return "Record hours and work for this task first with UPDATE [task] [hours] [work done]."
        entry = DsrEntry(submission_id=submission.id, work_package_id=change.work_package_id,
                         hours_spent=change.hours, work_done=change.work_done,
                         new_status_id=change.status_id)
        session.add(entry)
        session.flush()
        submission.entries.append(entry)
    else:
        if change.hours is not None:
            entry.hours_spent = change.hours
        if change.work_done is not None:
            entry.work_done = change.work_done
        if change.status_id is not None:
            entry.new_status_id = change.status_id
        session.flush()

    if submission.status == "draft":
        confirm_submission(session, submission.id, now=now, go_live=go_live)
    else:
        enqueue_for_submission(session, submission, go_live=go_live)
    _refresh_hashes(session, submission, entry)
    session.flush()
    return f"Saved task #{change.work_package_id}. It will sync within a few minutes."
