"""gift01 (v0.55.0): gift cards — sold (paid now or by the business's own pay link), sent to the recipient, redeemed in parts.

Revision ID: c8f5a1d9e2b6
Revises: a6d3f8b2c4e9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c8f5a1d9e2b6'
down_revision: Union[str, None] = 'a6d3f8b2c4e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gift_cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(16), nullable=False, unique=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("buyer_contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="SET NULL")),
        sa.Column("recipient_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("recipient_phone", sa.String(40), nullable=False, server_default=""),
        sa.Column("recipient_email", sa.String(320), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(8), nullable=False, server_default="pending"),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payments.id", ondelete="SET NULL")),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("expires_on", sa.Date()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('pending','active','void')", name=op.f("ck_gift_cards_status")),
        sa.CheckConstraint("amount_minor > 0 AND balance_minor >= 0 AND balance_minor <= amount_minor", name=op.f("ck_gift_cards_balance")),
    )
    op.create_index(op.f("ix_gift_cards_business_id"), "gift_cards", ["business_id"])
    op.create_table(
        "gift_card_uses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gift_card_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("gift_cards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("by", sa.String(100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_gift_card_uses_amount")),
    )
    op.create_index(op.f("ix_gift_card_uses_gift_card_id"), "gift_card_uses", ["gift_card_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_gift_card_uses_gift_card_id"), table_name="gift_card_uses")
    op.drop_table("gift_card_uses")
    op.drop_index(op.f("ix_gift_cards_business_id"), table_name="gift_cards")
    op.drop_table("gift_cards")
