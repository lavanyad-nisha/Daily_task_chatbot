"""T2 schema tests against the real PostgreSQL test database.

Each test runs inside a transaction that is rolled back at the end, so nothing is persisted.
Skipped unless DTB_TEST_DATABASE_URL is configured.
"""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from daily_task_bot.config import get_settings
from daily_task_bot.db.base import Base
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import (
    DailyPlan,
    DailyPlanItem,
    DsrEntry,
    DsrSubmission,
    Employee,
    ExternalSyncRecord,
)

ROOT = Path(__file__).resolve().parent.parent
_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None

pytestmark = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")

EXPECTED_TABLES = {
    "employees",
    "daily_plans",
    "daily_plan_items",
    "dsr_submissions",
    "dsr_entries",
    "external_sync_records",
}


@pytest.fixture(scope="session")
def engine():
    assert_test_database(TEST_URL)
    eng = make_engine(TEST_URL)
    cfg = Config(str(ROOT / "alembic.ini"))
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


NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)


def make_employee(session, email="asha@example.com", **overrides) -> Employee:
    data = {"name": "Asha", "email": email}
    data.update(overrides)
    emp = Employee(**data)
    session.add(emp)
    session.flush()
    return emp


def make_submission(session, employee, dsr_date=date(2026, 11, 19), **overrides) -> DsrSubmission:
    data = {"employee_id": employee.id, "dsr_date": dsr_date, "version": 1,
                "status": "draft", "is_current": True}
    data.update(overrides)
    sub = DsrSubmission(**data)
    session.add(sub)
    session.flush()
    return sub


# ---- structure -----------------------------------------------------------

def test_all_t2_tables_exist(engine):
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())


def test_models_match_migrations_with_no_drift(engine):
    """If this fails, a model changed without a matching Alembic revision."""
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diffs = compare_metadata(context, Base.metadata)
    assert diffs == [], f"Schema drift detected: {diffs}"


def test_every_model_check_constraint_exists_in_database(engine):
    """Alembic autogenerate does not compare CHECK constraints, so verify them explicitly."""
    from sqlalchemy import CheckConstraint

    inspector = inspect(engine)
    missing = []
    for table in Base.metadata.sorted_tables:
        in_db = {c["name"] for c in inspector.get_check_constraints(table.name)}
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name not in in_db:
                missing.append((table.name, constraint.name))
    assert missing == [], f"CHECK constraints declared on models but missing in the database: {missing}"


# ---- employees -----------------------------------------------------------

def test_employee_email_must_be_lowercase(session):
    with pytest.raises(IntegrityError), session.begin_nested():
        make_employee(session, email="Asha@Example.com")


def test_employee_email_is_unique(session):
    make_employee(session, email="dup@example.com")
    with pytest.raises(IntegrityError), session.begin_nested():
        make_employee(session, email="dup@example.com")


def test_employee_external_ids_are_unique_when_set(session):
    make_employee(session, email="a@example.com", openproject_user_id=7, chat_user_id="users/1")
    with pytest.raises(IntegrityError), session.begin_nested():
        make_employee(session, email="b@example.com", openproject_user_id=7)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_employee(session, email="c@example.com", chat_user_id="users/1")


def test_employee_external_ids_may_be_null(session):
    emp = make_employee(session, email="nulls@example.com")
    assert emp.openproject_user_id is None and emp.chat_user_id is None
    assert emp.is_active is True


def test_employee_name_cannot_be_blank(session):
    with pytest.raises(IntegrityError), session.begin_nested():
        make_employee(session, email="blank@example.com", name="   ")


# ---- daily plans ---------------------------------------------------------

def test_one_plan_per_employee_per_day(session):
    emp = make_employee(session)
    session.add(DailyPlan(employee_id=emp.id, plan_date=date(2026, 11, 19)))
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(DailyPlan(employee_id=emp.id, plan_date=date(2026, 11, 19)))
        session.flush()


def test_plan_status_is_restricted(session):
    emp = make_employee(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(DailyPlan(employee_id=emp.id, plan_date=date(2026, 11, 19), status="bogus"))
        session.flush()


def test_plan_items_cascade_when_plan_deleted(session):
    emp = make_employee(session)
    plan = DailyPlan(employee_id=emp.id, plan_date=date(2026, 11, 19))
    session.add(plan)
    session.flush()
    session.add(DailyPlanItem(plan_id=plan.id, work_package_id=1, subject="x", status_name="New"))
    session.flush()
    session.delete(plan)
    session.flush()
    assert session.query(DailyPlanItem).filter_by(plan_id=plan.id).count() == 0


# ---- DSR versions and entries -------------------------------------------

def test_only_one_current_dsr_per_employee_per_day(session):
    emp = make_employee(session)
    make_submission(session, emp, version=1, is_current=True)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_submission(session, emp, version=2, is_current=True)


def test_superseded_versions_may_coexist_with_current(session):
    emp = make_employee(session)
    make_submission(session, emp, version=1, status="superseded", is_current=False)
    current = make_submission(session, emp, version=2, status="draft", is_current=True)
    assert current.id is not None


def test_superseded_must_not_be_current(session):
    emp = make_employee(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_submission(session, emp, status="superseded", is_current=True)


def test_non_current_version_must_be_superseded(session):
    emp = make_employee(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_submission(session, emp, status="draft", is_current=False)


def test_version_unique_per_employee_and_date(session):
    emp = make_employee(session)
    make_submission(session, emp, version=1, status="superseded", is_current=False)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_submission(session, emp, version=1, status="draft", is_current=True)


def test_draft_cannot_have_confirmed_at(session):
    emp = make_employee(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_submission(session, emp, status="draft", confirmed_at=NOW)


def test_superseded_version_may_keep_confirmed_at(session):
    emp = make_employee(session)
    make_submission(session, emp, status="superseded", is_current=False, confirmed_at=NOW)


def test_entry_unique_per_submission_and_work_package(session):
    emp = make_employee(session)
    sub = make_submission(session, emp)
    session.add(DsrEntry(submission_id=sub.id, work_package_id=5, hours_spent=Decimal("2"), work_done="x"))
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(DsrEntry(submission_id=sub.id, work_package_id=5, hours_spent=Decimal("1"), work_done="y"))
        session.flush()


def test_entries_cascade_when_submission_deleted(session):
    emp = make_employee(session)
    sub = make_submission(session, emp)
    session.add(DsrEntry(submission_id=sub.id, work_package_id=5, hours_spent=Decimal("2"), work_done="x"))
    session.flush()
    session.delete(sub)
    session.flush()
    assert session.query(DsrEntry).filter_by(submission_id=sub.id).count() == 0


# ---- DSR hours and per-target sync records -------------------------------

def _entry_with_hours(session, hours=Decimal("2.5")):
    emp = make_employee(session, email=f"sync-{datetime.now().timestamp()}@example.com")
    sub = make_submission(session, emp)
    entry = DsrEntry(submission_id=sub.id, work_package_id=9, hours_spent=hours, work_done="x")
    session.add(entry)
    session.flush()
    return emp, entry


def _sync(emp, entry, target="openproject_time", key=None, **overrides):
    data = dict(dsr_entry_id=entry.id, employee_id=emp.id, target=target,
                idempotency_key=key or f"k-{entry.id}-{target}", payload_hash="0" * 64)
    data.update(overrides)
    return ExternalSyncRecord(**data)


def test_dsr_hours_are_required_and_bounded(session):
    emp = make_employee(session, email="hours@example.com")
    sub = make_submission(session, emp)
    for bad in (Decimal("0"), Decimal("-1"), Decimal("24.01")):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(DsrEntry(submission_id=sub.id, work_package_id=1, hours_spent=bad, work_done="x"))
            session.flush()


def test_same_entry_can_have_one_record_per_target(session):
    emp, entry = _entry_with_hours(session)
    session.add(_sync(emp, entry, "openproject_time"))
    session.add(_sync(emp, entry, "jibble_time"))
    session.flush()  # independent targets must both be allowed


def test_only_one_record_per_entry_and_target(session):
    emp, entry = _entry_with_hours(session)
    session.add(_sync(emp, entry, "openproject_time"))
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_sync(emp, entry, "openproject_time", key="other-key"))
        session.flush()


def test_sync_target_is_restricted(session):
    emp, entry = _entry_with_hours(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_sync(emp, entry, target="progress_percent"))
        session.flush()


def test_sync_status_is_restricted(session):
    emp, entry = _entry_with_hours(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_sync(emp, entry, status="bogus"))
        session.flush()


def test_in_progress_requires_a_lock_expiry(session):
    emp, entry = _entry_with_hours(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_sync(emp, entry, status="in_progress"))
        session.flush()


def test_sent_record_requires_sent_at_and_other_statuses_forbid_it(session):
    emp, entry = _entry_with_hours(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_sync(emp, entry, status="sent"))
        session.flush()
    emp2, entry2 = _entry_with_hours(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_sync(emp2, entry2, status="retry_wait", sent_at=NOW))
        session.flush()


def test_error_class_is_restricted(session):
    emp, entry = _entry_with_hours(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_sync(emp, entry, last_error_class="maybe"))
        session.flush()


def test_idempotency_key_is_globally_unique(session):
    emp, entry = _entry_with_hours(session)
    session.add(_sync(emp, entry, "openproject_time", key="shared-key"))
    session.flush()
    emp2, entry2 = _entry_with_hours(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_sync(emp2, entry2, "openproject_time", key="shared-key"))
        session.flush()
