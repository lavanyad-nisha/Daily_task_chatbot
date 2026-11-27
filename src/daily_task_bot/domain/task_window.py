"""Which OpenProject tasks an employee can update, and which are due for a reminder.

Rules (see docs/BUSINESS_RULES.md, "Task eligibility and reminders"):
- Completed tasks are never listed or updated: closed in OpenProject (Closed, Rejected), or a status
  in TaskRules.completed_status_ids (default Tested 10, Resolved 16, Passed 17).
- Eligible: open and not completed. The assignee is checked by the caller.
- Listed (TASKS, DSR card, UPDATE/STATUS numbers): eligible and has a due date that is either past
  (OVERDUE section) or within the next TaskRules.window_days days counting today (DUE_SOON section).
  Tasks with no due date, or due later, are not listed.
- Due for a reminder: eligible and due on that day (due date equals the day, Asia/Kolkata).

Pure functions; no I/O.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

DEFAULT_COMPLETED_STATUS_IDS: frozenset[int] = frozenset({10, 16, 17})
# Kept for existing imports: the default completed statuses (live OpenProject IDs).
COMPLETED_STATUS_IDS = DEFAULT_COMPLETED_STATUS_IDS

OVERDUE = "OVERDUE"
NO_DATES = "NO DATES"
DUE_SOON = "DUE_SOON"
DEFAULT_WINDOW_DAYS = 7


class _Task(Protocol):
    status_is_closed: bool
    status_id: int | None
    start_date: date | None
    due_date: date | None


@dataclass(frozen=True)
class TaskRules:
    completed_status_ids: frozenset[int] = DEFAULT_COMPLETED_STATUS_IDS
    window_days: int = DEFAULT_WINDOW_DAYS  # today plus the next (window_days - 1) days

    @classmethod
    def from_settings(cls, settings: Any) -> "TaskRules":
        return cls(completed_status_ids=frozenset(settings.completed_status_ids),
                   window_days=int(getattr(settings, "task_window_days", DEFAULT_WINDOW_DAYS)))


DEFAULT_RULES = TaskRules()


def is_completed(wp: _Task, rules: TaskRules = DEFAULT_RULES) -> bool:
    return wp.status_is_closed or (wp.status_id is not None and wp.status_id in rules.completed_status_ids)


def is_eligible(wp: _Task, rules: TaskRules = DEFAULT_RULES) -> bool:
    """Open and not completed, on any date. The caller checks the assignee."""
    return not is_completed(wp, rules)


def window_group(wp: _Task, day: date, rules: TaskRules = DEFAULT_RULES) -> str | None:
    """OVERDUE (due before the day), DUE_SOON (due within the window), or None (not listed).

    Only the due date counts. A task with no due date is not listed.
    """
    if not is_eligible(wp, rules) or wp.due_date is None:
        return None
    if wp.due_date < day:
        return OVERDUE
    if wp.due_date < day + timedelta(days=rules.window_days):
        return DUE_SOON
    return None


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
