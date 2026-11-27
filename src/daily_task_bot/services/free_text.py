"""Understand a free-text task update with Claude Haiku 5.5, so the person can check and save it.

Used only in private chats, only for messages that are not a valid command, and only when
DTB_FREE_TEXT_UPDATES is on. The AI never saves anything. It turns one message such as
"2232 completed, 2h retested the export" into a proposed change, which the bot shows as a
pre-filled form. Nothing is recorded until the person presses Save, and the save goes through the
same checks as UPDATE and STATUS.

The code checks every value the AI returns:
- the task must be one of the person's open tasks that were sent to the AI;
- the status must be one of the approved statuses (DTB_DSR_STATUS_CHOICES), matched by name;
- hours must be above 0 and at most 24.
A value that fails its check is dropped, never guessed.

Sent to Anthropic: the message text, the person's open task numbers, titles and current statuses,
and the approved status names. No names, emails or Chat IDs. Text is never written to logs;
only the error type name is logged.
"""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import anthropic

from daily_task_bot.domain.chat_cards import CardTask, StatusChoice

log = logging.getLogger("daily_task_bot.free_text")

MODEL = "claude-haiku-5-5"
MAX_OUTPUT_TOKENS = 300
TIMEOUT_SECONDS = 10.0  # Chat waits about 30 s for a reply; a normal answer takes about 1 s
MAX_MESSAGE_CHARS = 1000
MAX_WORK_DONE_CHARS = 2000
MAX_HOURS = Decimal(24)

SYSTEM_PROMPT = (
    "You read one employee's chat message about their own work and fill in a task update. "
    "You get the employee's open tasks as lines '#<id> <title> (<current status>)' and the "
    "statuses they may set. Rules:\n"
    "- task_id: the id of the one task the message is about, matched by its id (with or without #) "
    "or clearly by its title. Use null if no task is clearly meant or if several could be meant. "
    "Never pick a task that is not in the list.\n"
    "- hours: hours worked on it today if the message says so (\"2h\", \"90 min\" = 1.5), else null.\n"
    "- work_done: a short plain description of the work done, in the employee's own words, without "
    "the task id, hours or status. Null if the message only changes the status.\n"
    "- status: the exact name of one status from the allowed list that the message asks for. Match by "
    "meaning when exactly one allowed status fits (\"failed again\" -> a failed status, \"started\" -> "
    "in progress). Use null if no allowed status fits or if more than one could fit (for example "
    "\"completed\" or \"done\" when both Tested and Closed are allowed).\n"
    "- requested_status: the status words the employee used (e.g. \"completed\"), or null if they "
    "did not ask to change the status.\n"
    "Never invent work, hours or a status that the message does not state."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "hours": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "work_done": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "status": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "requested_status": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["task_id", "hours", "work_done", "status", "requested_status"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Proposal:
    """A checked proposal. Every field has passed the code checks above, or is None."""

    task: CardTask | None
    hours: Decimal | None
    work_done: str | None
    status_id: int | None
    requested_status: str | None  # what the person asked for, when it is not an approved status

    @property
    def has_change(self) -> bool:
        return self.hours is not None or self.work_done is not None or self.status_id is not None \
            or self.requested_status is not None


class Interpreter(Protocol):
    async def interpret(self, text: str, tasks: list[CardTask],
                        statuses: list[StatusChoice]) -> Proposal | None: ...


def build_user_message(text: str, tasks: list[CardTask], statuses: list[StatusChoice]) -> str:
    task_lines = "\n".join(f"#{t.work_package_id} {t.subject} ({t.status_name})" for t in tasks) or "(none)"
    status_lines = ", ".join(s.name for s in statuses) or "(none: status changes are not enabled)"
    return (f"Open tasks:\n{task_lines}\n\nStatuses the employee may set: {status_lines}\n\n"
            f"Message:\n{text[:MAX_MESSAGE_CHARS]}")


def check_proposal(raw: dict[str, Any], tasks: list[CardTask], statuses: list[StatusChoice]) -> Proposal:
    """Keep only the values that pass the code checks."""
    by_id = {t.work_package_id: t for t in tasks}
    task_id = raw.get("task_id")
    task = by_id.get(task_id) if isinstance(task_id, int) and not isinstance(task_id, bool) else None

    hours: Decimal | None = None
    value = raw.get("hours")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            candidate = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            candidate = None
        if candidate is not None and Decimal(0) < candidate <= MAX_HOURS:
            hours = candidate

    work = raw.get("work_done")
    work_done = work.strip()[:MAX_WORK_DONE_CHARS] if isinstance(work, str) and work.strip() else None

    status_id: int | None = None
    name = raw.get("status")
    if isinstance(name, str):
        wanted = name.strip().casefold()
        status_id = next((s.status_id for s in statuses if s.name.casefold() == wanted), None)
    asked = raw.get("requested_status")
    requested = asked.strip()[:60] if isinstance(asked, str) and asked.strip() and status_id is None else None
    return Proposal(task, hours, work_done, status_id, requested)


class HaikuInterpreter:
    def __init__(self, client: Any):
        self._client = client  # anthropic.AsyncAnthropic

    async def interpret(self, text: str, tasks: list[CardTask],
                        statuses: list[StatusChoice]) -> Proposal | None:
        """A checked proposal, or None when the AI call fails or is refused."""
        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                thinking={"type": "disabled"},
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_message(text, tasks, statuses)}],
            )
            if response.stop_reason != "end_turn":
                log.warning("free-text update not understood: stop reason %s", response.stop_reason)
                return None
            body = next((b.text for b in response.content if b.type == "text"), "")
            raw = json.loads(body)
            if not isinstance(raw, dict):
                return None
        except Exception as exc:  # any AI failure: the caller shows the normal help instead
            log.warning("free-text update failed: %s", type(exc).__name__)
            return None
        return check_proposal(raw, tasks, statuses)


def make_haiku_interpreter(api_key: str) -> HaikuInterpreter:
    """No retries: the person is waiting in Chat, and a failure only means they see the help text."""
    return HaikuInterpreter(anthropic.AsyncAnthropic(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0))
