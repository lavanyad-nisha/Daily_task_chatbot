"""Task window rule: a task is listed on a day only when its start date is that day or earlier.

Covers the rules in docs/BUSINESS_RULES.md ("Task visibility"). Offline; no database or network.
"""

from datetime import date

from daily_task_bot.config import Settings
from daily_task_bot.domain.models import Employee, WorkPackage
from daily_task_bot.domain.task_window import (
    COMPLETED_STATUS_IDS, NO_DATES, OVERDUE, TaskRules, is_scheduled_on, task_flag,
)
from daily_task_bot.services.chat_events import _validate_tasks, open_tasks_for

TODAY = date(2026, 11, 24)


def wp(id_, *, start=date(2026, 11, 20), due=date(2026, 11, 30), closed=False, assignee=5, status_id=7):
    return WorkPackage(id=id_, subject=f"Task {id_}", status_name="In progress", status_is_closed=closed,
                       status_id=status_id, assignee_id=assignee, start_date=start, due_date=due)


def test_task_starting_today_is_listed():
    assert is_scheduled_on(wp(1, start=TODAY, due=date(2026, 12, 1)), TODAY)


def test_task_starting_in_the_past_and_still_running_is_listed():
    assert is_scheduled_on(wp(1, start=date(2026, 11, 20), due=date(2026, 11, 30)), TODAY)


def test_task_starting_later_is_not_listed():
    assert not is_scheduled_on(wp(1, start=date(2026, 11, 25), due=date(2026, 12, 1)), TODAY)


def test_task_due_today_is_listed():
    assert is_scheduled_on(wp(1, start=date(2026, 11, 20), due=TODAY), TODAY)


STRICT = TaskRules(show_overdue=False, show_undated=False)


def test_overdue_open_task_is_listed_and_flagged_by_default():
    late = wp(1, start=date(2026, 7, 29), due=date(2026, 8, 10))
    assert is_scheduled_on(late, TODAY)
    assert task_flag(late, TODAY) == OVERDUE


def test_overdue_task_is_hidden_when_show_overdue_is_off():
    assert not is_scheduled_on(wp(1, start=date(2026, 7, 29), due=date(2026, 8, 10)), TODAY, STRICT)


def test_closed_task_is_never_listed():
    assert not is_scheduled_on(wp(1, closed=True), TODAY)


def test_tested_passed_and_resolved_are_completed_and_not_listed():
    assert COMPLETED_STATUS_IDS == frozenset({10, 16, 17})
    for status_id in (10, 16, 17):
        assert not is_scheduled_on(wp(1, status_id=status_id), TODAY)


def test_test_failed_and_on_hold_stay_listed():
    assert is_scheduled_on(wp(1, status_id=11), TODAY)  # Test failed
    assert is_scheduled_on(wp(2, status_id=13), TODAY)  # On hold


def test_task_without_dates_is_listed_and_flagged_by_default():
    for task in (wp(1, start=None, due=date(2026, 11, 30)), wp(1, start=date(2026, 11, 20), due=None),
                 wp(1, start=None, due=None)):
        assert is_scheduled_on(task, TODAY)
        assert task_flag(task, TODAY) == NO_DATES


def test_task_without_dates_is_hidden_when_show_undated_is_off():
    assert not is_scheduled_on(wp(1, start=None, due=date(2026, 11, 30)), TODAY, STRICT)
    assert not is_scheduled_on(wp(1, start=date(2026, 11, 20), due=None), TODAY, STRICT)
    assert not is_scheduled_on(wp(1, start=None, due=None), TODAY, STRICT)


def test_future_start_is_hidden_even_without_a_due_date():
    assert not is_scheduled_on(wp(1, start=date(2026, 12, 1), due=None), TODAY)


def test_completed_statuses_are_configurable():
    assert is_scheduled_on(wp(1, status_id=10), TODAY, TaskRules(completed_status_ids=frozenset({16})))
    assert not is_scheduled_on(wp(1, status_id=99), TODAY, TaskRules(completed_status_ids=frozenset({99})))


def test_rules_come_from_settings():
    rules = TaskRules.from_settings(Settings(_env_file=None, completed_status_ids=[5],
                                             show_overdue_tasks=False, show_undated_tasks=False))
    assert rules == TaskRules(completed_status_ids=frozenset({5}), show_overdue=False, show_undated=False)


class FakeOpenProject:
    """Returns the given work packages; get_work_package returns the same objects as details."""

    def __init__(self, packages):
        self.packages = packages

    async def get_statuses(self):
        return {}

    async def get_open_work_packages(self, statuses, project_ids=None):
        return [p for p in self.packages if not p.status_is_closed]

    async def get_work_package(self, wp_id, statuses=None):
        for p in self.packages:
            if p.id == wp_id:
                return p
        return None


def employee():
    return Employee(name="Asha", email="asha@example.com", openproject_user_id=5, chat_user_id="users/1")


async def test_card_lists_only_tasks_scheduled_for_today_and_assigned():
    packages = [
        wp(1, start=date(2026, 11, 20), due=date(2026, 11, 30)),   # running: listed
        wp(2, start=date(2026, 12, 1), due=date(2026, 12, 9)),     # future: hidden
        wp(3, start=date(2026, 11, 20), due=date(2026, 11, 30), closed=True),  # closed: hidden
        wp(4, start=None, due=None),                               # no dates: listed, flagged
        wp(5, start=date(2026, 11, 20), due=date(2026, 11, 30), assignee=6),   # someone else: hidden
        wp(6, start=date(2026, 8, 1), due=date(2026, 8, 10)),      # overdue: listed, flagged
    ]
    tasks = await open_tasks_for(FakeOpenProject(packages), employee(), Settings(_env_file=None), TODAY)
    assert [(t.work_package_id, t.status_name) for t in tasks] == [
        (1, "In progress"), (4, "In progress, NO DATES"), (6, "In progress, OVERDUE")]


async def test_card_with_strict_rules_lists_only_tasks_in_their_window():
    packages = [wp(1), wp(4, start=None, due=None), wp(6, start=date(2026, 8, 1), due=date(2026, 8, 10))]
    settings = Settings(_env_file=None, show_overdue_tasks=False, show_undated_tasks=False)
    tasks = await open_tasks_for(FakeOpenProject(packages), employee(), settings, TODAY)
    assert [t.work_package_id for t in tasks] == [1]


async def test_next_task_appears_once_the_current_one_is_closed():
    t1 = wp(1, start=date(2026, 11, 20), due=date(2026, 11, 30))
    t2 = wp(2, start=date(2026, 11, 25), due=date(2026, 12, 5))
    before = await open_tasks_for(FakeOpenProject([t1, t2]), employee(), Settings(_env_file=None), TODAY)
    assert [t.work_package_id for t in before] == [1]

    t1_closed = t1.model_copy(update={"status_is_closed": True})
    after = await open_tasks_for(FakeOpenProject([t1_closed, t2]), employee(), Settings(_env_file=None), date(2026, 11, 25))
    assert [t.work_package_id for t in after] == [2]


async def test_submission_refuses_a_task_not_scheduled_for_today():
    packages = [wp(1, start=date(2026, 12, 1), due=date(2026, 12, 9))]
    problems = await _validate_tasks(FakeOpenProject(packages), employee(), [1], TODAY)
    assert problems == ["#1 is not scheduled for today"]


async def test_submission_accepts_a_task_scheduled_for_today():
    packages = [wp(1, start=date(2026, 11, 20), due=date(2026, 11, 30))]
    assert await _validate_tasks(FakeOpenProject(packages), employee(), [1], TODAY) == []
