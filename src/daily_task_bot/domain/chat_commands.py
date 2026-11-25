"""Chat text commands. Pure: no I/O.

Only these commands are accepted. Anything else gets the help text, so the bot never guesses.

  HELP                         list the commands
  DSR                          open today's status form
  TASKS                        list your open tasks with numbers
  UPDATE <task> <hours> <work> record or change hours and work done for a task
  STATUS <task> <status id>    set the status for a task (from the approved list)
  PREVIEW                      show where today's saved entries would be routed
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

HELP_TEXT = (
    "Commands:\n"
    "TASKS - list your open tasks with numbers\n"
    "UPDATE <task> <hours> <work done> - record or change a task's hours and work\n"
    "STATUS <task> <status id> - set a task's status\n"
    "DSR - open today's status form\n"
    "PREVIEW - show where today's saved entries would be routed\n"
    "HELP - show this list"
)


@dataclass(frozen=True)
class Command:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def parse_command(text: str) -> Command:
    parts = (text or "").strip().split()
    if not parts:
        return Command("HELP")
    word = parts[0].upper()

    if word in ("HELP", "DSR", "TASKS", "PREVIEW"):
        return Command(word)

    if word == "UPDATE":
        if len(parts) < 4:
            return Command("ERROR", error="Use: UPDATE <task> <hours> <work done>")
        task = _task_number(parts[1])
        hours = _hours(parts[2])
        work = " ".join(parts[3:]).strip()
        if task is None:
            return Command("ERROR", error="The task number must be a whole number from TASKS.")
        if hours is None:
            return Command("ERROR", error="Hours must be a number above 0 and at most 24.")
        if not work:
            return Command("ERROR", error="A work description is required.")
        return Command("UPDATE", {"task": task, "hours": hours, "work": work})

    if word == "STATUS":
        if len(parts) != 3:
            return Command("ERROR", error="Use: STATUS <task> <status id>")
        task = _task_number(parts[1])
        if task is None or not parts[2].isdigit():
            return Command("ERROR", error="Use numbers only: STATUS <task> <status id>")
        return Command("STATUS", {"task": task, "status_id": int(parts[2])})

    return Command("HELP", error="Sorry, I did not understand that.")


def _task_number(value: str) -> int | None:
    return int(value) if value.isdigit() and int(value) >= 1 else None


def _hours(value: str) -> Decimal | None:
    try:
        hours = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return hours if Decimal("0") < hours <= Decimal("24") else None
