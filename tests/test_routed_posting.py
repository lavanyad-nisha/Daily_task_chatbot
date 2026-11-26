"""Routed posting of DSR entries to project spaces (Phase D). Offline; fake Chat client."""

import asyncio
from datetime import date
from decimal import Decimal

from daily_task_bot.domain.routing import Route
from daily_task_bot.services.dsr_preview import PreviewLine
from daily_task_bot.services.routed_posting import plan_routed_posts, send_plans

DAY = date(2026, 11, 25)
ROUTES = [
    Route(openproject_project_id=10, role_name="QA", chat_space_id="spaces/GPLQA", space_label="C-GPL-QA"),
    Route(openproject_project_id=7, role_name="Project Manager", chat_space_id="spaces/TEST",
          space_label="Test-Bot"),
]
ROLES = {10: ["QA"], 7: ["Project Manager"], 11: ["Team Member"]}


def line(wp, project, hours="1", work="Work"):
    return PreviewLine(work_package_id=wp, project_id=project, hours=Decimal(hours), work_done=work)


class FakeChat:
    def __init__(self, fail_for=()):
        self.posts: list[tuple[str, str]] = []
        self.fail_for = set(fail_for)

    async def post_message(self, space, text):
        if space in self.fail_for:
            raise RuntimeError("boom")
        self.posts.append((space, text))
        return {"name": f"{space}/messages/1"}


def _plan(lines):
    return plan_routed_posts(employee_name="Lavanya D", dsr_date=DAY, lines=lines,
                             roles_by_project=ROLES, routes=ROUTES)


def test_one_message_per_space_listing_only_that_spaces_entries():
    plan = _plan([line(101, 10, "2", "Login UAT"), line(205, 7, "1.5", "Asset import")])
    assert [p.space_id for p in plan.posts] == ["spaces/GPLQA", "spaces/TEST"]
    gpl = plan.posts[0]
    assert "from Lavanya D" in gpl.text and "#101: 2 h, Login UAT" in gpl.text
    assert "#205" not in gpl.text
    assert gpl.work_package_ids == (101,)


def test_unroutable_entries_are_not_posted_but_returned_for_review():
    plan = _plan([line(101, 10), line(310, 11, "1", "Report check")])
    assert [p.space_id for p in plan.posts] == ["spaces/GPLQA"]
    assert [(l.work_package_id, reason) for l, reason in plan.flagged] == [
        (310, "no active route for this project and role")]


def test_dry_run_sends_nothing_even_when_writes_are_allowed():
    chat = FakeChat()
    outcomes = asyncio.run(send_plans(chat, _plan([line(101, 10)]).posts, allow_writes=True, dry_run=True))
    assert chat.posts == []
    assert outcomes[0]["action"] == "would_post"


def test_writes_off_sends_nothing_even_when_not_dry_run():
    chat = FakeChat()
    outcomes = asyncio.run(send_plans(chat, _plan([line(101, 10)]).posts, allow_writes=False, dry_run=False))
    assert chat.posts == []
    assert outcomes[0]["action"] == "would_post"


def test_both_gates_open_posts_each_space_once():
    chat = FakeChat()
    plans = _plan([line(101, 10), line(205, 7)]).posts
    outcomes = asyncio.run(send_plans(chat, plans, allow_writes=True, dry_run=False))
    assert [s for s, _ in chat.posts] == ["spaces/GPLQA", "spaces/TEST"]
    assert all(o["action"] == "posted" for o in outcomes)


def test_one_failing_space_does_not_stop_the_others():
    chat = FakeChat(fail_for={"spaces/GPLQA"})
    plans = _plan([line(101, 10), line(205, 7)]).posts
    outcomes = asyncio.run(send_plans(chat, plans, allow_writes=True, dry_run=False))
    assert [o["action"] for o in outcomes] == ["failed", "posted"]
    assert outcomes[0]["error"] == "RuntimeError"
    assert [s for s, _ in chat.posts] == ["spaces/TEST"]
