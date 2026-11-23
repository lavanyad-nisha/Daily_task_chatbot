"""T1 tests that need no running database."""

import argparse
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from daily_task_bot.config import Settings
from daily_task_bot.db.base import Base
from daily_task_bot.db.engine import (
    UnsafeTestDatabaseError,
    assert_test_database,
    make_engine,
)

ROOT = Path(__file__).resolve().parent.parent
FAKE_URL = "postgresql+psycopg://dtb_app:pw@localhost:5432/dtb_test"


def test_settings_default_to_safe_values():
    settings = Settings(_env_file=None)
    assert settings.database_url is None
    assert settings.dry_run is True
    assert settings.allow_external_writes is False


def test_engine_requires_psycopg_driver():
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        make_engine("postgresql://u:p@localhost/dtb_test")


def test_engine_builds_without_connecting():
    engine = make_engine(FAKE_URL)
    assert engine.dialect.name == "postgresql"
    engine.dispose()


def test_test_database_guard_accepts_test_names():
    assert_test_database(FAKE_URL)


@pytest.mark.parametrize("name", [
    "dtb_dev",
    "postgres",
    "contest_prod",       # contains "test": the old substring check accepted this
    "dtb_testing_prod",
    "latest_orders",
    "dtb_test_prod",
    "DTB_TEST_PROD",
    "",
])
def test_test_database_guard_refuses_non_allowlisted_names(name):
    with pytest.raises(UnsafeTestDatabaseError):
        assert_test_database(f"postgresql+psycopg://u:p@localhost:5432/{name}")
def test_naming_convention_is_applied():
    convention = Base.metadata.naming_convention
    assert convention["pk"] == "pk_%(table_name)s"
    assert convention["fk"].startswith("fk_")


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    return cfg


def test_alembic_scripts_load_and_head_is_single():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config(FAKE_URL))
    assert len(script.get_heads()) == 1


def test_alembic_offline_sql_generates_without_database(capsys):
    command.upgrade(_alembic_config(FAKE_URL), "head", sql=True)
    out = capsys.readouterr().out
    assert "alembic_version" in out


def test_alembic_refuses_to_run_without_any_url(monkeypatch):
    monkeypatch.delenv("DTB_DATABASE_URL", raising=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=None)
    monkeypatch.setattr("daily_task_bot.config.get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(RuntimeError, match="No database URL"):
        command.upgrade(cfg, "head", sql=True)
