"""Jibble HourEntry sync for new confirmed DSR entries. Reuses external_sync_records.

Order of checks for each record:
1. Authorization (same as OpenProject: confirmed, current, active employee).
2. Mapping: the employee has a Jibble person ID, and the project and activity are configured.
   Nothing is invented: unset configuration goes to review.
3. Duplicate detection: list the person's HourEntries for the DSR date (verified read filter).
   Any entry with the same project, activity and duration goes to review. Nothing is written.
4. Contract gate: no create is sent unless the HourEntry create contract has been verified and
   reviewed (HOUR_ENTRY_CREATE_CONTRACT_VERIFIED). Until then, records go to review.
5. Remote ID: a successful create stores the Jibble ID in external_ref, so retries can't duplicate.

Writes are never performed by this module while the contract gate is closed.
"""

import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

import httpx

from daily_task_bot.clients.jibble import (
    HOUR_ENTRY_CREATE_CONTRACT_VERIFIED,
    JibbleError,
    JibbleWriteNotVerified,
)
from daily_task_bot.db.models import ExternalSyncRecord
from daily_task_bot.domain.sync_mapping import (
    jibble_fingerprint,
    jibble_hour_entry_uuid,
    payload_hash,
)
from daily_task_bot.domain.sync_policy import classify_http_status
from daily_task_bot.services.openproject_time_sync import (
    DEFAULT_BATCH,
    AuthorizationError,
    RecordResult,
    RunSummary,
    _handle_exception,
    _mark_failure,
    _mark_sent,
    _needs_review,
    _permanent,
    _release,
    authorize_submission,
    claim_due_records,
)

JIBBLE_TARGET = "jibble_time"
_DURATION = re.compile(
    r"^-?P(?:(\d+)D)?T?(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$"
)
MINUTE_TOLERANCE_HOURS = 1 / 60


def duration_hours(value: str | None) -> float | None:
    """Parse an ISO 8601 duration such as PT15M or PT0H15M into hours. None if unreadable."""
    if not value:
        return None
    match = _DURATION.match(value)
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = match.groups()
    return ((int(days or 0) * 24) + float(hours or 0) + float(minutes or 0) / 60
            + float(seconds or 0) / 3600)


async def process_jibble_record(
    session: Session,
    record: ExternalSyncRecord,
    client: Any,
    *,
    now: datetime,
    hour_project_id: str | None,
    hour_activity_id: str | None,
    contract_verified: bool = HOUR_ENTRY_CREATE_CONTRACT_VERIFIED,
) -> RecordResult:
    entry = record.entry
    submission = entry.submission
    try:
        employee = authorize_submission(session, submission)
    except AuthorizationError as exc:
        status = _permanent(record, f"not authorized: {exc}")
        return RecordResult(record.id, status, str(exc), target=JIBBLE_TARGET)
    if record.employee_id != employee.id:
        status = _permanent(record, "record does not belong to the DSR's employee")
        return RecordResult(record.id, status, record.last_error or "", target=JIBBLE_TARGET)

    if not employee.jibble_person_id:
        status = _needs_review(record, "employee has no Jibble mapping")
        return RecordResult(record.id, status, record.last_error or "", target=JIBBLE_TARGET)
    if not hour_project_id or not hour_activity_id:
        status = _needs_review(record, "Jibble project and activity are not configured")
        return RecordResult(record.id, status, record.last_error or "", target=JIBBLE_TARGET)

    try:
        wanted_hours = float(entry.hours_spent)
        record.payload_hash = payload_hash(jibble_fingerprint(entry, submission))
        existing = await client.list_hour_entries_for_day(
            person_id=employee.jibble_person_id, day=submission.dsr_date.isoformat()
        )
        same = [
            e for e in existing
            if e.get("projectId") == hour_project_id
            and e.get("activityId") == hour_activity_id
            and (duration_hours(e.get("duration")) is not None)
            and abs(duration_hours(e.get("duration")) - wanted_hours) <= MINUTE_TOLERANCE_HOURS
        ]
        entry_uuid = jibble_hour_entry_uuid(entry.id)
        if any(str(e.get("id")) == entry_uuid for e in existing):
            # A previous attempt created this exact entry (for example, the response was lost).
            _mark_sent(record, entry_uuid, now)
            return RecordResult(record.id, "reused", "entry with the stable id already exists",
                                target=JIBBLE_TARGET)
        if same:
            status = _needs_review(record, "a matching Jibble entry already exists for this day; confirm before writing")
            return RecordResult(record.id, status, record.last_error or "", target=JIBBLE_TARGET)
        if not contract_verified:
            status = _needs_review(record, "Jibble HourEntry create contract is not verified; no request sent")
            return RecordResult(record.id, status, record.last_error or "", target=JIBBLE_TARGET)
        remote = await client.create_hour_entry(
            entry_id=entry_uuid,
            person_id=employee.jibble_person_id,
            day=submission.dsr_date,
            hours=entry.hours_spent,
            project_id=hour_project_id,
            activity_id=hour_activity_id,
            note=entry.work_done,
        )
        _mark_sent(record, remote.id, now)
        return RecordResult(record.id, "posted", target=JIBBLE_TARGET)
    except JibbleWriteNotVerified as exc:
        # Gate closed: nothing was sent, so the record stays pending without an attempt.
        record.status = "pending"
        _release(record)
        return RecordResult(record.id, "skipped_writes_disabled", str(exc), target=JIBBLE_TARGET)
    except JibbleError as exc:
        # Unknown outcome on a lost response is handled by the stable-id check on the next attempt.
        error_class = classify_http_status(exc.status) if exc.status else "review"
        status = _mark_failure(record, error_class, str(exc), now)
        return RecordResult(record.id, status, str(exc), target=JIBBLE_TARGET)
    except httpx.TransportError as exc:
        status = _mark_failure(record, "retryable", type(exc).__name__, now)
        return RecordResult(record.id, status, type(exc).__name__, target=JIBBLE_TARGET)
    except Exception as exc:  # classified by the shared handler; unknown errors are re-raised
        return _handle_exception(record, exc, now)


async def run_jibble_sync(
    session_factory: sessionmaker,
    client: Any,
    *,
    now: datetime,
    worker_id: str,
    writes_allowed: bool,
    hour_project_id: str | None,
    hour_activity_id: str | None,
    batch_size: int = DEFAULT_BATCH,
) -> RunSummary:
    """Process due Jibble records. Does nothing while writes are not allowed."""
    summary = RunSummary()
    if not writes_allowed:
        return summary
    with session_factory() as session:
        claimed = claim_due_records(session, now=now, worker_id=worker_id,
                                    limit=batch_size, targets=(JIBBLE_TARGET,))
        ids = [r.id for r in claimed]
        session.commit()
    for record_id in ids:
        with session_factory() as session:
            record = session.get(ExternalSyncRecord, record_id)
            result = await process_jibble_record(
                session, record, client, now=now,
                hour_project_id=hour_project_id, hour_activity_id=hour_activity_id,
            )
            session.commit()
            summary.results.append(result)
    return summary
