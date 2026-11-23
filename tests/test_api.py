from fastapi.testclient import TestClient

from daily_task_bot.config import Settings, get_settings
from daily_task_bot.main import app


def make_client(**overrides) -> TestClient:
    settings = Settings(_env_file=None, **overrides)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_health():
    assert make_client().get("/health").json() == {"status": "ok"}


def test_run_refuses_without_secret_configured():
    client = make_client(run_secret=None)
    assert client.post("/internal/run").status_code == 503


def test_run_rejects_wrong_secret():
    client = make_client(run_secret="right-secret")
    assert client.post("/internal/run", headers={"X-Run-Secret": "wrong"}).status_code == 401


def test_live_run_refused_while_env_forces_dry_run():
    client = make_client(run_secret="s", dry_run=True, openproject_api_key="k")
    response = client.post("/internal/run?dry_run=false", headers={"X-Run-Secret": "s"})
    assert response.status_code == 409


def test_live_run_refused_when_external_writes_disabled():
    client = make_client(run_secret="s", dry_run=False, allow_external_writes=False)
    response = client.post("/internal/run", headers={"X-Run-Secret": "s"})
    assert response.status_code == 409
    assert "external writes are disabled" in response.json()["detail"]
