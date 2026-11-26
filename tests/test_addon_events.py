"""Chat app built as a Google Workspace add-on: event conversion, replies and token signers.

Offline: no database, no network. Google token verification is replaced by a fake.
"""

import pytest
from fastapi.testclient import TestClient

from daily_task_bot.api import addon
from daily_task_bot.api.addon import ACTION_PARAM, is_addon_event, to_addon_response, to_chat_event
from daily_task_bot.api.internal import get_openproject_port, get_session_factory
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.domain.chat_cards import SUBMIT_FUNCTION
from daily_task_bot.main import app
from daily_task_bot.services import chat_events
from daily_task_bot.services.chat_events import ChatAuthError, find_employee, verify_chat_token

URL = "https://bot.example.test/daily-task-bot/chat"
ADDON_SA = "service-123@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
USER = {"name": "users/700", "email": "Asha@Example.com", "displayName": "Asha"}


def _addon(chat_part, common=None):
    return {"commonEventObject": common or {"hostApp": "CHAT"}, "chat": {"user": USER, **chat_part}}


# ---- conversion -------------------------------------------------------------------

def test_plain_events_are_not_addon_events():
    assert not is_addon_event({"type": "MESSAGE", "message": {"text": "dsr"}})
    assert is_addon_event(_addon({"messagePayload": {}}))


def test_message_event_becomes_plain_message():
    event = to_chat_event(_addon({"messagePayload": {"message": {"text": "tasks"}, "space": {"name": "spaces/A"}}}))
    assert event["type"] == "MESSAGE"
    assert event["message"]["text"] == "tasks"
    assert event["user"]["name"] == "users/700"
    assert event["space"]["name"] == "spaces/A"


def test_added_and_removed_events():
    assert to_chat_event(_addon({"addedToSpacePayload": {"space": {"name": "spaces/A"}}}))["type"] == "ADDED_TO_SPACE"
    assert to_chat_event(_addon({"removedFromSpacePayload": {}}))["type"] == "REMOVED_FROM_SPACE"
    assert to_chat_event(_addon({}))["type"] is None


def test_button_click_restores_own_action_and_keeps_form_inputs():
    inputs = {"hours_5": {"stringInputs": {"value": ["2"]}}}
    event = to_chat_event(_addon(
        {"buttonClickedPayload": {"message": {"name": "m"}}},
        {"invokedFunction": URL, "parameters": {ACTION_PARAM: SUBMIT_FUNCTION, "dsr_date": "2026-11-24"},
         "formInputs": inputs}))
    assert event["type"] == "CARD_CLICKED"
    assert event["common"]["invokedFunction"] == SUBMIT_FUNCTION
    assert event["common"]["parameters"] == {"dsr_date": "2026-11-24"}
    assert event["common"]["formInputs"] == inputs


def test_button_click_with_unknown_action_is_not_restored():
    event = to_chat_event(_addon({"buttonClickedPayload": {}},
                                 {"invokedFunction": URL, "parameters": {ACTION_PARAM: "drop_tables"}}))
    assert event["common"]["invokedFunction"] == URL


# ---- replies ------------------------------------------------------------------------

def test_text_reply_is_wrapped_as_create_message_action():
    reply = to_addon_response({"text": "hi"}, URL, "MESSAGE")
    assert reply == {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": {"text": "hi"}}}}}


def test_removed_from_space_gets_empty_reply():
    assert to_addon_response({"text": "bye"}, URL, "REMOVED_FROM_SPACE") == {}


def test_card_button_points_at_endpoint_and_round_trips():
    card = {"cardsV2": [{"card": {"sections": [{"widgets": [{"buttonList": {"buttons": [{"onClick": {
        "action": {"function": SUBMIT_FUNCTION, "parameters": [{"key": "dsr_date", "value": "2026-11-24"}]}}}]}}]}]}}]}
    reply = to_addon_response(card, URL, "MESSAGE")
    action = (reply["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
              ["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"][0]["onClick"]["action"])
    assert action["function"] == URL
    assert {"key": ACTION_PARAM, "value": SUBMIT_FUNCTION} in action["parameters"]
    # the original body is not modified
    assert card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"][0][
        "onClick"]["action"]["function"] == SUBMIT_FUNCTION
    params = {p["key"]: p["value"] for p in action["parameters"]}
    back = to_chat_event(_addon({"buttonClickedPayload": {}}, {"invokedFunction": URL, "parameters": params}))
    assert back["common"]["invokedFunction"] == SUBMIT_FUNCTION


# ---- token signers ---------------------------------------------------------------------

@pytest.fixture
def fake_claims(monkeypatch):
    holder = {}
    monkeypatch.setattr(chat_events.id_token, "verify_oauth2_token",
                        lambda token, request, audience: dict(holder["claims"]))
    return holder


def test_chat_signer_accepted(fake_claims):
    fake_claims["claims"] = {"iss": "https://accounts.google.com", "email": chat_events.CHAT_ISSUER}
    assert verify_chat_token("t", URL)


def test_addon_signer_accepted_only_when_configured(fake_claims):
    fake_claims["claims"] = {"iss": "https://accounts.google.com", "email": ADDON_SA, "email_verified": True}
    assert verify_chat_token("t", URL, ADDON_SA)
    with pytest.raises(ChatAuthError):
        verify_chat_token("t", URL, None)


def test_other_projects_addon_signer_refused(fake_claims):
    fake_claims["claims"] = {"iss": "https://accounts.google.com",
                             "email": "service-999@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
                             "email_verified": True}
    with pytest.raises(ChatAuthError):
        verify_chat_token("t", URL, ADDON_SA)


def test_unverified_addon_email_refused(fake_claims):
    fake_claims["claims"] = {"iss": "https://accounts.google.com", "email": ADDON_SA, "email_verified": False}
    with pytest.raises(ChatAuthError):
        verify_chat_token("t", URL, ADDON_SA)


# ---- email linking (fake session) ------------------------------------------------------------

class _Emp:
    def __init__(self, email, chat_user_id=None):
        self.email, self.chat_user_id, self.is_active = email, chat_user_id, True


class _Session:
    """Answers the two find_employee queries in order: by Chat ID, then by email."""

    def __init__(self, by_chat, by_email):
        self.answers = [by_chat, by_email]
        self.flushed = False

    def scalar(self, _query):
        return self.answers.pop(0)

    def flush(self):
        self.flushed = True

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_email_links_employee_without_chat_id():
    emp = _Emp("asha@example.com")
    session = _Session(None, emp)
    assert find_employee(session, {"user": USER}) is emp
    assert emp.chat_user_id == "users/700" and session.flushed


def test_email_never_replaces_existing_chat_link():
    emp = _Emp("asha@example.com", chat_user_id="users/111")
    assert find_employee(_Session(None, emp), {"user": USER}) is None
    assert emp.chat_user_id == "users/111"


# ---- endpoint ----------------------------------------------------------------------------------

def _client(monkeypatch, claims):
    settings = Settings(_env_file=None, chat_endpoint_audience=URL, chat_addon_service_account=ADDON_SA)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_factory] = lambda: (lambda: _Session(None, None))
    app.dependency_overrides[get_openproject_port] = lambda: object()
    monkeypatch.setattr(chat_events.id_token, "verify_oauth2_token", lambda t, r, audience: claims)
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_endpoint_answers_addon_event_in_addon_format(monkeypatch):
    client = _client(monkeypatch, {"email": ADDON_SA, "email_verified": True})
    response = client.post("/chat", headers={"Authorization": "Bearer t"},
                           json=_addon({"messagePayload": {"message": {"text": "help"}, "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}}}))
    assert response.status_code == 200
    text = response.json()["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]["text"]
    assert "not linked" in text


def test_endpoint_refuses_foreign_signer(monkeypatch):
    client = _client(monkeypatch, {"email": "someone@example.com", "email_verified": True})
    response = client.post("/chat", headers={"Authorization": "Bearer t"},
                           json=_addon({"messagePayload": {"message": {"text": "help"}, "space": {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}}}))
    assert response.status_code == 401


def test_addon_module_has_no_io():
    assert not hasattr(addon, "httpx")
