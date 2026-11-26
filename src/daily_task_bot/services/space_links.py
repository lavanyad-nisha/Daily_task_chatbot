"""Link a Chat space to one client. A space belongs to one client only; an unlinked space gets nothing.

Admin checks happen in the chat handler, before these functions are called.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.db.models import SpaceLink

NOT_LINKED = "This space is not linked to a client."


def linked_client(session: Session, space_id: str) -> str | None:
    row = session.scalar(select(SpaceLink).where(SpaceLink.space_id == space_id))
    return row.client_name if row else None


def _canonical(client: str, allowed: Sequence[str]) -> str | None:
    wanted = client.strip().casefold()
    return next((name for name in allowed if name.casefold() == wanted), None)


def link_space(session: Session, *, space_id: str, client: str, linked_by: str,
               allowed_clients: Sequence[str]) -> str:
    name = _canonical(client, allowed_clients)
    if name is None:
        return f"Unknown client '{client.strip()}'. Allowed clients: {', '.join(allowed_clients)}."
    current = linked_client(session, space_id)
    if current == name:
        return f"This space is already linked to {name}."
    if current is not None:
        return f"This space is linked to {current}. Type UNLINK first, then LINK {name}."
    session.add(SpaceLink(space_id=space_id, client_name=name, linked_by=linked_by))
    session.flush()
    return f"Linked this space to {name}."


def unlink_space(session: Session, *, space_id: str) -> str:
    row = session.scalar(select(SpaceLink).where(SpaceLink.space_id == space_id))
    if row is None:
        return NOT_LINKED
    name = row.client_name
    session.delete(row)
    session.flush()
    return f"Unlinked this space from {name}."


def describe_link(session: Session, *, space_id: str) -> str:
    name = linked_client(session, space_id)
    return f"This space is linked to {name}." if name else NOT_LINKED
