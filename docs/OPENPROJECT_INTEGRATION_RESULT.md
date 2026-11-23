# OpenProject integration: controlled live test result

Date: 2026-11-19. Environment: dtb_dev (local PostgreSQL). Writes were enabled for one process
only and disabled afterwards. `.env` was never changed.

## Test entry (kept, not deleted)

| Field | Value |
|---|---|
| Time entry ID | **50** |
| Work package | #2481, "R2 Revision re-extraction fails with HTTP 422 error" (project L&TV_MAQ_QA) |
| User | 42 (API key owner, assignee of #2481) |
| Spent on | 2026-11-19 |
| Hours | 0.25 (stored as `PT15M`) |
| Comment | `DTB controlled integration test` plus marker `[dtb:dsr-entry:1:openproject_time]` |
| Activity | **none** (see open items) |

Re-verified with a read-only GET on the day of the test: entry 50 exists with the values above.

## Sequence and results

1. Task check (GET): open, assigned to user 42, log-time permission present, no existing entries
   for that work package, user and date.
2. DSR created and confirmed in dtb_dev (submission 1, one entry).
3. Preview with writes off: exactly one `would_post`, no conflicts.
4. One POST with writes enabled for that process only: `posted`, entry 50 created.
5. Read-back (GET): values in the table above.
6. Rerun with writes enabled: nothing due, nothing posted. One entry only for that day (no duplicate).
7. Fresh process with the flag unset: sync refused with `409 external writes are disabled`.

## Verified behaviour

- Assignment check, duplicate detection, and the write gate behave as designed against the
  live instance.
- The time-entry payload (`entity` link, `spentOn`, ISO-duration `hours`, marker comment) is
  accepted by OpenProject.
- Local sync state moved pending → sent, with the external reference stored.

## Open items for rollout

1. **Activity not set.** The entry has no activity link, so OpenProject applied no activity.
   Decide whether time entries must carry a specific activity, and if so, which one, before
   pilot. The fix is a single optional mapping value.
2. **Placeholder employee mapping.** The dtb_dev employee for user 42 has the placeholder email
   `op-user-42.integration-test@example.invalid`. The real employee email, Chat ID and Jibble ID
   must be loaded before any real DSR flow.
3. **Test entry 50 remains in OpenProject.** It is on a live QA task. It needs approval before
   deletion, or manual removal by someone with admin access.
4. **Run response omits the entry ID.** The result shows `posted` without the new ID. The ID is
   available from sync status. This is cosmetic.

## Status

- External writes: **disabled** (`DTB_ALLOW_EXTERNAL_WRITES=false`).
- OpenProject integration: **complete for the controlled test**. No further OpenProject
  development is planned unless items 1 or 2 block rollout.
