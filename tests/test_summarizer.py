"""DSR summaries with Claude Haiku 5.5 and the rule-based fallback. Offline; fake client, no network."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from daily_task_bot.domain.routing import Route
from daily_task_bot.services.dsr_preview import PreviewLine
from daily_task_bot.services.routed_posting import plan_routed_posts
from daily_task_bot.services.summarizer import (
    MODEL,
    HaikuSummarizer,
    SummaryError,
    rule_based_summary,
    summarize,
)

UPDATE = ("Fixed the null check on the login form. Retested on staging and the login flow passed. "
          "Two bugs were logged for the reporting page and will be picked up next sprint.")


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def fake_client(response=None, error=None):
    messages = FakeMessages(response=response, error=error)
    return SimpleNamespace(messages=messages), messages


def text_response(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


# ---- Haiku path -------------------------------------------------------------------------

def test_haiku_summary_is_used_and_sends_only_the_update_text():
    client, messages = fake_client(response=text_response("Login fix passed retest; two reporting bugs logged."))
    result = summarize(UPDATE, HaikuSummarizer(client))
    assert result.method == "haiku"
    assert result.text == "Login fix passed retest; two reporting bugs logged."
    call = messages.calls[0]
    assert call["model"] == MODEL == "claude-haiku-5-5"
    assert call["messages"] == [{"role": "user", "content": UPDATE}]
    assert "system" in call and call["max_tokens"] <= 200


def test_refused_or_truncated_reply_falls_back_to_rule_based():
    client, _ = fake_client(response=text_response("partial", stop="max_tokens"))
    result = summarize(UPDATE, HaikuSummarizer(client))
    assert result.method == "rule_based"


def test_empty_reply_falls_back_to_rule_based():
    client, _ = fake_client(response=text_response("   "))
    assert summarize(UPDATE, HaikuSummarizer(client)).method == "rule_based"


def test_api_error_falls_back_without_keeping_the_error_text():
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client, _ = fake_client(error=error)
    result = summarize(UPDATE, HaikuSummarizer(client))
    assert result.method == "rule_based"
    assert "api.anthropic.com" not in result.text


def test_no_summarizer_uses_rule_based():
    assert summarize(UPDATE, None).method == "rule_based"


def test_summary_error_type_is_raised_by_haiku_summarizer_on_bad_reply():
    client, _ = fake_client(response=text_response("", stop="end_turn"))
    with pytest.raises(SummaryError):
        HaikuSummarizer(client).summarize(UPDATE)


# ---- rule-based fallback ------------------------------------------------------------------

def test_short_text_is_kept_as_it_is():
    assert rule_based_summary("Fixed the bug.") == "Fixed the bug."


def test_long_text_keeps_whole_sentences_that_fit():
    text = "First sentence is short. " + "Second sentence is much longer " * 20 + "."
    out = rule_based_summary(text, max_chars=60)
    assert out == "First sentence is short."


def test_one_huge_sentence_is_cut_at_a_word_boundary():
    text = "word " * 200
    out = rule_based_summary(text, max_chars=50)
    assert out.endswith("...") and len(out) <= 53


# ---- routed posting uses summaries --------------------------------------------------------

ROUTES = [Route(openproject_project_id=10, role_name="QA", chat_space_id="spaces/GPLQA", space_label="C-GPL-QA")]


def test_posting_uses_the_summary_and_falls_back_to_the_original_text():
    lines = [
        PreviewLine(work_package_id=101, project_id=10, hours=Decimal("2"), work_done=UPDATE),
        PreviewLine(work_package_id=102, project_id=10, hours=Decimal("1"), work_done="Short note"),
    ]
    plan = plan_routed_posts(
        employee_name="Lavanya D", dsr_date=date(2026, 11, 25), lines=lines,
        roles_by_project={10: ["QA"]}, routes=ROUTES,
        summaries={101: "Login fix passed retest."},
    )
    text = plan.posts[0].text
    assert "#101: 2 h, Login fix passed retest." in text
    assert UPDATE not in text
    assert "#102: 1 h, Short note" in text


def test_short_update_is_not_sent_to_the_model():
    client, messages = fake_client(response=text_response("should not be used"))
    result = summarize("Routing dry-run test on the scrum test task.", HaikuSummarizer(client))
    assert result.method == "rule_based"
    assert result.text == "Routing dry-run test on the scrum test task."
    assert messages.calls == []
