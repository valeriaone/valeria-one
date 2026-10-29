"""Database: SQLAlchemy Core tables. Every row that belongs to a business carries business_id, and
every query in services/ filters by it — one business can never read another's data."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer,
                        LargeBinary, MetaData, String, Table, Text, UniqueConstraint, create_engine, func, text)
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
    # what this business uses and what it calls things (see blocks.py); empty = everything on, the pack's words
    Column("features", JSONB, nullable=False, server_default=text("'{}'")),   # {"pipeline": false, …}
    Column("words", JSONB, nullable=False, server_default=text("'{}'")),      # {"contact": "Member", "stage:new": "Inquiry"}
    # set by whoever hosts the business (Partner API): paused = no sign-in, no API, nothing sent; messages still arrive
    Column("suspended_at", DateTime(timezone=True)),
    Column("suspended_reason", String(300)),
    Column("partner_ref", String(100), unique=True),                         # the host's own id for this customer
    # billing (only on a server that charges: VO_BILLING=on; self-hosted installs never charge)
    Column("plan", String(20), nullable=False, server_default="starter"),    # free | starter | pro
    Column("trial_ends_at", DateTime(timezone=True)),
    Column("paid_until", DateTime(timezone=True)),
    Column("billing_exempt", Boolean, nullable=False, server_default=text("false")),   # no charge (the server's own, friends)
    Column("billing_ref", String(12), unique=True),                         # VO-XXXXX: transfer reference + referral code
    Column("referred_by", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="SET NULL")),
    Column("stripe_customer_id", String(100)),
    Column("stripe_subscription_id", String(100)),
    Column("review_url", String(500)),                                       # the business's Google (or other) review page
    Column("promo", String(20)),                                             # launch = joined in the launch window (free months)
    Column("tax_id", String(40)),                                            # its tax number (TIN, GSTIN, VAT, RFC…) on quotes
    Column("powered_tag", Boolean, nullable=False, server_default=text("true")),   # "— Valeria One · link" on the first AI reply
    Column("studio_host", String(253), unique=True),                         # its own address for the design studio (studio.example.com)
    Column("ai_staff", JSONB, nullable=False, server_default=text("'{}'")),  # which AI helpers work here: {"coordinator": true, …}
    Column("apps_ready", JSONB, nullable=False, server_default=text("'[]'")),  # design apps whose account is set up: ["canva", …]
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
    Column("is_admin", Boolean, nullable=False, server_default=text("false")),   # runs this server (first account made at /setup)
    Column("must_change_password", Boolean, nullable=False, server_default=text("false")),   # set up by someone else: new password first
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
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL")),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("description", String(300), nullable=False),
    Column("token", String(40), nullable=False, unique=True),   # public page /p/<token>
    Column("provider_ref", String(200)),
    Column("attempts", JSONB, nullable=False, server_default=text("'[]'")),   # every phone prompt sent: [{"ref", "at"}]
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
                                                              # | payment.paid | contact.quiet (win-back: no visit in N days)
    Column("minutes", Integer, nullable=False),               # before (reminder) or after (the others); quiet: N days in minutes
    Column("message", Text, nullable=False),                  # {name} {business} {when} {link} (+ {offer} {book} {referral_link}…)
    Column("template", String(100)),                          # approved WhatsApp template when outside the 24 h window
    Column("kind", String(12), nullable=False, server_default="service"),
    Column("preset", String(20)),                             # a ready-made one from Grow: winback | referral (NULL = the owner's own)
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
    CheckConstraint("trigger IN ('appointment.reminder','appointment.done','contact.created','deal.won','payment.paid',"
                    "'contact.quiet')", name="trigger"),
    CheckConstraint("minutes BETWEEN 0 AND 525600", name="minutes"),
    CheckConstraint("preset IS NULL OR preset IN ('winback','referral')", name="preset"),
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

quotes = Table(                                              # a quote the customer accepts by e-signature
    "quotes", metadata, _id(), _biz(),
    Column("number", Integer, nullable=False),               # 1, 2, 3… per business
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("deal_id", UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL")),
    Column("title", String(200), nullable=False),
    Column("lines", JSONB, nullable=False),                   # [{"description", "qty", "unit_minor"}]
    Column("currency", String(3), nullable=False),
    Column("subtotal_minor", BigInteger, nullable=False),
    Column("tax_minor", BigInteger),                          # NULL = tax computed elsewhere (e.g. US sales tax)
    Column("total_minor", BigInteger, nullable=False),
    Column("tax_name", String(60), nullable=False, server_default=""),
    Column("tax_rate_bp", Integer),
    Column("notes", Text, nullable=False, server_default=""),
    Column("valid_until", Date, nullable=False),
    Column("token", String(40), nullable=False, unique=True),
    Column("doc_hash", String(64), nullable=False),           # sha256 of exactly what the customer sees and signs
    Column("status", String(10), nullable=False, server_default="sent"),
    Column("signed_name", String(200)),
    Column("signed_at", DateTime(timezone=True)),
    Column("signed_ip", String(64)),
    Column("signed_ua", String(300)),
    Column("decline_reason", Text),
    _ts(),
    UniqueConstraint("business_id", "number", name="uq_quote_number"),
    CheckConstraint("status IN ('sent','accepted','declined','cancelled')", name="status"),
)

page_views = Table(                                          # sandbox only: who is looking (no personal data kept)
    "page_views", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("path", String(300), nullable=False),
    Column("ref_host", String(200)),
    Column("visitor", String(16), nullable=False),           # hash of IP + secret, rotated daily: counts people, can't find them
    Column("meta_network", Boolean, nullable=False, server_default=text("false")),
    Column("ua_kind", String(20)),
    _ts(),
)

instance_kv = Table(                                         # small facts about this server (e.g. when background jobs last ran)
    "instance_kv", metadata,
    Column("key", String(60), primary_key=True),
    Column("value", JSONB, nullable=False, server_default=text("'{}'")),
    _ts("updated_at"),
)

review_requests = Table(                                     # "would you leave us a review?" — the same ask for everyone
    "review_requests", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("token", String(32), nullable=False, unique=True),
    Column("how", String(12), nullable=False),                                # manual | automatic
    Column("clicked_at", DateTime(timezone=True)),
    _ts(),
)

growth_settings = Table(                                     # Grow: win-back and bring-a-friend, in the owner's words
    "growth_settings", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("quiet_days", Integer, nullable=False, server_default=text("60")),     # "gone quiet" = nothing from them in N days
    Column("gap_days", Integer, nullable=False, server_default=text("180")),      # one win-back per customer per N days, at most
    Column("daily_cap", Integer, nullable=False, server_default=text("30")),      # win-backs per day, at most
    Column("offer", String(200), nullable=False, server_default=""),             # optional line: "10% off this week"
    Column("reward_referrer", String(200), nullable=False, server_default=""),   # what the customer who shares gets
    Column("reward_friend", String(200), nullable=False, server_default=""),     # what the friend gets
    Column("ran_on", Date),                                                      # the business's local day win-backs were last picked
    _ts(), _ts("updated_at"),
    CheckConstraint("quiet_days BETWEEN 14 AND 365", name="quiet_days"),
    CheckConstraint("gap_days BETWEEN 30 AND 730", name="gap_days"),
    CheckConstraint("daily_cap BETWEEN 1 AND 500", name="daily_cap"),
)

referral_links = Table(                                      # each customer's own "bring a friend" link /f/<code>
    "referral_links", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("code", String(16), nullable=False, unique=True),                    # random, unguessable; never a number in a row
    Column("opens", Integer, nullable=False, server_default=text("0")),
    Column("last_sent_at", DateTime(timezone=True)),                             # last time the link went to them in a message
    _ts(),
    UniqueConstraint("business_id", "contact_id", name="uq_referral_link"),
)

referrals = Table(                                           # a friend who came through a customer's link — and both rewards
    "referrals", metadata, _id(), _biz(),
    Column("link_id", UUID(as_uuid=True), ForeignKey("referral_links.id", ondelete="CASCADE"), nullable=False),
    Column("referrer_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("friend_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("how", String(12), nullable=False),                                   # booking | website | chat | form
    Column("reward_referrer", String(200), nullable=False, server_default=""),   # the rewards as they were that day
    Column("reward_friend", String(200), nullable=False, server_default=""),
    Column("referrer_given_at", DateTime(timezone=True)),
    Column("friend_given_at", DateTime(timezone=True)),
    _ts(),
    UniqueConstraint("business_id", "friend_id", name="uq_referral_friend"),    # a friend is brought once
    CheckConstraint("referrer_id <> friend_id", name="not_self"),
    CheckConstraint("how IN ('booking','website','chat','form')", name="how"),
)

billing_payments = Table(                                    # what a business paid THIS server for its plan
    "billing_payments", metadata, _id(), _biz(),
    Column("plan", String(20), nullable=False),
    Column("months", Integer, nullable=False),
    Column("amount_minor", BigInteger, nullable=False),                       # price + tax, in the currency's minor unit
    Column("tax_minor", BigInteger, nullable=False, server_default=text("0")),
    Column("currency", String(3), nullable=False),
    Column("method", String(12), nullable=False),                             # transfer | stripe
    Column("status", String(12), nullable=False, server_default="pending"),   # pending | paid | rejected
    Column("reference", String(24), nullable=False),
    Column("note", String(300)),
    Column("provider_id", String(120), unique=True),                          # Stripe invoice / session id (applied once)
    Column("decided_by", String(100)),
    Column("decided_at", DateTime(timezone=True)),
    _ts(),
    CheckConstraint("status IN ('pending','paid','rejected')", name="status"),
)

user_logins = Table(                                         # "continue with Facebook / Google": who that account is here
    "user_logins", metadata, _id(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("provider", String(20), nullable=False),                          # facebook | google
    Column("subject", String(200), nullable=False),                          # the provider's own id for the person
    Column("email", String(320)),
    _ts(),
    UniqueConstraint("provider", "subject"),
)
password_resets = Table(                                     # "forgot my password" links (1 hour, once)
    "password_resets", metadata, _id(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True)),
    _ts(),
)

partner_keys = Table(                                        # keys for a host's or agency's systems (Partner API)
    "partner_keys", metadata, _id(),
    Column("name", String(100), nullable=False),
    Column("prefix", String(24), nullable=False),
    Column("key_hash", String(64), nullable=False, unique=True),             # sha256; the key itself is never stored
    Column("last_used_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    _ts(),
)

instance_events = Table(                                     # what was done to the server itself, and by whom
    "instance_events", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("type", String(60), nullable=False),
    Column("actor", String(100), nullable=False),            # user:<id>, partner:<prefix>, host
    Column("data", JSONB, nullable=False, server_default=text("'{}'")),
    _ts(),
)

# ---------------------------------------------------------------- projects (any business that delivers work in stages)
projects = Table(                                            # one job for one customer: a home, a kitchen, an event, a build
    "projects", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("deal_id", UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")),
    Column("name", String(200), nullable=False),
    Column("stage", String(40), nullable=False),              # a project stage key from the industry pack
    Column("status", String(10), nullable=False, server_default="active"),
    Column("address", String(300), nullable=False, server_default=""),
    Column("brief", Text, nullable=False, server_default=""),  # what the customer wants, in their words
    Column("style", String(200), nullable=False, server_default=""),
    Column("budget_minor", BigInteger),
    Column("fee_minor", BigInteger),                          # the business's own fee for the work (design, labour)
    Column("markup_bp", Integer, nullable=False, server_default=text("0")),   # default margin on items it buys for them
    Column("currency", String(3), nullable=False),
    Column("starts_on", Date), Column("due_on", Date),
    Column("token", String(40), nullable=False, unique=True),   # the customer's portal /portal/<token>: no password
    Column("published", Boolean, nullable=False, server_default=text("false")),   # on the business's website as finished work
    Column("notes", Text, nullable=False, server_default=""),   # private to the team
    _ts(), _ts("updated_at"),
    CheckConstraint("status IN ('active','done','cancelled')", name="status"),
    CheckConstraint("markup_bp BETWEEN 0 AND 100000", name="markup"),
)
Index("ix_projects_biz", projects.c.business_id, projects.c.status)

project_rooms = Table(                                       # rooms or areas: kitchen, terrace, main bedroom
    "project_rooms", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(120), nullable=False),
    Column("width_cm", Integer), Column("length_cm", Integer), Column("height_cm", Integer),
    Column("notes", Text, nullable=False, server_default=""),
    Column("plan", JSONB, nullable=False, server_default=text("'{}'")),     # the 2D layout: {"items": [{label,x,y,w,h,r}]}
    Column("palette", JSONB, nullable=False, server_default=text("'[]'")),  # [{"hex": "#d8c3a5", "name": "Comex 123"}]
    Column("position", Integer, nullable=False, server_default=text("0")),
    _ts(),
)

files = Table(                                               # photos, renders, plans, PDFs: kept in the database, so backups hold them
    "files", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")),
    Column("room_id", UUID(as_uuid=True), ForeignKey("project_rooms.id", ondelete="SET NULL")),
    Column("kind", String(12), nullable=False),               # photo | render | plan | doc | canva | site
    Column("name", String(200), nullable=False, server_default=""),
    Column("mime", String(100), nullable=False),
    Column("size", Integer, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("thumb", LargeBinary),
    Column("caption", String(500), nullable=False, server_default=""),
    Column("shared", Boolean, nullable=False, server_default=text("false")),   # the customer sees it in their portal
    Column("public", Boolean, nullable=False, server_default=text("false")),   # on the website (a published project's pictures)
    Column("reaction", String(8)),                            # the customer's: like | dislike
    Column("comment", Text, nullable=False, server_default=""),
    Column("source_id", UUID(as_uuid=True)),                  # a render: the photo it was made from
    Column("created_by", String(100), nullable=False),
    _ts(),
    CheckConstraint("kind IN ('photo','render','plan','doc','canva','site')", name="kind"),
    CheckConstraint("reaction IS NULL OR reaction IN ('like','dislike')", name="reaction"),
)
Index("ix_files_biz_project", files.c.business_id, files.c.project_id)

boards = Table(                                              # mood boards: images, colours, notes and links, laid out freely
    "boards", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("room_id", UUID(as_uuid=True), ForeignKey("project_rooms.id", ondelete="SET NULL")),
    Column("title", String(200), nullable=False),
    Column("items", JSONB, nullable=False, server_default=text("'[]'")),
    Column("shared", Boolean, nullable=False, server_default=text("false")),
    Column("reaction", String(8)),
    Column("comment", Text, nullable=False, server_default=""),
    _ts(), _ts("updated_at"),
    CheckConstraint("reaction IS NULL OR reaction IN ('like','dislike')", name="reaction"),
)

suppliers = Table(
    "suppliers", metadata, _id(), _biz(),
    Column("name", String(200), nullable=False),
    Column("kind", String(60), nullable=False, server_default=""),   # furniture, fabrics, carpentry, installer…
    Column("contact_name", String(200), nullable=False, server_default=""),
    Column("phone", String(20)), Column("email", String(320)),
    Column("website", String(500), nullable=False, server_default=""),
    Column("lead_days", Integer),
    Column("notes", Text, nullable=False, server_default=""),
    Column("archived", Boolean, nullable=False, server_default=text("false")),
    _ts(),
)

catalog_items = Table(                                       # the business's own library: pieces and materials it uses again
    "catalog_items", metadata, _id(), _biz(),
    Column("supplier_id", UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL")),
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")),
    Column("category", String(20), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("sku", String(100), nullable=False, server_default=""),
    Column("url", String(500), nullable=False, server_default=""),
    Column("finish", String(200), nullable=False, server_default=""),
    Column("width_cm", Integer), Column("depth_cm", Integer), Column("height_cm", Integer),
    Column("cost_minor", BigInteger),                         # what the business pays
    Column("price_minor", BigInteger),                        # what it charges
    Column("lead_days", Integer),
    Column("archived", Boolean, nullable=False, server_default=text("false")),
    _ts(),
)

purchase_orders = Table(
    "purchase_orders", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("supplier_id", UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL")),
    Column("number", Integer, nullable=False),
    Column("status", String(10), nullable=False, server_default="draft"),
    Column("total_minor", BigInteger, nullable=False, server_default=text("0")),
    Column("paid_minor", BigInteger, nullable=False, server_default=text("0")),   # deposits and payments made to the supplier
    Column("currency", String(3), nullable=False),
    Column("expected_on", Date), Column("delivered_on", Date),
    Column("notes", Text, nullable=False, server_default=""),
    _ts(), _ts("updated_at"),
    UniqueConstraint("business_id", "number", name="uq_po_number"),
    CheckConstraint("status IN ('draft','sent','confirmed','delivered','cancelled')", name="status"),
)

project_items = Table(                                       # selections: each piece or material proposed for a room
    "project_items", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("room_id", UUID(as_uuid=True), ForeignKey("project_rooms.id", ondelete="SET NULL")),
    Column("catalog_id", UUID(as_uuid=True), ForeignKey("catalog_items.id", ondelete="SET NULL")),
    Column("supplier_id", UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL")),
    Column("po_id", UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="SET NULL")),
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")),
    Column("category", String(20), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("sku", String(100), nullable=False, server_default=""),
    Column("url", String(500), nullable=False, server_default=""),
    Column("finish", String(200), nullable=False, server_default=""),
    Column("width_cm", Integer), Column("depth_cm", Integer), Column("height_cm", Integer),
    Column("qty", Integer, nullable=False, server_default=text("1")),
    Column("cost_minor", BigInteger),
    Column("price_minor", BigInteger),                        # per unit, to the customer
    Column("lead_days", Integer),
    Column("status", String(10), nullable=False, server_default="proposed"),
    Column("client_note", Text, nullable=False, server_default=""),
    Column("decided_at", DateTime(timezone=True)),
    Column("position", Integer, nullable=False, server_default=text("0")),
    _ts(), _ts("updated_at"),
    CheckConstraint("qty BETWEEN 1 AND 100000", name="qty"),
    CheckConstraint("status IN ('proposed','approved','rejected','ordered','delivered','installed')", name="status"),
)
Index("ix_project_items_project", project_items.c.project_id)

tasks = Table(                                               # the team's to-dos, install checklists and punch lists
    "tasks", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")),
    Column("room_id", UUID(as_uuid=True), ForeignKey("project_rooms.id", ondelete="SET NULL")),
    Column("kind", String(8), nullable=False, server_default="task"),       # task | install | punch
    Column("title", String(300), nullable=False),
    Column("assignee_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    Column("due_on", Date),
    Column("done_at", DateTime(timezone=True)),
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")),
    Column("created_by", String(100), nullable=False),
    _ts(),
    CheckConstraint("kind IN ('task','install','punch')", name="kind"),
)
Index("ix_tasks_biz_open", tasks.c.business_id, tasks.c.done_at)

time_entries = Table(                                        # hours per person per project
    "time_entries", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    Column("on_date", Date, nullable=False),
    Column("minutes", Integer, nullable=False),
    Column("note", String(300), nullable=False, server_default=""),
    _ts(),
    CheckConstraint("minutes BETWEEN 1 AND 1440", name="minutes"),
)

project_updates = Table(                                     # the project's story: what happened, and what the customer was told
    "project_updates", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(12), nullable=False),               # note | stage | approved | rejected | comment | update | file | ai
    Column("body", Text, nullable=False, server_default=""),
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")),
    Column("shared", Boolean, nullable=False, server_default=text("false")),
    Column("by", String(100), nullable=False),
    _ts(),
)
Index("ix_project_updates_project", project_updates.c.project_id, project_updates.c.created_at)

app_links = Table(                                           # the business's OWN accounts on other apps (Canva), connected with OAuth
    "app_links", metadata, _id(), _biz(),
    Column("provider", String(20), nullable=False),           # canva
    Column("account_name", String(200), nullable=False, server_default=""),
    Column("tokens", JSONB, nullable=False),                  # {"access_token": enc, "secret": enc (refresh token)}
    Column("expires_at", DateTime(timezone=True)),
    Column("connected_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    UniqueConstraint("business_id", "provider", name="uq_app_link"),
)

canva_designs = Table(                                       # Canva designs started from a project, to bring back into it
    "canva_designs", metadata, _id(), _biz(),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("design_id", String(100), nullable=False),
    Column("title", String(255), nullable=False, server_default=""),
    Column("edit_url", Text, nullable=False, server_default=""),
    Column("imported_at", DateTime(timezone=True)),
    _ts(),
    UniqueConstraint("business_id", "design_id", name="uq_canva_design"),
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
