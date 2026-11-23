"""T1 tests against a real PostgreSQL test database.

Skipped unless DTB_TEST_DATABASE_URL is set. The database name must contain "test".
Example: DTB_TEST_DATABASE_URL=postgresql+psycopg://dtb_app:<pw>@localhost:5432/dtb_test
"""

import argparse
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine, make_session_factory
from daily_task_bot.db.health import ping

ROOT = Path(__file__).resolve().parent.parent
_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None

pytestmark = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")


@pytest.fixture
def engine():
    assert_test_database(TEST_URL)
    eng = make_engine(TEST_URL)
    yield eng
    eng.dispose()


def _alembic(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    return cfg


def test_database_is_reachable(engine):
    assert ping(engine) is True


def test_session_can_open_and_roll_back(engine):
    session = make_session_factory(engine)()
    try:
        assert session.execute(text("SELECT 1")).scalar() == 1
    finally:
        session.close()


def _current_revision(engine):
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def test_migrations_upgrade_and_downgrade_cleanly(engine):
    from alembic.script import ScriptDirectory

    cfg = _alembic(TEST_URL)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    command.downgrade(cfg, "base")
    assert _current_revision(engine) is None
    try:
        command.upgrade(cfg, "head")
        assert _current_revision(engine) == head
        assert "alembic_version" in inspect(engine).get_table_names()
        command.downgrade(cfg, "base")
        assert _current_revision(engine) is None
    finally:
        command.upgrade(cfg, "head")
