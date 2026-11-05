"""claim01 (v0.32.0): claim kits — a business set up ahead of time (its sample site, its demo) waiting for its owner

claim_kits   one row per business we prepared: the claim link /claim/<code> signs the owner up with the name, trade,
             website, services and prices, booking page and tools already in place. One use; then it points at the account.

Data integrity: one new table; nothing existing changes, nothing is back-filled.
Downgrade refuses while any kit exists (it never deletes a prepared business or the record of a claim).

Revision ID: a9c4e7d2b8f1
Revises: d7a3c9e2f4b1
Create Date: 2026-11-05 03:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a9c4e7d2b8f1'
down_revision: Union[str, Sequence[str], None] = 'd7a3c9e2f4b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('claim_kits',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('kit', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('business_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_claim_kits_business_id_businesses'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_claim_kits')),
    sa.UniqueConstraint('code', name=op.f('uq_claim_kits_code'))
    )


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("select count(*) from claim_kits")).scalar()
    if n:
        raise RuntimeError(f"refusing to drop claim_kits: {n} kit(s) exist")
    op.drop_table('claim_kits')
