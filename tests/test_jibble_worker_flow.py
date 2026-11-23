"""Jibble worker flow for a new DSR entry: stable id, duplicate safety, error classes and gates.

Rolled back, test database only. The Jibble client is replaced by a fake; nothing is sent.
"""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from daily_task_bot.clients.jibble import JibbleError, JibbleWriteNotVerified
from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.domain.jibble_hour_entry import RemoteHourEntry
from daily_task_bot.domain.sync_mapping import jibble_hour_entry_uuid
from daily_task_bot.services.jibble_hour_sync import process_jibble_record

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
DAY = date(2026, 11, 21)
PROJECT = "PROJECT-1"
ACTIVITY = "ACTIVITY-1"
PERSON = "PERSON-1"
_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None


class FakeJibble:
    def __init__(self, existing=None, create_errors=None):
        self.existing = list(existing or [])
        self.create_errors = list(create_errors or [])
        self.create_calls = []

    async def list_hour_entries_for_day(self, *, person_id, day):
        return list(self.existing)

    async def create_hour_entry(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.create_errors:
            raise self.create_errors.pop(0)
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


def _record(session):
    emp = Employee(name="Asha", email=f"flow-{datetime.now(UTC).timestamp()}@example.com",
                   openproject_user_id=42, jibble_person_id=PERSON, is_active=True)
    session.add(emp)
    session.flush()
    sub = DsrSubmission(employee_id=emp.id, dsr_date=DAY, version=1, status="confirmed",
                        is_current=True, confirmed_at=NOW)
    session.add(sub)
    session.flush()
    entry = DsrEntry(submission_id=sub.id, work_package_id=2481, hours_spent=Decimal("0.25"),
                     work_done="Fixed the export")
    session.add(entry)
    session.flush()
    rec = ExternalSyncRecord(dsr_entry_id=entry.id, employee_id=emp.id, target="jibble_time",
                             idempotency_key=f"dtb:dsr-entry:{entry.id}:jibble_time",
                             payload_hash="0" * 64, status="pending")
    session.add(rec)
    session.flush()
    return rec


async def _run(session, rec, fake):
    return await process_jibble_record(session, rec, fake, now=NOW, hour_project_id=PROJECT,
                                       hour_activity_id=ACTIVITY)


async def test_create_uses_the_stable_id_and_the_dsr_fields(session):
    rec = _record(session)
    fake = FakeJibble()
    result = await _run(session, rec, fake)
    assert result.action == "posted" and len(fake.create_calls) == 1
    call = fake.create_calls[0]
    assert call["entry_id"] == jibble_hour_entry_uuid(rec.dsr_entry_id)
    assert call["person_id"] == PERSON and call["day"] == DAY
    assert str(call["hours"]) == "0.25" and call["note"] == "Fixed the export"
    assert call["project_id"] == PROJECT and call["activity_id"] == ACTIVITY
    assert rec.status == "sent" and rec.external_ref == call["entry_id"]


async def test_stable_id_already_present_is_reused_and_nothing_is_created(session):
    rec = _record(session)
    uid = jibble_hour_entry_uuid(rec.dsr_entry_id)
    fake = FakeJibble(existing=[{"id": uid, "projectId": PROJECT, "activityId": ACTIVITY, "duration": "PT15M"}])
    result = await _run(session, rec, fake)
    assert result.action == "reused" and fake.create_calls == []
    assert rec.status == "sent" and rec.external_ref == uid


async def test_lost_response_is_recovered_without_a_second_create(session):
    rec = _record(session)
    fake = FakeJibble(create_errors=[httpx.ReadTimeout("lost response")])
    first = await _run(session, rec, fake)
    assert first.action == "retry_wait" and rec.status == "retry_wait"
    fake.existing = [{"id": jibble_hour_entry_uuid(rec.dsr_entry_id), "projectId": PROJECT,
                      "activityId": ACTIVITY, "duration": "PT15M"}]
    second = await _run(session, rec, fake)
    assert second.action == "reused" and len(fake.create_calls) == 1


async def test_jibble_400_is_permanent(session):
    rec = _record(session)
    await _run(session, rec, FakeJibble(create_errors=[JibbleError("create hour entry", 400)]))
    assert rec.status == "failed_permanent" and rec.last_error_class == "permanent"


async def test_jibble_500_is_retried_with_backoff(session):
    rec = _record(session)
    await _run(session, rec, FakeJibble(create_errors=[JibbleError("create hour entry", 500)]))
    assert rec.status == "retry_wait" and rec.next_attempt_at is not None


async def test_jibble_401_needs_review(session):
    rec = _record(session)
    await _run(session, rec, FakeJibble(create_errors=[JibbleError("create hour entry", 401)]))
    assert rec.status == "needs_review"


async def test_closed_write_gate_leaves_the_record_pending_with_no_attempt(session):
    rec = _record(session)
    result = await _run(session, rec, FakeJibble(create_errors=[JibbleWriteNotVerified("writes disabled")]))
    assert result.action == "skipped_writes_disabled"
    assert rec.status == "pending" and rec.attempt_count == 0


async def test_matching_existing_entry_with_a_different_id_is_flagged_not_recreated(session):
    rec = _record(session)
    fake = FakeJibble(existing=[{"id": "someone-else", "projectId": PROJECT, "activityId": ACTIVITY,
                                 "duration": "PT15M"}])
    result = await _run(session, rec, fake)
    assert result.action == "needs_review" and fake.create_calls == []
