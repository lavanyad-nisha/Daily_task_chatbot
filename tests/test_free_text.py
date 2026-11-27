"""Free-text task updates: the AI only proposes, the code checks, the person saves.

No network: the AI is a fake. Handler tests run against the test database.
"""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
import test_task_updates as shared  # same database setup and fakes as the UPDATE/STATUS tests
from sqlalchemy import select
from test_task_updates import CHAT_USER, NOW, TEST_URL, TODAY, FakeOpenProject, _employee, _settings

from daily_task_bot.db.models import DsrEntry, DsrSubmission
from daily_task_bot.domain.chat_cards import (
    CANCEL_UPDATE_FUNCTION,
    CONFIRM_UPDATE_FUNCTION,
    CardTask,
    StatusChoice,
    build_update_card,
    parse_update_form,
)
from daily_task_bot.services import free_text
from daily_task_bot.services.chat_events import handle_chat_event
from daily_task_bot.services.free_text import (
    HaikuInterpreter,
    Proposal,
    build_user_message,
    check_proposal,
    make_haiku_interpreter,
)

engine = shared.engine
session = shared.session

pytestmark_db = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")

TASKS = [CardTask(2232, "Stage 1-8 enrichment", "On hold"), CardTask(2481, "R2 re-extraction 422", "Test failed")]
STATUSES = [StatusChoice(7, "In progress"), StatusChoice(10, "Tested")]


# ---- the code checks every value the AI returns -------------------------------------------------

def _raw(**kw):
    base = {"task_id": None, "hours": None, "work_done": None, "status": None, "requested_status": None}
    return {**base, **kw}


def test_a_task_that_was_not_offered_is_dropped():
    assert check_proposal(_raw(task_id=9999, hours=2), TASKS, STATUSES).task is None
    assert check_proposal(_raw(task_id=True), TASKS, STATUSES).task is None
    assert check_proposal(_raw(task_id=2232), TASKS, STATUSES).task == TASKS[0]


@pytest.mark.parametrize("hours, expected", [(2, Decimal("2.00")), (1.5, Decimal("1.50")), (0, None),
                                             (-1, None), (25, None), (24, Decimal("24.00")), ("2", None),
                                             (True, None)])
def test_hours_must_be_above_0_and_at_most_24(hours, expected):
    assert check_proposal(_raw(task_id=2232, hours=hours), TASKS, STATUSES).hours == expected


def test_status_is_matched_by_name_from_the_approved_list_only():
    assert check_proposal(_raw(status="tested"), TASKS, STATUSES).status_id == 10
    unknown = check_proposal(_raw(status="Closed", requested_status="completed"), TASKS, STATUSES)
    assert unknown.status_id is None and unknown.requested_status == "completed"


def test_requested_status_is_dropped_when_it_matched():
    p = check_proposal(_raw(status="Tested", requested_status="done"), TASKS, STATUSES)
    assert p.status_id == 10 and p.requested_status is None


def test_blank_work_is_none_and_long_work_is_cut():
    assert check_proposal(_raw(work_done="   "), TASKS, STATUSES).work_done is None
    assert len(check_proposal(_raw(work_done="x" * 5000), TASKS, STATUSES).work_done) == 2000


def test_only_tasks_statuses_and_the_message_are_sent():
    text = build_user_message("2232 done", TASKS, STATUSES)
    assert "#2232 Stage 1-8 enrichment (On hold)" in text and "In progress, Tested" in text
    assert text.endswith("Message:\n2232 done")


# ---- the AI call ---------------------------------------------------------------------------------

class _FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _response(payload, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(payload))])


def _interpret(messages, text="2232 tested, 2h retested"):
    return asyncio.run(HaikuInterpreter(SimpleNamespace(messages=messages)).interpret(text, TASKS, STATUSES))


def test_ai_answer_is_checked_and_returned():
    messages = _FakeMessages(_response(_raw(task_id=2232, hours=2, work_done="retested", status="Tested")))
    p = _interpret(messages)
    assert p == Proposal(TASKS[0], Decimal("2.00"), "retested", 10, None)
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"


def test_refusal_or_failure_gives_none(monkeypatch):
    sent: list[str] = []

    class _Recorder:
        def warning(self, message, *args):
            sent.append(message % args)

    monkeypatch.setattr(free_text, "log", _Recorder())
    assert _interpret(_FakeMessages(_response(_raw(), stop="refusal"))) is None
    assert _interpret(_FakeMessages(error=RuntimeError("secret text"))) is None
    logged = " ".join(sent)
    assert "refusal" in logged and "RuntimeError" in logged
    assert "secret text" not in logged and "2232" not in logged


def test_client_has_a_10_second_limit_and_no_retry():
    interpreter = make_haiku_interpreter("sk-test-not-used")
    assert interpreter._client.timeout == free_text.TIMEOUT_SECONDS == 10.0
    assert interpreter._client.max_retries == 0


# ---- the check-and-save form ---------------------------------------------------------------------

def _widgets(card):
    return card["cardsV2"][0]["card"]["sections"][0]["widgets"]


def test_form_is_pre_filled_and_has_save_and_cancel():
    card = build_update_card(task=TASKS[0], dsr_date=TODAY, hours=Decimal("2.00"), work_done="retested",
                             status_id=10, requested_status=None, status_choices=STATUSES)
    inputs = {w["textInput"]["name"]: w["textInput"]["value"] for w in _widgets(card) if "textInput" in w}
    assert inputs == {"hours_2232": "2", "work_2232": "retested"}
    items = next(w["selectionInput"]["items"] for w in _widgets(card) if "selectionInput" in w)
    assert [i["value"] for i in items if i["selected"]] == ["10"]
    buttons = next(w["buttonList"]["buttons"] for w in _widgets(card) if "buttonList" in w)
    assert [b["onClick"]["action"]["function"] for b in buttons] == [CONFIRM_UPDATE_FUNCTION, CANCEL_UPDATE_FUNCTION]


def test_form_says_when_the_asked_status_cannot_be_set():
    card = build_update_card(task=TASKS[0], dsr_date=TODAY, hours=None, work_done=None, status_id=None,
                             requested_status="completed", status_choices=STATUSES)
    texts = [w["textParagraph"]["text"] for w in _widgets(card) if "textParagraph" in w]
    assert '"completed" is not a status you can set. Choose one of: In progress, Tested.' in texts


def _form(**values):
    return {name: {"stringInputs": {"value": [value]}} for name, value in values.items()}


def test_form_parser_checks_hours_and_status_again():
    ok = parse_update_form(_form(hours_2232="1.5", work_2232=" did it ", status_2232="10"), 2232,
                           frozenset({7, 10}))
    assert (ok.hours, ok.work_done, ok.status_id, ok.errors) == (Decimal("1.5"), "did it", 10, [])
    bad = parse_update_form(_form(hours_2232="30", status_2232="99"), 2232, frozenset({7, 10}))
    assert len(bad.errors) == 2


# ---- the chat handler ----------------------------------------------------------------------------

class FakeInterpreter:
    def __init__(self, proposal="auto"):
        self.proposal, self.calls = proposal, []

    async def interpret(self, text, tasks, statuses):
        self.calls.append((text, [t.work_package_id for t in tasks], [s.status_id for s in statuses]))
        if self.proposal == "auto":
            task = next(t for t in tasks if str(t.work_package_id) in text)
            return Proposal(task, Decimal(2), "retested the export", 11, None)
        return self.proposal


def _dm(text):
    return {"type": "MESSAGE", "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"},
            "user": {"name": CHAT_USER}, "message": {"text": text}}


def _run(session, event, interpreter=None, **overrides):
    settings = _settings().model_copy(update={"free_text_updates": True, **overrides})
    return asyncio.run(handle_chat_event(event, session=session, op_client=FakeOpenProject(), settings=settings,
                                         now=NOW, interpreter=interpreter))


def _click(function, task="2481", day=None, **inputs):
    day = day or TODAY.isoformat()
    return {"type": "CARD_CLICKED", "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"},
            "user": {"name": CHAT_USER},
            "common": {"invokedFunction": function, "parameters": {"dsr_date": day, "task": task},
                       "formInputs": _form(**inputs)}}


def _entries(session):
    return session.scalars(select(DsrEntry).join(DsrSubmission).where(DsrSubmission.dsr_date == TODAY)).all()


@pytestmark_db
def test_free_text_gets_a_pre_filled_form_and_nothing_is_saved_yet(session):
    _employee(session)
    ai = FakeInterpreter()
    reply = _run(session, _dm("update the 2481 - completed, 2h retested the export"), ai)
    assert reply["cardsV2"][0]["cardId"] == "update-2481"
    assert ai.calls[0][1] == [1774, 2481] and ai.calls[0][2] == [7, 11]
    assert _entries(session) == []


@pytestmark_db
def test_valid_commands_never_go_to_the_ai(session):
    _employee(session)
    ai = FakeInterpreter()
    assert "Your tasks" in _run(session, _dm("TASKS"), ai)["text"]
    assert ai.calls == []


@pytestmark_db
def test_feature_off_or_no_ai_gives_the_help_text(session):
    _employee(session)
    ai = FakeInterpreter()
    assert "Sorry, I did not understand that." in _run(session, _dm("2481 done"), ai, free_text_updates=False)["text"]
    assert ai.calls == []
    assert "Sorry, I did not understand that." in _run(session, _dm("2481 done"), None)["text"]


@pytestmark_db
def test_ai_failure_gives_the_help_text(session):
    _employee(session)
    assert "Sorry, I did not understand that." in _run(session, _dm("2481 done"), FakeInterpreter(None))["text"]


@pytestmark_db
def test_unclear_task_gets_a_short_hint(session):
    _employee(session)
    nothing = Proposal(None, None, None, None, None)
    assert "could not tell which of your tasks" in _run(session, _dm("did stuff"), FakeInterpreter(nothing))["text"]


@pytestmark_db
def test_save_records_hours_work_and_status(session):
    _employee(session)
    reply = _run(session, _click(CONFIRM_UPDATE_FUNCTION, hours_2481="2", work_2481="retested", status_2481="11"))
    assert reply["text"].startswith("Saved task #2481")
    (entry,) = _entries(session)
    assert (entry.work_package_id, entry.hours_spent, entry.work_done, entry.new_status_id) == \
        (2481, Decimal(2), "retested", 11)


@pytestmark_db
def test_status_only_without_hours_today_asks_for_hours(session):
    _employee(session)
    reply = _run(session, _click(CONFIRM_UPDATE_FUNCTION, status_2481="11"))
    assert "Record hours and work" in reply["text"]
    assert _entries(session) == []


@pytestmark_db
def test_cancel_saves_nothing(session):
    _employee(session)
    assert _run(session, _click(CANCEL_UPDATE_FUNCTION, hours_2481="2", work_2481="x"))["text"] == \
        "Cancelled. Nothing was saved."
    assert _entries(session) == []


@pytestmark_db
def test_save_refuses_a_task_that_is_not_assigned_or_another_day(session):
    _employee(session)
    assert "not visible" in _run(session, _click(CONFIRM_UPDATE_FUNCTION, task="5555", hours_5555="1",
                                                  work_5555="x"))["text"]
    assert "different day" in _run(session, _click(CONFIRM_UPDATE_FUNCTION, day="2026-11-22", hours_2481="1",
                                                    work_2481="x"))["text"]
    assert _entries(session) == []


@pytestmark_db
def test_save_refuses_a_status_that_is_not_approved(session):
    _employee(session)
    reply = _run(session, _click(CONFIRM_UPDATE_FUNCTION, hours_2481="1", work_2481="x", status_2481="10"))
    assert "not an approved choice" in reply["text"]


@pytestmark_db
def test_not_used_in_a_space(session):
    _employee(session)
    ai = FakeInterpreter()
    event = {"type": "MESSAGE", "space": {"name": "spaces/ROOM1", "spaceType": "SPACE"},
             "user": {"name": CHAT_USER}, "message": {"text": "2481 done", "argumentText": "2481 done"}}
    _run(session, event, ai)
    assert ai.calls == []
