"""Read-only OpenProject API v3 client.

Only GET requests are made. Authentication uses HTTP Basic with the username "apikey"
and the API key as the password, as documented by OpenProject.
"""

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from daily_task_bot.domain.models import WorkPackage

PAGE_SIZE = 100


@dataclass(frozen=True)
class Status:
    id: int
    name: str
    is_closed: bool


def _error_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    """Pull OpenProject's error identifier and message. Ignores anything else in the body."""
    try:
        body = response.json()
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    identifier = body.get("errorIdentifier")
    message = body.get("message")
    return (identifier if isinstance(identifier, str) else None,
            message if isinstance(message, str) else None)


def _id_from_href(href: str | None) -> int | None:
    if not href:
        return None
    try:
        return int(href.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return None


@dataclass(frozen=True)
class TimeEntrySnapshot:
    """A read-only view of an existing OpenProject time entry."""

    id: int
    comment: str
    hours: str | None
    spent_on: str | None
    work_package_id: int | None
    user_id: int | None


class OpenProjectError(Exception):
    """Base class for OpenProject client errors."""


class WritesDisabledError(OpenProjectError):
    """Raised when a write is attempted while writes are disabled. No request is sent."""


class OpenProjectHttpError(OpenProjectError):
    """An HTTP error response. Carries the status, never the request body or credentials."""

    def __init__(self, status: int, identifier: str | None, message: str | None):
        self.status = status
        self.identifier = identifier
        super().__init__(f"OpenProject returned {status} {identifier or ''}: {message or ''}".strip())


class OpenProjectTransportError(OpenProjectError):
    """A network failure or timeout. The outcome of a write may be unknown."""


@dataclass(frozen=True)
class WorkPackageDetail:
    id: int
    assignee_id: int | None
    status_is_closed: bool
    status_id: int | None = None
    lock_version: int | None = None
    # True only when OpenProject lists an update link for the API user on this work package.
    # Absent or unknown = False, so the bot does not attempt the change.
    can_update: bool = False


class OpenProjectClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        writes_enabled: bool = False,
    ):
        # Writes are off unless explicitly enabled. This gate sits in the client, so no
        # caller can write by accident, even if the API-level gate is bypassed.
        self._writes_enabled = writes_enabled
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=httpx.BasicAuth("apikey", api_key),
            headers={"Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OpenProjectClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._send("GET", path, params=params)
        return response.json()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise OpenProjectTransportError(f"{method} {path} failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            identifier, message = _error_fields(response)
            raise OpenProjectHttpError(response.status_code, identifier, message)
        return response

    async def create_time_entry(self, payload: dict[str, Any]) -> int:
        """POST /api/v3/time_entries. Returns the new time entry ID.

        Refuses to send anything unless the client was created with writes_enabled=True.
        """
        if not self._writes_enabled:
            raise WritesDisabledError("writes are disabled for this client; no request was sent")
        response = await self._send("POST", "/api/v3/time_entries", json=payload)
        body = response.json()
        time_entry_id = body.get("id")
        if not isinstance(time_entry_id, int):
            raise OpenProjectHttpError(response.status_code, "MissingId", "response had no time entry id")
        return time_entry_id

    async def get_work_package(
        self, work_package_id: int, statuses: dict[int, Status] | None = None
    ) -> WorkPackageDetail | None:
        """GET one work package. Returns None when it does not exist or is not visible.

        The closed flag comes from the embedded status when present, otherwise from the
        statuses map. Without either, closed is unknown and the caller must not assume open.
        """
        try:
            data = await self._get(f"/api/v3/work_packages/{work_package_id}")
        except OpenProjectHttpError as exc:
            if exc.status == 404:
                return None
            raise
        links = data.get("_links", {})
        embedded_status = data.get("_embedded", {}).get("status")
        if embedded_status is not None and "isClosed" in embedded_status:
            closed = bool(embedded_status["isClosed"])
        else:
            status_id = _id_from_href(links.get("status", {}).get("href"))
            known = (statuses or {}).get(status_id) if status_id is not None else None
            if known is None:
                raise OpenProjectError(f"work package {work_package_id}: closed state unknown")
            closed = known.is_closed
        return WorkPackageDetail(
            id=data["id"],
            assignee_id=_id_from_href(links.get("assignee", {}).get("href")),
            status_is_closed=closed,
            status_id=_id_from_href(links.get("status", {}).get("href")),
            lock_version=data.get("lockVersion"),
            can_update="updateImmediately" in links,
        )

    async def get_allowed_status_ids(self, work_package_id: int, *, lock_version: int) -> set[int]:
        """Statuses OpenProject allows for this work package right now, from its update form.

        This is a validation-only POST to the work package's update form. OpenProject does not save
        anything from it. The body contains only the current lock version. Used before a PATCH,
        and to offer choices in the DSR form. Raises if the response has no status list.
        """
        data = await self._post_json(
            f"/api/v3/work_packages/{work_package_id}/form", {"lockVersion": lock_version}
        )
        status_prop = data.get("_embedded", {}).get("schema", {}).get("status", {})
        allowed = status_prop.get("_links", {}).get("allowedValues")
        if not isinstance(allowed, list):
            raise OpenProjectError(f"work package {work_package_id}: allowed statuses not returned")
        ids: set[int] = set()
        for item in allowed:
            status_id = _id_from_href(item.get("href") if isinstance(item, dict) else None)
            if status_id is not None:
                ids.add(status_id)
        return ids

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._send("POST", path, json=body)
        return response.json()

    async def update_work_package_status(
        self, work_package_id: int, *, lock_version: int, status_id: int
    ) -> None:
        """PATCH the status of one work package. The body carries only the lock version and status.

        OpenProject rejects the change (422) if its workflow does not allow it, and returns 409
        if the work package changed since it was read. Both are classified by the caller.
        """
        if not self._writes_enabled:
            raise WritesDisabledError("writes are disabled for this client; no request was sent")
        body = {
            "lockVersion": lock_version,
            "_links": {"status": {"href": f"/api/v3/statuses/{status_id}"}},
        }
        await self._send("PATCH", f"/api/v3/work_packages/{work_package_id}", json=body)

    async def get_statuses(self) -> dict[int, Status]:
        data = await self._get("/api/v3/statuses")
        elements = data.get("_embedded", {}).get("elements", [])
        return {
            s["id"]: Status(id=s["id"], name=s["name"], is_closed=bool(s.get("isClosed")))
            for s in elements
        }

    async def get_open_work_packages(
        self, statuses: dict[int, Status], project_ids: list[int] | None = None
    ) -> list[WorkPackage]:
        """Fetch all open work packages, optionally limited to some projects.

        Uses the "status_id" filter with operator "o" (open), so closed items are not
        downloaded at all. The status flag is still checked locally as a safety net.
        """
        filters: list[dict[str, Any]] = [{"status_id": {"operator": "o", "values": []}}]
        if project_ids:
            filters.append({"project": {"operator": "=", "values": [str(p) for p in project_ids]}})

        results: list[WorkPackage] = []
        page = 1
        while True:
            data = await self._get(
                "/api/v3/work_packages",
                params={
                    "filters": json.dumps(filters),
                    "pageSize": PAGE_SIZE,
                    "offset": page,  # OpenProject uses 1-based page numbers here
                },
            )
            elements = data.get("_embedded", {}).get("elements", [])
            results.extend(self._to_work_package(e, statuses) for e in elements)
            if not elements or page * PAGE_SIZE >= data.get("total", 0):
                break
            page += 1
        return [wp for wp in results if not wp.status_is_closed]

    @staticmethod
    def _to_work_package(element: dict[str, Any], statuses: dict[int, Status]) -> WorkPackage:
        links = element.get("_links", {})
        embedded = element.get("_embedded", {})
        status_id = _id_from_href(links.get("status", {}).get("href"))
        status = statuses.get(status_id) if status_id is not None else None
        embedded_status = embedded.get("status", {})

        status_name = (status.name if status else None) or embedded_status.get("name", "Unknown")
        is_closed = status.is_closed if status else bool(embedded_status.get("isClosed"))
        priority = embedded.get("priority") or {}
        project = embedded.get("project") or {}

        return WorkPackage(
            id=element["id"],
            subject=element.get("subject", ""),
            project_name=project.get("name"),
            status_name=status_name,
            status_is_closed=is_closed,
            priority_name=priority.get("name"),
            assignee_id=_id_from_href(links.get("assignee", {}).get("href")),
        )

    async def list_time_entries(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """GET /api/v3/time_entries with the given filters, following all pages. Read-only."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._get(
                "/api/v3/time_entries",
                params={
                    "filters": json.dumps(filters),
                    "pageSize": PAGE_SIZE,
                    "offset": page,
                },
            )
            elements = data.get("_embedded", {}).get("elements", [])
            results.extend(elements)
            if not elements or page * PAGE_SIZE >= data.get("total", 0):
                return results
            page += 1

    async def find_time_entry_candidates(
        self, *, work_package_id: int, user_id: int, spent_on: date
    ) -> list[TimeEntrySnapshot]:
        """Existing entries for one work package, one user and one day.

        Uses only filters the live instance accepts (verified read-only): entity_type,
        entity_id, user_id and spent_on. OpenProject has no comment filter, so the caller must
        check comments for the DSR marker.
        """
        day = spent_on.isoformat()
        filters = [
            {"entity_type": {"operator": "=", "values": ["WorkPackage"]}},
            {"entity_id": {"operator": "=", "values": [str(work_package_id)]}},
            {"user_id": {"operator": "=", "values": [str(user_id)]}},
            {"spent_on": {"operator": "<>d", "values": [day, day]}},
        ]
        elements = await self.list_time_entries(filters)
        return [_to_time_entry_snapshot(e) for e in elements]


def _to_time_entry_snapshot(element: dict[str, Any]) -> TimeEntrySnapshot:
    links = element.get("_links", {})
    comment = (element.get("comment") or {}).get("raw") or ""
    return TimeEntrySnapshot(
        id=element["id"],
        comment=comment,
        hours=element.get("hours"),
        spent_on=element.get("spentOn"),
        work_package_id=_id_from_href(links.get("entity", {}).get("href")),
        user_id=_id_from_href(links.get("user", {}).get("href")),
    )
