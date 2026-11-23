"""Google Chat DSR card and form parsing. Pure: no I/O.

Card structure follows the Chat API cardsV2 format. Each assigned task gets:
- hours (text input, parsed as a decimal, blank = not worked today)
- work description (multi-line text input)
- remaining work (multi-line text input, optional)
- status (dropdown of approved statuses, "No change" by default)

Form input names encode the work package ID, so the submission can be mapped back exactly.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

SUBMIT_FUNCTION = "submit_dsr"
NO_CHANGE = ""


def hours_field(wp_id: int) -> str:
    return f"hours_{wp_id}"


def work_field(wp_id: int) -> str:
    return f"work_{wp_id}"


def remaining_field(wp_id: int) -> str:
    return f"remaining_{wp_id}"


def status_field(wp_id: int) -> str:
    return f"status_{wp_id}"


@dataclass(frozen=True)
class CardTask:
    work_package_id: int
    subject: str
    status_name: str


@dataclass(frozen=True)
class StatusChoice:
    status_id: int
    name: str


def _text(value: str) -> dict[str, Any]:
    return {"textParagraph": {"text": value}}


def build_dsr_card(
    *, employee_name: str, dsr_date: date, tasks: list[CardTask], status_choices: list[StatusChoice]
) -> dict[str, Any]:
    """A cardsV2 message asking for today's DSR. Returns the message body."""
    widgets: list[dict[str, Any]] = [_text(
        f"Hi {employee_name}, please submit your work for {dsr_date.isoformat()}. "
        "Leave hours blank for tasks you did not work on."
    )]
    if not tasks:
        widgets.append(_text("You have no open assigned tasks today."))
    choice_items = [{"text": "No change", "value": NO_CHANGE, "selected": True}] + [
        {"text": c.name, "value": str(c.status_id), "selected": False} for c in status_choices
    ]
    for task in tasks:
        wp = task.work_package_id
        widgets.append({"decoratedText": {"topLabel": f"#{wp}", "text": f"{task.subject} ({task.status_name})"}})
        widgets.append({"textInput": {"name": hours_field(wp), "label": "Hours worked", "type": "SINGLE_LINE"}})
        widgets.append({"textInput": {"name": work_field(wp), "label": "Work done", "type": "MULTIPLE_LINE"}})
        widgets.append({"textInput": {"name": remaining_field(wp), "label": "Remaining work",
                                      "type": "MULTIPLE_LINE"}})
        widgets.append({"selectionInput": {"name": status_field(wp), "label": "Status",
                                           "type": "DROPDOWN", "items": choice_items}})
    if tasks:
        widgets.append({"buttonList": {"buttons": [{
            "text": "Submit DSR",
            "onClick": {"action": {"function": SUBMIT_FUNCTION,
                                   "parameters": [{"key": "dsr_date", "value": dsr_date.isoformat()}]}},
        }]}})
    return {"cardsV2": [{"cardId": "dsr-form", "card": {
        "header": {"title": "Daily status report"},
        "sections": [{"widgets": widgets}],
    }}]}


@dataclass
class ParsedEntry:
    work_package_id: int
    hours: Decimal
    work_done: str
    remaining_work: str | None
    new_status_id: int | None


@dataclass
class ParsedForm:
    entries: list[ParsedEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _value(form_inputs: dict[str, Any], name: str) -> str:
    raw = form_inputs.get(name) or {}
    values = (raw.get("stringInputs") or {}).get("value") or []
    return str(values[0]).strip() if values else ""


def parse_dsr_form(form_inputs: dict[str, Any], allowed_status_ids: frozenset[int]) -> ParsedForm:
    """Turn submitted form inputs into entries. Rows with no hours are skipped, not errors."""
    result = ParsedForm()
    wp_ids = sorted({
        int(name.split("_", 1)[1]) for name in form_inputs
        if name.startswith("hours_") and name.split("_", 1)[1].isdigit()
    })
    for wp in wp_ids:
        raw_hours = _value(form_inputs, hours_field(wp))
        if not raw_hours:
            continue
        try:
            hours = Decimal(raw_hours)
        except (InvalidOperation, ValueError):
            result.errors.append(f"hours for #{wp} must be a number")
            continue
        description = _value(form_inputs, work_field(wp))
        if not description:
            result.errors.append(f"work description for #{wp} is required")
            continue
        raw_status = _value(form_inputs, status_field(wp))
        status_id: int | None = None
        if raw_status and raw_status != NO_CHANGE:
            if not raw_status.isdigit() or int(raw_status) not in allowed_status_ids:
                result.errors.append(f"status for #{wp} is not an approved choice")
                continue
            status_id = int(raw_status)
        remaining = _value(form_inputs, remaining_field(wp)) or None
        result.entries.append(ParsedEntry(wp, hours, description, remaining, status_id))
    return result
