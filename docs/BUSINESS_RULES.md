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

## Task visibility (built; tests in `tests/test_task_window.py`)

Manager's rule: each task has a start date and a due date, set in OpenProject. An employee's list
(DSR card, `TASKS`, 9:30 AM plan, daily summary) shows a task only when all are true:
1. It is not completed. Completed means closed or rejected in OpenProject, or one of the statuses
   Tested (10), Resolved (16), Passed (17) (`COMPLETED_STATUS_IDS`). Completed tasks never appear.
2. Its assignee is that employee's OpenProject user ID.
3. Today is between its start date and due date, inclusive (`startDate` and `dueDate`).
4. It has both a start date and a due date. A task with either missing is hidden.
5. It belongs to a project in `DTB_OPENPROJECT_PROJECT_IDS`, when that list is set.

Overdue tasks (due date passed) are hidden, not kept. This follows "start fresh": old tasks do not
come back. Trade-off: an employee who is still working on a task after its due date can't see it
or log hours for it until management moves the dates.

Next task: once the current task is closed in OpenProject, the next task whose start date has come
appears. Nothing else needs to happen.

Submission check: a DSR entry is refused if its task is not scheduled for today (same rule as
point 3 and 4 above).

Existing tasks: they have no start or due dates yet. Under this rule they stay hidden until dates
are set in OpenProject, so the manager's new tasks are the ones that start appearing.

Note: earlier the code deliberately ignored OpenProject dates as stale (`API_MAPPINGS.md`,
`models.py`). Those notes are now out of date for start and due dates. Percentage done and
estimates are still not used.

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
