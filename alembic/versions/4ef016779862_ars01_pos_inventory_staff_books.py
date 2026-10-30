"""ars01 (v0.20.0): point of sale, inventory, staff (schedule, clock in/out, timesheets) and books

Inventory is the business's own items (catalog_items, the Library's table) — no second products table: an item listed in
Inventory carries in_inventory, its stock count, its "running low" mark and, optionally, the item it is a variant of.
Every change to a count is a stock_moves row (why, by whom, from which sale). pos_sales: each sale at the counter,
numbered per business, amounts in the smallest currency unit, never deleted (void / refunded are marks); pos_days: the
end-of-day cash count. staff_members / shifts / clock_entries: the team, the schedule, the hours (an owner's fix keeps
the old times in history). book_entries: expenses and other money in, written by hand — sales, paid invoices and pay
links are read where they already are, never copied. businesses.pos_settings: the register's own tax when the owner sets one.

Data integrity: nothing existing changes; nothing is deleted. The tools' "holds data" SQL (kits.REGISTRY[*].uses) for the
four new tools is frozen below like v0.19's; at this upgrade no business holds data in them yet, so the pins change
nothing — they are here so this release's copy is complete.

Revision ID: 4ef016779862
Revises: a424ed548d9d
Create Date: 2026-10-30 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4ef016779862'
down_revision: Union[str, Sequence[str], None] = 'a424ed548d9d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rows(table: str, where: str = "") -> str:
    return f"SELECT 1 FROM {table} x WHERE x.business_id = b.id{' AND x.' + where if where else ''}"


USES = {
    "pos": [_rows("pos_sales")],
    "inventory": [_rows("catalog_items", "in_inventory"), _rows("stock_moves")],
    "staff": [_rows("staff_members"), _rows("shifts")],
    "books": [_rows("book_entries")],
}


def pin_statements() -> list[str]:
    """One UPDATE per new tool: pin it ON for every business that holds data in it (tests run these too)."""
    return [f"UPDATE businesses b SET tools = b.tools || jsonb_build_object('{key}', true) WHERE EXISTS({' UNION ALL '.join(uses)})"
            for key, uses in USES.items()]


def _uuid(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.UUID(), nullable=nullable)


def _id() -> sa.Column:
    return sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False)


def _ts(name: str = 'created_at') -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)


def _biz_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f(f'fk_{table}_business_id_businesses'),
                                   ondelete='CASCADE')


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('businesses', sa.Column('pos_settings', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"),
                                          nullable=False))
    op.add_column('catalog_items', sa.Column('in_inventory', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('catalog_items', sa.Column('stock', sa.Integer(), nullable=True))
    op.add_column('catalog_items', sa.Column('low_at', sa.Integer(), nullable=True))
    op.add_column('catalog_items', _uuid('parent_id'))
    op.add_column('catalog_items', sa.Column('variant', sa.String(length=100), server_default='', nullable=False))
    op.create_foreign_key(op.f('fk_catalog_items_parent_id_catalog_items'), 'catalog_items', 'catalog_items', ['parent_id'], ['id'],
                          ondelete='SET NULL')
    op.create_check_constraint(op.f('ck_catalog_items_low_at'), 'catalog_items', 'low_at IS NULL OR low_at >= 0')
    op.create_index('ix_catalog_items_inventory', 'catalog_items', ['business_id', 'in_inventory'], unique=False)

    op.create_table('staff_members', _id(), _uuid('business_id', False), _uuid('user_id'),
                    sa.Column('name', sa.String(length=200), nullable=False),
                    sa.Column('phone', sa.String(length=30), server_default='', nullable=False),
                    sa.Column('job', sa.String(length=80), server_default='', nullable=False),
                    sa.Column('hourly_minor', sa.BigInteger(), nullable=True),
                    sa.Column('per_shift_minor', sa.BigInteger(), nullable=True),
                    sa.Column('commission_bp', sa.Integer(), nullable=True),
                    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
                    _ts(),
                    sa.CheckConstraint('hourly_minor IS NULL OR hourly_minor >= 0', name=op.f('ck_staff_members_hourly')),
                    sa.CheckConstraint('per_shift_minor IS NULL OR per_shift_minor >= 0', name=op.f('ck_staff_members_per_shift')),
                    sa.CheckConstraint('commission_bp IS NULL OR commission_bp BETWEEN 0 AND 10000',
                                       name=op.f('ck_staff_members_commission')),
                    _biz_fk('staff_members'),
                    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_staff_members_user_id_users'), ondelete='SET NULL'),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_staff_members')))
    op.create_index('uq_staff_members_user', 'staff_members', ['business_id', 'user_id'], unique=True,
                    postgresql_where=sa.text('user_id IS NOT NULL'))

    op.create_table('pos_sales', _id(), _uuid('business_id', False),
                    sa.Column('number', sa.Integer(), nullable=False),
                    _uuid('contact_id'), _uuid('staff_id'),
                    sa.Column('lines', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
                    sa.Column('currency', sa.String(length=3), nullable=False),
                    sa.Column('subtotal_minor', sa.BigInteger(), nullable=False),
                    sa.Column('discount_minor', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
                    sa.Column('tax_minor', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
                    sa.Column('total_minor', sa.BigInteger(), nullable=False),
                    sa.Column('tax_name', sa.String(length=60), server_default='', nullable=False),
                    sa.Column('tax_rate_bp', sa.Integer(), server_default=sa.text('0'), nullable=False),
                    sa.Column('tax_included', sa.Boolean(), server_default=sa.text('true'), nullable=False),
                    sa.Column('method', sa.String(length=8), nullable=False),
                    sa.Column('cash_given_minor', sa.BigInteger(), nullable=True),
                    sa.Column('change_minor', sa.BigInteger(), nullable=True),
                    _uuid('payment_id'),
                    sa.Column('status', sa.String(length=8), nullable=False),
                    sa.Column('token', sa.String(length=40), nullable=False),
                    sa.Column('note', sa.String(length=300), server_default='', nullable=False),
                    sa.Column('day', sa.Date(), nullable=False),
                    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
                    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
                    sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
                    sa.Column('refund_day', sa.Date(), nullable=True),
                    sa.Column('undo_reason', sa.String(length=300), server_default='', nullable=False),
                    sa.Column('created_by', sa.String(length=100), nullable=False),
                    _ts(),
                    sa.CheckConstraint("method IN ('cash','link','card','other')", name=op.f('ck_pos_sales_method')),
                    sa.CheckConstraint("status IN ('paid','pending','void','refunded')", name=op.f('ck_pos_sales_status')),
                    sa.CheckConstraint('subtotal_minor >= 0 AND discount_minor >= 0 AND discount_minor <= subtotal_minor AND tax_minor >= 0 '
                                       'AND total_minor >= 0', name=op.f('ck_pos_sales_amounts')),
                    sa.CheckConstraint('change_minor IS NULL OR change_minor >= 0', name=op.f('ck_pos_sales_change')),
                    _biz_fk('pos_sales'),
                    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_pos_sales_contact_id_contacts'),
                                            ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['staff_id'], ['staff_members.id'], name=op.f('fk_pos_sales_staff_id_staff_members'),
                                            ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], name=op.f('fk_pos_sales_payment_id_payments'),
                                            ondelete='SET NULL'),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_pos_sales')),
                    sa.UniqueConstraint('business_id', 'number', name='uq_pos_sale_number'),
                    sa.UniqueConstraint('token', name=op.f('uq_pos_sales_token')))
    op.create_index('ix_pos_sales_biz_day', 'pos_sales', ['business_id', 'day'], unique=False)

    op.create_table('pos_days', _id(), _uuid('business_id', False),
                    sa.Column('day', sa.Date(), nullable=False),
                    sa.Column('float_minor', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
                    sa.Column('expected_minor', sa.BigInteger(), nullable=False),
                    sa.Column('counted_minor', sa.BigInteger(), nullable=False),
                    sa.Column('totals', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
                    sa.Column('note', sa.String(length=300), server_default='', nullable=False),
                    sa.Column('closed_by', sa.String(length=100), nullable=False),
                    _ts(),
                    sa.CheckConstraint('float_minor >= 0 AND counted_minor >= 0', name=op.f('ck_pos_days_amounts')),
                    _biz_fk('pos_days'),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_pos_days')))

    op.create_table('stock_moves', _id(), _uuid('business_id', False), _uuid('item_id', False),
                    sa.Column('delta', sa.Integer(), nullable=False),
                    sa.Column('stock_after', sa.Integer(), nullable=False),
                    sa.Column('reason', sa.String(length=10), nullable=False),
                    _uuid('sale_id'),
                    sa.Column('note', sa.String(length=300), server_default='', nullable=False),
                    sa.Column('created_by', sa.String(length=100), nullable=False),
                    _ts(),
                    sa.CheckConstraint("reason IN ('start','received','damaged','count','sale','refund','void','import')",
                                       name=op.f('ck_stock_moves_reason')),
                    _biz_fk('stock_moves'),
                    sa.ForeignKeyConstraint(['item_id'], ['catalog_items.id'], name=op.f('fk_stock_moves_item_id_catalog_items'),
                                            ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['sale_id'], ['pos_sales.id'], name=op.f('fk_stock_moves_sale_id_pos_sales'),
                                            ondelete='SET NULL'),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_stock_moves')))
    op.create_index('ix_stock_moves_item', 'stock_moves', ['item_id', 'created_at'], unique=False)

    op.create_table('shifts', _id(), _uuid('business_id', False), _uuid('staff_id', False),
                    sa.Column('day', sa.Date(), nullable=False),
                    sa.Column('starts', sa.Time(), nullable=False),
                    sa.Column('ends', sa.Time(), nullable=False),
                    sa.Column('note', sa.String(length=200), server_default='', nullable=False),
                    sa.Column('created_by', sa.String(length=100), nullable=False),
                    _ts(),
                    _biz_fk('shifts'),
                    sa.ForeignKeyConstraint(['staff_id'], ['staff_members.id'], name=op.f('fk_shifts_staff_id_staff_members'),
                                            ondelete='CASCADE'),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_shifts')))
    op.create_index('ix_shifts_biz_day', 'shifts', ['business_id', 'day'], unique=False)

    op.create_table('clock_entries', _id(), _uuid('business_id', False), _uuid('staff_id', False),
                    sa.Column('in_at', sa.DateTime(timezone=True), nullable=False),
                    sa.Column('out_at', sa.DateTime(timezone=True), nullable=True),
                    sa.Column('source', sa.String(length=6), server_default='self', nullable=False),
                    sa.Column('note', sa.String(length=300), server_default='', nullable=False),
                    sa.Column('history', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
                    sa.Column('voided', sa.Boolean(), server_default=sa.text('false'), nullable=False),
                    _ts(),
                    sa.CheckConstraint('out_at IS NULL OR out_at > in_at', name=op.f('ck_clock_entries_order')),
                    sa.CheckConstraint("source IN ('self','owner')", name=op.f('ck_clock_entries_source')),
                    _biz_fk('clock_entries'),
                    sa.ForeignKeyConstraint(['staff_id'], ['staff_members.id'], name=op.f('fk_clock_entries_staff_id_staff_members'),
                                            ondelete='CASCADE'),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_clock_entries')))
    op.create_index('ix_clock_entries_biz_in', 'clock_entries', ['business_id', 'in_at'], unique=False)

    op.create_table('book_entries', _id(), _uuid('business_id', False),
                    sa.Column('direction', sa.String(length=3), nullable=False),
                    sa.Column('on_date', sa.Date(), nullable=False),
                    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
                    sa.Column('currency', sa.String(length=3), nullable=False),
                    sa.Column('category', sa.String(length=20), nullable=False),
                    sa.Column('description', sa.String(length=300), server_default='', nullable=False),
                    _uuid('supplier_id'), _uuid('file_id'),
                    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
                    sa.Column('void_reason', sa.String(length=300), server_default='', nullable=False),
                    sa.Column('created_by', sa.String(length=100), nullable=False),
                    _ts(),
                    sa.CheckConstraint("direction IN ('in','out')", name=op.f('ck_book_entries_direction')),
                    sa.CheckConstraint('amount_minor > 0', name=op.f('ck_book_entries_amount')),
                    _biz_fk('book_entries'),
                    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_book_entries_supplier_id_suppliers'),
                                            ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_book_entries_file_id_files'), ondelete='SET NULL'),
                    sa.PrimaryKeyConstraint('id', name=op.f('pk_book_entries')))
    op.create_index('ix_book_entries_biz_date', 'book_entries', ['business_id', 'on_date'], unique=False)

    for sql in pin_statements():                         # what a business already uses never leaves its menu
        op.execute(sql)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_book_entries_biz_date', table_name='book_entries')
    op.drop_table('book_entries')
    op.drop_index('ix_clock_entries_biz_in', table_name='clock_entries')
    op.drop_table('clock_entries')
    op.drop_index('ix_shifts_biz_day', table_name='shifts')
    op.drop_table('shifts')
    op.drop_index('ix_stock_moves_item', table_name='stock_moves')
    op.drop_table('stock_moves')
    op.drop_table('pos_days')
    op.drop_index('ix_pos_sales_biz_day', table_name='pos_sales')
    op.drop_table('pos_sales')
    op.drop_index('uq_staff_members_user', table_name='staff_members', postgresql_where=sa.text('user_id IS NOT NULL'))
    op.drop_table('staff_members')
    op.drop_index('ix_catalog_items_inventory', table_name='catalog_items')
    op.drop_constraint(op.f('ck_catalog_items_low_at'), 'catalog_items', type_='check')
    op.drop_constraint(op.f('fk_catalog_items_parent_id_catalog_items'), 'catalog_items', type_='foreignkey')
    op.drop_column('catalog_items', 'variant')
    op.drop_column('catalog_items', 'parent_id')
    op.drop_column('catalog_items', 'low_at')
    op.drop_column('catalog_items', 'stock')
    op.drop_column('catalog_items', 'in_inventory')
    op.drop_column('businesses', 'pos_settings')
