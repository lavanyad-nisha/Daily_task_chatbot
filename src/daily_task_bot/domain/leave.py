"""Leave rules. Given an employee's leave windows, decide their state on a run date.

Rules (Phase 1):
- on_leave: the run date falls inside a leave window.
- returning_today: the run date is the first working day after a leave window ends.
  The first working day is the next date that is not a weekend or holiday.
- Leave windows are inclusive. Half-day leave still counts as on leave for the day.
"""

from datetime import date, timedelta

from daily_task_bot.domain.models import LeaveState, LeaveWindow


def is_working_day(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def next_working_day(day: date, holidays: set[date]) -> date:
    candidate = day + timedelta(days=1)
    while not is_working_day(candidate, holidays):
        candidate += timedelta(days=1)
    return candidate


def leave_state(
    run_date: date, windows: list[LeaveWindow], holidays: set[date] | None = None
) -> LeaveState:
    holidays = holidays or set()
    on_leave_window = next((w for w in windows if w.covers(run_date)), None)
    if on_leave_window is not None:
        return LeaveState(on_leave=True, leave_end=on_leave_window.end)

    for window in windows:
        if next_working_day(window.end, holidays) == run_date:
            return LeaveState(returning_today=True, leave_end=window.end)

    return LeaveState()
