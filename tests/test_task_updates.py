"""Chat commands for updating today's tasks, against the test database. Nothing is sent anywhere."""

import argparse
import asyncio
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.clients.openproject import Status, WorkPackageDetail
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import DsrSubmission, Employee, ExternalSyncRecord
from daily_task_bot.domain.chat_commands import parse_command
from daily_task_bot.domain.models import WorkPackage
from daily_task_bot.services.chat_events import handle_chat_event

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)
TODAY = date(2026, 11, 23)
CHAT_USER = "users/901"
OP_USER = 42
_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None


class FakeOpenProject:
    def __init__(self, assigned=(2481, 1774)):
        self.assigned = list(assigned)
        self.statuses = {7: Status(7, "In progress", False), 11: Status(11, "Test failed", False),
                         10: Status(10, "Tested", False)}

    async def get_statuses(self):
        return self.statuses

    async def get_open_work_packages(self, statuses, project_ids=None):
        return [WorkPackage(id=w, subject=f"Task {w}", status_name="In progress", status_id=7, status_is_closed=False,
                            assignee_id=OP_USER, start_date=date(2026, 11, 20), due_date=date(2026, 11, 30))
                for w in self.assigned]

    async def get_work_package(self, wp_id, statuses=None):
        if wp_id not in self.assigned:
            return None
        return WorkPackageDetail(id=wp_id, assignee_id=OP_USER, status_is_closed=False,
                                 status_id=7, lock_version=1, can_update=True,
                                 start_date=date(2026, 11, 20), due_date=date(2026, 11, 30))


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
    connection = engine.connect()
    transaction = connection.begin()
    s = Session(bind=connection, expire_on_commit=False)
    yield s
    s.close()
    transaction.rollback()
    connection.close()


def _settings():
    return Settings(_env_file=None, timezone="Asia/Kolkata", dsr_status_choices=[7, 11],
                    dsr_go_live_date=date(2026, 11, 1), allow_external_writes=True,
                    pilot_chat_user_ids=[CHAT_USER])


def _employee(session):
    emp = Employee(name="Asha", email=f"upd-{uuid.uuid4().hex[:8]}@example.com",
                   openproject_user_id=OP_USER, chat_user_id=CHAT_USER, is_active=True)
    session.add(emp)
    session.flush()
    return emp


def _say(session, text, op=None):
    event = {"type": "MESSAGE", "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}, "user": {"name": CHAT_USER}, "message": {"text": text}}
    return asyncio.run(handle_chat_event(event, session=session, op_client=op or FakeOpenProject(),
                                         settings=_settings(), now=NOW))["text"]


def _entry(session, emp, wp):
    sub = session.scalar(select(DsrSubmission).where(DsrSubmission.employee_id == emp.id,
                                                     DsrSubmission.dsr_date == TODAY))
    return next((e for e in sub.entries if e.work_package_id == wp), None) if sub else None


# ---- parser (pure) ----------------------------------------------------------------

@pytest.mark.parametrize("text, name", [
    ("TASKS", "TASKS"), ("tasks", "TASKS"), ("dsr", "DSR"), ("HELP", "HELP"), ("", "HELP"),
])
def test_simple_commands_parse(text, name):
    assert parse_command(text).name == name


def test_update_parses_task_hours_and_work():
    cmd = parse_command("UPDATE 2 2.5 Fixed the export bug")
    assert cmd.name == "UPDATE" and cmd.args == {"task": 2, "hours": Decimal("2.5"),
                                                 "work": "Fixed the export bug"}


@pytest.mark.parametrize("text", ["UPDATE 2 2.5", "UPDATE 0 1 x", "UPDATE 2 30 x", "UPDATE x 1 y",
                                  "UPDATE 2 0 x"])
def test_malformed_update_is_an_error(text):
    assert parse_command(text).error


def test_status_parses_numbers_only():
    assert parse_command("STATUS 2 7").args == {"task": 2, "status_id": 7}
    assert parse_command("STATUS 1 done").error


def test_unknown_text_gets_help_not_a_guess():
    cmd = parse_command("please change my hours")
    assert cmd.name == "HELP" and cmd.error


# ---- chat handler (database) ------------------------------------------------------

def test_tasks_lists_open_tasks_with_numbers(session):
    _employee(session)
    text = _say(session, "TASKS")
    assert "1. #1774" in text and "2. #2481" in text


def test_first_update_creates_and_confirms_todays_entry(session):
    emp = _employee(session)
    assert "Saved task #2481" in _say(session, "UPDATE 2 2 Fixed export")
    entry = _entry(session, emp, 2481)
    assert entry.hours_spent == Decimal("2") and entry.work_done == "Fixed export"
    targets = {r.target for r in session.scalars(select(ExternalSyncRecord))}
    assert "openproject_time" in targets and "jibble_time" in targets


def test_second_update_changes_the_same_entry_without_a_duplicate(session):
    emp = _employee(session)
    _say(session, "UPDATE 2 2 First version")
    _say(session, "UPDATE 2 3 Corrected version")
    sub = session.scalar(select(DsrSubmission).where(DsrSubmission.employee_id == emp.id))
    assert len([e for e in sub.entries if e.work_package_id == 2481]) == 1
    entry = _entry(session, emp, 2481)
    assert entry.hours_spent == Decimal("3") and entry.work_done == "Corrected version"


def test_update_adds_a_second_task_to_the_same_day(session):
    emp = _employee(session)
    _say(session, "UPDATE 2 2 Task one")
    _say(session, "UPDATE 1 1 Task two")
    assert _entry(session, emp, 1774) is not None


def test_status_change_is_queued_for_the_entry(session):
    emp = _employee(session)
    _say(session, "UPDATE 2 2 Work")
    assert "Saved task #2481" in _say(session, "STATUS 2 7")
    assert _entry(session, emp, 2481).new_status_id == 7
    assert session.scalar(select(ExternalSyncRecord.id).where(
        ExternalSyncRecord.target == "openproject_status")) is not None


def test_status_outside_the_approved_list_is_refused(session):
    emp = _employee(session)
    _say(session, "UPDATE 2 2 Work")
    assert "not on the approved list" in _say(session, "STATUS 1 10")
    assert _entry(session, emp, 2481).new_status_id is None


def test_task_number_not_on_the_list_is_refused(session):
    _employee(session)
    assert "not on your list" in _say(session, "UPDATE 9 1 Work")


def test_changes_are_refused_once_the_entry_has_been_sent(session):
    emp = _employee(session)
    _say(session, "UPDATE 2 2 Work")
    entry = _entry(session, emp, 2481)
    for record in session.scalars(select(ExternalSyncRecord).where(ExternalSyncRecord.dsr_entry_id == entry.id)):
        if record.target == "openproject_time":
            record.status = "sent"
            record.sent_at = NOW
            record.external_ref = "123"
    session.flush()
    message = _say(session, "UPDATE 2 5 Too late")
    assert "already been sent" in message
    assert _entry(session, emp, 2481).hours_spent == Decimal("2")


def test_day_total_over_24_hours_is_refused(session):
    emp = _employee(session)
    _say(session, "UPDATE 2 20 Long day")
    assert "more than 24 hours" in _say(session, "UPDATE 1 10 Too much")
    assert _entry(session, emp, 1774) is None
