"""Which open tasks are scheduled for a given day, from OpenProject start and due dates.

Rules (see docs/BUSINESS_RULES.md, "Task visibility"):
- Closed tasks are never listed. "Closed" and "Rejected" are closed in OpenProject. The statuses
  in COMPLETED_STATUS_IDS are also completed for this bot (QA-finished work).
- A task without both a start date and a due date is not listed.
- A task is listed only on days from its start date to its due date, inclusive.
- Overdue tasks (due date passed) are not listed.

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


def is_scheduled_on(wp: WorkPackage, day: date) -> bool:
    if is_completed(wp) or wp.start_date is None or wp.due_date is None:
        return False
    return wp.start_date <= day <= wp.due_date
