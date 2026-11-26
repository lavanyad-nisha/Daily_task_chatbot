"""Space linking (T1): LINK, UNLINK, LINKED, admin-only, private-only commands. Rolled-back DB; no OpenProject."""

import argparse
import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.config import Settings, get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import SpaceLink
from daily_task_bot.domain.chat_commands import parse_command
from daily_task_bot.services.chat_events import handle_chat_event

ROOT = Path(__file__).resolve().parent.parent
ADMIN = "users/900001"
MEMBER = "users/900002"
SPACE = "spaces/LINKTEST1"
OTHER_SPACE = "spaces/LINKTEST2"


def _settings(**overrides):
    base = dict(_env_file=None, bot_admin_chat_user_ids=[ADMIN])
    base.update(overrides)
    return Settings(**base)


def _space_event(text, user=ADMIN, space=SPACE, space_type="ROOM"):
    return {"type": "MESSAGE", "user": {"name": user},
            "space": {"name": space, "type": space_type}, "message": {"text": text}}


def _dm_event(text, user=ADMIN):
    return {"type": "MESSAGE", "user": {"name": user},
            "space": {"name": "spaces/DM1", "type": "DM"}, "message": {"text": text}}


def _run(session, event, settings=None):
    return asyncio.run(handle_chat_event(event, session=session, op_client=None,
                                         settings=settings or _settings(), now=None))


# ---- parser ---------------------------------------------------------------------------------

def test_parser_reads_link_with_a_multi_word_client():
    cmd = parse_command("LINK L&T Valves")
    assert cmd.name == "LINK" and cmd.args == {"client": "L&T Valves"}


def test_parser_reads_unlink_and_linked():
    assert parse_command("UNLINK").name == "UNLINK"
    assert parse_command("linked").name == "LINKED"


def test_parser_refuses_link_without_a_client():
    cmd = parse_command("LINK")
    assert cmd.error == "Use: LINK <client>"


# ---- database behaviour (rolled back) -----------------------------------------------------

_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None
pytestmark_db = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")


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


def _rows(session, space_id=SPACE):
    return session.scalars(select(SpaceLink).where(SpaceLink.space_id == space_id)).all()


@pytestmark_db
def test_admin_links_a_space_to_a_client(session):
    reply = _run(session, _space_event("LINK GPL"))
    assert reply["text"] == "Linked this space to GPL."
    row = _rows(session)[0]
    assert row.client_name == "GPL" and row.linked_by == ADMIN


@pytestmark_db
def test_client_name_is_matched_case_insensitively_and_stored_canonically(session):
    _run(session, _space_event("LINK l&t valves"))
    assert _rows(session)[0].client_name == "L&T Valves"


@pytestmark_db
def test_non_admin_cannot_link(session):
    reply = _run(session, _space_event("LINK GPL", user=MEMBER))
    assert reply["text"] == "Only bot admins can link or unlink this space."
    assert _rows(session) == []


@pytestmark_db
def test_link_is_refused_in_a_private_chat(session):
    reply = _run(session, _dm_event("LINK GPL"))
    assert "inside the client's Chat space" in reply["text"]
    assert session.scalars(select(SpaceLink)).all() == []


@pytestmark_db
def test_unknown_client_is_refused(session):
    reply = _run(session, _space_event("LINK Acme"))
    assert reply["text"].startswith("Unknown client 'Acme'")
    assert _rows(session) == []


@pytestmark_db
def test_space_cannot_move_to_another_client_without_unlinking(session):
    _run(session, _space_event("LINK GPL"))
    reply = _run(session, _space_event("LINK TVS"))
    assert reply["text"] == "This space is linked to GPL. Type UNLINK first, then LINK TVS."
    assert _rows(session)[0].client_name == "GPL"


@pytestmark_db
def test_linking_the_same_client_twice_says_so(session):
    _run(session, _space_event("LINK GPL"))
    assert _run(session, _space_event("LINK GPL"))["text"] == "This space is already linked to GPL."


@pytestmark_db
def test_linked_reports_the_client_and_unlinked_says_not_linked(session):
    assert _run(session, _space_event("LINKED"))["text"] == "This space is not linked to a client."
    _run(session, _space_event("LINK GPL"))
    assert _run(session, _space_event("LINKED"))["text"] == "This space is linked to GPL."


@pytestmark_db
def test_unlink_removes_the_link(session):
    _run(session, _space_event("LINK GPL"))
    assert _run(session, _space_event("UNLINK"))["text"] == "Unlinked this space from GPL."
    assert _rows(session) == []


@pytestmark_db
def test_each_space_keeps_its_own_client(session):
    _run(session, _space_event("LINK GPL", space=SPACE))
    _run(session, _space_event("LINK TVS", space=OTHER_SPACE))
    assert _rows(session, SPACE)[0].client_name == "GPL"
    assert _rows(session, OTHER_SPACE)[0].client_name == "TVS"


@pytestmark_db
@pytest.mark.parametrize("text", ["TASKS", "DSR", "PREVIEW", "UPDATE 1 2 done", "STATUS 1 10"])
def test_private_commands_are_refused_in_a_space(session, text):
    reply = _run(session, _space_event(text, user=MEMBER))
    assert reply["text"].startswith("Please message me privately to use ")


@pytestmark_db
def test_ordinary_text_in_a_space_gets_no_reply(session):
    assert _run(session, _space_event("good morning all", user=MEMBER)) == {}


@pytestmark_db
def test_admin_command_with_no_client_gets_the_usage_message(session):
    reply = _run(session, _space_event("LINK"))
    assert reply["text"] == "Use: LINK <client>"
