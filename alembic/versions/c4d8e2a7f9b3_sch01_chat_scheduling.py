"""sch01 (v0.23.0): a time picked in the chat lands in both calendars

appointments.conversation_id: the chat a time was picked in (so the customer can move or cancel it there, and the owner
is asked afterwards whether they came); appointments.item_id: the Marketplace item it is for (so "they came" can offer
Mark sold); appointments.sequence: the calendar file's SEQUENCE, +1 on every move or cancel, so the customer's phone and
the owner's calendar feed replace the event instead of adding a second one. memberships.feed_gen: the version of each
person's private calendar link (+1 = a new link; the old one stops working).

Data integrity: nothing existing changes (new columns are empty / 0); nothing is deleted. No new tool, so no "holds data"
pin.

Revision ID: c4d8e2a7f9b3
Revises: e3a9c7d1b5f2
Create Date: 2026-10-30 23:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4d8e2a7f9b3'
down_revision: Union[str, Sequence[str], None] = 'e3a9c7d1b5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('appointments', sa.Column('conversation_id', sa.UUID(), nullable=True))
    op.add_column('appointments', sa.Column('item_id', sa.UUID(), nullable=True))
    op.add_column('appointments', sa.Column('sequence', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.create_foreign_key(op.f('fk_appointments_conversation_id_conversations'), 'appointments', 'conversations',
                          ['conversation_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_appointments_item_id_marketplace_items'), 'appointments', 'marketplace_items',
                          ['item_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_appointments_conv', 'appointments', ['conversation_id'], unique=False,
                    postgresql_where=sa.text('conversation_id IS NOT NULL'))
    op.add_column('memberships', sa.Column('feed_gen', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('memberships', 'feed_gen')
    op.drop_index('ix_appointments_conv', table_name='appointments', postgresql_where=sa.text('conversation_id IS NOT NULL'))
    op.drop_constraint(op.f('fk_appointments_item_id_marketplace_items'), 'appointments', type_='foreignkey')
    op.drop_constraint(op.f('fk_appointments_conversation_id_conversations'), 'appointments', type_='foreignkey')
    op.drop_column('appointments', 'sequence')
    op.drop_column('appointments', 'item_id')
    op.drop_column('appointments', 'conversation_id')
