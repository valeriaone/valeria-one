"""Shared pieces for the v0.13 tests (Valeria, the owner's assistant): a business with its own Telegram bot (where the
owner talks to Valeria) and its own messaging gateway (where customers are), and one in-process network that answers
Telegram, the gateway and the business's own Gemini — nothing ever leaves the machine."""
import base64
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.channels import HookRequest
from valeria_one.services import ai as AI
from valeria_one.services import autoreply
from valeria_one.services import booking as BK
from valeria_one.services import businesses as B
from valeria_one.services import contacts as C
from valeria_one.services import inbox as X
from valeria_one.services import owner as OW

NOW = lambda: datetime.now(timezone.utc)                     # noqa: E731
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
ALL_WEEK = {d: [["08:00", "20:00"]] for d in BK.DAYS}
TG_SECRET = "tg-secret-123"


def refuse_network(monkeypatch):
    def refuse(self, request):
        raise RuntimeError(f"real network call in a test: {request.url}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


class Net:
    """Telegram (the owner's chat), the business's gateway (customers) and Gemini (the business's own key)."""

    def __init__(self, ai_text=None, audio_words=None):
        self.tg, self.gw, self.ai_calls, self.files = [], [], [], {}
        self.ai_text = ai_text                                 # str | callable(prompt) -> str | None (= no answer)
        self.audio_words = audio_words

    def __call__(self, req: httpx.Request):
        url = str(req.url)
        if "api.telegram.org" in url:
            if "/getFile" in url:
                fid = req.url.params.get("file_id")
                return httpx.Response(200, json={"ok": True, "result": {"file_path": f"voice/{fid}.oga"}})
            if "/file/bot" in url:
                fid = url.rsplit("/", 1)[-1].split(".")[0]
                return httpx.Response(200, content=self.files.get(fid, b"OggS-fake-audio"))
            body = json.loads(req.content or b"{}")
            self.tg.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.tg)}})
        if "gw.example" in url:
            self.gw.append(json.loads(req.content))
            return httpx.Response(200, json={"id": f"gw-{len(self.gw)}"})
        if "generativelanguage.googleapis.com" in url:
            j = json.loads(req.content)
            self.ai_calls.append(j)
            parts = j["contents"][-1]["parts"]
            if any("inline_data" in p for p in parts):
                if self.audio_words is None:
                    return httpx.Response(400, json={"error": {"message": "no audio"}})
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": self.audio_words}]}}]})
            prompt = " ".join(p.get("text", "") for p in parts)
            out = self.ai_text(prompt, j) if callable(self.ai_text) else self.ai_text
            if out is None:
                return httpx.Response(503, json={"error": {"message": "busy"}})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": out}]}}]})
        return httpx.Response(404, json={"error": "unexpected " + url})

    @property
    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    def owner_texts(self):
        return [m.get("text") for m in self.tg]

    def last(self):
        return (self.tg[-1].get("text") if self.tg else "") or ""


def shop(country="MX", name="Estética Sol", booking=True, lang=None):
    biz, _ = make_business(country, "salon", name=name)
    with db.tx() as conn:
        tg = X.connect(conn, biz, "t", channel="telegram", name="Bot", cfg={"bot_token": "123:abc", "secret": TG_SECRET})
        gw = X.connect(conn, biz, "t", channel="webhook", name="Gateway", cfg={"url": "https://gw.example/send", "secret": "s3"})
        if booking:
            biz = BK.save_settings(conn, biz, "t", enabled=True, slug=f"sol-{str(biz['id'])[:6]}", hours=ALL_WEEK, notice_minutes=0,
                                   days_ahead=30)
            BK.add_service(conn, biz, "t", name="Corte", minutes=60, price_minor=20000)
        if lang:
            biz = B.patch(conn, biz["id"], "t", language=lang)
        biz = B.get(conn, biz["id"])
    return biz, tg, gw


def raw_acc(acc_id):
    with db.tx() as conn:
        return conn.execute(db.channel_accounts.select().where(db.channel_accounts.c.id == acc_id)).mappings().one()


_mid = [0]


def tg_update(chat_id, text="", voice=None, photo=None, name="Dueña"):
    _mid[0] += 1
    m = {"message_id": _mid[0], "chat": {"id": chat_id}, "from": {"first_name": name}, "date": int(NOW().timestamp())}
    if text:
        m["text" if not photo else "caption"] = text
    if voice:
        m["voice"] = {"file_id": voice, "mime_type": "audio/ogg"}
    if photo:
        m["photo"] = [{"file_id": photo + "-s", "width": 90, "height": 90}, {"file_id": photo, "width": 800, "height": 600}]
    return json.dumps({"update_id": _mid[0], "message": m}).encode()


def tg_in(acc, chat_id, text="", net=None, **kw):
    """The owner (or anyone) writes to the business's Telegram bot; Valeria answers in line. -> (stats, answers)."""
    st = X.receive(raw_acc(acc["id"]), HookRequest("POST", "", {"x-telegram-bot-api-secret-token": TG_SECRET}, {},
                                                   tg_update(chat_id, text, **kw)))
    h = (net or Net()).http
    owner = [autoreply.respond_owner(acc["business_id"], mid, http=h) for mid in st.get("owner_ids", [])]
    return st, owner


def gw_in(acc, who, text="", name=None, media=None, net=None, pid=None):
    """A customer writes on the business's gateway; the usual after-message steps run in line."""
    from valeria_one.crypto import hmac_sha256_hex
    body = json.dumps({"from": who, "body": text, "name": name, "media": media or [], "id": pid or f"in-{who}-{NOW().timestamp()}"}).encode()
    st = X.receive(raw_acc(acc["id"]), HookRequest("POST", "", {"x-vo-signature": hmac_sha256_hex("s3", body)}, {}, body))
    h = (net or Net()).http
    return st, [autoreply.respond(acc["business_id"], cid, http=h) for cid in st.get("conversation_ids", [])]


def link_owner(biz, tg, chat_id="900", role="owner", perms=None, net=None):
    """Pair a phone the toddler-proof way: a code made on the screen, sent from the phone."""
    with db.tx() as conn:
        row = OW.start_pairing(conn, biz, "t", role=role, perms=perms)
    net = net or Net()
    st, out = tg_in(tg, chat_id, f"hola {row['pair_code']}", net=net)
    assert out == ["paired"], (st, out)
    with db.tx() as conn:
        return OW.match(conn, biz["id"], "telegram", chat_id)


def with_ai(biz):
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="AIza-own-key", mode="suggest")
        return B.get(conn, biz["id"])


def customer(biz, gw, name, who=None, consent=True, phone=None):
    with db.tx() as conn:
        data = {"name": name}
        if consent:
            data.update(marketing_consent=True, marketing_consent_source="signed at the counter")
        if phone:
            data["phone"] = phone
        c = C.create(conn, biz, "t", data)
        C.link(conn, biz["id"], c["id"], "webhook", who or f"id-{name}")
    return c


def appt(biz, c, start, status="booked", title="Corte", minutes=60):
    from valeria_one.services import deals as D
    with db.tx() as conn:
        a = D.book(conn, biz, "t", {"title": title, "contact_id": c["id"] if c else None, "starts_at": start,
                                    "ends_at": start + timedelta(minutes=minutes), "allow_overlap": True})
        if status != "booked":
            a = D.set_appointment_status(conn, biz["id"], "t", a["id"], status)
    return a


def actions(biz, **where):
    with db.tx() as conn:
        q = db.assistant_actions.select().where(db.assistant_actions.c.business_id == biz["id"])
        for k, v in where.items():
            q = q.where(db.assistant_actions.c[k] == v)
        return [dict(r) for r in conn.execute(q.order_by(db.assistant_actions.c.created_at)).mappings()]


def due_now(biz):
    """Let the undo minute pass: everything approved is due."""
    with db.tx() as conn:
        conn.execute(db.assistant_actions.update().where(db.assistant_actions.c.business_id == biz["id"],
                                                         db.assistant_actions.c.status == "queued")
                     .values(send_at=NOW() - timedelta(seconds=1)))


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def owner_session(client, biz, role="owner"):
    from valeria_one.services import users as U
    email = f"{role}-{str(biz['id'])[:6]}@example.com"
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email=email, name="Dueña Sol", password_hash=U._ph.hash("una-clave-larga"))
                           .returning(db.users.c.id)).scalar()
        U.add_member(conn, biz["id"], uid, role)
    tok = csrf(client, "/login")
    client.post("/login", data={"csrf": tok, "email": email, "password": "una-clave-larga"})
    return uid
