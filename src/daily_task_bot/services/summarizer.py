"""Summarise one employee's task update for a project Chat space, with Claude Haiku 5.5.

Privacy: only the work-description text is sent to Anthropic. No task IDs, names, emails or Chat IDs.
Fallback: if the API is missing, fails, is refused, or returns nothing usable, a rule-based
shortening of the original text is used instead. Text is never written to logs.
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

MODEL = "claude-haiku-5-5"
MAX_OUTPUT_TOKENS = 200
FALLBACK_MAX_CHARS = 300

SYSTEM_PROMPT = (
    "You summarise one employee's task update for a project status channel. "
    "Reply with one short sentence, or two at most, under 40 words, in plain English. "
    "State only what was done and its result, using facts from the update. "
    "Never comment on the update itself, never say what is missing or unclear, "
    "and never mention names, IDs or hours. "
    "Example: update 'Fixed the login null check, retested, passed.' -> "
    "'Fixed the login null check; retest passed.'"
)


class SummaryError(Exception):
    """The model did not return a usable summary."""


class Summarizer(Protocol):
    def summarize(self, text: str) -> str: ...


@dataclass(frozen=True)
class SummaryResult:
    text: str
    method: str  # "haiku" or "rule_based"


class HaikuSummarizer:
    def __init__(self, client: Any):
        self._client = client

    def summarize(self, text: str) -> str:
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            output_config={"effort": "low"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        if response.stop_reason != "end_turn":
            raise SummaryError(f"stop reason {response.stop_reason}")
        summary = "".join(b.text for b in response.content if b.type == "text").strip()
        if not summary:
            raise SummaryError("empty summary")
        return summary


def rule_based_summary(text: str, max_chars: int = FALLBACK_MAX_CHARS) -> str:
    """Keep the first sentences that fit in max_chars. Cut at a word boundary if none fit."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    kept: list[str] = []
    for sentence in sentences:
        candidate = " ".join(kept + [sentence])
        if len(candidate) > max_chars:
            break
        kept.append(sentence)
    if kept:
        return " ".join(kept)
    cut = cleaned[:max_chars].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "..."


SHORT_UPDATE_WORDS = 20  # short updates are already summaries; they go unchanged


def summarize(text: str, summarizer: Summarizer | None) -> SummaryResult:
    """Haiku summary for longer descriptions; short updates and the fallback use the rule-based text."""
    if len(text.split()) <= SHORT_UPDATE_WORDS:
        return SummaryResult(rule_based_summary(text), "rule_based")
    if summarizer is not None:
        try:
            return SummaryResult(summarizer.summarize(text), "haiku")
        except (anthropic.APIError, SummaryError):
            pass  # use the fallback; the error text may contain request details, so it is not kept
    return SummaryResult(rule_based_summary(text), "rule_based")
