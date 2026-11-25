"""Post an employee's routed DSR entries to their project Chat spaces.

Dry run is the default. A message is sent only when external writes are allowed AND dry_run is off.
Each space gets one message per employee per day, listing only the entries routed to that space.
Entries without a route are not posted; they are returned for review.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from daily_task_bot.domain.routing import Route
from daily_task_bot.services.dsr_preview import PreviewLine, RoutingResult, route_lines


@dataclass(frozen=True)
class PostPlan:
    space_id: str
    label: str
    text: str
    work_package_ids: tuple[int, ...]


@dataclass(frozen=True)
class PostingPlan:
    posts: list[PostPlan]
    flagged: list[tuple[PreviewLine, str]]


def plan_routed_posts(
    *,
    employee_name: str,
    dsr_date: date,
    lines: Sequence[PreviewLine],
    roles_by_project: dict[int, list[str]],
    routes: Sequence[Route],
    summaries: dict[int, str] | None = None,
) -> PostingPlan:
    """summaries maps work package ID to its summary. Missing entries use the employee's own text."""
    result: RoutingResult = route_lines(lines, roles_by_project, routes)
    summaries = summaries or {}
    posts: list[PostPlan] = []
    for space in sorted(result.grouped, key=lambda s: result.labels[s]):
        entries = result.grouped[space]
        body = [f"Daily update for {dsr_date.isoformat()} from {employee_name}:"]
        body += [
            f"- #{line.work_package_id}: {line.hours} h, "
            f"{summaries.get(line.work_package_id, line.work_done)}"
            for line in entries
        ]
        posts.append(PostPlan(
            space_id=space,
            label=result.labels[space],
            text="\n".join(body),
            work_package_ids=tuple(line.work_package_id for line in entries),
        ))
    return PostingPlan(posts=posts, flagged=result.flagged)


async def send_plans(gchat: Any, plans: Sequence[PostPlan], *, allow_writes: bool, dry_run: bool) -> list[dict]:
    """Send the plans, or report what would be sent. Never sends unless both gates are open."""
    live = allow_writes and not dry_run
    outcomes: list[dict] = []
    for plan in plans:
        base = {"space": plan.space_id, "label": plan.label, "tasks": list(plan.work_package_ids)}
        if not live:
            outcomes.append({**base, "action": "would_post"})
            continue
        try:
            response = await gchat.post_message(plan.space_id, plan.text)
            outcomes.append({**base, "action": "posted", "message": response.get("name")})
        except Exception as exc:  # isolate per space; keep only the error type name
            outcomes.append({**base, "action": "failed", "error": type(exc).__name__})
    return outcomes
