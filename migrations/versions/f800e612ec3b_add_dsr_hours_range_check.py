"""add dsr hours range check

Revision ID: f800e612ec3b
Revises: 781e0ff71ba5
Create Date: 2026-11-19 13:26:31.959767

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f800e612ec3b'
down_revision: Union[str, Sequence[str], None] = '781e0ff71ba5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    The T2 revision added hours_spent with add_column, which does not create the CHECK
    constraint declared on the model. Create it here.
    """
    op.create_check_constraint(
        op.f("ck_dsr_entries_hours_range"),
        "dsr_entries",
        "hours_spent > 0 AND hours_spent <= 24",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_dsr_entries_hours_range"), "dsr_entries", type_="check")
