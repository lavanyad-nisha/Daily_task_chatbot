"""Core domain models. Pure data; no I/O."""

from datetime import date, datetime

from pydantic import BaseModel, Field


class WorkPackage(BaseModel):
    """An OpenProject work package, reduced to the fields the summary needs."""

    id: int
    subject: str
    project_name: str | None = None
    status_name: str
    status_is_closed: bool
    priority_name: str | None = None
    assignee_id: int | None = None
    # Dates, estimates and completion % are deliberately not modelled: the OpenProject values
    # are stale, so they must not drive summaries or the DSR form.


class ChatMessage(BaseModel):
    """A Google Chat message, reduced to what matching needs."""

    space: str
    sender_id: str | None = None
    created_at: datetime
    text: str


class Employee(BaseModel):
    """Mapping across the three systems. Maintained as a config file."""

    name: str
    email: str
    openproject_user_id: int
    chat_user_id: str | None = None  # e.g. "users/1234567890"


class LeaveWindow(BaseModel):
    """An inclusive date range during which an employee is on leave."""

    start: date
    end: date
    half_day: bool = False

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


class LeaveState(BaseModel):
    """What the leave rules say about one employee on one date."""

    on_leave: bool = False
    returning_today: bool = False
    leave_end: date | None = None


class ChatTaskUpdate(BaseModel):
    """A Chat message that refers to one or more work package IDs."""

    work_package_id: int
    message: ChatMessage


class EmployeeSummary(BaseModel):
    employee: Employee
    run_date: date
    leave: LeaveState = Field(default_factory=LeaveState)
    assigned: list[WorkPackage] = Field(default_factory=list)  # open tasks, shown with status
    needs_cover: list[WorkPackage] = Field(default_factory=list)  # open tasks while on leave
    chat_updates: list[ChatTaskUpdate] = Field(default_factory=list)
