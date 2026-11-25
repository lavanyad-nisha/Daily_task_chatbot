"""Which OpenProject tasks an employee can see and update, and which are due for a reminder.

Rules (see docs/BUSINESS_RULES.md, "Task visibility"):
- Completed tasks are never listed or updated. "Closed" and "Rejected" are closed in OpenProject.
  The statuses in COMPLETED_STATUS_IDS are also completed for this bot (QA-finished work).
- Eligible for update: assigned to the employee and not completed, on any date. Start and due
  dates do not limit updates.
- Due for a reminder: eligible and due on that day (due date equals the day, Asia/Kolkata).
  Reminders do not list tasks due on other days.

Pure functions; no I/O.
"""

from datetime import date

from daily_task_bot.domain.models import WorkPackage

# OpenProject status IDs treated as completed by this bot: Tested (10), Resolved (16), Passed (17).
# These are IDs from the live instance (GET /api/v3/statuses). Closed and Rejected are already
# closed in OpenProject, so they need no entry here.
COMPLETED_STATUS_IDS: frozenset[int] = frozenset({10, 16, 17})


def is_completed(wp: WorkPackage) -> bool:
    return wp.status_is_closed or (wp.status_id is not None and wp.status_id in COMPLETED_STATUS_IDS)


def is_eligible(wp: WorkPackage) -> bool:
    """Open and not completed. The caller checks the assignee."""
    return not is_completed(wp)


def is_due_on(wp: WorkPackage, day: date) -> bool:
    """Eligible and due on this day. Used for reminders only."""
    return is_eligible(wp) and wp.due_date == day
