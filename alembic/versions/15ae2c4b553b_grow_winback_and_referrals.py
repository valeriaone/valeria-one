"""grow: win-back and bring-a-friend

Revision ID: 15ae2c4b553b
Revises: f3b8d2a6c1e4
Create Date: 2026-10-29 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '15ae2c4b553b'
down_revision: Union[str, Sequence[str], None] = 'f3b8d2a6c1e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_TRIGGERS = "trigger IN ('appointment.reminder','appointment.done','contact.created','deal.won')"
NEW_TRIGGERS = ("trigger IN ('appointment.reminder','appointment.done','contact.created','deal.won','payment.paid',"
                "'contact.quiet')")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('automations', sa.Column('preset', sa.String(length=20), nullable=True))
    op.create_check_constraint(op.f('ck_automations_preset'), 'automations', "preset IS NULL OR preset IN ('winback','referral')")
    op.drop_constraint(op.f('ck_automations_trigger'), 'automations', type_='check')
    op.create_check_constraint(op.f('ck_automations_trigger'), 'automations', NEW_TRIGGERS)

    op.create_table('growth_settings',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('quiet_days', sa.Integer(), server_default=sa.text('60'), nullable=False),
    sa.Column('gap_days', sa.Integer(), server_default=sa.text('180'), nullable=False),
    sa.Column('daily_cap', sa.Integer(), server_default=sa.text('30'), nullable=False),
    sa.Column('offer', sa.String(length=200), server_default='', nullable=False),
    sa.Column('reward_referrer', sa.String(length=200), server_default='', nullable=False),
    sa.Column('reward_friend', sa.String(length=200), server_default='', nullable=False),
    sa.Column('ran_on', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('quiet_days BETWEEN 14 AND 365', name=op.f('ck_growth_settings_quiet_days')),
    sa.CheckConstraint('gap_days BETWEEN 30 AND 730', name=op.f('ck_growth_settings_gap_days')),
    sa.CheckConstraint('daily_cap BETWEEN 1 AND 500', name=op.f('ck_growth_settings_daily_cap')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_growth_settings_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('business_id', name=op.f('pk_growth_settings'))
    )
    op.create_table('referral_links',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('opens', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_referral_links_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_referral_links_contact_id_contacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_referral_links')),
    sa.UniqueConstraint('business_id', 'contact_id', name='uq_referral_link'),
    sa.UniqueConstraint('code', name=op.f('uq_referral_links_code'))
    )
    op.create_table('referrals',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('link_id', sa.UUID(), nullable=False),
    sa.Column('referrer_id', sa.UUID(), nullable=False),
    sa.Column('friend_id', sa.UUID(), nullable=False),
    sa.Column('how', sa.String(length=12), nullable=False),
    sa.Column('reward_referrer', sa.String(length=200), server_default='', nullable=False),
    sa.Column('reward_friend', sa.String(length=200), server_default='', nullable=False),
    sa.Column('referrer_given_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('friend_given_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('referrer_id <> friend_id', name=op.f('ck_referrals_not_self')),
    sa.CheckConstraint("how IN ('booking','website','chat','form')", name=op.f('ck_referrals_how')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_referrals_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['friend_id'], ['contacts.id'], name=op.f('fk_referrals_friend_id_contacts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['link_id'], ['referral_links.id'], name=op.f('fk_referrals_link_id_referral_links'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['referrer_id'], ['contacts.id'], name=op.f('fk_referrals_referrer_id_contacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_referrals')),
    sa.UniqueConstraint('business_id', 'friend_id', name='uq_referral_friend')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('referrals')
    op.drop_table('referral_links')
    op.drop_table('growth_settings')
    op.execute("DELETE FROM automations WHERE trigger IN ('payment.paid','contact.quiet')")   # rules the old code can't run
    op.drop_constraint(op.f('ck_automations_trigger'), 'automations', type_='check')
    op.create_check_constraint(op.f('ck_automations_trigger'), 'automations', OLD_TRIGGERS)
    op.drop_constraint(op.f('ck_automations_preset'), 'automations', type_='check')
    op.drop_column('automations', 'preset')
