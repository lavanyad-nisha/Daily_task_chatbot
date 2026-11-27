# Business rules and workflow status

## Rule

Management alone assigns tasks in OpenProject. The bot never creates, assigns, reassigns or
deletes work packages, and never changes anything on a work package except its status.
The bot uses AI in one place only: summarising an employee's DSR text for the project Chat space,
with Claude Haiku 5.5 (`claude-haiku-5-5`), approved by the TL. Only the work-description text is
sent. No IDs, names, emails or Chat IDs. The AI is the first choice for every summary. If the AI
fails for a summary (timeout, error, refusal or empty reply), that one summary uses a rule-based
shortening. The next summary tries the AI again. Each posted message records how many of its
summaries were AI and how many were rule-based. The AI never chooses tasks, statuses, hours,
projects or spaces.

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

**Eligible (DSR submission).** A task is eligible when all are true:
1. It is not completed. Completed means closed or rejected in OpenProject, or one of the statuses
   Tested (10), Resolved (16), Passed (17) (`COMPLETED_STATUS_IDS`).
2. Its assignee is that employee's OpenProject user ID.
3. It belongs to a project in `DTB_OPENPROJECT_PROJECT_IDS`, when that list is set.

**Listed (DSR card, `TASKS`, and the numbers `UPDATE` and `STATUS` use).** An eligible task is listed
only when it has a due date that is either:
- before today: shown under **Overdue**, or
- today or within the next 7 days counting today (`DTB_TASK_WINDOW_DAYS`, default 7): shown under
  **Due in the next 7 days**.

Tasks with no due date, or due after the window, are not listed. Start dates are ignored. Overdue tasks
come first, then due-soon tasks, each by due date and then ID; the numbers run straight through both
sections. The DSR card shows the same two sections.

**Due for a reminder (morning plan and daily summary).** A task is listed in the reminder only when it
is eligible and its due date is today, in `Asia/Kolkata`. Start dates and other days are ignored.

**Submission check:** a DSR entry is refused only if its task is closed, completed, or not assigned to
the employee. Future-dated and overdue tasks are accepted.

Tasks without a due date are not reminded or listed. To get one onto the list, set its due date in
OpenProject.

The earlier settings `DTB_SHOW_OVERDUE_TASKS` and `DTB_SHOW_UNDATED_TASKS` were removed. Reminders
list only tasks due today.

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
- Daily project posts run after 20:30 local time (`DTB_ROUTED_POST_TIME`, default `20:30`), because employees work until 20:30. They run in the background loop, and a post made earlier is never repeated.
- Posting is still as the bot. Posting as the employee is not built.

## Client spaces (T1; tests in `tests/test_space_links.py`)

- A Chat space is linked to one client, and a client to one space (`space_links`). An unlinked space gets nothing.
- In a space the bot must be @mentioned; commands are read from `message.argumentText` (text without the mention).
- `LINK <client>` and `UNLINK`: only users in `DTB_BOT_ADMIN_CHAT_USER_IDS`, only inside a space. `LINKED`: anyone in the space.
- Clients and their OpenProject parent project come from `DTB_CLIENTS`; the project ID is stored with the link.
- TASKS, DSR, PREVIEW, UPDATE and STATUS are refused in spaces. A chat counts as private only when Chat says it is a
  direct message (`spaceType` DIRECT_MESSAGE, `type` DM or `singleUserBotDm`); if the type is missing it is treated as a space.
