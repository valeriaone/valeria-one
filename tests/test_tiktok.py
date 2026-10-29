import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from conftest import make_business
from valeria_one import channels, crypto, db
from valeria_one.channels import HookRequest, Outbound
from valeria_one.crypto import hmac_sha256_hex
from valeria_one.services import Blocked, Invalid
from valeria_one.services import inbox as X
from valeria_one.services import tiktok as TT

LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
SECRET = "tt-app-secret"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(self, request):
        raise RuntimeError(f"real network call in a test: {request.url}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture
def tiktok_on(monkeypatch):
    monkeypatch.setenv("VO_TIKTOK_APP_ID", "tt-app")
    monkeypatch.setenv("VO_TIKTOK_APP_SECRET", SECRET)


class TikTokAPI:
    """business-api.tiktok.com, answered in-process."""

    def __init__(self):
        self.calls, self.refresh_ok, self.n = [], True, 0

    def __call__(self, req: httpx.Request):
        body = json.loads(req.content) if req.content else {}
        self.calls.append((req.url.path, dict(req.headers), body, str(req.url)))
        p = req.url.path
        if p.endswith("/tt_user/oauth2/token/"):
            return httpx.Response(200, json={"code": 0, "message": "OK", "data": {
                "open_id": "biz-open-1", "access_token": "act-1", "expires_in": 86400, "refresh_token": "rft-1",
                "refresh_token_expires_in": 2592000, "scope": "user.info.basic,message.list.read,message.list.send,message.list.manage"}})
        if p.endswith("/tt_user/oauth2/refresh_token/"):
            if not self.refresh_ok:
                return httpx.Response(200, json={"code": 40104, "message": "refresh token expired", "data": {}})
            self.n += 1
            return httpx.Response(200, json={"code": 0, "message": "OK", "data": {
                "access_token": f"act-new-{self.n}", "expires_in": 86400, "refresh_token": f"rft-new-{self.n}", "refresh_token_expires_in": 2592000}})
        if p.endswith("/business/get/"):
            return httpx.Response(200, json={"code": 0, "data": {"username": "estetica.sol", "display_name": "Estética Sol"}})
        if p.endswith("/business/message/send/"):
            return httpx.Response(200, json={"code": 0, "message": "OK", "data": {"message_id": f"m-out-{len(self.calls)}"}})
        return httpx.Response(404, json={"code": 40400, "message": f"unexpected {p}"})

    @property
    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    def sends(self):
        return [c for c in self.calls if c[0].endswith("/business/message/send/")]


def connect(country="MX", api=None):
    """A business that connected its TikTok Business Account (through TikTok's sign-in)."""
    biz, _ = make_business(country, "salon", name="Estética Sol")
    api = api or TikTokAPI()
    with db.tx() as conn:
        url = TT.start(conn, biz, None)
    state = re.search(r"state=([^&]+)", url).group(1)
    bid, acc = TT.finish(state, "auth-code", http=api.http)
    assert bid == biz["id"]
    return biz, acc, api


def event(text="Hola, ¿tienen cita mañana?", conv="conv-77", mid="m-1", kind="im_receive_msg", to="biz-open-1", sender="user-9",
          ts=None):
    content = {"conversation_id": conv, "message_id": mid, "timestamp": int((ts or time.time()) * 1000), "type": "text",
               "text": {"body": text}, "from": "Rosa", "from_user": {"id": sender}, "to": "Estética Sol", "to_user": {"id": to}}
    return {"event": kind, "user_openid": to, "content": json.dumps(content)}


def signed(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict:
    t = str(ts or int(time.time()))
    return {"TikTok-Signature": f"t={t},s={hmac_sha256_hex(secret, t.encode() + b'.' + body)}", "Content-Type": "application/json"}


def deliver(client, ev, **kw):
    body = json.dumps(ev).encode()
    return client.post("/hooks/tiktok", content=body, headers=signed(body, **kw))


def owner_session(client, biz):
    from valeria_one.services import users as U
    email = f"owner-{str(biz['id'])[:6]}@example.com"
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email=email, name="Dueña", password_hash=U._ph.hash("una-clave-larga"))
                           .returning(db.users.c.id)).scalar()
        U.add_member(conn, biz["id"], uid)
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"csrf": tok, "email": email, "password": "una-clave-larga"})


# ---------------------------------------------------------------- dormant
def test_dormant_without_the_operators_app(client):
    assert not TT.configured() and TT.refresh_due() == {"accounts": 0}
    biz, _ = make_business("MX", "salon")
    owner_session(client, biz)
    page = client.get("/app/tiktok").text
    assert "Not connected yet" in page or "Aún no conectado" in page
    assert 'href="/app/tiktok/connect"' not in page
    r = client.get("/app/tiktok/connect", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] != "https://www.tiktok.com"   # refused calmly, back to a page
    body = json.dumps(event()).encode()
    assert client.post("/hooks/tiktok", content=body, headers=signed(body)).status_code == 403
    assert "tiktok" in channels.available()
    with db.tx() as conn, pytest.raises(Invalid):
        X.connect(conn, biz, "t", channel="tiktok", name="x", cfg={"business_id": "b", "access_token": "a"})   # no pasting tokens
    assert 'value="tiktok"' not in client.get("/app/channels").text


def test_not_offered_in_the_eea_switzerland_or_the_uk(client, tiktok_on):
    for cc in ("DE", "FR", "CH", "GB", "NO"):
        assert not TT.available_in(cc)
    assert TT.available_in("MX") and TT.available_in("US") and TT.available_in("PH")
    biz, _ = make_business("DE", "salon")
    with db.tx() as conn, pytest.raises(Invalid):
        TT.start(conn, biz, None)
    owner_session(client, biz)
    page = client.get("/app/tiktok").text
    assert ("EEA" in page or "EWR" in page or "EEE" in page) and 'href="/app/tiktok/connect"' not in page


# ---------------------------------------------------------------- connecting
def test_connect_with_tiktoks_sign_in(client, tiktok_on):
    biz, acc, api = connect()
    url = TT.AUTH
    assert url.startswith("https://www.tiktok.com/")
    tok_req = api.calls[0]
    assert tok_req[2] == {"client_id": "tt-app", "client_secret": SECRET, "grant_type": "authorization_code", "auth_code": "auth-code",
                          "redirect_uri": "https://one.test/app/tiktok/callback"}
    assert acc["channel"] == "tiktok" and acc["name"] == "@estetica.sol"
    with db.tx() as conn:
        raw = conn.execute(select(db.channel_accounts.c.config).where(db.channel_accounts.c.channel == "tiktok")).scalar()
    assert raw["refresh_token"].startswith("enc:") and raw["access_token"].startswith("enc:")           # both sealed
    assert crypto.open_(raw)["refresh_token"] == "rft-1" and raw["business_id"] == "biz-open-1"
    assert acc["config"]["refresh_token"] == "••••"
    with db.tx() as conn:
        url2 = TT.start(conn, biz, None)
    TT.finish(re.search(r"state=([^&]+)", url2).group(1), "auth-code-2", http=api.http)               # again: renewed in place
    with db.tx() as conn:
        assert conn.execute(select(db.func.count()).select_from(db.channel_accounts).where(db.channel_accounts.c.channel == "tiktok")).scalar() == 1
    with pytest.raises(TT.TikTokError):
        TT.finish(re.search(r"state=([^&]+)", url2).group(1), "auth-code-3", http=api.http)           # a state works once
    other, _ = make_business("MX", "salon", name="Otra")
    with db.tx() as conn:
        url3 = TT.start(conn, other, None)
    with pytest.raises(TT.TikTokError):
        TT.finish(re.search(r"state=([^&]+)", url3).group(1), "c", http=api.http)                     # one TikTok, one business


def test_messages_need_the_scopes(tiktok_on):
    api = TikTokAPI()
    biz, _ = make_business("MX", "salon")

    def narrow(req):
        if req.url.path.endswith("/tt_user/oauth2/token/"):
            return httpx.Response(200, json={"code": 0, "data": {"open_id": "x", "access_token": "a", "scope": "user.info.basic"}})
        return api(req)
    with db.tx() as conn:
        url = TT.start(conn, biz, None)
    with pytest.raises(TT.TikTokError):
        TT.finish(re.search(r"state=([^&]+)", url).group(1), "c", http=httpx.Client(transport=httpx.MockTransport(narrow)))


# ---------------------------------------------------------------- the webhook: signed, routed, into the inbox
def test_webhook_brings_messages_into_the_inbox(client, tiktok_on):
    biz, acc, api = connect()
    r = deliver(client, event())
    assert r.status_code == 200
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.business_id == biz["id"])).mappings().one()
        m = conn.execute(select(db.messages)).mappings().one()
        ident = conn.execute(select(db.identities.c.channel, db.identities.c.address)).one()
    assert c["name"] == "Rosa" and c["source"] == "tiktok" and m["body"] == "Hola, ¿tienen cita mañana?" and m["direction"] == "in"
    assert tuple(ident) == ("tiktok", "conv-77")
    assert deliver(client, event()).status_code == 200                                     # TikTok retries: stored once
    deliver(client, event("echo", mid="m-2", kind="im_send_msg", sender="biz-open-1", to="user-9"))   # the business's own message
    deliver(client, {**event(mid="m-3"), "user_openid": "someone-else"})                  # not ours
    with db.tx() as conn:
        assert conn.execute(select(db.func.count()).select_from(db.messages)).scalar() == 1


def test_webhook_signature_is_checked(client, tiktok_on):
    connect()
    body = json.dumps(event()).encode()
    assert client.post("/hooks/tiktok", content=body, headers=signed(body, secret="wrong")).status_code == 403
    assert client.post("/hooks/tiktok", content=body, headers=signed(body, ts=int(time.time()) - 3600)).status_code == 403   # replay
    assert client.post("/hooks/tiktok", content=body, headers={"Content-Type": "application/json"}).status_code == 403
    tampered = body.replace(b"cita", b"CITA")
    assert client.post("/hooks/tiktok", content=tampered, headers=signed(body)).status_code == 403
    with db.tx() as conn:
        assert conn.execute(select(db.func.count()).select_from(db.messages)).scalar() == 0


# ---------------------------------------------------------------- the rules: they write first, 48 hours
def test_reply_inside_48_hours_goes_out(client, tiktok_on):
    biz, acc, api = connect()
    deliver(client, event())
    with db.tx() as conn:
        cid = conn.execute(select(db.contacts.c.id)).scalar()
    msg = X.send(biz, "t", contact_id=cid, body="¡Claro! Mañana a las 10.", channel="tiktok", http=api.http)
    assert msg["status"] == "sent" and msg["provider_id"].startswith("m-out-")
    path, headers, body, _ = api.sends()[0]
    assert headers["access-token"] == "act-1"
    assert body == {"business_id": "biz-open-1", "recipient_type": "CONVERSATION", "recipient": "conv-77", "message_type": "TEXT",
                    "text": {"body": "¡Claro! Mañana a las 10."}}


def test_the_customer_writes_first_and_48_hours_is_the_limit(client, tiktok_on):
    biz, acc, api = connect()
    deliver(client, event())
    with db.tx() as conn:
        cid = conn.execute(select(db.contacts.c.id)).scalar()
        conn.execute(update(db.conversations).values(last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=49)))
    with pytest.raises(Blocked) as e:
        X.send(biz, "t", contact_id=cid, body="¿Sigues ahí?", channel="tiktok", http=api.http)
    assert e.value.reason == "window_closed" and "48" in str(e.value)
    with pytest.raises(Blocked):                                                           # no template opens TikTok's window
        X.send(biz, "t", contact_id=cid, body="x", template={"name": "hola"}, channel="tiktok", http=api.http)
    with db.tx() as conn:
        conn.execute(update(db.conversations).values(last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=47)))
    assert X.send(biz, "t", contact_id=cid, body="¡Aquí estamos!", channel="tiktok", http=api.http)["status"] == "sent"
    with db.tx() as conn:                                                                  # someone who never wrote on TikTok
        from valeria_one.services import contacts as C
        c2 = C.create(conn, biz, "t", {"name": "Nuevo"})
        C.link(conn, biz["id"], c2["id"], "tiktok", "conv-never")
    with pytest.raises(Blocked) as e:
        X.send(biz, "t", contact_id=c2["id"], body="Hola", channel="tiktok", http=api.http)
    assert e.value.reason == "customer_first"
    assert len(api.sends()) == 1


def test_inbox_screen_says_why_calmly(client, tiktok_on):
    biz, acc, api = connect()
    deliver(client, event())
    owner_session(client, biz)
    with db.tx() as conn:
        conv = conn.execute(select(db.conversations.c.id)).scalar()
        conn.execute(update(db.conversations).values(last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=60)))
    page = client.get(f"/app/inbox?c={conv}")
    assert page.status_code == 200 and "Hola, ¿tienen cita mañana?" in page.text
    tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    shown = client.post(f"/app/inbox/{conv}/send", data={"csrf": tok, "body": "Hola"}).text      # back on the chat, with the reason
    assert "48" in shown and api.sends() == []


# ---------------------------------------------------------------- tokens: 24 h access, 30 days refresh
def test_an_expired_access_token_is_renewed_before_sending(client, tiktok_on):
    biz, acc, api = connect()
    deliver(client, event())
    with db.tx() as conn:
        cid = conn.execute(select(db.contacts.c.id)).scalar()
        row = conn.execute(select(db.channel_accounts).where(db.channel_accounts.c.channel == "tiktok")).mappings().one()
    X.save_config(row["id"], {**crypto.open_(row["config"]), "expires_at": int(time.time()) - 10})
    msg = X.send(biz, "t", contact_id=cid, body="Hola", channel="tiktok", http=api.http)
    assert msg["status"] == "sent" and api.sends()[0][1]["access-token"] == "act-new-1"
    refresh = [c for c in api.calls if c[0].endswith("/refresh_token/")][0][2]
    assert refresh == {"client_id": "tt-app", "client_secret": SECRET, "grant_type": "refresh_token", "refresh_token": "rft-1"}
    with db.tx() as conn:
        cfg = crypto.open_(conn.execute(select(db.channel_accounts.c.config).where(db.channel_accounts.c.id == row["id"])).scalar())
    assert cfg["access_token"] == "act-new-1" and cfg["refresh_token"] == "rft-new-1" and cfg["expires_at"] > time.time() + 80000


def test_a_dead_refresh_token_means_reconnect(client, tiktok_on):
    biz, acc, api = connect()
    deliver(client, event())
    owner_session(client, biz)
    with db.tx() as conn:
        cid = conn.execute(select(db.contacts.c.id)).scalar()
        row = conn.execute(select(db.channel_accounts).where(db.channel_accounts.c.channel == "tiktok")).mappings().one()
    X.save_config(row["id"], {**crypto.open_(row["config"]), "expires_at": int(time.time()) - 10})
    api.refresh_ok = False
    msg = X.send(biz, "t", contact_id=cid, body="Hola", channel="tiktok", http=api.http)
    assert msg["status"] == "failed" and "TikTok" in msg["error"] and api.sends() == []
    assert 'href="/app/tiktok"' in client.get("/app").text                                # "TikTok needs you to reconnect"
    page = client.get("/app/tiktok").text
    assert "/app/tiktok/connect" in page


def test_the_job_renews_tokens_before_they_run_out(tiktok_on):
    biz, acc, api = connect()
    assert TT.refresh_due(api.http) == {"accounts": 1, "renewed": 0, "failed": 0}           # 24 h left: nothing to do
    assert TT.refresh_due(api.http, now=time.time() + 23 * 3600) == {"accounts": 1, "renewed": 1, "failed": 0}
    with db.tx() as conn:
        cfg = crypto.open_(conn.execute(select(db.channel_accounts.c.config)).scalar())
    assert cfg["access_token"] == "act-new-1" and cfg["refresh_token"] == "rft-new-1"
    api.refresh_ok = False
    assert TT.refresh_due(api.http, now=time.time() + 47 * 3600)["failed"] == 1
    TT.refresh_due(api.http, now=time.time() + 47 * 3600)
    with db.tx() as conn:
        cfg = crypto.open_(conn.execute(select(db.channel_accounts.c.config)).scalar())
        told = conn.execute(select(db.func.count()).select_from(db.events).where(db.events.c.type == "tiktok.reconnect")).scalar()
    assert cfg["error"] and told == 1                                                      # told once


def test_adapter_contract():
    a = channels.adapter("tiktok")
    assert a.reply_window_hours == 48 and a.templates is False and a.manual is False and a.address_kind == "id"
    req = HookRequest("POST", "u", {}, {}, b"{}")
    with pytest.raises(channels.BadSignature):
        a.parse({"business_id": "b"}, req)
    api = TikTokAPI()
    r = channels.adapter("tiktok", api.http).send({"business_id": "b", "access_token": "x"}, "conv",
                                                  Outbound(to="conv", body="Mira", media=[{"url": "https://one.test/f.jpg"}]))
    assert r.status == "sent" and api.sends()[0][2]["text"]["body"] == "Mira\nhttps://one.test/f.jpg"   # a picture goes as its link


def test_tiktok_page_in_every_language(client, tiktok_on):
    biz, acc, api = connect()
    owner_session(client, biz)
    for lang in LANGS:
        tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app").text).group(1)
        client.post(f"/lang/{lang}", data={"csrf": tok})
        r = client.get("/app/tiktok")
        assert r.status_code == 200 and "@estetica.sol" in r.text, lang
    assert 'href="/app/tiktok"' in client.get("/app/connect").text and "@estetica.sol" in client.get("/app/connect").text
