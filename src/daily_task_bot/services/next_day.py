"""Next-day reminder plan: open assignments plus the employee's previous confirmed DSR.

Rules:
- The task list comes from the latest OpenProject assignments. Closed tasks are excluded.
- The previous confirmed, current DSR is the most recent one dated before the run date.
- Each open task shows the remaining-work note from that DSR, if the task was in it.
- If the previous DSR chose a status and OpenProject shows a different one, the task is flagged.
- Nothing is written. Assignments are never changed.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.db.models import DsrEntry, DsrSubmission
from daily_task_bot.domain.models import WorkPackage


@dataclass(frozen=True)
class PreviousEntry:
    remaining_work: str | None
    chosen_status_id: int | None


@dataclass(frozen=True)
class PlanItem:
    work_package_id: int
    subject: str
    status_name: str
    remaining_note: str | None
    status_mismatch: str | None  # e.g. "DSR chose 'Test failed', OpenProject shows 'Tested'"


@dataclass
class NextDayPlan:
    run_date: date
    previous_dsr_date: date | None
    items: list[PlanItem] = field(default_factory=list)
    no_previous_dsr: bool = False


def load_previous_entries(
    session: Session, employee_id: int, run_date: date
) -> tuple[date | None, dict[int, PreviousEntry]]:
    """The most recent confirmed, current DSR before run_date, keyed by work package."""
    submission = session.scalars(
        select(DsrSubmission)
        .where(
            DsrSubmission.employee_id == employee_id,
            DsrSubmission.dsr_date < run_date,
            DsrSubmission.status == "confirmed",
            DsrSubmission.is_current.is_(True),
        )
        .order_by(DsrSubmission.dsr_date.desc())
        .limit(1)
    ).first()
    if submission is None:
        return None, {}
    entries = session.scalars(
        select(DsrEntry).where(DsrEntry.submission_id == submission.id)
    ).all()
    return submission.dsr_date, {
        e.work_package_id: PreviousEntry(e.remaining_work, e.new_status_id) for e in entries
    }


def build_next_day_plan(
    *,
    run_date: date,
    open_assignments: list[WorkPackage],
    previous_dsr_date: date | None,
    previous_entries: dict[int, PreviousEntry],
    status_names: dict[int, str] | None = None,
) -> NextDayPlan:
    names = status_names or {}
    plan = NextDayPlan(run_date=run_date, previous_dsr_date=previous_dsr_date,
                       no_previous_dsr=previous_dsr_date is None)
    for wp in sorted(open_assignments, key=lambda w: (w.status_name.lower(), w.id)):
        if wp.status_is_closed:
            continue
        prev = previous_entries.get(wp.id)
        mismatch = None
        if prev and prev.chosen_status_id is not None:
            chosen = names.get(prev.chosen_status_id)
            if chosen and chosen != wp.status_name:
                mismatch = f"DSR chose '{chosen}', OpenProject shows '{wp.status_name}'"
        plan.items.append(PlanItem(
            work_package_id=wp.id,
            subject=wp.subject,
            status_name=wp.status_name,
            remaining_note=prev.remaining_work if prev else None,
            status_mismatch=mismatch,
        ))
    return plan


def render_next_day_plan(plan: NextDayPlan, employee_name: str) -> str:
    lines = [f"Good morning {employee_name}, your tasks for {plan.run_date.isoformat()}."]
    if plan.no_previous_dsr:
        lines.append("No confirmed DSR found for the previous day.")
    lines.append("")
    if not plan.items:
        lines.append("No open assigned tasks.")
    for item in plan.items:
        lines.append(f"- #{item.work_package_id} {item.subject} ({item.status_name})")
        if item.remaining_note:
            lines.append(f"  Remaining from last DSR: {item.remaining_note}")
        if item.status_mismatch:
            lines.append(f"  Check status: {item.status_mismatch}")
    return "\n".join(lines).rstrip() + "\n"
