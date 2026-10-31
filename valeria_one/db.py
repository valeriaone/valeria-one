"""Database: SQLAlchemy Core tables. Every row that belongs to a business carries business_id, and
every query in services/ filters by it — one business can never read another's data."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer,
                        LargeBinary, MetaData, String, Table, Text, Time, UniqueConstraint, create_engine, func, text)
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
    # kit01 (v0.19): the owner's own choices on top of the trade's kit (see kits.py): {"invoices": true, "posts": false};
    # a tool that is off is only hidden (its data stays). And the suggestions the owner said "no thanks" to, never shown again.
    Column("tools", JSONB, nullable=False, server_default=text("'{}'")),
    Column("tips_dismissed", JSONB, nullable=False, server_default=text("'[]'")),
    # ars01 (v0.20): the register's own tax, when the owner sets one: {"tax_bp": 825, "included": false}; empty = the country's
    Column("pos_settings", JSONB, nullable=False, server_default=text("'{}'")),
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
    Column("feed_gen", Integer, nullable=False, server_default=text("0")),   # sch01: their calendar link's version (+1 = new link, old one dead)
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
    Column("custom", JSONB, nullable=False, server_default=text("'{}'")),   # the owner's own fields: {"f_ab12cd": value} (custom_fields)
    Column("assignee_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),   # who on the team looks after them
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
    Column("source", String(20)),                              # mkt01 (v0.22): where the chat came from ("marketplace")
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
    Column("transcript", Text),                               # a voice note, as the business's own AI heard it
    Column("suggestion", Text),                               # a reply the AI suggests for it (the owner sends it, or not)
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
    Column("custom", JSONB, nullable=False, server_default=text("'{}'")),   # the owner's own fields (custom_fields)
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
    # sch01 (v0.23): a time picked in a chat — which chat (to move / cancel it there), which Marketplace item, and the
    # calendar file's SEQUENCE (+1 on every move or cancel, so phones replace the event instead of adding a second one)
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")),
    Column("item_id", UUID(as_uuid=True), ForeignKey("marketplace_items.id", ondelete="SET NULL")),
    Column("sequence", Integer, nullable=False, server_default=text("0")),
    CheckConstraint("ends_at > starts_at", name="times"),
    CheckConstraint("status IN ('booked','confirmed','done','no_show','cancelled')", name="status"),
)
Index("ix_appointments_conv", appointments.c.conversation_id, postgresql_where=appointments.c.conversation_id.isnot(None))

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
    Column("preset", String(20)),                             # ready-made: winback | referral (Grow), reminder (Valeria's
                                                              # "reply 1 to confirm") — NULL = the owner's own
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
    CheckConstraint("trigger IN ('appointment.reminder','appointment.done','contact.created','deal.won','payment.paid',"
                    "'contact.quiet')", name="trigger"),
    CheckConstraint("minutes BETWEEN 0 AND 525600", name="minutes"),
    CheckConstraint("preset IS NULL OR preset IN ('winback','referral','reminder')", name="preset"),
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
    # v0.18 (ghl01): the campaign builder — one channel, an audience from filters, the email's blocks, tracked links
    Column("channel", String(10)),                             # email | sms | whatsapp (NULL = the customer's usual channel, v0.6)
    Column("audience", JSONB, nullable=False, server_default=text("'{}'")),   # {"tags": [], "field": {key, value}, "stage", "source", "seen": {op, days}}
    Column("subject", String(200)),                            # email
    Column("blocks", JSONB, nullable=False, server_default=text("'[]'")),     # email: [{"t": heading|text|image|button, …}]
    Column("links", JSONB, nullable=False, server_default=text("'[]'")),      # the addresses a click may go to (/c/<token>/<n>)
    Column("track_opens", Boolean, nullable=False, server_default=text("false")),   # the email pixel: off unless the owner asks
    Column("by_ai", Boolean, nullable=False, server_default=text("false")),
    Column("created_by", String(100)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    _ts(),
    CheckConstraint("status IN ('draft','scheduled','sending','done','cancelled')", name="status"),
    CheckConstraint("channel IS NULL OR channel IN ('email','sms','whatsapp')", name="channel"),
)

sequences = Table(                                           # v0.18: drips — N steps with waits, for one customer at a time
    "sequences", metadata, _id(), _biz(),
    Column("name", String(120), nullable=False),
    Column("kind", String(10), nullable=False, server_default="marketing"),   # marketing (needs consent) | service (about their request)
    Column("trigger", String(20), nullable=False, server_default="manual"),   # manual | lead.new | form.submitted | tag.added | booking.made
    Column("trigger_arg", JSONB, nullable=False, server_default=text("'{}'")),   # {"form_id"} {"tag"}
    Column("steps", JSONB, nullable=False, server_default=text("'[]'")),      # [{"wait": minutes, "channel", "text", "subject", "blocks", "template"}]
    Column("exit_on", ARRAY(String(12)), nullable=False, server_default=text("'{reply,booking,unsubscribe}'")),
    Column("active", Boolean, nullable=False, server_default=text("false")),
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    CheckConstraint("kind IN ('marketing','service')", name="kind"),
    CheckConstraint("trigger IN ('manual','lead.new','form.submitted','tag.added','booking.made')", name="trigger"),
)

sequence_enrollments = Table(                                # one customer in one sequence (once: a sequence never starts twice for them)
    "sequence_enrollments", metadata, _id(), _biz(),
    Column("sequence_id", UUID(as_uuid=True), ForeignKey("sequences.id", ondelete="CASCADE"), nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("step", Integer, nullable=False, server_default=text("0")),          # the next step to send
    Column("next_at", DateTime(timezone=True)),
    Column("status", String(10), nullable=False, server_default="active"),
    Column("exit_reason", String(20)),
    Column("entered_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    UniqueConstraint("sequence_id", "contact_id", name="uq_enrollment"),
    CheckConstraint("status IN ('active','done','exited','stopped')", name="status"),
)
Index("ix_enrollments_due", sequence_enrollments.c.next_at, postgresql_where=sequence_enrollments.c.status == "active")

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
    # v0.18: a drip's step; the customer's own token for unsubscribe / open / click; what they did with it
    Column("enrollment_id", UUID(as_uuid=True), ForeignKey("sequence_enrollments.id", ondelete="CASCADE")),
    Column("step", Integer),
    Column("token", String(24), unique=True),
    Column("channel", String(20)),                            # the channel it went out on (for the numbers)
    Column("opened_at", DateTime(timezone=True)),
    Column("clicked_at", DateTime(timezone=True)),
    Column("unsubscribed_at", DateTime(timezone=True)),
    _ts(),
    CheckConstraint("status IN ('pending','sent','skipped','failed')", name="status"),
    UniqueConstraint("automation_id", "subject_id", name="uq_sched_auto"),
    UniqueConstraint("campaign_id", "contact_id", name="uq_sched_campaign"),
    UniqueConstraint("enrollment_id", "step", name="uq_sched_sequence"),
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
    Column("kind", String(12), nullable=False),               # photo | render | plan | doc | canva | site | post
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
    CheckConstraint("kind IN ('photo','render','plan','doc','canva','site','post')", name="kind"),
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
    # ars01 (v0.20): the same items are the business's stock — what it sells at the counter (Inventory, Point of sale)
    Column("in_inventory", Boolean, nullable=False, server_default=text("false")),   # listed in Inventory and the register
    Column("stock", Integer),                                 # how many there are now (NULL = not counted: a service)
    Column("low_at", Integer),                                # "running low" at this many or fewer
    Column("parent_id", UUID(as_uuid=True), ForeignKey("catalog_items.id", ondelete="SET NULL")),   # a variant of (one level)
    Column("variant", String(100), nullable=False, server_default=""),   # "Red / L", "500 g"
    CheckConstraint("low_at IS NULL OR low_at >= 0", name="low_at"),
)
Index("ix_catalog_items_inventory", catalog_items.c.business_id, catalog_items.c.in_inventory)

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
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),   # v0.16: a to-do about a customer
    Column("deal_id", UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")),
    Column("due_at", DateTime(timezone=True)),                # "reply within 1 hour" (due_on stays the day)
    Column("started_at", DateTime(timezone=True)),            # the board's "Doing" (done_at = Done)
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

# ---------------------------------------------------------------- v0.12: directory, Google Business Profile, posts
directory_listings = Table(                                  # "find a business near you": OFF until the owner lists the business
    "directory_listings", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("listed", Boolean, nullable=False, server_default=text("false")),
    Column("category", String(60), nullable=False, server_default=""),     # its own word for what it is ('' = the industry's name)
    Column("city", String(100), nullable=False, server_default=""),
    Column("area", String(100), nullable=False, server_default=""),        # state, region or neighbourhood
    Column("summary", String(300), nullable=False, server_default=""),     # one or two lines about it
    # what the owner chose to show: nothing personal is on the page unless it is ticked here
    Column("show_hours", Boolean, nullable=False, server_default=text("true")),
    Column("show_phone", Boolean, nullable=False, server_default=text("false")),
    Column("show_whatsapp", Boolean, nullable=False, server_default=text("false")),
    Column("show_address", Boolean, nullable=False, server_default=text("false")),
    _ts(), _ts("updated_at"),
)
Index("ix_directory_listed_city", directory_listings.c.listed, func.lower(directory_listings.c.city))

google_links = Table(                                        # a business's own Google Business Profile, connected with OAuth
    "google_links", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("account_email", String(320)),
    Column("tokens", JSONB, nullable=False),                  # {"refresh_token": enc} (crypto.seal)
    Column("account", String(100)),                           # accounts/123…
    Column("location", String(100)),                          # locations/456… (the profile the owner picked)
    Column("location_title", String(300)),
    Column("info", JSONB, nullable=False, server_default=text("'{}'")),       # name, phone, hours as Google has them (read-only)
    Column("insights", JSONB, nullable=False, server_default=text("'{}'")),   # the last 30 days, as last fetched
    Column("insights_at", DateTime(timezone=True)),
    Column("status", String(12), nullable=False, server_default="ok"),        # ok | reconnect (Google said invalid_grant)
    Column("last_error", Text),
    Column("backoff_until", DateTime(timezone=True)),         # Google said 429: nothing is asked of it before then
    Column("polled_at", DateTime(timezone=True)),             # reviews last fetched (the job: at most every 30 minutes)
    Column("reminded_at", DateTime(timezone=True)),           # "reconnect Google" email sent
    Column("connected_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    CheckConstraint("status IN ('ok','reconnect')", name="status"),
)

reviews = Table(                                             # reviews customers wrote (on Google), kept once each; answered here
    "reviews", metadata, _id(), _biz(),
    Column("source", String(20), nullable=False),             # google
    Column("external_id", String(200), nullable=False),
    Column("author", String(200), nullable=False, server_default=""),
    Column("rating", Integer),
    Column("comment", Text, nullable=False, server_default=""),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("reply", Text),                                    # the answer as published on Google
    Column("replied_at", DateTime(timezone=True)),
    Column("draft", Text),                                    # an answer waiting for the owner's OK (never sent without it)
    Column("draft_by_ai", Boolean, nullable=False, server_default=text("false")),
    Column("reply_error", Text),
    _ts(), _ts("updated_at"),
    UniqueConstraint("business_id", "source", "external_id", name="uq_review"),
    CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating"),
)

posts = Table(                                               # a post for the business's Facebook Page, Instagram and Google profile
    "posts", metadata, _id(), _biz(),
    Column("kind", String(12), nullable=False, server_default="idea"),       # idea | offer | product | holiday | review
    Column("idea", Text, nullable=False, server_default=""),                 # what the owner typed
    Column("networks", ARRAY(String(20)), nullable=False, server_default=text("'{}'")),   # facebook | instagram | google
    Column("texts", JSONB, nullable=False, server_default=text("'{}'")),     # {network: the words for that network}
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")),   # the picture (JPEG/PNG)
    Column("status", String(12), nullable=False, server_default="draft"),
    Column("publish_at", DateTime(timezone=True)),
    Column("claimed_at", DateTime(timezone=True)),            # a worker took it (status publishing): nobody else will
    Column("published_at", DateTime(timezone=True)),
    Column("results", JSONB, nullable=False, server_default=text("'{}'")),   # {network: {"id", "url", "error", "at"}}
    Column("by_ai", Boolean, nullable=False, server_default=text("false")),
    Column("created_by", String(100), nullable=False),
    # ars02 (v0.21): the networks the OWNER posts on by hand (not connected, or no posting door): at the time they get a
    # reminder with the words to copy; results[net] = {"hand": true, "due_at", "done_at"} — never published by us
    Column("by_hand", ARRAY(String(20)), nullable=False, server_default=text("'{}'")),
    _ts(), _ts("updated_at"),
    CheckConstraint("status IN ('draft','scheduled','publishing','published','failed','cancelled')", name="status"),
    CheckConstraint("kind IN ('idea','offer','product','holiday','review')", name="kind"),
)
Index("ix_posts_due", posts.c.publish_at, postgresql_where=posts.c.status == "scheduled")

# ---------------------------------------------------------------- v0.13: Valeria, the owner's assistant by chat
owner_numbers = Table(                                       # the owner's and staff's OWN phones/chats: what they write is a command
    "owner_numbers", metadata, _id(), _biz(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    Column("name", String(200), nullable=False, server_default=""),
    Column("channel", String(20)),                            # phone (any phone channel) | telegram | sandbox | …; NULL while pairing
    Column("address", String(320)),                           # E.164 for phone; the chat id for the others
    Column("role", String(10), nullable=False, server_default="owner"),
    Column("perms", ARRAY(String(20)), nullable=False, server_default=text("'{}'")),   # staff: what they may ask (owner: all)
    Column("lang", String(10)),                               # the language Valeria speaks to this person
    Column("pair_code", String(12), unique=True),             # "V-482913": sent from the phone once, links it
    Column("pair_expires_at", DateTime(timezone=True)),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("last_seen_at", DateTime(timezone=True)),
    _ts(),
    UniqueConstraint("business_id", "channel", "address", name="uq_owner_number"),
    CheckConstraint("role IN ('owner','staff')", name="role"),
)

assistant_settings = Table(                                  # how Valeria works for this business (one row; defaults until saved)
    "assistant_settings", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("paused_at", DateTime(timezone=True)),             # "Valeria, stop": nothing on her own until "resume"
    Column("autonomy", JSONB, nullable=False, server_default=text("'{}'")),       # {"review_ask": true}: "do it yourself" per kind
    Column("style", JSONB, nullable=False, server_default=text("'{}'")),          # how the owner sounds, in plain words
    Column("style_learned_on", Date),
    Column("morning", Boolean, nullable=False, server_default=text("true")),      # the 8:00 summary
    Column("weekly", Boolean, nullable=False, server_default=text("true")),       # Monday's proof
    Column("slowday", Boolean, nullable=False, server_default=text("true")),      # "tomorrow looks quiet"
    Column("holidays", Boolean, nullable=False, server_default=text("true")),
    Column("holidays_skip", ARRAY(String(60)), nullable=False, server_default=text("'{}'")),   # the ones the owner turned off
    Column("answers", Boolean, nullable=False, server_default=text("false")),     # 24/7 answers without AI (rules, the owner's facts)
    Column("reminder_hours", Integer, nullable=False, server_default=text("24")),
    Column("loyalty_every", Integer),                         # every Nth visit gets loyalty_reward (NULL = off)
    Column("loyalty_reward", String(200), nullable=False, server_default=""),
    Column("unanswered", Integer, nullable=False, server_default=text("0")),      # questions in a row the owner didn't answer
    Column("quiet_until", DateTime(timezone=True)),           # never nag: ignored twice = quiet until the next day
    _ts(), _ts("updated_at"),
    CheckConstraint("reminder_hours BETWEEN 1 AND 72", name="reminder_hours"),
    CheckConstraint("loyalty_every IS NULL OR loyalty_every BETWEEN 2 AND 100", name="loyalty_every"),
)

assistant_actions = Table(                                   # what Valeria asked, did or will do: her questions, her log, undo
    "assistant_actions", metadata, _id(), _biz(),
    Column("kind", String(20), nullable=False),               # reply | promo | review_ask | paylink | pay_reminder | booking_move …
    Column("status", String(10), nullable=False, server_default="asked"),
    Column("summary", Text, nullable=False, server_default=""),   # one line for the owner
    Column("payload", JSONB, nullable=False, server_default=text("'{}'")),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("number_id", UUID(as_uuid=True), ForeignKey("owner_numbers.id", ondelete="SET NULL")),   # who it was asked of / by
    Column("by", String(100), nullable=False),                # valeria | owner:<number id> | user:<id>
    Column("dedupe", String(120)),                            # one per thing: morning:2026-10-29, holiday:MX:muertos:2026
    Column("prompted_at", DateTime(timezone=True)),           # the question went to the owner's phone
    Column("send_at", DateTime(timezone=True)),               # approved: goes out then (until then "undo" stops it)
    Column("decided_at", DateTime(timezone=True)),
    Column("done_at", DateTime(timezone=True)),
    Column("result", JSONB, nullable=False, server_default=text("'{}'")),
    _ts(),
    UniqueConstraint("business_id", "dedupe", name="uq_assistant_dedupe"),
    CheckConstraint("status IN ('asked','queued','running','done','declined','later','undone','failed','expired')", name="status"),
)
Index("ix_assistant_actions_due", assistant_actions.c.send_at, postgresql_where=assistant_actions.c.status == "queued")
Index("ix_assistant_actions_biz", assistant_actions.c.business_id, assistant_actions.c.created_at)

assistant_messages = Table(                                  # the owner's chat with Valeria (never in the customer inbox)
    "assistant_messages", metadata, _id(), _biz(),
    Column("number_id", UUID(as_uuid=True), ForeignKey("owner_numbers.id", ondelete="CASCADE")),
    Column("channel_account_id", UUID(as_uuid=True), ForeignKey("channel_accounts.id", ondelete="SET NULL")),
    Column("direction", String(3), nullable=False),
    Column("body", Text, nullable=False, server_default=""),
    Column("media", JSONB, nullable=False, server_default=text("'[]'")),
    Column("transcript", Text),
    Column("provider_id", String(200)),
    Column("status", String(12), nullable=False, server_default="received"),
    Column("handled_at", DateTime(timezone=True)),            # answered (claimed once: a replayed webhook is never answered twice)
    _ts(),
    CheckConstraint("direction IN ('in','out')", name="direction"),
)
Index("uq_assistant_messages_provider", assistant_messages.c.business_id, assistant_messages.c.provider_id, unique=True,
      postgresql_where=assistant_messages.c.provider_id.isnot(None))

style_samples = Table(                                       # Valeria's draft and what the owner actually sent: how she learns their voice
    "style_samples", metadata, _id(), _biz(),
    Column("kind", String(12), nullable=False),               # reply | post | assistant
    Column("draft", Text, nullable=False),
    Column("sent", Text, nullable=False),
    Column("learned", Boolean, nullable=False, server_default=text("false")),
    _ts(),
)

loyalty_visits = Table(                                      # one visit per customer per local day (a booking done, a payment, a tap)
    "loyalty_visits", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("on_date", Date, nullable=False),
    Column("source", String(12), nullable=False),             # booking | payment | manual
    Column("ref_id", UUID(as_uuid=True)),
    _ts(),
    UniqueConstraint("business_id", "contact_id", "on_date", name="uq_loyalty_visit"),
)

loyalty_rewards = Table(                                     # earned on the Nth visit; the owner marks it used
    "loyalty_rewards", metadata, _id(), _biz(),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("visit_number", Integer, nullable=False),
    Column("reward", String(200), nullable=False),
    Column("used_at", DateTime(timezone=True)),
    Column("used_by", String(100)),
    _ts(),
    UniqueConstraint("business_id", "contact_id", "visit_number", name="uq_loyalty_reward"),
)

helpers = Table(                                             # people and agencies who set Valeria One up for a business (their own work)
    "helpers", metadata, _id(),
    Column("name", String(200), nullable=False),
    Column("company", String(200), nullable=False, server_default=""),
    Column("email", String(320), nullable=False),
    Column("phone", String(30), nullable=False, server_default=""),
    Column("country", String(2), nullable=False),
    Column("city", String(100), nullable=False, server_default=""),
    Column("languages", ARRAY(String(10)), nullable=False, server_default=text("'{}'")),
    Column("about", String(500), nullable=False, server_default=""),
    Column("token_hash", String(64), nullable=False, unique=True),          # their private dashboard link (sha256; never stored)
    Column("approved_at", DateTime(timezone=True)),                         # the server's admin approved them (NULL = not shown)
    Column("approved_by", String(100)),
    Column("lead_price_minor", BigInteger, nullable=False, server_default=text("0")),   # recorded only: nothing is ever charged
    Column("currency", String(3), nullable=False, server_default="USD"),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
)
Index("uq_helpers_email", func.lower(helpers.c.email), unique=True)

helper_leads = Table(                                        # "help me set it up": what the owner agreed to share, with whom
    "helper_leads", metadata, _id(), _biz(),
    Column("helper_id", UUID(as_uuid=True), ForeignKey("helpers.id", ondelete="SET NULL")),   # NULL = no helper yet ("soon")
    Column("status", String(10), nullable=False, server_default="sent"),
    Column("shared", JSONB, nullable=False, server_default=text("'{}'")),    # exactly what the helper sees: name, business, city, needs
    Column("consent_text", Text, nullable=False),
    Column("consent_at", DateTime(timezone=True), nullable=False),
    Column("consent_by", String(100), nullable=False),
    Column("free", Boolean, nullable=False, server_default=text("true")),    # one of the helper's first 5
    Column("price_minor", BigInteger, nullable=False, server_default=text("0")),   # recorded, never charged
    Column("currency", String(3), nullable=False, server_default="USD"),
    Column("rating", Integer),
    Column("rated_at", DateTime(timezone=True)),
    Column("decided_at", DateTime(timezone=True)),
    _ts(),
    CheckConstraint("status IN ('waiting','sent','accepted','declined','done')", name="status"),
    CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating"),
)

# ---------------------------------------------------------------- v0.16: the owner's own fields, recipes, board, forms
custom_fields = Table(                                       # "Birthday", "Budget", "Size": the owner's own fields on customers and deals
    "custom_fields", metadata, _id(), _biz(),
    Column("object", String(8), nullable=False),              # contact | deal
    Column("key", String(24), nullable=False),                # f_ab12cd: where the value lives in contacts.custom / deals.custom
    Column("label", String(60), nullable=False),
    Column("kind", String(10), nullable=False),               # text number money date yesno choice phone link
    Column("options", ARRAY(String(60)), nullable=False, server_default=text("'{}'")),   # choice: the list to pick from
    Column("show_in_list", Boolean, nullable=False, server_default=text("false")),
    Column("show_on_card", Boolean, nullable=False, server_default=text("false")),       # on the pipeline board's cards
    Column("position", Integer, nullable=False, server_default=text("0")),
    Column("deleted_at", DateTime(timezone=True)),            # deleted: values kept 30 days so it can be undone, then purged
    _ts(),
    UniqueConstraint("business_id", "object", "key", name="uq_custom_field"),
    CheckConstraint("object IN ('contact','deal')", name="object"),
    CheckConstraint("kind IN ('text','number','money','date','yesno','choice','phone','link')", name="kind"),
)

recipes = Table(                                             # "when this happens, do that" — ready-made or the owner's own
    "recipes", metadata, _id(), _biz(),
    Column("preset", String(30)),                             # the gallery's recipe it came from (NULL = made in the builder)
    Column("name", String(160), nullable=False, server_default=""),
    Column("trigger", String(20), nullable=False),            # lead.new | deal.stage | booking.made | field.changed | form.submitted | contact.quiet
    Column("trigger_arg", JSONB, nullable=False, server_default=text("'{}'")),   # {"stage": "won"} {"hours": 2} {"form_id"} {"key", "value"}
    Column("condition", JSONB, nullable=False, server_default=text("'{}'")),     # {} or {"kind": tag|field|source, "key"?, "value"}
    Column("actions", JSONB, nullable=False, server_default=text("'[]'")),       # 1-3: [{"do": "task", "title": "Reply", "hours": 1}, …]
    Column("ask_first", Boolean, nullable=False, server_default=text("true")),   # messages to customers wait for the owner's yes
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    CheckConstraint("trigger IN ('lead.new','deal.stage','booking.made','field.changed','form.submitted','contact.quiet')",
                    name="trigger"),
)

recipe_runs = Table(                                         # "what happened": each time a recipe ran, once per thing that set it off
    "recipe_runs", metadata, _id(), _biz(),
    Column("recipe_id", UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False),
    Column("dedupe", String(120), nullable=False),            # ev:<event id> | quiet:<conversation>:<last message> — never twice
    Column("subject_id", UUID(as_uuid=True)),                 # the contact / deal / appointment it was about
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("status", String(10), nullable=False),
    Column("lines", JSONB, nullable=False, server_default=text("'[]'")),         # what it did, in words: [{"k": "rc_l_task", "p": {…}}]
    _ts(),
    UniqueConstraint("recipe_id", "dedupe", name="uq_recipe_run"),
    CheckConstraint("status IN ('done','failed','loop')", name="status"),
)
Index("ix_recipe_runs_biz", recipe_runs.c.business_id, recipe_runs.c.created_at)
Index("ix_recipe_runs_subject", recipe_runs.c.recipe_id, recipe_runs.c.subject_id, recipe_runs.c.created_at)

forms = Table(                                               # the owner's own forms: a public link /form/<slug>, an embed, the website
    "forms", metadata, _id(), _biz(),
    Column("slug", String(80), nullable=False, unique=True),
    Column("name", String(120), nullable=False),
    Column("intro", String(500), nullable=False, server_default=""),
    Column("fields", JSONB, nullable=False, server_default=text("'[]'")),        # [{"key": "phone" | "cf:f_ab12cd", "required": true}]
    Column("thanks", String(500), nullable=False, server_default=""),
    Column("stage", String(40)),                              # a deal lands in this stage (NULL = no deal)
    Column("tag", String(60)),
    Column("on_site", Boolean, nullable=False, server_default=text("false")),    # a button on its website and directory listing
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("submissions", Integer, nullable=False, server_default=text("0")),
    _ts(), _ts("updated_at"),
)

form_submissions = Table(                                    # each form sent: what they wrote and what they agreed to (kept apart)
    "form_submissions", metadata, _id(), _biz(),
    Column("form_id", UUID(as_uuid=True), ForeignKey("forms.id", ondelete="CASCADE"), nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("deal_id", UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")),
    Column("data", JSONB, nullable=False, server_default=text("'{}'")),
    Column("request_consent", Boolean, nullable=False),       # "you may contact me about this request" (required to send)
    Column("marketing_consent", Boolean, nullable=False, server_default=text("false")),   # "send me offers" (optional, its own box)
    _ts(),
)
Index("ix_form_submissions_form", form_submissions.c.form_id, form_submissions.c.created_at)

# ---------------------------------------------------------------- v0.18 (ghl01): workflows, the phone agent, invoices, setups, the app
workflows = Table(                                           # "when this, then these steps": a step up from recipes (waits, if/else, a goal)
    "workflows", metadata, _id(), _biz(),
    Column("name", String(160), nullable=False, server_default=""),
    Column("trigger", String(20), nullable=False),
    Column("trigger_arg", JSONB, nullable=False, server_default=text("'{}'")),
    Column("steps", JSONB, nullable=False, server_default=text("'[]'")),     # a list; an "if" step holds "yes" and "no" lists
    Column("goal", String(10)),                               # booked | paid | replied: reached = the run stops there
    Column("active", Boolean, nullable=False, server_default=text("false")),
    Column("template", String(30)),                           # the gallery's workflow it came from
    Column("recipe_id", UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="SET NULL")),   # "open this recipe as a workflow"
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    CheckConstraint("trigger IN ('lead.new','deal.stage','booking.made','booking.done','booking.no_show','field.changed',"
                    "'form.submitted','tag.added','payment.paid','contact.quiet','contact.inactive','date.field','manual')",
                    name="trigger"),
    CheckConstraint("goal IS NULL OR goal IN ('booked','paid','replied')", name="goal"),
)

workflow_runs = Table(                                       # one customer going through one workflow: where they are, what happened
    "workflow_runs", metadata, _id(), _biz(),
    Column("workflow_id", UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("deal_id", UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")),
    Column("subject_id", UUID(as_uuid=True)),
    Column("dedupe", String(120), nullable=False),            # ev:<event id> | date:<key>:<year> | … — never twice
    Column("status", String(10), nullable=False, server_default="running"),
    Column("pointer", JSONB, nullable=False, server_default=text("'[0]'")),  # the next step: [2, "yes", 0] = step 2's yes-list, first step
    Column("wake_at", DateTime(timezone=True)),
    Column("steps_done", Integer, nullable=False, server_default=text("0")),
    Column("history", JSONB, nullable=False, server_default=text("'[]'")),   # [{"at", "k", "p"}] in words, like recipe runs
    _ts(), _ts("updated_at"),
    UniqueConstraint("workflow_id", "dedupe", name="uq_workflow_run"),
    CheckConstraint("status IN ('running','waiting','done','goal','failed','loop','stopped')", name="status"),
)
Index("ix_workflow_runs_due", workflow_runs.c.wake_at, postgresql_where=workflow_runs.c.status == "waiting")
Index("ix_workflow_runs_contact", workflow_runs.c.business_id, workflow_runs.c.contact_id)

voice_agents = Table(                                        # the phone agent: on the business's OWN Twilio number, with its OWN AI key
    "voice_agents", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("greeting", Text, nullable=False, server_default=""),
    Column("can_book", Boolean, nullable=False, server_default=text("true")),
    Column("can_message", Boolean, nullable=False, server_default=text("true")),
    Column("transfer_to", String(20)),                        # the owner's phone (E.164): "let me put you through"
    Column("record", Boolean, nullable=False, server_default=text("false")),   # only with the announcement first
    _ts(), _ts("updated_at"),
)

calls = Table(                                               # each call the agent took: what was said, what came of it
    "calls", metadata, _id(), _biz(),
    Column("channel_account_id", UUID(as_uuid=True), ForeignKey("channel_accounts.id", ondelete="SET NULL")),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("call_sid", String(64), nullable=False),
    Column("caller", String(32), nullable=False, server_default=""),
    Column("status", String(8), nullable=False, server_default="live"),
    Column("outcome", String(10)),                            # answered | booked | message | transfer | callback | hangup
    Column("turns", JSONB, nullable=False, server_default=text("'[]'")),     # [{"who": "caller"|"agent", "text", "at"}]
    Column("state", JSONB, nullable=False, server_default=text("'{}'")),     # a booking in progress
    Column("summary", Text, nullable=False, server_default=""),
    Column("recording_url", Text),
    Column("appointment_id", UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")),
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL")),
    _ts("started_at"),
    Column("ended_at", DateTime(timezone=True)),
    UniqueConstraint("business_id", "call_sid", name="uq_call_sid"),
    CheckConstraint("status IN ('live','done')", name="status"),
)

plans = Table(                                               # memberships / subscriptions the business sells: an amount every period
    "plans", metadata, _id(), _biz(),
    Column("name", String(120), nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("interval", String(5), nullable=False, server_default="month"),
    Column("every", Integer, nullable=False, server_default=text("1")),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
    CheckConstraint("amount_minor > 0", name="amount"),
    CheckConstraint("interval IN ('week','month','year')", name="interval"),
    CheckConstraint("every BETWEEN 1 AND 12", name="every"),
)

subscriptions = Table(                                       # a customer on a plan: an invoice (and its pay link) each period
    "subscriptions", metadata, _id(), _biz(),
    Column("plan_id", UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(10), nullable=False, server_default="active"),
    Column("started_on", Date, nullable=False),
    Column("next_on", Date, nullable=False),                  # the day the next invoice is made (the business's own day)
    Column("auto_send", Boolean, nullable=False, server_default=text("true")),   # the owner set it up: each invoice goes out by itself
    Column("paused_at", DateTime(timezone=True)),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    CheckConstraint("status IN ('active','paused','cancelled')", name="status"),
)
Index("ix_subscriptions_due", subscriptions.c.next_on, postgresql_where=subscriptions.c.status == "active")

invoices = Table(                                            # what a customer owes: numbered per business, tax from the country pack
    "invoices", metadata, _id(), _biz(),
    Column("number", Integer, nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("quote_id", UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="SET NULL")),
    Column("subscription_id", UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL")),
    Column("period_start", Date),                             # a subscription's period (one invoice per period, never two)
    Column("title", String(200), nullable=False),
    Column("lines", JSONB, nullable=False),                   # [{"description", "qty", "unit_minor"}]
    Column("currency", String(3), nullable=False),
    Column("subtotal_minor", BigInteger, nullable=False),
    Column("tax_minor", BigInteger),                          # NULL = tax computed elsewhere (US sales tax)
    Column("total_minor", BigInteger, nullable=False),
    Column("tax_name", String(60), nullable=False, server_default=""),
    Column("tax_rate_bp", Integer),
    Column("notes", Text, nullable=False, server_default=""),
    Column("issued_on", Date, nullable=False),
    Column("due_on", Date, nullable=False),
    Column("status", String(8), nullable=False, server_default="draft"),
    Column("token", String(40), nullable=False, unique=True),   # the customer's page /inv/<token>
    Column("payment_id", UUID(as_uuid=True), ForeignKey("payments.id", ondelete="SET NULL")),   # its pay link (the business's own account)
    Column("sent_at", DateTime(timezone=True)),
    Column("paid_at", DateTime(timezone=True)),
    Column("reminded_at", DateTime(timezone=True)),
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    UniqueConstraint("business_id", "number", name="uq_invoice_number"),
    UniqueConstraint("subscription_id", "period_start", name="uq_invoice_period"),
    CheckConstraint("status IN ('draft','sent','overdue','paid','void')", name="status"),
    CheckConstraint("total_minor >= 0", name="total"),
)
Index("ix_invoices_biz_status", invoices.c.business_id, invoices.c.status)

# ---------------------------------------------------------------- ars01 (v0.20): point of sale, stock, staff, books
pos_sales = Table(                                           # one sale at the counter: never deleted (a mistake is voided, a return refunded)
    "pos_sales", metadata, _id(), _biz(),
    Column("number", Integer, nullable=False),                # 1, 2, 3… per business
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("staff_id", UUID(as_uuid=True), ForeignKey("staff_members.id", ondelete="SET NULL")),   # who sold it
    Column("lines", JSONB, nullable=False),                   # [{"item_id", "description", "qty", "unit_minor"}] (prices as sold)
    Column("currency", String(3), nullable=False),
    Column("subtotal_minor", BigInteger, nullable=False),
    Column("discount_minor", BigInteger, nullable=False, server_default=text("0")),
    Column("tax_minor", BigInteger, nullable=False, server_default=text("0")),
    Column("total_minor", BigInteger, nullable=False),        # what the customer pays
    Column("tax_name", String(60), nullable=False, server_default=""),
    Column("tax_rate_bp", Integer, nullable=False, server_default=text("0")),
    Column("tax_included", Boolean, nullable=False, server_default=text("true")),   # the prices already had the tax in them
    Column("method", String(8), nullable=False),              # cash | link (their own pay link / QR) | card (their own machine) | other
    Column("cash_given_minor", BigInteger),
    Column("change_minor", BigInteger),
    Column("payment_id", UUID(as_uuid=True), ForeignKey("payments.id", ondelete="SET NULL")),   # the pay link (their own account)
    Column("status", String(8), nullable=False),              # paid | pending (link not paid yet) | void | refunded
    Column("token", String(40), nullable=False, unique=True), # the customer's receipt /receipt/<token>
    Column("note", String(300), nullable=False, server_default=""),
    Column("day", Date, nullable=False),                      # the business's own day of the sale (end-of-day count)
    Column("paid_at", DateTime(timezone=True)),
    Column("voided_at", DateTime(timezone=True)), Column("refunded_at", DateTime(timezone=True)),
    Column("refund_day", Date),                               # the business's own day of the refund
    Column("undo_reason", String(300), nullable=False, server_default=""),
    Column("created_by", String(100), nullable=False),
    _ts(),
    UniqueConstraint("business_id", "number", name="uq_pos_sale_number"),
    CheckConstraint("method IN ('cash','link','card','other')", name="method"),
    CheckConstraint("status IN ('paid','pending','void','refunded')", name="status"),
    CheckConstraint("subtotal_minor >= 0 AND discount_minor >= 0 AND discount_minor <= subtotal_minor AND tax_minor >= 0 "
                    "AND total_minor >= 0", name="amounts"),
    CheckConstraint("change_minor IS NULL OR change_minor >= 0", name="change"),
)
Index("ix_pos_sales_biz_day", pos_sales.c.business_id, pos_sales.c.day)

pos_days = Table(                                            # end of day: the cash counted against what the register expected
    "pos_days", metadata, _id(), _biz(),
    Column("day", Date, nullable=False),
    Column("float_minor", BigInteger, nullable=False, server_default=text("0")),     # cash in the drawer when the day began
    Column("expected_minor", BigInteger, nullable=False),     # float + cash sales − cash refunds
    Column("counted_minor", BigInteger, nullable=False),
    Column("totals", JSONB, nullable=False, server_default=text("'{}'")),   # the day by method, as it was when closed
    Column("note", String(300), nullable=False, server_default=""),
    Column("closed_by", String(100), nullable=False),
    _ts(),                                                    # closed again = a new row (a recount); the latest is the day's
    CheckConstraint("float_minor >= 0 AND counted_minor >= 0", name="amounts"),
)

stock_moves = Table(                                         # every change to a stock count, and why (the count itself is catalog_items.stock)
    "stock_moves", metadata, _id(), _biz(),
    Column("item_id", UUID(as_uuid=True), ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False),
    Column("delta", Integer, nullable=False),
    Column("stock_after", Integer, nullable=False),
    Column("reason", String(10), nullable=False),
    Column("sale_id", UUID(as_uuid=True), ForeignKey("pos_sales.id", ondelete="SET NULL")),
    Column("note", String(300), nullable=False, server_default=""),
    Column("created_by", String(100), nullable=False),
    _ts(),
    CheckConstraint("reason IN ('start','received','damaged','count','sale','refund','void','import')", name="reason"),
)
Index("ix_stock_moves_item", stock_moves.c.item_id, stock_moves.c.created_at)

staff_members = Table(                                       # the team on the schedule; with their own sign-in (users) to clock in
    "staff_members", metadata, _id(), _biz(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    Column("name", String(200), nullable=False),
    Column("phone", String(30), nullable=False, server_default=""),
    Column("job", String(80), nullable=False, server_default=""),     # what they do: "Stylist", "Cook"
    Column("hourly_minor", BigInteger),                       # pay per hour (recorded only: payroll is the accountant's)
    Column("per_shift_minor", BigInteger),                    # or a fixed amount per day worked
    Column("commission_bp", Integer),                         # or a share of what they sold (1000 = 10 %)
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(),
    CheckConstraint("hourly_minor IS NULL OR hourly_minor >= 0", name="hourly"),
    CheckConstraint("per_shift_minor IS NULL OR per_shift_minor >= 0", name="per_shift"),
    CheckConstraint("commission_bp IS NULL OR commission_bp BETWEEN 0 AND 10000", name="commission"),
)
Index("uq_staff_members_user", staff_members.c.business_id, staff_members.c.user_id, unique=True,
      postgresql_where=staff_members.c.user_id.isnot(None))

shifts = Table(                                              # the weekly schedule: who works when
    "shifts", metadata, _id(), _biz(),
    Column("staff_id", UUID(as_uuid=True), ForeignKey("staff_members.id", ondelete="CASCADE"), nullable=False),
    Column("day", Date, nullable=False),
    Column("starts", Time, nullable=False),
    Column("ends", Time, nullable=False),                     # earlier than starts = ends the next day (a night shift)
    Column("note", String(200), nullable=False, server_default=""),
    Column("created_by", String(100), nullable=False),
    _ts(),
)
Index("ix_shifts_biz_day", shifts.c.business_id, shifts.c.day)

clock_entries = Table(                                       # clocked in / out, from their own phone; the owner's fixes keep the old times
    "clock_entries", metadata, _id(), _biz(),
    Column("staff_id", UUID(as_uuid=True), ForeignKey("staff_members.id", ondelete="CASCADE"), nullable=False),
    Column("in_at", DateTime(timezone=True), nullable=False),
    Column("out_at", DateTime(timezone=True)),
    Column("source", String(6), nullable=False, server_default="self"),   # self (they clocked) | owner (the owner wrote it)
    Column("note", String(300), nullable=False, server_default=""),
    Column("history", JSONB, nullable=False, server_default=text("'[]'")),   # each fix: [{"at", "by", "note", "in_at", "out_at"}]
    Column("voided", Boolean, nullable=False, server_default=text("false")),
    _ts(),
    CheckConstraint("out_at IS NULL OR out_at > in_at", name="order"),
    CheckConstraint("source IN ('self','owner')", name="source"),
)
Index("ix_clock_entries_biz_in", clock_entries.c.business_id, clock_entries.c.in_at)

book_entries = Table(                                        # Books: money out (expenses) and other money in, written by hand
    "book_entries", metadata, _id(), _biz(),                 # (sales, paid invoices and pay links are read from where they are)
    Column("direction", String(3), nullable=False),           # in | out
    Column("on_date", Date, nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("category", String(20), nullable=False),
    Column("description", String(300), nullable=False, server_default=""),
    Column("supplier_id", UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL")),
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")),   # the receipt's photo
    Column("voided_at", DateTime(timezone=True)),             # a mistake is voided (kept, not counted), never deleted
    Column("void_reason", String(300), nullable=False, server_default=""),
    Column("created_by", String(100), nullable=False),
    _ts(),
    CheckConstraint("direction IN ('in','out')", name="direction"),
    CheckConstraint("amount_minor > 0", name="amount"),
)
Index("ix_book_entries_biz_date", book_entries.c.business_id, book_entries.c.on_date)

# ---------------------------------------------------------------- ars02 (v0.21): funnels, email designs, memberships, courses,
# community, affiliates, ads, apps that send in
funnels = Table(                                             # landing pages in steps: page 1 -> page 2 -> thank you
    "funnels", metadata, _id(), _biz(),
    Column("name", String(120), nullable=False),
    Column("active", Boolean, nullable=False, server_default=text("false")),   # off = only the owner can open its pages
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
)

funnel_pages = Table(                                        # one step of a funnel: blocks, public at /s/<business>/p/<slug>
    "funnel_pages", metadata, _id(), _biz(),
    Column("funnel_id", UUID(as_uuid=True), ForeignKey("funnels.id", ondelete="CASCADE"), nullable=False),
    Column("position", Integer, nullable=False, server_default=text("0")),
    Column("title", String(120), nullable=False),
    Column("slug", String(60), nullable=False),
    Column("blocks", JSONB, nullable=False, server_default=text("'[]'")),     # [{"t": "hero", ...}] (funnels.clean_blocks)
    _ts(), _ts("updated_at"),
    UniqueConstraint("business_id", "slug", name="uq_funnel_page_slug"),
)
Index("ix_funnel_pages_funnel", funnel_pages.c.funnel_id, funnel_pages.c.position)

funnel_hits = Table(                                         # visits and leads per step — no cookie, no IP: a daily hash
    "funnel_hits", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _biz(),
    Column("page_id", UUID(as_uuid=True), ForeignKey("funnel_pages.id", ondelete="CASCADE"), nullable=False),
    Column("day", Date, nullable=False),
    Column("visitor", String(16), nullable=False),            # hash of IP + secret + day: counts people, can't find or follow them
    Column("kind", String(5), nullable=False),                # view | lead
    _ts(),
    UniqueConstraint("page_id", "day", "visitor", "kind", name="uq_funnel_hit"),
    CheckConstraint("kind IN ('view','lead')", name="kind"),
)

email_designs = Table(                                       # saved emails (blocks), reused in campaigns and drips
    "email_designs", metadata, _id(), _biz(),
    Column("name", String(120), nullable=False),
    Column("subject", String(200), nullable=False, server_default=""),
    Column("blocks", JSONB, nullable=False, server_default=text("'[]'")),     # campaigns.clean_blocks
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
)

member_plans = Table(                                        # a membership the business offers: free, or paid through one of its plans
    "member_plans", metadata, _id(), _biz(),
    Column("name", String(120), nullable=False),
    Column("about", Text, nullable=False, server_default=""),
    Column("plan_id", UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")),   # paid: the recurring plan (invoices.py)
    Column("community", Boolean, nullable=False, server_default=text("true")),            # members can post and comment
    Column("open_join", Boolean, nullable=False, server_default=text("true")),            # visitors may ask to join (owner says yes)
    Column("active", Boolean, nullable=False, server_default=text("true")),
    _ts(), _ts("updated_at"),
)

members = Table(                                             # a customer in a membership; in by the owner's yes, never by itself
    "members", metadata, _id(), _biz(),
    Column("member_plan_id", UUID(as_uuid=True), ForeignKey("member_plans.id", ondelete="CASCADE"), nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("status", String(8), nullable=False, server_default="pending"),
    Column("subscription_id", UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL")),
    Column("joined_at", DateTime(timezone=True)),             # the day they got in (lessons drip from here)
    Column("last_seen_at", DateTime(timezone=True)),
    Column("created_by", String(100), nullable=False),
    _ts(),
    UniqueConstraint("member_plan_id", "contact_id", name="uq_member"),
    CheckConstraint("status IN ('pending','active','paused','removed')", name="status"),
)

member_links = Table(                                        # one-time sign-in links for members (only the hash is kept)
    "member_links", metadata, _id(), _biz(),
    Column("member_id", UUID(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True)),
    _ts(),
)

courses = Table(                                             # modules -> lessons, for the members of one membership (or all)
    "courses", metadata, _id(), _biz(),
    Column("member_plan_id", UUID(as_uuid=True), ForeignKey("member_plans.id", ondelete="SET NULL")),   # NULL = every member
    Column("title", String(160), nullable=False),
    Column("about", Text, nullable=False, server_default=""),
    Column("published", Boolean, nullable=False, server_default=text("false")),
    _ts(), _ts("updated_at"),
)

course_modules = Table(
    "course_modules", metadata, _id(), _biz(),
    Column("course_id", UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
    Column("title", String(160), nullable=False),
    Column("position", Integer, nullable=False, server_default=text("0")),
    _ts(),
)

lessons = Table(                                             # text, a video link or a file; opens N days after the member joined
    "lessons", metadata, _id(), _biz(),
    Column("module_id", UUID(as_uuid=True), ForeignKey("course_modules.id", ondelete="CASCADE"), nullable=False),
    Column("title", String(160), nullable=False),
    Column("kind", String(5), nullable=False, server_default="text"),
    Column("body", Text, nullable=False, server_default=""),
    Column("video_url", String(500)),
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")),
    Column("drip_days", Integer, nullable=False, server_default=text("0")),
    Column("position", Integer, nullable=False, server_default=text("0")),
    _ts(), _ts("updated_at"),
    CheckConstraint("kind IN ('text','video','file')", name="kind"),
    CheckConstraint("drip_days BETWEEN 0 AND 3650", name="drip"),
)

lesson_progress = Table(                                     # a member finished a lesson
    "lesson_progress", metadata, _id(), _biz(),
    Column("member_id", UUID(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
    Column("lesson_id", UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
    _ts("done_at"),
    UniqueConstraint("member_id", "lesson_id", name="uq_lesson_done"),
)

community_posts = Table(                                     # a membership's own board: members (and the owner) post
    "community_posts", metadata, _id(), _biz(),
    Column("member_plan_id", UUID(as_uuid=True), ForeignKey("member_plans.id", ondelete="CASCADE"), nullable=False),
    Column("member_id", UUID(as_uuid=True), ForeignKey("members.id", ondelete="SET NULL")),
    Column("by_business", Boolean, nullable=False, server_default=text("false")),   # the owner wrote it, as the business
    Column("body", Text, nullable=False),
    Column("hidden_at", DateTime(timezone=True)),             # the owner hid it (kept, not shown)
    Column("hidden_by", String(100)),
    _ts(),
)
Index("ix_community_posts_plan", community_posts.c.member_plan_id, community_posts.c.created_at)

community_comments = Table(
    "community_comments", metadata, _id(), _biz(),
    Column("post_id", UUID(as_uuid=True), ForeignKey("community_posts.id", ondelete="CASCADE"), nullable=False),
    Column("member_id", UUID(as_uuid=True), ForeignKey("members.id", ondelete="SET NULL")),
    Column("by_business", Boolean, nullable=False, server_default=text("false")),
    Column("body", Text, nullable=False),
    Column("hidden_at", DateTime(timezone=True)),
    Column("hidden_by", String(100)),
    _ts(),
)

affiliates = Table(                                          # people who send customers, with their own link /a/<code>
    "affiliates", metadata, _id(), _biz(),
    Column("name", String(200), nullable=False),
    Column("email", String(320), nullable=False, server_default=""),
    Column("phone", String(30), nullable=False, server_default=""),
    Column("code", String(16), nullable=False, unique=True),
    Column("kind", String(5), nullable=False, server_default="pct"),          # pct (rate_bp of the sale before tax) | fixed (per sale)
    Column("rate_bp", Integer, nullable=False, server_default=text("0")),
    Column("fixed_minor", BigInteger, nullable=False, server_default=text("0")),
    Column("first_only", Boolean, nullable=False, server_default=text("false")),   # only their customer's first purchase
    Column("clicks", Integer, nullable=False, server_default=text("0")),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("note", String(300), nullable=False, server_default=""),
    _ts(), _ts("updated_at"),
    CheckConstraint("kind IN ('pct','fixed')", name="kind"),
    CheckConstraint("rate_bp BETWEEN 0 AND 10000", name="rate"),
    CheckConstraint("fixed_minor >= 0", name="fixed"),
)

affiliate_referrals = Table(                                 # a customer brought by an affiliate (once; the first one counts)
    "affiliate_referrals", metadata, _id(), _biz(),
    Column("affiliate_id", UUID(as_uuid=True), ForeignKey("affiliates.id", ondelete="CASCADE"), nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("how", String(12), nullable=False),                # link | owner (added by hand)
    _ts(),
    UniqueConstraint("business_id", "contact_id", name="uq_affiliate_customer"),
)

affiliate_payouts = Table(                                   # the owner paid an affiliate (outside the app) and wrote it down
    "affiliate_payouts", metadata, _id(), _biz(),
    Column("affiliate_id", UUID(as_uuid=True), ForeignKey("affiliates.id", ondelete="CASCADE"), nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("paid_on", Date, nullable=False),
    Column("note", String(300), nullable=False, server_default=""),
    Column("created_by", String(100), nullable=False),
    _ts(),
)

affiliate_commissions = Table(                               # what an affiliate earned on a paid sale (and back out on a refund)
    "affiliate_commissions", metadata, _id(), _biz(),
    Column("affiliate_id", UUID(as_uuid=True), ForeignKey("affiliates.id", ondelete="CASCADE"), nullable=False),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("source", String(8), nullable=False),              # invoice | pos
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("kind", String(7), nullable=False),                # earn | reverse (a refund/void: the same amount, negative)
    Column("base_minor", BigInteger, nullable=False),         # the sale before tax
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("payout_id", UUID(as_uuid=True), ForeignKey("affiliate_payouts.id", ondelete="SET NULL")),   # NULL = still owed
    _ts(),
    UniqueConstraint("source", "source_id", "kind", name="uq_affiliate_commission"),
    CheckConstraint("source IN ('invoice','pos')", name="source"),
    CheckConstraint("kind IN ('earn','reverse')", name="kind"),
)

ads = Table(                                                 # ad copy for the business's OWN ad accounts + a tracked link /go/<code>
    "ads", metadata, _id(), _biz(),
    Column("name", String(120), nullable=False),
    Column("platform", String(8), nullable=False, server_default="meta"),     # meta | google | tiktok | other
    Column("idea", Text, nullable=False, server_default=""),
    Column("headline", String(200), nullable=False, server_default=""),
    Column("body", Text, nullable=False, server_default=""),
    Column("audience", Text, nullable=False, server_default=""),
    Column("dest", String(500), nullable=False),               # where the ad sends people (the business's own page)
    Column("code", String(16), nullable=False, unique=True),
    Column("clicks", Integer, nullable=False, server_default=text("0")),
    Column("by_ai", Boolean, nullable=False, server_default=text("false")),
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    CheckConstraint("platform IN ('meta','google','tiktok','other')", name="platform"),
)

inbound_hooks = Table(                                       # other apps send a new customer (or deal) in: POST /hooks/in/<token>
    "inbound_hooks", metadata, _id(), _biz(),
    Column("name", String(100), nullable=False),
    Column("prefix", String(12), nullable=False),             # shown on screen to tell them apart; the token itself is never kept
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("makes", String(7), nullable=False, server_default="contact"),     # contact | deal (a contact and a deal in `stage`)
    Column("stage", String(40)),
    Column("tag", String(60)),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("uses", Integer, nullable=False, server_default=text("0")),
    Column("last_used_at", DateTime(timezone=True)),
    Column("created_by", String(100), nullable=False),
    _ts(),
    CheckConstraint("makes IN ('contact','deal')", name="makes"),
)

saved_replies = Table(                                       # the owner's ready answers, one tap in the inbox
    "saved_replies", metadata, _id(), _biz(),
    Column("title", String(80), nullable=False),
    Column("body", Text, nullable=False),
    Column("position", Integer, nullable=False, server_default=text("0")),
    _ts(),
    UniqueConstraint("business_id", "title", name="uq_saved_reply"),
)

snapshot_applies = Table(                                    # a ready setup applied (or imported): what it added, so it can be undone
    "snapshot_applies", metadata, _id(), _biz(),
    Column("snapshot", String(40), nullable=False),           # the setup's key (salon, clinic…) or "import"
    Column("added", JSONB, nullable=False, server_default=text("'{}'")),     # {"custom_fields": [ids], "recipes": [ids], …}
    Column("words", JSONB, nullable=False, server_default=text("'{}'")),     # the words it set (only those that were empty)
    Column("site", JSONB, nullable=False, server_default=text("'{}'")),      # the website parts it filled (only empty ones)
    Column("hours", Boolean, nullable=False, server_default=text("false")),  # it set the opening hours (there were none)
    Column("by", String(100), nullable=False),
    _ts("applied_at"),
    Column("undone_at", DateTime(timezone=True)),
)

push_subscriptions = Table(                                  # the owner's phones and computers that get notifications (Web Push)
    "push_subscriptions", metadata, _id(), _biz(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("endpoint", Text, nullable=False, unique=True),
    Column("p256dh", String(200), nullable=False),
    Column("auth", String(100), nullable=False),
    Column("ua", String(200), nullable=False, server_default=""),
    Column("failures", Integer, nullable=False, server_default=text("0")),
    Column("last_ok_at", DateTime(timezone=True)),
    _ts(),
)

# mkt01 (v0.22): things the business sells on Facebook Marketplace (as its Page) and the buyers who wrote about each
marketplace_items = Table(                                   # a listing: its own price, pickup and notes; the product itself stays in catalog_items
    "marketplace_items", metadata, _id(), _biz(),
    Column("catalog_item_id", UUID(as_uuid=True), ForeignKey("catalog_items.id", ondelete="SET NULL")),   # picked from Inventory
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")),
    Column("title", String(200), nullable=False),
    Column("icon", String(16), nullable=False, server_default=""),          # one emoji for the owner's alerts ("🛋 Black sofa")
    Column("price_minor", BigInteger),
    Column("pickup", String(200), nullable=False, server_default=""),       # the area where it is picked up
    Column("delivery", Boolean, nullable=False, server_default=text("false")),
    Column("notes", Text, nullable=False, server_default=""),               # for Valeria: what she may tell buyers
    Column("listing_id", String(40)),                                       # Facebook's listing / product number, when known
    Column("status", String(8), nullable=False, server_default="open"),     # open | paused | sold
    Column("sold_at", DateTime(timezone=True)),
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    CheckConstraint("status IN ('open','paused','sold')", name="status"),
    CheckConstraint("price_minor IS NULL OR price_minor >= 0", name="price"),
)
Index("ix_marketplace_items_biz", marketplace_items.c.business_id, marketplace_items.c.status)

marketplace_buyers = Table(                                  # one Messenger chat = one buyer; the item they asked about, where it stands
    "marketplace_buyers", metadata, _id(), _biz(),
    Column("item_id", UUID(as_uuid=True), ForeignKey("marketplace_items.id", ondelete="SET NULL")),   # NULL: not known yet
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("ref", Integer, nullable=False),                                 # "#3" in the owner's chat
    Column("status", String(10), nullable=False, server_default="asking"),
    Column("offer_minor", BigInteger),                                      # what they offered (status offered)
    Column("coming", String(60), nullable=False, server_default=""),        # when they said they'd come ("6pm")
    Column("lang", String(10)),                                             # the language they write in
    Column("asked_which_at", DateTime(timezone=True)),                      # Valeria asked "which item?"
    _ts(), _ts("updated_at"),
    UniqueConstraint("business_id", "ref", name="uq_marketplace_buyer_ref"),
    CheckConstraint("status IN ('asking','offered','coming','bought','told_sold','not_told')", name="status"),
)
Index("ix_marketplace_buyers_item", marketplace_buyers.c.business_id, marketplace_buyers.c.item_id)

# ---------------------------------------------------------------- ctr01 (v0.24): contracts the customer signs on the phone
contract_templates = Table(                                  # the business's own contract wording: from its kit, Valeria's draft, typed or uploaded
    "contract_templates", metadata, _id(), _biz(),
    Column("name", String(200), nullable=False),
    Column("source", String(8), nullable=False),                            # kit | ai | own (typed / pasted) | upload (a PDF)
    Column("lang", String(10), nullable=False),
    Column("sections", JSONB, nullable=False, server_default=text("'[]'")),  # [{"key", "title", "text"}] — base, k:<block>, own:<n>
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id")),          # an uploaded PDF (kept while used: files.remove refuses)
    Column("archived", Boolean, nullable=False, server_default=text("false")),
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    CheckConstraint("source IN ('kit','ai','own','upload')", name="source"),
)
Index("ix_contract_templates_biz", contract_templates.c.business_id, contract_templates.c.archived)

contracts = Table(                                           # one contract for one customer; frozen once sent (a change = a new version)
    "contracts", metadata, _id(), _biz(),
    Column("number", Integer, nullable=False),                              # 1, 2, 3… per business; versions share it
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("template_id", UUID(as_uuid=True), ForeignKey("contract_templates.id", ondelete="SET NULL")),
    Column("contact_id", UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")),
    Column("appointment_id", UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")),
    Column("invoice_id", UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL")),
    Column("quote_id", UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="SET NULL")),
    Column("payment_id", UUID(as_uuid=True), ForeignKey("payments.id", ondelete="SET NULL")),   # the deposit's pay link
    Column("title", String(200), nullable=False),
    Column("body", Text, nullable=False, server_default=""),                # exactly the words the customer reads and signs
    Column("fields", JSONB, nullable=False, server_default=text("'{}'")),   # the filled-in facts: client, service, date, price…
    Column("file_id", UUID(as_uuid=True), ForeignKey("files.id")),          # the owner's own PDF, when uploaded (kept: files.remove refuses)
    Column("file_sha256", String(64)),
    Column("lang", String(10), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("deposit_minor", BigInteger),
    Column("deposit_paid_at", DateTime(timezone=True)),
    Column("status", String(8), nullable=False, server_default="draft"),
    Column("doc_hash", String(64)),                                         # sha256 of what was sent (set when sent, never changed)
    Column("channel", String(20)),                                          # where the link went
    Column("sent_at", DateTime(timezone=True)),
    Column("viewed_at", DateTime(timezone=True)),
    Column("reminded_at", DateTime(timezone=True)),
    Column("signed_at", DateTime(timezone=True)),
    Column("signed_name", String(200)),
    Column("signed_ip", String(64)),
    Column("signed_ua", String(300)),
    Column("signature", Text),                                              # the drawn signature (data:image/png;base64,…)
    Column("signed_hash", String(64)),                                      # the fingerprint checked again at the moment of signing
    Column("declined_at", DateTime(timezone=True)),
    Column("decline_reason", Text),
    Column("voided_at", DateTime(timezone=True)),
    Column("void_reason", Text),
    Column("created_by", String(100), nullable=False),
    _ts(), _ts("updated_at"),
    UniqueConstraint("business_id", "number", "version", name="uq_contract_version"),
    CheckConstraint("status IN ('draft','sent','viewed','signed','declined','voided')", name="status"),
    CheckConstraint("deposit_minor IS NULL OR deposit_minor >= 0", name="deposit"),
)
Index("ix_contracts_biz", contracts.c.business_id, contracts.c.status)
Index("ix_contracts_contact", contracts.c.business_id, contracts.c.contact_id)

contract_settings = Table(                                   # one row per business: "a booking waits for the signed agreement (and deposit)"
    "contract_settings", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("require_signed", Boolean, nullable=False, server_default=text("false")),
    Column("require_deposit", Boolean, nullable=False, server_default=text("false")),
    _ts("updated_at"),
)

# agr01 (v0.25): the provider (whoever runs this server) and every business that uses it sign one written agreement
provider_agreements = Table(                                 # one published version of the agreement — frozen (a DB trigger refuses changes)
    "provider_agreements", metadata, _id(),
    Column("version", Integer, nullable=False, unique=True),                # 1, 2, 3… for the whole server
    Column("provider", JSONB, nullable=False, server_default=text("'{}'")),  # the operator's details as published
    Column("texts", JSONB, nullable=False, server_default=text("'{}'")),     # {lang: {"title", "body"}}; English = the canonical text
    Column("doc_hash", String(64), nullable=False),                         # sha256 of the version, its details and every text
    Column("published_by", String(100), nullable=False),
    _ts("published_at"),
)
agreement_acceptances = Table(                               # a business signed a version — never changed, kept for good
    "agreement_acceptances", metadata, _id(), _biz(),
    Column("agreement_id", UUID(as_uuid=True), ForeignKey("provider_agreements.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),   # who signed (their email is kept below)
    Column("signer_email", String(320), nullable=False, server_default=""),
    Column("business_name", String(200), nullable=False),                   # "I agree, for <business name>"
    Column("signed_name", String(200), nullable=False),                     # the full name they typed
    Column("lang", String(10), nullable=False),
    Column("text", Text, nullable=False),                                   # exactly the words shown and signed (in that language)
    Column("text_sha256", String(64), nullable=False),
    Column("doc_hash", String(64), nullable=False),                         # the version's own fingerprint
    Column("method", String(6), nullable=False),                            # drawn (signature pad) | typed (the name only)
    Column("signature", Text),                                              # the drawn signature (data:image/png;base64,…)
    Column("ip", String(64), nullable=False, server_default=""),
    Column("ua", String(300), nullable=False, server_default=""),
    _ts("accepted_at"),
    UniqueConstraint("business_id", "agreement_id", name="uq_agreement_acceptance"),
    CheckConstraint("method IN ('drawn','typed')", name="method"),
)
agreement_asks = Table(                                      # when a business's owner was first asked about a version; Later; I don't agree
    "agreement_asks", metadata,
    Column("business_id", UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
    Column("agreement_id", UUID(as_uuid=True), ForeignKey("provider_agreements.id"), primary_key=True),
    Column("first_asked_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("later_count", Integer, nullable=False, server_default=text("0")),
    Column("last_later_at", DateTime(timezone=True)),
    Column("declined_at", DateTime(timezone=True)),
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
