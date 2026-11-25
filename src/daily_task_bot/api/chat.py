"""Google Chat HTTP endpoint. Verified by Google's token, not the internal run secret.

Google calls POST /chat with a JSON event and a bearer token. Configure the Chat app's HTTP
endpoint to the public URL that routes here (for example .../daily-task-bot/chat), and set
DTB_CHAT_ENDPOINT_AUDIENCE to that same URL.

Both event formats are accepted:
- plain Chat app events, signed by chat@system.gserviceaccount.com
- Google Workspace add-on events (Chat app built as an add-on), signed by the project's add-on
  service account set in DTB_CHAT_ADDON_SERVICE_ACCOUNT
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import sessionmaker

from daily_task_bot.api.addon import is_addon_event, to_addon_response, to_chat_event
from daily_task_bot.api.internal import get_openproject_port, get_session_factory
from daily_task_bot.clients.openproject import OpenProjectClient
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.services.chat_events import ChatAuthError, handle_chat_event, verify_chat_token

router = APIRouter()
log = logging.getLogger("uvicorn.error")

FAILURE_TEXT = "Sorry, something went wrong on my side. Please try again in a few minutes."


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


@router.post("/chat")
async def chat_event(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    factory: sessionmaker = Depends(get_session_factory),
    op_client: OpenProjectClient = Depends(get_openproject_port),
) -> dict[str, Any]:
    if not settings.chat_endpoint_audience:
        raise HTTPException(status_code=503, detail="chat endpoint audience is not configured")
    try:
        verify_chat_token(_bearer(authorization), settings.chat_endpoint_audience,
                          settings.chat_addon_service_account)
    except ChatAuthError as exc:
        log.warning("chat token refused: %s", exc)
        raise HTTPException(status_code=401, detail="invalid Chat token") from exc

    raw = await request.json()
    addon = is_addon_event(raw)
    event = to_chat_event(raw) if addon else raw

    with factory() as session:
        try:
            body = await handle_chat_event(event, session=session, op_client=op_client, settings=settings)
            session.commit()
        except Exception:
            session.rollback()
            log.exception("chat event failed (type=%s)", event.get("type"))
            body = {"text": FAILURE_TEXT}

    if addon:
        return to_addon_response(body, settings.chat_endpoint_audience, event.get("type"))
    return body
