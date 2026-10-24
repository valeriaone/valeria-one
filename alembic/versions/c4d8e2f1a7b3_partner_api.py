"""partner api: keys, suspended businesses, partner refs, server audit

Revision ID: c4d8e2f1a7b3
Revises: 7b1e5c2d9a40
Create Date: 2026-10-24 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c4d8e2f1a7b3'
down_revision: Union[str, Sequence[str], None] = '7b1e5c2d9a40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('businesses', sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('businesses', sa.Column('suspended_reason', sa.String(length=300), nullable=True))
    op.add_column('businesses', sa.Column('partner_ref', sa.String(length=100), nullable=True))
    op.create_unique_constraint(op.f('uq_businesses_partner_ref'), 'businesses', ['partner_ref'])
    op.create_table('partner_keys',
                    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
                    sa.Column('name', sa.String(length=100), nullable=False),
                    sa.Column('prefix', sa.String(length=24), nullable=False),
                    sa.Column('key_hash', sa.String(length=64), nullable=False),
                    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
                    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
                    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_partner_keys')),
                    sa.UniqueConstraint('key_hash', name=op.f('uq_partner_keys_key_hash')))
    op.create_table('instance_events',
                    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
                    sa.Column('type', sa.String(length=60), nullable=False),
                    sa.Column('actor', sa.String(length=100), nullable=False),
                    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
                    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_instance_events')))


def downgrade() -> None:
    op.drop_table('instance_events')
    op.drop_table('partner_keys')
    op.drop_constraint(op.f('uq_businesses_partner_ref'), 'businesses', type_='unique')
    op.drop_column('businesses', 'partner_ref')
    op.drop_column('businesses', 'suspended_reason')
    op.drop_column('businesses', 'suspended_at')
