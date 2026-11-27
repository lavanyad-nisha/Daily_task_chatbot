"""Google Workspace add-on event format, for Chat apps built as Google Workspace add-ons.

When "Build this Chat app as a Google Workspace add-on" is ticked in the Chat API configuration,
Google sends events in the add-on shape and expects add-on responses:

  request:  {"commonEventObject": {...}, "chat": {"user": ..., "messagePayload": {...}}}
  response: {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": ...}}}}

This module converts an add-on event into the plain Chat event shape that handle_chat_event()
already understands, and wraps its reply in the add-on response. Pure: no I/O.

Card buttons: an HTTP add-on calls the endpoint URL itself as the action "function", so the
logical action name is carried in a parameter and restored when the click comes back.
"""

import copy
from typing import Any

from daily_task_bot.domain.chat_cards import (
    CANCEL_UPDATE_FUNCTION,
    CONFIRM_UPDATE_FUNCTION,
    SUBMIT_FUNCTION,
)

ACTION_PARAM = "dtb_action"
KNOWN_ACTIONS = frozenset({SUBMIT_FUNCTION, CONFIRM_UPDATE_FUNCTION, CANCEL_UPDATE_FUNCTION})


def is_addon_event(event: Any) -> bool:
    return isinstance(event, dict) and isinstance(event.get("chat"), dict)


def to_chat_event(event: dict[str, Any]) -> dict[str, Any]:
    """Convert an add-on event into the plain Chat event shape."""
    chat = event.get("chat") or {}
    common = event.get("commonEventObject") or {}
    plain: dict[str, Any] = {"user": chat.get("user") or {}, "space": chat.get("space") or {},
                             "eventTime": chat.get("eventTime")}

    if "addedToSpacePayload" in chat:
        payload = chat.get("addedToSpacePayload") or {}
        plain["type"] = "ADDED_TO_SPACE"
        plain["space"] = payload.get("space") or plain["space"]
        # Added by @mentioning the app: the message that added it comes with the event.
        if payload.get("message"):
            plain["message"] = payload["message"]
    elif "removedFromSpacePayload" in chat:
        plain["type"] = "REMOVED_FROM_SPACE"
    elif "buttonClickedPayload" in chat:
        payload = chat.get("buttonClickedPayload") or {}
        params = {str(k): str(v) for k, v in (common.get("parameters") or {}).items()}
        action = params.pop(ACTION_PARAM, None)
        plain["type"] = "CARD_CLICKED"
        plain["message"] = payload.get("message") or {}
        plain["common"] = {
            # Only an action this bot put on its own card is restored; anything else stays unknown.
            "invokedFunction": action if action in KNOWN_ACTIONS else common.get("invokedFunction"),
            "parameters": params,
            "formInputs": common.get("formInputs") or {},
        }
    elif "messagePayload" in chat or "appCommandPayload" in chat:
        payload = chat.get("messagePayload") or chat.get("appCommandPayload") or {}
        plain["type"] = "MESSAGE"
        plain["message"] = payload.get("message") or {}
        plain["space"] = payload.get("space") or plain["space"]
    else:
        plain["type"] = None
    return plain


def _point_actions_at_endpoint(node: Any, endpoint_url: str) -> None:
    if isinstance(node, dict):
        action = node.get("action")
        if isinstance(action, dict) and action.get("function") in KNOWN_ACTIONS:
            name = action["function"]
            params = [p for p in action.get("parameters") or [] if p.get("key") != ACTION_PARAM]
            action["function"] = endpoint_url
            action["parameters"] = params + [{"key": ACTION_PARAM, "value": name}]
        for value in node.values():
            _point_actions_at_endpoint(value, endpoint_url)
    elif isinstance(node, list):
        for item in node:
            _point_actions_at_endpoint(item, endpoint_url)


def to_addon_response(body: dict[str, Any], endpoint_url: str, event_type: str | None) -> dict[str, Any]:
    """Wrap a plain Chat message body ({"text": ...} or {"cardsV2": [...]}) as an add-on reply."""
    if event_type == "REMOVED_FROM_SPACE" or not body:
        return {}
    message = copy.deepcopy(body)
    _point_actions_at_endpoint(message, endpoint_url)
    return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": message}}}}
