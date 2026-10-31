"""agr01 (v0.25.0): our agreement with every business — the provider (whoever runs this server) and each business sign it

provider_agreements: one published version of the agreement for the whole server — the operator's details as published
and the words in every language (English is the canonical text), with the SHA-256 of all of it; frozen: a trigger refuses
any UPDATE or DELETE (a change is a new version). agreement_acceptances: a business signed a version — who (user, email,
typed name), for which business, the exact words shown (its language) and their SHA-256, the version's fingerprint, the
drawn signature (or "typed" when the pad could not be used), IP, device, time; never changed (a trigger refuses any UPDATE
except the user link becoming empty when that person's account is removed); a business's own rows go when the business
itself is deleted. agreement_asks: when the owner was first asked about a version (the grace runs from then), "Later",
"I don't agree".

Data integrity: nothing existing changes; nothing is deleted; no business is asked anything until the operator fills in
the provider's details and publishes a version (the agreement is dormant until then). Not a tool in the menu: no
"holds data" pin.

Revision ID: b7e4d1f9a2c6
Revises: a8d3f6c2e1b9
Create Date: 2026-10-31 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7e4d1f9a2c6'
down_revision: Union[str, Sequence[str], None] = 'a8d3f6c2e1b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FROZEN = """
CREATE FUNCTION provider_agreements_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'a published agreement never changes: publish a new version';
END $$;
CREATE TRIGGER provider_agreements_frozen BEFORE UPDATE OR DELETE ON provider_agreements
  FOR EACH ROW EXECUTE FUNCTION provider_agreements_frozen();
CREATE FUNCTION agreement_acceptances_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (to_jsonb(NEW) - 'user_id') IS DISTINCT FROM (to_jsonb(OLD) - 'user_id') OR NEW.user_id IS NOT NULL THEN
    RAISE EXCEPTION 'a signed agreement never changes';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER agreement_acceptances_frozen BEFORE UPDATE ON agreement_acceptances
  FOR EACH ROW EXECUTE FUNCTION agreement_acceptances_frozen();
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('provider_agreements',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('provider', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('texts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('doc_hash', sa.String(length=64), nullable=False),
    sa.Column('published_by', sa.String(length=100), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provider_agreements')),
    sa.UniqueConstraint('version', name=op.f('uq_provider_agreements_version'))
    )
    op.create_table('agreement_acceptances',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('agreement_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('signer_email', sa.String(length=320), server_default='', nullable=False),
    sa.Column('business_name', sa.String(length=200), nullable=False),
    sa.Column('signed_name', sa.String(length=200), nullable=False),
    sa.Column('lang', sa.String(length=10), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('text_sha256', sa.String(length=64), nullable=False),
    sa.Column('doc_hash', sa.String(length=64), nullable=False),
    sa.Column('method', sa.String(length=6), nullable=False),
    sa.Column('signature', sa.Text(), nullable=True),
    sa.Column('ip', sa.String(length=64), server_default='', nullable=False),
    sa.Column('ua', sa.String(length=300), server_default='', nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("method IN ('drawn','typed')", name=op.f('ck_agreement_acceptances_method')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_agreement_acceptances_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['agreement_id'], ['provider_agreements.id'], name=op.f('fk_agreement_acceptances_agreement_id_provider_agreements')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_agreement_acceptances_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agreement_acceptances')),
    sa.UniqueConstraint('business_id', 'agreement_id', name='uq_agreement_acceptance')
    )
    op.create_table('agreement_asks',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('agreement_id', sa.UUID(), nullable=False),
    sa.Column('first_asked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('later_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_later_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('declined_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_agreement_asks_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['agreement_id'], ['provider_agreements.id'], name=op.f('fk_agreement_asks_agreement_id_provider_agreements')),
    sa.PrimaryKeyConstraint('business_id', 'agreement_id', name=op.f('pk_agreement_asks'))
    )
    op.execute(FROZEN)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('agreement_asks')
    op.drop_table('agreement_acceptances')
    op.drop_table('provider_agreements')
    op.execute("DROP FUNCTION IF EXISTS agreement_acceptances_frozen(); DROP FUNCTION IF EXISTS provider_agreements_frozen();")
