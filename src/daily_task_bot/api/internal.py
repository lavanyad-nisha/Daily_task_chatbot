"""Internal endpoints for driving the DSR-to-OpenProject flow without Google Chat.

Every route requires the X-Run-Secret header. Nothing here is public.

- POST /internal/dsr/drafts                create a DSR draft for an employee and date
- POST /internal/dsr/{id}/confirm          confirm it and enqueue its OpenProject records
- GET  /internal/dsr/{id}/sync-status      per-entry sync state
- POST /internal/openproject-sync/preview  read-only: what the sync would do (GETs only)
- POST /internal/openproject-sync/run      performs the sync. Refused unless external writes are enabled.

Chat identity is not checked here. The caller is trusted through the shared secret, and that
is only acceptable for internal testing. Chat-based authorization comes later.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import sessionmaker

from daily_task_bot.api.security import require_run_secret
from daily_task_bot.clients.jibble import JibbleAuth, JibbleClient
from daily_task_bot.clients.openproject import OpenProjectClient
from daily_task_bot.config import Settings, get_settings, jibble_configuration_problems
from daily_task_bot.db.engine import make_engine, make_session_factory
from daily_task_bot.domain.sync_mapping import MAX_HOURS
from daily_task_bot.services.dsr import (
    DsrConflictError,
    DsrNotFoundError,
    DsrValidationError,
    EntryInput,
    confirm_submission,
    create_draft,
)
from daily_task_bot.services.jibble_hour_sync import run_jibble_sync
from daily_task_bot.services.openproject_time_sync import (
    AuthorizationError,
    preview_time_entry_sync,
    run_time_entry_sync,
    sync_status_for_submission,
)

router = APIRouter(prefix="/internal", dependencies=[Depends(require_run_secret)])


# ---- dependencies ---------------------------------------------------------------

@lru_cache
def _session_factory_for(url: str) -> sessionmaker:
    return make_session_factory(make_engine(url))


def get_session_factory(settings: Settings = Depends(get_settings)) -> sessionmaker:
    if settings.database_url is None:
        raise HTTPException(status_code=503, detail="DTB_DATABASE_URL is not configured")
    return _session_factory_for(settings.database_url.get_secret_value())


async def get_openproject_port(settings: Settings = Depends(get_settings)) -> AsyncIterator[OpenProjectClient]:
    if settings.openproject_api_key is None:
        raise HTTPException(status_code=503, detail="DTB_OPENPROJECT_API_KEY is not configured")
    client = OpenProjectClient(
        settings.openproject_base_url,
        settings.openproject_api_key.get_secret_value(),
        writes_enabled=settings.allow_external_writes,
    )
    try:
        yield client
    finally:
        await client.aclose()


def _now() -> datetime:
    return datetime.now(UTC)


# ---- schemas --------------------------------------------------------------------

class EntryIn(BaseModel):
    work_package_id: int = Field(gt=0)
    hours_spent: Decimal = Field(gt=0, le=MAX_HOURS)
    work_done: str = Field(min_length=1, max_length=2000)
    remaining_work: str | None = Field(default=None, max_length=2000)
    new_status_id: int | None = Field(default=None, gt=0)


class DraftIn(BaseModel):
    employee_id: int = Field(gt=0)
    dsr_date: date
    entries: list[EntryIn] = Field(min_length=1)


def _entries(items: list[EntryIn]) -> list[EntryInput]:
    return [
        EntryInput(
            work_package_id=e.work_package_id,
            hours_spent=e.hours_spent,
            work_done=e.work_done,
            remaining_work=e.remaining_work,
            new_status_id=e.new_status_id,
        )
        for e in items
    ]


def _map_domain_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, DsrValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, DsrConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, DsrNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    raise exc


# ---- DSR ------------------------------------------------------------------------

@router.post("/dsr/drafts", status_code=201)
def create_dsr_draft(
    body: DraftIn,
    factory: sessionmaker = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> dict:
    with factory() as session:
        try:
            submission = create_draft(
                session, employee_id=body.employee_id, dsr_date=body.dsr_date,
                entries=_entries(body.entries),
                allowed_status_ids=frozenset(settings.dsr_status_choices),
            )
            session.commit()
        except (DsrValidationError, DsrConflictError, AuthorizationError) as exc:
            session.rollback()
            raise _map_domain_errors(exc) from exc
        return {"submission_id": submission.id, "status": submission.status}


@router.post("/dsr/{submission_id}/confirm")
def confirm_dsr(
    submission_id: int,
    factory: sessionmaker = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> dict:
    with factory() as session:
        try:
            result = confirm_submission(session, submission_id, now=_now(),
                                        go_live=settings.dsr_go_live_date)
            session.commit()
        except (DsrValidationError, DsrConflictError, AuthorizationError, DsrNotFoundError) as exc:
            session.rollback()
            raise _map_domain_errors(exc) from exc
        return {
            "submission_id": result.submission_id,
            "status": result.status,
            "enqueued": result.enqueued,
            "already_confirmed": result.already_confirmed,
        }


@router.get("/dsr/{submission_id}/sync-status")
def dsr_sync_status(submission_id: int, factory: sessionmaker = Depends(get_session_factory)) -> dict:
    with factory() as session:
        rows = sync_status_for_submission(session, submission_id)
    return {"submission_id": submission_id, "entries": rows}


# ---- OpenProject sync -----------------------------------------------------------

@router.post("/openproject-sync/preview")
async def preview_sync(
    settings: Settings = Depends(get_settings),
    factory: sessionmaker = Depends(get_session_factory),
    client: OpenProjectClient = Depends(get_openproject_port),
) -> dict:
    summary = await preview_time_entry_sync(
        factory, client, now=_now(), time_activity_id=settings.openproject_time_activity_id,
        status_choices=frozenset(settings.dsr_status_choices))
    return {
        "counts": summary.counts(),
        "results": [
            {"record_id": r.record_id, "action": r.action, "detail": r.detail}
            for r in summary.results
        ],
    }


async def get_jibble_client(settings: Settings = Depends(get_settings)) -> AsyncIterator[JibbleClient]:
    problems = jibble_configuration_problems(settings)
    if problems:
        raise HTTPException(status_code=503, detail={"message": "Jibble configuration is incomplete",
                                                     "problems": problems})
    auth = JibbleAuth(settings.jibble_token_url, settings.jibble_client_id.get_secret_value(),
                      settings.jibble_client_secret.get_secret_value())
    client = JibbleClient(settings.jibble_people_base_url, auth,
                          time_entries_base_url=settings.jibble_time_entries_base_url,
                          writes_enabled=settings.allow_external_writes)
    try:
        yield client
    finally:
        await client.aclose()
        await auth.aclose()


@router.post("/jibble-sync/run")
async def run_jibble(
    settings: Settings = Depends(get_settings),
    factory: sessionmaker = Depends(get_session_factory),
    client: JibbleClient = Depends(get_jibble_client),
) -> dict:
    """Process pending Jibble HourEntry records for new confirmed DSRs. Refused unless writes are enabled."""
    if not settings.allow_external_writes:
        raise HTTPException(status_code=409, detail="external writes are disabled")
    summary = await run_jibble_sync(
        factory, client, now=_now(), worker_id="internal-api-jibble", writes_allowed=True,
        hour_project_id=settings.jibble_hour_project_id,
        hour_activity_id=settings.jibble_hour_activity_id,
    )
    return {
        "counts": summary.counts(),
        "results": [{"record_id": r.record_id, "action": r.action, "detail": r.detail} for r in summary.results],
    }


@router.post("/openproject-sync/run")
async def run_sync(
    settings: Settings = Depends(get_settings),
    factory: sessionmaker = Depends(get_session_factory),
    client: OpenProjectClient = Depends(get_openproject_port),
) -> dict:
    if not settings.allow_external_writes:
        raise HTTPException(status_code=409, detail="external writes are disabled")
    summary = await run_time_entry_sync(
        factory, client, now=_now(), worker_id="internal-api", writes_allowed=True,
        time_activity_id=settings.openproject_time_activity_id,
        status_choices=frozenset(settings.dsr_status_choices),
    )
    return {
        "counts": summary.counts(),
        "results": [
            {"record_id": r.record_id, "action": r.action, "detail": r.detail}
            for r in summary.results
        ],
    }
