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
    _ts(), _ts("updated_at"),
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
    _ts(),
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
