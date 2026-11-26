"""Chat flow against the real test database: form submission, identity, duplicates and reminders.

The OpenProject client and the Google Chat client are replaced by fakes. Nothing is sent anywhere.
Reminder code commits, so the module truncates the test database afterwards (guarded to dtb_test).
"""

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
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from daily_task_bot.clients.openproject import Status, WorkPackageDetail
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine, make_session_factory
from daily_task_bot.db.models import DsrSubmission, Employee, ExternalSyncRecord, ReminderSent
from daily_task_bot.domain.models import WorkPackage
from daily_task_bot.services.chat_events import handle_chat_event
from daily_task_bot.services.chat_reminders import run_reminder_cycle, send_evening_reminders

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)
TODAY = date(2026, 11, 23)
CHAT_USER = "users/700"
OP_USER = 42
TABLES = ["external_sync_records", "reminders_sent", "dsr_entries", "dsr_submissions",
          "daily_plan_items", "daily_plans", "employees"]
_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None


class FakeOpenProject:
    def __init__(self, assigned=(2481,), closed=()):
        self.assigned = list(assigned)
        self.closed = set(closed)
        self.statuses = {7: Status(7, "In progress", False), 11: Status(11, "Test failed", False),
                         10: Status(10, "Tested", False)}

    async def get_statuses(self):
        return self.statuses

    async def get_open_work_packages(self, statuses, project_ids=None):
        return [WorkPackage(id=w, subject=f"Task {w}", status_name="In progress", status_id=7, status_is_closed=False,
                            assignee_id=OP_USER, start_date=date(2026, 11, 20), due_date=date(2026, 11, 30))
                for w in self.assigned if w not in self.closed]

    async def get_work_package(self, wp_id, statuses=None):
        if wp_id not in self.assigned and wp_id not in self.closed:
            return None
        return WorkPackageDetail(id=wp_id, assignee_id=OP_USER, status_is_closed=wp_id in self.closed,
                                 status_id=7, lock_version=1,
                                 start_date=date(2026, 11, 20), due_date=date(2026, 11, 30))


class FakeGChat:
    def __init__(self):
        self.cards = []
        self.texts = []

    async def find_direct_message(self, chat_user_id):
        return f"spaces/dm-{chat_user_id}"

    async def post_card(self, space, body):
        self.cards.append((space, body))
        return {}

    async def post_message(self, space, text):
        self.texts.append((space, text))
        return {}


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


@pytest.fixture
def cleanup(engine):
    yield
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


def _settings(**overrides):
    base = dict(_env_file=None, timezone="Asia/Kolkata", dsr_status_choices=[7, 11],
                dsr_go_live_date=date(2026, 11, 1), allow_external_writes=True,
                pilot_chat_user_ids=[CHAT_USER])
    base.update(overrides)
    return Settings(**base)


def _employee(session, chat=CHAT_USER, op=OP_USER, active=True):
    emp = Employee(name="Asha", email=f"chat-{uuid.uuid4().hex[:8]}@example.com",
                   openproject_user_id=op, chat_user_id=chat, is_active=active)
    session.add(emp)
    session.flush()
    return emp


def _submit_event(wp_hours, day=TODAY.isoformat(), user=CHAT_USER):
    inputs = {}
    for wp, (hours, work, status) in wp_hours.items():
        inputs[f"hours_{wp}"] = {"stringInputs": {"value": [hours]}}
        inputs[f"work_{wp}"] = {"stringInputs": {"value": [work]}}
        inputs[f"remaining_{wp}"] = {"stringInputs": {"value": ["Next step"]}}
        inputs[f"status_{wp}"] = {"stringInputs": {"value": [status]}}
    return {"type": "CARD_CLICKED", "user": {"name": user},
            "common": {"invokedFunction": "submit_dsr", "parameters": {"dsr_date": day},
                       "formInputs": inputs}}


def _handle(session, event, op=None, settings=None):
    return asyncio.run(handle_chat_event(event, session=session, op_client=op or FakeOpenProject(),
                                         settings=settings or _settings(), now=NOW))


def test_message_containing_dsr_returns_todays_card(session):
    _employee(session)
    body = _handle(session, {"type": "MESSAGE", "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}, "user": {"name": CHAT_USER},
                             "message": {"text": "DSR please"}})
    assert "cardsV2" in body


def test_unknown_chat_user_is_refused_without_details(session):
    body = _handle(session, {"type": "MESSAGE", "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}, "user": {"name": "users/999"}, "message": {"text": "dsr"}})
    assert "not linked" in body["text"] and "users/999" not in body["text"]


def test_valid_submission_records_and_confirms_the_dsr(session):
    emp = _employee(session)
    body = _handle(session, _submit_event({2481: ("2.5", "Fixed export", "7")}))
    assert "1 task(s) recorded" in body["text"]
    sub = session.scalar(select(DsrSubmission).where(DsrSubmission.employee_id == emp.id))
    assert sub.status == "confirmed" and sub.dsr_date == TODAY
    assert sub.entries[0].hours_spent == Decimal("2.5") and sub.entries[0].new_status_id == 7
    targets = {r.target for r in session.scalars(select(ExternalSyncRecord))}
    assert targets == {"openproject_time", "openproject_status", "jibble_time"}


def test_task_not_assigned_to_the_employee_refuses_the_whole_submission(session):
    emp = _employee(session)
    body = _handle(session, _submit_event({9999: ("1", "x", "")}), op=FakeOpenProject(assigned=(2481,)))
    assert "not visible" in body["text"] or "not assigned" in body["text"]
    assert session.scalar(select(DsrSubmission.id).where(DsrSubmission.employee_id == emp.id)) is None


def test_second_submission_for_the_same_day_is_refused(session):
    _employee(session)
    _handle(session, _submit_event({2481: ("1", "first", "")}))
    body = _handle(session, _submit_event({2481: ("1", "again", "")}))
    assert "already recorded" in body["text"]


def test_submission_for_another_day_is_refused(session):
    _employee(session)
    body = _handle(session, _submit_event({2481: ("1", "x", "")}, day="2026-11-01"))
    assert "different day" in body["text"]


def test_blank_hours_submission_records_nothing(session):
    emp = _employee(session)
    body = _handle(session, _submit_event({2481: ("", "", "")}))
    assert "No hours" in body["text"]
    assert session.scalar(select(DsrSubmission.id).where(DsrSubmission.employee_id == emp.id)) is None


def test_closed_task_refuses_submission(session):
    _employee(session)
    body = _handle(session, _submit_event({2481: ("1", "x", "")}),
                   op=FakeOpenProject(assigned=(2481,), closed=(2481,)))
    assert "closed" in body["text"]


@pytest.mark.usefixtures("cleanup")
def test_evening_reminder_is_sent_once_per_day_and_logged(engine):
    factory = make_session_factory(engine)
    with factory() as s:
        _employee(s)
        s.commit()
    gchat = FakeGChat()
    first = asyncio.run(send_evening_reminders(factory, _settings(), FakeOpenProject(), gchat, now=NOW))
    second = asyncio.run(send_evening_reminders(factory, _settings(), FakeOpenProject(), gchat, now=NOW))
    assert first == {"sent": 1, "skipped": 0, "failed": 0}
    assert second == {"sent": 0, "skipped": 1, "failed": 0}
    assert len(gchat.cards) == 1
    with factory() as s:
        assert s.scalar(select(ReminderSent.id)) is not None


@pytest.mark.usefixtures("cleanup")
def test_evening_reminder_is_not_sent_once_the_dsr_exists(engine):
    factory = make_session_factory(engine)
    with factory() as s:
        emp = _employee(s)
        s.add(DsrSubmission(employee_id=emp.id, dsr_date=TODAY, version=1, status="draft", is_current=True))
        s.commit()
    gchat = FakeGChat()
    result = asyncio.run(send_evening_reminders(factory, _settings(), FakeOpenProject(), gchat, now=NOW))
    assert result["sent"] == 0 and gchat.cards == []


def test_reminders_do_nothing_while_external_writes_are_disabled():
    gchat = FakeGChat()
    result = asyncio.run(run_reminder_cycle(None, _settings(allow_external_writes=False),
                                            FakeOpenProject(), gchat, now=NOW))
    assert result["status"] == "skipped" and gchat.cards == [] and gchat.texts == []


def test_employee_outside_the_pilot_is_not_served(session):
    _employee(session, chat="users/888")
    body = _handle(session, {"type": "MESSAGE", "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}, "user": {"name": "users/888"}, "message": {"text": "dsr"}})
    assert "not enabled" in body["text"]


@pytest.mark.usefixtures("cleanup")
def test_reminders_go_only_to_pilot_employees(engine):
    factory = make_session_factory(engine)
    with factory() as s:
        _employee(s, chat=CHAT_USER)
        _employee(s, chat="users/999", op=77)
        s.commit()
    gchat = FakeGChat()
    result = asyncio.run(send_evening_reminders(factory, _settings(), FakeOpenProject(), gchat, now=NOW))
    assert result["sent"] == 1 and len(gchat.cards) == 1
