"""Jibble HourEntries $batch payload builder and response parser. Pure: no I/O.

Verified from docs/jibble_hourentry_capture_redacted.json (captured 2026-11-20):
- Outer request: POST {time-tracking base}/$batch with a JSON body {"requests": [...]}.
- Inner request: id "r1", method POST, url {base}/HourEntries, headers odata-version 4.0,
  body with personId, date (YYYY-MM-DD), duration (ISO 8601, e.g. "PT2H"), activityId,
  projectId, note, and a client-generated id (GUID).
- Outer response: HTTP 200 with {"responses": [...]}; the inner response with id "r1" has
  status 201 and the created entry in its body, including id, personId, date, duration,
  projectId, activityId, note, isLocked and status.

Deliberately NOT sent: clientType. Jibble accepted the request without it in a live test.

platform IS sent, and it identifies this bot, not a browser. Jibble rejects the create with
"The Platform field is required." without it. The values describe this bot: a server-side
service on Linux, not a browser or a person's device.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

BATCH_REQUEST_ID = "r1"
BATCH_ATOMICITY_GROUP = "ag1"
ODATA_VERSION = "4.0"
BOT_CLIENT_VERSION = "0.1.0"  # matches pyproject.toml


def bot_platform() -> dict[str, Any]:
    """The platform object Jibble requires. Describes this bot; never a browser."""
    return {
        "deviceName": "Daily Task Bot",
        "deviceModel": None,
        "clientVersion": BOT_CLIENT_VERSION,
        "os": "Linux",
    }


class HourEntryResponseError(Exception):
    """The batch response was not the expected successful shape. Carries only the status."""

    def __init__(self, message: str, status: int | None = None):
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class RemoteHourEntry:
    id: str
    person_id: str | None
    date: str | None
    duration: str | None
    project_id: str | None
    activity_id: str | None
    note: str | None
    status: str | None
    is_locked: bool | None


def duration_for_jibble(hours: Decimal) -> str:
    """ISO 8601 duration in the shape the capture uses: PT2H, PT15M, PT2H30M.

    Whole minutes only. Zero-hour values are refused.
    """
    if not isinstance(hours, Decimal):
        raise TypeError("hours must be a Decimal")
    if hours <= 0 or hours > 24:
        raise ValueError("hours must be above 0 and at most 24")
    total = int((hours * 60).to_integral_value())
    if total <= 0:
        raise ValueError("hours round to zero minutes")
    h, m = divmod(total, 60)
    if h and m:
        return f"PT{h}H{m}M"
    if h:
        return f"PT{h}H"
    return f"PT{m}M"


def build_hour_entry_batch(
    *,
    base_url: str,
    entry_id: str,
    person_id: str,
    day: date,
    hours: Decimal,
    project_id: str,
    activity_id: str,
    note: str,
) -> dict[str, Any]:
    """The JSON body for POST {base}/$batch that creates one HourEntry."""
    for name, value in (("entry_id", entry_id), ("person_id", person_id),
                        ("project_id", project_id), ("activity_id", activity_id)):
        if not value or not isinstance(value, str):
            raise ValueError(f"{name} is required")
    text = note.strip()
    if not text:
        raise ValueError("note is required")
    base = base_url.rstrip("/")
    return {
        "requests": [
            {
                "url": f"{base}/HourEntries",
                "headers": {"odata-version": ODATA_VERSION},
                "id": BATCH_REQUEST_ID,
                "atomicityGroup": BATCH_ATOMICITY_GROUP,
                "body": {
                    "personId": person_id,
                    "date": day.isoformat(),
                    "duration": duration_for_jibble(hours),
                    "activityId": activity_id,
                    "id": entry_id,
                    "note": text,
                    "projectId": project_id,
                    "platform": bot_platform(),
                },
                "method": "POST",
            }
        ]
    }


def parse_hour_entry_batch_response(data: Any) -> RemoteHourEntry:
    """Read the created entry from the batch response. Raises if it isn't a clean 201."""
    if not isinstance(data, dict) or not isinstance(data.get("responses"), list):
        raise HourEntryResponseError("batch response has no responses list")
    inner = next((r for r in data["responses"] if isinstance(r, dict) and r.get("id") == BATCH_REQUEST_ID), None)
    if inner is None:
        raise HourEntryResponseError("batch response has no entry for the request")
    status = inner.get("status")
    if status != 201:
        raise HourEntryResponseError("inner HourEntries request was not created", status=status)
    body = inner.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"]:
        raise HourEntryResponseError("created entry has no id")
    return RemoteHourEntry(
        id=body["id"],
        person_id=body.get("personId"),
        date=body.get("date"),
        duration=body.get("duration"),
        project_id=body.get("projectId"),
        activity_id=body.get("activityId"),
        note=body.get("note"),
        status=body.get("status"),
        is_locked=body.get("isLocked"),
    )
