"""Google Chat REST client.

Phase 1 operations:
- find_direct_message: resolve the DM space between the bot and a user (GET)
- list_messages: read messages from an approved space since a timestamp (GET)
- post_message: deliver a summary (POST, the only write in Phase 1)

Auth uses a service account. The OAuth scopes are passed in explicitly because the
read scope (chat.messages.readonly vs chat.app.messages.readonly) is not confirmed yet.
See the feasibility spike in the project notes.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from daily_task_bot.domain.models import ChatMessage

CHAT_BASE_URL = "https://chat.googleapis.com/v1"
BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"


def chat_filter_timestamp(since: datetime) -> str:
    """Format a timezone-aware datetime as the UTC RFC 3339 text the Chat filter expects.

    Naive datetimes are rejected: guessing their timezone is how the window shifts silently.
    """
    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError("since must be timezone-aware")
    return since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GChatClient:
    def __init__(
        self,
        service_account_file: Path,
        scopes: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._credentials = service_account.Credentials.from_service_account_file(
            str(service_account_file), scopes=scopes or [BOT_SCOPE]
        )
        self._http = httpx.AsyncClient(base_url=CHAT_BASE_URL, timeout=30.0, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _auth_headers(self) -> dict[str, str]:
        if not self._credentials.valid:
            await asyncio.to_thread(self._credentials.refresh, Request())
        return {"Authorization": f"Bearer {self._credentials.token}"}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._http.get(path, params=params, headers=await self._auth_headers())
        response.raise_for_status()
        return response.json()

    async def find_direct_message(self, chat_user_id: str) -> str:
        """Return the DM space name for a user, e.g. "spaces/AAA". Raises 404 if none exists."""
        data = await self._get("/spaces:findDirectMessage", params={"name": chat_user_id})
        return data["name"]

    async def list_messages(self, space: str, since: datetime) -> list[ChatMessage]:
        results: list[ChatMessage] = []
        page_token: str | None = None
        since_text = chat_filter_timestamp(since)
        while True:
            params: dict[str, Any] = {
                "filter": f'createTime > "{since_text}"',
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            data = await self._get(f"/{space}/messages", params=params)
            for m in data.get("messages", []):
                results.append(
                    ChatMessage(
                        space=space,
                        sender_id=m.get("sender", {}).get("name"),
                        created_at=datetime.fromisoformat(m["createTime"].replace("Z", "+00:00")),
                        text=m.get("text", ""),
                    )
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                return results

    async def post_card(self, space: str, body: dict[str, Any]) -> dict[str, Any]:
        """Post a cardsV2 (or other) message body into a space or direct message."""
        response = await self._http.post(
            f"/{space}/messages",
            json=body,
            headers=await self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def post_message(self, space: str, text: str) -> dict[str, Any]:
        response = await self._http.post(
            f"/{space}/messages",
            json={"text": text},
            headers=await self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()
