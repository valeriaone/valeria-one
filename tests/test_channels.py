import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from valeria_one import channels, db
from valeria_one.channels import HookRequest
from valeria_one.channels.twilio_sms import signature as twilio_sig
from valeria_one.crypto import hmac_sha256_hex
from valeria_one.services import Blocked
from valeria_one.services import inbox as X

WA_CFG = {"phone_number_id": "PNID", "token": "TOK", "app_secret": "APPSECRET", "verify_token": "VT"}


def _connect(client, h, channel, cfg, name="x"):
    r = client.post("/v1/channels", headers=h, json={"channel": channel, "name": name, "config": cfg})
    assert r.status_code == 201, r.text
    j = r.json()
    return j, j["webhook_url"].replace("https://one.test", "")


def _wa_payload(frm="5219991234567", mid="wamid.1", body="Hola", name="Ana", ts=None):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PNID"},
        "contacts": [{"wa_id": frm, "profile": {"name": name}}],
        "messages": [{"from": frm, "id": mid, "timestamp": str(ts or int(datetime.now(timezone.utc).timestamp())),
                      "type": "text", "text": {"body": body}}]}}]}]}


def _post_signed(client, path, payload, secret="APPSECRET"):
    raw = json.dumps(payload).encode()
    return client.post(path, content=raw, headers={"Content-Type": "application/json",
                                                   "X-Hub-Signature-256": "sha256=" + hmac_sha256_hex(secret, raw)})


def test_whatsapp_handshake(client, mx):
    _, h = mx
    _, path = _connect(client, h, "whatsapp", WA_CFG)
    ok = client.get(path, params={"hub.mode": "subscribe", "hub.verify_token": "VT", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert client.get(path, params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code == 403
    assert client.get("/hooks/unknown-hook").status_code == 404


def test_whatsapp_inbound_creates_contact_once(client, mx):
    _, h = mx
    _, path = _connect(client, h, "whatsapp", WA_CFG)
    assert _post_signed(client, path, _wa_payload()).status_code == 200
    assert _post_signed(client, path, _wa_payload()).status_code == 200          # Meta retries: stored once
    cs = client.get("/v1/contacts", headers=h).json()
    assert len(cs) == 1 and cs[0]["phone"] == "+529991234567" and cs[0]["name"] == "Ana"   # legacy 521 cleaned
    conv = client.get("/v1/conversations", headers=h).json()
    assert len(conv) == 1 and conv[0]["channel"] == "whatsapp"
    msgs = client.get(f"/v1/conversations/{conv[0]['id']}", headers=h).json()["messages"]
    assert [m["body"] for m in msgs] == ["Hola"]


def test_whatsapp_bad_signature_touches_nothing(client, mx):
    _, h = mx
    _, path = _connect(client, h, "whatsapp", WA_CFG)
    assert _post_signed(client, path, _wa_payload(), secret="WRONG").status_code == 403
    raw = json.dumps(_wa_payload()).encode()
    assert client.post(path, content=raw).status_code == 403
    assert client.get("/v1/contacts", headers=h).json() == []


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reply_window_then_template(client, mx):
    biz, h = mx
    _, path = _connect(client, h, "whatsapp", WA_CFG)
    old = int((datetime.now(timezone.utc) - timedelta(hours=30)).timestamp())
    _post_signed(client, path, _wa_payload(ts=old))
    cid = client.get("/v1/contacts", headers=h).json()[0]["id"]
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer TOK" and req.url.path.endswith("/PNID/messages")
        return httpx.Response(200, json={"messages": [{"id": "wamid.out1"}]})

    with pytest.raises(Blocked) as e:
        X.send(biz, "test", contact_id=cid, body="¿Sigues interesada?", channel="whatsapp", http=_mock(handler))
    assert e.value.reason == "outside_reply_window" and calls == []
    m = X.send(biz, "test", contact_id=cid, channel="whatsapp", http=_mock(handler),
               template={"name": "seguimiento", "language": "es_MX", "params": ["Ana"]})
    assert m["status"] == "sent" and m["provider_id"] == "wamid.out1"
    assert calls[0]["to"] == "529991234567" and calls[0]["template"]["name"] == "seguimiento"


def test_free_reply_inside_window_and_status_never_goes_back(client, mx):
    biz, h = mx
    _, path = _connect(client, h, "whatsapp", WA_CFG)
    _post_signed(client, path, _wa_payload())
    cid = client.get("/v1/contacts", headers=h).json()[0]["id"]
    m = X.send(biz, "test", contact_id=cid, body="¡Hola Ana!", channel="whatsapp",
               http=_mock(lambda r: httpx.Response(200, json={"messages": [{"id": "wamid.out2"}]})))
    assert m["status"] == "sent"

    def status(s):
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "PNID"}, "statuses": [{"id": "wamid.out2", "status": s}]}}]}]}
    _post_signed(client, path, status("read"))
    _post_signed(client, path, status("delivered"))                              # late, out of order
    with db.tx() as conn:
        row = conn.execute(db.messages.select().where(db.messages.c.provider_id == "wamid.out2")).mappings().one()
    assert row["status"] == "read"


def test_provider_error_is_recorded_not_raised(client, mx):
    biz, h = mx
    _, path = _connect(client, h, "whatsapp", WA_CFG)
    _post_signed(client, path, _wa_payload())
    cid = client.get("/v1/contacts", headers=h).json()[0]["id"]
    m = X.send(biz, "test", contact_id=cid, body="hi", channel="whatsapp",
               http=_mock(lambda r: httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement"}})))
    assert m["status"] == "failed" and "131047" in m["error"]

    def boom(r):
        raise httpx.ConnectError("down")
    m = X.send(biz, "test", contact_id=cid, body="hi", channel="whatsapp", http=_mock(boom))
    assert m["status"] == "failed"


def test_marketing_needs_consent_and_respects_us_quiet_hours(client, us):
    biz, h = us
    acc, _ = _connect(client, h, "sms", {"account_sid": "AC1", "auth_token": "T", "from_number": "+15550001111"})
    c = client.post("/v1/contacts", headers=h, json={"name": "Bob", "phone": "(951) 434-3352"}).json()
    n = iter(range(1, 100))
    ok = _mock(lambda r: httpx.Response(201, json={"sid": f"SM{next(n)}", "status": "queued"}))
    noon = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)          # 12:00 in New York
    with pytest.raises(Blocked) as e:
        X.send(biz, "t", contact_id=c["id"], body="Sale!", kind="marketing", channel="sms", http=ok, now=noon)
    assert e.value.reason == "no_consent"
    client.patch(f"/v1/contacts/{c['id']}", headers=h, json={"marketing_consent": True,
                                                              "marketing_consent_source": "web form, checkbox"})
    night = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)          # 22:00 in New York
    with pytest.raises(Blocked) as e:
        X.send(biz, "t", contact_id=c["id"], body="Sale!", kind="marketing", channel="sms", http=ok, now=night)
    assert e.value.reason == "quiet_hours"
    m = X.send(biz, "t", contact_id=c["id"], body="Sale!", kind="marketing", channel="sms", http=ok, now=noon)
    assert m["status"] == "queued" and m["provider_id"].startswith("SM")
    # a service message (their appointment) is fine at night
    assert X.send(biz, "t", contact_id=c["id"], body="Your visit is tomorrow 9am", channel="sms", http=ok, now=night)["status"] == "queued"


def test_twilio_inbound_signed_and_stop(client, us):
    biz, h = us
    _, path = _connect(client, h, "sms", {"account_sid": "AC1", "auth_token": "TWTOKEN", "from_number": "+15550001111"})
    form = {"From": "+19514343352", "To": "+15550001111", "Body": "STOP", "MessageSid": "SMin1", "NumMedia": "0"}
    sig = twilio_sig("TWTOKEN", "https://one.test" + path, form)
    r = client.post(path, data=form, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200 and "<Response/>" in r.text
    assert client.post(path, data=form, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    c = client.get("/v1/contacts", headers=h).json()[0]
    assert c["unsubscribed_at"] is not None
    with pytest.raises(Blocked) as e:
        X.send(biz, "t", contact_id=c["id"], body="hi", channel="sms",
               http=_mock(lambda r: httpx.Response(201, json={"sid": "x", "status": "queued"})))
    assert e.value.reason == "unsubscribed"


def test_messenger_and_instagram_inbound(client, mx):
    _, h = mx
    _, mpath = _connect(client, h, "messenger", {"page_id": "PAGE1", "page_token": "PT", "app_secret": "APPSECRET", "verify_token": "VT"})
    _, ipath = _connect(client, h, "instagram", {"ig_user_id": "IG1", "page_token": "PT", "app_secret": "APPSECRET", "verify_token": "VT"})
    msg = {"object": "page", "entry": [{"messaging": [
        {"sender": {"id": "PSID9"}, "recipient": {"id": "PAGE1"}, "timestamp": 1761300000000, "message": {"mid": "m1", "text": "precio?"}},
        {"sender": {"id": "PAGE1"}, "recipient": {"id": "PSID9"}, "message": {"mid": "m2", "text": "echo", "is_echo": True}}]}]}
    assert _post_signed(client, mpath, msg).status_code == 200
    ig = {"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": "IGSID5"}, "recipient": {"id": "IG1"}, "timestamp": 1761300000000, "message": {"mid": "i1", "text": "info"}}]}]}
    assert _post_signed(client, ipath, ig).status_code == 200
    assert _post_signed(client, ipath, msg).status_code == 200            # a page payload on the IG hook is ignored
    chans = sorted(c["channel"] for c in client.get("/v1/conversations", headers=h).json())
    assert chans == ["instagram", "messenger"]
    assert len(client.get("/v1/contacts", headers=h).json()) == 2


def test_messenger_send(client, mx):
    biz, h = mx
    _, mpath = _connect(client, h, "messenger", {"page_id": "PAGE1", "page_token": "PT", "app_secret": "APPSECRET", "verify_token": "VT"})
    _post_signed(client, mpath, {"object": "page", "entry": [{"messaging": [
        {"sender": {"id": "PSID9"}, "recipient": {"id": "PAGE1"}, "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
         "message": {"mid": "m1", "text": "precio?"}}]}]})
    cid = client.get("/v1/contacts", headers=h).json()[0]["id"]
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content), token=req.url.params["access_token"])
        return httpx.Response(200, json={"recipient_id": "PSID9", "message_id": "m.out"})
    m = X.send(biz, "t", contact_id=cid, body="$450", channel="messenger", http=_mock(handler))
    assert m["status"] == "sent" and seen["recipient"] == {"id": "PSID9"} and seen["token"] == "PT"


def test_telegram_and_generic_webhook(client, mx):
    biz, h = mx
    _, tpath = _connect(client, h, "telegram", {"bot_token": "BT", "secret": "TGS"})
    upd = {"update_id": 1, "message": {"message_id": 7, "date": 1761300000, "chat": {"id": 555}, "from": {"first_name": "Kenji"}, "text": "こんにちは"}}
    assert client.post(tpath, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 403
    assert client.post(tpath, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "TGS"}).status_code == 200
    _, wpath = _connect(client, h, "webhook", {"url": "https://gateway.example/send", "secret": "WHS"})
    raw = json.dumps({"from": "viber:abc", "body": "hi from Viber", "id": "v1", "name": "Maria"}).encode()
    assert client.post(wpath, content=raw, headers={"X-VO-Signature": hmac_sha256_hex("WHS", raw)}).status_code == 200
    names = sorted(c["name"] for c in client.get("/v1/contacts", headers=h).json())
    assert names == ["Kenji", "Maria"]
    cid = next(c["id"] for c in client.get("/v1/contacts", headers=h).json() if c["name"] == "Maria")

    def handler(req):
        assert req.headers["x-vo-signature"] == hmac_sha256_hex("WHS", req.content)
        return httpx.Response(200, json={"id": "v-out"})
    m = X.send(biz, "t", contact_id=cid, body="hola", channel="webhook", http=_mock(handler))
    assert m["status"] == "sent" and m["provider_id"] == "v-out"


def test_email_send_and_inbound(client, mx, monkeypatch):
    biz, h = mx
    cfg = {"smtp_host": "smtp.example", "smtp_port": "587", "username": "u", "password": "p", "from_email": "hola@salon.mx", "secret": "ES"}
    _, epath = _connect(client, h, "email", cfg)
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("smtp.example", 587)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self, context):
            pass
        def login(self, u, p):
            assert (u, p) == ("u", "p")
        def send_message(self, m):
            sent.append(m)
    monkeypatch.setattr(channels.adapter("email").__class__, "smtp_factory", FakeSMTP)
    c = client.post("/v1/contacts", headers=h, json={"name": "Ana", "email": "Ana@Example.com"}).json()
    m = X.send(biz, "t", contact_id=c["id"], body="Tu cita es mañana", channel="email")
    assert m["status"] == "sent" and sent[0]["To"] == "ana@example.com"
    raw = json.dumps({"from": "ANA@example.com", "subject": "Re: cita", "text": "Gracias", "message_id": "<x@y>"}).encode()
    assert client.post(epath, content=raw, headers={"X-VO-Signature": hmac_sha256_hex("ES", raw)}).status_code == 200
    assert len(client.get("/v1/contacts", headers=h).json()) == 1                   # same person, matched by email


def test_erase_wipes_message_text(client, mx):
    _, h = mx
    _, path = _connect(client, h, "whatsapp", WA_CFG)
    _post_signed(client, path, _wa_payload(body="mi dirección es Calle 60"))
    cid = client.get("/v1/contacts", headers=h).json()[0]["id"]
    client.delete(f"/v1/contacts/{cid}", headers=h)
    with db.tx() as conn:
        bodies = [r["body"] for r in conn.execute(db.messages.select()).mappings()]
    assert bodies == [""]


def test_every_adapter_has_a_contract():
    for ch in channels.available():
        a = channels.adapter(ch)
        assert a.channel == ch and a.address_kind in ("phone", "email", "id")


def test_hook_request_dataclass():
    r = HookRequest("POST", "u", {}, {}, b"")
    assert r.form is None


def test_telegram_registers_its_own_webhook(client, mx, monkeypatch):
    """Connecting Telegram is paste-the-token: we make the secret and tell Telegram where to deliver."""
    from valeria_one import channels as CH
    from valeria_one.channels.telegram import Telegram
    biz, h = mx
    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content or b"{}")))
        if req.url.path.endswith("/setWebhook") and "BAD" in req.url.path:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        return httpx.Response(200, json={"ok": True, "result": True})
    monkeypatch.setenv("VO_REGISTER_WEBHOOKS", "1")
    monkeypatch.setattr(CH, "adapter", lambda ch, http=None: Telegram(http=_mock(handler)) if ch == "telegram" else CH._REGISTRY[ch]())
    r = client.post("/v1/channels", headers=h, json={"channel": "telegram", "name": "Bot", "config": {"bot_token": "123:OK"}})
    assert r.status_code == 201, r.text
    path, body = seen[-1]
    assert path == "/bot123:OK/setWebhook" and body["url"] == r.json()["webhook_url"] and len(body["secret_token"]) >= 24
    upd = {"update_id": 1, "message": {"message_id": 1, "date": 1761300000, "chat": {"id": 9}, "text": "hi"}}
    hook = r.json()["webhook_url"].replace("https://one.test", "")
    assert client.post(hook, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": body["secret_token"]}).status_code == 200
    bad = client.post("/v1/channels", headers=h, json={"channel": "telegram", "name": "Bot", "config": {"bot_token": "BAD"}})
    assert bad.status_code == 422 and "Unauthorized" in bad.json()["error"]["message"]
    assert client.delete(f"/v1/channels/{r.json()['id']}", headers=h).status_code == 204
    assert seen[-1][0] == "/bot123:OK/deleteWebhook"
    monkeypatch.setenv("VO_PUBLIC_URL", "http://203.0.113.10")
    plain = client.post("/v1/channels", headers=h, json={"channel": "telegram", "name": "Bot", "config": {"bot_token": "123:OK"}})
    assert plain.status_code == 422 and "HTTPS" in plain.json()["error"]["message"]
