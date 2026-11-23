"""Optional live check of time-entry detection. Read-only GET requests only.

Runs only when DTB_RUN_LIVE_READONLY=1 and an OpenProject key is configured. It checks one
existing entry observed on the live instance, and never creates, edits or deletes anything.
"""

import os
from datetime import date

import pytest

from daily_task_bot.clients.openproject import OpenProjectClient
from daily_task_bot.config import get_settings
from daily_task_bot.domain.sync_mapping import decide_time_entry_action

_settings = get_settings()
_live = os.environ.get("DTB_RUN_LIVE_READONLY") == "1" and _settings.openproject_api_key is not None

pytestmark = pytest.mark.skipif(not _live, reason="set DTB_RUN_LIVE_READONLY=1 to run")


async def test_existing_entry_is_found_and_carries_no_marker():
    async with OpenProjectClient(
        _settings.openproject_base_url, _settings.openproject_api_key.get_secret_value()
    ) as op:
        found = await op.find_time_entry_candidates(
            work_package_id=1809, user_id=42, spent_on=date(2026, 7, 28)
        )
    assert [e.id for e in found] == [41]
    outcome = decide_time_entry_action(
        key="dtb:dsr-entry:999999:openproject_time",
        candidates=[(e.id, e.comment) for e in found],
    )
    assert outcome.action == "create"
