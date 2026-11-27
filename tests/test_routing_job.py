"""Daily routed posting job: dry run, live posts, no duplicates, and the time gate. Rolled-back DB; fakes."""

import argparse
import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from daily_task_bot.clients.openproject import MembershipRecord, WorkPackageDetail
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import Employee, EmployeeProjectRole, ProjectRoute, RoutedPost
from daily_task_bot.services.dsr import EntryInput, create_draft
from daily_task_bot.services.routing_job import is_due, run_routing_cycle

ROOT = Path(__file__).resolve().parent.parent
OP_USER = 90101
CHAT_USER = "users/90101"
SPACE = "spaces/ROUTEJOB"
NOW_DUE = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)  # 19:00 in Asia/Kolkata
NOW_EARLY = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)  # 17:30 in Asia/Kolkata
DAY = date(2026, 11, 25)


class FakeOpenProject:
    async def list_user_memberships(self):
        return [MembershipRecord(project_id=10, user_id=OP_USER, role_names=("QA",))]

    async def get_statuses(self):
        return {}

    async def get_work_package(self, wp_id, statuses=None):
        return WorkPackageDetail(id=wp_id, assignee_id=OP_USER, status_is_closed=False, status_id=7,
                                 lock_version=1, can_update=True, project_id=10)


class FakeChat:
    def __init__(self):
        self.posts: list[tuple[str, str]] = []

    async def post_message(self, space, text):
        self.posts.append((space, text))
        return {"name": f"{space}/messages/x"}


def _settings(**overrides):
    base = dict(_env_file=None, pilot_chat_user_ids=[CHAT_USER], allow_external_writes=True, dry_run=True,
                timezone="Asia/Kolkata", routed_post_time="18:30")
    base.update(overrides)
    return Settings(**base)


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
def world(engine):
    """A pilot employee with a QA role on project 10, a route to SPACE, and one saved entry for DAY."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    emp = Employee(name="Routing Job Test", email=f"routing-job-{datetime.now(UTC).timestamp()}@example.com",
                   openproject_user_id=OP_USER, chat_user_id=CHAT_USER, is_active=True)
    session.add(emp)
    session.flush()
    session.add(EmployeeProjectRole(employee_id=emp.id, openproject_project_id=10, role_name="QA"))
    session.add(ProjectRoute(openproject_project_id=10, role_name="QA", chat_space_id=SPACE,
                             space_label="Routing job test"))
    create_draft(session, employee_id=emp.id, dsr_date=DAY, entries=[
        EntryInput(work_package_id=701, hours_spent=Decimal("2"), work_done="Login UAT passed on staging."),
    ])
    session.flush()
    factory = sessionmaker(bind=connection, expire_on_commit=False)
    yield {"factory": factory, "session": session}
    session.close()
    transaction.rollback()
    connection.close()


def _run(world, settings, now, chat):
    return asyncio.run(run_routing_cycle(world["factory"], settings, FakeOpenProject(), chat,
                                         now=now, summarizer=None))


pytestmark_db = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")


@pytestmark_db
def test_dry_run_logs_what_would_post_and_records_nothing(world):
    chat = FakeChat()
    report = _run(world, _settings(dry_run=True), NOW_DUE, chat)
    assert report["would_post"] == 1 and report["posted"] == 0
    assert chat.posts == []
    assert world["session"].scalar(select(func.count()).select_from(RoutedPost)) == 0


@pytestmark_db
def test_writes_off_never_posts_even_when_not_dry_run(world):
    chat = FakeChat()
    report = _run(world, _settings(allow_external_writes=False, dry_run=False), NOW_DUE, chat)
    assert report["would_post"] == 1 and chat.posts == []


@pytestmark_db
def test_live_post_is_sent_once_and_recorded(world):
    chat = FakeChat()
    first = _run(world, _settings(dry_run=False), NOW_DUE, chat)
    assert first["posted"] == 1
    assert [s for s, _ in chat.posts] == [SPACE]
    assert "#701: 2.00 h, Login UAT passed on staging." in chat.posts[0][1]
    assert world["session"].scalar(select(func.count()).select_from(RoutedPost)) == 1


@pytestmark_db
def test_second_live_run_the_same_day_does_not_post_again(world):
    chat = FakeChat()
    _run(world, _settings(dry_run=False), NOW_DUE, chat)
    again = _run(world, _settings(dry_run=False), NOW_DUE, chat)
    assert again["posted"] == 0
    assert len(chat.posts) == 1


def test_routed_post_time_gate():
    settings = _settings()
    assert not is_due(settings, NOW_EARLY)
    assert is_due(settings, NOW_DUE)


class CountingSummarizer:
    def __init__(self):
        self.calls = 0

    def summarize(self, text):
        self.calls += 1
        return "Summary."


LONG_TEXT = ("Ran the full login UAT on staging with three user roles, fixed two null checks in the "
             "session handler, retested every case and all of them passed before the end of the day.")


@pytestmark_db
def test_already_posted_space_is_not_summarised_again(world):
    session = world["session"]
    entry_owner = session.scalar(select(Employee).where(Employee.chat_user_id == CHAT_USER))
    create_draft(session, employee_id=entry_owner.id, dsr_date=date(2026, 11, 26), entries=[
        EntryInput(work_package_id=702, hours_spent=Decimal("3"), work_done=LONG_TEXT),
    ])
    session.flush()
    later = datetime(2026, 11, 26, 13, 30, tzinfo=UTC)
    chat, counter = FakeChat(), CountingSummarizer()
    run = lambda: asyncio.run(run_routing_cycle(world["factory"], _settings(dry_run=False), FakeOpenProject(),
                                                chat, now=later, summarizer=counter))
    first = run()
    assert first["posted"] == 1 and counter.calls == 1
    second = run()
    assert second["posted"] == 0 and second["already_posted"] == 1
    assert counter.calls == 1  # no second AI call for a space already posted


def test_live_runs_repeat_after_the_post_time_and_dry_runs_do_not():
    from daily_task_bot.services.routing_job import routing_done_for_today
    live = _settings(dry_run=False, allow_external_writes=True)
    dry = _settings(dry_run=True)
    assert not routing_done_for_today(live, {"status": "waiting"})
    assert not routing_done_for_today(live, {"status": "ran"})  # later DSRs are posted next cycle
    assert routing_done_for_today(dry, {"status": "ran"})
    assert routing_done_for_today(live, {"status": "skipped"})


def test_summary_call_has_a_short_timeout():
    from daily_task_bot.services.routing_job import SUMMARY_TIMEOUT_SECONDS
    assert SUMMARY_TIMEOUT_SECONDS <= 60


class _AiOk:
    def summarize(self, text):
        return "AI summary of the update."


class _AiDown:
    def summarize(self, text):
        raise RuntimeError("service unavailable")


def _run_with(world, summarizer):
    chat = FakeChat()
    report = asyncio.run(run_routing_cycle(world["factory"], _settings(dry_run=False), FakeOpenProject(), chat,
                                           now=NOW_DUE, summarizer=summarizer))
    return report, chat


@pytestmark_db
def test_live_post_records_that_the_ai_made_the_summary(world):
    report, chat = _run_with(world, _AiOk())
    assert report["posted"] == 1
    assert "AI summary of the update." in chat.posts[0][1]
    row = world["session"].scalar(select(RoutedPost))
    assert row.ai_summaries == 1 and row.rule_based_summaries == 0


@pytestmark_db
def test_live_post_records_rule_based_when_the_ai_failed_for_that_summary(world):
    report, chat = _run_with(world, _AiDown())
    assert report["posted"] == 1
    assert "Login UAT passed on staging." in chat.posts[0][1]
    row = world["session"].scalar(select(RoutedPost))
    assert row.ai_summaries == 0 and row.rule_based_summaries == 1
