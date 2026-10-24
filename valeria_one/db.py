"""Database: SQLAlchemy Core tables. Every row that belongs to a business carries business_id, and
every query in services/ filters by it — one business can never read another's data."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
                        MetaData, String, Table, Text, UniqueConstraint, create_engine, func, text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from . import config

metadata = MetaData(naming_convention={
    "ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"})


def _id():
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _biz():
    return Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)


def _ts(name="created_at"):
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


businesses = Table(
    "businesses", metadata, _id(),
    Column("name", String(200), nullable=False),
    Column("country", String(2), nullable=False),          # country pack code
    Column("industry", String(40), nullable=False),        # industry pack code
    Column("language", String(10), nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("sandbox", Boolean, nullable=False, server_default=text("false")),
    Column("demo", Boolean, nullable=False, server_default=text("false")),   # a public demo anyone may open (sandbox only)
    # public booking page /b/<slug>
    Column("slug", String(60), unique=True),
    Column("booking_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("hours", JSONB, nullable=False, server_default=text("'{}'")),     # {"mon": [["09:00","18:00"]], …}
    Column("booking_notice_minutes", Integer, nullable=False, server_default=text("120")),
    Column("booking_days_ahead", Integer, nullable=False, server_default=text("30")),
    # public website /s/<slug>
    Column("site_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("site", JSONB, nullable=False, server_default=text("'{}'")),       # headline, about, color, links, address…
    _ts(), _ts("updated_at"),
)

booking_services = Table(                                    # what a customer can book on the public page
    "booking_services", metadata, _id(), _biz(),
    Column("name", String(120), nullable=False),
    Column("minutes", Integer, nullable=False),
    Column("price_minor", BigInteger),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("position", Integer, nullable=False, server_default=text("0")),
    _ts(),
    CheckConstraint("minutes BETWEEN 5 AND 1440", name="minutes"),
    CheckConstraint("price_minor IS NULL OR price_minor >= 0", name="price"),
)

api_keys = Table(
    "api_keys", metadata, _id(), _biz(),
    Column("name", String(100), nullable=False),
    Column("prefix", String(16), nullable=False),           # shown to people: vo_test_ab12…
    Column("key_hash", String(64), nullable=False, unique=True),   # sha256 of the full key; the key itself is never stored
    Column("last_used_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    _ts(),
)

users = Table(                                               # people who sign in to the web app
    "users", metadata, _id(),
    Column("email", String(320), nullable=False),
    Column("name", String(200), nullable=False, server_default=""),
    Column("password_hash", String(200), nullable=False),
    Column("failed_attempts", Integer, nullable=False, server_default=text("0")),
    Column("locked_until", DateTime(timezone=True)),
    Column("last_login_at", DateTime(timezone=True)),
    _ts(),
)
Index("uq_users_email", func.lower(users.c.email), unique=True)

memberships = Table(
    "memberships", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role", String(10), nullable=False, server_default="owner"),
    _ts(),
    CheckConstraint("role IN ('owner','staff')", name="role"),
)

contacts = Table(
    "contacts", metadata, _id(), _biz(),
    Column("name", String(200), nullable=False, server_default=""),
    Column("email", String(320)),
    Column("phone", String(20)),                             # E.164
    Column("language", String(10)),
    Column("source", String(60)),                            # whatsapp, lead_ad, website, import, …
    Column("tags", ARRAY(String(60)), nullable=False, server_default=text("'{}'")),
    Column("notes", Text, nullable=False, server_default=""),
    Column("marketing_consent", Boolean, nullable=False, server_default=text("false")),
    Column("marketing_consent_at", DateTime(timezone=True)),
    Column("marketing_consent_source", String(200)),         # how they agreed: "web form /contact", "replied YES"
    Column("unsubscribed_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
    _ts(), _ts("updated_at"),
)
Index("ix_contacts_biz_phone", contacts.c.business_id, contacts.c.phone)
Index("ix_contacts_biz_email", contacts.c.business_id, func.lower(contacts.c.email))

channel_accounts = Table(
    "channel_accounts", metadata, _id(), _biz(),
    Column("channel", String(20), nullable=False),
    Column("name", String(100), nullable=False),
    Column("address", String(200)),                          # our side: phone number id, page id, from-email …
    Column("config", JSONB, nullable=False, server_default=text("'{}'")),   # secrets inside are encrypted (crypto.py)
    Column("hook_id", String(40), nullable=False, unique=True),             # unguessable path for inbound webhooks
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
)

identities = Table(                                          # how a contact is reached on each channel
    "contact_identities", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("channel", String(20), nullable=False),
    Column("address", String(320), nullable=False),          # phone, email, Messenger PSID, IG id, LINE user id …
    UniqueConstraint("business_id", "channel", "address", name="uq_identity"),
)

conversations = Table(
    "conversations", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("channel_account_id", UUID(as_uuid=True), ForeignKey("channel_accounts.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(10), nullable=False, server_default="open"),
    Column("last_inbound_at", DateTime(timezone=True)),      # starts the reply window (WhatsApp/Messenger 24 h)
    Column("last_message_at", DateTime(timezone=True)),
    Column("needs_human", Boolean, nullable=False, server_default=text("false")),   # the AI handed it to a person
    Column("flow", JSONB),                                     # an in-chat booking in progress (step, choices, expires)
    _ts(),
    UniqueConstraint("channel_account_id", "contact_id", name="uq_conversation"),
    CheckConstraint("status IN ('open','closed')", name="status"),
)

messages = Table(
    "messages", metadata, _id(), _biz(),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
    Column("direction", String(3), nullable=False),
    Column("kind", String(12), nullable=False, server_default="service"),   # service | marketing
    Column("body", Text, nullable=False, server_default=""),
    Column("media", JSONB, nullable=False, server_default=text("'[]'")),
    Column("template", JSONB),                                # pre-approved template (outside a reply window)
    Column("provider_id", String(200)),
    Column("status", String(12), nullable=False),
    Column("error", Text),
    Column("by_ai", Boolean, nullable=False, server_default=text("false")),
    _ts(),
    CheckConstraint("direction IN ('in','out')", name="direction"),
    CheckConstraint("kind IN ('service','marketing')", name="kind"),
    CheckConstraint("status IN ('received','queued','sent','delivered','read','failed','blocked')", name="status"),
)
Index("ix_messages_conv_created", messages.c.conversation_id, messages.c.created_at)
Index("uq_messages_provider", messages.c.business_id, messages.c.provider_id, unique=True,
      postgresql_where=messages.c.provider_id.isnot(None))

deals = Table(
    "deals", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("title", String(200), nullable=False),
    Column("stage", String(40), nullable=False),             # a stage key from the industry pack
    Column("amount_minor", BigInteger),
    Column("currency", String(3), nullable=False),
    Column("expected_close", DateTime(timezone=True)),
    Column("closed_at", DateTime(timezone=True)),
    Column("notes", Text, nullable=False, server_default=""),
    _ts(), _ts("updated_at"),
    CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="amount"),
)

appointments = Table(
    "appointments", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("deal_id", UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")),
    Column("title", String(200), nullable=False),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("ends_at", DateTime(timezone=True), nullable=False),
    Column("status", String(12), nullable=False, server_default="booked"),
    Column("location", String(300)),
    Column("notes", Text, nullable=False, server_default=""),
    Column("external_id", String(200)),                      # the event's id in the business's Google Calendar
    Column("synced_at", DateTime(timezone=True)),             # last time it matched Google (NULL = needs a push)
    _ts(), _ts("updated_at"),
    CheckConstraint("ends_at > starts_at", name="times"),
    CheckConstraint("status IN ('booked','confirmed','done','no_show','cancelled')", name="status"),
)

meta_leads = Table(                                          # Facebook / Instagram Lead Ads, stored once per lead
    "meta_leads", metadata, _id(), _biz(),
    Column("leadgen_id", String(64), nullable=False),
    Column("page_id", String(64), nullable=False),
    Column("form_id", String(64)),
    Column("ad_id", String(64)),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("data", JSONB, nullable=False, server_default=text("'{}'")),
    _ts(),
    UniqueConstraint("business_id", "leadgen_id", name="uq_meta_lead"),
)

comment_rules = Table(                                       # "someone comments 'precio' -> answer them by private message"
    "comment_rules", metadata, _id(), _biz(),
    Column("keywords", ARRAY(String(60)), nullable=False),    # lower-case; empty = any comment
    Column("reply", Text, nullable=False),
    Column("channels", ARRAY(String(20)), nullable=False, server_default=text("'{messenger,instagram}'")),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
)

social_comments = Table(                                     # public comments on the business's posts, kept once each
    "social_comments", metadata, _id(), _biz(),
    Column("channel_account_id", UUID(as_uuid=True), ForeignKey("channel_accounts.id", ondelete="CASCADE"), nullable=False),
    Column("platform", String(20), nullable=False),           # messenger (Facebook Page) | instagram
    Column("comment_id", String(100), nullable=False),
    Column("post_id", String(100)),
    Column("author_id", String(100)),
    Column("author_name", String(200)),
    Column("text", Text, nullable=False, server_default=""),
    Column("commented_at", DateTime(timezone=True)),
    Column("rule_id", UUID(as_uuid=True), ForeignKey("comment_rules.id", ondelete="SET NULL")),
    Column("replied_at", DateTime(timezone=True)),
    Column("reply_error", Text),
    _ts(),
    UniqueConstraint("business_id", "comment_id", name="uq_social_comment"),
)

calendar_links = Table(                                      # a business's own Google Calendar, connected with OAuth
    "calendar_links", metadata, _id(), _biz(),
    Column("provider", String(20), nullable=False),           # google
    Column("account_email", String(320)),
    Column("calendar_id", String(300), nullable=False, server_default="primary"),
    Column("refresh_token", Text, nullable=False),            # encrypted (crypto.seal)
    Column("last_error", Text),
    _ts(),
    UniqueConstraint("business_id", "provider", name="uq_calendar_link"),
)

ai_settings = Table(                                         # the business's OWN AI key (Gemini, Claude, OpenAI)
    "ai_settings", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("provider", String(20), nullable=False),
    Column("api_key", Text),                                  # encrypted
    Column("model", String(100)),
    Column("mode", String(10), nullable=False, server_default="off"),   # off | suggest | auto
    Column("instructions", Text, nullable=False, server_default=""),     # facts, tone, FAQ — in the owner's words
    _ts("updated_at"),
    CheckConstraint("mode IN ('off','suggest','auto')", name="mode"),
    CheckConstraint("provider IN ('gemini','anthropic','openai','demo')", name="provider"),
)

webhook_endpoints = Table(                                   # other apps that want to hear about events
    "webhook_endpoints", metadata, _id(), _biz(),
    Column("url", String(500), nullable=False),
    Column("secret", Text, nullable=False),                   # encrypted; signs every delivery (X-VO-Signature)
    Column("events", ARRAY(String(60)), nullable=False, server_default=text("'{*}'")),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("last_status", Integer),
    Column("last_error", Text),
    _ts(),
)

webhook_deliveries = Table(                                  # outbox: written in the same transaction as the event
    "webhook_deliveries", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _biz(),
    Column("endpoint_id", UUID(as_uuid=True), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False),
    Column("event_id", BigInteger, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("next_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("delivered_at", DateTime(timezone=True)),
    Column("given_up_at", DateTime(timezone=True)),
    Column("last_status", Integer),
    Column("last_error", Text),
    _ts(),
)
Index("ix_webhook_due", webhook_deliveries.c.next_at, postgresql_where=webhook_deliveries.c.delivered_at.is_(None) & webhook_deliveries.c.given_up_at.is_(None))

invitations = Table(                                         # "join my business" links for staff
    "invitations", metadata, _id(), _biz(),
    Column("email", String(320), nullable=False),
    Column("role", String(10), nullable=False, server_default="staff"),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("accepted_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    _ts(),
    CheckConstraint("role IN ('owner','staff')", name="role"),
)

payment_accounts = Table(                                    # the business's OWN payment provider account
    "payment_accounts", metadata, _id(), _biz(),
    Column("provider", String(20), nullable=False),
    Column("config", JSONB, nullable=False, server_default=text("'{}'")),   # secrets encrypted
    Column("hook_id", String(40), nullable=False, unique=True),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
)

payments = Table(                                            # a payment link sent to a customer
    "payments", metadata, _id(), _biz(),
    Column("account_id", UUID(as_uuid=True), ForeignKey("payment_accounts.id", ondelete="SET NULL")),
    Column("provider", String(20), nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("deal_id", UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("description", String(300), nullable=False),
    Column("token", String(40), nullable=False, unique=True),   # public page /p/<token>
    Column("provider_ref", String(200)),
    Column("url", Text),
    Column("status", String(12), nullable=False, server_default="pending"),
    Column("paid_at", DateTime(timezone=True)),
    _ts(),
    CheckConstraint("amount_minor > 0", name="amount"),
    CheckConstraint("status IN ('pending','paid','failed','cancelled')", name="status"),
)

automations = Table(                                         # "the day before an appointment, remind them"
    "automations", metadata, _id(), _biz(),
    Column("trigger", String(30), nullable=False),            # appointment.reminder | appointment.done | contact.created | deal.won
    Column("minutes", Integer, nullable=False),               # before (reminder) or after (the others)
    Column("message", Text, nullable=False),                  # {name} {business} {when} {link}
    Column("template", String(100)),                          # approved WhatsApp template when outside the 24 h window
    Column("kind", String(12), nullable=False, server_default="service"),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
    CheckConstraint("trigger IN ('appointment.reminder','appointment.done','contact.created','deal.won')", name="trigger"),
    CheckConstraint("minutes BETWEEN 0 AND 525600", name="minutes"),
)

campaigns = Table(                                           # a one-off message to everyone with a tag
    "campaigns", metadata, _id(), _biz(),
    Column("name", String(120), nullable=False),
    Column("tag", String(60)),                                 # NULL = every customer who agreed
    Column("message", Text, nullable=False),
    Column("template", String(100)),
    Column("send_at", DateTime(timezone=True), nullable=False),
    Column("status", String(12), nullable=False, server_default="scheduled"),
    Column("counts", JSONB, nullable=False, server_default=text("'{}'")),
    _ts(),
    CheckConstraint("status IN ('scheduled','sending','done','cancelled')", name="status"),
)

scheduled_messages = Table(                                  # the outbox both of them feed; sent by a job
    "scheduled_messages", metadata, _id(), _biz(),
    Column("automation_id", UUID(as_uuid=True), ForeignKey("automations.id", ondelete="CASCADE")),
    Column("campaign_id", UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE")),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("subject_id", UUID(as_uuid=True)),                 # the appointment / deal / contact it's about
    Column("send_at", DateTime(timezone=True), nullable=False),
    Column("status", String(12), nullable=False, server_default="pending"),
    Column("error", Text),
    Column("sent_message_id", UUID(as_uuid=True)),
    _ts(),
    CheckConstraint("status IN ('pending','sent','skipped','failed')", name="status"),
    UniqueConstraint("automation_id", "subject_id", name="uq_sched_auto"),
    UniqueConstraint("campaign_id", "contact_id", name="uq_sched_campaign"),
)
Index("ix_sched_due", scheduled_messages.c.send_at, postgresql_where=scheduled_messages.c.status == "pending")

events = Table(                                              # append-only audit trail of everything that happened
    "events", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _biz(),
    Column("type", String(60), nullable=False),
    Column("actor", String(100), nullable=False),            # api_key:<prefix>, webhook:<channel>, system
    Column("subject_id", UUID(as_uuid=True)),
    Column("data", JSONB, nullable=False, server_default=text("'{}'")),
    _ts(),
)
Index("ix_events_biz_id", events.c.business_id, events.c.id)

_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(config.DATABASE_URL(), pool_pre_ping=True, future=True)
    return _engine


def reset_engine():
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def tx():
    """One transaction: commit on success, roll back on any error."""
    with engine().begin() as conn:
        yield conn
