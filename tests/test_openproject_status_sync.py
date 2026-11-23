"""OpenProject status sync against the real test database, with a fake OpenProject client.

No network calls. The fake records every status update so tests can prove that nothing is
sent when it shouldn't be. Data is committed by the runner, so each test truncates the test
database afterwards. The guard refuses to run against anything but dtb_test.
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
    WorkPackageDetail,
)
from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine, make_session_factory
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.services.openproject_time_sync import (
    enqueue_for_submission,
    preview_time_entry_sync,
    run_time_entry_sync,
    sync_status_for_submission,
)

ROOT = Path(__file__).resolve().parent.parent
_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None
pytestmark = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
OP_USER = 42
WP = 2481
CURRENT_STATUS = 10   # Tested
TARGET_STATUS = 7     # In progress
TABLES = [
    "external_sync_records", "dsr_entries", "dsr_submissions",
    "daily_plan_items", "daily_plans", "employees",
]


class FakeOpenProject:
    def __init__(self):
        self.statuses = {10: Status(id=10, name="Tested", is_closed=False),
                         7: Status(id=7, name="In progress", is_closed=False)}
        self.wps = {WP: WorkPackageDetail(id=WP, assignee_id=OP_USER, status_is_closed=False,
                                          status_id=CURRENT_STATUS, lock_version=8, can_update=True)}
        self.updates: list[tuple[int, int, int]] = []
        self.update_script: list = []
        self.creates: list[dict] = []
        self.create_script: list = []
        # What OpenProject's update form allows for the task right now (from the #2481 probe).
        self.allowed_ids = {1, 4, 7, 8, 9, 10, 11, 12, 13, 14}

    async def get_statuses(self):
        return self.statuses

    async def get_work_package(self, work_package_id, statuses=None):
        return self.wps.get(work_package_id)

    async def find_time_entry_candidates(self, *, work_package_id, user_id, spent_on):
        return []

    async def create_time_entry(self, payload):
        # Every seeded DSR also has a time record. Record the call and succeed, so the
        # status tests can focus on status behaviour.
        self.creates.append(payload)
        action = self.create_script.pop(0) if self.create_script else None
        if isinstance(action, BaseException):
            raise action
        return 900 + len(self.creates)

    async def get_allowed_status_ids(self, work_package_id, *, lock_version):
        return set(self.allowed_ids)

    async def update_work_package_status(self, work_package_id, *, lock_version, status_id):
        self.updates.append((work_package_id, lock_version, status_id))
        action = self.update_script.pop(0) if self.update_script else None
        if isinstance(action, BaseException):
            raise action
        # Successful update: the fake moves the work package and bumps the lock version.
        current = self.wps[work_package_id]
        self.wps[work_package_id] = WorkPackageDetail(
            id=current.id, assignee_id=current.assignee_id, status_is_closed=False,
            status_id=status_id, lock_version=current.lock_version + 1, can_update=True)


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


def seed(factory, *, wp=WP, new_status_id=TARGET_STATUS, active=True, op_user=OP_USER):
    with factory() as s:
        emp = Employee(name="Test Employee", email=f"s-{uuid.uuid4().hex[:8]}@example.com",
                       openproject_user_id=op_user, is_active=active)
        s.add(emp)
        s.flush()
        sub = DsrSubmission(employee_id=emp.id, dsr_date=date(2026, 11, 19), version=1,
                            status="confirmed", is_current=True, confirmed_at=NOW)
        s.add(sub)
        s.flush()
        entry = DsrEntry(submission_id=sub.id, work_package_id=wp, hours_spent=Decimal("0.25"),
                         work_done="Re-ran extraction", new_status_id=new_status_id)
        s.add(entry)
        s.commit()
        return sub.id, entry.id


def enqueue(factory, submission_id):
    with factory() as s:
        n = enqueue_for_submission(s, s.get(DsrSubmission, submission_id), go_live=date(2026, 1, 1))
        s.commit()
        return n


def status_record(factory, entry_id):
    with factory() as s:
        r = s.scalar(select(ExternalSyncRecord).where(
            ExternalSyncRecord.dsr_entry_id == entry_id,
            ExternalSyncRecord.target == "openproject_status"))
        return None if r is None else {
            "status": r.status, "attempts": r.attempt_count, "ref": r.external_ref,
            "error_class": r.last_error_class, "next_attempt_at": r.next_attempt_at, "id": r.id,
        }


APPROVED_CHOICES = frozenset({1, 4, 7, 8, 9, 10, 11, 12, 13, 14})


def run(factory, op, *, now=NOW, writes=True):
    return asyncio.run(run_time_entry_sync(
        factory, op, now=now, worker_id="test-worker", writes_allowed=writes,
        time_activity_id=4, status_choices=APPROVED_CHOICES))


# ---- enqueue ----------------------------------------------------------------------

def test_status_record_is_created_only_when_a_status_was_chosen(factory):
    sub_with, _ = seed(factory, new_status_id=TARGET_STATUS)
    sub_without, entry_without = seed(factory, new_status_id=None, op_user=43)
    enqueue(factory, sub_with)
    enqueue(factory, sub_without)
    assert status_record(factory, entry_without) is None


def test_enqueue_is_idempotent_for_status_records(factory):
    sub_id, _ = seed(factory)
    assert enqueue(factory, sub_id) == 3  # time + status + Jibble record
    assert enqueue(factory, sub_id) == 0


# ---- success and reuse ---------------------------------------------------------------

def test_status_is_changed_once_with_the_current_lock_version(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    summary = run(factory, op)
    assert summary.counts().get("updated") == 1
    assert op.updates == [(WP, 8, TARGET_STATUS)]
    rec = status_record(factory, entry_id)
    assert rec["status"] == "sent" and rec["ref"] == f"{WP}:{TARGET_STATUS}"


def test_already_in_target_status_is_reused_and_nothing_is_sent(factory):
    """Covers the retry after an unclear response: the first attempt may have worked."""
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.wps[WP] = WorkPackageDetail(id=WP, assignee_id=OP_USER, status_is_closed=False,
                                   status_id=TARGET_STATUS, lock_version=9, can_update=True)
    summary = run(factory, op)
    assert summary.counts().get("reused") == 1
    assert op.updates == []
    assert status_record(factory, entry_id)["status"] == "sent"


def test_status_update_runs_again_only_once_after_success(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    run(factory, op)
    again = run(factory, op, now=NOW + timedelta(days=1))
    assert again.results == [] or all(r.target != "openproject_status" for r in again.results)
    assert len(op.updates) == 1


# ---- gates and assignment ------------------------------------------------------------

def test_writes_disabled_sends_no_status_update(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    run(factory, op, writes=False)
    assert op.updates == []
    assert status_record(factory, entry_id)["status"] == "pending"


def test_status_for_a_task_not_assigned_to_the_employee_goes_to_review(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.wps[WP] = WorkPackageDetail(id=WP, assignee_id=99, status_is_closed=False,
                                   status_id=CURRENT_STATUS, lock_version=8)
    run(factory, op)
    assert op.updates == []
    assert status_record(factory, entry_id)["error_class"] == "review"


def test_closed_task_fails_permanently_without_an_update(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.wps[WP] = WorkPackageDetail(id=WP, assignee_id=OP_USER, status_is_closed=True,
                                   status_id=12, lock_version=8)
    run(factory, op)
    assert op.updates == []
    assert status_record(factory, entry_id)["status"] == "failed_permanent"


# ---- error handling --------------------------------------------------------------------

def test_workflow_rejection_is_permanent_and_not_retried(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.update_script = [OpenProjectHttpError(422, "Validation", "Status is not allowed")]
    run(factory, op)
    assert status_record(factory, entry_id)["status"] == "failed_permanent"
    run(factory, op, now=NOW + timedelta(days=1))
    assert len(op.updates) == 1


def test_conflict_409_is_retried_with_a_fresh_read(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.update_script = [OpenProjectHttpError(409, "UpdateConflict", "stale"), None]
    first = run(factory, op)
    assert first.counts().get("retry_wait") == 1
    assert status_record(factory, entry_id)["status"] == "retry_wait"
    second = run(factory, op, now=NOW + timedelta(seconds=60))
    assert second.counts().get("updated") == 1
    assert len(op.updates) == 2
    assert op.updates[1][1] == 8, "the retry must use the lock version it re-read"


def test_timeout_during_update_is_recovered_by_the_status_check(factory):
    """The update may have worked before the timeout. The retry sees the status and sends nothing."""
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()

    async def update_then_time_out(work_package_id, *, lock_version, status_id):
        op.updates.append((work_package_id, lock_version, status_id))
        op.wps[work_package_id] = WorkPackageDetail(
            id=work_package_id, assignee_id=OP_USER, status_is_closed=False,
            status_id=status_id, lock_version=lock_version + 1, can_update=True)
        raise OpenProjectTransportError("read timeout")

    op.update_work_package_status = update_then_time_out
    assert run(factory, op).counts().get("retry_wait") == 1
    assert run(factory, op, now=NOW + timedelta(seconds=60)).counts().get("reused") == 1
    assert len(op.updates) == 1, "a second PATCH would repeat a write that already succeeded"


def test_unknown_lock_version_is_refused_for_review(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.wps[WP] = WorkPackageDetail(id=WP, assignee_id=OP_USER, status_is_closed=False,
                                   status_id=CURRENT_STATUS, lock_version=None)
    run(factory, op)
    assert op.updates == []
    assert status_record(factory, entry_id)["error_class"] == "review"


# ---- preview ---------------------------------------------------------------------------

def test_preview_reports_would_update_and_changes_nothing(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    summary = asyncio.run(preview_time_entry_sync(factory, op, now=NOW,
                                                  status_choices=APPROVED_CHOICES))
    assert summary.counts().get("would_update") == 1
    assert op.updates == []
    assert status_record(factory, entry_id)["status"] == "pending"


def test_status_change_without_update_permission_goes_to_review_and_is_not_attempted(factory):
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.wps[WP] = WorkPackageDetail(id=WP, assignee_id=OP_USER, status_is_closed=False,
                                   status_id=CURRENT_STATUS, lock_version=8, can_update=False)
    run(factory, op)
    assert op.updates == []
    assert status_record(factory, entry_id)["error_class"] == "review"


def test_hours_and_status_sync_fail_independently(factory):
    """A failing time-entry POST must not block the status change for the same DSR entry."""
    from daily_task_bot.clients.openproject import OpenProjectHttpError as HttpErr
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.create_script = [HttpErr(500, None, "time service down")]
    summary = run(factory, op)
    assert summary.counts().get("updated") == 1
    assert op.updates == [(WP, 8, TARGET_STATUS)]
    with factory() as s:
        rows = {r["target"]: r["status"] for r in sync_status_for_submission(s, sub_id)}
    assert rows == {"openproject_time": "retry_wait", "openproject_status": "sent"}


def test_status_failure_does_not_block_the_time_entry(factory):
    from daily_task_bot.clients.openproject import OpenProjectHttpError as HttpErr
    sub_id, entry_id = seed(factory)
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.update_script = [HttpErr(422, "Validation", "status not allowed by workflow")]
    summary = run(factory, op)
    assert summary.counts().get("posted") == 1
    with factory() as s:
        rows = {r["target"]: r["status"] for r in sync_status_for_submission(s, sub_id)}
    assert rows == {"openproject_time": "sent", "openproject_status": "failed_permanent"}


def test_status_outside_the_approved_choices_is_refused_without_calling_openproject(factory):
    sub_id, entry_id = seed(factory, new_status_id=2)  # 2 = "In specification", not approved
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    run(factory, op)
    assert op.updates == []
    assert status_record(factory, entry_id)["status"] == "failed_permanent"


def test_status_not_allowed_by_the_workflow_for_this_task_is_refused_before_patch(factory):
    sub_id, entry_id = seed(factory)  # target 7
    enqueue(factory, sub_id)
    op = FakeOpenProject()
    op.allowed_ids = {10, 11}  # workflow no longer allows "In progress" from here
    run(factory, op)
    assert op.updates == []
    rec = status_record(factory, entry_id)
    assert rec["status"] == "failed_permanent"


def test_choices_are_the_intersection_of_approved_and_allowed_statuses():
    import asyncio as _asyncio
    from daily_task_bot.services.status_choices import choices_for_work_package
    op = FakeOpenProject()
    op.allowed_ids = {7, 11, 2}  # 2 is allowed by the workflow but not approved
    statuses = {i: Status(id=i, name=f"S{i}", is_closed=False) for i in (2, 7, 11)}
    out = _asyncio.run(choices_for_work_package(
        op, work_package_id=WP, lock_version=8,
        approved_status_ids=frozenset({7, 11, 12}), statuses=statuses))
    assert out == [(11, "S11"), (7, "S7")]  # sorted by name


def test_no_approved_statuses_means_no_choices_and_no_lookup():
    import asyncio as _asyncio
    from daily_task_bot.services.status_choices import choices_for_work_package
    op = FakeOpenProject()
    out = _asyncio.run(choices_for_work_package(
        op, work_package_id=WP, lock_version=8, approved_status_ids=frozenset(), statuses={}))
    assert out == []
