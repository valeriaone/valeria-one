"""ask01 (v0.39.0): owner asks — «anything else you want me to do, or something special in your business?»

owner_asks   one row per answer an owner gave on Today (their words + the business's trade), or «Ahora no».

Data integrity: one new table; nothing existing changes, nothing is back-filled.
Downgrade refuses while any answer exists (it never deletes what an owner told us).

Revision ID: b5e2a8c4d9f3
Revises: a9c4e7d2b8f1
Create Date: 2026-11-07 12:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b5e2a8c4d9f3'
down_revision: Union[str, Sequence[str], None] = 'a9c4e7d2b8f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('owner_asks',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('industry', sa.String(length=40), nullable=False),
    sa.Column('text', sa.Text(), server_default='', nullable=False),
    sa.Column('status', sa.String(length=12), server_default='new', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_owner_asks_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_owner_asks_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_owner_asks'))
    )


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("select count(*) from owner_asks")).scalar()
    if n:
        raise RuntimeError(f"refusing to drop owner_asks: {n} answer(s) exist")
    op.drop_table('owner_asks')
