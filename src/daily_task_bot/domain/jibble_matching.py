"""Match Jibble people to bot employees by verified email. Pure: no I/O, no writes.

Rules:
- Email comparison is case-insensitive and ignores surrounding spaces.
- A match needs exactly one Jibble person with that email, and exactly one bot employee.
- Anything else is reported as ambiguous or unmatched. Nothing is guessed.
"""

from collections import defaultdict
from dataclasses import dataclass, field


def _norm(email: str | None) -> str | None:
    value = (email or "").strip().lower()
    return value or None


@dataclass
class MatchPreview:
    employees_total: int = 0
    jibble_total: int = 0
    jibble_without_email: int = 0
    matches: list[tuple[int, str]] = field(default_factory=list)  # (employee_id, jibble_id)
    employees_unmatched: list[int] = field(default_factory=list)
    employees_ambiguous: list[int] = field(default_factory=list)
    jibble_ambiguous: list[str] = field(default_factory=list)  # Jibble IDs sharing an email

    def counts(self) -> dict[str, int]:
        return {
            "employees": self.employees_total,
            "jibble_people": self.jibble_total,
            "jibble_without_email": self.jibble_without_email,
            "matched": len(self.matches),
            "employees_unmatched": len(self.employees_unmatched),
            "employees_ambiguous": len(self.employees_ambiguous),
            "jibble_ambiguous": len(self.jibble_ambiguous),
        }


def match_by_email(
    employees: list[tuple[int, str | None]],
    jibble_people: list[tuple[str, str | None]],
) -> MatchPreview:
    """employees: (employee_id, email). jibble_people: (jibble_id, email)."""
    preview = MatchPreview(employees_total=len(employees), jibble_total=len(jibble_people))

    jibble_by_email: dict[str, list[str]] = defaultdict(list)
    for jibble_id, email in jibble_people:
        key = _norm(email)
        if key is None:
            preview.jibble_without_email += 1
        else:
            jibble_by_email[key].append(jibble_id)

    employees_by_email: dict[str, list[int]] = defaultdict(list)
    for employee_id, email in employees:
        key = _norm(email)
        if key is not None:
            employees_by_email[key].append(employee_id)

    for key, ids in jibble_by_email.items():
        if len(ids) > 1:
            preview.jibble_ambiguous.extend(ids)

    for employee_id, email in employees:
        key = _norm(email)
        if key is None:
            preview.employees_unmatched.append(employee_id)
            continue
        jibble_ids = jibble_by_email.get(key, [])
        employee_count = len(employees_by_email[key])
        if len(jibble_ids) == 1 and employee_count == 1:
            preview.matches.append((employee_id, jibble_ids[0]))
        elif len(jibble_ids) > 1 or employee_count > 1:
            preview.employees_ambiguous.append(employee_id)
        else:
            preview.employees_unmatched.append(employee_id)
    return preview
