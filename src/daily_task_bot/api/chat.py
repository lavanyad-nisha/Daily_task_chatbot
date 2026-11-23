"""Google Chat HTTP endpoint. Verified by Google's token, not the internal run secret.

Google calls POST /chat with a JSON event and a bearer token. Configure the Chat app's HTTP
endpoint to the public URL that routes here (for example .../daily-task-bot/chat), and set
DTB_CHAT_ENDPOINT_AUDIENCE to that same URL.
"""

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import sessionmaker

from daily_task_bot.api.internal import get_openproject_port, get_session_factory
from daily_task_bot.clients.openproject import OpenProjectClient
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.services.chat_events import ChatAuthError, handle_chat_event, verify_chat_token

router = APIRouter()


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
        verify_chat_token(_bearer(authorization), settings.chat_endpoint_audience)
    except ChatAuthError as exc:
        raise HTTPException(status_code=401, detail="invalid Chat token") from exc
    event = await request.json()
    with factory() as session:
        try:
            body = await handle_chat_event(event, session=session, op_client=op_client, settings=settings)
            session.commit()
        except Exception:
            session.rollback()
            raise
    return body
