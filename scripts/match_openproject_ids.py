"""Match local employees to OpenProject user IDs by exact normalized email. Dry run by default.

- Reads the OpenProject users table through a read-only transaction (read-only connection string
  from DTB_OPENPROJECT_READONLY_URL, kept in secrets/openproject-prod-readonly.env).
- Reads local employees from dtb_dev.
- Only employees with an empty OpenProject ID are considered. Existing IDs are never changed.
- Locked OpenProject accounts are excluded. Ambiguous matches are reported, never guessed.
- With --write: a backup of dtb_dev is taken first, then only the unique matches are written,
  in one transaction. Output is counts only.
"""

import argparse
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import psycopg
from sqlalchemy import select, update

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from daily_task_bot.config import get_settings  # noqa: E402
from daily_task_bot.db.engine import make_engine, make_session_factory  # noqa: E402
from daily_task_bot.db.models import Employee  # noqa: E402

PROD_FILE = ROOT / "secrets" / "openproject-prod-readonly.env"
DEV_PASSWORD_FILE = ROOT / "secrets" / "dtb-postgres.env"
LOCKED_STATUS = 3  # OpenProject: locked accounts cannot be assigned work


def _env_value(path: Path, key: str) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return None


def _norm(value: str | None) -> str | None:
    cleaned = (value or "").strip().lower()
    return cleaned or None


def read_openproject_users(url: str) -> list[tuple[int, str | None, int | None]]:
    parts = urlparse(url)
    conn = psycopg.connect(host=parts.hostname, port=parts.port, user=unquote(parts.username),
                           password=unquote(parts.password), dbname=parts.path.lstrip("/").split("/")[0],
                           connect_timeout=10)
    try:
        conn.execute("SET default_transaction_read_only = on")
        with conn.cursor() as cur:
            cur.execute("select id, mail, status from users")
            return [(row[0], _norm(row[1]), row[2]) for row in cur.fetchall()]
    finally:
        conn.rollback()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    prod_url = _env_value(PROD_FILE, "DTB_OPENPROJECT_READONLY_URL")
    if not prod_url:
        print("DTB_OPENPROJECT_READONLY_URL is not set in secrets/openproject-prod-readonly.env")
        return 2
    op_users = read_openproject_users(prod_url)

    by_email: dict[str, list[tuple[int, int | None]]] = defaultdict(list)
    for uid, mail, status in op_users:
        if mail:
            by_email[mail].append((uid, status))

    dev_url = get_settings().database_url
    settings = get_settings()
    dev_password = _env_value(DEV_PASSWORD_FILE, "POSTGRES_PASSWORD")
    dev_engine = make_engine(f"postgresql+psycopg://dtb_app:{dev_password}@127.0.0.1:5436/dtb_dev")
    factory = make_session_factory(dev_engine)
    del dev_url, settings

    outcomes: Counter = Counter()
    planned: list[tuple[int, int]] = []
    with factory() as session:
        taken = {e.openproject_user_id for e in session.scalars(select(Employee))
                 if e.openproject_user_id is not None}
        for emp in session.scalars(select(Employee).where(Employee.openproject_user_id.is_(None))):
            email = _norm(emp.email)
            hits = [h for h in by_email.get(email or "", []) if h[1] != LOCKED_STATUS]
            if not email:
                outcomes["no_email"] += 1
            elif not hits:
                outcomes["no_openproject_match"] += 1
            elif len(hits) > 1:
                outcomes["ambiguous_skipped"] += 1
            elif hits[0][0] in taken:
                outcomes["openproject_id_already_used"] += 1
            else:
                planned.append((emp.id, hits[0][0]))
                taken.add(hits[0][0])
                outcomes["unique_match"] += 1

        print("OpenProject accounts read:", len(op_users))
        print("results (counts only):", dict(outcomes))
        if not args.write:
            session.rollback()
            print("dry run: nothing written.")
            return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "backups" / f"dtb_dev_pre_openproject_ids_{stamp}.dump"
    with open(backup, "wb") as out:
        subprocess.run(["docker", "exec", "dtb-postgres", "pg_dump", "-U", "dtb_app", "-Fc", "dtb_dev"],
                       stdout=out, check=True, timeout=120)
    print("backup written:", backup.name)
    with factory() as session:
        for emp_id, op_id in planned:
            session.execute(update(Employee).where(Employee.id == emp_id, Employee.openproject_user_id.is_(None))
                            .values(openproject_user_id=op_id))
        session.commit()
    print("written:", len(planned), "employee(s) in one transaction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
