"""Jibble HourEntry sync: mapping, duplicate detection, contract gate and retries. Rolled back."""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from daily_task_bot.clients.jibble import JibbleError
from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.services.jibble_hour_sync import process_jibble_record
from daily_task_bot.services.openproject_time_sync import claim_due_records

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
PROJECT = "11111111-1111-1111-1111-111111111111"
ACTIVITY = "22222222-2222-2222-2222-222222222222"
PERSON = "33333333-3333-3333-3333-333333333333"
_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None


class FakeJibble:
    """Records every call. Stands in for the client only; no network."""

    def __init__(self, existing=None, fail_list=False, create_errors=None):
        self.existing = existing or []
        self.fail_list = fail_list
        self.list_calls = 0
        self.create_calls = 0
        self.create_args = []
        self.create_errors = list(create_errors or [])

    async def list_hour_entries_for_day(self, *, person_id, day):
        self.list_calls += 1
        if self.fail_list:
            raise JibbleError("list hour entries", 500)
        return list(self.existing)

    async def create_hour_entry(self, **kwargs):
        self.create_calls += 1
        self.create_args.append(kwargs)
        if self.create_errors:
            raise self.create_errors.pop(0)
        from daily_task_bot.domain.jibble_hour_entry import RemoteHourEntry
        return RemoteHourEntry(id=kwargs["entry_id"], person_id=kwargs["person_id"], date=None,
                               duration=None, project_id=kwargs["project_id"],
                               activity_id=kwargs["activity_id"], note=kwargs["note"],
                               status="Active", is_locked=False)


@pytest.fixture(scope="module")
def engine():
    if not TEST_URL:
        pytest.skip("DTB_TEST_DATABASE_URL not set")
    assert_test_database(TEST_URL)
    eng = make_engine(TEST_URL)
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={TEST_URL}"])
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    connection = engine.connect()
    transaction = connection.begin()
    s = Session(bind=connection, expire_on_commit=False)
    yield s
    s.close()
    transaction.rollback()
    connection.close()


def _record(session, *, jibble=PERSON, op_user=42, status="confirmed"):
    emp = Employee(name="Asha", email=f"jh-{datetime.now(UTC).timestamp()}@example.com",
                   openproject_user_id=op_user, jibble_person_id=jibble, is_active=True)
    session.add(emp)
    session.flush()
    sub = DsrSubmission(employee_id=emp.id, dsr_date=date(2026, 11, 20), version=1, status=status,
                        is_current=True, confirmed_at=NOW if status == "confirmed" else None)
    session.add(sub)
    session.flush()
    entry = DsrEntry(submission_id=sub.id, work_package_id=2481, hours_spent=Decimal("0.25"),
                     work_done="Test work")
    session.add(entry)
    session.flush()
    rec = ExternalSyncRecord(dsr_entry_id=entry.id, employee_id=emp.id, target="jibble_time",
                             idempotency_key=f"dtb:dsr-entry:{entry.id}:jibble_time",
                             payload_hash="0" * 64, status="pending")
    session.add(rec)
    session.flush()
    return rec


async def _run(session, rec, fake, *, project=PROJECT, activity=ACTIVITY, verified=True):
    return await process_jibble_record(session, rec, fake, now=NOW, hour_project_id=project,
                                       hour_activity_id=activity, contract_verified=verified)


async def test_unmapped_employee_goes_to_review_without_any_lookup(session):
    rec = _record(session, jibble=None)
    fake = FakeJibble()
    result = await _run(session, rec, fake)
    assert result.action == "needs_review" and fake.list_calls == 0 and fake.create_calls == 0


async def test_missing_project_or_activity_is_not_invented(session):
    rec = _record(session)
    fake = FakeJibble()
    result = await _run(session, rec, fake, project=None)
    assert result.action == "needs_review" and "not configured" in result.detail
    assert fake.list_calls == 0


async def test_matching_existing_entry_is_flagged_not_duplicated(session):
    rec = _record(session)
    fake = FakeJibble(existing=[{"projectId": PROJECT, "activityId": ACTIVITY, "duration": "PT15M"}])
    result = await _run(session, rec, fake)
    assert result.action == "needs_review" and "already exists" in result.detail
    assert fake.create_calls == 0


async def test_different_existing_entries_do_not_block_the_create(session):
    rec = _record(session)
    fake = FakeJibble(existing=[{"projectId": PROJECT, "activityId": ACTIVITY, "duration": "PT9H0M"}])
    result = await _run(session, rec, fake)
    assert result.action == "posted" and fake.create_calls == 1


async def test_contract_gate_closed_means_no_create_is_attempted(session):
    rec = _record(session)
    fake = FakeJibble()
    result = await _run(session, rec, fake, verified=False)
    assert result.action == "needs_review" and "no request sent" in result.detail
    assert fake.create_calls == 0


async def test_lookup_failure_is_retried_with_backoff_and_nothing_is_created(session):
    rec = _record(session)
    fake = FakeJibble(fail_list=True)
    result = await _run(session, rec, fake)
    assert result.action == "retry_wait"
    assert rec.status == "retry_wait" and rec.next_attempt_at is not None
    assert fake.create_calls == 0


def test_claim_can_be_limited_to_jibble_records(session):
    _record(session)
    claimed = claim_due_records(session, now=NOW, worker_id="t", targets=("jibble_time",))
    assert claimed and all(r.target == "jibble_time" for r in claimed)


@pytest.mark.parametrize("duration", ["PT15M", "PT0H15M", "PT0.25H"])
async def test_duration_formats_are_compared_by_value(session, duration):
    from daily_task_bot.services.jibble_hour_sync import duration_hours
    assert abs(duration_hours(duration) - 0.25) < 1e-9
    rec = _record(session)
    fake = FakeJibble(existing=[{"projectId": PROJECT, "activityId": ACTIVITY, "duration": duration}])
    result = await _run(session, rec, fake)
    assert "already exists" in result.detail


def test_unreadable_duration_is_not_treated_as_a_match():
    from daily_task_bot.services.jibble_hour_sync import duration_hours
    assert duration_hours("not-a-duration") is None
    assert duration_hours("") is None


def test_client_generated_hour_entry_uuid_is_stable_and_unique_per_entry():
    from uuid import UUID
    from daily_task_bot.domain.sync_mapping import jibble_hour_entry_uuid
    first = jibble_hour_entry_uuid(7)
    assert first == jibble_hour_entry_uuid(7)
    assert first != jibble_hour_entry_uuid(8)
    assert str(UUID(first)) == first
    assert UUID(first).version == 5
