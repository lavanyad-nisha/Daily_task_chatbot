# Daily Task Summary Bot (Phase 1)

Sends each employee a private Google Chat summary of their OpenProject tasks every working morning.
Read-only for OpenProject. Google Chat is only written to when posting summaries.

## Setup

```bash
uv sync
cp .env.example .env          # fill in values; never commit .env
cp config/employees.example.json config/employees.json
cp config/leave.example.json config/leave.json        # optional
cp config/holidays.example.json config/holidays.json  # optional
```

## Run

```bash
uv run uvicorn daily_task_bot.main:app --reload
uv run pytest
```

Trigger a run (dry run by default, which returns summary text without posting):

```bash
curl -X POST "http://localhost:8000/internal/run" -H "X-Run-Secret: <DTB_RUN_SECRET>"
```

A live run needs `DTB_DRY_RUN=false` in the environment. Per-call `?dry_run=false` is refused while the environment forces dry run.

## Credentials and access needed

| Credential | Used for | Where it goes |
|---|---|---|
| OpenProject read-only service account API key | Reading statuses and work packages | `DTB_OPENPROJECT_API_KEY` |
| OpenProject base URL | Connecting to the instance | `DTB_OPENPROJECT_BASE_URL` |
| Google Chat service account JSON key | Reading and posting Chat messages | file at `DTB_GCHAT_SERVICE_ACCOUNT_FILE` (in `secrets/`, not committed) |
| Chat space IDs (bot added to each) | Reading updates from approved spaces | `DTB_GCHAT_READ_SPACES` |
| Chat user ID per employee | Delivering DMs, matching own messages | `config/employees.json` |
| OpenProject user ID per employee | Mapping tasks to people | `config/employees.json` |
| Run secret (any long random string) | Protecting `/internal/run` | `DTB_RUN_SECRET` |

Google Workspace admin approval is also needed for the Chat scopes. The read scope is still to be confirmed in the Chat feasibility spike.

## Status

- Implemented: config, OpenProject read client, Chat client (find DM, list, post), matching, leave rules, summary classification and rendering, daily run orchestration, API.
- Stand-ins until access is approved: employee list, leave, and holidays come from local JSON files. Calendar integration is not built yet.
- Not yet built: scheduler, Dockerfile, live Chat spike.

## Database (T1)

Use a dedicated PostgreSQL database and role for this project. Do not point it at an existing database.

1. Create a role and two databases, `dtb_dev` for the app and `dtb_test` for tests (run as a PostgreSQL admin, outside this repo).
2. Set `DTB_DATABASE_URL` (for example `postgresql+psycopg://dtb_app:<password>@127.0.0.1:5436/dtb_dev`) in `.env`.
3. Set `DTB_TEST_DATABASE_URL` to the `dtb_test` URL. Tests refuse any database whose name does not contain "test".
4. Apply migrations: `uv run alembic upgrade head`

Live database tests are skipped when `DTB_TEST_DATABASE_URL` is not set. Offline tests always run.

## Decisions recorded

- External writes (Chat posts now; Jibble and OpenProject later) are off by default. A live run needs `DTB_ALLOW_EXTERNAL_WRITES=true` as well as `DTB_DRY_RUN=false`.
- DSR corrections are allowed before the Jibble sync.
- Reminders are sent for missing DSRs. Not yet implemented (later task).
