"""DSR drafts and confirmation rules. Pure rules first, then database tests with rollback."""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.services.dsr import (
    DsrConflictError,
    DsrNotFoundError,
    DsrValidationError,
    EntryInput,
    confirm_submission,
    create_draft,
    validate_entries,
)
from daily_task_bot.services.openproject_time_sync import AuthorizationError

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _e(wp=1809, hours="2", work="Fixed login bug"):
    return EntryInput(work_package_id=wp, hours_spent=Decimal(hours), work_done=work)


# ---- pure validation -------------------------------------------------------------

def test_valid_entries_pass():
    validate_entries([_e(1), _e(2, hours="0.25")])


def test_no_entries_rejected():
    with pytest.raises(DsrValidationError, match="at least one"):
        validate_entries([])


def test_duplicate_work_package_rejected():
    with pytest.raises(DsrValidationError, match="appears twice"):
        validate_entries([_e(5), _e(5)])


@pytest.mark.parametrize("hours", ["0", "-1", "24.01"])
def test_hours_must_be_above_zero_and_at_most_24(hours):
    with pytest.raises(DsrValidationError, match="hours"):
        validate_entries([_e(hours=hours)])


def test_total_hours_per_day_capped_at_24():
    with pytest.raises(DsrValidationError, match="total hours"):
        validate_entries([_e(1, hours="13"), _e(2, hours="12")])
    validate_entries([_e(1, hours="12"), _e(2, hours="12")])


@pytest.mark.parametrize("work", ["", "   "])
def test_description_is_required(work):
    with pytest.raises(DsrValidationError, match="description"):
        validate_entries([_e(work=work)])


def test_description_length_capped():
    with pytest.raises(DsrValidationError, match="too long"):
        validate_entries([_e(work="x" * 2001)])


# ---- database behaviour (rolled back) ---------------------------------------------

_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None
pytestmark_db = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def engine():
    if not TEST_URL:
        pytest.skip("DTB_TEST_DATABASE_URL not set")
    assert_test_database(TEST_URL)
    eng = make_engine(TEST_URL)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={TEST_URL}"])
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    from sqlalchemy.orm import Session

    connection = engine.connect()
    transaction = connection.begin()
    s = Session(bind=connection, expire_on_commit=False)
    yield s
    s.close()
    transaction.rollback()
    connection.close()


def _employee(session, *, active=True, op_user=42, email=None):
    emp = Employee(name="Asha", email=email or f"dsr-{datetime.now(UTC).timestamp()}@example.com",
                   openproject_user_id=op_user, is_active=active)
    session.add(emp)
    session.flush()
    return emp


@pytestmark_db
def test_create_draft_saves_draft_with_entries(session):
    emp = _employee(session)
    sub = create_draft(session, employee_id=emp.id, dsr_date=date(2026, 11, 19),
                       entries=[_e(1809), _e(1774, hours="1")])
    assert sub.status == "draft" and sub.is_current and sub.confirmed_at is None
    assert sorted(e.work_package_id for e in sub.entries) == [1774, 1809]


@pytestmark_db
def test_second_current_draft_for_same_day_is_a_conflict(session):
    emp = _employee(session)
    create_draft(session, employee_id=emp.id, dsr_date=date(2026, 11, 19), entries=[_e()])
    with pytest.raises(DsrConflictError):
        create_draft(session, employee_id=emp.id, dsr_date=date(2026, 11, 19), entries=[_e(2)])


@pytestmark_db
def test_inactive_employee_cannot_get_a_draft(session):
    emp = _employee(session, active=False)
    with pytest.raises(AuthorizationError):
        create_draft(session, employee_id=emp.id, dsr_date=date(2026, 11, 19), entries=[_e()])


@pytestmark_db
def test_confirm_sets_status_and_enqueues_one_record_per_entry(session):
    emp = _employee(session)
    sub = create_draft(session, employee_id=emp.id, dsr_date=date(2026, 11, 19),
                       entries=[_e(1809), _e(1774, hours="1")])
    result = confirm_submission(session, sub.id, now=NOW, go_live=date(2026, 1, 1))
    # per entry: one OpenProject time + one Jibble record (no status chosen)
    assert (result.status, result.enqueued, result.already_confirmed) == ("confirmed", 4, False)
    session.refresh(sub)
    assert sub.status == "confirmed" and sub.confirmed_at == NOW
    records = session.scalars(select(ExternalSyncRecord)).all()
    assert len(records) == 4
    assert {r.target for r in records} == {"openproject_time", "jibble_time"}
    assert all(r.status == "pending" for r in records if r.target == "openproject_time")
    # employees without a Jibble mapping get a Jibble record in review, never a pending send
    assert all(r.status == "needs_review" for r in records if r.target == "jibble_time")


@pytestmark_db
def test_confirming_twice_is_a_no_op(session):
    emp = _employee(session)
    sub = create_draft(session, employee_id=emp.id, dsr_date=date(2026, 11, 19), entries=[_e()])
    confirm_submission(session, sub.id, now=NOW, go_live=date(2026, 1, 1))
    again = confirm_submission(session, sub.id, now=NOW, go_live=date(2026, 1, 1))
    assert again.already_confirmed is True and again.enqueued == 0
    assert len(session.scalars(select(ExternalSyncRecord)).all()) == 2  # time + Jibble, once each


@pytestmark_db
def test_confirm_without_openproject_id_leaves_draft_untouched(session):
    emp = _employee(session, op_user=None)
    sub = DsrSubmission(employee_id=emp.id, dsr_date=date(2026, 11, 19), version=1,
                        status="draft", is_current=True)
    session.add(sub)
    session.flush()
    from daily_task_bot.db.models import DsrEntry
    session.add(DsrEntry(submission_id=sub.id, work_package_id=1809,
                         hours_spent=Decimal("1"), work_done="x"))
    session.flush()
    with pytest.raises(AuthorizationError):
        confirm_submission(session, sub.id, now=NOW, go_live=date(2026, 1, 1))
    session.refresh(sub)
    assert sub.status == "draft" and sub.confirmed_at is None


@pytestmark_db
def test_confirm_unknown_submission_is_not_found(session):
    with pytest.raises(DsrNotFoundError):
        confirm_submission(session, 987654321, now=NOW, go_live=date(2026, 1, 1))


@pytestmark_db
def test_confirm_rejects_superseded_submission(session):
    emp = _employee(session)
    sub = create_draft(session, employee_id=emp.id, dsr_date=date(2026, 11, 19), entries=[_e()])
    sub.is_current = False
    sub.status = "superseded"
    session.flush()
    with pytest.raises(DsrConflictError):
        confirm_submission(session, sub.id, now=NOW, go_live=date(2026, 1, 1))


# ---- status choice ----------------------------------------------------------------

def test_status_choice_outside_the_configured_list_is_refused():
    entry = EntryInput(work_package_id=1, hours_spent=Decimal("1"), work_done="x", new_status_id=99)
    with pytest.raises(DsrValidationError, match="not an allowed choice"):
        validate_entries([entry], frozenset({7, 8}))


def test_status_choice_inside_the_configured_list_is_accepted():
    entry = EntryInput(work_package_id=1, hours_spent=Decimal("1"), work_done="x", new_status_id=7)
    validate_entries([entry], frozenset({7, 8}))


def test_status_choices_are_off_when_the_list_is_empty():
    entry = EntryInput(work_package_id=1, hours_spent=Decimal("1"), work_done="x", new_status_id=7)
    with pytest.raises(DsrValidationError, match="status updates are off"):
        validate_entries([entry])


def test_no_status_choice_is_always_fine():
    validate_entries([EntryInput(work_package_id=1, hours_spent=Decimal("1"), work_done="x")])
