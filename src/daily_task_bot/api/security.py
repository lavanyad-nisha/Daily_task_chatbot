"""Shared-secret protection for internal endpoints."""

import hmac

from fastapi import Depends, Header, HTTPException

from daily_task_bot.config import Settings, get_settings


def require_run_secret(
    settings: Settings = Depends(get_settings),
    x_run_secret: str | None = Header(default=None),
) -> None:
    if settings.run_secret is None:
        raise HTTPException(status_code=503, detail="DTB_RUN_SECRET is not configured")
    if not x_run_secret or not hmac.compare_digest(
        x_run_secret, settings.run_secret.get_secret_value()
    ):
        raise HTTPException(status_code=401, detail="invalid run secret")
