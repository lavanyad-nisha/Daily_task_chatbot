"""Duplicate detection for OpenProject time entries. Offline: mocked HTTP, no writes."""

import json
from datetime import date

import httpx
import respx

import daily_task_bot.clients.openproject as openproject_module
from daily_task_bot.clients.openproject import OpenProjectClient
from daily_task_bot.domain.sync_mapping import decide_time_entry_action, marker_in_comment

BASE = "https://op.test"
KEY = "dtb:dsr-entry:7:openproject_time"


# ---- marker matching ----------------------------------------------------------

def test_marker_matches_exact_bracketed_key():
    assert marker_in_comment(f"Fixed login\n[{KEY}]", KEY)


def test_marker_does_not_match_a_longer_key_id():
    # Key 7 must not match entries 17 or 70. This is why the match is bracketed and exact.
    assert not marker_in_comment("[dtb:dsr-entry:17:openproject_time]", KEY)
    assert not marker_in_comment("[dtb:dsr-entry:70:openproject_time]", KEY)


def test_marker_absent_or_empty_comment():
    assert not marker_in_comment("", KEY)
    assert not marker_in_comment(None, KEY)
    assert not marker_in_comment("dtb:dsr-entry:7:openproject_time", KEY)  # brackets required


# ---- decision table -----------------------------------------------------------

def test_no_candidates_means_create():
    assert decide_time_entry_action(key=KEY, candidates=[]).action == "create"


def test_candidate_without_marker_means_create():
    out = decide_time_entry_action(key=KEY, candidates=[(41, "manual entry")])
    assert out.action == "create"


def test_marker_match_means_reuse_existing_id():
    out = decide_time_entry_action(key=KEY, candidates=[(41, "x"), (42, f"y [{KEY}]")])
    assert out.action == "reuse" and out.time_entry_id == 42


def test_two_marker_matches_means_needs_review_never_create():
    out = decide_time_entry_action(key=KEY, candidates=[(1, f"[{KEY}]"), (2, f"[{KEY}]")])
    assert out.action == "needs_review"


def test_stored_id_still_found_with_marker_means_reuse():
    out = decide_time_entry_action(key=KEY, candidates=[(9, f"[{KEY}]")], stored_time_entry_id=9)
    assert out.action == "reuse" and out.time_entry_id == 9


def test_stored_id_found_without_marker_is_reused_not_duplicated():
    out = decide_time_entry_action(
        key=KEY, candidates=[(9, "edited, marker removed")], stored_time_entry_id=9
    )
    assert out.action == "reuse" and out.time_entry_id == 9


def test_stored_id_missing_from_search_means_needs_review_never_create():
    # The earlier attempt recorded an entry, but search cannot see it. Creating again is unsafe.
    out = decide_time_entry_action(key=KEY, candidates=[], stored_time_entry_id=9)
    assert out.action == "needs_review" and out.time_entry_id == 9


def test_stored_id_and_different_marker_entry_means_needs_review():
    out = decide_time_entry_action(
        key=KEY, candidates=[(9, "x"), (10, f"[{KEY}]")], stored_time_entry_id=9
    )
    assert out.action == "needs_review"


# ---- client: request shape and parsing (mocked) --------------------------------

def _entry(id_, comment, wp=1809, user=42, spent="2026-07-28"):
    return {
        "id": id_,
        "spentOn": spent,
        "hours": "PT1H",
        "comment": {"raw": comment},
        "_links": {
            "entity": {"href": f"/api/v3/work_packages/{wp}"},
            "user": {"href": f"/api/v3/users/{user}"},
        },
    }


@respx.mock
async def test_candidate_lookup_sends_supported_filters_and_parses_entries():
    route = respx.get(f"{BASE}/api/v3/time_entries").mock(
        return_value=httpx.Response(200, json={
            "total": 1, "count": 1,
            "_embedded": {"elements": [_entry(41, "work done")]},
        })
    )
    async with OpenProjectClient(BASE, "k") as op:
        found = await op.find_time_entry_candidates(
            work_package_id=1809, user_id=42, spent_on=date(2026, 7, 28)
        )

    filters = json.loads(route.calls[0].request.url.params["filters"])
    by_name = {next(iter(f)): f[next(iter(f))] for f in filters}
    assert by_name["entity_type"] == {"operator": "=", "values": ["WorkPackage"]}
    assert by_name["entity_id"] == {"operator": "=", "values": ["1809"]}
    assert by_name["user_id"] == {"operator": "=", "values": ["42"]}
    assert by_name["spent_on"] == {"operator": "<>d", "values": ["2026-07-28", "2026-07-28"]}
    assert "comment" not in by_name  # OpenProject has no comment filter; checked client-side

    assert len(found) == 1
    assert (found[0].id, found[0].work_package_id, found[0].user_id) == (41, 1809, 42)
    assert found[0].comment == "work done"


@respx.mock
async def test_candidate_lookup_follows_all_pages(monkeypatch):
    monkeypatch.setattr(openproject_module, "PAGE_SIZE", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        elements = [_entry(1, ""), _entry(2, "")] if offset == 1 else [_entry(3, "")]
        return httpx.Response(200, json={"total": 3, "_embedded": {"elements": elements}})

    respx.get(f"{BASE}/api/v3/time_entries").mock(side_effect=handler)
    async with OpenProjectClient(BASE, "k") as op:
        found = await op.find_time_entry_candidates(
            work_package_id=1809, user_id=42, spent_on=date(2026, 7, 28)
        )
    assert [e.id for e in found] == [1, 2, 3]


@respx.mock
async def test_lookup_never_issues_a_write():
    respx.get(f"{BASE}/api/v3/time_entries").mock(
        return_value=httpx.Response(200, json={"total": 0, "_embedded": {"elements": []}})
    )
    post = respx.post(f"{BASE}/api/v3/time_entries").mock(return_value=httpx.Response(201, json={}))
    async with OpenProjectClient(BASE, "k") as op:
        await op.find_time_entry_candidates(work_package_id=1, user_id=2, spent_on=date(2026, 1, 1))
    assert post.call_count == 0

