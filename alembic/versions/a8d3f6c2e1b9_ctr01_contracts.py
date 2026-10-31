"""ctr01 (v0.24.0): contracts — a business sends its customer a written agreement, signed on the phone

contract_templates: the business's own wording — from its kit (the base contract + the clause blocks its trade switches
on), drafted by Valeria from the owner's own words, typed / pasted, or an uploaded PDF (files; files.remove refuses one a
template or a contract uses). contracts: one agreement for one customer, filled in from the customer and the booking / quote /
invoice it started from; frozen when sent (sha256 of exactly what is signed), a change = a new version (the old one is
voided, so it can't be signed); the signature record (typed name, drawn signature, time, IP, device, the fingerprint
checked again). contract_settings: "a booking is confirmed only after the signed agreement (and the deposit)".

Data integrity: nothing existing changes; nothing is deleted. The new tool's "holds data" SQL
(kits.REGISTRY["contracts"].uses) is frozen below like v0.19–v0.22's; no business holds contract rows yet, so the pin
changes nothing today — it is here so the copies keep matching the registry.

Revision ID: a8d3f6c2e1b9
Revises: c4d8e2a7f9b3
Create Date: 2026-10-31 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a8d3f6c2e1b9'
down_revision: Union[str, Sequence[str], None] = 'c4d8e2a7f9b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rows(table: str, where: str = "") -> str:
    return f"SELECT 1 FROM {table} x WHERE x.business_id = b.id{' AND x.' + where if where else ''}"


USES = {
    "contracts": [_rows("contracts"), _rows("contract_templates")],
}


def pin_statements() -> list[str]:
    """One UPDATE per new tool: pin it ON for every business that holds data in it (tests run these too)."""
    return [f"UPDATE businesses b SET tools = b.tools || jsonb_build_object('{key}', true) WHERE EXISTS({' UNION ALL '.join(uses)})"
            for key, uses in USES.items()]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('contract_templates',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('source', sa.String(length=8), nullable=False),
    sa.Column('lang', sa.String(length=10), nullable=False),
    sa.Column('sections', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=True),
    sa.Column('archived', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('kit','ai','own','upload')", name=op.f('ck_contract_templates_source')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_contract_templates_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_contract_templates_file_id_files')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_templates'))
    )
    op.create_index('ix_contract_templates_biz', 'contract_templates', ['business_id', 'archived'], unique=False)
    op.create_table('contracts',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('template_id', sa.UUID(), nullable=True),
    sa.Column('contact_id', sa.UUID(), nullable=True),
    sa.Column('appointment_id', sa.UUID(), nullable=True),
    sa.Column('invoice_id', sa.UUID(), nullable=True),
    sa.Column('quote_id', sa.UUID(), nullable=True),
    sa.Column('payment_id', sa.UUID(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), server_default='', nullable=False),
    sa.Column('fields', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=True),
    sa.Column('file_sha256', sa.String(length=64), nullable=True),
    sa.Column('lang', sa.String(length=10), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('deposit_minor', sa.BigInteger(), nullable=True),
    sa.Column('deposit_paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=8), server_default='draft', nullable=False),
    sa.Column('doc_hash', sa.String(length=64), nullable=True),
    sa.Column('channel', sa.String(length=20), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('viewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('signed_name', sa.String(length=200), nullable=True),
    sa.Column('signed_ip', sa.String(length=64), nullable=True),
    sa.Column('signed_ua', sa.String(length=300), nullable=True),
    sa.Column('signature', sa.Text(), nullable=True),
    sa.Column('signed_hash', sa.String(length=64), nullable=True),
    sa.Column('declined_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decline_reason', sa.Text(), nullable=True),
    sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft','sent','viewed','signed','declined','voided')", name=op.f('ck_contracts_status')),
    sa.CheckConstraint('deposit_minor IS NULL OR deposit_minor >= 0', name=op.f('ck_contracts_deposit')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_contracts_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['template_id'], ['contract_templates.id'], name=op.f('fk_contracts_template_id_contract_templates'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_contracts_contact_id_contacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['appointment_id'], ['appointments.id'], name=op.f('fk_contracts_appointment_id_appointments'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name=op.f('fk_contracts_invoice_id_invoices'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['quote_id'], ['quotes.id'], name=op.f('fk_contracts_quote_id_quotes'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], name=op.f('fk_contracts_payment_id_payments'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_contracts_file_id_files')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contracts')),
    sa.UniqueConstraint('business_id', 'number', 'version', name='uq_contract_version')
    )
    op.create_index('ix_contracts_biz', 'contracts', ['business_id', 'status'], unique=False)
    op.create_index('ix_contracts_contact', 'contracts', ['business_id', 'contact_id'], unique=False)
    op.create_table('contract_settings',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('require_signed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('require_deposit', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_contract_settings_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('business_id', name=op.f('pk_contract_settings'))
    )
    for sql in pin_statements():                         # what a business already uses never leaves its menu
        op.execute(sql)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('contract_settings')
    op.drop_index('ix_contracts_contact', table_name='contracts')
    op.drop_index('ix_contracts_biz', table_name='contracts')
    op.drop_table('contracts')
    op.drop_index('ix_contract_templates_biz', table_name='contract_templates')
    op.drop_table('contract_templates')
