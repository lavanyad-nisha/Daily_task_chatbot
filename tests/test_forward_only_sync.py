"""Forward-only DSR sync: historical data is never touched, and Jibble records are never sent."""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from daily_task_bot.config import Settings, get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.services.dsr import confirm_submission
from daily_task_bot.services.openproject_time_sync import (
    claim_due_records,
    enqueue_for_submission,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
GO_LIVE = date(2026, 11, 20)

_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None


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


def _employee(session, *, jibble=None, op_user=42):
    emp = Employee(name="Asha", email=f"fo-{datetime.now(UTC).timestamp()}@example.com",
                   openproject_user_id=op_user, jibble_person_id=jibble, is_active=True)
    session.add(emp)
    session.flush()
    return emp


def _submission(session, emp, day, status="draft"):
    sub = DsrSubmission(employee_id=emp.id, dsr_date=day, version=1, status=status, is_current=True,
                        confirmed_at=NOW if status == "confirmed" else None)
    session.add(sub)
    session.flush()
    session.add(DsrEntry(submission_id=sub.id, work_package_id=2481, hours_spent=Decimal("0.25"),
                         work_done="Test work"))
    session.flush()
    return sub


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_no_go_live_date_means_nothing_is_synced(session):
    emp = _employee(session, jibble="jb-1")
    sub = _submission(session, emp, date(2026, 11, 20), status="confirmed")
    assert enqueue_for_submission(session, sub, go_live=None) == 0
    assert _count(session, ExternalSyncRecord) == 0


def test_historical_dsr_before_go_live_is_not_touched(session):
    emp = _employee(session, jibble="jb-2")
    sub = _submission(session, emp, date(2026, 11, 19), status="confirmed")
    before_entries = _count(session, DsrEntry)
    assert enqueue_for_submission(session, sub, go_live=GO_LIVE) == 0
    assert _count(session, ExternalSyncRecord) == 0
    assert _count(session, DsrEntry) == before_entries


def test_dsr_on_or_after_go_live_gets_openproject_and_jibble_records(session):
    emp = _employee(session, jibble="jb-3")
    sub = _submission(session, emp, GO_LIVE, status="confirmed")
    assert enqueue_for_submission(session, sub, go_live=GO_LIVE) == 2
    targets = {r.target: r.status for r in session.scalars(select(ExternalSyncRecord))}
    assert targets == {"openproject_time": "pending", "jibble_time": "pending"}


def test_unmapped_employee_gets_a_jibble_record_in_review_not_pending(session):
    emp = _employee(session, jibble=None, op_user=77)
    sub = _submission(session, emp, GO_LIVE, status="confirmed")
    enqueue_for_submission(session, sub, go_live=GO_LIVE)
    jibble = session.scalar(select(ExternalSyncRecord).where(ExternalSyncRecord.target == "jibble_time"))
    assert jibble.status == "needs_review"
    assert jibble.last_error == "employee has no Jibble mapping"


def test_openproject_claim_never_picks_up_jibble_records(session):
    emp = _employee(session, jibble="jb-4", op_user=78)
    sub = _submission(session, emp, GO_LIVE, status="confirmed")
    enqueue_for_submission(session, sub, go_live=GO_LIVE)
    claimed = claim_due_records(session, now=NOW, worker_id="test")
    assert claimed and all(r.target != "jibble_time" for r in claimed)
    jibble = session.scalar(select(ExternalSyncRecord).where(ExternalSyncRecord.target == "jibble_time"))
    assert jibble.status == "pending" and jibble.locked_by is None


def test_confirm_applies_the_go_live_gate(session):
    emp = _employee(session, jibble="jb-5", op_user=79)
    sub = _submission(session, emp, date(2026, 11, 19))
    result = confirm_submission(session, sub.id, now=NOW, go_live=GO_LIVE)
    assert result.enqueued == 0
    assert _count(session, ExternalSyncRecord) == 0


def test_go_live_date_is_configurable_from_the_environment(monkeypatch):
    monkeypatch.setenv("DTB_DSR_GO_LIVE_DATE", "2026-12-01")
    assert Settings(_env_file=None).dsr_go_live_date == date(2026, 12, 1)


def test_go_live_date_defaults_to_unset():
    assert Settings(_env_file=None).dsr_go_live_date is None
