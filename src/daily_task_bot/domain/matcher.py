"""Deterministic matching of Chat messages to OpenProject work packages by ID.

A message refers to a work package when it contains "#<id>" (for example "#1234").
Matching is purely rule-based; no NLP or AI is involved.
"""

import re
from collections.abc import Iterable

from daily_task_bot.domain.models import ChatMessage, ChatTaskUpdate

_TASK_REF = re.compile(r"(?<![\w&])#(\d{1,9})\b")


def extract_work_package_ids(text: str) -> list[int]:
    """Return the work package IDs referenced in text, in order, without duplicates."""
    seen: dict[int, None] = {}
    for match in _TASK_REF.finditer(text):
        seen.setdefault(int(match.group(1)), None)
    return list(seen)


def match_messages(
    messages: Iterable[ChatMessage], known_ids: set[int]
) -> list[ChatTaskUpdate]:
    """Pair each message with every referenced work package that is in known_ids.

    Messages referencing unknown IDs are ignored, so only assigned work is reported.
    """
    updates: list[ChatTaskUpdate] = []
    for message in messages:
        for wp_id in extract_work_package_ids(message.text):
            if wp_id in known_ids:
                updates.append(ChatTaskUpdate(work_package_id=wp_id, message=message))
    return updates
