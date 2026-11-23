"""Load employee mapping, leave windows and holidays from JSON config files.

These files stand in for the employee directory and calendar integration until those
access approvals are in place. Validation fails loudly so bad config never reaches a run.
"""

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from pydantic import TypeAdapter

from daily_task_bot.domain.models import Employee, LeaveWindow

_employees_adapter = TypeAdapter(list[Employee])


def load_employees(path: Path) -> list[Employee]:
    employees = _employees_adapter.validate_json(path.read_text(encoding="utf-8"))
    emails = [e.email.lower() for e in employees]
    duplicates = {e for e in emails if emails.count(e) > 1}
    if duplicates:
        raise ValueError(f"Duplicate employee emails in {path}: {sorted(duplicates)}")
    return employees


def load_leave(path: Path | None) -> dict[str, list[LeaveWindow]]:
    """Leave file format: [{"email": "...", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}]."""
    if path is None or not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    by_email: dict[str, list[LeaveWindow]] = defaultdict(list)
    for item in raw:
        window = LeaveWindow(
            start=date.fromisoformat(item["start"]),
            end=date.fromisoformat(item["end"]),
            half_day=bool(item.get("half_day", False)),
        )
        if window.end < window.start:
            raise ValueError(f"Leave end before start for {item['email']}")
        by_email[item["email"].lower()].append(window)
    return dict(by_email)


def load_holidays(path: Path | None) -> set[date]:
    """Holidays file format: ["YYYY-MM-DD", ...]."""
    if path is None or not path.exists():
        return set()
    return {date.fromisoformat(d) for d in json.loads(path.read_text(encoding="utf-8"))}
