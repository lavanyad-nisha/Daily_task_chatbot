"""Render an EmployeeSummary as plain text for Google Chat.

Shows assigned tasks with their OpenProject status. Due dates, estimates and completion
percentages are intentionally left out because the source data is stale.
"""

from daily_task_bot.domain.models import EmployeeSummary, WorkPackage


def _line(wp: WorkPackage) -> str:
    return f"- #{wp.id} {wp.subject} ({wp.status_name})"


def _section(title: str, items: list[WorkPackage]) -> list[str]:
    if not items:
        return []
    return [f"*{title}* ({len(items)})", *(_line(wp) for wp in items), ""]


def render_summary(summary: EmployeeSummary) -> str:
    lines = [
        f"Good morning {summary.employee.name}, here is your task summary for "
        f"{summary.run_date.isoformat()}.",
        "",
    ]

    if summary.leave.on_leave:
        lines += [
            f"You are on leave until {summary.leave.leave_end.isoformat()}. "
            "Your open tasks are listed below for cover.",
            "",
        ]
        lines += _section("Needs cover", summary.needs_cover)
    else:
        if summary.leave.returning_today:
            lines += ["Welcome back. Your tasks are active again today.", ""]
        lines += _section("Assigned tasks", summary.assigned)
        if not summary.assigned:
            lines += ["No open tasks.", ""]

    if summary.chat_updates:
        lines.append(f"*Chat updates* ({len(summary.chat_updates)})")
        for update in summary.chat_updates:
            lines.append(f"- #{update.work_package_id}: {update.message.text[:120]}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
