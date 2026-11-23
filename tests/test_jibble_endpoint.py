"""Protected Jibble sync-run endpoint: secret, write gate and configuration checks. No network."""

from fastapi.testclient import TestClient

from daily_task_bot.api.internal import get_session_factory
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.main import app

SECRET = "jibble-endpoint-test-secret"
HEADERS = {"X-Run-Secret": SECRET}


def _complete(**overrides):
    data = dict(
        _env_file=None, run_secret=SECRET, database_url="postgresql+psycopg://u:p@localhost:1/dtb_dev",
        jibble_client_id="cid", jibble_client_secret="csecret",
        jibble_token_url="https://identity.example.test/connect/token",
        jibble_people_base_url="https://people.example.test/v1",
        jibble_time_entries_base_url="https://time.example.test/v1",
        jibble_hour_project_id="p", jibble_hour_activity_id="a",
        allow_external_writes=False,
    )
    data.update(overrides)
    return Settings(**data)


def _client(settings):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_factory] = lambda: (lambda: None)
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_jibble_run_requires_the_run_secret():
    client = _client(_complete())
    assert client.post("/internal/jibble-sync/run").status_code == 401
    assert client.post("/internal/jibble-sync/run", headers={"X-Run-Secret": "wrong"}).status_code == 401


def test_jibble_run_is_refused_while_external_writes_are_disabled():
    client = _client(_complete(allow_external_writes=False))
    response = client.post("/internal/jibble-sync/run", headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"] == "external writes are disabled"


def test_incomplete_jibble_configuration_is_reported_by_name_without_values():
    client = _client(_complete(jibble_client_secret=None, allow_external_writes=True))
    response = client.post("/internal/jibble-sync/run", headers=HEADERS)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "DTB_JIBBLE_CLIENT_SECRET is missing" in detail["problems"]
    assert "csecret" not in response.text
