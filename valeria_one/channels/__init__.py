"""Channel adapters. The core (contacts, inbox, deals) never knows which pipe a message used.

Each adapter turns our Outbound into the provider's API call, and the provider's webhook into our
Inbound / StatusUpdate. Adding LINE, Viber or whatever comes next = one new adapter file here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx


@dataclass
class Outbound:
    to: str                                  # the contact's address on this channel
    body: str = ""
    media: list[dict] = field(default_factory=list)      # [{"type": "image", "url": "…"}]
    template: dict | None = None             # {"name": …, "language": …, "params": […]}
    options: list[dict] = field(default_factory=list)    # [{"id", "title", "description"}] — tap-to-pick where the channel can


@dataclass
class SendResult:
    status: str                              # sent | queued | failed
    provider_id: str | None = None
    error: str | None = None


@dataclass
class Inbound:
    address: str                             # who wrote (phone, PSID, email …)
    body: str = ""
    provider_id: str | None = None
    name: str | None = None
    media: list[dict] = field(default_factory=list)
    at: datetime | None = None


@dataclass
class StatusUpdate:
    provider_id: str
    status: str                              # sent | delivered | read | failed
    error: str | None = None


@dataclass
class HookRequest:
    method: str
    url: str
    headers: dict[str, str]                  # lower-case names
    query: dict[str, str]
    body: bytes
    form: dict[str, str] | None = None


class BadSignature(Exception):
    pass


class Adapter:
    channel: str = ""
    reply_window_hours: int | None = None    # free-form replies only within N hours of the contact's last message
    address_kind: str = "id"                 # phone | email | id
    needs: tuple[str, ...] = ()              # config fields a business must give to connect

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=20)

    def send(self, cfg: dict, address: str | None, out: Outbound) -> SendResult:
        raise NotImplementedError

    def verify(self, cfg: dict, req: HookRequest) -> str | None:
        """Answer a provider's GET handshake (Meta hub.challenge). None = not a handshake."""
        return None

    def parse(self, cfg: dict, req: HookRequest) -> list[Inbound | StatusUpdate]:
        raise NotImplementedError

    def prepare(self, cfg: dict) -> dict:
        """Fill in what can be made for the business (e.g. a webhook secret) before the config is checked."""
        return cfg

    def register_hook(self, cfg: dict, webhook_url: str) -> None:
        """Tell the provider where to deliver messages, when its API allows it, so the business never has to.
        Raise ValueError(reason, in plain words) if the provider says no."""
        return None

    def unregister_hook(self, cfg: dict) -> None:
        """Best effort when a channel is disconnected; never raises."""
        return None

    def check_config(self, cfg: dict) -> list[str]:
        return [f for f in self.needs if not cfg.get(f)]


_REGISTRY: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    _REGISTRY[cls.channel] = cls
    return cls


def adapter(channel: str, http: httpx.Client | None = None) -> Adapter:
    try:
        return _REGISTRY[channel](http)
    except KeyError:
        raise KeyError(f"no adapter for channel {channel!r}") from None


def available() -> list[str]:
    return sorted(_REGISTRY)


from . import email_smtp, meta, sandbox, telegram, twilio_sms, twilio_whatsapp, viber, webchat, webhook  # noqa: E402,F401  (registers adapters)
