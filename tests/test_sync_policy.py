"""Pure tests for the sync retry and error policy."""

import pytest

from daily_task_bot.domain.sync_policy import (
    BACKOFF_CAP_SECONDS,
    backoff_seconds,
    classify_http_status,
    safe_error_text,
    should_give_up,
)


@pytest.mark.parametrize("status, expected", [
    (400, "permanent"),
    (404, "permanent"),
    (422, "permanent"),
    (401, "review"),
    (403, "review"),
    (409, "retryable"),
    (423, "retryable"),
    (429, "retryable"),
    (500, "retryable"),
    (503, "retryable"),
])
def test_http_status_classification(status, expected):
    assert classify_http_status(status) == expected


@pytest.mark.parametrize("status", [200, 201, 302])
def test_non_error_statuses_are_not_classified(status):
    with pytest.raises(ValueError):
        classify_http_status(status)


def test_backoff_doubles_then_caps():
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert backoff_seconds(50) == BACKOFF_CAP_SECONDS


def test_backoff_rejects_non_positive_attempt():
    with pytest.raises(ValueError):
        backoff_seconds(0)


def test_give_up_at_max_attempts():
    assert should_give_up(4, 5) is False
    assert should_give_up(5, 5) is True


def test_error_text_is_collapsed_and_truncated():
    assert safe_error_text("a \n\n  b") == "a b"
    assert len(safe_error_text("x" * 10_000)) == 500
    assert safe_error_text(None) == ""
