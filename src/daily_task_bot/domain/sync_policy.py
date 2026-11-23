"""Retry and error policy for outbound sync. Pure functions, no I/O.

Error classes (stored in external_sync_records.last_error_class):
- retryable: transient. Timeouts, connection errors, 409, 423, 429 and 5xx. Retry with backoff.
- permanent: the request can't succeed as sent. Validation errors, missing work package.
  Stop retrying and record the reason.
- review: a person must act first. Authentication or permission failures, or an ambiguous
  state. Never retried automatically.
"""

from typing import Literal

ErrorClass = Literal["retryable", "permanent", "review"]

BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
ERROR_TEXT_LIMIT = 500


def classify_http_status(status: int) -> ErrorClass:
    if status in (401, 403):
        return "review"
    if status in (409, 423, 429) or status >= 500:
        return "retryable"
    if 400 <= status < 500:
        return "permanent"
    raise ValueError(f"status {status} is not an error status")


def backoff_seconds(attempt: int) -> int:
    """Delay before attempt N+1. Doubles each time, capped. attempt is 1-based."""
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def should_give_up(attempt_count: int, max_attempts: int) -> bool:
    return attempt_count >= max_attempts


def safe_error_text(message: str) -> str:
    """Trim error text before storing. Never store request bodies: they hold personal work notes."""
    text = " ".join((message or "").split())
    return text[:ERROR_TEXT_LIMIT]
