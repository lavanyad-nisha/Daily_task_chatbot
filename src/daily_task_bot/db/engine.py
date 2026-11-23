"""Engine and session factory. Pure construction; no connection is opened until used."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when a test would run against a database that is not clearly a test database."""


# Exact allowlist. A substring check would accept names such as "contest_prod".
# To use a different test database, add its name here deliberately.
ALLOWED_TEST_DATABASES = frozenset({"dtb_test"})


def assert_test_database(url: str) -> None:
    name = make_url(url).database or ""
    if name not in ALLOWED_TEST_DATABASES:
        raise UnsafeTestDatabaseError(
            f"Refusing to run tests against database '{name}': "
            f"allowed test databases are {sorted(ALLOWED_TEST_DATABASES)}."
        )


def make_engine(url: str) -> Engine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("Database URL must use the postgresql+psycopg:// driver.")
    return create_engine(url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
