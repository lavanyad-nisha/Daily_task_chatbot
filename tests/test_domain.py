from datetime import date, datetime

from daily_task_bot.domain.leave import leave_state, next_working_day
from daily_task_bot.domain.matcher import extract_work_package_ids, match_messages
from daily_task_bot.domain.models import ChatMessage, Employee, LeaveWindow, WorkPackage
from daily_task_bot.domain.rendering import render_summary
from daily_task_bot.domain.summary import build_summary

EMP = Employee(name="Asha", email="asha@example.com", openproject_user_id=1, chat_user_id="users/9")


def wp(id_, **kw) -> WorkPackage:
    base = {"id": id_, "subject": f"Task {id_}", "status_name": "In progress", "status_is_closed": False}
    base.update(kw)
    return WorkPackage(**base)


# ---- matcher -------------------------------------------------------------

def test_extracts_ids_in_order_without_duplicates():
    assert extract_work_package_ids("Done #1234, follow-up #55 and #1234") == [1234, 55]


def test_ignores_non_reference_hashes():
    assert extract_work_package_ids("mail a&#123 or C# or #abc") == []


def test_match_only_known_ids():
    msg = ChatMessage(space="spaces/A", created_at=datetime(2026, 11, 18), text="working on #10 and #99")
    updates = match_messages([msg], known_ids={10})
    assert [u.work_package_id for u in updates] == [10]


# ---- leave ---------------------------------------------------------------

def test_on_leave_during_window():
    windows = [LeaveWindow(start=date(2026, 11, 23), end=date(2026, 11, 25))]
    state = leave_state(date(2026, 11, 24), windows)
    assert state.on_leave and not state.returning_today


def test_returns_on_next_working_day_after_leave_ends_friday():
    # Leave ends Friday 27 Nov 2026; next working day is Monday 30 Nov.
    windows = [LeaveWindow(start=date(2026, 11, 23), end=date(2026, 11, 27))]
    assert leave_state(date(2026, 11, 30), windows).returning_today
    assert not leave_state(date(2026, 11, 28), windows).returning_today


def test_return_skips_holidays():
    holidays = {date(2026, 12, 1)}
    assert next_working_day(date(2026, 11, 30), holidays) == date(2026, 12, 2)
    windows = [LeaveWindow(start=date(2026, 11, 25), end=date(2026, 11, 30))]
    assert leave_state(date(2026, 12, 2), windows, holidays).returning_today


def test_no_leave_no_flags():
    state = leave_state(date(2026, 11, 18), [])
    assert not state.on_leave and not state.returning_today


# ---- summary (assignment and status only) -------------------------------

RUN = date(2026, 11, 18)


def test_open_tasks_listed_with_status_and_closed_excluded():
    wps = [wp(1, status_name="New"), wp(2, status_name="Tested"), wp(3, status_is_closed=True)]
    s = build_summary(EMP, RUN, wps, leave=leave_state(RUN, []))
    assert [w.id for w in s.assigned] == [1, 2]
    assert s.needs_cover == []


def test_assigned_tasks_sorted_by_status_then_id():
    wps = [wp(5, status_name="Tested"), wp(4, status_name="New"), wp(6, status_name="New")]
    s = build_summary(EMP, RUN, wps, leave=leave_state(RUN, []))
    assert [w.id for w in s.assigned] == [4, 6, 5]


def test_on_leave_open_tasks_go_to_cover_not_assigned():
    windows = [LeaveWindow(start=date(2026, 11, 17), end=date(2026, 11, 20))]
    s = build_summary(EMP, RUN, [wp(1), wp(2)], leave=leave_state(RUN, windows))
    assert [w.id for w in s.needs_cover] == [1, 2]
    assert s.assigned == []


def test_render_on_leave_mentions_leave_and_cover():
    windows = [LeaveWindow(start=date(2026, 11, 17), end=date(2026, 11, 20))]
    s = build_summary(EMP, RUN, [wp(1)], leave=leave_state(RUN, windows))
    text = render_summary(s)
    assert "on leave until 2026-11-20" in text
    assert "Needs cover" in text


def test_render_shows_status_and_no_dates_or_progress():
    s = build_summary(EMP, RUN, [wp(7, status_name="Developed")], leave=leave_state(RUN, []))
    text = render_summary(s)
    assert "#7 Task 7 (Developed)" in text
    assert "due" not in text.lower()
    assert "%" not in text
    assert "Overdue" not in text
