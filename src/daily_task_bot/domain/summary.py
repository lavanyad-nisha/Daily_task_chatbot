"""Turn an employee's work packages into a summary for one run date.

Only assignment and status are used. OpenProject due dates, start dates, estimates and
completion percentages are stale and are not part of the summary or the DSR form.

- Normal day: every open assigned task is listed with its OpenProject status.
- On leave: the same open tasks are listed under "needs cover", so a person can reassign them.
- Returning today: normal listing resumes, so the tasks reappear automatically.

Closed work packages are excluded.
"""

from datetime import date

from daily_task_bot.domain.models import (
    ChatTaskUpdate,
    Employee,
    EmployeeSummary,
    LeaveState,
    WorkPackage,
)


def _open_sorted(packages: list[WorkPackage]) -> list[WorkPackage]:
    open_wps = [wp for wp in packages if not wp.status_is_closed]
    return sorted(open_wps, key=lambda wp: (wp.status_name.lower(), wp.id))


def build_summary(
    employee: Employee,
    run_date: date,
    work_packages: list[WorkPackage],
    leave: LeaveState,
    chat_updates: list[ChatTaskUpdate] | None = None,
) -> EmployeeSummary:
    open_wps = _open_sorted(work_packages)
    summary = EmployeeSummary(
        employee=employee,
        run_date=run_date,
        leave=leave,
        chat_updates=chat_updates or [],
    )
    if leave.on_leave:
        summary.needs_cover = open_wps
    else:
        summary.assigned = open_wps
    return summary
