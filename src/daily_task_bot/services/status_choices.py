"""Dynamic status choices for one task: approved DSR statuses that OpenProject allows right now.

Used by the DSR form (Chat, later) to offer only valid options. Read-only: it calls the
validation-only form lookup and never sends a change.
"""

from daily_task_bot.clients.openproject import OpenProjectError, Status


async def choices_for_work_package(
    client,
    *,
    work_package_id: int,
    lock_version: int,
    approved_status_ids: frozenset[int],
    statuses: dict[int, Status],
) -> list[tuple[int, str]]:
    """Return (status_id, name) pairs: approved AND allowed for this task, sorted by name."""
    if not approved_status_ids:
        return []
    allowed = await client.get_allowed_status_ids(work_package_id, lock_version=lock_version)
    chosen = []
    for status_id in approved_status_ids & allowed:
        status = statuses.get(status_id)
        if status is None:
            raise OpenProjectError(f"status {status_id} is unknown to OpenProject; refusing to offer it")
        chosen.append((status_id, status.name))
    return sorted(chosen, key=lambda pair: pair[1].lower())
