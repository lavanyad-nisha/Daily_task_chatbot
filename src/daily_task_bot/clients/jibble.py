"""Jibble client: OAuth client-credentials token and read-only People access.

Security rules:
- The client secret and access token are never logged, printed or included in errors.
- Errors carry the HTTP status only, not response bodies, because bodies can echo sensitive data.
- Read-only: this module has GET calls only. Jibble writes are not implemented.
"""

from typing import Any

import httpx

from daily_task_bot.domain.jibble_hour_entry import (
    HourEntryResponseError,
    RemoteHourEntry,
    build_hour_entry_batch,
    parse_hour_entry_batch_response,
)


# Verified from the captured browser request and 201 response (docs/jibble_hourentry_capture_redacted.json,
# 2026-11-20). Writes are still controlled separately by writes_enabled and the external-write gate.
HOUR_ENTRY_CREATE_CONTRACT_VERIFIED = True


class JibbleWriteNotVerified(Exception):
    """A Jibble write was requested before its contract was verified. No request was sent."""


class JibbleError(Exception):
    """A Jibble request failed. Carries the HTTP status and the operation name only."""

    def __init__(self, operation: str, status: int | None = None):
        self.operation = operation
        self.status = status
        super().__init__(f"Jibble {operation} failed" + (f" with HTTP {status}" if status else ""))


class JibbleAuth:
    """Client-credentials token. Held in memory only, refreshed when missing."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_token(self) -> str:
        if self._token is None:
            response = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Accept": "application/json"},
            )
            if response.status_code != 200:
                raise JibbleError("token request", response.status_code)
            try:
                token = response.json().get("access_token")
            except ValueError as exc:
                raise JibbleError("token response") from exc
            if not isinstance(token, str) or not token:
                raise JibbleError("token response")
            self._token = token
        return self._token


class JibbleClient:
    """Jibble People (read-only) and HourEntries (verified read; create gated)."""

    def __init__(
        self,
        people_base_url: str,
        auth: JibbleAuth,
        transport: httpx.AsyncBaseTransport | None = None,
        time_entries_base_url: str | None = None,
        *,
        writes_enabled: bool = False,
    ):
        self._auth = auth
        self._writes_enabled = writes_enabled
        self._time_base = (time_entries_base_url or people_base_url).rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=people_base_url.rstrip("/"), timeout=30.0, transport=transport
        )
        self._time_http = httpx.AsyncClient(
            base_url=(time_entries_base_url or people_base_url).rstrip("/"),
            timeout=30.0, transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._time_http.aclose()

    async def _get(self, path: str, operation: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._auth.get_token()
        response = await self._http.get(
            path, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        if response.status_code != 200:
            raise JibbleError(operation, response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise JibbleError(operation) from exc

    async def list_hour_entries_for_day(self, *, person_id: str, day: str) -> list[dict[str, Any]]:
        """GET /HourEntries filtered by person and date. Verified read-only filter.

        Used before any create, to find existing entries for the same person and day.
        """
        token = await self._auth.get_token()
        response = await self._time_http.get(
            "/HourEntries",
            params={"$filter": f"personId eq {person_id} and date eq {day}", "$top": 200, "$count": "true"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise JibbleError("list hour entries", response.status_code)
        try:
            return response.json().get("value", [])
        except ValueError as exc:
            raise JibbleError("list hour entries") from exc

    async def create_hour_entry(
        self, *, entry_id: str, person_id: str, day, hours, project_id: str,
        activity_id: str, note: str,
    ) -> "RemoteHourEntry":
        """POST the verified $batch request that creates one HourEntry. Returns the created entry.

        Refused unless the contract is verified AND this client was created with writes_enabled.
        """
        if not HOUR_ENTRY_CREATE_CONTRACT_VERIFIED:
            raise JibbleWriteNotVerified("HourEntry create contract is not verified; no request was sent")
        if not self._writes_enabled:
            raise JibbleWriteNotVerified("writes are disabled for this client; no request was sent")
        body = build_hour_entry_batch(
            base_url=self._time_base, entry_id=entry_id, person_id=person_id, day=day,
            hours=hours, project_id=project_id, activity_id=activity_id, note=note,
        )
        token = await self._auth.get_token()
        response = await self._time_http.post(
            "/$batch", json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise JibbleError("create hour entry", response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise JibbleError("create hour entry response") from exc
        try:
            return parse_hour_entry_batch_response(data)
        except HourEntryResponseError as exc:
            raise JibbleError("create hour entry", exc.status) from exc

    async def list_people(self, *, top: int = 100, skip: int = 0) -> dict[str, Any]:
        """GET /People. Returns the raw collection. The caller must not log its contents."""
        return await self._get("/People", "list people", {"$top": top, "$skip": skip, "$count": "true"})
