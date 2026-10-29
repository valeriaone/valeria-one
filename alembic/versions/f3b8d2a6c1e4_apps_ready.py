"""apps ready

Revision ID: f3b8d2a6c1e4
Revises: e5c2a8d4f1b7
Create Date: 2026-10-29 02:40:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f3b8d2a6c1e4'
down_revision: Union[str, Sequence[str], None] = 'e5c2a8d4f1b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('businesses', sa.Column('apps_ready', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"),
                                          nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('businesses', 'apps_ready')
