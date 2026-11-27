"""T2 schema: employees, daily plans, task-wise DSRs and Jibble sync logs.

Design notes:
- Dates, estimates and completion % from OpenProject are stale and are not stored here.
- Status fields are text with CHECK constraints (not PostgreSQL ENUMs), so later changes are
  simple Alembic migrations.
- OpenProject owns task identity and status. We store a snapshot per plan (daily_plan_items)
  and reference work packages by their OpenProject integer ID. We never write to OpenProject.
- A DSR is versioned per employee and date. Only one version is current at a time, which
  allows corrections before the Jibble sync. Superseded versions are kept for audit.
- Each DSR entry has one sync record per target (OpenProject time, Jibble time). Each record
  has its own status, retry schedule and unique idempotency key, so retries cannot duplicate writes.
- Jibble project/activity references are not modelled yet. They need the Jibble API details
  (see the T2 blockers list) and will be added in T9.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from daily_task_bot.db.base import Base

PLAN_STATUSES = ("pending", "sent", "failed", "skipped_leave", "skipped_holiday")
SUBMISSION_STATUSES = ("draft", "confirmed", "superseded")
SYNC_STATUSES = ("pending", "in_progress", "sent", "retry_wait", "failed_permanent", "needs_review")
SYNC_TARGETS = ("openproject_time", "openproject_status", "jibble_time")
ERROR_CLASSES = ("retryable", "permanent", "review")


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # onupdate uses clock_timestamp(): now() returns the transaction start time, which would
    # not change within a single transaction. This is ORM-level, so it needs no migration.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.clock_timestamp(),
        nullable=False,
    )


class Employee(TimestampMixin, Base):
    """One row per person. Identifiers from the three systems are stored here."""

    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    openproject_user_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    chat_user_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    jibble_person_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    plans: Mapped[list["DailyPlan"]] = relationship(back_populates="employee")


class DailyPlan(TimestampMixin, Base):
    """The morning task list for one employee on one date."""

    __tablename__ = "daily_plans"
    __table_args__ = (
        UniqueConstraint("employee_id", "plan_date", name="uq_daily_plans_employee_date"),
        CheckConstraint(_sql_in("status", PLAN_STATUSES), name="status_valid"),
        Index("ix_daily_plans_plan_date", "plan_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"))
    plan_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    on_leave: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    chat_message_name: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    employee: Mapped[Employee] = relationship(back_populates="plans")
    items: Mapped[list["DailyPlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class DailyPlanItem(Base):
    """Snapshot of one OpenProject work package as shown in a plan."""

    __tablename__ = "daily_plan_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "work_package_id", name="uq_daily_plan_items_plan_wp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("daily_plans.id", ondelete="CASCADE"), index=True
    )
    work_package_id: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(Text)
    status_name: Mapped[str] = mapped_column(String(100))

    plan: Mapped[DailyPlan] = relationship(back_populates="items")


class ReminderSent(Base):
    """One row per reminder sent. The unique key stops duplicates across restarts."""

    __tablename__ = "reminders_sent"
    __table_args__ = (
        UniqueConstraint("employee_id", "kind", "plan_date", name="uq_reminders_employee_kind_day"),
        CheckConstraint("kind IN ('morning', 'evening')", name="kind_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(20))
    plan_date: Mapped[date] = mapped_column(Date)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrSubmission(TimestampMixin, Base):
    """A versioned daily status report for one employee and one date."""

    __tablename__ = "dsr_submissions"
    __table_args__ = (
        UniqueConstraint("employee_id", "dsr_date", "version", name="uq_dsr_employee_date_version"),
        CheckConstraint(_sql_in("status", SUBMISSION_STATUSES), name="status_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(is_current AND status <> 'superseded') OR (NOT is_current AND status = 'superseded')",
            name="current_matches_status",
        ),
        # A draft has never been confirmed. Confirmed and superseded versions keep confirmed_at.
        CheckConstraint(
            "status <> 'draft' OR confirmed_at IS NULL", name="draft_not_confirmed"
        ),
        # At most one current version per employee per date.
        Index(
            "uq_dsr_one_current_per_day",
            "employee_id",
            "dsr_date",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"))
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("daily_plans.id", ondelete="SET NULL")
    )
    dsr_date: Mapped[date] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    status: Mapped[str] = mapped_column(String(20), server_default="draft")
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    entries: Mapped[list["DsrEntry"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )


class DsrEntry(Base):
    """What the employee did on one assigned task during the day."""

    __tablename__ = "dsr_entries"
    __table_args__ = (
        UniqueConstraint("submission_id", "work_package_id", name="uq_dsr_entries_sub_wp"),
        CheckConstraint("hours_spent > 0 AND hours_spent <= 24", name="hours_range"),
        CheckConstraint("new_status_id IS NULL OR new_status_id > 0", name="status_id_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("dsr_submissions.id", ondelete="CASCADE"), index=True
    )
    work_package_id: Mapped[int] = mapped_column(Integer)
    # Hours worked on this task today. Synced to OpenProject time entries and to Jibble.
    hours_spent: Mapped[Decimal] = mapped_column(Numeric(4, 2))
    # The employee's description of the work. The task title is read from OpenProject, not stored.
    work_done: Mapped[str] = mapped_column(Text)
    remaining_work: Mapped[str | None] = mapped_column(Text)
    # Optional status the employee chose for this task. Must be in the configured choice list.
    new_status_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    submission: Mapped[DsrSubmission] = relationship(back_populates="entries")
    sync_records: Mapped[list["ExternalSyncRecord"]] = relationship(
        back_populates="entry", cascade="save-update, merge"
    )


class ExternalSyncRecord(TimestampMixin, Base):
    """Independent sync state for one DSR entry and one target system.

    Each target (OpenProject time entry, Jibble time entry) succeeds or fails on its own.
    The idempotency key is unique and does not change when the DSR is corrected, so a
    correction updates the existing external record instead of creating a duplicate.
    """

    __tablename__ = "external_sync_records"
    __table_args__ = (
        UniqueConstraint("dsr_entry_id", "target", name="uq_external_sync_entry_target"),
        UniqueConstraint("idempotency_key", name="uq_external_sync_idempotency_key"),
        CheckConstraint(_sql_in("target", SYNC_TARGETS), name="target_valid"),
        CheckConstraint(_sql_in("status", SYNC_STATUSES), name="status_valid"),
        CheckConstraint(
            f"last_error_class IS NULL OR {_sql_in('last_error_class', ERROR_CLASSES)}",
            name="error_class_valid",
        ),
        CheckConstraint("attempt_count >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="sent_at_matches_status"),
        CheckConstraint(
            "status <> 'in_progress' OR locked_until IS NOT NULL", name="in_progress_is_locked"
        ),
        Index("ix_external_sync_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dsr_entry_id: Mapped[int] = mapped_column(
        ForeignKey("dsr_entries.id", ondelete="RESTRICT")
    )
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"))
    target: Mapped[str] = mapped_column(String(30))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    # SHA-256 of the payload last sent. A retry with a different hash is an update, not a new create.
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error_class: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    entry: Mapped[DsrEntry] = relationship(back_populates="sync_records")


class EmployeeProjectRole(TimestampMixin, Base):
    """An employee's OpenProject role on one project. Refreshed from memberships (read-only)."""

    __tablename__ = "employee_project_roles"
    __table_args__ = (
        UniqueConstraint("employee_id", "openproject_project_id", "role_name",
                         name="uq_employee_project_role"),
        CheckConstraint("length(trim(role_name)) > 0", name="role_name_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    openproject_project_id: Mapped[int] = mapped_column(Integer)
    role_name: Mapped[str] = mapped_column(String(100))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectRoute(TimestampMixin, Base):
    """Which Chat space receives a project's content for one role on that project."""

    __tablename__ = "project_routes"
    __table_args__ = (
        UniqueConstraint("openproject_project_id", "role_name", name="uq_project_routes_project_role"),
        CheckConstraint("chat_space_id LIKE 'spaces/%'", name="space_id_format"),
        CheckConstraint("length(trim(role_name)) > 0", name="route_role_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    openproject_project_id: Mapped[int] = mapped_column(Integer)
    role_name: Mapped[str] = mapped_column(String(100))
    chat_space_id: Mapped[str] = mapped_column(String(100))
    space_label: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class RoutedPost(TimestampMixin, Base):
    """One message per employee, per Chat space, per day. Written only after a real post."""

    __tablename__ = "routed_posts"
    __table_args__ = (
        UniqueConstraint("employee_id", "space_id", "post_date", name="uq_routed_posts_employee_space_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"))
    space_id: Mapped[str] = mapped_column(String(100))
    post_date: Mapped[date] = mapped_column(Date)
    message_name: Mapped[str | None] = mapped_column(String(200))
    # How each summary in this post was made. AI first; rule-based only for a summary the AI failed on.
    ai_summaries: Mapped[int] = mapped_column(Integer, server_default="0")
    rule_based_summaries: Mapped[int] = mapped_column(Integer, server_default="0")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SpaceLink(TimestampMixin, Base):
    """Which client a Chat space belongs to. One client per space and one space per client;
    an unlinked space gets nothing."""

    __tablename__ = "space_links"
    __table_args__ = (
        CheckConstraint("length(trim(client_name)) > 0", name="client_name_not_blank"),
        UniqueConstraint("client_name", name="uq_space_links_client_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[str] = mapped_column(String(100), unique=True)
    client_name: Mapped[str] = mapped_column(String(100))
    # The client's OpenProject parent project (sub-projects are resolved from it in T2).
    openproject_project_id: Mapped[int | None] = mapped_column(Integer)
    linked_by: Mapped[str] = mapped_column(String(100))
