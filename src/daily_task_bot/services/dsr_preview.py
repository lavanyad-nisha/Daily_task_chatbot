"""DSR preview and routing of saved entries to Chat spaces. Pure; sends nothing.

Routing rules live in domain/routing.py. Entries that cannot be routed are listed under
"Needs attention" and are held for review. They do not stop the routed entries.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from daily_task_bot.domain.routing import ROUTED, Route, resolve_route


@dataclass(frozen=True)
class PreviewLine:
    work_package_id: int
    project_id: int | None
    hours: Decimal
    work_done: str
    note: str | None = None  # why the project is unknown, e.g. "work package not visible"


@dataclass
class RoutingResult:
    labels: dict[str, str] = field(default_factory=dict)  # space id -> display label
    grouped: dict[str, list[PreviewLine]] = field(default_factory=dict)  # space id -> lines
    flagged: list[tuple[PreviewLine, str]] = field(default_factory=list)  # line, reason


def route_lines(
    lines: Sequence[PreviewLine],
    roles_by_project: dict[int, list[str]],
    routes: Sequence[Route],
) -> RoutingResult:
    result = RoutingResult(labels={r.chat_space_id: (r.space_label or r.chat_space_id)
                                   for r in routes if r.is_active})
    for line in lines:
        if line.project_id is None:
            result.flagged.append((line, line.note or "work package has no project"))
            continue
        roles = roles_by_project.get(line.project_id, [])
        decision = resolve_route(line.project_id, roles, routes)
        if decision.status == ROUTED and decision.chat_space_id is not None:
            result.grouped.setdefault(decision.chat_space_id, []).append(line)
        else:
            reason = decision.reason
            if decision.candidates:
                reason += f" ({', '.join(decision.candidates)})"
            result.flagged.append((line, reason))
    return result


def build_preview_text(
    *,
    dsr_date: date,
    lines: Sequence[PreviewLine],
    roles_by_project: dict[int, list[str]],
    routes: Sequence[Route],
) -> str:
    if not lines:
        return f"No entries saved for {dsr_date.isoformat()} yet."

    result = route_lines(lines, roles_by_project, routes)
    out = [f"Preview for {dsr_date.isoformat()}:"]
    for space in sorted(result.grouped, key=lambda s: result.labels[s]):
        out.append(f"\n{result.labels[space]}:")
        for line in result.grouped[space]:
            out.append(f"  - #{line.work_package_id}: {line.hours} h, {line.work_done}")
    if result.flagged:
        out.append("\nNeeds attention (held for review, not routed):")
        for line, reason in result.flagged:
            out.append(f"  - #{line.work_package_id}: {reason}")
    return "\n".join(out)
