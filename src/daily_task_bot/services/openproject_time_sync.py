"""OpenProject sync for DSR entries: time entries and task status changes.

Two targets share one set of safety rules:
- openproject_time:   POST a time entry (hours, description, date)
- openproject_status: PATCH the work package status, only when the employee chose one

Rules applied to every record, in order:
1. Authorization: the submission is confirmed and current, the employee is active, and the
   employee has an OpenProject user ID. The record must belong to that employee.
2. Assignment: the work package exists, is open, and is assigned to the employee.
3. Duplicate protection:
   - time: search for existing entries and check the marker before any POST.
   - status: if the work package already has the target status, nothing is sent.
4. Writes are gated: nothing is sent unless the caller enables writes.
5. Errors are classified. Retryable errors back off, permanent and review errors stop.
   A timeout may hide a completed write, so the next attempt checks before writing again.

The caller controls commits. The runner commits after claiming a batch, and again after each
record, so a crash leaves a lease that expires instead of a stuck record.
"""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from daily_task_bot.clients.openproject import (
    OpenProjectError,
    OpenProjectHttpError,
    OpenProjectTransportError,
    Status,
    TimeEntrySnapshot,
    WorkPackageDetail,
    WritesDisabledError,
)
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.domain.sync_mapping import (
    STATUS_TARGET,
    decide_time_entry_action,
    jibble_fingerprint,
    jibble_hour_idempotency_key,
    openproject_status_idempotency_key,
    openproject_time_entry_payload,
    openproject_time_idempotency_key,
    payload_hash,
    status_change_payload,
)
from daily_task_bot.domain.sync_policy import (
    backoff_seconds,
    classify_http_status,
    safe_error_text,
    should_give_up,
)

TARGET = "openproject_time"
JIBBLE_TARGET = "jibble_time"
OPENPROJECT_TARGETS = (TARGET, STATUS_TARGET)
LEASE = timedelta(minutes=5)
DEFAULT_BATCH = 20


class AuthorizationError(Exception):
    """The DSR or employee is not allowed to produce an OpenProject change."""


class OpenProjectPort(Protocol):
    async def get_statuses(self) -> dict[int, Status]: ...
    async def get_work_package(
        self, work_package_id: int, statuses: dict[int, Status] | None = None
    ) -> WorkPackageDetail | None: ...
    async def find_time_entry_candidates(
        self, *, work_package_id: int, user_id: int, spent_on
    ) -> list[TimeEntrySnapshot]: ...
    async def create_time_entry(self, payload: dict) -> int: ...
    async def update_work_package_status(
        self, work_package_id: int, *, lock_version: int, status_id: int
    ) -> None: ...
    async def get_allowed_status_ids(self, work_package_id: int, *, lock_version: int) -> set[int]: ...


@dataclass
class RecordResult:
    record_id: int
    action: str  # posted | updated | reused | would_post | would_update | skipped_writes_disabled |
    #              retry_wait | failed_permanent | needs_review
    detail: str = ""
    target: str = TARGET


@dataclass
class RunSummary:
    results: list[RecordResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.action] = out.get(r.action, 0) + 1
        return out


# ---- enqueue ------------------------------------------------------------------

def build_entry_payload(entry: DsrEntry, submission: DsrSubmission, activity_id: int | None = None) -> dict:
    return openproject_time_entry_payload(
        dsr_entry_id=entry.id,
        work_package_id=entry.work_package_id,
        spent_on=submission.dsr_date,
        hours=entry.hours_spent,
        description=entry.work_done,
        activity_id=activity_id,
    )


def authorize_submission(session: Session, submission: DsrSubmission) -> Employee:
    """Raise AuthorizationError unless this submission may be synced to OpenProject."""
    if submission.status != "confirmed" or not submission.is_current:
        raise AuthorizationError("submission is not a confirmed, current DSR")
    employee = session.get(Employee, submission.employee_id)
    if employee is None or not employee.is_active:
        raise AuthorizationError("employee is missing or inactive")
    if employee.openproject_user_id is None:
        raise AuthorizationError("employee has no OpenProject user ID")
    return employee


def _has_record(session: Session, entry_id: int, target: str) -> bool:
    return session.scalar(
        select(ExternalSyncRecord.id).where(
            ExternalSyncRecord.dsr_entry_id == entry_id,
            ExternalSyncRecord.target == target,
        )
    ) is not None


def enqueue_for_submission(
    session: Session, submission: DsrSubmission, *, go_live: date | None = None
) -> int:
    """Create pending sync records for a confirmed DSR. Idempotent. Returns new count.

    Forward-only: when go_live is unset, or the DSR is dated before it, nothing is created.
    Historical submissions are never touched.

    Per entry: one OpenProject time record, one OpenProject status record if a status was
    chosen, and one Jibble time record. Jibble records are created but never processed by the
    OpenProject runner. Employees without a Jibble mapping get a Jibble record in needs_review.
    """
    if go_live is None or submission.dsr_date < go_live:
        return 0
    employee = authorize_submission(session, submission)
    created = 0
    for entry in submission.entries:
        if not _has_record(session, entry.id, TARGET):
            payload = build_entry_payload(entry, submission)
            session.add(ExternalSyncRecord(
                dsr_entry_id=entry.id,
                employee_id=employee.id,
                target=TARGET,
                idempotency_key=openproject_time_idempotency_key(entry.id),
                payload_hash=payload_hash(payload),
                status="pending",
            ))
            created += 1
        if entry.new_status_id is not None and not _has_record(session, entry.id, STATUS_TARGET):
            payload = status_change_payload(
                work_package_id=entry.work_package_id, status_id=entry.new_status_id
            )
            session.add(ExternalSyncRecord(
                dsr_entry_id=entry.id,
                employee_id=employee.id,
                target=STATUS_TARGET,
                idempotency_key=openproject_status_idempotency_key(entry.id),
                payload_hash=payload_hash(payload),
                status="pending",
            ))
            created += 1
        if not _has_record(session, entry.id, JIBBLE_TARGET):
            unmapped = employee.jibble_person_id is None
            session.add(ExternalSyncRecord(
                dsr_entry_id=entry.id,
                employee_id=employee.id,
                target=JIBBLE_TARGET,
                idempotency_key=jibble_hour_idempotency_key(entry.id),
                payload_hash=payload_hash(jibble_fingerprint(entry, submission)),
                status="needs_review" if unmapped else "pending",
                last_error_class="review" if unmapped else None,
                last_error="employee has no Jibble mapping" if unmapped else None,
            ))
            created += 1
    session.flush()
    return created


# ---- claim and finish ----------------------------------------------------------

def claim_due_records(
    session: Session, *, now: datetime, worker_id: str, limit: int = DEFAULT_BATCH,
    targets: tuple[str, ...] = OPENPROJECT_TARGETS,
) -> list[ExternalSyncRecord]:
    """Lock and lease due records for the given targets. Locked rows are skipped, not waited on."""
    due = (
        select(ExternalSyncRecord)
        .where(
            ExternalSyncRecord.target.in_(targets),
            ExternalSyncRecord.status.in_(["pending", "retry_wait", "in_progress"]),
            or_(ExternalSyncRecord.next_attempt_at.is_(None),
                ExternalSyncRecord.next_attempt_at <= now),
            or_(ExternalSyncRecord.locked_until.is_(None),
                ExternalSyncRecord.locked_until < now),
        )
        .order_by(ExternalSyncRecord.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    records = list(session.scalars(due))
    for record in records:
        record.status = "in_progress"
        record.locked_until = now + LEASE
        record.locked_by = worker_id
    session.flush()
    return records


def _release(record: ExternalSyncRecord) -> None:
    record.locked_until = None
    record.locked_by = None


def _mark_sent(record: ExternalSyncRecord, external_ref: str, now: datetime) -> None:
    record.status = "sent"
    record.sent_at = now
    record.external_ref = external_ref
    record.last_error_class = None
    record.last_error = None
    record.next_attempt_at = None
    _release(record)


def _mark_failure(record: ExternalSyncRecord, error_class: str, text: str, now: datetime) -> str:
    """Record a failed attempt and return the resulting status."""
    record.attempt_count = (record.attempt_count or 0) + 1
    record.last_error_class = error_class
    record.last_error = safe_error_text(text)
    _release(record)
    if error_class == "retryable":
        if should_give_up(record.attempt_count, record.max_attempts):
            record.status = "failed_permanent"
            record.last_error = safe_error_text(f"gave up after {record.attempt_count} attempts: {text}")
        else:
            record.status = "retry_wait"
            record.next_attempt_at = now + timedelta(seconds=backoff_seconds(record.attempt_count))
    elif error_class == "permanent":
        record.status = "failed_permanent"
        record.next_attempt_at = None
    else:  # review
        record.status = "needs_review"
        record.next_attempt_at = None
    return record.status


def _needs_review(record: ExternalSyncRecord, reason: str) -> str:
    record.status = "needs_review"
    record.last_error_class = "review"
    record.last_error = safe_error_text(reason)
    record.next_attempt_at = None
    _release(record)
    return "needs_review"


def _permanent(record: ExternalSyncRecord, reason: str) -> str:
    record.status = "failed_permanent"
    record.last_error_class = "permanent"
    record.last_error = safe_error_text(reason)
    record.next_attempt_at = None
    _release(record)
    return "failed_permanent"


def _handle_exception(record: ExternalSyncRecord, exc: Exception, now: datetime) -> RecordResult:
    """Shared error handling for both targets. Unknown exceptions are re-raised."""
    if isinstance(exc, WritesDisabledError):
        # Not a failure: nothing was sent. Return the record to pending without counting an attempt.
        record.status = "pending"
        _release(record)
        return RecordResult(record.id, "skipped_writes_disabled", target=record.target)
    if isinstance(exc, OpenProjectHttpError):
        status = _mark_failure(record, classify_http_status(exc.status), str(exc), now)
        return RecordResult(record.id, status, str(exc), target=record.target)
    if isinstance(exc, OpenProjectTransportError):
        # The write may or may not have happened. The next attempt checks before writing again.
        status = _mark_failure(record, "retryable", str(exc), now)
        return RecordResult(record.id, status, str(exc), target=record.target)
    if isinstance(exc, ValueError):
        # The DSR data fails the mapping rules. Retrying won't help.
        status = _permanent(record, f"invalid DSR data: {exc}")
        return RecordResult(record.id, status, str(exc), target=record.target)
    if isinstance(exc, OpenProjectError):
        status = _needs_review(record, str(exc))
        return RecordResult(record.id, status, str(exc), target=record.target)
    raise exc


# ---- process one record --------------------------------------------------------

async def _check_work_package(
    record: ExternalSyncRecord, employee: Employee, client: OpenProjectPort, statuses
) -> tuple[WorkPackageDetail | None, RecordResult | None]:
    """Shared checks: the work package exists, is open, and is assigned to the employee."""
    wp = await client.get_work_package(record.entry.work_package_id, statuses)
    if wp is None:
        status = _permanent(record, "work package not found or not visible")
        return None, RecordResult(record.id, status, target=record.target)
    if wp.status_is_closed:
        status = _permanent(record, "work package is closed")
        return None, RecordResult(record.id, status, target=record.target)
    if wp.assignee_id != employee.openproject_user_id:
        status = _needs_review(record, "work package is not assigned to this employee")
        return None, RecordResult(record.id, status, target=record.target)
    return wp, None


async def _process_time(
    session: Session, record: ExternalSyncRecord, client: OpenProjectPort, employee: Employee,
    *, now: datetime, statuses, dry_run: bool, time_activity_id: int | None,
) -> RecordResult:
    entry = record.entry
    submission = entry.submission
    _, blocked = await _check_work_package(record, employee, client, statuses)
    if blocked is not None:
        return blocked
    if time_activity_id is None:
        # Never post a time entry without the configured activity. A person must decide.
        status = _needs_review(record, "no time-entry activity is configured")
        return RecordResult(record.id, status, "no time-entry activity is configured", target=TARGET)

    record.payload_hash = payload_hash(build_entry_payload(entry, submission))
    payload = build_entry_payload(entry, submission, activity_id=time_activity_id)

    candidates = await client.find_time_entry_candidates(
        work_package_id=entry.work_package_id,
        user_id=employee.openproject_user_id,
        spent_on=submission.dsr_date,
    )
    stored = int(record.external_ref) if record.external_ref else None
    decision = decide_time_entry_action(
        key=record.idempotency_key,
        candidates=[(c.id, c.comment) for c in candidates],
        stored_time_entry_id=stored,
    )
    if decision.action == "reuse":
        _mark_sent(record, str(decision.time_entry_id), now)
        return RecordResult(record.id, "reused", decision.reason, target=TARGET)
    if decision.action == "needs_review":
        status = _needs_review(record, decision.reason)
        return RecordResult(record.id, status, decision.reason, target=TARGET)
    if dry_run:
        return RecordResult(record.id, "would_post", "dry run: no request sent", target=TARGET)
    new_id = await client.create_time_entry(payload)
    _mark_sent(record, str(new_id), now)
    return RecordResult(record.id, "posted", target=TARGET)


async def _process_status(
    session: Session, record: ExternalSyncRecord, client: OpenProjectPort, employee: Employee,
    *, now: datetime, statuses, dry_run: bool, status_choices: frozenset[int],
) -> RecordResult:
    entry = record.entry
    target_status = entry.new_status_id
    if target_status is None:
        status = _permanent(record, "no status was chosen for this entry")
        return RecordResult(record.id, status, target=STATUS_TARGET)
    if target_status not in status_choices:
        # Only the approved DSR statuses are ever sent, even if OpenProject would allow more.
        status = _permanent(record, "status is not in the approved DSR choices")
        return RecordResult(record.id, status, target=STATUS_TARGET)

    wp, blocked = await _check_work_package(record, employee, client, statuses)
    if blocked is not None:
        return blocked
    if not wp.can_update:
        # The API user may not change this work package. Do not try; a person must review.
        status = _needs_review(record, "API user has no permission to update this work package")
        return RecordResult(record.id, status, target=STATUS_TARGET)
    ref = f"{wp.id}:{target_status}"
    record.payload_hash = payload_hash(
        status_change_payload(work_package_id=wp.id, status_id=target_status)
    )

    if wp.status_id == target_status:
        # Already there: either a retry after an unclear response, or nothing to do. Send nothing.
        _mark_sent(record, ref, now)
        return RecordResult(record.id, "reused", "work package already has this status",
                            target=STATUS_TARGET)
    if wp.lock_version is None:
        status = _needs_review(record, "work package lock version unknown; refusing to update")
        return RecordResult(record.id, status, target=STATUS_TARGET)
    # OpenProject's own workflow decides which statuses are allowed for this task right now.
    allowed = await client.get_allowed_status_ids(wp.id, lock_version=wp.lock_version)
    if target_status not in allowed:
        status = _permanent(record, "status is not allowed by the OpenProject workflow for this task")
        return RecordResult(record.id, status, target=STATUS_TARGET)
    if dry_run:
        return RecordResult(record.id, "would_update", "dry run: no request sent",
                            target=STATUS_TARGET)
    await client.update_work_package_status(
        wp.id, lock_version=wp.lock_version, status_id=target_status
    )
    _mark_sent(record, ref, now)
    return RecordResult(record.id, "updated", target=STATUS_TARGET)


async def process_record(
    session: Session,
    record: ExternalSyncRecord,
    client: OpenProjectPort,
    *,
    now: datetime,
    statuses: dict[int, Status] | None = None,
    dry_run: bool = False,
    time_activity_id: int | None = None,
    status_choices: frozenset[int] = frozenset(),
) -> RecordResult:
    """Run every safety check for one leased record and move it to its next status.

    dry_run runs all checks and lookups but never sends a write. The caller must roll back the
    session, because state changes made here are not meant to persist.
    """
    try:
        employee = authorize_submission(session, record.entry.submission)
    except AuthorizationError as exc:
        status = _permanent(record, f"not authorized: {exc}")
        return RecordResult(record.id, status, str(exc), target=record.target)
    if record.employee_id != employee.id:
        status = _permanent(record, "record does not belong to the DSR's employee")
        return RecordResult(record.id, status, target=record.target)

    try:
        if record.target == TARGET:
            return await _process_time(session, record, client, employee, now=now,
                                       statuses=statuses, dry_run=dry_run,
                                       time_activity_id=time_activity_id)
        if record.target == STATUS_TARGET:
            return await _process_status(session, record, client, employee, now=now,
                                         statuses=statuses, dry_run=dry_run,
                                         status_choices=status_choices)
        status = _permanent(record, f"unknown target {record.target}")
        return RecordResult(record.id, status, target=record.target)
    except Exception as exc:  # classified by _handle_exception; unknown errors are re-raised
        return _handle_exception(record, exc, now)


# ---- runner --------------------------------------------------------------------

async def run_time_entry_sync(
    session_factory: sessionmaker,
    client: OpenProjectPort,
    *,
    now: datetime,
    worker_id: str,
    writes_allowed: bool,
    time_activity_id: int | None = None,
    status_choices: frozenset[int] = frozenset(),
    batch_size: int = DEFAULT_BATCH,
) -> RunSummary:
    """Claim a batch of due OpenProject records, then process each in its own transaction."""
    summary = RunSummary()
    if not writes_allowed:
        # Gate closed: do not claim anything, so records stay exactly as they are.
        return summary

    statuses = await client.get_statuses()
    with session_factory() as session:
        claimed = claim_due_records(session, now=now, worker_id=worker_id, limit=batch_size)
        ids = [r.id for r in claimed]
        session.commit()

    for record_id in ids:
        with session_factory() as session:
            record = session.get(ExternalSyncRecord, record_id)
            result = await process_record(session, record, client, now=now, statuses=statuses,
                                          time_activity_id=time_activity_id,
                                          status_choices=status_choices)
            session.commit()
            summary.results.append(result)
    return summary


async def preview_time_entry_sync(
    session_factory: sessionmaker,
    client: OpenProjectPort,
    *,
    now: datetime,
    time_activity_id: int | None = None,
    status_choices: frozenset[int] = frozenset(),
    batch_size: int = DEFAULT_BATCH,
) -> RunSummary:
    """Show what the sync would do for due records, without writing anything.

    Reads only: it runs the checks and lookups, then rolls back each record's changes. It takes
    no lease, so it never disturbs a live run.
    """
    summary = RunSummary()
    statuses = await client.get_statuses()
    with session_factory() as session:
        ids = [
            r.id for r in session.scalars(
                select(ExternalSyncRecord)
                .where(
                    ExternalSyncRecord.target.in_(OPENPROJECT_TARGETS),
                    ExternalSyncRecord.status.in_(["pending", "retry_wait"]),
                    or_(ExternalSyncRecord.next_attempt_at.is_(None),
                        ExternalSyncRecord.next_attempt_at <= now),
                )
                .order_by(ExternalSyncRecord.id)
                .limit(batch_size)
            )
        ]
    for record_id in ids:
        with session_factory() as session:
            record = session.get(ExternalSyncRecord, record_id)
            result = await process_record(
                session, record, client, now=now, statuses=statuses, dry_run=True,
                time_activity_id=time_activity_id, status_choices=status_choices,
            )
            session.rollback()
            summary.results.append(result)
    return summary


def sync_status_for_submission(session: Session, submission_id: int) -> list[dict]:
    """Per-entry sync state for each OpenProject target. Contains no description text."""
    rows = session.scalars(
        select(ExternalSyncRecord)
        .join(DsrEntry, DsrEntry.id == ExternalSyncRecord.dsr_entry_id)
        .where(DsrEntry.submission_id == submission_id,
               ExternalSyncRecord.target.in_(OPENPROJECT_TARGETS))
        .order_by(DsrEntry.id, ExternalSyncRecord.target)
    ).all()
    return [
        {
            "dsr_entry_id": r.dsr_entry_id,
            "target": r.target,
            "status": r.status,
            "attempts": r.attempt_count,
            "last_error_class": r.last_error_class,
            "next_attempt_at": r.next_attempt_at,
            "external_ref": r.external_ref,
        }
        for r in rows
    ]


async def push_status_now(
    session: Session, *, entry: DsrEntry, client: OpenProjectPort, statuses,
    status_choices: frozenset[int], now: datetime,
) -> RecordResult | None:
    """Send one entry's status change to OpenProject straight away, for today's DSR only.

    Creates the status record if it does not exist yet, then runs the same checks as the
    scheduled sync (approved status, assignment, open task, OpenProject workflow, lock version).
    The go-live date does not apply here: it only limits the scheduled backlog sync.
    Returns None when the entry has no status change.
    """
    if entry.new_status_id is None:
        return None
    employee = authorize_submission(session, entry.submission)
    if not _has_record(session, entry.id, STATUS_TARGET):
        payload = status_change_payload(
            work_package_id=entry.work_package_id, status_id=entry.new_status_id
        )
        session.add(ExternalSyncRecord(
            dsr_entry_id=entry.id,
            employee_id=employee.id,
            target=STATUS_TARGET,
            idempotency_key=openproject_status_idempotency_key(entry.id),
            payload_hash=payload_hash(payload),
            status="pending",
        ))
        session.flush()
    record = session.scalar(select(ExternalSyncRecord).where(
        ExternalSyncRecord.dsr_entry_id == entry.id, ExternalSyncRecord.target == STATUS_TARGET))
    result = await process_record(
        session, record, client, now=now, statuses=statuses, dry_run=False,
        status_choices=status_choices,
    )
    if not result.detail:
        # Refusals store their reason on the record, not in the result. Carry it over for the reply.
        result = replace(result, detail=record.last_error or "")
    return result
