import json
from datetime import date, datetime

import httpx
import respx

from daily_task_bot.clients.openproject import OpenProjectClient
from daily_task_bot.domain.models import ChatMessage, Employee, LeaveWindow, WorkPackage
from daily_task_bot.services.daily_run import run_daily_summary

BASE = "https://op.test"

STATUSES = {
    "_embedded": {
        "elements": [
            {"id": 1, "name": "New", "isClosed": False},
            {"id": 2, "name": "In progress", "isClosed": False},
            {"id": 3, "name": "Closed", "isClosed": True},
        ]
    }
}


def wp_element(id_, status_id, assignee_id, due=None):
    return {
        "id": id_,
        "subject": f"WP {id_}",
        "dueDate": due,
        "startDate": None,
        "percentageDone": 0,
        "_links": {
            "status": {"href": f"/api/v3/statuses/{status_id}"},
            "assignee": {"href": f"/api/v3/users/{assignee_id}"} if assignee_id else {"href": None},
        },
        "_embedded": {"project": {"name": "Demo"}},
    }


@respx.mock
async def test_openproject_uses_basic_auth_open_filter_and_paginates(monkeypatch):
    monkeypatch.setattr("daily_task_bot.clients.openproject.PAGE_SIZE", 2)
    respx.get(f"{BASE}/api/v3/statuses").mock(return_value=httpx.Response(200, json=STATUSES))
    page1 = {
        "total": 3,
        "_embedded": {"elements": [wp_element(10, 1, 5, "2026-11-01"), wp_element(11, 2, 5)]},
    }
    page2 = {"total": 3, "_embedded": {"elements": [wp_element(12, 3, 6)]}}  # closed: dropped
    route = respx.get(f"{BASE}/api/v3/work_packages").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )

    async with OpenProjectClient(BASE, "secret-key") as op:
        statuses = await op.get_statuses()
        wps = await op.get_open_work_packages(statuses, project_ids=[7])

    assert [w.id for w in wps] == [10, 11]
    assert wps[0].assignee_id == 5
    assert wps[0].status_is_closed is False

    first_request = route.calls[0].request
    assert first_request.headers["Authorization"].startswith("Basic ")
    filters = json.loads(first_request.url.params["filters"])
    assert filters[0] == {"status_id": {"operator": "o", "values": []}}
    assert filters[1]["project"]["values"] == ["7"]
    assert route.call_count == 2


class FakeOpenProject:
    def __init__(self, wps):
        self._wps = wps

    async def get_statuses(self):
        return {}

    async def get_open_work_packages(self, statuses, project_ids=None):
        return self._wps


class FakeChat:
    def __init__(self, fail_for=None):
        self.posts = []
        self.fail_for = fail_for

    async def find_direct_message(self, chat_user_id):
        return f"spaces/dm-{chat_user_id}"

    async def post_message(self, space, text):
        if self.fail_for and space.endswith(self.fail_for):
            raise RuntimeError("chat down")
        self.posts.append((space, text))
        return {}


EMPLOYEES = [
    Employee(name="Asha", email="asha@example.com", openproject_user_id=5, chat_user_id="users/1"),
    Employee(name="Ben", email="ben@example.com", openproject_user_id=6, chat_user_id="users/2"),
]
NOW = datetime(2026, 11, 18, 9, 0)


async def test_dry_run_renders_without_posting():
    wps = [WorkPackage(id=10, subject="A", status_name="New", status_is_closed=False, assignee_id=5, start_date=date(2026, 11, 18), due_date=date(2026, 11, 18))]
    chat = FakeChat()
    report = await run_daily_summary(
        run_date=NOW.date(), now=NOW, employees=EMPLOYEES, leave_by_email={}, holidays=set(),
        openproject=FakeOpenProject(wps), chat_writer=chat, dry_run=True,
    )
    assert report.counts() == {"dry_run": 2}
    assert chat.posts == []
    assert "#10 A" in report.results[0].text


async def test_live_run_posts_and_isolates_failures():
    wps = [
        WorkPackage(id=10, subject="A", status_name="New", status_is_closed=False, assignee_id=5, start_date=date(2026, 11, 18), due_date=date(2026, 11, 18)),
        WorkPackage(id=20, subject="B", status_name="New", status_is_closed=False, assignee_id=6, start_date=date(2026, 11, 18), due_date=date(2026, 11, 18)),
    ]
    chat = FakeChat(fail_for="dm-users/2")
    report = await run_daily_summary(
        run_date=NOW.date(), now=NOW, employees=EMPLOYEES, leave_by_email={}, holidays=set(),
        openproject=FakeOpenProject(wps), chat_writer=chat, dry_run=False,
    )
    assert report.counts() == {"sent": 1, "failed": 1}
    assert chat.posts[0][0] == "spaces/dm-users/1"


async def test_on_leave_employee_gets_cover_summary_and_leave_is_respected():
    wps = [WorkPackage(id=10, subject="A", status_name="New", status_is_closed=False, assignee_id=5, start_date=date(2026, 11, 18), due_date=date(2026, 11, 18))]
    leave = {"asha@example.com": [LeaveWindow(start=date(2026, 11, 18), end=date(2026, 11, 20))]}
    report = await run_daily_summary(
        run_date=NOW.date(), now=NOW, employees=EMPLOYEES[:1], leave_by_email=leave, holidays=set(),
        openproject=FakeOpenProject(wps), dry_run=True,
    )
    assert "on leave until 2026-11-20" in report.results[0].text
    assert "Needs cover" in report.results[0].text


async def test_chat_updates_only_count_from_the_employee_themselves():
    class FakeReader:
        async def list_messages(self, space, since):
            return [
                ChatMessage(space=space, sender_id="users/1", created_at=NOW, text="fixed #10"),
                ChatMessage(space=space, sender_id="users/2", created_at=NOW, text="also #10"),
            ]

    wps = [WorkPackage(id=10, subject="A", status_name="New", status_is_closed=False, assignee_id=5, start_date=date(2026, 11, 18), due_date=date(2026, 11, 18))]
    report = await run_daily_summary(
        run_date=NOW.date(), now=NOW, employees=EMPLOYEES[:1], leave_by_email={}, holidays=set(),
        openproject=FakeOpenProject(wps), chat_reader=FakeReader(), read_spaces=["spaces/A"], dry_run=True,
    )
    assert "Chat updates* (1)" in report.results[0].text
    assert "fixed #10" in report.results[0].text
    assert "also #10" not in report.results[0].text


def test_start_and_due_dates_are_parsed_but_progress_is_not():
    element = wp_element(9, 2, 5, due="2026-07-01")
    element["percentageDone"] = 80
    element["startDate"] = "2026-06-01"
    element["estimatedTime"] = "PT8H"
    wp = OpenProjectClient._to_work_package(element, {})
    assert wp.start_date == date(2026, 6, 1)
    assert wp.due_date == date(2026, 7, 1)
    assert not hasattr(wp, "percentage_done")
    assert wp.assignee_id == 5 and wp.id == 9


def test_missing_dates_are_none_not_an_error():
    element = wp_element(9, 2, 5)  # due=None and startDate=None
    wp = OpenProjectClient._to_work_package(element, {})
    assert wp.start_date is None and wp.due_date is None
    assert wp.assignee_id == 5 and wp.id == 9
