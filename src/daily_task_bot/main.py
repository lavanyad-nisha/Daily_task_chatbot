"""FastAPI application.

Endpoints:
- GET  /health            liveness check
- POST /internal/run      runs the daily summary. Requires header X-Run-Secret.
                          Defaults to dry-run, so nothing is posted to Chat unless
                          dry_run=false is passed and DTB_DRY_RUN is also false.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query

from daily_task_bot.api.chat import router as chat_router
from daily_task_bot.api.internal import router as internal_router
from daily_task_bot.api.security import require_run_secret
from daily_task_bot.clients.gchat import GChatClient
from daily_task_bot.clients.openproject import OpenProjectClient
from daily_task_bot.config import Settings, get_settings, live_configuration_problems
from daily_task_bot.db.engine import make_engine, make_session_factory
from daily_task_bot.services.background import background_sync_loop
from daily_task_bot.services.daily_run import run_daily_summary
from daily_task_bot.services.loaders import load_employees, load_holidays, load_leave

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start automatic background sync when enabled. It stops cleanly at shutdown."""
    settings = get_settings()
    task = None
    stop = asyncio.Event()
    if settings.background_sync_enabled and settings.database_url is not None:
        factory = make_session_factory(make_engine(settings.database_url.get_secret_value()))
        task = asyncio.create_task(background_sync_loop(settings, factory, stop))
    try:
        yield
    finally:
        if task is not None:
            stop.set()
            await task


app = FastAPI(title="Daily Task Summary Bot", version="0.1.0", lifespan=lifespan)
app.include_router(internal_router)
app.include_router(chat_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/run", dependencies=[Depends(require_run_secret)])
async def run(
    settings: Settings = Depends(get_settings),
    dry_run: bool | None = Query(default=None, description="Overrides DTB_DRY_RUN when set"),
) -> dict:
    if dry_run is False and settings.dry_run:
        # A live run requested per call is refused while the environment forces dry run.
        raise HTTPException(status_code=409, detail="live run disabled by DTB_DRY_RUN=true")
    effective_dry_run = settings.dry_run if dry_run is None else dry_run
    if not effective_dry_run and not settings.allow_external_writes:
        # External writes stay off until explicitly approved (DTB_ALLOW_EXTERNAL_WRITES=true).
        raise HTTPException(status_code=409, detail="external writes are disabled")
    if not effective_dry_run:
        problems = live_configuration_problems(settings)
        if problems:
            raise HTTPException(
                status_code=503,
                detail={"message": "live configuration is incomplete", "problems": problems},
            )

    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    run_date = now.date()

    if settings.openproject_api_key is None:
        raise HTTPException(status_code=503, detail="DTB_OPENPROJECT_API_KEY is not configured")

    employees = load_employees(settings.employees_file)
    leave_by_email = load_leave(settings.leave_file)
    holidays = load_holidays(settings.holidays_file)

    chat_reader = chat_writer = None
    gchat = None
    if settings.gchat_service_account_file:
        gchat = GChatClient(settings.gchat_service_account_file)
        chat_reader = chat_writer = gchat

    try:
        async with OpenProjectClient(
            settings.openproject_base_url, settings.openproject_api_key.get_secret_value()
        ) as op:
            report = await run_daily_summary(
                run_date=run_date,
                now=now,
                employees=employees,
                leave_by_email=leave_by_email,
                holidays=holidays,
                openproject=op,
                project_ids=settings.openproject_project_ids or None,
                chat_reader=chat_reader,
                read_spaces=settings.gchat_read_spaces,
                lookback_hours=settings.gchat_lookback_hours,
                chat_writer=chat_writer,
                dry_run=effective_dry_run,
            )
    finally:
        if gchat:
            await gchat.aclose()

    return {
        "run_date": report.run_date.isoformat(),
        "dry_run": effective_dry_run,
        "counts": report.counts(),
        "results": [
            {
                "email": r.email,
                "outcome": r.outcome,
                "error": r.error,
                # Summary text is returned only in dry run, so it can be reviewed.
                "text": r.text if effective_dry_run else None,
            }
            for r in report.results
        ],
    }
