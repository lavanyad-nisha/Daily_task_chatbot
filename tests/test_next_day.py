"""Next-day reminder plan: pure rules, then the previous-DSR lookup against the test database."""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import DsrEntry, DsrSubmission, Employee
from daily_task_bot.domain.models import WorkPackage
from daily_task_bot.services.next_day import (
    PreviousEntry,
    build_next_day_plan,
    load_previous_entries,
    render_next_day_plan,
)

RUN = date(2026, 11, 19)


def wp(id_, status="In progress", closed=False, subject=None):
    return WorkPackage(id=id_, subject=subject or f"Task {id_}", status_name=status,
                       status_is_closed=closed)


# ---- pure rules --------------------------------------------------------------------

def test_closed_tasks_are_excluded():
    plan = build_next_day_plan(run_date=RUN, open_assignments=[wp(1), wp(2, closed=True)],
                               previous_dsr_date=None, previous_entries={})
    assert [i.work_package_id for i in plan.items] == [1]


def test_remaining_note_comes_from_previous_dsr_for_the_same_task():
    plan = build_next_day_plan(
        run_date=RUN, open_assignments=[wp(1), wp(2)],
        previous_dsr_date=date(2026, 11, 18),
        previous_entries={1: PreviousEntry("Finish validation tests", None)},
    )
    notes = {i.work_package_id: i.remaining_note for i in plan.items}
    assert notes == {1: "Finish validation tests", 2: None}


def test_status_mismatch_is_flagged_when_openproject_differs_from_dsr_choice():
    plan = build_next_day_plan(
        run_date=RUN, open_assignments=[wp(1, status="Tested")],
        previous_dsr_date=date(2026, 11, 18),
        previous_entries={1: PreviousEntry(None, 11)},
        status_names={11: "Test failed"},
    )
    assert plan.items[0].status_mismatch == "DSR chose 'Test failed', OpenProject shows 'Tested'"


def test_no_flag_when_status_matches_the_dsr_choice():
    plan = build_next_day_plan(
        run_date=RUN, open_assignments=[wp(1, status="Test failed")],
        previous_dsr_date=date(2026, 11, 18),
        previous_entries={1: PreviousEntry(None, 11)},
        status_names={11: "Test failed"},
    )
    assert plan.items[0].status_mismatch is None


def test_no_previous_dsr_is_stated_and_tasks_still_listed():
    plan = build_next_day_plan(run_date=RUN, open_assignments=[wp(5)],
                               previous_dsr_date=None, previous_entries={})
    text = render_next_day_plan(plan, "Asha")
    assert plan.no_previous_dsr is True
    assert "No confirmed DSR found" in text
    assert "#5 Task 5" in text


def test_render_shows_remaining_note_and_mismatch_lines():
    plan = build_next_day_plan(
        run_date=RUN, open_assignments=[wp(1, status="Tested")],
        previous_dsr_date=date(2026, 11, 18),
        previous_entries={1: PreviousEntry("Fix retry", 11)},
        status_names={11: "Test failed"},
    )
    text = render_next_day_plan(plan, "Asha")
    assert "Remaining from last DSR: Fix retry" in text
    assert "Check status:" in text


def test_render_with_no_open_tasks_says_so():
    plan = build_next_day_plan(run_date=RUN, open_assignments=[],
                               previous_dsr_date=None, previous_entries={})
    assert "No open assigned tasks." in render_next_day_plan(plan, "Asha")


# ---- previous DSR lookup (rolled back) -------------------------------------------

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


def _submission(session, emp, day, status, entries):
    sub = DsrSubmission(employee_id=emp.id, dsr_date=day, version=1, status=status,
                        is_current=True, confirmed_at=datetime(2026, 11, 18, 18, tzinfo=UTC)
                        if status == "confirmed" else None)
    session.add(sub)
    session.flush()
    for wp_id, remaining, status_id in entries:
        session.add(DsrEntry(submission_id=sub.id, work_package_id=wp_id, hours_spent=Decimal("1"),
                             work_done="x", remaining_work=remaining, new_status_id=status_id))
    session.flush()
    return sub


def _employee(session):
    emp = Employee(name="Asha", email=f"np-{datetime.now(UTC).timestamp()}@example.com",
                   openproject_user_id=42, is_active=True)
    session.add(emp)
    session.flush()
    return emp


def test_lookup_returns_latest_confirmed_dsr_before_run_date(session):
    if not TEST_URL:
        pytest.skip("DTB_TEST_DATABASE_URL not set")
    emp = _employee(session)
    _submission(session, emp, date(2026, 11, 16), "confirmed", [(1809, "older note", None)])
    _submission(session, emp, date(2026, 11, 18), "confirmed", [(1809, "latest note", 11)])
    _submission(session, emp, date(2026, 11, 19), "draft", [(1809, "today draft", None)])
    day, entries = load_previous_entries(session, emp.id, RUN)
    assert day == date(2026, 11, 18)
    assert entries[1809].remaining_work == "latest note"
    assert entries[1809].chosen_status_id == 11


def test_draft_and_same_day_submissions_are_ignored(session):
    if not TEST_URL:
        pytest.skip("DTB_TEST_DATABASE_URL not set")
    emp = _employee(session)
    _submission(session, emp, date(2026, 11, 19), "confirmed", [(1, "same day", None)])
    _submission(session, emp, date(2026, 11, 18), "draft", [(1, "unconfirmed", None)])
    day, entries = load_previous_entries(session, emp.id, RUN)
    assert day is None and entries == {}
