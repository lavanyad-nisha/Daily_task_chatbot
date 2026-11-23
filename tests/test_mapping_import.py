"""Mapping import: preview, conflict detection, and approval-gated apply. Rolled back, test DB only."""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee
from daily_task_bot.services.mapping_import import ImportRefused, apply, preview

_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None
pytestmark = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def engine():
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


def _employee(session, *, id_hint=None, email, op_id, chat=None, jibble=None):
    emp = Employee(name="Existing", email=email, openproject_user_id=op_id,
                   chat_user_id=chat, jibble_person_id=jibble, is_active=True)
    session.add(emp)
    session.flush()
    return emp


def _row(email="new.person@example.com", op=5001, name="New Person", **extra):
    data = {"name": name, "email": email, "openproject_user_id": op}
    data.update(extra)
    return data


# ---- validation -------------------------------------------------------------------

def test_invalid_rows_block_the_import(session):
    result = preview(session, [_row(email="not-an-email"), _row(op=0, email="a@b.example")])
    assert result.counts().get("invalid") == 2
    assert result.blocked


def test_chat_id_must_have_the_expected_shape(session):
    result = preview(session, [_row(chat_user_id="123")])
    assert result.counts().get("invalid") == 1


# ---- conflicts ----------------------------------------------------------------------

def test_new_employee_is_a_create(session):
    result = preview(session, [_row()])
    assert result.counts() == {"create": 1} and not result.blocked


def test_duplicate_email_inside_the_file_is_a_conflict(session):
    result = preview(session, [_row(op=5001), _row(op=5002)])
    assert "conflict" in result.counts()


def test_duplicate_openproject_id_inside_the_file_is_a_conflict(session):
    result = preview(session, [_row(email="a@example.com", op=7), _row(email="b@example.com", op=7)])
    assert result.counts().get("conflict") == 1


def test_email_and_openproject_id_belonging_to_different_employees_is_a_conflict(session):
    _employee(session, email="one@example.com", op_id=8001)
    _employee(session, email="two@example.com", op_id=8002)
    result = preview(session, [_row(email="one@example.com", op=8002)])
    assert result.counts() == {"conflict": 1}


def test_chat_id_owned_by_another_employee_is_a_conflict(session):
    _employee(session, email="owner@example.com", op_id=9001, chat="users/111")
    result = preview(session, [_row(email="new@example.com", op=9002, chat_user_id="users/111")])
    assert result.counts() == {"conflict": 1}


def test_identical_existing_employee_is_unchanged(session):
    _employee(session, email="same@example.com", op_id=9100)
    result = preview(session, [_row(email="same@example.com", op=9100, name="Existing")])
    assert result.counts() == {"unchanged": 1}


def test_existing_employee_update_is_planned_not_applied(session):
    emp = _employee(session, email="old@example.com", op_id=9200)
    result = preview(session, [_row(email="old@example.com", op=9200, name="Renamed Person")])
    assert result.counts() == {"update": 1}
    assert result.rows[0].employee_id == emp.id
    assert result.rows[0].changes == {"name": "Renamed Person"}
    session.refresh(emp)
    assert emp.name == "Existing"


def test_employee_one_is_updated_in_place_and_keeps_its_id_and_history(session):
    """The real mapping for user 42 arrives. Employee 1 must keep its id and its DSR history."""
    emp = _employee(session, email="placeholder.example@example.com", op_id=42)
    session.flush()
    assert emp.id is not None
    sub = DsrSubmission(employee_id=emp.id, dsr_date=date(2026, 11, 18), version=1,
                        status="confirmed", is_current=True,
                        confirmed_at=datetime(2026, 11, 18, 18, tzinfo=UTC))
    session.add(sub)
    session.flush()
    session.add(DsrEntry(submission_id=sub.id, work_package_id=1809, hours_spent=Decimal("1"), work_done="x"))
    session.flush()
    before = session.scalar(select(func.count()).select_from(DsrSubmission).where(DsrSubmission.employee_id == emp.id))

    result = preview(session, [_row(email="real.person@example.com", op=42, name="Real Person")])
    assert result.counts() == {"update": 1}
    assert result.rows[0].employee_id == emp.id
    assert "keeps its id and history" in result.rows[0].detail

    apply(session, [_row(email="real.person@example.com", op=42, name="Real Person")], result.plan_hash)
    session.refresh(emp)
    assert emp.id == result.rows[0].employee_id
    after = session.scalar(select(func.count()).select_from(DsrSubmission).where(DsrSubmission.employee_id == emp.id))
    assert after == before


# ---- preview is read-only ------------------------------------------------------------

def test_preview_writes_nothing(session):
    before = session.scalar(select(func.count()).select_from(Employee))
    preview(session, [_row(), _row(email="other@example.com", op=5002)])
    assert session.scalar(select(func.count()).select_from(Employee)) == before


# ---- apply is gated by the approved preview -------------------------------------------

def test_apply_refuses_without_the_approved_hash(session):
    with pytest.raises(ImportRefused, match="changed since the approved preview"):
        apply(session, [_row()], approved_plan_hash="not-the-hash")
    assert session.scalar(select(func.count()).select_from(Employee)) == 0


def test_apply_refuses_when_the_preview_is_blocked(session):
    rows = [_row(email="bad")]
    result = preview(session, rows)
    with pytest.raises(ImportRefused, match="invalid or conflicting"):
        apply(session, rows, approved_plan_hash=result.plan_hash)


def test_apply_with_the_approved_hash_creates_the_employee(session):
    rows = [_row()]
    result = preview(session, rows)
    applied = apply(session, rows, approved_plan_hash=result.plan_hash)
    assert applied == {"created": 1, "updated": 0, "unchanged": 0}
    assert session.scalar(select(func.count()).select_from(Employee)) == 1


def test_apply_refuses_if_the_database_changed_after_approval(session):
    rows = [_row()]
    approved = preview(session, rows).plan_hash
    # Someone else creates the employee this plan was going to create.
    _employee(session, email="new.person@example.com", op_id=5001)
    with pytest.raises(ImportRefused):
        apply(session, rows, approved_plan_hash=approved)
