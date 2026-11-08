"""nps01 (v0.53.0): the 0–10 survey after a visit — who was asked, their score, when. Low scores reach the owner.

Revision ID: a6d3f8b2c4e9
Revises: f9a4c2e7b1d3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a6d3f8b2c4e9'
down_revision: Union[str, None] = 'f9a4c2e7b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nps_responses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("score", sa.Integer()),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("score IS NULL OR score BETWEEN 0 AND 10", name=op.f("ck_nps_responses_score")),
    )
    op.create_index(op.f("ix_nps_responses_business_contact"), "nps_responses", ["business_id", "contact_id", "asked_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_nps_responses_business_contact"), table_name="nps_responses")
    op.drop_table("nps_responses")
