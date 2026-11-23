"""Load employees from the Workspace export into the local database, matched by exact email.

For each active account in the export:
- If its email matches a Jibble person exactly, the Jibble person ID is used.
- Its Chat user ID comes from the export field added by lookup_chat_ids.py.
- A new employee is created with no OpenProject ID. Sync stays off for that employee until
  an OpenProject ID is added.
- An existing employee with the same email keeps its OpenProject ID and name. Only empty Chat or
  Jibble IDs are filled in.
- Any row whose Chat or Jibble ID already belongs to a different employee is reported and skipped.

Dry run by default. With --write, the database is changed in one transaction. Output is counts only.
Back up dtb_dev before using --write.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from daily_task_bot.clients.jibble import JibbleAuth, JibbleClient  # noqa: E402
from daily_task_bot.config import get_settings  # noqa: E402
from daily_task_bot.db.engine import make_engine, make_session_factory  # noqa: E402
from daily_task_bot.db.models import Employee  # noqa: E402
from daily_task_bot.domain.jibble_matching import _norm  # noqa: E402

DEFAULT_EXPORT = ROOT / "secrets" / "User_Download_23112026_114724.json"


async def jibble_people() -> list[tuple[str, str | None]]:
    s = get_settings()
    auth = JibbleAuth(s.jibble_token_url, s.jibble_client_id.get_secret_value(),
                      s.jibble_client_secret.get_secret_value())
    client = JibbleClient(s.jibble_people_base_url, auth)
    try:
        people = (await client.list_people(top=100)).get("value", [])
    finally:
        await client.aclose()
        await auth.aclose()
    return [(str(p["id"]), _norm(p.get("email"))) for p in people]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if settings.database_url is None:
        print("DTB_DATABASE_URL is not set.")
        return 2
    accounts = [u for u in json.loads(args.export.read_text(encoding="utf-8"))["users"]
                if u.get("Status [READ ONLY]") == "Active"]
    jibble_by_email: dict[str, list[str]] = {}
    for jid, email in asyncio.run(jibble_people()):
        if email:
            jibble_by_email.setdefault(email, []).append(jid)

    factory = make_session_factory(make_engine(settings.database_url.get_secret_value()))
    outcomes: Counter = Counter()
    with factory() as session:
        for account in accounts:
            email = _norm(account.get("Email Address [Required]"))
            chat_id = account.get("Chat User ID (DTB)")
            jibble_ids = jibble_by_email.get(email or "", [])
            if not email or not chat_id:
                outcomes["skipped_no_chat_id"] += 1
                continue
            if len(jibble_ids) != 1:
                outcomes["skipped_jibble_not_unique" if jibble_ids else "skipped_no_jibble_match"] += 1
                continue
            jibble_id = jibble_ids[0]

            clash = session.scalar(select(Employee).where(
                (Employee.chat_user_id == chat_id) | (Employee.jibble_person_id == jibble_id),
            ).where(Employee.email != email))
            if clash is not None:
                outcomes["review_id_belongs_to_another"] += 1
                continue

            existing = session.scalar(select(Employee).where(Employee.email == email))
            if existing is None:
                name = " ".join(filter(None, [account.get("First Name [Required]"),
                                               account.get("Last Name [Required]")])).strip() or email
                session.add(Employee(name=name, email=email, openproject_user_id=None,
                                     chat_user_id=chat_id, jibble_person_id=jibble_id, is_active=True))
                outcomes["create"] += 1
            else:
                changed = False
                if not existing.chat_user_id:
                    existing.chat_user_id = chat_id
                    changed = True
                if not existing.jibble_person_id:
                    existing.jibble_person_id = jibble_id
                    changed = True
                outcomes["update_existing" if changed else "unchanged"] += 1

        print("results (counts only):", dict(outcomes))
        if not args.write:
            session.rollback()
            print("dry run: nothing written. Back up dtb_dev, then re-run with --write.")
            return 0
        session.commit()
        print("written in one transaction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
