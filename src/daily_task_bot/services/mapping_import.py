"""Employee mapping import: validate, detect conflicts, preview, and apply only an approved preview.

Safety rules:
- preview() never writes. It reads the database and reports what apply() would do.
- Every row is classified: create, update, unchanged, invalid or conflict.
- Conflicts block the whole import until resolved in the file.
- Employees are never deleted. Existing employees are only updated, and only after approval.
- apply() requires the plan hash from an approved preview. If the database changed since the
  preview, the hash differs and apply() refuses.
- DSR history is untouched: the import never modifies submissions or entries.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from daily_task_bot.db.models import DsrSubmission, Employee

CHAT_ID_PATTERN = re.compile(r"^users/[0-9]+$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FIELDS = ("name", "email", "openproject_user_id", "chat_user_id", "jibble_person_id", "is_active")


class MappingRow(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    openproject_user_id: int = Field(gt=0)
    chat_user_id: str | None = None
    jibble_person_id: str | None = Field(default=None, max_length=100)
    is_active: bool = True


@dataclass
class RowResult:
    line: int
    action: str  # create | update | unchanged | invalid | conflict
    email: str
    detail: str = ""
    employee_id: int | None = None
    changes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Preview:
    rows: list[RowResult]
    plan_hash: str

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.action] = out.get(r.action, 0) + 1
        return out

    @property
    def blocked(self) -> bool:
        return any(r.action in ("invalid", "conflict") for r in self.rows)


class ImportRefused(Exception):
    pass


def parse_rows(raw: list[dict]) -> tuple[list[tuple[int, MappingRow]], list[RowResult]]:
    """Validate raw rows. Returns valid rows (with line numbers) and invalid-row results."""
    valid: list[tuple[int, MappingRow]] = []
    invalid: list[RowResult] = []
    for i, item in enumerate(raw, start=1):
        email = str(item.get("email", "")).strip().lower()
        problems: list[str] = []
        try:
            row = MappingRow(**{**item, "email": email})
        except ValidationError as exc:
            problems.append("; ".join(e["msg"] for e in exc.errors()))
            row = None
        if row is not None:
            if not EMAIL_PATTERN.match(row.email):
                problems.append("email is not a valid address")
            if row.chat_user_id is not None and not CHAT_ID_PATTERN.match(row.chat_user_id):
                problems.append("chat_user_id must look like users/<digits>")
        if problems or row is None:
            invalid.append(RowResult(i, "invalid", email, "; ".join(problems)))
        else:
            valid.append((i, row))
    return valid, invalid


def _plan_hash(rows: list[RowResult]) -> str:
    canonical = [
        {"line": r.line, "action": r.action, "email": r.email, "employee_id": r.employee_id,
         "changes": r.changes}
        for r in rows
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()


def preview(session: Session, raw: list[dict]) -> Preview:
    """Read-only. Classifies every row against the current database."""
    valid, results = parse_rows(raw)

    # Duplicates inside the file.
    seen_email: dict[str, int] = {}
    seen_op: dict[int, int] = {}
    for line, row in valid:
        if row.email in seen_email:
            results.append(RowResult(line, "conflict", row.email,
                                     f"email also on line {seen_email[row.email]}"))
        seen_email.setdefault(row.email, line)
        if row.openproject_user_id in seen_op:
            results.append(RowResult(line, "conflict", row.email,
                                     f"OpenProject ID also on line {seen_op[row.openproject_user_id]}"))
        seen_op.setdefault(row.openproject_user_id, line)

    emails = [r.email for _, r in valid]
    op_ids = [r.openproject_user_id for _, r in valid]
    chat_ids = [r.chat_user_id for _, r in valid if r.chat_user_id]
    jibble_ids = [r.jibble_person_id for _, r in valid if r.jibble_person_id]

    by_email = {e.email: e for e in session.scalars(select(Employee).where(Employee.email.in_(emails)))}
    by_op = {e.openproject_user_id: e for e in session.scalars(
        select(Employee).where(Employee.openproject_user_id.in_(op_ids)))} if op_ids else {}
    by_chat = {e.chat_user_id: e for e in session.scalars(
        select(Employee).where(Employee.chat_user_id.in_(chat_ids)))} if chat_ids else {}
    by_jibble = {e.jibble_person_id: e for e in session.scalars(
        select(Employee).where(Employee.jibble_person_id.in_(jibble_ids)))} if jibble_ids else {}

    for line, row in valid:
        if any(r.line == line for r in results):
            continue  # already reported as a duplicate conflict
        match_email = by_email.get(row.email)
        match_op = by_op.get(row.openproject_user_id)
        if match_email and match_op and match_email.id != match_op.id:
            results.append(RowResult(line, "conflict", row.email,
                                     "email and OpenProject ID belong to different employees"))
            continue
        existing = match_email or match_op
        if existing is None:
            clash = _id_clash(row, by_chat, by_jibble, None)
            if clash:
                results.append(RowResult(line, "conflict", row.email, clash))
            else:
                results.append(RowResult(line, "create", row.email, changes=row.model_dump()))
            continue
        clash = _id_clash(row, by_chat, by_jibble, existing.id)
        if clash:
            results.append(RowResult(line, "conflict", row.email, clash, employee_id=existing.id))
            continue
        current = {k: getattr(existing, k) for k in FIELDS}
        wanted = row.model_dump()
        changes = {k: wanted[k] for k in FIELDS if wanted[k] != current[k]}
        if not changes:
            results.append(RowResult(line, "unchanged", row.email, employee_id=existing.id))
        else:
            has_history = session.scalar(
                select(DsrSubmission.id).where(DsrSubmission.employee_id == existing.id).limit(1)
            ) is not None
            detail = "existing employee; update needs approval"
            if has_history:
                detail = "has DSR history: update keeps its id and history; needs approval"
            results.append(RowResult(line, "update", row.email, detail,
                                     employee_id=existing.id, changes=changes))

    results.sort(key=lambda r: r.line)
    return Preview(rows=results, plan_hash=_plan_hash(results))


def _id_clash(row: MappingRow, by_chat, by_jibble, own_id: int | None) -> str | None:
    if row.chat_user_id:
        other = by_chat.get(row.chat_user_id)
        if other is not None and other.id != own_id:
            return "chat_user_id already belongs to another employee"
    if row.jibble_person_id:
        other = by_jibble.get(row.jibble_person_id)
        if other is not None and other.id != own_id:
            return "jibble_person_id already belongs to another employee"
    return None


def apply(session: Session, raw: list[dict], approved_plan_hash: str) -> dict[str, int]:
    """Apply a preview. Refuses unless the current preview matches the approved hash."""
    current = preview(session, raw)
    if current.plan_hash != approved_plan_hash:
        raise ImportRefused("the database or file changed since the approved preview; re-run preview")
    if current.blocked:
        raise ImportRefused("the preview has invalid or conflicting rows; resolve them first")

    applied = {"created": 0, "updated": 0, "unchanged": 0}
    for result in current.rows:
        if result.action == "create":
            session.add(Employee(**result.changes))
            applied["created"] += 1
        elif result.action == "update":
            employee = session.get(Employee, result.employee_id)
            for key, value in result.changes.items():
                setattr(employee, key, value)
            applied["updated"] += 1
        else:
            applied["unchanged"] += 1
    session.flush()
    return applied
