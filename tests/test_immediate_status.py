"""STATUS sent to OpenProject straight away (today's DSR only). Offline fake OpenProject; rolled-back DB."""

import argparse
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from daily_task_bot.clients.openproject import WorkPackageDetail
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import Employee, ExternalSyncRecord
from daily_task_bot.domain.models import WorkPackage
from daily_task_bot.services.chat_events import handle_chat_event

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 25)
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
OP_USER = 90007
CHAT_USER = "users/90007"


class FakeOpenProject:
    def __init__(self, *, allowed=frozenset({7, 10})):
        self.allowed = allowed
        self.updates: list[dict] = []

    async def get_statuses(self):
        return {}

    async def get_open_work_packages(self, statuses, project_ids=None):
        return [WorkPackage(id=1, subject="Login UAT", status_name="In progress", status_is_closed=False,
                            status_id=7, assignee_id=OP_USER, project_id=7)]

    async def get_work_package(self, wp_id, statuses=None):
        return WorkPackageDetail(id=wp_id, assignee_id=OP_USER, status_is_closed=False, status_id=7,
                                 lock_version=3, can_update=True, project_id=7)

    async def get_allowed_status_ids(self, wp_id, *, lock_version):
        return set(self.allowed)

    async def update_work_package_status(self, wp_id, *, lock_version, status_id):
        self.updates.append({"wp": wp_id, "lock_version": lock_version, "status_id": status_id})


def _settings(**overrides):
    base = dict(_env_file=None, allow_external_writes=True, dry_run=False,
                pilot_chat_user_ids=[CHAT_USER], dsr_status_choices=[7, 10])
    base.update(overrides)
    return Settings(**base)


def _event(text):
    return {"type": "MESSAGE", "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}, "user": {"name": CHAT_USER}, "message": {"text": text}}


async def _hours_then_status(session, op, settings, status_id):
    """A STATUS needs hours logged for the task today first, so log them, then change the status."""
    await handle_chat_event(_event("UPDATE 1 2 Login UAT done"), session=session, op_client=op,
                            settings=settings, now=NOW)
    return await handle_chat_event(_event(f"STATUS 1 {status_id}"), session=session, op_client=op,
                                   settings=settings, now=NOW)


_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None


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


def _employee(session):
    emp = Employee(name="Immediate Test",
                   email=f"immediate-{datetime.now(UTC).timestamp()}@example.com",
                   openproject_user_id=OP_USER, chat_user_id=CHAT_USER, is_active=True)
    session.add(emp)
    session.flush()
    return emp


@pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")
async def test_status_is_sent_to_openproject_at_once_when_writes_are_on(session):
    _employee(session)
    op = FakeOpenProject()
    reply = await _hours_then_status(session, op, _settings(), 10)
    assert "Updated in OpenProject" in reply["text"]
    assert op.updates == [{"wp": 1, "lock_version": 3, "status_id": 10}]
    record = session.query(ExternalSyncRecord).filter_by(target="openproject_status").one()
    assert record.status == "sent"


@pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")
async def test_dry_run_saves_but_sends_nothing(session):
    _employee(session)
    op = FakeOpenProject()
    reply = await _hours_then_status(session, op, _settings(dry_run=True), 10)
    assert op.updates == []
    assert "Updated in OpenProject" not in reply["text"]


@pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")
async def test_status_refused_by_openproject_workflow_is_reported_not_forced(session):
    _employee(session)
    op = FakeOpenProject(allowed=frozenset({7}))
    reply = await _hours_then_status(session, op, _settings(), 10)
    assert op.updates == []
    assert "did not accept the status" in reply["text"]
    assert "workflow" in reply["text"]
