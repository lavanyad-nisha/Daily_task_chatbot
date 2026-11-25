"""Routing of DSR entries to Chat spaces, role sync and DSR preview (Phase B and C).

Pure rules first. Database tests (role sync, route loading) use rolled-back sessions on dtb_test.
"""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from daily_task_bot.clients.openproject import MembershipRecord
from daily_task_bot.config import get_settings
from daily_task_bot.db.engine import assert_test_database, make_engine
from daily_task_bot.db.models import Employee, EmployeeProjectRole, ProjectRoute
from daily_task_bot.domain.routing import AMBIGUOUS, MISSING, ROUTED, Route, resolve_route
from daily_task_bot.services.dsr_preview import PreviewLine, build_preview_text
from daily_task_bot.services.routing_sync import load_routes, roles_by_project, sync_employee_roles

ROOT = Path(__file__).resolve().parent.parent
DAY = date(2026, 11, 25)


def route(project, role, space, label=None, active=True):
    return Route(openproject_project_id=project, role_name=role, chat_space_id=space,
                 space_label=label, is_active=active)


# ---- resolver: one route, missing, ambiguous -----------------------------------------

def test_one_matching_route_is_routed():
    d = resolve_route(10, ["QA"], [route(10, "QA", "spaces/C1")])
    assert d.status == ROUTED and d.chat_space_id == "spaces/C1"


def test_scope_example_gpl_qa_routes_to_c_gpl_qa():
    routes = [route(10, "QA", "spaces/GPLQA", "C-GPL-QA")]
    d = resolve_route(10, ["QA"], routes)
    assert d.status == ROUTED and d.chat_space_id == "spaces/GPLQA"


def test_role_match_ignores_case_and_surrounding_spaces():
    d = resolve_route(10, [" qa "], [route(10, "QA", "spaces/C1")])
    assert d.status == ROUTED


def test_employee_with_no_role_on_project_is_missing():
    d = resolve_route(10, [], [route(10, "QA", "spaces/C1")])
    assert d.status == MISSING and "no role" in d.reason


def test_role_without_a_route_is_missing():
    d = resolve_route(10, ["Developer"], [route(10, "QA", "spaces/C1")])
    assert d.status == MISSING


def test_route_for_another_project_is_not_used():
    d = resolve_route(11, ["QA"], [route(10, "QA", "spaces/C1")])
    assert d.status == MISSING


def test_inactive_route_is_ignored():
    d = resolve_route(10, ["QA"], [route(10, "QA", "spaces/C1", active=False)])
    assert d.status == MISSING


def test_routes_to_two_spaces_are_ambiguous_not_guessed():
    routes = [route(10, "QA", "spaces/B"), route(10, "Developer", "spaces/A")]
    d = resolve_route(10, ["QA", "Developer"], routes)
    assert d.status == AMBIGUOUS
    assert d.candidates == ("spaces/A", "spaces/B")
    assert d.chat_space_id is None


def test_two_roles_routing_to_the_same_space_is_routed():
    routes = [route(10, "QA", "spaces/C1"), route(10, "Developer", "spaces/C1")]
    d = resolve_route(10, ["QA", "Developer"], routes)
    assert d.status == ROUTED and d.chat_space_id == "spaces/C1"


def test_missing_project_is_missing():
    d = resolve_route(None, ["QA"], [route(10, "QA", "spaces/C1")])
    assert d.status == MISSING


# ---- preview text ------------------------------------------------------------------------

def _line(wp, project, hours="1", work="Work", note=None):
    return PreviewLine(work_package_id=wp, project_id=project, hours=Decimal(hours),
                       work_done=work, note=note)


def test_preview_groups_routed_entries_under_space_label():
    routes = [route(10, "QA", "spaces/GPLQA", "C-GPL-QA")]
    text = build_preview_text(
        dsr_date=DAY, lines=[_line(1, 10, "2", "Fixed login")],
        roles_by_project={10: ["QA"]}, routes=routes,
    )
    assert "C-GPL-QA:" in text
    assert "#1: 2 h, Fixed login" in text
    assert "Needs attention" not in text


def test_preview_lists_unroutable_entries_for_review():
    text = build_preview_text(
        dsr_date=DAY, lines=[_line(7, 10, "1", "Checked report")],
        roles_by_project={10: ["Developer"]}, routes=[route(10, "QA", "spaces/C1")],
    )
    assert "Needs attention" in text
    assert "#7: no active route for this project and role" in text


def test_preview_flags_entry_whose_work_package_is_not_visible():
    text = build_preview_text(
        dsr_date=DAY, lines=[_line(9, None, note="work package not visible in OpenProject")],
        roles_by_project={}, routes=[],
    )
    assert "#9: work package not visible in OpenProject" in text


def test_preview_with_no_entries_says_so():
    text = build_preview_text(dsr_date=DAY, lines=[], roles_by_project={}, routes=[])
    assert text == "No entries saved for 2026-11-25 yet."


# ---- membership parsing (pure) -----------------------------------------------------------

def test_membership_record_holds_project_user_and_roles():
    rec = MembershipRecord(project_id=10, user_id=42, role_names=("QA",))
    assert rec.project_id == 10 and rec.user_id == 42 and rec.role_names == ("QA",)


# ---- database behaviour (rolled back) ----------------------------------------------------

_test_url = get_settings().test_database_url
TEST_URL = _test_url.get_secret_value() if _test_url else None
pytestmark_db = pytest.mark.skipif(not TEST_URL, reason="DTB_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def engine():
    if not TEST_URL:
        pytest.skip("DTB_TEST_DATABASE_URL not set")
    assert_test_database(TEST_URL)
    eng = make_engine(TEST_URL)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={TEST_URL}"])
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    connection = engine.connect()
    transaction = connection.begin()
    s = Session(bind=connection, expire_on_commit=False)
    yield s
    s.close()
    transaction.rollback()
    connection.close()


def _employee(session, op_user, name="Route Test"):
    emp = Employee(name=name, email=f"route-{op_user}-{datetime.now(UTC).timestamp()}@example.com",
                   openproject_user_id=op_user, is_active=True)
    session.add(emp)
    session.flush()
    return emp


@pytestmark_db
def test_sync_adds_roles_then_removes_the_ones_no_longer_returned(session):
    emp = _employee(session, 90001)
    first = [MembershipRecord(project_id=7, user_id=90001, role_names=("QA", "Developer"))]
    assert sync_employee_roles(session, first) == {"added": 2, "removed": 0, "unchanged": 0}
    assert sorted(roles_by_project(session, emp.id)[7]) == ["Developer", "QA"]

    second = [MembershipRecord(project_id=7, user_id=90001, role_names=("QA",))]
    assert sync_employee_roles(session, second) == {"added": 0, "removed": 1, "unchanged": 1}
    assert roles_by_project(session, emp.id) == {7: ["QA"]}


@pytestmark_db
def test_sync_ignores_memberships_for_people_we_do_not_know(session):
    _employee(session, 90002)
    stranger = [MembershipRecord(project_id=7, user_id=99999, role_names=("QA",))]
    assert sync_employee_roles(session, stranger) == {"added": 0, "removed": 0, "unchanged": 0}


@pytestmark_db
def test_load_routes_returns_route_rows(session):
    session.add(ProjectRoute(openproject_project_id=7, role_name="QA", chat_space_id="spaces/TESTQA",
                             space_label="C-TEST-QA"))
    session.flush()
    routes = [r for r in load_routes(session) if r.chat_space_id == "spaces/TESTQA"]
    assert len(routes) == 1
    assert routes[0].openproject_project_id == 7 and routes[0].space_label == "C-TEST-QA"


@pytestmark_db
def test_resolver_uses_stored_roles_and_routes(session):
    emp = _employee(session, 90003)
    session.add(EmployeeProjectRole(employee_id=emp.id, openproject_project_id=7, role_name="QA"))
    session.add(ProjectRoute(openproject_project_id=7, role_name="QA", chat_space_id="spaces/DBQA"))
    session.flush()
    decision = resolve_route(7, roles_by_project(session, emp.id).get(7, []), load_routes(session))
    assert decision.status == ROUTED and decision.chat_space_id == "spaces/DBQA"


class _FakeOpenProject:
    """Minimal stand-in: every work package is visible and sits in project 7 unless noted."""

    def __init__(self, projects=None, hidden=()):
        self.projects = projects or {}
        self.hidden = set(hidden)

    async def get_statuses(self):
        return {}

    async def get_work_package(self, wp_id, statuses=None):
        from daily_task_bot.clients.openproject import WorkPackageDetail
        if wp_id in self.hidden:
            return None
        return WorkPackageDetail(id=wp_id, assignee_id=90005, status_is_closed=False,
                                 project_id=self.projects.get(wp_id, 7))


@pytestmark_db
async def test_preview_command_routes_saved_entries_and_flags_the_rest(session):
    from daily_task_bot.services.chat_events import preview_text_for
    from daily_task_bot.services.dsr import EntryInput, create_draft

    emp = _employee(session, 90005, name="Preview Test")
    session.add(EmployeeProjectRole(employee_id=emp.id, openproject_project_id=7, role_name="QA"))
    session.add(ProjectRoute(openproject_project_id=7, role_name="QA",
                             chat_space_id="spaces/PREVIEWQA", space_label="C-PREVIEW-QA"))
    create_draft(session, employee_id=emp.id, dsr_date=DAY, entries=[
        EntryInput(work_package_id=501, hours_spent=Decimal("1.5"), work_done="Tested login"),
        EntryInput(work_package_id=502, hours_spent=Decimal("1"), work_done="Hidden task"),
    ])
    session.flush()

    op = _FakeOpenProject(projects={501: 7}, hidden={502})
    text = await preview_text_for(session, op, emp, DAY)
    assert "C-PREVIEW-QA:" in text
    assert "#501: 1.50 h, Tested login" in text  # DB stores hours with two decimals
    assert "#502: work package not visible in OpenProject" in text


@pytestmark_db
async def test_preview_command_says_so_when_nothing_is_saved(session):
    from daily_task_bot.services.chat_events import preview_text_for

    emp = _employee(session, 90006, name="Empty Preview")
    text = await preview_text_for(session, _FakeOpenProject(), emp, DAY)
    assert text == "No entries saved for 2026-11-25 yet."


# ---- membership parsing (mocked shapes; not yet checked against the live API) -----------

def test_membership_parser_reads_user_principal_with_embedded_role_names():
    from daily_task_bot.clients.openproject import _membership_from_element
    element = {
        "_links": {
            "principal": {"href": "/api/v3/users/42"},
            "project": {"href": "/api/v3/projects/7"},
            "roles": [{"href": "/api/v3/roles/5", "title": "QA"}],
        },
        "_embedded": {"roles": [{"name": "QA"}, {"name": " Developer "}]},
    }
    rec = _membership_from_element(element)
    assert rec == MembershipRecord(project_id=7, user_id=42, role_names=("Developer", "QA"))


def test_membership_parser_falls_back_to_role_link_titles():
    from daily_task_bot.clients.openproject import _membership_from_element
    element = {
        "_links": {
            "principal": {"href": "/api/v3/users/42"},
            "project": {"href": "/api/v3/projects/7"},
            "roles": [{"href": "/api/v3/roles/5", "title": "QA"}],
        },
    }
    assert _membership_from_element(element).role_names == ("QA",)


def test_membership_parser_skips_group_principals():
    from daily_task_bot.clients.openproject import _membership_from_element
    element = {"_links": {"principal": {"href": "/api/v3/groups/3"},
                          "project": {"href": "/api/v3/projects/7"}}}
    assert _membership_from_element(element) is None
