"""mkt01 (v0.22.0): the Marketplace view — things sold on Facebook Marketplace (as the business's Page) and their buyers

marketplace_items: a listing — its own price, pickup area / delivery, notes for Valeria, Facebook's listing number when
known, open / paused / sold — linked to the business's own product (catalog_items, Library / Inventory) when it was
picked from there; never a second products table. marketplace_buyers: one Messenger chat = one buyer, the item they
asked about (NULL until known), a short number for the owner's chat ("#3"), and where they stand (asking, offered,
coming, bought, told it's sold, couldn't be told). conversations.source: where a chat came from ("marketplace").

Data integrity: nothing existing changes (conversations gets one column, empty); nothing is deleted. The new tool's
"holds data" SQL (kits.REGISTRY["marketplace"].uses) is frozen below like v0.19's, v0.20's and v0.21's; no business holds
marketplace rows yet, so the pin changes nothing today — it is here so the copies keep matching the registry.

Revision ID: e3a9c7d1b5f2
Revises: b7d41e9c2a63
Create Date: 2026-10-30 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e3a9c7d1b5f2'
down_revision: Union[str, Sequence[str], None] = 'b7d41e9c2a63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rows(table: str, where: str = "") -> str:
    return f"SELECT 1 FROM {table} x WHERE x.business_id = b.id{' AND x.' + where if where else ''}"


USES = {
    "marketplace": [_rows("marketplace_items")],
}


def pin_statements() -> list[str]:
    """One UPDATE per new tool: pin it ON for every business that holds data in it (tests run these too)."""
    return [f"UPDATE businesses b SET tools = b.tools || jsonb_build_object('{key}', true) WHERE EXISTS({' UNION ALL '.join(uses)})"
            for key, uses in USES.items()]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('marketplace_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('catalog_item_id', sa.UUID(), nullable=True),
    sa.Column('file_id', sa.UUID(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('icon', sa.String(length=16), server_default='', nullable=False),
    sa.Column('price_minor', sa.BigInteger(), nullable=True),
    sa.Column('pickup', sa.String(length=200), server_default='', nullable=False),
    sa.Column('delivery', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('listing_id', sa.String(length=40), nullable=True),
    sa.Column('status', sa.String(length=8), server_default='open', nullable=False),
    sa.Column('sold_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('open','paused','sold')", name=op.f('ck_marketplace_items_status')),
    sa.CheckConstraint('price_minor IS NULL OR price_minor >= 0', name=op.f('ck_marketplace_items_price')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_marketplace_items_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['catalog_item_id'], ['catalog_items.id'], name=op.f('fk_marketplace_items_catalog_item_id_catalog_items'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_marketplace_items_file_id_files'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_marketplace_items'))
    )
    op.create_index('ix_marketplace_items_biz', 'marketplace_items', ['business_id', 'status'], unique=False)
    op.create_table('marketplace_buyers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('item_id', sa.UUID(), nullable=True),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('ref', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='asking', nullable=False),
    sa.Column('offer_minor', sa.BigInteger(), nullable=True),
    sa.Column('coming', sa.String(length=60), server_default='', nullable=False),
    sa.Column('lang', sa.String(length=10), nullable=True),
    sa.Column('asked_which_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('asking','offered','coming','bought','told_sold','not_told')", name=op.f('ck_marketplace_buyers_status')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_marketplace_buyers_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_marketplace_buyers_contact_id_contacts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_marketplace_buyers_conversation_id_conversations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['item_id'], ['marketplace_items.id'], name=op.f('fk_marketplace_buyers_item_id_marketplace_items'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_marketplace_buyers')),
    sa.UniqueConstraint('business_id', 'ref', name='uq_marketplace_buyer_ref'),
    sa.UniqueConstraint('conversation_id', name=op.f('uq_marketplace_buyers_conversation_id'))
    )
    op.create_index('ix_marketplace_buyers_item', 'marketplace_buyers', ['business_id', 'item_id'], unique=False)
    op.add_column('conversations', sa.Column('source', sa.String(length=20), nullable=True))
    for sql in pin_statements():                         # what a business already uses never leaves its menu
        op.execute(sql)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('conversations', 'source')
    op.drop_index('ix_marketplace_buyers_item', table_name='marketplace_buyers')
    op.drop_table('marketplace_buyers')
    op.drop_index('ix_marketplace_items_biz', table_name='marketplace_items')
    op.drop_table('marketplace_items')
