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

## Task eligibility and reminders (Phase A; tests in `tests/test_task_window.py`)

Two rules, with different scopes:

**Eligible for update (DSR card, `TASKS`, `UPDATE`, `STATUS`, DSR submission).** A task is eligible
when all are true, on any date:
1. It is not completed. Completed means closed or rejected in OpenProject, or one of the statuses
   Tested (10), Resolved (16), Passed (17) (`COMPLETED_STATUS_IDS`).
2. Its assignee is that employee's OpenProject user ID.
3. It belongs to a project in `DTB_OPENPROJECT_PROJECT_IDS`, when that list is set.

Start and due dates do not limit updates. An employee can update an eligible task at any time, even
if it was not in that day's reminder. Overdue and future-dated tasks are eligible.

**Due for a reminder (morning plan and daily summary).** A task is listed in the reminder only when it
is eligible and its due date is today, in `Asia/Kolkata`. Start dates and other days are ignored.

**Submission check:** a DSR entry is refused only if its task is closed, completed, or not assigned to
the employee. Future-dated and overdue tasks are accepted.

Tasks without a due date are not reminded, but they stay eligible for updates.

`DTB_SHOW_OVERDUE_TASKS` and `DTB_SHOW_UNDATED_TASKS` no longer hide tasks under the revised
scope. Overdue and undated tasks stay updatable, and reminders still list only tasks due today.

Note: earlier the code deliberately ignored OpenProject dates as stale (`API_MAPPINGS.md`,
`models.py`). Start and due dates are now used. Percentage done and estimates are still not used.

"Newly assigned" marks a task as NEW. A task is NEW until the bot has shown it to the employee
once. The bot records each (employee, work package) pair the first time it shows it.

Default proposed here (option B): open assigned tasks keep appearing every day, and NEW ones are
marked and listed first. Reason: if a task were hidden after one day, the employee could no longer
log hours for it in the DSR.

Option A, if chosen instead: a task is shown only on the first day it is seen, then hidden. The
consequence is that hours can't be logged for it after that day.

Needs: a per-employee "seen" record (new table) and a migration. Tests will cover closed, reassigned,
and NEW-then-seen cases.

## DSR submission flow (as built)

1. Employee types `DSR`. The card lists the tasks from the visibility rule above.
2. Employee enters hours, work done, remaining work and status per task, then submits.
3. Bot checks the form (today's date, no field errors, at least one entry with hours).
4. Bot re-reads each task from OpenProject. Any task that is missing, closed, or not assigned to
   the employee rejects the whole submission. Nothing is saved.
5. Bot saves a draft, then confirms it. One current DSR per employee per day; a second
   submission is refused.
6. Confirmed entries are queued for OpenProject time-entry sync, only for DSRs on or after the
   go-live date. Sync re-checks open and assigned status before each write.

## Not allowed (enforced in code)

- Creating, assigning, reassigning or deleting work packages.
- Changing any work-package field other than status.
- Editing or deleting time entries.
- Any OpenProject write other than the two listed above.
- External writes while `DTB_ALLOW_EXTERNAL_WRITES=false`.

## Routing and preview (Phase B and C)

- **Route by project and role.** Each DSR entry goes to the Chat space for its work package's
  OpenProject project and the employee's role on that project (`project_routes`).
- **Roles come from OpenProject memberships.** They are read-only and refreshed from a complete
  snapshot (`employee_project_roles`). A role removed in OpenProject is removed here.
- **One match is routed.** No roles, or no active route for those roles, is `missing`. Routes to two
  or more spaces is `ambiguous`. Neither is guessed.
- **Flagged entries are held for review.** They do not stop routed entries.
- **`PREVIEW` shows the routing** of today's saved entries, grouped by space. It sends nothing.
- Posting is still as the bot. Posting as the employee is not built.
