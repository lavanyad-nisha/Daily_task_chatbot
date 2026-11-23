"""Jibble settings: missing values are named, secrets are never printed, nothing defaults."""

from daily_task_bot.config import Settings, jibble_configuration_problems

SECRET = "jibble-secret-value-that-must-not-leak"


def _settings(**overrides):
    return Settings(_env_file=None, **overrides)


def test_nothing_is_configured_by_default():
    s = _settings()
    assert s.jibble_client_id is None and s.jibble_client_secret is None
    assert s.jibble_token_url is None
    assert s.jibble_people_base_url is None and s.jibble_time_entries_base_url is None


def test_missing_values_are_named_without_values():
    problems = jibble_configuration_problems(_settings())
    text = " | ".join(problems)
    assert "DTB_JIBBLE_CLIENT_ID is missing" in text
    assert "DTB_JIBBLE_CLIENT_SECRET is missing" in text
    assert "DTB_JIBBLE_TOKEN_URL is missing" in text
    assert "DTB_JIBBLE_PEOPLE_BASE_URL is missing" in text
    assert "DTB_JIBBLE_TIME_ENTRIES_BASE_URL is missing" in text


def test_urls_must_be_https():
    problems = jibble_configuration_problems(_settings(
        jibble_token_url="http://identity.example.test/token",
        jibble_people_base_url="http://people.example.test/v1",
        jibble_time_entries_base_url="http://time.example.test/v1",
    ))
    assert sum("must start with https://" in p for p in problems) == 3


def test_complete_configuration_has_no_problems():
    s = _settings(jibble_client_id="cid", jibble_client_secret=SECRET,
                  jibble_token_url="https://identity.example.test/connect/token",
                  jibble_people_base_url="https://people.example.test/v1",
                  jibble_time_entries_base_url="https://time.example.test/v1")
    assert jibble_configuration_problems(s) == []


def test_secret_is_never_shown_in_repr_or_string_form():
    s = _settings(jibble_client_secret=SECRET)
    assert SECRET not in repr(s)
    assert SECRET not in str(s.jibble_client_secret)
    assert SECRET not in repr(s.jibble_client_secret)
    assert s.jibble_client_secret.get_secret_value() == SECRET
