"""wati01 (v0.29): WhatsApp as complete as the paid inboxes, on the business's OWN WhatsApp account.

Templates written here, sent to Meta, read back (status, why rejected, the ones made in Meta's tool) and picked from a
list; sent in the language Meta approved with the right number of parts. The away message out of hours. A chat given
to someone on the team, and the team's own notes on it. The verified-badge guide. Nothing leaves the machine: Meta is
a mock, and the network is refused."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, update

from _va import Net, csrf, customer, gw_in, owner_session, raw_acc, refuse_network, shop
from conftest import make_business
from valeria_one import db
from valeria_one.channels import HookRequest
from valeria_one.services import Blocked, Invalid, autoreply
from valeria_one.services import away as AW
from valeria_one.services import inbox as X
from valeria_one.services import owner as OW
from valeria_one.services import wa_templates as WT

ROOT = Path(__file__).resolve().parent.parent
WA_CFG = {"phone_number_id": "PNID", "token": "wa-token", "app_secret": "APPSECRET", "verify_token": "VT", "waba_id": "WABA1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


class Meta:
    """Meta's Graph API for templates: what was asked, and what it answers."""

    def __init__(self, create=None, listing=None):
        self.calls = []
        self.create = create or (200, {"id": "9001", "status": "PENDING", "category": "UTILITY"})
        self.listing = listing or []

    def __call__(self, req: httpx.Request):
        self.calls.append((req.method, str(req.url), json.loads(req.content) if req.content else None, req.headers.get("authorization")))
        if req.method == "POST" and req.url.path.endswith("/WABA1/message_templates"):
            return httpx.Response(self.create[0], json=self.create[1])
        if req.method == "GET" and req.url.path.endswith("/WABA1/message_templates"):
            return httpx.Response(200, json={"data": self.listing})
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    @property
    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def wa_biz():
    biz, _ = make_business("MX", "fitness", name="Pulse Studio")
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="whatsapp", name="WhatsApp", cfg=WA_CFG)
    return biz, acc


# ---------------------------------------------------------------- templates
def test_template_rules_before_anything_goes_to_meta():
    biz, acc = wa_biz()
    with db.tx() as conn:
        for kw, field in [({"name": "Class Reminder"}, "name"), ({"language": "spanish"}, "language"), ({"category": "PROMO"}, "category"),
                          ({"body": ""}, "body"), ({"body": "Hola {{1}}, a las {{3}}", "example": "Ana|10"}, "body"),
                          ({"body": "Hola {{1}}", "example": ""}, "example")]:
            args = {"account_id": acc["id"], "name": "class_reminder", "language": "es_MX", "category": "UTILITY",
                    "body": "Hola {{1}}, tu clase es mañana.", "example": "Ana", **kw}
            with pytest.raises(Invalid) as e:
                WT.save(conn, biz, "t", **args)
            assert e.value.field == field, (kw, e.value.field)
        t = WT.save(conn, biz, "t", account_id=acc["id"], name="class_reminder", language="es_MX", category="utility",
                    body="Hola {{1}}, tu clase es mañana.", example="Ana")
        assert t["status"] == "DRAFT" and t["example"] == ["Ana"] and t["category"] == "UTILITY"
        with pytest.raises(Invalid):                           # the same name + language twice on one account
            WT.save(conn, biz, "t", account_id=acc["id"], name="class_reminder", language="es_MX", category="UTILITY", body="Otra")
        assert WT.default_language(biz) == "es_MX"


def test_send_to_meta_then_read_back_approved_and_the_ones_made_in_meta():
    biz, acc = wa_biz()
    with db.tx() as conn:
        t = WT.save(conn, biz, "t", account_id=acc["id"], name="class_reminder", language="es_MX", category="UTILITY",
                    body="Hola {{1}}, tu clase es mañana a las {{2}}.", example="Ana | 10:00")
    meta = Meta()
    row = WT.submit(biz, "t", t["id"], http=meta.http)
    method, url, body, auth = meta.calls[-1]
    assert method == "POST" and "/WABA1/message_templates" in url and auth == "Bearer wa-token"   # the business's own token
    assert body == {"name": "class_reminder", "language": "es_MX", "category": "UTILITY",
                    "components": [{"type": "BODY", "text": "Hola {{1}}, tu clase es mañana a las {{2}}.",
                                    "example": {"body_text": [["Ana", "10:00"]]}}]}
    assert row["status"] == "PENDING" and row["meta_id"] == "9001"
    assert WT.submit(biz, "t", t["id"], http=meta.http)["status"] == "PENDING" and len(meta.calls) == 1   # never sent twice
    with db.tx() as conn:
        with pytest.raises(Invalid):                           # Meta has it: changed and deleted in Meta, not here
            WT.delete(conn, biz, "t", t["id"])
    meta.listing = [
        {"id": "9001", "name": "class_reminder", "language": "es_MX", "status": "APPROVED", "category": "UTILITY",
         "components": [{"type": "BODY", "text": "Hola {{1}}, tu clase es mañana a las {{2}}."}]},
        {"id": "9002", "name": "promo_noviembre", "language": "es_MX", "status": "REJECTED", "category": "MARKETING",
         "rejected_reason": "INVALID_FORMAT", "components": [{"type": "BODY", "text": "Promo {{1}}"}]},
        {"id": "9003", "name": "hola", "language": "es", "status": "APPROVED", "category": "UTILITY",
         "components": [{"type": "HEADER", "format": "TEXT", "text": "x"}, {"type": "BODY", "text": "Hola, ¿cómo te ayudamos?"}]},
        {"id": "bad", "name": "Not Valid!", "language": "es", "status": "APPROVED", "components": [{"type": "BODY", "text": "x"}]},
    ]
    out = WT.sync(biz, acc["id"], http=meta.http)
    assert out == {"updated": 1, "added": 2}
    with db.tx() as conn:
        rows = {r["name"]: r for r in WT.list_(conn, biz["id"], acc["id"])}
        assert rows["class_reminder"]["status"] == "APPROVED" and rows["class_reminder"]["params"] == 2
        assert rows["promo_noviembre"]["status"] == "REJECTED" and rows["promo_noviembre"]["reason"] == "INVALID_FORMAT"
        assert rows["hola"]["body"] == "Hola, ¿cómo te ayudamos?" and "Not Valid!" not in rows
        # each screen is offered what it can fill: campaigns / inbox know {{1}}; reminders also know {{2}}
        assert [r["name"] for r in WT.approved(conn, biz["id"])] == ["hola"]
        assert [r["name"] for r in WT.approved(conn, biz["id"], max_params=2)] == ["class_reminder", "hola"]
        # sent in the language Meta approved (not the business's "es"), with exactly the parts it has
        assert WT.for_sending(conn, biz["id"], acc["id"], "class_reminder", "es", "Ana", more=["10:00"]) == \
            {"name": "class_reminder", "language": "es_MX", "params": ["Ana", "10:00"]}
        assert WT.for_sending(conn, biz["id"], acc["id"], "hola", "es_MX", "Ana") == {"name": "hola", "language": "es", "params": []}
        # a name typed by hand before v0.29 keeps its screen's old way
        assert WT.for_sending(conn, biz["id"], acc["id"], "old_one", "es", "Ana") == {"name": "old_one", "language": "es", "params": ["Ana"]}
        assert WT.for_sending(conn, biz["id"], acc["id"], "old_one", "es", "Ana", legacy_params=False)["params"] == []
        WT.delete(conn, biz, "t", rows["promo_noviembre"]["id"])  # a rejected one can go
        assert "promo_noviembre" not in {r["name"] for r in WT.list_(conn, biz["id"])}


def test_meta_says_no_the_reason_is_kept_and_nothing_crashes():
    biz, acc = wa_biz()
    with db.tx() as conn:
        t = WT.save(conn, biz, "t", account_id=acc["id"], name="x1", language="es_MX", category="MARKETING", body="Oferta")
    meta = Meta(create=(400, {"error": {"message": "Invalid parameter", "error_user_msg": "El nombre ya existe en otro idioma"}}))
    row = WT.submit(biz, "t", t["id"], http=meta.http)
    assert row["status"] == "ERROR" and row["reason"] == "El nombre ya existe en otro idioma"
    with db.tx() as conn:                                      # an error can be changed and sent again
        again = WT.save(conn, biz, "t", account_id=acc["id"], name="x1", language="es_MX", category="MARKETING", body="Oferta 2",
                        tid=t["id"])
    assert again["status"] == "DRAFT" and again["reason"] is None


def test_no_waba_id_says_how_to_fix_it():
    biz, _ = make_business("MX", "fitness")
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="whatsapp", name="WA", cfg={k: v for k, v in WA_CFG.items() if k != "waba_id"})
        t = WT.save(conn, biz, "t", account_id=acc["id"], name="x", language="es_MX", category="UTILITY", body="Hola")
        assert [a["ready"] for a in WT.accounts(conn, biz["id"])] == [False]
    with pytest.raises(WT.TemplateError) as e:
        WT.submit(biz, "t", t["id"], http=Meta().http)
    assert "waba_id" in str(e.value)


def test_pending_templates_are_read_back_by_the_jobs_timer_every_5_minutes():
    biz, acc = wa_biz()
    with db.tx() as conn:
        t = WT.save(conn, biz, "t", account_id=acc["id"], name="t1", language="es_MX", category="UTILITY", body="Hola")
    meta = Meta()
    WT.submit(biz, "t", t["id"], http=meta.http)
    meta.listing = [{"id": "9001", "name": "t1", "language": "es_MX", "status": "APPROVED", "components": [{"type": "BODY", "text": "Hola"}]}]
    assert WT.sync_pending(http=meta.http) == 0                # just checked: waits its 5 minutes
    with db.tx() as conn:
        conn.execute(update(db.wa_templates).values(synced_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
    assert WT.sync_pending(http=meta.http) == 1
    with db.tx() as conn:
        assert WT.get(conn, biz["id"], t["id"])["status"] == "APPROVED"
    from valeria_one import jobs
    assert "templates" in jobs.__doc__


def test_campaign_outside_the_window_sends_the_approved_language(monkeypatch):
    """campaigns.deliver: Blocked outside the window -> the template, in the language Meta approved."""
    from valeria_one.services import campaigns as CG
    biz, acc = wa_biz()
    with db.tx() as conn:
        conn.execute(db.wa_templates.insert().values(business_id=biz["id"], channel_account_id=acc["id"], name="promo", language="es_MX",
                                                     category="MARKETING", body="Hola {{1}}, hay clase gratis.", status="APPROVED"))
    from valeria_one.services import contacts as C
    with db.tx() as conn:
        c = C.create(conn, biz, "t", {"name": "Ana López", "phone": "+529991234567", "marketing_consent": True,
                                       "marketing_consent_source": "form"})
        C.link(conn, biz["id"], c["id"], "whatsapp", "+529991234567")
    seen = []

    def fake_send(b, actor, **kw):
        seen.append(kw.get("template"))
        if not kw.get("template"):
            raise Blocked("outside_reply_window", "24 h")
        return {"id": None, "status": "sent"}
    monkeypatch.setattr(X, "send", fake_send)
    monkeypatch.setattr(CG, "polite_until", lambda b, n: None)
    with db.tx() as conn:
        contact = dict(conn.execute(select(db.contacts).where(db.contacts.c.id == c["id"])).mappings().one())
    out = CG.deliver({"token": "tk", "created_at": datetime.now(timezone.utc)}, biz, contact,
                     {"channel": "whatsapp", "template": "promo", "message": "x"}, kind="marketing", track_opens=False,
                     http=None, now=datetime.now(timezone.utc))
    assert out[0] == "sent" and seen[-1] == {"name": "promo", "language": "es_MX", "params": ["Ana"]}


# ---------------------------------------------------------------- the away message
def test_open_or_closed_and_when_it_opens_again():
    tz = ZoneInfo("America/Merida")
    h = {"mon": [["09:00", "14:00"], ["16:00", "20:00"]], "fri": [["22:00", "02:00"]]}
    mon = datetime(2026, 11, 2, 15, 0, tzinfo=tz)              # a Monday, the lunch gap
    assert not AW.is_open(h, mon) and AW.is_open(h, mon.replace(hour=10)) and not AW.is_open(h, mon.replace(hour=20, minute=30))
    assert AW.next_open(h, mon)[0] == 0 and AW.next_open(h, mon)[1].hour == 16
    assert AW.when_text(h, mon, "es") == "hoy 16:00" and AW.when_text(h, mon.replace(hour=21), "es") == "viernes 22:00"
    sat = datetime(2026, 11, 7, 1, 0, tzinfo=tz)              # Saturday 01:00: Friday's 22:00–02:00 still running
    assert AW.is_open(h, sat) and not AW.is_open(h, sat.replace(hour=3))
    assert AW.when_text(h, datetime(2026, 11, 1, 12, 0, tzinfo=tz), "en") == "tomorrow 09:00"   # Sunday
    assert AW.next_open({}, mon) is None


def _closed_now(biz):
    """The last 23:00 in the shop's own time zone (ALL_WEEK = 08:00–20:00: closed) — in the past, so "once per night"
    compares it with events the database stamps with the real time."""
    tz = ZoneInfo(biz["timezone"])
    now = datetime.now(tz)
    at = now.replace(hour=23, minute=0, second=0, microsecond=0)
    return (at if at <= now else at - timedelta(days=1)).astimezone(timezone.utc)


def test_away_message_once_per_night_only_when_closed_and_switched_on():
    biz, tg, gw = shop()
    customer(biz, gw, "Ana", who="ana")
    st = X.receive(raw_acc(gw["id"]), HookRequest("POST", "", {"x-vo-signature": _sig(b := _body("ana", "¿Hay clase mañana?"))}, {}, b))
    conv = st["conversation_ids"][0]
    net = Net()
    night = _closed_now(biz)
    assert AW.respond(biz["id"], conv, http=net.http, now=night) == "off"     # off until the owner switches it on
    with db.tx() as conn:
        AW.save(conn, biz, "t", True, "")
    assert AW.respond(biz["id"], conv, http=net.http, now=night - timedelta(hours=10)) == "open"   # 13:00: open, nothing sent
    assert not net.gw
    assert AW.respond(biz["id"], conv, http=net.http, now=night) == "sent"
    out = net.gw[-1]["body"]
    assert biz["name"] in out and "08:00" in out                # «abrimos mañana 08:00»
    st = X.receive(raw_acc(gw["id"]), HookRequest("POST", "", {"x-vo-signature": _sig(b := _body("ana", "¿Hola?", "2"))}, {}, b))
    assert AW.respond(biz["id"], conv, http=net.http, now=night + timedelta(minutes=5)) == "already"   # once per night
    with db.tx() as conn:
        AW.save(conn, biz, "t", True, "Cerrado. Volvemos {next}.")
        conn.execute(db.events.delete().where(db.events.c.type == "away.sent"))
    X.receive(raw_acc(gw["id"]), HookRequest("POST", "", {"x-vo-signature": _sig(b := _body("ana", "hola", "3"))}, {}, b))
    assert AW.respond(biz["id"], conv, http=net.http, now=night) == "sent" and net.gw[-1]["body"].startswith("Cerrado. Volvemos ")
    with db.tx() as conn:
        conn.execute(db.events.delete().where(db.events.c.type == "away.sent"))
    X.receive(raw_acc(gw["id"]), HookRequest("POST", "", {"x-vo-signature": _sig(b := _body("ana", "STOP", "4"))}, {}, b))
    assert AW.respond(biz["id"], conv, http=net.http, now=night) == "not_last"   # an opt-out word gets nothing
    with db.tx() as conn:
        OW.pause(conn, biz, "t")
    assert AW.respond(biz["id"], conv, http=net.http, now=night) == "off"         # "Valeria, stop" stops it too
    with pytest.raises(Invalid):
        with db.tx() as conn:
            AW.save(conn, biz, "t", True, "x" * 501)


def test_away_needs_hours_and_runs_only_when_nothing_else_answered(monkeypatch):
    biz, tg, gw = shop(booking=False)
    with db.tx() as conn:
        AW.save(conn, biz, "t", True, "")
    customer(biz, gw, "Ana", who="ana")
    st, out = gw_in(gw, "ana", "hola")
    assert out == ["off"]                                       # no opening hours: there is no "closed"
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(hours={"mon": [["09:00", "10:00"]]}))
    monkeypatch.setattr(AW, "is_open", lambda h, local: False)
    net = Net()
    st, out = gw_in(gw, "ana", "¿abren hoy?", pid="p2", net=net)
    assert out == ["away:sent"] and net.gw                     # nothing else answered -> the away message


# ---------------------------------------------------------------- the team on a chat
def test_assign_a_chat_and_team_notes():
    biz, tg, gw = shop()
    customer(biz, gw, "Ana", who="ana")
    st, _ = gw_in(gw, "ana", "hola")
    conv = st["conversation_ids"][0]
    from valeria_one.services import users as U
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email="coach@example.com", name="Coach Leo", password_hash="x").returning(db.users.c.id)).scalar()
        U.add_member(conn, biz["id"], uid, "staff")
        outsider = conn.execute(db.users.insert().values(email="otro@example.com", name="Otro", password_hash="x").returning(db.users.c.id)).scalar()
        with pytest.raises(Invalid):
            X.assign(conn, biz["id"], "t", conv, outsider)
        X.assign(conn, biz["id"], "t", conv, uid)
        rows = X.conversations(conn, biz["id"], assignee=uid)
        assert [r["id"] for r in rows] == [conv] and rows[0]["assignee_name"] == "Coach Leo"
        assert X.conversations(conn, biz["id"], assignee="none") == []
        X.assign(conn, biz["id"], "t", conv, None)
        assert [r["id"] for r in X.conversations(conn, biz["id"], assignee="none")] == [conv]
        with pytest.raises(Invalid):
            X.add_note(conn, biz["id"], "t", conv, "   ", None)
        n = X.add_note(conn, biz["id"], "t", conv, "Quiere clase de prueba el sábado", {"id": uid, "name": "Coach Leo"})
        assert n["author_name"] == "Coach Leo"
        assert [x["body"] for x in X.notes(conn, biz["id"], conv)] == ["Quiere clase de prueba el sábado"]
        msgs = conn.execute(select(db.messages.c.body).where(db.messages.c.conversation_id == conv)).scalars().all()
    assert "Quiere clase de prueba el sábado" not in msgs       # a note is never a message to the customer
    from valeria_one.services import export as EX
    assert "conversation_notes" in EX.TABLES and "wa_templates" in EX.TABLES   # the owner leaves with all of it


# ---------------------------------------------------------------- screens
def test_screens(client):
    biz, tg, gw = shop()
    customer(biz, gw, "Ana", who="ana")
    st, _ = gw_in(gw, "ana", "hola")
    conv = st["conversation_ids"][0]
    me = owner_session(client, biz)
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="whatsapp", name="WhatsApp", cfg=WA_CFG)
        conn.execute(db.wa_templates.insert().values(business_id=biz["id"], channel_account_id=acc["id"], name="clase_gratis",
                                                     language="es_MX", category="MARKETING", body="Hola {{1}}", status="APPROVED"))
    r = client.get("/app/channels")
    assert f"/app/channels/{acc['id']}/templates" in r.text and "cfg_waba_id" in r.text and "/app/help/whatsapp-badge" in r.text
    r = client.get(f"/app/channels/{acc['id']}/templates")
    assert r.status_code == 200 and "clase_gratis" in r.text and "Aprobada" in r.text
    tok = csrf(client, f"/app/channels/{acc['id']}/templates")
    r = client.post(f"/app/channels/{acc['id']}/templates/save", data={"csrf": tok, "name": "Mal Nombre", "language": "es_MX",
                                                                       "category": "UTILITY", "body": "Hola"}, follow_redirects=True)
    assert "lowercase" in r.text
    r = client.post(f"/app/channels/{acc['id']}/templates/save", data={"csrf": tok, "name": "recordatorio", "language": "es_MX",
                                                                       "category": "UTILITY", "body": "Hola {{1}}", "example": "Ana"},
                    follow_redirects=True)
    assert "recordatorio" in r.text and "Borrador" in r.text
    assert client.get("/app/channels/00000000-0000-0000-0000-000000000000/templates").status_code == 404
    r = client.get(f"/app/inbox?c={conv}")
    assert '<datalist id="wa-approved">' in r.text and 'value="clase_gratis"' in r.text and 'id="notes"' in r.text
    tok = csrf(client, f"/app/inbox?c={conv}")
    client.post(f"/app/inbox/{conv}/note", data={"csrf": tok, "note": "Llamarla el lunes"})
    assert "Llamarla el lunes" in client.get(f"/app/inbox?c={conv}").text
    from valeria_one.services import users as U
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email="coach2@example.com", name="Coach Leo", password_hash="x").returning(db.users.c.id)).scalar()
        U.add_member(conn, biz["id"], uid, "staff")
    r = client.get(f"/app/inbox?c={conv}")
    assert 'name="user_id"' in r.text and "who=mine" in r.text and "who=none" in r.text
    client.post(f"/app/inbox/{conv}/assign", data={"csrf": tok, "user_id": "me"})
    with db.tx() as conn:
        assert conn.execute(select(db.conversations.c.assignee_id).where(db.conversations.c.id == conv)).scalar() == me
    assert str(conv) in client.get("/app/inbox?status=open&who=mine").text
    assert f"/app/inbox?c={conv}" not in client.get("/app/inbox?status=open&who=none").text
    r = client.get("/app/help/whatsapp-badge")
    assert r.status_code == 200 and "business.facebook.com/wa/manage" in r.text
    r = client.get("/app/valeria")
    assert 'action="/app/valeria/away"' in r.text
    client.post("/app/valeria/away", data={"csrf": csrf(client, "/app/valeria"), "on": "1", "away_text": "Cerrado, volvemos {next}"})
    with db.tx() as conn:
        s = OW.settings(conn, biz["id"])
    assert s["away"] is True and s["away_text"] == "Cerrado, volvemos {next}"


def test_sandbox_template_is_approved_at_once(client):
    biz, _ = make_business("MX", "fitness", sandbox=True)
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="sandbox", name="Sandbox")
        t = WT.save(conn, biz, "t", account_id=acc["id"], name="demo", language="es_MX", category="UTILITY", body="Hola")
        assert WT.accounts(conn, biz["id"])[0]["ready"]
    assert WT.submit(biz, "t", t["id"])["status"] == "APPROVED"
    assert WT.sync(biz, acc["id"]) == {"updated": 0, "added": 0}


def test_the_migration_never_loses_the_business_words():
    path = next((ROOT / "alembic" / "versions").glob("*_wati01_*.py"))
    src = path.read_text()
    assert "revision: str = 'd7a3c9e2f4b1'" in src and "down_revision: Union[str, Sequence[str], None] = 'c4d8e1f6a2b7'" in src
    assert "raise RuntimeError" in src and "wa_templates" in src and "conversation_notes" in src


# ---------------------------------------------------------------- helpers
def _body(who, text, pid="1"):
    return json.dumps({"from": who, "body": text, "name": None, "media": [], "id": f"in-{who}-{pid}-{text}"}).encode()


def _sig(body):
    from valeria_one.crypto import hmac_sha256_hex
    return hmac_sha256_hex("s3", body)
