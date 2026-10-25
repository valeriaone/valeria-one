"""easy sign-up: continue with Facebook / Google, and the "Valeria One" line on the first AI reply

Revision ID: b8e4d2a6c1f0
Revises: a7f3c1e9b2d4
Create Date: 2026-10-25 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b8e4d2a6c1f0'
down_revision: Union[str, Sequence[str], None] = 'a7f3c1e9b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_logins',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'subject'),
    )
    op.create_index(op.f('ix_user_logins_user_id'), 'user_logins', ['user_id'], unique=False)
    op.add_column('businesses', sa.Column('powered_tag', sa.Boolean(), server_default=sa.text('true'), nullable=False))


def downgrade() -> None:
    op.drop_column('businesses', 'powered_tag')
    op.drop_index(op.f('ix_user_logins_user_id'), table_name='user_logins')
    op.drop_table('user_logins')
