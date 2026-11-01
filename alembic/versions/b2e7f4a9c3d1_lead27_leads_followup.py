"""lead27 (v0.27.0): lead follow-up never stops — every source tracked, every lead worked

lead_settings: how Valeria works this business's leads (Suggest = one tap each, the default; Autopilot = she sends through
the business's OWN accounts within the rules), the plan (day 0, 1, 3, 7, 14, 30, then every 30 days with offers consent),
the click-to-chat number / Page, an approved WhatsApp template, her own voice notes (off), pause all. lead_sources: each
place a lead comes from (a website, a Page, a profile, a marketplace, a flyer, a sign, an ad, an email-only platform) with
its code — the short link /l/<code>, the "ref <code>" in a chat link's first words, the forwarding address, the QR and
the website snippet. leads: one row per person who reached out FIRST (never someone typed in or imported), where they
came from, how they asked to be contacted (wrote first / a form / the snippet / an email), and where the follow-up stands
(it stops on a reply, an opt-out word, booked, won, lost, or the owner's pause). lead_touches: every follow-up, sent or
tapped or skipped. lead_voice: voice notes — the owner's recordings and Valeria's (a stock voice, never a clone).

Data integrity: five new tables; nothing existing changes, nothing is deleted, nothing is back-filled (the leads list
starts with the next person who writes). The new tool's "holds data" SQL (kits.REGISTRY["leads"].uses) is frozen below
like v0.19–v0.24's; no business holds lead rows yet, so the pin changes nothing today. Downgrade refuses while any lead,
source or voice note exists (it never deletes one).

Revision ID: b2e7f4a9c3d1
Revises: a6f2c9e4d8b1
Create Date: 2026-11-01 12:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2e7f4a9c3d1'
down_revision: Union[str, Sequence[str], None] = 'a6f2c9e4d8b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rows(table: str, where: str = "") -> str:
    return f"SELECT 1 FROM {table} x WHERE x.business_id = b.id{' AND x.' + where if where else ''}"


USES = {
    "leads": [_rows("lead_sources"), _rows("leads")],
}


def pin_statements() -> list[str]:
    """One UPDATE per new tool: pin it ON for every business that holds data in it (tests run these too)."""
    return [f"UPDATE businesses b SET tools = b.tools || jsonb_build_object('{key}', true) WHERE EXISTS({' UNION ALL '.join(uses)})"
            for key, uses in USES.items()]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lead_settings',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('mode', sa.String(length=10), server_default='suggest', nullable=False),
    sa.Column('followup', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('days', postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{0,1,3,7,14,30}'"), nullable=False),
    sa.Column('nurture', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('nurture_days', sa.Integer(), server_default=sa.text('30'), nullable=False),
    sa.Column('daily_cap', sa.Integer(), server_default=sa.text('40'), nullable=False),
    sa.Column('template', sa.String(length=100), nullable=True),
    sa.Column('chat_number', sa.String(length=20), nullable=True),
    sa.Column('messenger', sa.String(length=100), nullable=True),
    sa.Column('greeting', sa.String(length=300), server_default='', nullable=False),
    sa.Column('voice', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("mode IN ('suggest','autopilot')", name=op.f('ck_lead_settings_mode')),
    sa.CheckConstraint('daily_cap BETWEEN 1 AND 500', name=op.f('ck_lead_settings_daily_cap')),
    sa.CheckConstraint('nurture_days BETWEEN 14 AND 90', name=op.f('ck_lead_settings_nurture_days')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_lead_settings_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('business_id', name=op.f('pk_lead_settings'))
    )
    op.create_table('lead_sources',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=12), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('dest', sa.String(length=500), server_default='', nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('clicks', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('website','facebook','instagram','google','marketplace','flyer','sign','ad','email','referral','event','other')", name=op.f('ck_lead_sources_kind')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_lead_sources_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lead_sources')),
    sa.UniqueConstraint('code', name=op.f('uq_lead_sources_code'))
    )
    op.create_index('ix_lead_sources_biz', 'lead_sources', ['business_id', 'created_at'], unique=False)
    op.create_table('lead_voice',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('who', sa.String(length=8), nullable=False),
    sa.Column('mime', sa.String(length=60), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('transcript', sa.Text(), nullable=True),
    sa.Column('token', sa.String(length=40), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("who IN ('owner','valeria')", name=op.f('ck_lead_voice_who')),
    sa.CheckConstraint('size BETWEEN 1 AND 5242880', name=op.f('ck_lead_voice_size')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_lead_voice_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_lead_voice_contact_id_contacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lead_voice')),
    sa.UniqueConstraint('token', name=op.f('uq_lead_voice_token'))
    )
    op.create_table('leads',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=True),
    sa.Column('origin', sa.String(length=60), server_default='', nullable=False),
    sa.Column('status', sa.String(length=10), server_default='new', nullable=False),
    sa.Column('step', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('next_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paused', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('stop_reason', sa.String(length=20), nullable=True),
    sa.Column('blocked', sa.String(length=24), nullable=True),
    sa.Column('consent', sa.String(length=12), nullable=False),
    sa.Column('consent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('touches', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_touch_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('replied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('booked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('won_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lost_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('hot_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("consent IN ('wrote_first','form','widget','mail')", name=op.f('ck_leads_consent')),
    sa.CheckConstraint("status IN ('new','talking','booked','won','lost','quiet','stopped')", name=op.f('ck_leads_status')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_leads_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_leads_contact_id_contacts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['lead_sources.id'], name=op.f('fk_leads_source_id_lead_sources'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_leads')),
    sa.UniqueConstraint('business_id', 'contact_id', name='uq_lead_contact')
    )
    op.create_index('ix_leads_biz', 'leads', ['business_id', 'created_at'], unique=False)
    op.create_index('ix_leads_due', 'leads', ['next_at'], unique=False, postgresql_where=sa.text('next_at IS NOT NULL'))
    op.create_table('lead_touches',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('lead_id', sa.UUID(), nullable=False),
    sa.Column('step', sa.Integer(), nullable=False),
    sa.Column('how', sa.String(length=8), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=True),
    sa.Column('message_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('note', sa.String(length=200), server_default='', nullable=False),
    sa.Column('by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("how IN ('auto','tap','link','voice','skip')", name=op.f('ck_lead_touches_how')),
    sa.CheckConstraint("status IN ('sent','failed','opened','skipped')", name=op.f('ck_lead_touches_status')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_lead_touches_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_lead_touches_lead_id_leads'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_lead_touches_message_id_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lead_touches'))
    )
    op.create_index('ix_lead_touches_lead', 'lead_touches', ['lead_id', 'created_at'], unique=False)
    for sql in pin_statements():                         # what a business already uses never leaves its menu
        op.execute(sql)


def downgrade() -> None:
    """Downgrade schema."""
    n = op.get_bind().execute(sa.text("SELECT (SELECT count(*) FROM leads) + (SELECT count(*) FROM lead_sources) + "
                                      "(SELECT count(*) FROM lead_voice)")).scalar()
    if n:
        raise RuntimeError(f"{n} leads, sources or voice notes are kept here; downgrading would lose them, so it stops "
                           "(nothing changed)")
    op.drop_index('ix_lead_touches_lead', table_name='lead_touches')
    op.drop_table('lead_touches')
    op.drop_index('ix_leads_due', table_name='leads', postgresql_where=sa.text('next_at IS NOT NULL'))
    op.drop_index('ix_leads_biz', table_name='leads')
    op.drop_table('leads')
    op.drop_table('lead_voice')
    op.drop_index('ix_lead_sources_biz', table_name='lead_sources')
    op.drop_table('lead_sources')
    op.drop_table('lead_settings')
