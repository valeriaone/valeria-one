"""blocks, words, server admin, instance facts

Revision ID: 7b1e5c2d9a40
Revises: 6cc379c860ef
Create Date: 2026-10-24 19:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '7b1e5c2d9a40'
down_revision: Union[str, Sequence[str], None] = '6cc379c860ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('businesses', sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('businesses', sa.Column('words', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('users', sa.Column('is_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.create_table('instance_kv',
                    sa.Column('key', sa.String(length=60), nullable=False),
                    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
                    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
                    sa.PrimaryKeyConstraint('key', name=op.f('pk_instance_kv')))


def downgrade() -> None:
    op.drop_table('instance_kv')
    op.drop_column('users', 'is_admin')
    op.drop_column('businesses', 'words')
    op.drop_column('businesses', 'features')
