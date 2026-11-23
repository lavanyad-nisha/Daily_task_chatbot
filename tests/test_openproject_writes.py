"""OpenProject write client: gate, request shape and error mapping. Mocked HTTP only."""

import httpx
import pytest
import respx

from daily_task_bot.clients.openproject import (
    OpenProjectClient,
    OpenProjectError,
    OpenProjectHttpError,
    OpenProjectTransportError,
    Status,
    WritesDisabledError,
)

BASE = "https://op.test"
SECRET = "super-secret-api-key-value"
PAYLOAD = {
    "comment": {"raw": "Fixed login\n[dtb:dsr-entry:5:openproject_time]"},
    "spentOn": "2026-11-19",
    "hours": "PT2H30M",
    "_links": {"entity": {"href": "/api/v3/work_packages/1809"}},
}


# ---- write gate ------------------------------------------------------------------

@respx.mock
async def test_writes_are_refused_by_default_and_no_request_is_sent():
    post = respx.post(f"{BASE}/api/v3/time_entries").mock(return_value=httpx.Response(201, json={"id": 1}))
    async with OpenProjectClient(BASE, SECRET) as op:
        with pytest.raises(WritesDisabledError):
            await op.create_time_entry(PAYLOAD)
    assert post.call_count == 0


@respx.mock
async def test_enabled_client_posts_the_payload_and_returns_the_id():
    post = respx.post(f"{BASE}/api/v3/time_entries").mock(
        return_value=httpx.Response(201, json={"id": 901, "_type": "TimeEntry"})
    )
    async with OpenProjectClient(BASE, SECRET, writes_enabled=True) as op:
        new_id = await op.create_time_entry(PAYLOAD)
    assert new_id == 901
    assert post.call_count == 1
    import json
    assert json.loads(post.calls[0].request.content) == PAYLOAD


# ---- error mapping ---------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
@respx.mock
async def test_http_errors_carry_status_and_identifier(status):
    respx.post(f"{BASE}/api/v3/time_entries").mock(
        return_value=httpx.Response(status, json={
            "_type": "Error", "errorIdentifier": "urn:openproject-org:api:v3:errors:Test",
            "message": "something went wrong",
        })
    )
    async with OpenProjectClient(BASE, SECRET, writes_enabled=True) as op:
        with pytest.raises(OpenProjectHttpError) as info:
            await op.create_time_entry(PAYLOAD)
    assert info.value.status == status
    assert info.value.identifier.endswith("Test")


@respx.mock
async def test_error_text_never_contains_the_api_key_or_the_request_body():
    respx.post(f"{BASE}/api/v3/time_entries").mock(
        return_value=httpx.Response(422, json={"message": "Hours can't be blank"})
    )
    async with OpenProjectClient(BASE, SECRET, writes_enabled=True) as op:
        with pytest.raises(OpenProjectHttpError) as info:
            await op.create_time_entry(PAYLOAD)
    text = str(info.value)
    assert SECRET not in text
    assert "Fixed login" not in text  # the description must not leak into error text


@respx.mock
async def test_timeout_is_a_transport_error_not_a_guess():
    respx.post(f"{BASE}/api/v3/time_entries").mock(side_effect=httpx.ReadTimeout("slow"))
    async with OpenProjectClient(BASE, SECRET, writes_enabled=True) as op:
        with pytest.raises(OpenProjectTransportError):
            await op.create_time_entry(PAYLOAD)


@respx.mock
async def test_success_without_an_id_is_an_error():
    respx.post(f"{BASE}/api/v3/time_entries").mock(return_value=httpx.Response(201, json={}))
    async with OpenProjectClient(BASE, SECRET, writes_enabled=True) as op:
        with pytest.raises(OpenProjectHttpError):
            await op.create_time_entry(PAYLOAD)


# ---- work package lookup -----------------------------------------------------------

@respx.mock
async def test_work_package_with_embedded_status_reports_assignee_and_closed_flag():
    respx.get(f"{BASE}/api/v3/work_packages/1809").mock(return_value=httpx.Response(200, json={
        "id": 1809,
        "_links": {"assignee": {"href": "/api/v3/users/42"}, "status": {"href": "/api/v3/statuses/3"}},
        "_embedded": {"status": {"id": 3, "isClosed": False}},
    }))
    async with OpenProjectClient(BASE, SECRET) as op:
        wp = await op.get_work_package(1809)
    assert (wp.id, wp.assignee_id, wp.status_is_closed) == (1809, 42, False)


@respx.mock
async def test_work_package_falls_back_to_statuses_map_for_closed_flag():
    respx.get(f"{BASE}/api/v3/work_packages/7").mock(return_value=httpx.Response(200, json={
        "id": 7,
        "_links": {"assignee": {"href": None}, "status": {"href": "/api/v3/statuses/9"}},
    }))
    statuses = {9: Status(id=9, name="Closed", is_closed=True)}
    async with OpenProjectClient(BASE, SECRET) as op:
        wp = await op.get_work_package(7, statuses)
    assert wp.status_is_closed is True and wp.assignee_id is None


@respx.mock
async def test_work_package_with_unknown_closed_state_is_an_error_not_open():
    respx.get(f"{BASE}/api/v3/work_packages/8").mock(return_value=httpx.Response(200, json={
        "id": 8, "_links": {"status": {"href": "/api/v3/statuses/99"}},
    }))
    async with OpenProjectClient(BASE, SECRET) as op:
        with pytest.raises(OpenProjectError, match="closed state unknown"):
            await op.get_work_package(8, {})


@respx.mock
async def test_missing_work_package_returns_none():
    respx.get(f"{BASE}/api/v3/work_packages/404").mock(return_value=httpx.Response(404, json={
        "_type": "Error", "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    }))
    async with OpenProjectClient(BASE, SECRET) as op:
        assert await op.get_work_package(404) is None


# ---- status update (PATCH) ----------------------------------------------------------

@respx.mock
async def test_status_update_is_refused_while_writes_are_disabled_and_nothing_is_sent():
    patch = respx.patch(f"{BASE}/api/v3/work_packages/2481").mock(return_value=httpx.Response(200, json={}))
    async with OpenProjectClient(BASE, SECRET) as op:
        with pytest.raises(WritesDisabledError):
            await op.update_work_package_status(2481, lock_version=8, status_id=7)
    assert patch.call_count == 0


@respx.mock
async def test_status_update_body_carries_only_lock_version_and_status():
    import json as _json
    patch = respx.patch(f"{BASE}/api/v3/work_packages/2481").mock(return_value=httpx.Response(200, json={}))
    async with OpenProjectClient(BASE, SECRET, writes_enabled=True) as op:
        await op.update_work_package_status(2481, lock_version=8, status_id=7)
    assert patch.call_count == 1
    body = _json.loads(patch.calls[0].request.content)
    assert body == {"lockVersion": 8, "_links": {"status": {"href": "/api/v3/statuses/7"}}}


@pytest.mark.parametrize("status, expected_kind", [(409, 409), (422, 422), (403, 403)])
@respx.mock
async def test_status_update_errors_keep_their_status_code(status, expected_kind):
    respx.patch(f"{BASE}/api/v3/work_packages/2481").mock(
        return_value=httpx.Response(status, json={"message": "x"}))
    async with OpenProjectClient(BASE, SECRET, writes_enabled=True) as op:
        with pytest.raises(OpenProjectHttpError) as info:
            await op.update_work_package_status(2481, lock_version=8, status_id=7)
    assert info.value.status == expected_kind


@respx.mock
async def test_work_package_lookup_returns_status_id_and_lock_version():
    respx.get(f"{BASE}/api/v3/work_packages/2481").mock(return_value=httpx.Response(200, json={
        "id": 2481, "lockVersion": 8,
        "_links": {"assignee": {"href": "/api/v3/users/42"}, "status": {"href": "/api/v3/statuses/10"}},
        "_embedded": {"status": {"id": 10, "isClosed": False}},
    }))
    async with OpenProjectClient(BASE, SECRET) as op:
        wp = await op.get_work_package(2481)
    assert (wp.status_id, wp.lock_version, wp.assignee_id) == (10, 8, 42)


@respx.mock
async def test_work_package_reports_update_permission_from_its_links():
    respx.get(f"{BASE}/api/v3/work_packages/2481").mock(return_value=httpx.Response(200, json={
        "id": 2481, "lockVersion": 9,
        "_links": {"status": {"href": "/api/v3/statuses/11"}, "updateImmediately": {"href": "/x"}},
    }))
    async with OpenProjectClient(BASE, SECRET) as op:
        assert (await op.get_work_package(2481, {11: Status(id=11, name="x", is_closed=False)})).can_update is True


@respx.mock
async def test_work_package_without_update_link_is_not_updatable():
    respx.get(f"{BASE}/api/v3/work_packages/2481").mock(return_value=httpx.Response(200, json={
        "id": 2481, "lockVersion": 9, "_links": {"status": {"href": "/api/v3/statuses/11"}},
    }))
    async with OpenProjectClient(BASE, SECRET) as op:
        assert (await op.get_work_package(2481, {11: Status(id=11, name="x", is_closed=False)})).can_update is False


# ---- allowed statuses from the update form (validation only) -------------------------

@respx.mock
async def test_allowed_statuses_come_from_form_validation_with_only_lock_version_sent():
    import json as _json
    form = respx.post(f"{BASE}/api/v3/work_packages/2481/form").mock(return_value=httpx.Response(200, json={
        "_embedded": {"schema": {"status": {"_links": {"allowedValues": [
            {"href": "/api/v3/statuses/7", "title": "In progress"},
            {"href": "/api/v3/statuses/11", "title": "Test failed"},
        ]}}}}}))
    async with OpenProjectClient(BASE, SECRET) as op:
        ids = await op.get_allowed_status_ids(2481, lock_version=9)
    assert ids == {7, 11}
    assert form.call_count == 1
    assert _json.loads(form.calls[0].request.content) == {"lockVersion": 9}


@respx.mock
async def test_form_without_a_status_list_is_an_error_not_an_empty_allowance():
    respx.post(f"{BASE}/api/v3/work_packages/2481/form").mock(
        return_value=httpx.Response(200, json={"_embedded": {"schema": {}}}))
    async with OpenProjectClient(BASE, SECRET) as op:
        with pytest.raises(OpenProjectError, match="allowed statuses not returned"):
            await op.get_allowed_status_ids(2481, lock_version=9)
