"""openproject status target and dsr status choice

Revision ID: 3cb226a98057
Revises: 2f093c34cbe4
Create Date: 2026-11-19 16:04:21.112798

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3cb226a98057'
down_revision: Union[str, Sequence[str], None] = '2f093c34cbe4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    - Adds the openproject_status sync target (the target CHECK is replaced).
    - Adds dsr_entries.new_status_id, the optional status the employee chose.
    """
    op.drop_constraint(op.f("ck_external_sync_records_target_valid"), "external_sync_records", type_="check")
    op.create_check_constraint(
        op.f("ck_external_sync_records_target_valid"),
        "external_sync_records",
        "target IN ('openproject_time', 'openproject_status', 'jibble_time')",
    )
    op.add_column("dsr_entries", sa.Column("new_status_id", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_dsr_entries_status_id_positive"),
        "dsr_entries",
        "new_status_id IS NULL OR new_status_id > 0",
    )


def downgrade() -> None:
    """Downgrade schema. Fails if any openproject_status records or status choices exist."""
    op.drop_constraint(op.f("ck_dsr_entries_status_id_positive"), "dsr_entries", type_="check")
    op.drop_column("dsr_entries", "new_status_id")
    op.drop_constraint(op.f("ck_external_sync_records_target_valid"), "external_sync_records", type_="check")
    op.create_check_constraint(
        op.f("ck_external_sync_records_target_valid"),
        "external_sync_records",
        "target IN ('openproject_time', 'jibble_time')",
    )
