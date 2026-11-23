# OpenProject integration demo runbook

Scope: shows the existing API endpoints for DSR 1 and time entry 50. No frontend, no new code.
External writes stay **disabled** throughout. The database is **dtb_dev**.

## 1. Start the API (PowerShell, from the project folder)

```powershell
cd D:\TYN\Daily-Task-Chat-Bot

# Create a demo secret for this session only. Do not paste it into chat or screenshots.
$env:DTB_RUN_SECRET = uv run python -c "import secrets; print(secrets.token_urlsafe(24))"

uv run uvicorn daily_task_bot.main:app --host 127.0.0.1 --port 8000
```

Leave this window open. Stop the server with Ctrl+C.

Check before the demo: `.env` must contain `DTB_ALLOW_EXTERNAL_WRITES=false`. The database
line must end in `/dtb_dev`. The secret is never written to a file.

## 2. Endpoints

Base URL: `http://127.0.0.1:8000`

| # | Method and path | Needs `X-Run-Secret` | What it shows |
|---|---|---|---|
| 1 | GET `/health` | No | Service is up |
| 2 | GET `/internal/dsr/1/sync-status` | Yes | DSR 1 entry is `sent`, external reference `50` |
| 3 | POST `/internal/openproject-sync/preview` | Yes | Read-only check. Nothing is due, so the result is empty `{}` |
| 4 | POST `/internal/openproject-sync/run` | Yes | **Refused with 409** while writes are disabled |
| 5 | POST `/internal/dsr/1/confirm` | Yes | Already confirmed, so nothing is enqueued again (idempotent) |
| 6 | GET `/internal/dsr/1/sync-status` with **no** header | No | **401**, unauthorized |
| 7 | Browser: OpenProject, work package #2481, time tracking | (OpenProject login) | Time entry **50**: 0.25 h on 2026-11-19 |

Swagger at `http://127.0.0.1:8000/docs` lists every endpoint and is good for showing the
routes. Swagger has no field for the secret header, so use **Postman** for calls 2–6:

1. New request, method and URL as in the table.
2. Headers tab: key `X-Run-Secret`, value from your `$env:DTB_RUN_SECRET`.
3. Send.

## 3. Demo script (about 5 minutes)

1. Open `/health`. Shows the service is running.
2. Call 6 without the header. Shows that internal endpoints are protected.
3. Call 2 with the header. Shows DSR 1 is synced, with OpenProject reference 50.
4. Call 3. Shows the read-only preview, with nothing pending.
5. Call 4. Shows that a live run is refused while writes are off (409).
6. Call 5. Shows a repeated confirm does nothing new.
7. Open OpenProject, work package #2481. Show time entry 50 (0.25 h, 2026-11-19, with the
   description "DTB controlled integration test").

## 4. Known limitations to mention

- Time entry 50 has no activity, because OpenProject applied none.
- The demo employee uses a placeholder email in `dtb_dev`.
- Google Chat and Jibble are not part of this demo.
