"""DSR preview: where each of today's saved entries would go. Builds text only; sends nothing.

Routing rules live in domain/routing.py. Entries that cannot be routed are listed under
"Needs attention" and are held for review. They do not stop the routed entries.
"""

from collections.abc import Sequence
from dataclasses import dataclass
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


def build_preview_text(
    *,
    dsr_date: date,
    lines: Sequence[PreviewLine],
    roles_by_project: dict[int, list[str]],
    routes: Sequence[Route],
) -> str:
    if not lines:
        return f"No entries saved for {dsr_date.isoformat()} yet."

    labels = {r.chat_space_id: (r.space_label or r.chat_space_id) for r in routes if r.is_active}
    grouped: dict[str, list[PreviewLine]] = {}
    flagged: list[tuple[PreviewLine, str]] = []
    for line in lines:
        if line.project_id is None:
            flagged.append((line, line.note or "work package has no project"))
            continue
        roles = roles_by_project.get(line.project_id, [])
        decision = resolve_route(line.project_id, roles, routes)
        if decision.status == ROUTED and decision.chat_space_id is not None:
            grouped.setdefault(decision.chat_space_id, []).append(line)
        else:
            reason = decision.reason
            if decision.candidates:
                reason += f" ({', '.join(decision.candidates)})"
            flagged.append((line, reason))

    out = [f"Preview for {dsr_date.isoformat()}:"]
    for space in sorted(grouped, key=lambda s: labels[s]):
        out.append(f"\n{labels[space]}:")
        for line in grouped[space]:
            out.append(f"  - #{line.work_package_id}: {line.hours} h, {line.work_done}")
    if flagged:
        out.append("\nNeeds attention (held for review, not routed):")
        for line, reason in flagged:
            out.append(f"  - #{line.work_package_id}: {reason}")
    return "\n".join(out)
