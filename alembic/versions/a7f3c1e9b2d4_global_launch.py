"""global: launch promo + the business's tax number

Revision ID: a7f3c1e9b2d4
Revises: c62c792ac99f
Create Date: 2026-10-25 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7f3c1e9b2d4'
down_revision: Union[str, Sequence[str], None] = 'c62c792ac99f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('businesses', sa.Column('promo', sa.String(length=20), nullable=True))
    op.add_column('businesses', sa.Column('tax_id', sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column('businesses', 'tax_id')
    op.drop_column('businesses', 'promo')
