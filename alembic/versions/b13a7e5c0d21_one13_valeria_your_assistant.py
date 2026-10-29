"""one13: Valeria, your assistant

The owner talks to Valeria from their own phone (owner_numbers, assistant_messages), her questions / log / undo
(assistant_actions), how she works and how the owner sounds (assistant_settings, style_samples), loyalty without
cards (loyalty_visits, loyalty_rewards), "help me set it up" (helpers, helper_leads), voice notes on messages
(transcript, suggestion), and the ready-made reminder rule (automations.preset 'reminder').

Revision ID: b13a7e5c0d21
Revises: 9d3e7b1a5c20
Create Date: 2026-10-29 22:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

OLD_PRESETS = "preset IS NULL OR preset IN ('winback','referral')"
NEW_PRESETS = "preset IS NULL OR preset IN ('winback','referral','reminder')"

# revision identifiers, used by Alembic.
revision: str = 'b13a7e5c0d21'
down_revision: Union[str, Sequence[str], None] = '9d3e7b1a5c20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f('ck_automations_preset'), 'automations', type_='check')
    op.create_check_constraint(op.f('ck_automations_preset'), 'automations', NEW_PRESETS)
    op.create_table('helpers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('company', sa.String(length=200), server_default='', nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('phone', sa.String(length=30), server_default='', nullable=False),
    sa.Column('country', sa.String(length=2), nullable=False),
    sa.Column('city', sa.String(length=100), server_default='', nullable=False),
    sa.Column('languages', postgresql.ARRAY(sa.String(length=10)), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('about', sa.String(length=500), server_default='', nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_by', sa.String(length=100), nullable=True),
    sa.Column('lead_price_minor', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default='USD', nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_helpers')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_helpers_token_hash'))
    )
    op.create_index('uq_helpers_email', 'helpers', [sa.literal_column('lower(email)')], unique=True)
    op.create_table('assistant_settings',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('autonomy', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('style', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('style_learned_on', sa.Date(), nullable=True),
    sa.Column('morning', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('weekly', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('slowday', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('holidays', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('holidays_skip', postgresql.ARRAY(sa.String(length=60)), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('answers', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('reminder_hours', sa.Integer(), server_default=sa.text('24'), nullable=False),
    sa.Column('loyalty_every', sa.Integer(), nullable=True),
    sa.Column('loyalty_reward', sa.String(length=200), server_default='', nullable=False),
    sa.Column('unanswered', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('quiet_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('loyalty_every IS NULL OR loyalty_every BETWEEN 2 AND 100', name=op.f('ck_assistant_settings_loyalty_every')),
    sa.CheckConstraint('reminder_hours BETWEEN 1 AND 72', name=op.f('ck_assistant_settings_reminder_hours')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_assistant_settings_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('business_id', name=op.f('pk_assistant_settings'))
    )
    op.create_table('helper_leads',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('helper_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=10), server_default='sent', nullable=False),
    sa.Column('shared', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('consent_text', sa.Text(), nullable=False),
    sa.Column('consent_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consent_by', sa.String(length=100), nullable=False),
    sa.Column('free', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('price_minor', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default='USD', nullable=False),
    sa.Column('rating', sa.Integer(), nullable=True),
    sa.Column('rated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('waiting','sent','accepted','declined','done')", name=op.f('ck_helper_leads_status')),
    sa.CheckConstraint('rating IS NULL OR rating BETWEEN 1 AND 5', name=op.f('ck_helper_leads_rating')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_helper_leads_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['helper_id'], ['helpers.id'], name=op.f('fk_helper_leads_helper_id_helpers'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_helper_leads'))
    )
    op.create_table('owner_numbers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=200), server_default='', nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=True),
    sa.Column('address', sa.String(length=320), nullable=True),
    sa.Column('role', sa.String(length=10), server_default='owner', nullable=False),
    sa.Column('perms', postgresql.ARRAY(sa.String(length=20)), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('lang', sa.String(length=10), nullable=True),
    sa.Column('pair_code', sa.String(length=12), nullable=True),
    sa.Column('pair_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('owner','staff')", name=op.f('ck_owner_numbers_role')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_owner_numbers_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_owner_numbers_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_owner_numbers')),
    sa.UniqueConstraint('business_id', 'channel', 'address', name='uq_owner_number'),
    sa.UniqueConstraint('pair_code', name=op.f('uq_owner_numbers_pair_code'))
    )
    op.create_table('style_samples',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('draft', sa.Text(), nullable=False),
    sa.Column('sent', sa.Text(), nullable=False),
    sa.Column('learned', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_style_samples_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_style_samples'))
    )
    op.create_table('assistant_actions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='asked', nullable=False),
    sa.Column('summary', sa.Text(), server_default='', nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=True),
    sa.Column('number_id', sa.UUID(), nullable=True),
    sa.Column('by', sa.String(length=100), nullable=False),
    sa.Column('dedupe', sa.String(length=120), nullable=True),
    sa.Column('prompted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('send_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('asked','queued','running','done','declined','later','undone','failed','expired')", name=op.f('ck_assistant_actions_status')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_assistant_actions_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_assistant_actions_contact_id_contacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['number_id'], ['owner_numbers.id'], name=op.f('fk_assistant_actions_number_id_owner_numbers'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assistant_actions')),
    sa.UniqueConstraint('business_id', 'dedupe', name='uq_assistant_dedupe')
    )
    op.create_index('ix_assistant_actions_biz', 'assistant_actions', ['business_id', 'created_at'], unique=False)
    op.create_index('ix_assistant_actions_due', 'assistant_actions', ['send_at'], unique=False, postgresql_where=sa.text("status = 'queued'"))
    op.create_table('assistant_messages',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('number_id', sa.UUID(), nullable=True),
    sa.Column('channel_account_id', sa.UUID(), nullable=True),
    sa.Column('direction', sa.String(length=3), nullable=False),
    sa.Column('body', sa.Text(), server_default='', nullable=False),
    sa.Column('media', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('transcript', sa.Text(), nullable=True),
    sa.Column('provider_id', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=12), server_default='received', nullable=False),
    sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("direction IN ('in','out')", name=op.f('ck_assistant_messages_direction')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_assistant_messages_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], name=op.f('fk_assistant_messages_channel_account_id_channel_accounts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['number_id'], ['owner_numbers.id'], name=op.f('fk_assistant_messages_number_id_owner_numbers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assistant_messages'))
    )
    op.create_index('uq_assistant_messages_provider', 'assistant_messages', ['business_id', 'provider_id'], unique=True, postgresql_where=sa.text('provider_id IS NOT NULL'))
    op.create_table('loyalty_rewards',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('visit_number', sa.Integer(), nullable=False),
    sa.Column('reward', sa.String(length=200), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('used_by', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_loyalty_rewards_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_loyalty_rewards_contact_id_contacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_loyalty_rewards')),
    sa.UniqueConstraint('business_id', 'contact_id', 'visit_number', name='uq_loyalty_reward')
    )
    op.create_table('loyalty_visits',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('on_date', sa.Date(), nullable=False),
    sa.Column('source', sa.String(length=12), nullable=False),
    sa.Column('ref_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_loyalty_visits_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_loyalty_visits_contact_id_contacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_loyalty_visits')),
    sa.UniqueConstraint('business_id', 'contact_id', 'on_date', name='uq_loyalty_visit')
    )
    op.add_column('messages', sa.Column('transcript', sa.Text(), nullable=True))
    op.add_column('messages', sa.Column('suggestion', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('messages', 'suggestion')
    op.drop_column('messages', 'transcript')
    op.drop_table('loyalty_visits')
    op.drop_table('loyalty_rewards')
    op.drop_index('uq_assistant_messages_provider', table_name='assistant_messages', postgresql_where=sa.text('provider_id IS NOT NULL'))
    op.drop_table('assistant_messages')
    op.drop_index('ix_assistant_actions_due', table_name='assistant_actions', postgresql_where=sa.text("status = 'queued'"))
    op.drop_index('ix_assistant_actions_biz', table_name='assistant_actions')
    op.drop_table('assistant_actions')
    op.drop_table('style_samples')
    op.drop_table('owner_numbers')
    op.drop_table('helper_leads')
    op.drop_table('assistant_settings')
    op.drop_index('uq_helpers_email', table_name='helpers')
    op.drop_table('helpers')
    op.execute("DELETE FROM automations WHERE preset = 'reminder'")     # rules the old code doesn't know
    op.drop_constraint(op.f('ck_automations_preset'), 'automations', type_='check')
    op.create_check_constraint(op.f('ck_automations_preset'), 'automations', OLD_PRESETS)
