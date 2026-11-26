"""space links: one space per client, and the client's OpenProject project

Revision ID: b7e2c9d41f60
Revises: a0f64cfb4d02
Create Date: 2026-11-26 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2c9d41f60'
down_revision: Union[str, Sequence[str], None] = 'a0f64cfb4d02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('space_links', sa.Column('openproject_project_id', sa.Integer(), nullable=True))
    # Backfill rows linked before this column existed (the default client list at the time).
    op.execute("UPDATE space_links SET openproject_project_id = CASE client_name "
               "WHEN 'GPL' THEN 16 WHEN 'L&T Valves' THEN 18 WHEN 'TVS' THEN 27 END "
               "WHERE openproject_project_id IS NULL")
    op.create_unique_constraint(op.f('uq_space_links_client_name'), 'space_links', ['client_name'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('uq_space_links_client_name'), 'space_links', type_='unique')
    op.drop_column('space_links', 'openproject_project_id')
