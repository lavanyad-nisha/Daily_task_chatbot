"""OpenProject time-entry sync against the real test database, with a fake OpenProject client.

No network calls are made. The fake records every POST so tests can prove duplicates
are never created. Data is committed (the runner commits), so each test truncates the
test database afterwards. The database guard refuses to run against anything but dtb_test.
"""

import argparse
import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from daily_task_bot.clients.openproject import (
    OpenProjectHttpError,
    OpenProjectTransportError,
    Status,
    TimeEntrySnapshot,
    WorkPackageDetail,
)
from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine, make_session_factory
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.domain.sync_mapping import openproject_time_idempotency_key
from daily_task_bot.services.openproject_time_sync import (
    AuthorizationError,
    claim_due_records,
    enqueue_for_submission,
    run_time_entry_sync,
    sync_status_for_submission,
)

ROOT = Path(__file__).resolve().parent.parent
_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None
pytestmark = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
OP_USER = 42
WP = 1809
TABLES = [
    "external_sync_records", "dsr_entries", "dsr_submissions",
    "daily_plan_items", "daily_plans", "employees",
]


class FakeOpenProject:
    """Scripted stand-in for OpenProjectClient. Records every write attempt."""

    def __init__(self):
        self.statuses = {1: Status(id=1, name="New", is_closed=False)}
        self.wps: dict[int, WorkPackageDetail] = {
            WP: WorkPackageDetail(id=WP, assignee_id=OP_USER, status_is_closed=False)
        }
        self.candidates: list[TimeEntrySnapshot] = []
        self.create_script: list = []  # each item: an exception to raise, or None to succeed
        self.creates: list[dict] = []
        self.next_id = 900

    async def get_statuses(self):
        return self.statuses

    async def get_work_package(self, work_package_id, statuses=None):
        return self.wps.get(work_package_id)

    async def find_time_entry_candidates(self, *, work_package_id, user_id, spent_on):
        return list(self.candidates)

    async def create_time_entry(self, payload):
        self.creates.append(payload)
        action = self.create_script.pop(0) if self.create_script else None
        if isinstance(action, BaseException):
            raise action
        self.next_id += 1
        return self.next_id


@pytest.fixture
def engine():
    assert_test_database(TEST_URL)
    eng = make_engine(TEST_URL)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={TEST_URL}"])
    command.upgrade(cfg, "head")
    yield eng
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    eng.dispose()


@pytest.fixture
def factory(engine):
    return make_session_factory(engine)


def seed(factory, *, active=True, op_user=OP_USER, status="confirmed", hours="2.5",
         work_done="Fixed login bug", wp=WP, max_attempts=5):
    """Create committed employee, submission and one entry. Returns (submission_id, entry_id)."""
    with factory() as s:
        emp = Employee(name="Test Employee", email=f"t-{uuid.uuid4().hex[:8]}@example.com",
                       openproject_user_id=op_user, is_active=active)
        s.add(emp)
        s.flush()
        confirmed = status == "confirmed"
        sub = DsrSubmission(employee_id=emp.id, dsr_date=date(2026, 11, 19), version=1,
                            status=status, is_current=status != "superseded",
                            confirmed_at=NOW if confirmed else None)
        s.add(sub)
        s.flush()
        entry = DsrEntry(submission_id=sub.id, work_package_id=wp,
                         hours_spent=Decimal(hours), work_done=work_done)
        s.add(entry)
        s.commit()
        return sub.id, entry.id, emp.id


def enqueue(factory, submission_id):
    with factory() as s:
        sub = s.get(DsrSubmission, submission_id)
        n = enqueue_for_submission(s, sub, go_live=date(2026, 1, 1))
        s.commit()
        return n


def record_for(factory, entry_id):
    with factory() as s:
        r = s.scalar(select(ExternalSyncRecord).where(
            ExternalSyncRecord.dsr_entry_id == entry_id,
            ExternalSyncRecord.target == "openproject_time"))
        return {"status": r.status, "attempts": r.attempt_count, "ref": r.external_ref,
                "error_class": r.last_error_class, "next_attempt_at": r.next_attempt_at,
                "locked_until": r.locked_until, "locked_by": r.locked_by, "id": r.id}


ACTIVITY_ID = 4  # "Testing" on the live instance


def run(factory, op, *, now=NOW, writes=True, activity=ACTIVITY_ID):
    return asyncio.run(run_time_entry_sync(
        factory, op, now=now, worker_id="test-worker", writes_allowed=writes,
        time_activity_id=activity))


# ---- enqueue and authorization ------------------------------------------------------

def test_enqueue_creates_one_pending_record_per_entry_and_is_idempotent(factory):
    sub_id, entry_id, _ = seed(factory)
    assert enqueue(factory, sub_id) == 2  # time + Jibble record
    assert enqueue(factory, sub_id) == 0
    rec = record_for(factory, entry_id)
    assert rec["status"] == "pending" and rec["attempts"] == 0


def test_draft_submission_cannot_be_enqueued(factory):
    sub_id, _, _ = seed(factory, status="draft")
    with factory() as s:
        with pytest.raises(AuthorizationError):
            enqueue_for_submission(s, s.get(DsrSubmission, sub_id), go_live=date(2026, 1, 1))


def test_inactive_employee_cannot_be_enqueued(factory):
    sub_id, _, _ = seed(factory, active=False)
    with factory() as s:
        with pytest.raises(AuthorizationError):
            enqueue_for_submission(s, s.get(DsrSubmission, sub_id), go_live=date(2026, 1, 1))


def test_employee_without_openproject_id_cannot_be_enqueued(factory):
    sub_id, _, _ = seed(factory, op_user=None)
    with factory() as s:
        with pytest.raises(AuthorizationError):
            enqueue_for_submission(s, s.get(DsrSubmission, sub_id), go_live=date(2026, 1, 1))


# ---- happy path and gates -----------------------------------------------------------

def test_confirmed_entry_is_posted_once_and_marked_sent(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    summary = run(factory, op)
    assert summary.counts() == {"posted": 1}
    assert len(op.creates) == 1
    assert op.creates[0]["_links"]["entity"]["href"] == f"/api/v3/work_packages/{WP}"
    assert op.creates[0]["hours"] == "PT2H30M"
    assert op.creates[0]["_links"]["activity"] == {"href": f"/api/v3/time_entries/activities/{ACTIVITY_ID}"}
    rec = record_for(factory, entry_id)
    assert rec["status"] == "sent" and rec["ref"] == "901" and rec["locked_until"] is None


def test_sent_record_is_never_posted_again(factory):
    sub_id, _, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    run(factory, op)
    later = run(factory, op, now=NOW + timedelta(days=1))
    assert later.results == []
    assert len(op.creates) == 1


def test_writes_disabled_claims_nothing_and_sends_nothing(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    summary = run(factory, op, writes=False)
    assert summary.results == [] and op.creates == []
    assert record_for(factory, entry_id)["status"] == "pending"


def test_writes_disabled_inside_client_returns_record_to_pending_without_attempt(factory):
    from daily_task_bot.clients.openproject import WritesDisabledError

    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.create_script = [WritesDisabledError("off")]
    summary = run(factory, op)
    assert summary.counts() == {"skipped_writes_disabled": 1}
    rec = record_for(factory, entry_id)
    assert rec["status"] == "pending" and rec["attempts"] == 0


# ---- duplicate protection -----------------------------------------------------------

def test_existing_entry_with_marker_is_reused_not_duplicated(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    key = openproject_time_idempotency_key(entry_id)
    op = FakeOpenProject()
    op.candidates = [TimeEntrySnapshot(id=555, comment=f"done\n[{key}]", hours="PT2H30M",
                                       spent_on="2026-11-19", work_package_id=WP, user_id=OP_USER)]
    summary = run(factory, op)
    assert summary.counts() == {"reused": 1}
    assert op.creates == []
    assert record_for(factory, entry_id)["ref"] == "555"


def test_timeout_after_post_is_recovered_on_retry_without_a_second_post(factory):
    """The POST may have created the entry before the timeout. The retry must find it first."""
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    key = openproject_time_idempotency_key(entry_id)
    op = FakeOpenProject()

    async def create_then_time_out(payload):
        op.creates.append(payload)
        # The entry exists on the server, but the response never arrived.
        op.candidates.append(TimeEntrySnapshot(
            id=777, comment=payload["comment"]["raw"], hours=payload["hours"],
            spent_on=payload["spentOn"], work_package_id=WP, user_id=OP_USER))
        raise OpenProjectTransportError("read timeout")

    op.create_time_entry = create_then_time_out
    first = run(factory, op)
    assert first.counts() == {"retry_wait": 1}
    assert record_for(factory, entry_id)["status"] == "retry_wait"

    second = run(factory, op, now=NOW + timedelta(seconds=60))
    assert second.counts() == {"reused": 1}
    assert len(op.creates) == 1, "a second POST would have duplicated the entry"
    rec = record_for(factory, entry_id)
    assert rec["status"] == "sent" and rec["ref"] == "777"
    assert key in op.candidates[0].comment


# ---- authorization and assignment ---------------------------------------------------

def test_work_package_not_assigned_to_employee_needs_review_and_is_not_posted(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.wps[WP] = WorkPackageDetail(id=WP, assignee_id=99, status_is_closed=False)
    assert run(factory, op).counts() == {"needs_review": 1}
    assert op.creates == []
    assert record_for(factory, entry_id)["error_class"] == "review"


def test_closed_work_package_fails_permanently(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.wps[WP] = WorkPackageDetail(id=WP, assignee_id=OP_USER, status_is_closed=True)
    assert run(factory, op).counts() == {"failed_permanent": 1}
    assert op.creates == []


def test_missing_work_package_fails_permanently(factory):
    sub_id, entry_id, _ = seed(factory, wp=4040)
    enqueue(factory, sub_id)
    op = FakeOpenProject()  # no work package 4040 known to the fake
    assert run(factory, op).counts() == {"failed_permanent": 1}
    assert op.creates == []


def test_inactive_employee_after_enqueue_is_blocked_before_any_openproject_call(factory):
    sub_id, entry_id, emp_id = seed(factory)
    enqueue(factory, sub_id)
    with factory() as s:
        s.get(Employee, emp_id).is_active = False
        s.commit()
    op = FakeOpenProject()
    assert run(factory, op).counts() == {"failed_permanent": 1}
    assert op.creates == []


# ---- error handling and retries -----------------------------------------------------

def test_server_error_schedules_retry_with_backoff(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.create_script = [OpenProjectHttpError(500, "InternalError", "boom")]
    assert run(factory, op).counts() == {"retry_wait": 1}
    rec = record_for(factory, entry_id)
    assert rec["attempts"] == 1
    assert rec["next_attempt_at"] == NOW + timedelta(seconds=30)


def test_record_is_not_retried_before_its_backoff_expires(factory):
    sub_id, _, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.create_script = [OpenProjectHttpError(503, None, "busy")]
    run(factory, op)
    too_early = run(factory, op, now=NOW + timedelta(seconds=10))
    assert too_early.results == []
    due = run(factory, op, now=NOW + timedelta(seconds=31))
    assert due.counts() == {"posted": 1}


def test_gives_up_after_max_attempts(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    with factory() as s:
        s.get(ExternalSyncRecord, record_for(factory, entry_id)["id"]).max_attempts = 2
        s.commit()
    op = FakeOpenProject()
    op.create_script = [OpenProjectHttpError(500, None, "x"), OpenProjectHttpError(500, None, "x")]
    run(factory, op)
    run(factory, op, now=NOW + timedelta(hours=1))
    rec = record_for(factory, entry_id)
    assert rec["status"] == "failed_permanent" and rec["attempts"] == 2


def test_validation_error_is_permanent_and_not_retried(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.create_script = [OpenProjectHttpError(422, "Validation", "Hours can't be blank")]
    assert run(factory, op).counts() == {"failed_permanent": 1}
    assert run(factory, op, now=NOW + timedelta(days=1)).results == []
    assert record_for(factory, entry_id)["error_class"] == "permanent"


def test_authentication_error_needs_review_and_is_not_retried(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.create_script = [OpenProjectHttpError(401, "Unauthenticated", "no")]
    assert run(factory, op).counts() == {"needs_review": 1}
    assert run(factory, op, now=NOW + timedelta(days=1)).results == []


def test_error_text_stored_for_a_record_contains_no_description(factory):
    sub_id, entry_id, _ = seed(factory, work_done="SECRET-WORK-NOTE")
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.create_script = [OpenProjectHttpError(422, "Validation", "Hours can't be blank")]
    run(factory, op)
    with factory() as s:
        rec = s.get(ExternalSyncRecord, record_for(factory, entry_id)["id"])
        assert "SECRET-WORK-NOTE" not in (rec.last_error or "")


# ---- leases and crash recovery ------------------------------------------------------

def test_record_leased_by_another_worker_is_not_claimed(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    with factory() as s:
        rec = s.scalar(select(ExternalSyncRecord))
        rec.status = "in_progress"
        rec.locked_until = NOW + timedelta(minutes=3)
        rec.locked_by = "other-worker"
        s.commit()
    op = FakeOpenProject()
    assert run(factory, op).results == []
    assert op.creates == []


def test_expired_lease_from_a_crashed_worker_is_recovered(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    with factory() as s:
        rec = s.scalar(select(ExternalSyncRecord))
        rec.status = "in_progress"
        rec.locked_until = NOW - timedelta(minutes=1)
        rec.locked_by = "crashed-worker"
        s.commit()
    op = FakeOpenProject()
    assert run(factory, op).counts() == {"posted": 1}


def test_claim_uses_skip_locked_semantics_in_sql(factory):
    """Records are claimed with FOR UPDATE SKIP LOCKED, so concurrent workers never block."""
    sub_id, _, _ = seed(factory)
    enqueue(factory, sub_id)
    with factory() as s:
        compiled = str(
            select(ExternalSyncRecord).with_for_update(skip_locked=True)
            .compile(dialect=s.bind.dialect)
        )
    assert "SKIP LOCKED" in compiled
    with factory() as s:
        claimed = claim_due_records(s, now=NOW, worker_id="w1")
        assert len(claimed) == 1
        s.commit()


# ---- status reporting ---------------------------------------------------------------

def test_sync_status_reports_per_entry_state_without_description_text(factory):
    sub_id, entry_id, _ = seed(factory, work_done="PRIVATE-NOTE")
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    run(factory, op)
    with factory() as s:
        rows = sync_status_for_submission(s, sub_id)
    assert rows == [{"dsr_entry_id": entry_id, "target": "openproject_time", "status": "sent",
                     "attempts": 0, "last_error_class": None, "next_attempt_at": None,
                     "external_ref": "901"}]
    assert "PRIVATE-NOTE" not in str(rows)


def test_one_failing_record_does_not_block_others(factory):
    sub_id, _, _ = seed(factory)
    with factory() as s:
        sub = s.get(DsrSubmission, sub_id)
        second = DsrEntry(submission_id=sub.id, work_package_id=1810,
                          hours_spent=Decimal("1"), work_done="Second task")
        s.add(second)
        s.commit()
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.wps[1810] = WorkPackageDetail(id=1810, assignee_id=OP_USER, status_is_closed=False)
    op.create_script = [OpenProjectHttpError(500, None, "boom"), None]
    summary = run(factory, op)
    assert sorted(summary.counts().items()) == [("posted", 1), ("retry_wait", 1)]


def test_no_configured_activity_means_review_and_nothing_is_posted(factory):
    sub_id, entry_id, _ = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    summary = run(factory, op, activity=None)
    assert summary.counts() == {"needs_review": 1}
    assert op.creates == []
