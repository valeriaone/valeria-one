"""cfdi01 (v0.52.0): Mexican CFDI 4.0 invoices through the business's OWN Facturapi account — its key (encrypted), the stamped
invoice's facts on the invoice, the customer's fiscal data remembered on the contact.

Revision ID: f9a4c2e7b1d3
Revises: e3f7a1b9c2d5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'f9a4c2e7b1d3'
down_revision: Union[str, None] = 'e3f7a1b9c2d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cfdi_accounts",
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("secret", sa.Text(), nullable=False),                       # their Facturapi key, encrypted
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),   # name, ready, pending steps
        sa.Column("defaults", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),   # product_key, unit_key, series…
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column("invoices", sa.Column("cfdi", postgresql.JSONB()))
    op.add_column("contacts", sa.Column("fiscal", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("contacts", "fiscal")
    op.drop_column("invoices", "cfdi")
    op.drop_table("cfdi_accounts")
