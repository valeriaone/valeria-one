"""realty01 (v0.66.0): a realtor's properties on their Valeria One site — sent by their portal (valeria.valai.mx), one source.

Revision ID: a3c7e1f9b5d2
Revises: d2e6b9a4c1f7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a3c7e1f9b5d2'
down_revision: Union[str, None] = 'd2e6b9a4c1f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "listings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ext_ref", sa.String(80), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("operation", sa.String(8), nullable=False, server_default="sale"),
        sa.Column("kind", sa.String(40), nullable=False, server_default=""),
        sa.Column("price_minor", sa.BigInteger()),
        sa.Column("currency", sa.String(3), nullable=False, server_default="MXN"),
        sa.Column("beds", sa.Integer()),
        sa.Column("baths", sa.String(6), nullable=False, server_default=""),
        sa.Column("area_m2", sa.Integer()),
        sa.Column("lot_m2", sa.Integer()),
        sa.Column("place", sa.String(200), nullable=False, server_default=""),
        sa.Column("photos", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("url", sa.String(500), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("operation IN ('sale','rent')", name=op.f("ck_listings_operation")),
        sa.UniqueConstraint("business_id", "ext_ref", name="uq_listings_ref"),
    )


def downgrade() -> None:
    op.drop_table("listings")
