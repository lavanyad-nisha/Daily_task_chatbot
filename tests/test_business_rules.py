"""Business rules that must hold in code, not only in documentation.

Rules:
- Management alone assigns tasks in OpenProject. The bot never creates, reassigns or deletes a
  work package, and never edits anything except a work package's status.
- The bot creates time entries. It never edits or deletes them.
- The only OpenProject writes are: POST a time entry, and PATCH a work package's status.
"""

import inspect
import re
from pathlib import Path

import daily_task_bot.clients.openproject as openproject_module
from daily_task_bot.clients.openproject import OpenProjectClient

SRC = Path(__file__).resolve().parent.parent / "src" / "daily_task_bot"
OP_SOURCE = (SRC / "clients" / "openproject.py").read_text(encoding="utf-8")
PERMITTED_WRITES = {
    ("POST", "/api/v3/time_entries"),
    ("PATCH", "/api/v3/work_packages/{work_package_id}"),
}


def test_only_permitted_openproject_write_paths_exist():
    found = {
        (verb, path)
        for verb, path in re.findall(
            r'_send\(\s*"(POST|PUT|PATCH|DELETE)"\s*,\s*f?"([^"]+)"', OP_SOURCE
        )
    }
    assert found == PERMITTED_WRITES, found


def test_put_and_delete_are_never_used_anywhere():
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for verb in ("PUT", "DELETE"):
            assert f'"{verb}"' not in text, f"{verb} found in {path.name}"


def test_patch_is_used_only_by_the_status_update_in_the_openproject_client():
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if '"PATCH"' in text:
            assert path.name == "openproject.py", f"PATCH found in {path.name}"
    assert text_of_method("update_work_package_status").count('"PATCH"') == 1


def test_only_one_work_package_mutation_method_exists_and_it_changes_status_only():
    mutators = [
        name for name, _ in inspect.getmembers(OpenProjectClient, inspect.isfunction)
        if re.search(r"(create|update|assign|reassign|delete|move|patch|edit)", name, re.IGNORECASE)
        and "work_package" in name.lower()
    ]
    assert mutators == ["update_work_package_status"], mutators


def test_no_time_entry_edit_or_delete_methods_exist():
    members = {name for name, _ in inspect.getmembers(OpenProjectClient, inspect.isfunction)}
    assert "create_time_entry" in members
    assert not any(re.search(r"(update|delete|edit).*time_entr", m) for m in members)


def test_no_assignment_helper_exists_in_the_client_module():
    assert not [n for n in dir(openproject_module) if re.search(r"assign", n, re.IGNORECASE)]


def text_of_method(name: str) -> str:
    start = OP_SOURCE.index(f"async def {name}(")
    nxt = OP_SOURCE.find("\n    async def ", start + 1)
    return OP_SOURCE[start:nxt if nxt != -1 else None]


def test_the_form_validation_post_is_used_only_for_the_allowed_status_lookup():
    """The form POST persists nothing. It must only be used to read allowed statuses."""
    text = OP_SOURCE
    assert text.count("_post_json(") == 2  # one definition, one call
    call_site = text.index("await self._post_json(")
    owner = text.rfind("    async def ", 0, call_site)
    assert "async def get_allowed_status_ids(" in text[owner:owner + 60]
