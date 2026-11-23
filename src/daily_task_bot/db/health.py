"""Database connectivity check. Opens a connection, runs SELECT 1, and reports the result."""

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar() == 1
    except SQLAlchemyError:
        return False
