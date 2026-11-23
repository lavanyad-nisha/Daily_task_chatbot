"""Pure mappings from a confirmed DSR entry to outbound payloads. No I/O.

OpenProject time entry (POST /api/v3/time_entries), as mapped here. Checked against the live
time-entry schema (read-only GET /api/v3/time_entries/schema):
- entity link (required): the assigned work package, as /api/v3/work_packages/{id}
- spentOn (required): the DSR date
- hours (required): ISO 8601 duration, rounded to whole minutes (for example 2.5 -> "PT2H30M")
- activity link (optional, has a default): sent only when explicitly configured
- comment: the employee's work description, plus an idempotency marker on its own line.
  Before creating an entry, the sync worker searches for this marker to avoid duplicates.

The Jibble mapping is intentionally absent. Its endpoint and fields are still unconfirmed.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

MAX_HOURS = Decimal("24")
MAX_DESCRIPTION_LENGTH = 2000
_MARKER_PATTERN = re.compile(r"\[(dtb:[A-Za-z0-9_.:\-]+)\]")


def hours_to_iso_duration(hours: Decimal) -> str:
    """Convert decimal hours to an ISO 8601 duration, rounded to the nearest minute."""
    if not isinstance(hours, Decimal):
        raise TypeError("hours must be a Decimal")
    if hours <= 0 or hours > MAX_HOURS:
        raise ValueError(f"hours must be above 0 and at most {MAX_HOURS}")
    total_minutes = int((hours * 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if total_minutes == 0:
        raise ValueError("hours round to zero minutes")
    return f"PT{total_minutes // 60}H{total_minutes % 60}M"


def openproject_time_idempotency_key(dsr_entry_id: int) -> str:
    """Stable per DSR entry. It does not change when the DSR is corrected."""
    return f"dtb:dsr-entry:{dsr_entry_id}:openproject_time"


def openproject_time_entry_payload(
    *,
    dsr_entry_id: int,
    work_package_id: int,
    spent_on: date,
    hours: Decimal,
    description: str,
    activity_id: int | None = None,
) -> dict[str, Any]:
    """Build the POST body for an OpenProject time entry. Pure; nothing is sent."""
    if work_package_id <= 0:
        raise ValueError("work_package_id must be positive")
    if activity_id is not None and activity_id <= 0:
        raise ValueError("activity_id must be positive when given")
    text = description.strip()
    if not text:
        raise ValueError("description is required")
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"description exceeds {MAX_DESCRIPTION_LENGTH} characters")

    key = openproject_time_idempotency_key(dsr_entry_id)
    links: dict[str, Any] = {"entity": {"href": f"/api/v3/work_packages/{work_package_id}"}}
    if activity_id is not None:
        links["activity"] = {"href": f"/api/v3/time_entries/activities/{activity_id}"}
    return {
        "comment": {"raw": f"{text}\n[{key}]"},
        "spentOn": spent_on.isoformat(),
        "hours": hours_to_iso_duration(hours),
        "_links": links,
    }


def idempotency_key_from_comment(comment: str) -> str | None:
    """Recover the idempotency key from an existing time entry comment, if present."""
    match = _MARKER_PATTERN.search(comment or "")
    return match.group(1) if match else None


def payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over a canonical JSON form. A retry with a different hash is an update."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- duplicate detection (pure) -----------------------------------------------

@dataclass(frozen=True)
class DetectionOutcome:
    """What the sync worker should do before writing a time entry.

    action:
      create        no matching entry exists, so POST one
      reuse         a matching entry exists, so store its ID and do not POST
      needs_review  the state is ambiguous; never POST, and queue for a person
    """

    action: Literal["create", "reuse", "needs_review"]
    time_entry_id: int | None = None
    reason: str = ""


def marker_in_comment(comment: str, key: str) -> bool:
    """Exact match on the bracketed marker. Key ...:1:... never matches ...:12:...."""
    return f"[{key}]" in (comment or "")


def decide_time_entry_action(
    *,
    key: str,
    candidates: list[tuple[int, str]],
    stored_time_entry_id: int | None = None,
) -> DetectionOutcome:
    """Decide create, reuse or needs_review from existing entries.

    candidates: (time_entry_id, comment) pairs from find_time_entry_candidates.
    stored_time_entry_id: the ID recorded by an earlier attempt, if any.
    """
    matches = [tid for tid, comment in candidates if marker_in_comment(comment, key)]
    candidate_ids = {tid for tid, _ in candidates}

    if len(matches) > 1:
        return DetectionOutcome("needs_review", reason=f"{len(matches)} entries carry this marker")

    if stored_time_entry_id is not None:
        if stored_time_entry_id not in candidate_ids:
            # An earlier attempt recorded an entry, but search can't see it. Never create again.
            return DetectionOutcome(
                "needs_review",
                time_entry_id=stored_time_entry_id,
                reason="stored entry not found by search; comment may have been edited",
            )
        if matches and matches[0] != stored_time_entry_id:
            return DetectionOutcome(
                "needs_review", reason="stored entry and marker entry differ"
            )
        return DetectionOutcome("reuse", time_entry_id=stored_time_entry_id, reason="stored entry")

    if matches:
        return DetectionOutcome("reuse", time_entry_id=matches[0], reason="marker found")
    return DetectionOutcome("create", reason="no entry carries this marker")


# ---- status change (OpenProject work package) ------------------------------------

STATUS_TARGET = "openproject_status"


def openproject_status_idempotency_key(dsr_entry_id: int) -> str:
    return f"dtb:dsr-entry:{dsr_entry_id}:{STATUS_TARGET}"


def status_change_payload(*, work_package_id: int, status_id: int) -> dict[str, Any]:
    """Canonical description of the change. Used for hashing; the PATCH body is built in the client."""
    if work_package_id <= 0 or status_id <= 0:
        raise ValueError("work_package_id and status_id must be positive")
    return {"workPackageId": work_package_id, "statusId": status_id}


# ---- Jibble hour entries (internal fingerprint only) -------------------------------

JIBBLE_TARGET = "jibble_time"


def jibble_hour_idempotency_key(dsr_entry_id: int) -> str:
    """Stable per DSR entry. Not sent to Jibble: the official create contract is not verified."""
    return f"dtb:dsr-entry:{dsr_entry_id}:{JIBBLE_TARGET}"


def jibble_fingerprint(entry, submission) -> dict[str, Any]:
    """What a Jibble record would represent. Used only to detect changes before a send."""
    return {
        "dsrEntryId": entry.id,
        "date": submission.dsr_date.isoformat(),
        "hours": str(entry.hours_spent),
        "description": entry.work_done,
        "workPackageId": entry.work_package_id,
    }


# ---- Jibble HourEntry client-generated id ----------------------------------------

import uuid as _uuid

# Fixed namespace for DTB-generated Jibble HourEntry IDs. Changing it would change every ID.
DTB_HOUR_ENTRY_NAMESPACE = _uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f")


def jibble_hour_entry_uuid(dsr_entry_id: int) -> str:
    """Stable client-generated UUID for a DSR entry's Jibble HourEntry.

    The same DSR entry always gets the same ID, so a retry can't create a second record with a
    different ID. HourEntry.id is a GUID in Jibble's metadata.
    """
    return str(_uuid.uuid5(DTB_HOUR_ENTRY_NAMESPACE, jibble_hour_idempotency_key(dsr_entry_id)))
