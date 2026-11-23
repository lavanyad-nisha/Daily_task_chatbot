"""Look up each employee's Google Chat user ID and write it into the Workspace export file.

How it works:
- Reads each account's email from the export.
- Calls the Workspace Directory API (users.get by email) as a Workspace admin, using the service
  account's domain-wide delegation. The Directory user ID is the same ID Chat uses: users/<id>.
- Adds the field "Chat User ID (DTB)" to each record that resolves. Every other field is kept.

Safety:
- Dry run by default. Nothing is written unless --write is given.
- With --write, the original file is copied to backups/ first, and the new file replaces the old
  one atomically.
- Output is counts only. No emails, names, or other personal data are printed.
- Only read access is requested from Google (admin.directory.user.readonly).

Requirements (set up by the Workspace admin, not by this script):
- Domain-wide delegation for the service account, with scope
  https://www.googleapis.com/auth/admin.directory.user.readonly
- --admin-subject: the admin account the service account impersonates.

Usage:
  uv run python scripts/lookup_chat_ids.py --admin-subject admin@yourdomain
  uv run python scripts/lookup_chat_ids.py --admin-subject admin@yourdomain --write
"""

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT = ROOT / "secrets" / "User_Download_23112026_114724.json"
KEY_FILE = ROOT / "secrets" / "service-account.json"
BACKUP_DIR = ROOT / "backups"
SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
DIRECTORY_URL = "https://admin.googleapis.com/admin/directory/v1/users/{key}"
EMAIL_FIELD = "Email Address [Required]"
CHAT_FIELD = "Chat User ID (DTB)"


def admin_token(subject: str) -> str:
    creds = service_account.Credentials.from_service_account_file(
        str(KEY_FILE), scopes=[SCOPE], subject=subject
    )
    creds.refresh(Request())
    return creds.token


def lookup(client: httpx.Client, token: str, email: str) -> tuple[str | None, str]:
    """Return (chat_user_name, outcome). Outcome is a short code, never the email."""
    response = client.get(DIRECTORY_URL.format(key=email), headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 200:
        user_id = response.json().get("id")
        return (f"users/{user_id}" if user_id else None), ("found" if user_id else "no_id")
    if response.status_code == 404:
        return None, "not_found"
    if response.status_code in (401, 403):
        return None, f"denied_{response.status_code}"
    return None, f"http_{response.status_code}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--admin-subject", default=os.environ.get("DTB_GOOGLE_ADMIN_SUBJECT"))
    parser.add_argument("--write", action="store_true", help="write results into the export file")
    args = parser.parse_args()

    if not args.admin_subject:
        print("Set --admin-subject (or DTB_GOOGLE_ADMIN_SUBJECT) to a Workspace admin account.")
        return 2
    if not KEY_FILE.is_file():
        print("secrets/service-account.json is missing.")
        return 2

    data = json.loads(args.export.read_text(encoding="utf-8"))
    users = data.get("users", [])
    try:
        token = admin_token(args.admin_subject)
    except Exception as exc:
        print(f"Could not obtain an admin-delegated token: {type(exc).__name__}. "
              "Check domain-wide delegation and the admin subject.")
        return 3

    outcomes: Counter = Counter()
    with httpx.Client(timeout=30) as client:
        for record in users:
            email = (record.get(EMAIL_FIELD) or "").strip().lower()
            if not email:
                outcomes["no_email"] += 1
                continue
            chat_name, outcome = lookup(client, token, email)
            outcomes[outcome] += 1
            if chat_name:
                record[CHAT_FIELD] = chat_name

    print("results (counts only):", dict(outcomes))
    print("accounts:", len(users), "| with Chat user ID:", sum(1 for u in users if u.get(CHAT_FIELD)))
    if not args.write:
        print("dry run: nothing written. Re-run with --write to save.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    BACKUP_DIR.mkdir(exist_ok=True)
    backup = BACKUP_DIR / f"{args.export.stem}_before_chat_ids_{stamp}.json"
    shutil.copy2(args.export, backup)
    temp = args.export.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, args.export)
    print(f"written. backup saved to backups/ ({backup.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
