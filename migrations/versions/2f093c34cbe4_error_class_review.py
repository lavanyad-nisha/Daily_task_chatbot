"""error class review

Revision ID: 2f093c34cbe4
Revises: f800e612ec3b
Create Date: 2026-11-19 13:52:26.729655

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f093c34cbe4'
down_revision: Union[str, Sequence[str], None] = 'f800e612ec3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    last_error_class must accept 'review' (authentication, permission and ambiguous states).
    The CHECK is replaced because Postgres cannot alter a CHECK expression in place.
    """
    op.drop_constraint(op.f("ck_external_sync_records_error_class_valid"), "external_sync_records", type_="check")
    op.create_check_constraint(
        op.f("ck_external_sync_records_error_class_valid"),
        "external_sync_records",
        "last_error_class IS NULL OR last_error_class IN ('retryable', 'permanent', 'review')",
    )


def downgrade() -> None:
    """Downgrade schema. Fails if any row already has last_error_class = 'review'."""
    op.drop_constraint(op.f("ck_external_sync_records_error_class_valid"), "external_sync_records", type_="check")
    op.create_check_constraint(
        op.f("ck_external_sync_records_error_class_valid"),
        "external_sync_records",
        "last_error_class IS NULL OR last_error_class IN ('retryable', 'permanent')",
    )
