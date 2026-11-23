"""Offline tests for the OpenProject time-entry mapping. No network calls."""

from datetime import date
from decimal import Decimal

import pytest

from daily_task_bot.domain.sync_mapping import (
    hours_to_iso_duration,
    idempotency_key_from_comment,
    openproject_time_entry_payload,
    openproject_time_idempotency_key,
    payload_hash,
)


def _payload(**overrides):
    data = dict(
        dsr_entry_id=5,
        work_package_id=1774,
        spent_on=date(2026, 11, 19),
        hours=Decimal("2.5"),
        description="Fixed login bug",
    )
    data.update(overrides)
    return openproject_time_entry_payload(**data)


@pytest.mark.parametrize("hours, expected", [
    (Decimal("2.5"), "PT2H30M"),
    (Decimal("0.25"), "PT0H15M"),
    (Decimal("8"), "PT8H0M"),
    (Decimal("1.999"), "PT2H0M"),   # rounds to the nearest minute
    (Decimal("24"), "PT24H0M"),
])
def test_hours_convert_to_iso_duration(hours, expected):
    assert hours_to_iso_duration(hours) == expected


@pytest.mark.parametrize("hours", [Decimal("0"), Decimal("-1"), Decimal("24.01")])
def test_hours_out_of_range_are_rejected(hours):
    with pytest.raises(ValueError):
        hours_to_iso_duration(hours)


def test_hours_must_be_decimal():
    with pytest.raises(TypeError):
        hours_to_iso_duration(2.5)


def test_payload_shape_matches_live_time_entry_schema():
    p = _payload()
    assert p["spentOn"] == "2026-11-19"
    assert p["hours"] == "PT2H30M"
    assert p["_links"]["entity"] == {"href": "/api/v3/work_packages/1774"}
    assert "workPackage" not in p["_links"]


def test_activity_is_omitted_unless_configured():
    assert "activity" not in _payload()["_links"]
    linked = _payload(activity_id=3)["_links"]["activity"]
    assert linked == {"href": "/api/v3/time_entries/activities/3"}


def test_comment_carries_description_and_idempotency_marker():
    p = _payload()
    assert p["comment"]["raw"] == "Fixed login bug\n[dtb:dsr-entry:5:openproject_time]"


def test_marker_round_trips_to_the_same_key():
    p = _payload()
    assert idempotency_key_from_comment(p["comment"]["raw"]) == openproject_time_idempotency_key(5)


def test_comment_without_marker_has_no_key():
    assert idempotency_key_from_comment("manual entry, no marker") is None


def test_idempotency_key_is_stable_per_entry_and_differs_between_entries():
    assert openproject_time_idempotency_key(5) == openproject_time_idempotency_key(5)
    assert openproject_time_idempotency_key(5) != openproject_time_idempotency_key(6)


@pytest.mark.parametrize("overrides", [
    {"description": "   "},
    {"description": "x" * 2001},
    {"work_package_id": 0},
    {"activity_id": -1},
    {"work_package_id": -5},
])
def test_invalid_payload_inputs_are_rejected(overrides):
    with pytest.raises(ValueError):
        _payload(**overrides)


def test_payload_hash_is_deterministic_and_detects_changes():
    assert payload_hash(_payload()) == payload_hash(_payload())
    assert payload_hash(_payload()) != payload_hash(_payload(hours=Decimal("3")))
    assert payload_hash(_payload()) != payload_hash(_payload(description="Other work"))
