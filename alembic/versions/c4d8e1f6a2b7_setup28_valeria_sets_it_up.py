"""setup28 (v0.28.0): «Valeria sets it up» — one talk that asks which accounts the business already has and connects each

onboarding: one row per business — what it told Valeria it has (WhatsApp and which kind, its email provider, its websites,
Facebook Page, Instagram, TikTok, YouTube, Google profile, calendar, the payments it takes, other chat apps, its trade's
usual apps), the tracked sources she made for each answer, "I don't have it" and "later". Never a password: the only
secret the talk takes is an email app password, and it goes straight into the existing encrypted channel account
(channel_accounts.config, VO_SECRET_KEY), never into this table.

Data integrity: one new table; nothing existing changes, nothing is deleted, nothing is back-filled (a business's talk
starts empty and shows what it already has connected). No new tool in the menu, so no "holds data" pin. Downgrade refuses
while any business has started its talk (it never deletes an answer).

Revision ID: c4d8e1f6a2b7
Revises: b2e7f4a9c3d1
Create Date: 2026-11-02 12:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4d8e1f6a2b7'
down_revision: Union[str, Sequence[str], None] = 'b2e7f4a9c3d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('onboarding',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_onboarding_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('business_id', name=op.f('pk_onboarding'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM onboarding")).scalar()
    if n:
        raise RuntimeError(f"{n} businesses have told Valeria what accounts they have; downgrading would lose it, so it stops "
                           "(nothing changed)")
    op.drop_table('onboarding')
