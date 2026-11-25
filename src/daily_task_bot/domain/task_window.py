"""Which open tasks are listed on a given day, from OpenProject start and due dates.

Rules (see docs/BUSINESS_RULES.md, "Task visibility"):
- Completed tasks are never listed: closed in OpenProject (Closed, Rejected), or a status in
  TaskRules.completed_status_ids (default Tested 10, Resolved 16, Passed 17).
- A task whose start date is after the day is not listed yet.
- Overdue tasks (due date passed) stay listed and are flagged OVERDUE, so hours can still be
  logged on late work. TaskRules.show_overdue=False hides them instead.
- Tasks without a start or due date stay listed and are flagged NO DATES, until management has
  dated them. TaskRules.show_undated=False hides them instead.

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
    show_overdue: bool = True
    show_undated: bool = True

    @classmethod
    def from_settings(cls, settings: Any) -> "TaskRules":
        return cls(
            completed_status_ids=frozenset(settings.completed_status_ids),
            show_overdue=settings.show_overdue_tasks,
            show_undated=settings.show_undated_tasks,
        )


DEFAULT_RULES = TaskRules()


def is_completed(wp: _Task, rules: TaskRules = DEFAULT_RULES) -> bool:
    return wp.status_is_closed or (wp.status_id is not None and wp.status_id in rules.completed_status_ids)


def task_flag(wp: _Task, day: date) -> str | None:
    """OVERDUE, NO DATES, or None for a task inside its date window."""
    if wp.start_date is None or wp.due_date is None:
        return NO_DATES
    if wp.due_date < day:
        return OVERDUE
    return None


def is_scheduled_on(wp: _Task, day: date, rules: TaskRules = DEFAULT_RULES) -> bool:
    if is_completed(wp, rules):
        return False
    if wp.start_date is not None and wp.start_date > day:
        return False  # not started yet
    flag = task_flag(wp, day)
    if flag == NO_DATES:
        return rules.show_undated
    if flag == OVERDUE:
        return rules.show_overdue
    return True
