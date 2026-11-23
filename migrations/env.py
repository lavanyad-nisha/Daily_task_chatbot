"""Alembic environment.

The database URL is taken from, in order:
1. the -x url=... argument (used by tests and one-off runs),
2. DTB_DATABASE_URL from the environment or .env.

No URL or credentials are stored in alembic.ini.
"""

from logging.config import fileConfig

from alembic import context

from daily_task_bot.config import get_settings
from daily_task_bot.db import models  # noqa: F401  (registers tables on Base.metadata)
from daily_task_bot.db.base import Base
from daily_task_bot.db.engine import make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError(
            "No database URL. Set DTB_DATABASE_URL or pass -x url=... to Alembic."
        )
    return settings.database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url()
    engine = make_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
