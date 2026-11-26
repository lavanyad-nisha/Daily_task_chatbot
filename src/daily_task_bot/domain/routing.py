"""Route DSR content to a Google Chat space by OpenProject project and employee role. Pure; no I/O.

For one DSR entry, the work package's project and the employee's roles on that project pick
one active route:
- exactly one space  -> routed to that space
- no roles on the project, or no active route for those roles -> missing
- routes that point to more than one space -> ambiguous (never guessed)

Role names are compared case-insensitively, ignoring surrounding spaces.
"""

from collections.abc import Iterable
from dataclasses import dataclass

ROUTED = "routed"
MISSING = "missing"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Route:
    openproject_project_id: int
    role_name: str
    chat_space_id: str
    space_label: str | None = None
    is_active: bool = True


@dataclass(frozen=True)
class RouteDecision:
    status: str  # ROUTED, MISSING or AMBIGUOUS
    project_id: int | None
    chat_space_id: str | None = None
    reason: str = ""
    candidates: tuple[str, ...] = ()


def _normalise(role: str) -> str:
    return role.strip().casefold()


def resolve_route(project_id: int | None, roles: Iterable[str], routes: Iterable[Route]) -> RouteDecision:
    if project_id is None:
        return RouteDecision(MISSING, None, reason="work package has no project")

    wanted = {_normalise(r) for r in roles if r and r.strip()}
    if not wanted:
        return RouteDecision(MISSING, project_id, reason="employee has no role on this project")

    matches = [
        r for r in routes
        if r.is_active and r.openproject_project_id == project_id and _normalise(r.role_name) in wanted
    ]
    spaces = tuple(sorted({r.chat_space_id for r in matches}))
    if not spaces:
        return RouteDecision(MISSING, project_id, reason="no active route for this project and role")
    if len(spaces) > 1:
        return RouteDecision(AMBIGUOUS, project_id, reason="more than one route matches",
                             candidates=spaces)
    return RouteDecision(ROUTED, project_id, chat_space_id=spaces[0])
