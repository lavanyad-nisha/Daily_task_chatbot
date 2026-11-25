"""Application settings, loaded from environment variables (prefix DTB_) or a .env file."""

from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DTB_", env_file=".env", extra="ignore")

    # OpenProject (read-only)
    openproject_base_url: str = "https://openproject.example.com"
    openproject_api_key: SecretStr | None = None
    openproject_project_ids: list[int] = Field(default_factory=list)
    # Time-entry activity for every OpenProject time entry. None = time entries go to review.
    openproject_time_activity_id: int | None = None
    # Statuses employees may choose in the DSR form. Empty = status updates are off.
    dsr_status_choices: list[int] = Field(default_factory=list)
    # Forward-only go-live: only DSRs dated on or after this day are synced. Unset = nothing syncs.
    dsr_go_live_date: date | None = None
    # Task visibility (domain/task_window.py). Completed = closed in OpenProject or one of these
    # status IDs (live IDs: Tested 10, Resolved 16, Passed 17).
    completed_status_ids: list[int] = Field(default_factory=lambda: [10, 16, 17])
    # Overdue tasks stay listed (flagged OVERDUE) so late work can still be logged. False = hide.
    show_overdue_tasks: bool = True
    # Tasks without a start or due date stay listed (flagged NO DATES). False = hide them, once
    # management has dated all tasks in OpenProject.
    show_undated_tasks: bool = True

    # Anthropic (Claude Haiku 5.5 for DSR summaries). Read from ANTHROPIC_API_KEY, not DTB_-prefixed.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    # Google Chat
    gchat_service_account_file: Path | None = None
    gchat_read_spaces: list[str] = Field(default_factory=list)  # e.g. ["spaces/AAAA"]
    gchat_lookback_hours: int = 24

    # Jibble (OAuth client credentials). Secrets are SecretStr and never printed or logged.
    # The token URL and API base URL have NO defaults: fill them from Jibble's official docs.
    jibble_client_id: SecretStr | None = None
    jibble_client_secret: SecretStr | None = None
    jibble_token_url: str | None = None
    jibble_people_base_url: str | None = None
    jibble_time_entries_base_url: str | None = None
    # Jibble project and activity for hour entries. Not invented: set only after approval.
    jibble_hour_project_id: str | None = None
    jibble_hour_activity_id: str | None = None
    # Automatic background sync. Off by default. Runs only while external writes are allowed.
    background_sync_enabled: bool = False
    background_sync_interval_seconds: int = Field(default=300, ge=30)

    # Google Chat inbound events. The audience must equal the endpoint URL configured in the
    # Chat app. Unset = every inbound event is refused.
    chat_endpoint_audience: str | None = None
    # Chat app built as a Workspace add-on: the project's add-on service account that signs
    # requests, service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com.
    # Unset = add-on tokens are refused.
    chat_addon_service_account: str | None = None
    # Local times (HH:MM) for the morning plan and the evening DSR reminder.
    morning_send_time: str = "09:30"
    # Pilot: the only Chat user IDs that receive reminders or may submit a DSR. Empty = nobody.
    pilot_chat_user_ids: list[str] = Field(default_factory=list)
    evening_reminder_time: str = "18:00"
    # Local time (HH:MM) after which the day's routed project posts are sent, once per day.
    routed_post_time: str = "18:30"

    # Employee and leave data (local files until directory / calendar access is approved)
    employees_file: Path = Path("config/employees.json")
    leave_file: Path | None = Path("config/leave.json")
    holidays_file: Path | None = Path("config/holidays.json")

    # Database (dedicated database for this project; never point at an existing one)
    # Example: postgresql+psycopg://dtb_app:<password>@localhost:5432/dtb_dev
    database_url: SecretStr | None = None
    # Tests run only against a URL whose database name contains "test".
    test_database_url: SecretStr | None = None

    # Scheduling and run behaviour
    timezone: str = "Asia/Kolkata"
    run_secret: SecretStr | None = None
    dry_run: bool = True  # safe default: never post to Chat unless explicitly disabled
    # All external writes (Chat posts now; Jibble and OpenProject later) stay off until approved.
    allow_external_writes: bool = False


PLACEHOLDER_SUFFIXES = (".example.com", ".example.org", ".example.net")


def jibble_configuration_problems(settings: "Settings") -> list[str]:
    """Names of missing Jibble settings. Values are never included. No network calls."""
    problems: list[str] = []
    if settings.jibble_client_id is None:
        problems.append("DTB_JIBBLE_CLIENT_ID is missing")
    if settings.jibble_client_secret is None:
        problems.append("DTB_JIBBLE_CLIENT_SECRET is missing")
    for name, value in (("DTB_JIBBLE_TOKEN_URL", settings.jibble_token_url),
                        ("DTB_JIBBLE_PEOPLE_BASE_URL", settings.jibble_people_base_url),
                        ("DTB_JIBBLE_TIME_ENTRIES_BASE_URL", settings.jibble_time_entries_base_url)):
        if not value:
            problems.append(f"{name} is missing (use the URL from Jibble's official API docs)")
        elif not value.startswith("https://"):
            problems.append(f"{name} must start with https://")
    return problems


def live_configuration_problems(settings: "Settings") -> list[str]:
    """List every setting a live run needs that is missing or still a placeholder.

    Pure check: no network calls. An empty list means the live configuration looks complete.
    """
    problems: list[str] = []

    if settings.openproject_api_key is None:
        problems.append("DTB_OPENPROJECT_API_KEY is missing")

    base_url = settings.openproject_base_url.strip()
    if not base_url.startswith("https://"):
        problems.append("DTB_OPENPROJECT_BASE_URL must start with https://")
    host = urlparse(base_url).hostname or ""
    if not host or host.endswith(PLACEHOLDER_SUFFIXES):
        problems.append("DTB_OPENPROJECT_BASE_URL is missing or still a placeholder")

    if settings.gchat_service_account_file is None:
        problems.append("DTB_GCHAT_SERVICE_ACCOUNT_FILE is missing")
    elif not settings.gchat_service_account_file.is_file():
        problems.append("DTB_GCHAT_SERVICE_ACCOUNT_FILE does not point to an existing file")

    if settings.run_secret is None:
        problems.append("DTB_RUN_SECRET is missing")

    return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
