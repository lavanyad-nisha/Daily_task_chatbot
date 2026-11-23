# API mappings

Status key: **Verified** = checked with a live read-only call. **Unverified** = documented
from the API reference or planned, not yet checked. **Blocked** = needs details or access.

## DSR form (Google Chat) and data flow

| Form field | Source | Stored as | Status |
|---|---|---|---|
| Task title | OpenProject work package `subject` | Read at form time, not stored | Verified (subject field read live) |
| Task ID | OpenProject work package `id` | `dsr_entries.work_package_id` | Verified |
| Status (display only) | OpenProject `_links.status` | Read at form time | Verified |
| Hours worked | Employee input | `dsr_entries.hours_spent` (0 < h ≤ 24) | Model in place |
| Work description | Employee input | `dsr_entries.work_done` | Model in place |
| Remaining work | Employee input (optional) | `dsr_entries.remaining_work` | Open decision: required or optional |

The form shows only assigned, open tasks. Dates, estimates and completion percentages are
not read into the form or the summary.

## OpenProject

### Read (Verified)

| Purpose | Endpoint | Result |
|---|---|---|
| Current user | `GET /api/v3/users/me` | 200, returns user id |
| Statuses | `GET /api/v3/statuses` | 200, 17 statuses, 2 closed |
| Projects visible | `GET /api/v3/projects` | 200, 13 visible |
| Open tasks | `GET /api/v3/work_packages` with `status_id` filter `o` | 200, paginated |
| Tasks for one user | Same, plus `assignee` filter `=` with the user id | 200, 84 open for the test user |
| Auth check | Request without credentials | 401 |

Filters are passed as a JSON array in the `filters` query parameter. Pagination uses the
`offset` page number and `pageSize`.

### Time-entry write (schema verified read-only; no POST sent)

Schema checked live with `GET /api/v3/time_entries/schema`:

| Field | Required | Writable | Mapping |
|---|---|---|---|
| `entity` link | Yes | Yes | `/api/v3/work_packages/{work_package_id}` (from the DSR entry) |
| `spentOn` | Yes | Yes | DSR date, `YYYY-MM-DD` |
| `hours` | Yes | Yes | ISO 8601 duration rounded to whole minutes, e.g. `PT2H30M` (`domain/sync_mapping.py`) |
| `activity` link | No (has default) | Yes | Omitted unless configured. OpenProject applies its default. |
| `project` link | No | Yes | Not sent. Derived from the work package. |
| `comment` | No | Yes | Employee description, plus `[dtb:dsr-entry:{id}:openproject_time]` on its own line |
| `user` | Yes | No | Set by OpenProject from the API key's user |

Endpoints that do not exist on this instance (checked read-only, all 404):
`/api/v3/projects/{id}/time_entries/activities`, `/api/v3/time_entries/activities`,
`/api/v3/time_entries/schemas`. Activity options are not needed for the first version.

### Time-entry lookup and duplicate detection (verified read-only)

`GET /api/v3/time_entries` filters, tested against the live instance:

| Filter | Operator and value | Result |
|---|---|---|
| `entity_type` | `=` `WorkPackage` | Works |
| `entity_id` | `=` work package ID | Works |
| `user_id` | `=` user ID | Works |
| `spent_on` | `<>d` with `[day, day]` | Works (exact day) |
| `work_package_id` | any | **400, filter does not exist** |
| `comment` | any | **400, filter does not exist** |
| `spent_on` | `=d` with no values | 400, date can't be blank |

Combined filter `entity_type` + `entity_id` + `user_id` + `spent_on` returned exactly the
expected entry (ID 41) for work package 1809, user 42, day 2026-07-28. The same work package
with another user or another day returned nothing.

**Comments cannot be searched on the server.** Marker detection therefore works in two steps:

1. Server: fetch candidates with the four filters above (all pages are followed).
2. Client: keep candidates whose comment contains the exact bracketed key, for example
   `[dtb:dsr-entry:7:openproject_time]`. The brackets stop key 7 matching entries 17 or 70.

Decision before any POST (`domain/sync_mapping.decide_time_entry_action`, pure):

| Situation | Action |
|---|---|
| No candidate carries the marker, and no stored ID | `create` |
| Exactly one candidate carries the marker | `reuse` (store its ID, no POST) |
| Two or more candidates carry the marker | `needs_review` (never POST) |
| A stored ID exists and is among the candidates | `reuse` |
| A stored ID exists but search cannot see it (for example, the comment was edited) | `needs_review` (never POST) |
| A stored ID exists and a different entry carries the marker | `needs_review` |

Limits to keep in mind:

- The check runs only for the entry's own user. Entries logged by someone else on the same
  work package are not candidates.
- If an employee edits or removes the marker in the OpenProject UI, search can't find the
  entry. A stored ID guards against a second POST in that case, and the record goes to review.
- The live check of this flow is opt-in: `DTB_RUN_LIVE_READONLY=1 uv run pytest tests/test_live_readonly_time_entries.py`.

### Lock and concurrency

Time-entry creation does not use a lock version. Progress updates are no longer in scope, so
no lock-version handling is needed for this flow.

## Time-entry sync flow (implemented, gated)

1. Enqueue: each entry of a confirmed, current DSR gets one `openproject_time` record.
2. Authorize: the DSR is confirmed and current, the employee is active and has an OpenProject ID.
3. Check the work package: it exists, is open, and is assigned to the employee (live GET verified).
4. Detect: search for existing entries and check the marker. Reuse, flag or create.
5. Create: POST only when nothing matches. Store the returned ID.
6. Failures: retryable errors back off (30 s, doubling, capped at 1 h, max 5 attempts).
   Permanent errors stop. Authentication and permission errors go to review.
   A timeout is retried, and detection runs first, so an entry created before the timeout is reused.

## Jibble

**Blocked.** The Jibble work-log API is not yet confirmed. Needed before implementation:

- Endpoint and authentication
- Required fields for a work entry (person, project, task or activity, date, duration, description)
- Whether entries can be updated, and how
- Whether the API accepts an external reference or idempotency key
- Rate limits
- Mapping from OpenProject project and activity to Jibble project and activity (owner to be agreed)
- Read endpoint for time off and tracked time, kept separate from DSR hours

Until this is confirmed, `external_sync_records` supports the `jibble_time` target, but no
Jibble payload is built or sent.

## Google Chat

| Purpose | Status |
|---|---|
| Find DM space for a user (`spaces:findDirectMessage`) | Implemented, not verified live (needs service account) |
| Post message (`spaces/{space}/messages`) | Implemented, not verified live. Message length limit to verify. |
| List messages (`spaces/{space}/messages`, UTC filter) | Implemented and tested offline. Live read scope to confirm. |
| Card with task list and Submit DSR button | Unverified. Card format to confirm against the Chat cards reference. |
| Inbound button and form events | Blocked. Event payload shape and endpoint configuration needed. |

## Sync tracking (independent per target)

`external_sync_records` holds one row per DSR entry per target:

- Targets: `openproject_time`, `jibble_time`
- Statuses: `pending`, `in_progress`, `sent`, `retry_wait`, `failed_permanent`, `needs_review`
- Error classes: `retryable`, `permanent`
- Retry fields: `attempt_count`, `max_attempts`, `next_attempt_at`, `locked_until`, `locked_by`
- Duplicate protection: unique idempotency key per entry and target, plus a payload hash
  (SHA-256 of the canonical payload). A retry with a changed hash is treated as an update.

Each target succeeds or fails on its own, so an OpenProject failure doesn't block the Jibble
record, and the reverse.

## Not yet done (no external writes)

- No live POST to OpenProject, and no Jibble calls.
- The time-entry POST is implemented and tested against a mocked server. It has **not** been
  sent to the live instance. It is gated: the client refuses to POST unless created with
  `writes_enabled=True`, and the sync runner does nothing unless writes are allowed.
  The first live POST needs an approved test task.
- Chat card and event formats need verifying.
