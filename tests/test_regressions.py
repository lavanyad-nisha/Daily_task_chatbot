"""Regression tests for the four critical backend review findings.

1. Chat lookback filter must be sent in UTC.
2. A live run without a Chat writer must fail, never report dry_run.
3. updated_at must change when a row is updated.
4. The test-database guard must reject look-alike names (covered in test_db_offline.py).
Also covers live configuration validation (missing or placeholder settings).
"""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from daily_task_bot.clients.gchat import CHAT_BASE_URL, GChatClient, chat_filter_timestamp
from daily_task_bot.config import Settings, get_settings, live_configuration_problems
from daily_task_bot.db.engine import make_engine
from daily_task_bot.db.models import Employee
from daily_task_bot.domain.models import Employee as DomainEmployee
from daily_task_bot.main import app
from daily_task_bot.services.daily_run import run_daily_summary

IST = ZoneInfo("Asia/Kolkata")


# ---- 1. Chat lookback filter is UTC -----------------------------------------

def test_chat_filter_converts_ist_to_utc():
    # 09:00 IST is 03:30 UTC. The old code sent "09:00:00Z", which reads as 14:30 IST.
    since = datetime(2026, 11, 19, 9, 0, tzinfo=IST)
    assert chat_filter_timestamp(since) == "2026-11-19T03:30:00Z"


def test_chat_filter_accepts_utc_unchanged():
    since = datetime(2026, 11, 19, 3, 30, tzinfo=UTC)
    assert chat_filter_timestamp(since) == "2026-11-19T03:30:00Z"


def test_chat_filter_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        chat_filter_timestamp(datetime(2026, 11, 19, 9, 0))


async def test_list_messages_sends_utc_filter_over_the_wire():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["filter"] = request.url.params["filter"]
        return httpx.Response(200, json={
            "messages": [{
                "name": "spaces/AAA/messages/1",
                "createTime": "2026-11-19T04:00:00.000000Z",
                "sender": {"name": "users/1"},
                "text": "fixed #10",
            }]
        })

    client = GChatClient.__new__(GChatClient)  # skip credential loading; no real Google call
    client._http = httpx.AsyncClient(base_url=CHAT_BASE_URL, transport=httpx.MockTransport(handler))

    async def fake_headers():
        return {"Authorization": "Bearer test-token"}

    client._auth_headers = fake_headers
    since = datetime(2026, 11, 19, 9, 0, tzinfo=IST)

    messages = await client.list_messages("spaces/AAA", since)

    assert captured["filter"] == 'createTime > "2026-11-19T03:30:00Z"'
    assert len(messages) == 1 and messages[0].sender_id == "users/1"
    await client.aclose()


# ---- 2. Live run without a Chat writer must not be reported as dry run ------

class _OpenProjectMustNotBeCalled:
    async def get_statuses(self):
        raise AssertionError("live run without a Chat writer must fail before any fetch")

    async def get_open_work_packages(self, statuses, project_ids=None):
        raise AssertionError("live run without a Chat writer must fail before any fetch")


async def test_live_run_without_chat_writer_raises_before_fetching():
    employee = DomainEmployee(name="Asha", email="asha@example.com",
                              openproject_user_id=1, chat_user_id="users/1")
    now = datetime(2026, 11, 19, 9, 0, tzinfo=IST)
    with pytest.raises(ValueError, match="requires a Chat writer"):
        await run_daily_summary(
            run_date=now.date(), now=now, employees=[employee], leave_by_email={},
            holidays=set(), openproject=_OpenProjectMustNotBeCalled(),
            chat_writer=None, dry_run=False,
        )


async def test_dry_run_without_chat_writer_still_renders():
    class OP:
        async def get_statuses(self):
            return {}

        async def get_open_work_packages(self, statuses, project_ids=None):
            return []

    employee = DomainEmployee(name="Asha", email="asha@example.com",
                              openproject_user_id=1, chat_user_id="users/1")
    now = datetime(2026, 11, 19, 9, 0, tzinfo=IST)
    report = await run_daily_summary(
        run_date=now.date(), now=now, employees=[employee], leave_by_email={},
        holidays=set(), openproject=OP(), chat_writer=None, dry_run=True,
    )
    assert report.counts() == {"dry_run": 1}


# ---- 3. updated_at changes on update (real database; skipped without test DB)

_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None


@pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")
def test_updated_at_changes_when_row_is_updated():
    import argparse

    from alembic import command
    from alembic.config import Config

    from daily_task_bot.db.engine import assert_test_database

    assert_test_database(TEST_URL)
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={TEST_URL}"])
    command.upgrade(cfg, "head")
    engine = make_engine(TEST_URL)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        emp = Employee(name="Before", email="updated-at-check@example.com")
        session.add(emp)
        session.flush()
        session.refresh(emp)
        before = emp.updated_at

        emp.name = "After"
        session.flush()
        session.refresh(emp)
        after = emp.updated_at

        assert after > before, "updated_at did not change on update"
        assert emp.name == "After"
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


# ---- live configuration validation (no network) -----------------------------

def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, openproject_base_url="https://op.company.internal")
    base.update(overrides)
    return Settings(**base)


def test_defaults_report_every_missing_live_setting():
    problems = live_configuration_problems(_settings())
    joined = " | ".join(problems)
    assert "DTB_OPENPROJECT_API_KEY is missing" in joined
    assert "DTB_GCHAT_SERVICE_ACCOUNT_FILE is missing" in joined
    assert "DTB_RUN_SECRET is missing" in joined


def test_placeholder_openproject_url_is_flagged():
    problems = live_configuration_problems(
        _settings(openproject_base_url="https://openproject.example.com")
    )
    assert any("placeholder" in p for p in problems)


def test_http_openproject_url_is_flagged():
    problems = live_configuration_problems(
        _settings(openproject_base_url="http://op.company.internal")
    )
    assert any("https://" in p for p in problems)


def test_missing_service_account_file_is_flagged(tmp_path: Path):
    problems = live_configuration_problems(
        _settings(gchat_service_account_file=tmp_path / "absent.json")
    )
    assert any("does not point to an existing file" in p for p in problems)


def test_complete_live_configuration_has_no_problems(tmp_path: Path):
    key_file = tmp_path / "service-account.json"
    key_file.write_text("{}", encoding="utf-8")
    problems = live_configuration_problems(_settings(
        openproject_api_key="k",
        gchat_service_account_file=key_file,
        run_secret="s",
    ))
    assert problems == []


# ---- endpoint: live run with incomplete configuration returns 503 -----------

def test_live_run_with_incomplete_configuration_returns_503_with_problems():
    settings = _settings(run_secret="s", dry_run=False, allow_external_writes=True)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = TestClient(app).post("/internal/run", headers={"X-Run-Secret": "s"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["message"] == "live configuration is incomplete"
    assert any("DTB_OPENPROJECT_API_KEY" in p for p in detail["problems"])
