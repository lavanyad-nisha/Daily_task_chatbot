"""Chat DSR card structure, form parsing, and token refusal. Pure: no database, no network."""

from datetime import date
from decimal import Decimal

import pytest

from daily_task_bot.domain.chat_cards import (
    NO_CHANGE,
    CardTask,
    StatusChoice,
    build_dsr_card,
    parse_dsr_form,
)
from daily_task_bot.services.chat_events import ChatAuthError, verify_chat_token

DAY = date(2026, 11, 23)


def _card(tasks):
    return build_dsr_card(
        employee_name="Asha", dsr_date=DAY,
        tasks=[CardTask(w, f"Task {w}", "Tested") for w in tasks],
        status_choices=[StatusChoice(7, "In progress"), StatusChoice(11, "Test failed")],
    )


def _widgets(card):
    return [w for s in card["cardsV2"][0]["card"]["sections"] for w in s["widgets"]]


def test_card_has_inputs_and_a_submit_button_for_each_task():
    widgets = _widgets(_card([2481]))
    names = [list(w.values())[0].get("name") for w in widgets if isinstance(w, dict)]
    names = [n for n in names if n]
    assert {"hours_2481", "work_2481", "remaining_2481", "status_2481"} <= set(names)
    buttons = [w for w in widgets if "buttonList" in w]
    assert len(buttons) == 1
    action = buttons[0]["buttonList"]["buttons"][0]["onClick"]["action"]
    assert action["function"] == "submit_dsr"
    assert action["parameters"] == [{"key": "dsr_date", "value": "2026-11-23"}]


def test_card_status_dropdown_defaults_to_no_change():
    widgets = _widgets(_card([2481]))
    dropdown = next(w["selectionInput"] for w in widgets if "selectionInput" in w)
    assert dropdown["items"][0] == {"text": "No change", "value": NO_CHANGE, "selected": True}
    assert [i["value"] for i in dropdown["items"][1:]] == ["7", "11"]


def test_card_without_tasks_says_so_and_has_no_submit_button():
    widgets = _widgets(_card([]))
    assert not any("buttonList" in w for w in widgets)
    assert any("no overdue tasks and none due in the next 7 days" in w.get("textParagraph", {}).get("text", "")
               for w in widgets)


def _section_headers(card):
    return [s.get("header") for s in card["cardsV2"][0]["card"]["sections"]]


def test_card_puts_overdue_and_due_soon_tasks_in_their_own_sections():
    card = build_dsr_card(
        employee_name="Asha", dsr_date=DAY, status_choices=[],
        tasks=[CardTask(1, "Late one", "In progress", date(2026, 11, 20), "OVERDUE"),
               CardTask(2, "Soon one", "In progress", date(2026, 11, 25), "DUE_SOON")],
    )
    assert _section_headers(card) == [None, "Overdue", "Due in the next 7 days", None]
    sections = card["cardsV2"][0]["card"]["sections"]
    assert sections[1]["widgets"][0]["decoratedText"]["text"] == "Late one (In progress, due 20 Nov)"
    assert sections[2]["widgets"][0]["decoratedText"]["text"] == "Soon one (In progress, due 25 Nov)"
    assert "buttonList" in sections[3]["widgets"][0]


def test_card_with_only_due_soon_tasks_has_no_overdue_section():
    card = build_dsr_card(employee_name="Asha", dsr_date=DAY, status_choices=[], window_days=5,
                          tasks=[CardTask(2, "Soon one", "New", date(2026, 11, 23), "DUE_SOON")])
    assert _section_headers(card) == [None, "Due in the next 5 days", None]


def _form(**values):
    return {name: {"stringInputs": {"value": [value]}} for name, value in values.items()}


ALLOWED = frozenset({7, 11})


def test_parses_a_complete_task_row():
    parsed = parse_dsr_form(_form(hours_2481="2.5", work_2481="Fixed export",
                                  remaining_2481="Test on staging", status_2481="7"), ALLOWED)
    assert parsed.errors == []
    entry = parsed.entries[0]
    assert (entry.work_package_id, entry.hours, entry.work_done) == (2481, Decimal("2.5"), "Fixed export")
    assert entry.remaining_work == "Test on staging" and entry.new_status_id == 7


def test_blank_hours_means_the_task_was_not_worked_and_is_skipped():
    parsed = parse_dsr_form(_form(hours_2481="", work_2481=""), ALLOWED)
    assert parsed.entries == [] and parsed.errors == []


def test_hours_without_a_description_is_an_error():
    parsed = parse_dsr_form(_form(hours_2481="1"), ALLOWED)
    assert parsed.entries == [] and "work description" in parsed.errors[0]


def test_non_numeric_hours_are_an_error():
    parsed = parse_dsr_form(_form(hours_2481="two", work_2481="x"), ALLOWED)
    assert parsed.errors and "must be a number" in parsed.errors[0]


def test_status_outside_the_approved_list_is_refused():
    parsed = parse_dsr_form(_form(hours_2481="1", work_2481="x", status_2481="2"), ALLOWED)
    assert parsed.entries == [] and "not an approved choice" in parsed.errors[0]


def test_no_change_status_is_stored_as_none():
    parsed = parse_dsr_form(_form(hours_2481="1", work_2481="x", status_2481=NO_CHANGE), ALLOWED)
    assert parsed.entries[0].new_status_id is None


def test_missing_audience_refuses_every_token():
    with pytest.raises(ChatAuthError, match="audience"):
        verify_chat_token("anything", None)


def test_a_malformed_token_is_refused_without_network():
    with pytest.raises(ChatAuthError):
        verify_chat_token("not-a-jwt", "https://example.test/chat")
