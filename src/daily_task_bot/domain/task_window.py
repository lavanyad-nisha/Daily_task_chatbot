"""Which OpenProject tasks an employee can update, and which are due for a reminder.

Rules (see docs/BUSINESS_RULES.md, "Task eligibility and reminders"):
- Completed tasks are never listed or updated: closed in OpenProject (Closed, Rejected), or a status
  in TaskRules.completed_status_ids (default Tested 10, Resolved 16, Passed 17).
- Eligible for update: open and not completed, on any date. Start and due dates do not limit updates,
  so overdue and undated tasks are eligible too.
- Due for a reminder: eligible and due on that day (due date equals the day, Asia/Kolkata).
- OVERDUE and NO DATES flags are informational (for display). They do not hide tasks.

Pure functions; no I/O.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

DEFAULT_COMPLETED_STATUS_IDS: frozenset[int] = frozenset({10, 16, 17})
# Kept for existing imports: the default completed statuses (live OpenProject IDs).
COMPLETED_STATUS_IDS = DEFAULT_COMPLETED_STATUS_IDS

OVERDUE = "OVERDUE"
NO_DATES = "NO DATES"


class _Task(Protocol):
    status_is_closed: bool
    status_id: int | None
    start_date: date | None
    due_date: date | None


@dataclass(frozen=True)
class TaskRules:
    completed_status_ids: frozenset[int] = DEFAULT_COMPLETED_STATUS_IDS

    @classmethod
    def from_settings(cls, settings: Any) -> "TaskRules":
        return cls(completed_status_ids=frozenset(settings.completed_status_ids))


DEFAULT_RULES = TaskRules()


def is_completed(wp: _Task, rules: TaskRules = DEFAULT_RULES) -> bool:
    return wp.status_is_closed or (wp.status_id is not None and wp.status_id in rules.completed_status_ids)


def is_eligible(wp: _Task, rules: TaskRules = DEFAULT_RULES) -> bool:
    """Open and not completed, on any date. The caller checks the assignee."""
    return not is_completed(wp, rules)


def is_due_on(wp: _Task, day: date, rules: TaskRules = DEFAULT_RULES) -> bool:
    """Eligible and due on this day. Used for reminders only."""
    return is_eligible(wp, rules) and wp.due_date == day


def task_flag(wp: _Task, day: date) -> str | None:
    """OVERDUE, NO DATES, or None. Informational only; it never hides a task."""
    if wp.start_date is None or wp.due_date is None:
        return NO_DATES
    if wp.due_date < day:
        return OVERDUE
    return None
