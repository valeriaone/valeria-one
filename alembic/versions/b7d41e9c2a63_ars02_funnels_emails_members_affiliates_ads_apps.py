"""ars02 (v0.21.0): funnels, email designs, the posting calendar, memberships + courses + community, affiliates, ads,
apps that send customers in

funnels / funnel_pages: landing pages in steps, public at /s/<business>/p/<page>; funnel_hits: visits and leads per step,
counted with a daily hash (no cookie, no IP kept). email_designs: saved block emails reused by campaigns and drips.
posts.by_hand: the networks the owner posts on by hand (a reminder at the time, never published by us).
member_plans / members / member_links: memberships (free, or paid through one of the business's own plans: invoices.py),
members (in only by the owner's yes), one-time sign-in links (hash only). courses / course_modules / lessons /
lesson_progress: modules -> lessons, drip by days, progress per member. community_posts / community_comments: a board per
membership, the owner hides (never deletes). affiliates / affiliate_referrals / affiliate_commissions / affiliate_payouts:
people with their own link, the customers they brought, what they earned on paid sales (a refund takes it back), and
what the owner paid them outside the app. ads: ad copy for the business's OWN ad accounts and a tracked link.
inbound_hooks: other apps send a customer (or a deal) in; only the token's hash is kept.

Data integrity: nothing existing changes (posts gets one column, empty); nothing is deleted. The tools' "holds data" SQL
(kits.REGISTRY[*].uses) for the eight new tools is frozen below like v0.19's and v0.20's: a business that already has a
scheduled post (Schedule) or an active webhook (Connect other apps) keeps them in its menu; the others hold no data yet.

Revision ID: b7d41e9c2a63
Revises: 4ef016779862
Create Date: 2026-10-30 21:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7d41e9c2a63'
down_revision: Union[str, Sequence[str], None] = '4ef016779862'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rows(table: str, where: str = "") -> str:
    return f"SELECT 1 FROM {table} x WHERE x.business_id = b.id{' AND x.' + where if where else ''}"


USES = {
    "funnels": [_rows("funnels")],
    "emails": [_rows("email_designs")],
    "schedule": [_rows("posts", "publish_at IS NOT NULL")],
    "members": [_rows("member_plans"), _rows("members")],
    "courses": [_rows("courses")],
    "affiliates": [_rows("affiliates")],
    "ads": [_rows("ads")],
    "connect_apps": [_rows("inbound_hooks"), _rows("webhook_endpoints", "active")],
}


def pin_statements() -> list[str]:
    """One UPDATE per new tool: pin it ON for every business that holds data in it (tests run these too)."""
    return [f"UPDATE businesses b SET tools = b.tools || jsonb_build_object('{key}', true) WHERE EXISTS({' UNION ALL '.join(uses)})"
            for key, uses in USES.items()]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('ads',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('platform', sa.String(length=8), server_default='meta', nullable=False),
    sa.Column('idea', sa.Text(), server_default='', nullable=False),
    sa.Column('headline', sa.String(length=200), server_default='', nullable=False),
    sa.Column('body', sa.Text(), server_default='', nullable=False),
    sa.Column('audience', sa.Text(), server_default='', nullable=False),
    sa.Column('dest', sa.String(length=500), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('clicks', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('by_ai', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("platform IN ('meta','google','tiktok','other')", name=op.f('ck_ads_platform')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_ads_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ads')),
    sa.UniqueConstraint('code', name=op.f('uq_ads_code'))
    )
    op.create_table('affiliates',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=320), server_default='', nullable=False),
    sa.Column('phone', sa.String(length=30), server_default='', nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('kind', sa.String(length=5), server_default='pct', nullable=False),
    sa.Column('rate_bp', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('fixed_minor', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('first_only', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('clicks', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('note', sa.String(length=300), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('pct','fixed')", name=op.f('ck_affiliates_kind')),
    sa.CheckConstraint('fixed_minor >= 0', name=op.f('ck_affiliates_fixed')),
    sa.CheckConstraint('rate_bp BETWEEN 0 AND 10000', name=op.f('ck_affiliates_rate')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_affiliates_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_affiliates')),
    sa.UniqueConstraint('code', name=op.f('uq_affiliates_code'))
    )
    op.create_table('email_designs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('subject', sa.String(length=200), server_default='', nullable=False),
    sa.Column('blocks', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_email_designs_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_designs'))
    )
    op.create_table('funnels',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_funnels_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_funnels'))
    )
    op.create_table('inbound_hooks',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('prefix', sa.String(length=12), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('makes', sa.String(length=7), server_default='contact', nullable=False),
    sa.Column('stage', sa.String(length=40), nullable=True),
    sa.Column('tag', sa.String(length=60), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('uses', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("makes IN ('contact','deal')", name=op.f('ck_inbound_hooks_makes')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_inbound_hooks_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inbound_hooks')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_inbound_hooks_token_hash'))
    )
    op.create_table('affiliate_payouts',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('affiliate_id', sa.UUID(), nullable=False),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('paid_on', sa.Date(), nullable=False),
    sa.Column('note', sa.String(length=300), server_default='', nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['affiliate_id'], ['affiliates.id'], name=op.f('fk_affiliate_payouts_affiliate_id_affiliates'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_affiliate_payouts_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_affiliate_payouts'))
    )
    op.create_table('affiliate_referrals',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('affiliate_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('how', sa.String(length=12), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['affiliate_id'], ['affiliates.id'], name=op.f('fk_affiliate_referrals_affiliate_id_affiliates'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_affiliate_referrals_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_affiliate_referrals_contact_id_contacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_affiliate_referrals')),
    sa.UniqueConstraint('business_id', 'contact_id', name='uq_affiliate_customer')
    )
    op.create_table('funnel_pages',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('funnel_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('title', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=60), nullable=False),
    sa.Column('blocks', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_funnel_pages_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['funnel_id'], ['funnels.id'], name=op.f('fk_funnel_pages_funnel_id_funnels'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_funnel_pages')),
    sa.UniqueConstraint('business_id', 'slug', name='uq_funnel_page_slug')
    )
    op.create_index('ix_funnel_pages_funnel', 'funnel_pages', ['funnel_id', 'position'], unique=False)
    op.create_table('member_plans',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('about', sa.Text(), server_default='', nullable=False),
    sa.Column('plan_id', sa.UUID(), nullable=True),
    sa.Column('community', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('open_join', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_member_plans_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], name=op.f('fk_member_plans_plan_id_plans'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_member_plans'))
    )
    op.create_table('affiliate_commissions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('affiliate_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=True),
    sa.Column('source', sa.String(length=8), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=7), nullable=False),
    sa.Column('base_minor', sa.BigInteger(), nullable=False),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('payout_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('earn','reverse')", name=op.f('ck_affiliate_commissions_kind')),
    sa.CheckConstraint("source IN ('invoice','pos')", name=op.f('ck_affiliate_commissions_source')),
    sa.ForeignKeyConstraint(['affiliate_id'], ['affiliates.id'], name=op.f('fk_affiliate_commissions_affiliate_id_affiliates'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_affiliate_commissions_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_affiliate_commissions_contact_id_contacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['payout_id'], ['affiliate_payouts.id'], name=op.f('fk_affiliate_commissions_payout_id_affiliate_payouts'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_affiliate_commissions')),
    sa.UniqueConstraint('source', 'source_id', 'kind', name='uq_affiliate_commission')
    )
    op.create_table('courses',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('member_plan_id', sa.UUID(), nullable=True),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('about', sa.Text(), server_default='', nullable=False),
    sa.Column('published', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_courses_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['member_plan_id'], ['member_plans.id'], name=op.f('fk_courses_member_plan_id_member_plans'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_courses'))
    )
    op.create_table('funnel_hits',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('page_id', sa.UUID(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('visitor', sa.String(length=16), nullable=False),
    sa.Column('kind', sa.String(length=5), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('view','lead')", name=op.f('ck_funnel_hits_kind')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_funnel_hits_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['page_id'], ['funnel_pages.id'], name=op.f('fk_funnel_hits_page_id_funnel_pages'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_funnel_hits')),
    sa.UniqueConstraint('page_id', 'day', 'visitor', 'kind', name='uq_funnel_hit')
    )
    op.create_table('members',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('member_plan_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=8), server_default='pending', nullable=False),
    sa.Column('subscription_id', sa.UUID(), nullable=True),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending','active','paused','removed')", name=op.f('ck_members_status')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_members_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_members_contact_id_contacts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['member_plan_id'], ['member_plans.id'], name=op.f('fk_members_member_plan_id_member_plans'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], name=op.f('fk_members_subscription_id_subscriptions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_members')),
    sa.UniqueConstraint('member_plan_id', 'contact_id', name='uq_member')
    )
    op.create_table('community_posts',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('member_plan_id', sa.UUID(), nullable=False),
    sa.Column('member_id', sa.UUID(), nullable=True),
    sa.Column('by_business', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('hidden_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('hidden_by', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_community_posts_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], name=op.f('fk_community_posts_member_id_members'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['member_plan_id'], ['member_plans.id'], name=op.f('fk_community_posts_member_plan_id_member_plans'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_community_posts'))
    )
    op.create_index('ix_community_posts_plan', 'community_posts', ['member_plan_id', 'created_at'], unique=False)
    op.create_table('course_modules',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('course_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_course_modules_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['course_id'], ['courses.id'], name=op.f('fk_course_modules_course_id_courses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_course_modules'))
    )
    op.create_table('member_links',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('member_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_member_links_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], name=op.f('fk_member_links_member_id_members'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_member_links')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_member_links_token_hash'))
    )
    op.create_table('community_comments',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('post_id', sa.UUID(), nullable=False),
    sa.Column('member_id', sa.UUID(), nullable=True),
    sa.Column('by_business', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('hidden_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('hidden_by', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_community_comments_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], name=op.f('fk_community_comments_member_id_members'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['post_id'], ['community_posts.id'], name=op.f('fk_community_comments_post_id_community_posts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_community_comments'))
    )
    op.create_table('lessons',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('module_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('kind', sa.String(length=5), server_default='text', nullable=False),
    sa.Column('body', sa.Text(), server_default='', nullable=False),
    sa.Column('video_url', sa.String(length=500), nullable=True),
    sa.Column('file_id', sa.UUID(), nullable=True),
    sa.Column('drip_days', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('text','video','file')", name=op.f('ck_lessons_kind')),
    sa.CheckConstraint('drip_days BETWEEN 0 AND 3650', name=op.f('ck_lessons_drip')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_lessons_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_lessons_file_id_files'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['module_id'], ['course_modules.id'], name=op.f('fk_lessons_module_id_course_modules'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lessons'))
    )
    op.create_table('lesson_progress',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('member_id', sa.UUID(), nullable=False),
    sa.Column('lesson_id', sa.UUID(), nullable=False),
    sa.Column('done_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_lesson_progress_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['lesson_id'], ['lessons.id'], name=op.f('fk_lesson_progress_lesson_id_lessons'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], name=op.f('fk_lesson_progress_member_id_members'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_progress')),
    sa.UniqueConstraint('member_id', 'lesson_id', name='uq_lesson_done')
    )
    op.add_column('posts', sa.Column('by_hand', postgresql.ARRAY(sa.String(length=20)), server_default=sa.text("'{}'"), nullable=False))
    for sql in pin_statements():                         # what a business already uses never leaves its menu
        op.execute(sql)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('posts', 'by_hand')
    op.drop_table('lesson_progress')
    op.drop_table('lessons')
    op.drop_table('community_comments')
    op.drop_table('member_links')
    op.drop_table('course_modules')
    op.drop_index('ix_community_posts_plan', table_name='community_posts')
    op.drop_table('community_posts')
    op.drop_table('members')
    op.drop_table('funnel_hits')
    op.drop_table('courses')
    op.drop_table('affiliate_commissions')
    op.drop_table('member_plans')
    op.drop_index('ix_funnel_pages_funnel', table_name='funnel_pages')
    op.drop_table('funnel_pages')
    op.drop_table('affiliate_referrals')
    op.drop_table('affiliate_payouts')
    op.drop_table('inbound_hooks')
    op.drop_table('funnels')
    op.drop_table('email_designs')
    op.drop_table('affiliates')
    op.drop_table('ads')
