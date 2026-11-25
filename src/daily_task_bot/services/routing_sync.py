"""Refresh employee_project_roles from OpenProject memberships. Writes only to the bot database.

Call this only with a complete membership snapshot. The client raises on any error, so a partial
read never reaches this function. Rows for memberships that are no longer returned are removed.
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.clients.openproject import MembershipRecord
from daily_task_bot.db.models import Employee, EmployeeProjectRole, ProjectRoute
from daily_task_bot.domain.routing import Route


def sync_employee_roles(session: Session, memberships: Iterable[MembershipRecord]) -> dict[str, int]:
    employee_ids = {
        e.openproject_user_id: e.id
        for e in session.scalars(select(Employee).where(
            Employee.is_active.is_(True), Employee.openproject_user_id.is_not(None)))
    }

    desired: set[tuple[int, int, str]] = set()
    for membership in memberships:
        employee_id = employee_ids.get(membership.user_id)
        if employee_id is None:
            continue  # not one of our employees
        for role in membership.role_names:
            if role.strip():
                desired.add((employee_id, membership.project_id, role.strip()))

    existing = {
        (row.employee_id, row.openproject_project_id, row.role_name): row
        for row in session.scalars(select(EmployeeProjectRole))
    }
    now = datetime.now(UTC)
    added = removed = unchanged = 0
    for key in desired - existing.keys():
        session.add(EmployeeProjectRole(employee_id=key[0], openproject_project_id=key[1],
                                        role_name=key[2], synced_at=now))
        added += 1
    for key, row in existing.items():
        if key in desired:
            row.synced_at = now
            unchanged += 1
        else:
            session.delete(row)
            removed += 1
    session.flush()
    return {"added": added, "removed": removed, "unchanged": unchanged}


def roles_by_project(session: Session, employee_id: int) -> dict[int, list[str]]:
    """Role names for one employee, grouped by OpenProject project ID."""
    grouped: dict[int, list[str]] = {}
    for row in session.scalars(select(EmployeeProjectRole).where(EmployeeProjectRole.employee_id == employee_id)):
        grouped.setdefault(row.openproject_project_id, []).append(row.role_name)
    return grouped


def load_routes(session: Session) -> list[Route]:
    """All route rows, active and inactive. The resolver ignores inactive ones."""
    return [
        Route(openproject_project_id=r.openproject_project_id, role_name=r.role_name,
              chat_space_id=r.chat_space_id, space_label=r.space_label, is_active=r.is_active)
        for r in session.scalars(select(ProjectRoute))
    ]
