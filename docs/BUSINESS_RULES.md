# Business rules and workflow status

## Rule

Management alone assigns tasks in OpenProject. The bot never creates, assigns, reassigns or
deletes work packages, and never changes anything on a work package except its status.
The bot does not use AI.

The only OpenProject writes the bot performs:
- creating a time entry for a confirmed DSR entry (`POST /api/v3/time_entries`)
- changing a work package's status, when the employee chose one in the DSR form
  (`PATCH /api/v3/work_packages/{id}`, body limited to the lock version and status)

Status choices are limited to the list in `DTB_DSR_STATUS_CHOICES`. An empty list turns status
updates off. Enforced by `tests/test_business_rules.py`.

## Workflow

| Step | Status | Where |
|---|---|---|
| 1. Fetch assignments and statuses (read-only) | Done, verified live | `clients/openproject.py` |
| 2. Remind employees in Google Chat | Not built. Blocked on Chat app setup. | `clients/gchat.py` (post client exists, gated) |
| 3. Collect evening DSR: hours, description, remaining work | Data model and rules done. Chat form not built. Internal API only. | `services/dsr.py`, `api/internal.py` |
| 4. Sync to OpenProject: time entries | Done. Controlled live test passed (entry 50). Writes disabled. | `services/openproject_time_sync.py` |
| 4b. Sync to OpenProject: status change | Built and tested with mocks. Not verified live. Needs approval for a live test on a dedicated task. | `services/openproject_time_sync.py`, `clients/openproject.py` |
| 5. Sync to Jibble | Not built. Blocked on Jibble API details and mapping. | none yet |
| 6. Next-day reminders from latest assignments and previous DSR | Not built. Planned as T7. | none yet |

## Not allowed (enforced in code)

- Creating, assigning, reassigning or deleting work packages.
- Changing any work-package field other than status.
- Editing or deleting time entries.
- Any OpenProject write other than the two listed above.
- External writes while `DTB_ALLOW_EXTERNAL_WRITES=false`.
