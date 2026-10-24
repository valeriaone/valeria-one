"""Request bodies for the public API (responses are the stored rows)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessPatch(_In):
    name: str | None = Field(None, max_length=200)
    language: str | None = Field(None, max_length=10)
    timezone: str | None = Field(None, max_length=64)
    industry: str | None = Field(None, max_length=40)
    blocks: list[str] | None = Field(None, max_length=20, description="The blocks switched ON (the rest go off): "
                                     "calendar, pipeline, payments, quotes, followups, website, comments, ai.")
    words: dict[str, str] | None = Field(None, description='What the business calls things, e.g. {"contact": "Member", '
                                         '"stage:new": "Inquiry"}; an empty value goes back to the usual word.')


class ContactIn(_In):
    name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=320)
    phone: str | None = Field(None, max_length=40, description="Any format; stored as E.164 using the business's country.")
    language: str | None = Field(None, max_length=10)
    source: str | None = Field(None, max_length=60)
    tags: list[str] | None = None
    notes: str | None = None
    marketing_consent: bool | None = None
    marketing_consent_source: str | None = Field(None, max_length=200, description="How they agreed — required with consent.")


class ChannelIn(_In):
    channel: str = Field(examples=["whatsapp", "messenger", "instagram", "sms", "email", "telegram", "webhook", "sandbox"])
    name: str = Field("", max_length=100)
    address: str | None = Field(None, max_length=200)
    config: dict = Field(default_factory=dict, description="Channel credentials; secrets are encrypted at rest and never shown again.")


class MessageIn(_In):
    contact_id: UUID
    body: str = ""
    media: list[dict] = Field(default_factory=list)
    template: dict | None = Field(None, description="Approved template for channels with a reply window, e.g. "
                                                    '{"name": "appointment_reminder", "language": "es_MX", "params": ["Ana", "10:00"]}')
    kind: Literal["service", "marketing"] = "service"
    channel_account_id: UUID | None = None
    channel: str | None = Field(None, description="Use the first connected account of this channel.")


class StatusIn(_In):
    status: str


class DealIn(_In):
    title: str = Field(max_length=200)
    contact_id: UUID | None = None
    stage: str | None = None
    amount_minor: int | None = Field(None, ge=0, description="Whole number in the smallest currency unit (24900 = $249.00).")
    expected_close: datetime | None = None
    notes: str | None = None


class DealPatch(_In):
    title: str | None = Field(None, max_length=200)
    contact_id: UUID | None = None
    stage: str | None = None
    amount_minor: int | None = Field(None, ge=0)
    expected_close: datetime | None = None
    notes: str | None = None


class AppointmentIn(_In):
    title: str = Field(max_length=200)
    starts_at: datetime
    ends_at: datetime
    contact_id: UUID | None = None
    deal_id: UUID | None = None
    location: str | None = Field(None, max_length=300)
    notes: str | None = None
    allow_overlap: bool = False


class SandboxSignup(_In):
    name: str = Field(max_length=200)
    country: str = Field("MX", max_length=2)
    industry: str = Field("generic", max_length=40)


class SimulateIn(_In):
    from_: str = Field(alias="from", max_length=200, description="Who is writing (any id).")
    body: str = ""
    name: str | None = Field(None, max_length=200)
    channel_account_id: UUID | None = None
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CommentRuleIn(_In):
    keywords: list[str] = Field(default_factory=list, description="Words that trigger it (any accent/case). Empty = every comment.")
    reply: str = Field(max_length=1000, description="The private message. {name} becomes the person's first name.")
    channels: list[Literal["messenger", "instagram"]] = Field(default_factory=lambda: ["messenger", "instagram"])


class SimulateCommentIn(_In):
    name: str = Field("Customer", max_length=200)
    text: str = Field(max_length=2000)


class BookingSettingsIn(_In):
    enabled: bool | None = None
    slug: str | None = Field(None, max_length=60)
    hours: dict[str, list[list[str]]] | None = Field(None, description='{"mon": [["09:00","14:00"],["16:00","19:00"]], …}')
    notice_minutes: int | None = None
    days_ahead: int | None = None


class ServiceIn(_In):
    name: str = Field(max_length=120)
    minutes: int
    price_minor: int | None = None


class PublicBookIn(_In):
    service_id: UUID
    starts_at: datetime
    name: str = Field(max_length=200)
    phone: str = Field(max_length=40)
    email: str | None = Field(None, max_length=320)
    marketing_consent: bool = False
    notes: str = Field("", max_length=1000)


class AISettingsIn(_In):
    provider: Literal["gemini", "anthropic", "openai"] | None = None
    api_key: str | None = Field(None, max_length=300, description="Stored encrypted; never shown again. Empty = keep.")
    model: str | None = Field(None, max_length=100)
    mode: Literal["off", "suggest", "auto"] | None = None
    instructions: str | None = Field(None, max_length=4000, description="Facts, tone, FAQ — in your words.")


class WebhookIn(_In):
    url: str = Field(max_length=500, description="https only")
    events: list[str] = Field(default_factory=lambda: ["*"], description='e.g. ["contact.created", "message.*", "booking.made"]; "*" = all')


class PaymentAccountIn(_In):
    provider: str = Field(description="stripe, mercadopago (incl. pix), paymongo, paystack, razorpay (incl. upi), bank_transfer")
    config: dict = Field(default_factory=dict, description="The business's own credentials; secrets are encrypted and never shown again.")


class PaymentIn(_In):
    amount_minor: int = Field(gt=0)
    description: str = Field(max_length=300)
    contact_id: UUID | None = None
    deal_id: UUID | None = None
    provider: str | None = None
    send: bool = Field(False, description="Also send the link to the contact on their channel (channel_account_id or channel).")
    channel_account_id: UUID | None = None
    channel: str | None = None


class AutomationIn(_In):
    trigger: Literal["appointment.reminder", "appointment.done", "contact.created", "deal.won"]
    minutes: int = Field(ge=0, le=525600, description="Before the appointment (reminder) or after the event (the others).")
    message: str = Field(max_length=1000, description="{name} {business} {when} {link} are filled in.")
    template: str | None = Field(None, max_length=100, description="Approved WhatsApp template to use outside the 24 h window.")


class CampaignIn(_In):
    name: str = Field(max_length=120)
    message: str = Field(max_length=1000)
    tag: str | None = Field(None, max_length=60)
    send_at: datetime | None = None
    template: str | None = Field(None, max_length=100)


class QuoteLine(_In):
    description: str = Field(max_length=200)
    qty: int = Field(1, ge=1, le=100000)
    unit_minor: int = Field(ge=0)


class QuoteIn(_In):
    title: str = Field(max_length=200)
    lines: list[QuoteLine] = Field(min_length=1, max_length=30)
    contact_id: UUID | None = None
    deal_id: UUID | None = None
    notes: str = Field("", max_length=2000)
    valid_days: int = Field(15, ge=1, le=365)


# ---------------------------------------------------------------- Partner API (hosts, agencies)
class PartnerBusinessIn(_In):
    name: str = Field(max_length=200)
    country: str = Field(max_length=2, description="Country pack code, e.g. KE, PH, BR (GET /v1/packs/countries).")
    owner_email: str = Field(max_length=320, description="Gets the invitation link to own the business.")
    industry: str = Field("generic", max_length=40)
    language: str | None = Field(None, max_length=10)
    timezone: str | None = Field(None, max_length=64)
    partner_ref: str | None = Field(None, max_length=100, description="Your own id for this customer (unique).")
    blocks: list[str] | None = Field(None, max_length=20, description="Blocks switched on (default: all).")


class PartnerSuspendIn(_In):
    reason: str | None = Field(None, max_length=300, description="Kept on record, e.g. 'invoice 1042 unpaid'.")


class PartnerInviteIn(_In):
    email: str = Field(max_length=320)
    role: str = Field("owner", pattern="^(owner|staff)$")


class PartnerDeleteIn(_In):
    confirm: str = Field(max_length=200, description="The business's exact name: deleting cannot be undone.")


class PartnerKeyIn(_In):
    name: str = Field(max_length=100)
