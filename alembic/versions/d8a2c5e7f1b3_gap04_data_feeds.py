"""gap04 (v0.42.0): data feeds — a private live CSV link per business (contacts or deals) for Google Sheets' IMPORTDATA.

data_feeds   one row per link: kind, a short prefix to tell links apart, the SHA-256 of its token (the token itself is never
             kept), active, uses, last_used_at.

Data integrity: one new table; nothing existing changes, nothing is back-filled.
Downgrade refuses while any feed exists (it never deletes a link an owner made).

Revision ID: d8a2c5e7f1b3
Revises: c6f3b9d1e8a4
Create Date: 2026-11-07 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd8a2c5e7f1b3'
down_revision: Union[str, Sequence[str], None] = 'c6f3b9d1e8a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('data_feeds',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('prefix', sa.String(length=12), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('uses', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('contacts','deals')", name=op.f('ck_data_feeds_kind')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_data_feeds_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_feeds')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_data_feeds_token_hash'))
    )


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("select count(*) from data_feeds")).scalar()
    if n:
        raise RuntimeError(f"refusing to drop data_feeds: {n} feed(s) exist")
    op.drop_table('data_feeds')
