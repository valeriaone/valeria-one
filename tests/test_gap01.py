"""gap01 (v0.42.0): WhatsApp through the owner's own Twilio — we set the webhook ourselves when Twilio allows it (Messaging
Service); otherwise the owner gets 4 short steps with the exact URL to paste. The connection is never blocked."""
import re

import httpx
import pytest

from valeria_one import db
from valeria_one.channels.twilio_whatsapp import TwilioWhatsApp
from valeria_one.services import inbox as X

from _va import owner_session
from conftest import make_business


def adapter(status=200, seen=None):
    def handler(req):
        if seen is not None:
            seen.append((str(req.url), req.content.decode()))
        return httpx.Response(status, json={})
    return TwilioWhatsApp(http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_messaging_service_gets_its_webhook_set_by_us():
    seen, cfg = [], {"account_sid": "AC1", "auth_token": "t", "messaging_service_sid": "MG1"}
    adapter(seen=seen).register_hook(cfg, "https://one.test/hooks/abc")
    assert cfg["hook_set"] is True and seen[0][0] == "https://messaging.twilio.com/v1/Services/MG1"
    assert "InboundRequestUrl=https%3A%2F%2Fone.test%2Fhooks%2Fabc" in seen[0][1]


def test_wrong_keys_are_said_plainly_and_other_refusals_never_block():
    with pytest.raises(ValueError, match="Auth Token"):
        adapter(401).register_hook({"account_sid": "AC1", "auth_token": "x", "messaging_service_sid": "MG1"}, "https://one.test/h")
    cfg = {"account_sid": "AC1", "auth_token": "t", "messaging_service_sid": "MG1"}
    adapter(403).register_hook(cfg, "https://one.test/h")
    assert cfg["hook_set"] is False


def test_a_bare_sender_is_never_touched_and_the_owner_gets_the_steps(client):
    seen, cfg = [], {"account_sid": "AC1", "auth_token": "t", "from_number": "+529991234567"}
    adapter(seen=seen).register_hook(cfg, "https://one.test/h")
    assert cfg["hook_set"] is False and seen == []
    biz, _ = make_business("MX", "salon", name="Salón")
    with db.tx() as conn:
        X.connect(conn, biz, "t", channel="whatsapp_twilio", name="WhatsApp", cfg={**cfg, "hook_set": False})
    owner_session(client, biz)
    page = client.get("/app/channels", headers={"Accept-Language": "es-MX"}).text
    assert 'id="tw-steps"' in page and "WhatsApp senders" in page and re.search(r"/hooks/[\w-]+", page)


def test_a_set_webhook_says_connected(client):
    biz, _ = make_business("MX", "salon", name="Salón 2")
    with db.tx() as conn:
        X.connect(conn, biz, "t", channel="whatsapp_twilio", name="WhatsApp",
                  cfg={"account_sid": "AC1", "auth_token": "t", "messaging_service_sid": "MG1", "hook_set": True})
    owner_session(client, biz)
    page = client.get("/app/channels", headers={"Accept-Language": "es-MX"}).text
    assert 'id="tw-steps"' not in page and "llegan aquí solos" in page
