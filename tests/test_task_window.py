"""Task eligibility and reminder rules (Phase A). Offline; no database or network.

- Eligible for update: assigned to the employee and not completed, on any date.
- Due for a reminder: eligible and due_date equals today (Asia/Kolkata).
"""

from datetime import date

from daily_task_bot.config import Settings
from daily_task_bot.domain.models import Employee, WorkPackage
from daily_task_bot.domain.task_window import COMPLETED_STATUS_IDS, is_due_on, is_eligible
from daily_task_bot.services.chat_events import _validate_tasks, open_tasks_for

TODAY = date(2026, 11, 24)


def wp(id_, *, start=date(2026, 11, 20), due=date(2026, 11, 30), closed=False, assignee=5, status_id=7):
    return WorkPackage(id=id_, subject=f"Task {id_}", status_name="In progress", status_is_closed=closed,
                       status_id=status_id, assignee_id=assignee, start_date=start, due_date=due)


# ---- eligibility (updates, any date) ------------------------------------------------------

def test_open_task_is_eligible_whatever_its_dates():
    assert is_eligible(wp(1, start=date(2026, 7, 1), due=date(2026, 8, 1)))  # overdue
    assert is_eligible(wp(2, start=date(2026, 12, 1), due=date(2026, 12, 9)))  # future
    assert is_eligible(wp(3, start=None, due=None))  # no dates


def test_closed_task_is_not_eligible():
    assert not is_eligible(wp(1, closed=True))


def test_tested_passed_and_resolved_are_completed_and_not_eligible():
    assert COMPLETED_STATUS_IDS == frozenset({10, 16, 17})
    for status_id in (10, 16, 17):
        assert not is_eligible(wp(1, status_id=status_id))


def test_test_failed_and_on_hold_stay_eligible():
    assert is_eligible(wp(1, status_id=11))  # Test failed
    assert is_eligible(wp(2, status_id=13))  # On hold


# ---- reminders (due today only) -----------------------------------------------------------

def test_task_due_today_is_reminded():
    assert is_due_on(wp(1, start=date(2026, 11, 20), due=TODAY), TODAY)


def test_task_due_on_another_day_is_not_reminded():
    assert not is_due_on(wp(1, due=date(2026, 11, 30)), TODAY)  # later
    assert not is_due_on(wp(2, due=date(2026, 11, 23)), TODAY)  # overdue


def test_task_without_due_date_is_not_reminded():
    assert not is_due_on(wp(1, start=date(2026, 11, 20), due=None), TODAY)


def test_completed_task_due_today_is_not_reminded():
    assert not is_due_on(wp(1, due=TODAY, closed=True), TODAY)
    assert not is_due_on(wp(2, due=TODAY, status_id=10), TODAY)


def test_start_date_alone_does_not_trigger_a_reminder():
    assert not is_due_on(wp(1, start=TODAY, due=date(2026, 12, 1)), TODAY)


# ---- card and TASKS list ------------------------------------------------------------------

class FakeOpenProject:
    """Returns the given work packages; get_work_package returns the same objects as details."""

    def __init__(self, packages):
        self.packages = packages

    async def get_statuses(self):
        return {}

    async def get_open_work_packages(self, statuses, project_ids=None, assignee_id=None):
        return [p for p in self.packages if not p.status_is_closed]

    async def get_work_package(self, wp_id, statuses=None):
        for p in self.packages:
            if p.id == wp_id:
                return p
        return None


def employee():
    return Employee(name="Asha", email="asha@example.com", openproject_user_id=5, chat_user_id="users/1")


# TODAY is 24 Nov, so the 7-day window is 24-30 Nov.

async def test_list_has_overdue_and_due_within_7_days_only():
    packages = [
        wp(1, due=date(2026, 11, 30)),                       # last day of the window: listed
        wp(2, start=date(2026, 12, 1), due=date(2026, 12, 9)),  # due after the window: hidden
        wp(3, due=date(2026, 11, 26), closed=True),          # closed: hidden
        wp(4, start=None, due=None),                         # no dates: hidden
        wp(5, due=date(2026, 11, 26), assignee=6),           # someone else: hidden
        wp(6, due=date(2026, 11, 26), status_id=10),         # completed: hidden
        wp(7, due=TODAY),                                    # due today: listed
        wp(8, start=date(2026, 7, 1), due=date(2026, 8, 1)),  # overdue: listed
        wp(9, start=date(2026, 11, 1), due=None),            # start date only: hidden
        wp(10, due=date(2026, 12, 1)),                       # one day after the window: hidden
    ]
    tasks = await open_tasks_for(FakeOpenProject(packages), employee(), Settings(), TODAY)
    assert [(t.work_package_id, t.group) for t in tasks] == [(8, "OVERDUE"), (7, "DUE_SOON"), (1, "DUE_SOON")]


async def test_overdue_come_first_then_due_soon_each_by_due_date():
    packages = [wp(1, due=date(2026, 11, 29)), wp(2, due=date(2026, 11, 10)), wp(3, due=date(2026, 11, 25)),
                wp(4, due=date(2026, 11, 23)), wp(5, due=date(2026, 11, 25))]
    tasks = await open_tasks_for(FakeOpenProject(packages), employee(), Settings(), TODAY)
    assert [t.work_package_id for t in tasks] == [2, 4, 3, 5, 1]


async def test_window_length_comes_from_the_setting():
    packages = [wp(1, due=date(2026, 11, 26)), wp(2, due=date(2026, 11, 27))]
    settings = Settings(_env_file=None, task_window_days=3)  # 24, 25, 26 Nov
    tasks = await open_tasks_for(FakeOpenProject(packages), employee(), settings, TODAY)
    assert [t.work_package_id for t in tasks] == [1]


def test_tasks_reply_has_an_overdue_section_and_numbers_run_straight_through():
    from daily_task_bot.domain.chat_cards import CardTask
    from daily_task_bot.services.chat_events import tasks_text
    text = tasks_text([CardTask(8, "Old fix", "On hold", date(2026, 9, 21), "OVERDUE"),
                       CardTask(7, "Login UAT", "In progress", TODAY, "DUE_SOON"),
                       CardTask(1, "Export", "New", date(2026, 11, 30), "DUE_SOON")], TODAY, 7)
    assert text == ("Your tasks:\n\n"
                    "Overdue:\n1. #8 Old fix (On hold, due 21 Sep)\n\n"
                    "Due in the next 7 days (24 Nov - 30 Nov):\n"
                    "2. #7 Login UAT (In progress, due 24 Nov)\n3. #1 Export (New, due 30 Nov)")


def test_tasks_reply_without_overdue_has_no_overdue_heading():
    from daily_task_bot.domain.chat_cards import CardTask
    from daily_task_bot.services.chat_events import tasks_text
    text = tasks_text([CardTask(7, "Login UAT", "In progress", TODAY, "DUE_SOON")], TODAY, 7)
    assert "Overdue" not in text and "1. #7 Login UAT" in text


def test_empty_tasks_reply_explains_the_rule():
    from daily_task_bot.services.chat_events import tasks_text
    text = tasks_text([], TODAY, 7)
    assert "no overdue tasks and none due in the next 7 days" in text
    assert "no due date are not listed" in text


async def test_closing_a_task_removes_it_from_the_list():
    t1 = wp(1, start=date(2026, 11, 20), due=date(2026, 11, 27))
    t2 = wp(2, start=date(2026, 11, 25), due=date(2026, 11, 28))
    before = await open_tasks_for(FakeOpenProject([t1, t2]), employee(), Settings(), TODAY)
    assert [t.work_package_id for t in before] == [1, 2]

    t1_closed = t1.model_copy(update={"status_is_closed": True})
    after = await open_tasks_for(FakeOpenProject([t1_closed, t2]), employee(), Settings(), TODAY)
    assert [t.work_package_id for t in after] == [2]


# ---- submission check ---------------------------------------------------------------------

async def test_submission_accepts_a_future_dated_assigned_task():
    packages = [wp(1, start=date(2026, 12, 1), due=date(2026, 12, 9))]
    assert await _validate_tasks(FakeOpenProject(packages), employee(), [1], TODAY) == []


async def test_submission_accepts_an_overdue_assigned_task():
    packages = [wp(1, start=date(2026, 7, 1), due=date(2026, 8, 1))]
    assert await _validate_tasks(FakeOpenProject(packages), employee(), [1], TODAY) == []


async def test_submission_refuses_a_completed_task():
    packages = [wp(1, status_id=16)]
    problems = await _validate_tasks(FakeOpenProject(packages), employee(), [1], TODAY)
    assert problems == ["#1 is already completed"]


async def test_submission_refuses_a_closed_task():
    packages = [wp(1, closed=True)]
    problems = await _validate_tasks(FakeOpenProject(packages), employee(), [1], TODAY)
    assert problems == ["#1 is closed"]


async def test_submission_refuses_a_task_assigned_to_someone_else():
    packages = [wp(1, assignee=6)]
    problems = await _validate_tasks(FakeOpenProject(packages), employee(), [1], TODAY)
    assert problems == ["#1 is not assigned to you"]


# ---- missing dates note ---------------------------------------------------------------------

async def test_overview_names_open_tasks_missing_a_start_or_due_date():
    from daily_task_bot.services.chat_events import task_overview
    packages = [
        wp(1, due=TODAY),                                    # listed
        wp(2, start=None, due=None),                         # no dates: missing
        wp(3, start=date(2026, 11, 1), due=None),            # no due date: missing
        wp(4, start=None, due=None, status_id=10),           # completed: neither
        wp(5, start=None, due=None, assignee=6),             # someone else: neither
        wp(6, start=date(2026, 12, 1), due=date(2026, 12, 9)),  # dated, due later: neither
        wp(7, start=None, due=date(2026, 11, 26)),           # due soon, no start: listed only
    ]
    overview = await task_overview(FakeOpenProject(packages), employee(), Settings(), TODAY)
    assert [t.work_package_id for t in overview.listed] == [1, 7]
    assert [t.work_package_id for t in overview.missing_dates] == [2, 3]


def test_tasks_reply_ends_with_the_missing_dates_note():
    from daily_task_bot.domain.chat_cards import CardTask
    from daily_task_bot.services.chat_events import tasks_text
    text = tasks_text([CardTask(7, "Login UAT", "In progress", TODAY, "DUE_SOON")], TODAY, 7,
                      [CardTask(2, "Old story", "Test failed")])
    assert text.endswith("\n\nMissing dates: 1 of your open task has no start or due date in OpenProject, "
                         "so it is not listed. Please add both dates in OpenProject:\n"
                         "- #2 Old story (Test failed)")


def test_missing_dates_note_shows_ten_and_counts_the_rest():
    from daily_task_bot.domain.chat_cards import CardTask
    from daily_task_bot.services.chat_events import missing_dates_note
    note = missing_dates_note([CardTask(i, f"T{i}", "New") for i in range(1, 14)])
    assert note.startswith("Missing dates: 13 of your open tasks have")
    assert "- #10 T10 (New)" in note and "#11" not in note
    assert note.endswith("...and 3 more.")


def test_no_note_when_every_task_has_dates():
    from daily_task_bot.services.chat_events import missing_dates_note, tasks_text
    assert missing_dates_note([]) is None
    assert "Missing dates" not in tasks_text([], TODAY, 7, [])


def test_note_still_shows_when_nothing_is_listed():
    from daily_task_bot.domain.chat_cards import CardTask
    from daily_task_bot.services.chat_events import tasks_text
    text = tasks_text([], TODAY, 7, [CardTask(2, "Old story", "New")])
    assert text.startswith("You have no overdue tasks") and "- #2 Old story (New)" in text
