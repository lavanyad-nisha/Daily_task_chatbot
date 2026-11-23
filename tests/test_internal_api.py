from datetime import date
"""End-to-end DSR-to-OpenProject flow through the internal API. No Chat, no network.

The OpenProject client is replaced by a fake that records every create. Data is committed,
so each test truncates the test database afterwards (guarded to dtb_test only).
"""

import argparse
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from daily_task_bot.api.internal import get_openproject_port, get_session_factory
from daily_task_bot.clients.openproject import Status, WorkPackageDetail
from daily_task_bot.config import Settings, get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine, make_session_factory
from daily_task_bot.db.models import Employee
from daily_task_bot.main import app

ROOT = Path(__file__).resolve().parent.parent
SECRET = "internal-test-secret"
OP_USER = 42
WP = 1809
TABLES = [
    "external_sync_records", "dsr_entries", "dsr_submissions",
    "daily_plan_items", "daily_plans", "employees",
]


class FakeOpenProject:
    def __init__(self):
        self.statuses = {1: Status(id=1, name="New", is_closed=False)}
        self.wps = {WP: WorkPackageDetail(id=WP, assignee_id=OP_USER, status_is_closed=False)}
        self.creates: list[dict] = []

    async def get_statuses(self):
        return self.statuses

    async def get_work_package(self, work_package_id, statuses=None):
        return self.wps.get(work_package_id)

    async def find_time_entry_candidates(self, *, work_package_id, user_id, spent_on):
        return []

    async def create_time_entry(self, payload):
        self.creates.append(payload)
        return 900 + len(self.creates)

    async def aclose(self):
        return None


@pytest.fixture
def db():
    assert_test_database(TEST_URL)
    eng = make_engine(TEST_URL)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={TEST_URL}"])
    command.upgrade(cfg, "head")
    yield make_session_factory(eng)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    eng.dispose()


_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None
pytestmark = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")


@pytest.fixture
def client(db):
    fake = FakeOpenProject()
    state = {"allow_writes": False}

    def settings_override():
        return Settings(
            _env_file=None,
            run_secret=SECRET,
            database_url=TEST_URL,
            openproject_api_key="k",
            allow_external_writes=state["allow_writes"],
            openproject_time_activity_id=4,
            dsr_go_live_date=date(2000, 1, 1),
        )

    async def port_override():
        yield fake

    app.dependency_overrides[get_settings] = settings_override
    app.dependency_overrides[get_session_factory] = lambda: db
    app.dependency_overrides[get_openproject_port] = port_override
    try:
        yield TestClient(app), fake, state
    finally:
        app.dependency_overrides.clear()


HEADERS = {"X-Run-Secret": SECRET}


def _employee(db, *, active=True, op_user=OP_USER):
    with db() as s:
        emp = Employee(name="Asha", email=f"e-{uuid.uuid4().hex[:8]}@example.com",
                       openproject_user_id=op_user, is_active=active)
        s.add(emp)
        s.commit()
        return emp.id


def _draft(client, emp_id, *, day="2026-11-19", wp=WP, hours=2.5, work="Fixed login bug"):
    return client.post("/internal/dsr/drafts", headers=HEADERS, json={
        "employee_id": emp_id, "dsr_date": day,
        "entries": [{"work_package_id": wp, "hours_spent": hours, "work_done": work}],
    })


# ---- security ------------------------------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("POST", "/internal/dsr/drafts"),
    ("POST", "/internal/dsr/1/confirm"),
    ("GET", "/internal/dsr/1/sync-status"),
    ("POST", "/internal/openproject-sync/preview"),
    ("POST", "/internal/openproject-sync/run"),
])
def test_every_internal_route_requires_the_secret(client, method, path):
    test_client, _, _ = client
    response = test_client.request(method, path, json={})
    assert response.status_code == 401
    wrong = test_client.request(method, path, headers={"X-Run-Secret": "wrong"}, json={})
    assert wrong.status_code == 401


# ---- happy path ----------------------------------------------------------------------

def test_full_flow_draft_confirm_preview_then_refused_while_writes_disabled(client, db):
    test_client, fake, state = client
    emp = _employee(db)

    draft = _draft(test_client, emp)
    assert draft.status_code == 201
    sub_id = draft.json()["submission_id"]

    confirm = test_client.post(f"/internal/dsr/{sub_id}/confirm", headers=HEADERS)
    assert confirm.status_code == 200
    assert confirm.json() == {"submission_id": sub_id, "status": "confirmed",
                              "enqueued": 2, "already_confirmed": False}

    status = test_client.get(f"/internal/dsr/{sub_id}/sync-status", headers=HEADERS).json()
    assert status["entries"][0]["status"] == "pending"

    preview = test_client.post("/internal/openproject-sync/preview", headers=HEADERS).json()
    assert preview["counts"] == {"would_post": 1}
    assert fake.creates == [], "preview must never create"

    refused = test_client.post("/internal/openproject-sync/run", headers=HEADERS)
    assert refused.status_code == 409
    assert fake.creates == []


def test_run_posts_once_when_writes_are_enabled_and_status_reports_the_reference(client, db):
    test_client, fake, state = client
    emp = _employee(db)
    sub_id = _draft(test_client, emp).json()["submission_id"]
    test_client.post(f"/internal/dsr/{sub_id}/confirm", headers=HEADERS)

    state["allow_writes"] = True
    run = test_client.post("/internal/openproject-sync/run", headers=HEADERS)
    assert run.status_code == 200
    assert run.json()["counts"] == {"posted": 1}
    assert len(fake.creates) == 1
    assert fake.creates[0]["_links"]["entity"]["href"] == f"/api/v3/work_packages/{WP}"

    status = test_client.get(f"/internal/dsr/{sub_id}/sync-status", headers=HEADERS).json()
    assert status["entries"][0]["status"] == "sent"
    assert status["entries"][0]["external_ref"] == "901"

    again = test_client.post("/internal/openproject-sync/run", headers=HEADERS).json()
    assert again["counts"] == {} and len(fake.creates) == 1


def test_confirm_twice_enqueues_nothing_the_second_time(client, db):
    test_client, _, _ = client
    sub_id = _draft(test_client, _employee(db)).json()["submission_id"]
    test_client.post(f"/internal/dsr/{sub_id}/confirm", headers=HEADERS)
    second = test_client.post(f"/internal/dsr/{sub_id}/confirm", headers=HEADERS).json()
    assert second["already_confirmed"] is True and second["enqueued"] == 0


# ---- validation and errors -----------------------------------------------------------

def test_invalid_hours_are_rejected_before_touching_the_database(client, db):
    test_client, _, _ = client
    response = _draft(test_client, _employee(db), hours=30)
    assert response.status_code == 422


def test_second_draft_for_the_same_day_is_a_conflict(client, db):
    test_client, _, _ = client
    emp = _employee(db)
    assert _draft(test_client, emp).status_code == 201
    assert _draft(test_client, emp).status_code == 409


def test_inactive_employee_is_forbidden(client, db):
    test_client, _, _ = client
    assert _draft(test_client, _employee(db, active=False)).status_code == 403


def test_unknown_submission_confirm_is_404(client):
    test_client, _, _ = client
    assert test_client.post("/internal/dsr/987654/confirm", headers=HEADERS).status_code == 404


def test_responses_contain_no_work_description_text(client, db):
    test_client, _, _ = client
    sub_id = _draft(test_client, _employee(db), work="PRIVATE-WORK-NOTE").json()["submission_id"]
    test_client.post(f"/internal/dsr/{sub_id}/confirm", headers=HEADERS)
    status = test_client.get(f"/internal/dsr/{sub_id}/sync-status", headers=HEADERS).text
    preview = test_client.post("/internal/openproject-sync/preview", headers=HEADERS).text
    assert "PRIVATE-WORK-NOTE" not in status
    assert "PRIVATE-WORK-NOTE" not in preview
