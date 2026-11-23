"""Jibble HourEntries batch payload and response parsing, checked against the captured request."""

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from daily_task_bot.clients.jibble import JibbleAuth, JibbleClient, JibbleError, JibbleWriteNotVerified
from daily_task_bot.domain.jibble_hour_entry import (
    HourEntryResponseError,
    build_hour_entry_batch,
    duration_for_jibble,
    parse_hour_entry_batch_response,
)

CAPTURE = json.loads(
    (Path(__file__).resolve().parent.parent / "docs" / "jibble_hourentry_capture_redacted.json")
    .read_text(encoding="utf-8")
)
BASE = "https://time-tracking.prod.jibble.io/v1"
TOKEN_URL = "https://identity.prod.jibble.io/connect/token"
PEOPLE = "https://workspace.prod.jibble.io/v1"
KWARGS = dict(entry_id="ENTRY-1", person_id="PERSON-1", day=date(2026, 11, 20), hours=Decimal("2"),
              project_id="PROJECT-1", activity_id="ACTIVITY-1", note="DTB integration test")


@pytest.mark.parametrize("hours, expected", [
    (Decimal("2"), "PT2H"), (Decimal("0.25"), "PT15M"), (Decimal("2.5"), "PT2H30M"), (Decimal("24"), "PT24H"),
])
def test_duration_uses_the_captured_shape(hours, expected):
    assert duration_for_jibble(hours) == expected


@pytest.mark.parametrize("hours", [Decimal("0"), Decimal("-1"), Decimal("24.5")])
def test_out_of_range_hours_are_refused(hours):
    with pytest.raises(ValueError):
        duration_for_jibble(hours)


def test_batch_envelope_matches_the_captured_request_structure():
    built = build_hour_entry_batch(base_url=BASE, **KWARGS)
    captured_item = CAPTURE["request"]["body"]["requests"][0]
    assert set(built.keys()) == {"requests"}
    item = built["requests"][0]
    assert set(item.keys()) == set(captured_item.keys())
    assert item["url"] == f"{BASE}/HourEntries"
    assert item["method"] == "POST" and item["id"] == "r1" and item["atomicityGroup"] == "ag1"
    assert item["headers"] == {"odata-version": "4.0"}


def test_body_has_only_the_verified_fields_and_no_claimed_device():
    body = build_hour_entry_batch(base_url=BASE, **KWARGS)["requests"][0]["body"]
    assert body == {"personId": "PERSON-1", "date": "2026-11-20", "duration": "PT2H",
                    "activityId": "ACTIVITY-1", "id": "ENTRY-1", "note": "DTB integration test",
                    "projectId": "PROJECT-1"}


def test_only_the_two_unclaimed_captured_fields_are_left_out():
    captured = set(CAPTURE["request"]["body"]["requests"][0]["body"].keys())
    built = set(build_hour_entry_batch(base_url=BASE, **KWARGS)["requests"][0]["body"].keys())
    assert captured - built == {"clientType", "platform"}


@pytest.mark.parametrize("override", [{"entry_id": ""}, {"person_id": ""}, {"note": "   "}])
def test_required_inputs_are_enforced(override):
    with pytest.raises(ValueError):
        build_hour_entry_batch(base_url=BASE, **{**KWARGS, **override})


def test_parses_the_captured_created_entry():
    remote = parse_hour_entry_batch_response(CAPTURE["response"]["body"])
    created = CAPTURE["response"]["body"]["responses"][0]["body"]
    assert remote.id == created["id"]
    assert remote.duration == "PT2H" and remote.status == "Active" and remote.is_locked is False
    assert remote.project_id == created["projectId"] and remote.activity_id == created["activityId"]


def test_non_201_inner_response_is_an_error_carrying_the_status_only():
    data = {"responses": [{"id": "r1", "status": 400, "body": {"message": "private detail"}}]}
    with pytest.raises(HourEntryResponseError) as info:
        parse_hour_entry_batch_response(data)
    assert info.value.status == 400 and "private detail" not in str(info.value)


def test_missing_inner_response_or_id_is_an_error():
    with pytest.raises(HourEntryResponseError):
        parse_hour_entry_batch_response({"responses": [{"id": "other", "status": 201, "body": {"id": "x"}}]})
    with pytest.raises(HourEntryResponseError):
        parse_hour_entry_batch_response({"responses": [{"id": "r1", "status": 201, "body": {}}]})


def _client(writes_enabled):
    auth = JibbleAuth(TOKEN_URL, "client-id", "client-secret")
    return JibbleClient(PEOPLE, auth, time_entries_base_url=BASE, writes_enabled=writes_enabled), auth


@respx.mock
def test_create_is_refused_when_writes_are_disabled_and_nothing_is_sent():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    batch = respx.post(f"{BASE}/$batch").mock(return_value=httpx.Response(200, json=CAPTURE["response"]["body"]))
    client, auth = _client(writes_enabled=False)

    async def go():
        with pytest.raises(JibbleWriteNotVerified):
            await client.create_hour_entry(**KWARGS)
        await client.aclose()
        await auth.aclose()
    asyncio.run(go())
    assert batch.call_count == 0


@respx.mock
def test_enabled_create_posts_exactly_the_built_batch_and_returns_the_entry():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    batch = respx.post(f"{BASE}/$batch").mock(return_value=httpx.Response(200, json=CAPTURE["response"]["body"]))
    client, auth = _client(writes_enabled=True)

    async def go():
        remote = await client.create_hour_entry(**KWARGS)
        await client.aclose()
        await auth.aclose()
        return remote
    remote = asyncio.run(go())
    assert batch.call_count == 1
    assert json.loads(batch.calls[0].request.content) == build_hour_entry_batch(base_url=BASE, **KWARGS)
    assert remote.id == CAPTURE["response"]["body"]["responses"][0]["body"]["id"]


@respx.mock
def test_outer_and_inner_failures_raise_status_only_errors():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    client, auth = _client(writes_enabled=True)

    respx.post(f"{BASE}/$batch").mock(return_value=httpx.Response(503, json={"x": "y"}))

    async def outer():
        with pytest.raises(JibbleError) as info:
            await client.create_hour_entry(**KWARGS)
        return info.value.status
    assert asyncio.run(outer()) == 503

    respx.post(f"{BASE}/$batch").mock(return_value=httpx.Response(200, json={"responses": [
        {"id": "r1", "status": 400, "body": {"message": "secret detail"}}]}))

    async def inner():
        with pytest.raises(JibbleError) as info:
            await client.create_hour_entry(**KWARGS)
        await client.aclose()
        await auth.aclose()
        return info.value
    err = asyncio.run(inner())
    assert err.status == 400 and "secret detail" not in str(err)
