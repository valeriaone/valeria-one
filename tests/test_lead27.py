"""lead27 (v0.27): lead follow-up never stops — every source tracked, every lead worked.

Sources: each place people find a business gets a code — the short link /l/<code> (counted, remembered, claimed by the new
lead), the chat link's "(ref <code>)" (an incoming WhatsApp message is that source's), the website snippet (one <script>:
consent boxes, honeypot, time check, rate limit, CORS), the forwarding address (signed, dormant until the operator turns it
on), the QR. Only people who reach out FIRST become leads. Source -> result per source. Suggestions (answer, follow-up day N,
check in) with the message written; one tap sends from the business's own account or opens the owner's own app. Suggest mode
never sends by itself; Autopilot sends inside the rules (24 h window -> the business's approved template, else a tap; quiet
hours; one a day; a daily cap). It stops at once on a reply, an opt-out word (any language), booked, won, lost, a pause,
erasure; nurture only with offers consent. A dry run writes and sends nothing. Voice: the owner's recording (written down with
their own key, sent without one), Valeria's own stock voice with her introduction — never a clone. WhatsApp, the AI, the
speech services and email are all mocked: nothing leaves the machine."""
import base64
import importlib.util
import json
import re
import shutil
import subprocess
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select, text, update

from _va import ALL_WEEK, TG_SECRET, Net, csrf, owner_session, raw_acc, refuse_network
from conftest import make_business
from valeria_one import db, kits, packs
from valeria_one.channels import HookRequest
from valeria_one.crypto import hmac_sha256_hex
from valeria_one.services import NotFound, Invalid
from valeria_one.services import ai as AI
from valeria_one.services import autoreply
from valeria_one.services import booking as BK
from valeria_one.services import businesses as B
from valeria_one.services import contacts as C
from valeria_one.services import deals as D
from valeria_one.services import export as EX
from valeria_one.services import inbox as X
from valeria_one.services import leads as LD
from valeria_one.services import leadvoice as LV
from valeria_one.services import payments as PAY

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
WA_CFG = {"phone_number_id": "PNID", "token": "wa-token", "app_secret": "APPSECRET", "verify_token": "VT"}
NOW = lambda: datetime.now(timezone.utc)                     # noqa: E731


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


@pytest.fixture(autouse=True)
def clean_limits():
    from valeria_one.web import leads as WL
    WL._lead_hits.clear()
    WL._src_hits.clear()
    yield


class Wire(Net):
    """Net (Telegram, the gateway, Gemini) + the business's own WhatsApp Cloud API + OpenAI / Gemini speech."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.wa, self.speech = [], []

    def __call__(self, req: httpx.Request):
        url = str(req.url)
        if "graph.facebook.com" in url and url.endswith("/messages"):
            assert req.headers["authorization"] == "Bearer wa-token"
            self.wa.append(json.loads(req.content))
            return httpx.Response(200, json={"messages": [{"id": f"wamid.out{len(self.wa)}"}]})
        if "api.openai.com/v1/audio/speech" in url:
            self.speech.append(json.loads(req.content))
            return httpx.Response(200, content=b"OggS-valeria-voice")
        if "generativelanguage.googleapis.com" in url and "-tts:" in url:
            self.speech.append(json.loads(req.content))
            pcm = b"\x00\x01" * 2400
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
                "mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})
        return super().__call__(req)

    def texts(self):
        return [m.get("text", {}).get("body") for m in self.wa if m.get("type") == "text"]


def setup(country="MX", booking=True, name="Estudio Luz", industry="salon"):
    biz, _ = make_business(country, industry, name=name)
    with db.tx() as conn:
        wa = X.connect(conn, biz, "t", channel="whatsapp", name="WhatsApp", cfg=WA_CFG)
        if booking:
            biz = BK.save_settings(conn, biz, "t", enabled=True, slug=f"luz-{str(biz['id'])[:6]}", hours=ALL_WEEK,
                                   notice_minutes=0, days_ahead=30)
            BK.add_service(conn, biz, "t", name="Consulta", minutes=60, price_minor=50000)
        biz = B.get(conn, biz["id"])
    return biz, wa


_n = [0]


def wa_in(acc, frm="5219991234567", body="Hola", name="Ana", net=None, at=None):
    """Someone writes to the business's own WhatsApp (signed like Meta does); what happens next runs in line."""
    _n[0] += 1
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PNID"}, "contacts": [{"wa_id": frm, "profile": {"name": name}}],
        "messages": [{"from": frm, "id": f"wamid.in{_n[0]}", "timestamp": str(int((at or NOW()).timestamp())),
                      "type": "text", "text": {"body": body}}]}}]}]}
    raw = json.dumps(payload).encode()
    st = X.receive(raw_acc(acc["id"]), HookRequest("POST", "", {"x-hub-signature-256": "sha256=" + hmac_sha256_hex("APPSECRET", raw)},
                                                   {}, raw))
    h = (net or Wire()).http
    for cid in st.get("conversation_ids", []):
        autoreply.respond(acc["business_id"], cid, http=h)
    return st


def lead_by_phone(biz, phone="+529991234567"):
    with db.tx() as conn:
        cid = conn.execute(select(db.contacts.c.id).where(db.contacts.c.business_id == biz["id"], db.contacts.c.phone == phone)).scalar()
        return LD.lead_of(conn, biz["id"], cid) if cid else None


def source(biz, kind="website", name="Mi sitio", dest=""):
    with db.tx() as conn:
        return LD.save_source(conn, biz, "t", {"kind": kind, "name": name, "dest": dest})


def settings(biz, **kw):
    with db.tx() as conn:
        return LD.save_settings(conn, biz, "t", **kw)


def local_noon(biz, days=0):
    tz = ZoneInfo(biz["timezone"])
    d = datetime.now(tz).date() + timedelta(days=days)
    return datetime.combine(d, time(12), tz).astimezone(timezone.utc)


def age(lead, created_at, last_inbound=None):
    """Move a lead (and its chat) back in time, so the plan's days can be tried without waiting."""
    with db.tx() as conn:
        conn.execute(update(db.leads).where(db.leads.c.id == lead["id"]).values(created_at=created_at, next_at=created_at + timedelta(minutes=5)))
        conn.execute(update(db.conversations).where(db.conversations.c.contact_id == lead["contact_id"])
                     .values(last_inbound_at=last_inbound or created_at))
        convs = select(db.conversations.c.id).where(db.conversations.c.contact_id == lead["contact_id"])
        conn.execute(update(db.messages).where(db.messages.c.conversation_id.in_(convs), db.messages.c.direction == "in")
                     .values(created_at=created_at))
        conn.execute(update(db.messages).where(db.messages.c.conversation_id.in_(convs), db.messages.c.direction == "out")
                     .values(created_at=created_at + timedelta(minutes=1)))
        return LD.lead_of(conn, lead["business_id"], lead["contact_id"])


def get_lead(lead):
    with db.tx() as conn:
        return LD.lead_of(conn, lead["business_id"], lead["contact_id"])


def touches(lead):
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.lead_touches).where(db.lead_touches.c.lead_id == lead["id"])
                                              .order_by(db.lead_touches.c.created_at)).mappings()]


# ---------------------------------------------------------------- A. sources
def test_someone_who_writes_through_a_chat_link_is_a_lead_of_that_source():
    biz, wa = setup()
    settings(biz, chat_number="+52 999 765 4321")
    s = source(biz, "facebook", "Página de Facebook")
    with db.tx() as conn:
        st = LD.settings(conn, biz["id"])
    links = LD.chat_links(biz, st, s, "es")
    assert links["whatsapp"].startswith("https://wa.me/529997654321?text=") and f"%28ref%20{s['code']}%29" in links["whatsapp"]
    wa_in(wa, body=f"Hola, quiero información (ref {s['code']})")
    lead = lead_by_phone(biz)
    assert lead and lead["source_id"] == s["id"] and lead["consent"] == "wrote_first" and lead["origin"] == "whatsapp"
    assert lead["status"] == "new" and lead["step"] == 0
    assert timedelta(minutes=4) < lead["next_at"] - lead["created_at"] <= timedelta(minutes=6)        # day 0: a few minutes' grace
    wa_in(wa, frm="5219991112222", body="hola (ref zzzzzzzz)", name="Beto")                          # a code that isn't theirs
    assert lead_by_phone(biz, "+529991112222")["source_id"] is None
    other, wa2 = setup(name="Otro")
    wa_in(wa2, frm="5219993334444", body=f"hola (ref {s['code']})")                                   # another business's code
    assert lead_by_phone(other, "+529993334444")["source_id"] is None


def test_messenger_ref_names_the_source():
    biz, _ = setup()
    s = source(biz, "facebook", "Página")
    with db.tx() as conn:
        contact, _ = C.find_or_create_by_identity(conn, biz, "messenger", "PSID9", "Lu", "messenger")
        LD.on_inbound(conn, biz, contact, "hola", {"ref": s["code"], "source": "SHORTLINK"})
        assert LD.lead_of(conn, biz["id"], contact["id"])["source_id"] == s["id"]


def test_people_the_business_typed_in_imported_or_made_are_never_leads(client):
    biz, key = make_business("MX", "salon")
    with db.tx() as conn:
        for actor in ("user:1", "import", "csv:file", "api_key:vo_live_x", "valeria", "recipe:x", "workflow:y", "seed"):
            c = C.create(conn, biz, actor, {"name": f"N {actor}", "phone": None, "email": f"{abs(hash(actor))}@ex.com"})
            assert LD.lead_of(conn, biz["id"], c["id"]) is None, actor
        c = C.create(conn, biz, "website", {"name": "Web", "email": "web@ex.com"})
        assert LD.lead_of(conn, biz["id"], c["id"])["consent"] == "form"
        c = C.create(conn, biz, "webhook:meta", {"name": "Lead ad", "email": "ad@ex.com"})
        assert LD.lead_of(conn, biz["id"], c["id"])["consent"] == "form"
    r = client.post("/v1/contacts", headers={"Authorization": f"Bearer {key}"}, json={"name": "Por API", "email": "api@ex.com"})
    assert r.status_code == 201
    with db.tx() as conn:
        assert LD.lead_of(conn, biz["id"], r.json()["id"]) is None


def test_the_short_link_counts_remembers_and_the_new_lead_gets_its_source(client):
    from valeria_one.services import forms as FM
    biz, _ = setup()
    s = source(biz, "flyer", "Volante del mercado")
    r = client.get(f"/l/{s['code']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"https://one.test/b/{biz['slug']}"   # booking page: nowhere else
    with db.tx() as conn:
        assert LD.source(conn, biz["id"], s["id"])["clicks"] == 1
        fm = FM.save(conn, biz, "t", {"name": "Contacto", "fields": [{"key": "name"}, {"key": "phone"}, {"key": "message"}]})
    page = client.get(f"/form/{fm['slug']}").text
    tok = re.search(r'name="token" value="([^"]+)"', page).group(1)
    r = client.post(f"/form/{fm['slug']}", data={"token": tok, "name": "Luisa", "phone": "9991230000", "message": "Hola",
                                                 "consent_request": "1"}, follow_redirects=False)
    assert r.status_code == 303
    lead = lead_by_phone(biz, "+529991230000")
    assert lead["source_id"] == s["id"] and lead["consent"] == "form"
    s2 = source(biz, "website", "Mi web", dest="https://example.com/inicio")
    assert client.get(f"/l/{s2['code']}", follow_redirects=False).headers["location"] == "https://example.com/inicio"
    with db.tx() as conn:                                        # no booking, no website: the source's own small page
        biz2, _ = make_business("PH", "generic", name="Walang Site")
        s3 = LD.save_source(conn, biz2, "t", {"kind": "sign", "name": "Karatula"})
    r = client.get(f"/l/{s3['code']}", follow_redirects=False)
    assert r.headers["location"] == f"https://one.test/l/{s3['code']}/hi"
    hi = client.get(f"/l/{s3['code']}/hi")
    assert hi.status_code == 200 and f"/l/{s3['code']}/embed.js" in hi.text and "Walang Site" in hi.text
    assert hi.headers["x-robots-tag"] == "noindex"


def test_unknown_switched_off_or_paused_codes_are_404(client):
    biz, _ = setup()
    s = source(biz)
    assert client.get("/l/zzzzzzzz", follow_redirects=False).status_code == 404
    assert client.get("/l/<script>", follow_redirects=False).status_code == 404
    with db.tx() as conn:
        LD.set_active(conn, biz, "t", s["id"], False)
    assert client.get(f"/l/{s['code']}", follow_redirects=False).status_code == 404
    assert client.get(f"/l/{s['code']}/embed.js").status_code == 404
    with db.tx() as conn:
        LD.set_active(conn, biz, "t", s["id"], True)
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(suspended_at=func.now()))
    assert client.get(f"/l/{s['code']}", follow_redirects=False).status_code == 404


def test_claim_only_for_a_new_customer_of_that_business():
    biz, _ = setup()
    s = source(biz)
    ref = LD.remember(s)
    with db.tx() as conn:
        old = C.create(conn, biz, "website", {"name": "Vieja", "email": "vieja@ex.com"})
        conn.execute(update(db.contacts).where(db.contacts.c.id == old["id"]).values(created_at=NOW() - timedelta(days=3)))
        old = C.get(conn, biz["id"], old["id"])
        assert LD.claim(conn, biz, ref, old) is False                         # came before the click
        assert LD.claim(conn, biz, {**ref, "at": (NOW() - timedelta(days=31)).isoformat()}, old) is False
        other, _ = make_business("MX", "salon", name="Otra")
        c = C.create(conn, other, "website", {"name": "Nueva", "email": "nueva@ex.com"})
        assert LD.claim(conn, other, ref, c) is False                         # another business's link
        c = C.create(conn, biz, "website", {"name": "Nueva", "email": "nueva@ex.com"})
        assert LD.claim(conn, biz, ref, c) is True
        assert LD.claim(conn, biz, LD.remember(source(biz, name="Otra fuente")), c) is False   # never over another source


def test_the_website_snippet_is_one_script_with_consent_and_privacy(client):
    biz, _ = setup()
    settings(biz, chat_number="+529997654321", messenger="estudioluz")
    s = source(biz, "website", "Mi web")
    r = client.get(f"/l/{s['code']}/embed.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
    assert r.headers["access-control-allow-origin"] == "*" and r.headers["x-content-type-options"] == "nosniff"
    js = r.text
    assert f"https://one.test/l/{s['code']}/lead" in js and f"/privacy/{biz['slug']}" in js and '"LFPDPPP"' in js
    assert "attachShadow" in js and "name=\"hp\"" in js and f"ref%20{s['code']}" in js and "m.me/estudioluz?ref=" + s["code"] in js
    assert "</script" not in js.lower()
    if shutil.which("node"):
        p = ROOT / "tests" / f".lead27_embed_{s['code']}.js"
        p.write_text(js)
        try:
            assert subprocess.run(["node", "--check", str(p)]).returncode == 0
        finally:
            p.unlink()
    assert LD.embed(s) == f'<script src="https://one.test/l/{s["code"]}/embed.js" async></script>'


def _post_lead(client, code, **d):
    body = {"name": "Mara", "contact": "+52 999 222 3333", "message": "Info", "consent": True, "ms": 6000, **d}
    return client.post(f"/l/{code}/lead", content=json.dumps(body), headers={"Content-Type": "application/json",
                                                                              "Origin": "https://her-site.example"})


def test_the_snippet_takes_a_lead_only_with_consent_and_never_from_a_robot(client):
    biz, _ = setup()
    s = source(biz, "website", "Mi web")
    pre = client.options(f"/l/{s['code']}/lead")
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
    r = _post_lead(client, s["code"], consent=False)
    assert r.status_code == 400 and r.headers["access-control-allow-origin"] == "*"
    assert _post_lead(client, s["code"], hp="http://spam").json() == {"ok": True}       # honeypot: "ok", nothing kept
    assert _post_lead(client, s["code"], ms=300).json() == {"ok": True}                 # too fast for a person
    assert lead_by_phone(biz, "+529992223333") is None
    assert _post_lead(client, s["code"]).json() == {"ok": True}
    lead = lead_by_phone(biz, "+529992223333")
    assert lead["source_id"] == s["id"] and lead["consent"] == "widget"
    with db.tx() as conn:
        c = C.get(conn, biz["id"], lead["contact_id"])
        assert c["marketing_consent"] is False                                          # offers only with their own box
    assert _post_lead(client, s["code"], name="Rita", contact="rita@ex.com", offers=True).status_code == 200
    with db.tx() as conn:
        rita = conn.execute(select(db.contacts).where(db.contacts.c.email == "rita@ex.com")).mappings().one()
    assert rita["marketing_consent"] and s["code"] in rita["marketing_consent_source"] and "her-site.example" in rita["marketing_consent_source"]
    assert _post_lead(client, s["code"], contact="").status_code == 400
    codes = [_post_lead(client, s["code"], name=f"P{i}", contact=f"p{i}@ex.com").status_code for i in range(6)]
    assert 429 in codes                                                                 # a per-visitor limit
    assert client.post("/l/zzzzzzzz/lead", content=b"{}").status_code in (404, 429)


def test_the_forwarding_address_is_off_until_the_operator_turns_it_on(client, monkeypatch):
    biz, _ = setup()
    s = source(biz, "email", "Portal de anuncios")
    assert LD.mail_address(s) is None and client.post("/lmail", content=b"{}").status_code == 404
    monkeypatch.setenv("VO_LEADS_MAIL_DOMAIN", "leads.one.test")
    monkeypatch.setenv("VO_LEADS_MAIL_SECRET", "mail-secret")
    assert LD.mail_address(s) == f"{s['code']}@leads.one.test"
    mail = {"to": f"{s['code']}@leads.one.test", "from": "noreply@portal.example", "subject": "Nuevo interesado",
            "text": "Tienes un nuevo contacto.\nNombre: Julia Ramos\nTeléfono: 999 444 5555\nCorreo: julia@ex.com\nMensaje: ¿sigue disponible?",
            "message_id": "<m1@portal>"}
    raw = json.dumps(mail).encode()
    assert client.post("/lmail", content=raw, headers={"X-VO-Signature": "bad"}).status_code == 401
    sig = {"X-VO-Signature": hmac_sha256_hex("mail-secret", raw)}
    r = client.post("/lmail", content=raw, headers=sig)
    assert r.status_code == 200 and r.json()["ok"]
    lead = lead_by_phone(biz, "+529994445555")
    assert lead and lead["source_id"] == s["id"] and lead["consent"] == "mail"
    with db.tx() as conn:
        c = C.get(conn, biz["id"], lead["contact_id"])
    assert c["name"] == "Julia Ramos" and c["email"] == "julia@ex.com"
    assert client.post("/lmail", content=raw, headers=sig).json().get("duplicate")          # the same email twice: once
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.leads).where(db.leads.c.business_id == biz["id"])).scalar() == 1
    none = json.dumps({**mail, "text": "nada útil", "message_id": "<m2@portal>"}).encode()
    assert client.post("/lmail", content=none, headers={"X-VO-Signature": hmac_sha256_hex("mail-secret", none)}).json() == \
        {"ok": False, "why": "no_contact"}
    wrong = json.dumps({**mail, "to": "zzzzzzzz@leads.one.test"}).encode()
    assert client.post("/lmail", content=wrong, headers={"X-VO-Signature": hmac_sha256_hex("mail-secret", wrong)}).json()["why"] == \
        "unknown_address"


def test_qr_per_source_and_one_business_never_sees_another(client):
    biz, _ = setup()
    s = source(biz, "sign", "Letrero")
    owner_session(client, biz)
    r = client.get(f"/app/leads/sources/{s['id']}/qr.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and "<svg" in r.text
    assert "attachment" in client.get(f"/app/leads/sources/{s['id']}/qr.svg?download=1").headers["content-disposition"]
    other, _ = setup(name="Otra")
    so = source(other, "sign", "Ajeno")
    assert client.get(f"/app/leads/sources/{so['id']}/qr.svg").status_code == 404
    wa_lead_other = None
    with db.tx() as conn:
        c = C.create(conn, other, "website", {"name": "Ajena", "email": "ajena@ex.com"})
        wa_lead_other = LD.lead_of(conn, other["id"], c["id"])
    assert client.get(f"/app/leads/{wa_lead_other['id']}").status_code == 404
    tok = csrf(client, "/app/leads")
    assert client.post(f"/app/leads/{wa_lead_other['id']}/act", data={"csrf": tok, "act": "won"}).status_code == 404
    assert get_lead(wa_lead_other)["status"] == "new"


# ---------------------------------------------------------------- B. source -> result
def test_source_to_result_counts_leads_replies_bookings_and_money():
    biz, wa = setup()
    s1, s2 = source(biz, "facebook", "Facebook"), source(biz, "flyer", "Volante")
    with db.tx() as conn:
        conn.execute(update(db.lead_sources).where(db.lead_sources.c.id == s1["id"]).values(clicks=10))
    wa_in(wa, "5219990000001", f"hola (ref {s1['code']})", "Uno")
    wa_in(wa, "5219990000002", f"hola (ref {s1['code']})", "Dos")
    wa_in(wa, "5219990000003", f"hola (ref {s2['code']})", "Tres")
    wa_in(wa, "5219990000004", "hola", "Cuatro")                                        # straight to WhatsApp
    one, two = lead_by_phone(biz, "+529990000001"), lead_by_phone(biz, "+529990000002")
    net = Wire()
    assert LD.send(biz, "user:x", one["id"], body="¡Hola! ¿En qué te ayudamos?", do="answer", http=net.http)["status"] == "sent"
    wa_in(wa, "5219990000001", "Quiero una cita el viernes", "Uno")                      # they answered: replied
    with db.tx() as conn:
        D.book(conn, biz, "t", {"title": "Consulta", "contact_id": one["contact_id"], "starts_at": NOW() + timedelta(days=2),
                                "ends_at": NOW() + timedelta(days=2, hours=1), "allow_overlap": True})
    PAY.record_received(biz, "t", amount_minor=50000, description="Consulta", contact_id=two["contact_id"])
    with db.tx() as conn:
        rep = LD.report(conn, biz, 90, "es")
    rows = {r["label"]: r for r in rep["rows"]}
    fb = rows["Facebook"]
    assert (fb["clicks"], fb["leads"], fb["replied"], fb["booked"], fb["won"], fb["paid_minor"]) == (10, 2, 1, 1, 1, 50000)
    assert rows["Volante"]["leads"] == 1 and rows["WhatsApp"]["leads"] == 1
    assert rep["total"]["leads"] == 4 and rep["total"]["paid_minor"] == 50000
    assert get_lead(one)["status"] == "booked" and get_lead(two)["status"] == "won"      # booked / paid stop the plan
    s3 = source(biz, "ad", "Anuncio sin leads")
    with db.tx() as conn:
        assert {r["label"] for r in LD.report(conn, biz, 90)["rows"]} >= {"Anuncio sin leads"}


# ---------------------------------------------------------------- C. suggestions
def test_today_suggests_answer_followup_and_check_in_one_per_lead(client):
    biz, wa = setup()
    wa_in(wa, "5219990000011", "hola", "Ana")
    ana = lead_by_phone(biz, "+529990000011")
    later = NOW() + timedelta(minutes=10)
    with db.tx() as conn:
        got = LD.suggestions(conn, biz, later)
    assert [(g["id"], g["do"]) for g in got] == [(ana["id"], "answer")]                    # they wrote; nobody answered
    net = Wire()
    LD.send(biz, "user:x", ana["id"], body="¡Hola Ana!", do="answer", http=net.http)
    assert net.texts() == ["¡Hola Ana!"]
    with db.tx() as conn:
        got = LD.suggestions(conn, biz, NOW() + timedelta(days=1, hours=1))
    assert [(g["do"], g["day"]) for g in got if g["id"] == ana["id"]] == [("followup", 0)]   # day 0 still on (we answered by hand)
    wa_in(wa, "5219990000011", "Gracias, lo pienso", "Ana")                              # a reply: a talk now
    LD.send(biz, "user:x", ana["id"], body="Claro, aquí estamos", do="answer", http=net.http)
    with db.tx() as conn:
        assert get_lead(ana)["status"] == "talking"
        got = LD.suggestions(conn, biz, NOW() + timedelta(days=3))
    assert [g["do"] for g in got if g["id"] == ana["id"]] == ["checkin"]
    owner_session(client, biz)
    page = client.get("/app")
    assert page.status_code == 200
    r = client.get("/app/leads?tab=all")
    assert r.status_code == 200 and f"/app/leads/{ana['id']}" in r.text


def test_the_today_screen_shows_leads_waiting_with_one_tap(client):
    biz, wa = setup()
    wa_in(wa, "5219990000021", "hola", "Berta")
    b = lead_by_phone(biz, "+529990000021")
    with db.tx() as conn:
        conn.execute(update(db.messages).where(db.messages.c.business_id == biz["id"]).values(created_at=NOW() - timedelta(minutes=30)))
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": csrf(client, "/app")})
    page = client.get("/app").text
    assert 'id="leads"' in page and f"/app/leads/{b['id']}/send" in page and "Send (WhatsApp)" in page
    tok = csrf(client, "/app")
    net = Wire()
    import valeria_one.services.inbox as XX
    orig = XX.channels.adapter
    XX.channels.adapter = lambda ch, http=None: orig(ch, net.http)
    try:
        r = client.post(f"/app/leads/{b['id']}/send", data={"csrf": tok, "do": "answer", "body": "¡Hola Berta! ¿En qué te ayudamos?",
                                                            "back": "/app#leads"}, follow_redirects=False)
    finally:
        XX.channels.adapter = orig
    with db.tx() as conn:
        errs = [x.error for x in conn.execute(select(db.messages.c.error).where(db.messages.c.direction == "out"))]
    assert r.status_code == 303 and r.headers["location"] == "/app#leads", errs
    assert net.texts() == ["¡Hola Berta! ¿En qué te ayudamos?"]
    assert [t["how"] for t in touches(b)] == ["tap"]


# ---------------------------------------------------------------- F. never-stop follow-up
def test_suggest_mode_never_sends_by_itself():
    biz, wa = setup()
    wa_in(wa, "5219990000031", "hola", "Caro")
    lead = lead_by_phone(biz, "+529990000031")
    lead = age(lead, local_noon(biz) - timedelta(hours=1))
    got = LD.round_(now=local_noon(biz))                                                 # no http: a send would hit the refused network
    assert got["sent"] == 0 and got["suggest"] >= 1
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.messages).where(db.messages.c.business_id == biz["id"],
                                                                                db.messages.c.direction == "out")).scalar() == 0


def test_day_0_is_skipped_when_someone_already_answered_and_a_missed_day_moves_on():
    biz, wa = setup()
    wa_in(wa, "5219990000041", "hola", "Dani")
    lead = lead_by_phone(biz, "+529990000041")
    LD.send(biz, "user:x", lead["id"], body="Hola Dani", do="answer", http=Wire().http)
    lead = get_lead(lead)
    got = LD.round_(now=NOW() + timedelta(minutes=10))
    assert got["skipped"] == 1 and get_lead(lead)["step"] == 1
    assert touches(lead)[-1]["note"] == "answered"
    lead = get_lead(lead)
    with db.tx() as conn:                                                               # day 1 never tapped; day 3 came
        conn.execute(update(db.leads).where(db.leads.c.id == lead["id"]).values(created_at=NOW() - timedelta(days=3, hours=1),
                                                                                next_at=NOW() - timedelta(days=2)))
    LD.round_(now=NOW())
    assert get_lead(lead)["step"] == 2 and touches(lead)[-1]["note"] == "missed"


def test_autopilot_sends_inside_the_window_from_the_business_s_own_account():
    biz, wa = setup()
    settings(biz, mode="autopilot")
    wa_in(wa, "5219990000051", "hola", "Eli")
    lead = lead_by_phone(biz, "+529990000051")
    t0 = local_noon(biz, -1) + timedelta(hours=1)
    lead = age(lead, t0, last_inbound=t0)
    net = Wire()
    now = t0 + timedelta(hours=6)                                                       # day 0, inside 24 h, 19:00 local
    got = LD.round_(now=now, http=net.http)
    assert got["sent"] == 1 and len(net.wa) == 1 and net.wa[0]["to"] == "529990000051"
    assert "Eli" in net.wa[0]["text"]["body"] and "Estudio Luz" in net.wa[0]["text"]["body"]   # her words, in Spanish
    lead2 = get_lead(lead)
    assert lead2["step"] == 1 and lead2["touches"] == 1 and lead2["next_at"] >= now + timedelta(hours=20)
    assert [(t["how"], t["status"], t["by"]) for t in touches(lead)] == [("auto", "sent", "valeria")]
    assert LD.round_(now=now + timedelta(minutes=1), http=net.http)["sent"] == 0             # never twice
    with db.tx() as conn:
        m = conn.execute(select(db.messages).where(db.messages.c.business_id == biz["id"], db.messages.c.direction == "out")).mappings().one()
    assert m["kind"] == "service"


def test_after_24_hours_the_approved_template_else_one_tap_in_the_owners_whatsapp(client):
    biz, wa = setup()
    settings(biz, mode="autopilot")
    wa_in(wa, "5219990000061", "hola", "Fer")
    lead = lead_by_phone(biz, "+529990000061")
    LD.send(biz, "user:x", lead["id"], body="Hola Fer", do="answer", http=Wire().http)
    t0 = local_noon(biz, -2)
    lead = age(lead, t0)
    with db.tx() as conn:
        conn.execute(update(db.leads).where(db.leads.c.id == lead["id"]).values(step=1, next_at=t0 + timedelta(days=1)))
    net = Wire()
    now = local_noon(biz, 0) - timedelta(hours=1)
    got = LD.round_(now=now, http=net.http)
    assert got["blocked"] == 1 and net.wa == []
    lead = get_lead(lead)
    assert lead["blocked"] == "outside_reply_window" and lead["step"] == 1
    with db.tx() as conn:
        s = [g for g in LD.suggestions(conn, biz, now) if g["id"] == lead["id"]]
    assert s and s[0]["do"] == "followup"                                                   # waits for the owner's tap
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": csrf(client, "/app")})
    page = client.get("/app/leads").text
    assert f"/app/leads/{lead['id']}/open" in page and "Send by WhatsApp" in page
    tok = csrf(client, "/app/leads")
    r = client.post(f"/app/leads/{lead['id']}/open", data={"csrf": tok, "do": "followup", "via": "whatsapp", "body": "Hola Fer, ¿seguimos?"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://wa.me/529990000061?text=Hola%20Fer")
    assert get_lead(lead)["step"] == 2 and touches(lead)[-1]["how"] == "link"
    settings(biz, template="seguimiento_1")                                                 # with an approved template: she sends it
    with db.tx() as conn:
        conn.execute(update(db.leads).where(db.leads.c.id == lead["id"]).values(step=2, blocked=None, next_at=now, created_at=now - timedelta(days=3, hours=1)))
    got = LD.round_(now=now, http=net.http)
    assert got["sent"] == 1 and net.wa[-1]["type"] == "template" and net.wa[-1]["template"]["name"] == "seguimiento_1"


def test_quiet_hours_in_the_business_time_zone_move_the_send():
    biz, wa = setup()
    settings(biz, mode="autopilot")
    wa_in(wa, "5219990000071", "hola", "Gil")
    lead = lead_by_phone(biz, "+529990000071")
    tz = ZoneInfo(biz["timezone"])
    night = datetime.combine(datetime.now(tz).date(), time(23, 30), tz).astimezone(timezone.utc)
    lead = age(lead, night - timedelta(hours=1), last_inbound=night - timedelta(hours=1))
    net = Wire()
    got = LD.round_(now=night, http=net.http)
    assert got["later"] == 1 and net.wa == []
    nxt = get_lead(lead)["next_at"].astimezone(tz)
    assert nxt.hour == 8 and nxt > night


@pytest.mark.parametrize("word", ["STOP", "baja", "Arrêt", "停止", "tigil", "إلغاء"])
def test_an_opt_out_word_in_any_language_stops_it_at_once(word):
    biz, wa = setup()
    settings(biz, mode="autopilot")
    wa_in(wa, "5219990000081", "hola", "Hugo")
    lead = lead_by_phone(biz, "+529990000081")
    LD.send(biz, "user:x", lead["id"], body="Hola Hugo", do="answer", http=Wire().http)
    wa_in(wa, "5219990000081", word, "Hugo")
    lead = get_lead(lead)
    assert (lead["status"], lead["stop_reason"], lead["next_at"], lead["replied_at"]) == ("stopped", "optout", None, None)
    net = Wire()
    assert LD.round_(now=NOW() + timedelta(days=5), http=net.http)["sent"] == 0 and net.wa == []
    with db.tx() as conn:
        with pytest.raises(Invalid):
            LD.owner_action(conn, biz, "user:x", lead["id"], "restart")


def test_a_reply_won_lost_pause_and_erasure_each_stop_it():
    biz, wa = setup()
    leads = {}
    for n, who in enumerate(("reply", "won", "lost", "pause", "erase", "allpause")):
        frm = f"52199900001{n}0"
        wa_in(wa, frm, "hola", who)
        leads[who] = lead_by_phone(biz, "+52" + frm[3:])
        LD.send(biz, "user:x", leads[who]["id"], body="Hola", do="answer", http=Wire().http)
    wa_in(wa, "5219990000100", "Sí me interesa", "reply")
    with db.tx() as conn:
        d = D.create(conn, biz, "t", {"title": "x", "contact_id": leads["won"]["contact_id"], "stage": "new"})
        D.patch(conn, biz, "t", d["id"], {"stage": "won"})
        d = D.create(conn, biz, "t", {"title": "y", "contact_id": leads["lost"]["contact_id"], "stage": "new"})
        D.patch(conn, biz, "t", d["id"], {"stage": "lost"})
        LD.owner_action(conn, biz, "user:x", leads["pause"]["id"], "pause")
        conn.execute(update(db.lead_voice.insert().table).values()) if False else None
        LV.save(conn, biz, "t", leads["erase"]["contact_id"], b"OggS-x", "audio/ogg")
        C.delete(conn, biz["id"], "user:x", leads["erase"]["contact_id"])
    st = {k: get_lead(v) for k, v in leads.items()}
    assert (st["reply"]["status"], st["reply"]["stop_reason"]) == ("talking", "replied") and st["reply"]["replied_at"]
    assert (st["won"]["status"], st["lost"]["status"]) == ("won", "lost")
    assert st["pause"]["paused"] and (st["erase"]["status"], st["erase"]["stop_reason"]) == ("stopped", "erased")
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.lead_voice).where(
            db.lead_voice.c.contact_id == leads["erase"]["contact_id"])).scalar() == 0
    settings(biz, mode="autopilot", paused=True)
    net = Wire()
    assert LD.round_(now=NOW() + timedelta(days=2), http=net.http)["sent"] == 0 and net.wa == []
    with db.tx() as conn:
        LD.owner_action(conn, biz, "user:x", leads["lost"]["id"], "restart")             # the owner may try again
    assert get_lead(leads["lost"])["status"] == "new"


def test_nurture_only_for_people_who_said_yes_to_offers():
    biz, wa = setup()
    settings(biz, mode="autopilot")
    for frm, name in (("5219990000201", "Sin"), ("5219990000202", "Con")):
        wa_in(wa, frm, "hola", name)
    sin, con = lead_by_phone(biz, "+529990000201"), lead_by_phone(biz, "+529990000202")
    with db.tx() as conn:
        C.patch(conn, biz, "user:x", con["contact_id"], {"marketing_consent": True, "marketing_consent_source": "dijo que sí"})
        for ld in (sin, con):
            conn.execute(update(db.leads).where(db.leads.c.id == ld["id"]).values(step=5))
        c_sin = C.get(conn, biz["id"], sin["contact_id"])
        c_con = C.get(conn, biz["id"], con["contact_id"])
        st = LD.settings(conn, biz["id"])
    now = NOW()
    assert LD._next_values(get_lead(sin), st, c_sin, now, sent=True)["status"] == "quiet"
    nv = LD._next_values(get_lead(con), st, c_con, now, sent=True)
    assert nv["step"] == 6 and nv["next_at"] >= now + timedelta(days=29)
    with db.tx() as conn:                                                               # a nurture step due without consent: the plan ends
        conn.execute(update(db.leads).where(db.leads.c.id == sin["id"]).values(step=6, next_at=now))
    got = LD.round_(now=now + timedelta(seconds=1), http=Wire().http)
    assert got["ended"] >= 1 and get_lead(sin)["status"] == "quiet"


def test_the_daily_cap_leaves_the_rest_for_a_tap():
    biz, wa = setup()
    settings(biz, mode="autopilot", daily_cap=1)
    t0 = local_noon(biz) - timedelta(hours=3)
    for i in range(3):
        wa_in(wa, f"52199900003{i}0", "hola", f"N{i}")
        age(lead_by_phone(biz, f"+5299900003{i}0"), t0, last_inbound=t0)
    net = Wire()
    got = LD.round_(now=local_noon(biz), http=net.http)
    assert got["sent"] == 1 and got["suggest"] == 2 and len(net.wa) == 1


def test_a_dry_run_writes_nothing_and_sends_nothing(monkeypatch):
    biz, wa = setup()
    settings(biz, mode="autopilot")
    for i in range(2):
        wa_in(wa, f"52199900004{i}0", "¿Cuánto cuesta?", f"D{i}")
        age(lead_by_phone(biz, f"+5299900004{i}0"), local_noon(biz) - timedelta(hours=2))
    tables = ("leads", "lead_touches", "messages", "events", "assistant_actions", "lead_voice", "scheduled_messages")

    def snap():
        with db.tx() as conn:
            return {t: conn.execute(text(f"SELECT count(*), md5(coalesce(string_agg(x::text, ',' ORDER BY x::text), '')) FROM {t} x")).one()
                    for t in tables}
    before = snap()
    got = LD.round_(now=local_noon(biz), dry_run=True)                                     # no http: any send would be refused
    assert got["dry_run"] and [p["do"] for p in got["plan"] if p["do"] != "tell"] == ["sent", "sent"]
    assert sum(1 for p in got["plan"] if p["do"] == "tell") == 2
    assert snap() == before
    monkeypatch.setenv("VO_LEADS_DRY_RUN", "1")
    assert LD.round_(now=local_noon(biz))["dry_run"] and snap() == before
    from valeria_one import jobs
    monkeypatch.setattr(LD, "round_", LD.round_)
    assert jobs.main(["jobs", "leads", "--dry-run"]) == 0 and snap() == before


def test_a_hot_lead_is_told_to_the_owner_once_with_a_summary():
    biz, wa = setup()
    s = source(biz, "instagram", "Instagram")
    wa_in(wa, "5219990000501", f"¿Cuánto cuesta la consulta? (ref {s['code']})", "Iris")
    lead = lead_by_phone(biz, "+529990000501")
    assert get_lead(lead)["hot_at"]
    assert LD.round_(http=Wire().http)["told"] == 1
    with db.tx() as conn:
        a = conn.execute(select(db.assistant_actions).where(db.assistant_actions.c.business_id == biz["id"],
                                                            db.assistant_actions.c.dedupe == f"lead:hot:{lead['id']}")).mappings().one()
    assert a["kind"] == "notice" and "Iris" in a["summary"] and "Instagram" in a["summary"] and "Cuánto cuesta" in a["summary"]
    assert f"/app/leads/{lead['id']}" in a["summary"]
    LD.round_(http=Wire().http)
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.assistant_actions).where(
            db.assistant_actions.c.dedupe == f"lead:hot:{lead['id']}")).scalar() == 1


# ---------------------------------------------------------------- E. talk 24/7, D. drafts
def test_the_business_ai_knows_a_lead_and_qualifies_then_hands_over():
    biz, wa = setup()
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="AIza-own", mode="auto")
    seen = []

    def ai(prompt, j):
        seen.append(j["systemInstruction"]["parts"][0]["text"])
        return "¡Hola! ¿Para qué día lo necesitas?"
    net = Wire(ai_text=ai)
    wa_in(wa, "5219990000601", "hola, info por favor", "Juana", net=net)
    assert seen and "LEAD: this person contacted the business first" in seen[-1] and "[[HUMAN]]" in seen[-1]
    assert "booking link" in seen[-1]
    lead = lead_by_phone(biz, "+529990000601")
    with db.tx() as conn:
        conn.execute(update(db.leads).where(db.leads.c.id == lead["id"]).values(status="won"))
        conv = conn.execute(select(db.conversations.c.id).where(db.conversations.c.contact_id == lead["contact_id"])).scalar()
        assert LD.ai_facts(conn, biz, conv) == ""                                     # a customer already won: no lead talk


def test_a_draft_from_the_business_own_ai_else_plain_words_in_their_language():
    biz, wa = setup()
    wa_in(wa, "5219990000701", "hola, ¿tienen lugar?", "Karla")
    lead = lead_by_phone(biz, "+529990000701")
    plain = LD.draft(biz, lead["id"], "followup")
    assert not plain["by_ai"] and plain["lang"] == "es" and "Karla" in plain["text"] and "Estudio Luz" in plain["text"]
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="AIza-own", mode="suggest")
    net = Wire(ai_text="Hola Karla, ¿te aparto un lugar mañana?")
    got = LD.draft(biz, lead["id"], "followup", http=net.http)
    assert got["by_ai"] and got["text"] == "Hola Karla, ¿te aparto un lugar mañana?"
    assert "Use ONLY the facts below" in net.ai_calls[-1]["systemInstruction"]["parts"][0]["text"]
    broken = Wire(ai_text=None)                                                          # busy AI: her plain words, never nothing
    assert LD.draft(biz, lead["id"], "followup", http=broken.http)["by_ai"] is False
    with db.tx() as conn:
        cards = LD.cards(conn, biz, "es")
    assert any(c["kind"] == "service" and "Consulta" in c["text"] and f"/b/{biz['slug']}/" in c["text"] for c in cards)
    assert any(c["kind"] == "booking" for c in cards)


def test_no_account_means_the_owners_own_apps_with_the_message_filled_in(client):
    biz, _ = make_business("PH", "generic", name="Kape ni Lito")
    with db.tx() as conn:
        c = C.create(conn, biz, "website", {"name": "Lito", "phone": "+639171234567", "email": "lito@ex.com"})
        lead = LD.lead_of(conn, biz["id"], c["id"])
    res = LD.send(biz, "user:x", lead["id"], body="Hi Lito! Salamat")
    assert res["status"] == "blocked" and res["why"] == "no_channel"
    kinds = {x["kind"]: x["url"] for x in res["links"]}
    assert kinds["whatsapp"].startswith("https://wa.me/639171234567?text=Hi%20Lito") and kinds["sms"].startswith("sms:+639171234567?body=")
    assert kinds["email"].startswith("mailto:lito@ex.com?subject=")
    owner_session(client, biz)
    tok = csrf(client, f"/app/leads/{lead['id']}")
    r = client.post(f"/app/leads/{lead['id']}/send", data={"csrf": tok, "body": "Hi Lito!", "do": "followup"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/app/leads/{lead['id']}").text
    assert "No account connected that reaches them." in page and "Send by SMS" in page


# ---------------------------------------------------------------- D. voice
OGG = b"OggS\x00\x02" + b"\x00" * 200


def test_the_owner_speaks_her_voice_is_kept_written_down_and_sent(client):
    biz, wa = setup()
    wa_in(wa, "5219990000801", "hola", "Lola")
    lead = lead_by_phone(biz, "+529990000801")
    owner_session(client, biz)
    tok = csrf(client, f"/app/leads/{lead['id']}")
    r = client.post(f"/app/leads/{lead['id']}/voice", data={"csrf": tok}, files={"audio": ("voice.ogg", OGG, "audio/ogg")})
    j = r.json()
    assert r.status_code == 200 and j["why"] == "no_key" and j["transcript"] is None and j["url"].startswith("https://one.test/lv/")
    got = client.get(j["url"].replace("https://one.test", ""))
    assert got.status_code == 200 and got.content == OGG and got.headers["content-type"] == "audio/ogg"
    assert client.get("/lv/nope.ogg").status_code == 404
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="AIza-own", mode="suggest")
    import valeria_one.services.ai as AIM
    net = Wire(audio_words="Hola Lola, te espero el jueves")
    orig = AIM._http
    AIM._http = lambda http=None: net.http
    try:
        j2 = client.post(f"/app/leads/{lead['id']}/voice", data={"csrf": tok}, files={"audio": ("v.ogg", OGG, "audio/ogg")}).json()
    finally:
        AIM._http = orig
    assert j2["transcript"] == "Hola Lola, te espero el jueves" and j2["why"] is None
    assert client.post(f"/app/leads/{lead['id']}/voice", data={"csrf": "bad"}, files={"audio": ("v.ogg", OGG, "audio/ogg")}).status_code == 403
    bad = client.post(f"/app/leads/{lead['id']}/voice", data={"csrf": tok}, files={"audio": ("v.exe", b"MZ", "application/x-msdownload")})
    assert bad.status_code == 400
    net2 = Wire()
    res = LD.send(biz, "user:x", lead["id"], body="Te mando un audio", do="answer", voice_id=j2["id"], http=net2.http)
    assert res["status"] == "sent" and [m["type"] for m in net2.wa] == ["text", "audio"]       # words first, then the note
    assert net2.wa[1]["audio"]["link"] == j2["url"]
    assert touches(lead)[-1]["how"] == "voice"
    other, _ = setup(name="Otra")
    with db.tx() as conn:
        c = C.create(conn, other, "website", {"name": "X", "email": "x@ex.com"})
        with pytest.raises(NotFound):
            LV.media(conn, other["id"], j2["id"], c["id"])                               # never another business's note


def test_valeria_speaks_in_her_own_stock_voice_and_says_who_she_is():
    biz, wa = setup()
    wa_in(wa, "5219990000901", "hola", "Mila")
    lead = lead_by_phone(biz, "+529990000901")
    with db.tx() as conn:
        contact = C.get(conn, biz["id"], lead["contact_id"])
        AI.save(conn, biz, "t", provider="openai", api_key="sk-own", mode="suggest")
    net = Wire()
    m = LV.valeria_note(biz, contact, "Te esperamos mañana https://one.test/b/x", "es", http=net.http)
    req = net.speech[-1]
    assert req["voice"] == "nova" and req["model"] == "gpt-4o-mini-tts" and req["response_format"] == "opus"
    assert req["input"].startswith("Hola Mila, soy Valeria") or "Valeria" in req["input"].split(".")[0]
    assert "https://" not in req["input"] and len(req["input"]) <= LV.MAX_SCRIPT
    assert m["mime"] == "audio/ogg" and m["url"].startswith("https://one.test/lv/")
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="AIza-own")
    m2 = LV.valeria_note(biz, contact, "Hola", "es", http=net.http)
    voice = net.speech[-1]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
    assert voice == "Kore" and m2["mime"] == "audio/wav"
    raw, mime = LV.serve(m2["url"].rsplit("/", 1)[1])
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE" and mime == "audio/wav"
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="anthropic", api_key="sk-ant-own")
    with pytest.raises(AI.AIError):
        LV.valeria_note(biz, contact, "Hola", "es", http=net.http)
    import inspect
    assert "sample" not in inspect.signature(LV.speak).parameters and set(LV.STOCK.values()) == {"nova", "Kore"}


def test_autopilot_voice_note_on_day_1_goes_after_the_words():
    biz, wa = setup()
    settings(biz, mode="autopilot", voice=True)
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="openai", api_key="sk-own", mode="suggest")
    wa_in(wa, "5219990000951", "hola", "Nora")
    lead = lead_by_phone(biz, "+529990000951")
    t0 = local_noon(biz, -1) - timedelta(hours=1)
    lead = age(lead, t0, last_inbound=local_noon(biz) - timedelta(hours=2))
    with db.tx() as conn:
        conn.execute(update(db.leads).where(db.leads.c.id == lead["id"]).values(step=1, next_at=t0 + timedelta(days=1)))
    net = Wire(ai_text="Hola Nora, ¿te ayudamos?")

    class Chat:                                                       # the business's OpenAI for the words, too
        pass
    import valeria_one.services.ai as AIM
    calls = []
    orig = AIM._call
    AIM._call = lambda *a, **k: calls.append(a) or "Hola Nora, ¿te ayudamos a decidir?"
    try:
        got = LD.round_(now=local_noon(biz), http=net.http)
    finally:
        AIM._call = orig
    assert got["sent"] == 1 and [m["type"] for m in net.wa] == ["text", "audio"]
    assert net.speech and "Valeria" in net.speech[-1]["input"]
    assert touches(lead)[-1]["how"] == "voice"


# ---------------------------------------------------------------- the screens, the API, the words
def test_every_tab_and_the_lead_page_render_in_all_11_languages_on_a_phone(client):
    biz, wa = setup()
    s = source(biz, "website", "Mi web")
    wa_in(wa, "5219990001001", f"hola (ref {s['code']})", "Olga")
    lead = lead_by_phone(biz, "+529990001001")
    owner_session(client, biz)
    phone = {"User-Agent": "Mozilla/5.0 (iPhone)"}
    for lang in LANGS:
        client.post(f"/lang/{lang}", data={"csrf": csrf(client, "/app")})
        for path in ("/app/leads?tab=today", "/app/leads?tab=all", "/app/leads?tab=sources", "/app/leads?tab=results&days=30",
                     "/app/leads?tab=settings", f"/app/leads/{lead['id']}", "/app"):
            r = client.get(path, headers=phone)
            assert r.status_code == 200, (lang, path, r.text[:300])
            main = r.text.split("<main", 1)[1]
            assert not re.search(r"\{\w+\}", main), (lang, path, re.findall(r".{30}\{\w+\}.{10}", main)[:2])
            assert ('dir="rtl"' in r.text) == (lang == "ar")
    menu = client.get("/app").text
    assert 'href="/app/leads"' in menu


def test_owner_settings_sources_and_preview_from_the_screen(client):
    biz, wa = setup()
    owner_session(client, biz)
    tok = csrf(client, "/app/leads?tab=settings")
    r = client.post("/app/leads/settings", data={"csrf": tok, "mode": "autopilot", "days": "0, 2, 5, 10", "nurture_days": "45",
                                                 "daily_cap": "12", "chat_number": "999 765 4321", "messenger": "https://facebook.com/estudioluz",
                                                 "greeting": "Hola, quiero info", "template": "seguimiento 1", "followup": "1", "voice": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        st = LD.settings(conn, biz["id"])
    assert (st["mode"], st["days"], st["nurture_days"], st["daily_cap"], st["chat_number"], st["messenger"], st["template"]) == \
        ("autopilot", [0, 2, 5, 10], 45, 12, "+529997654321", "estudioluz", "seguimiento1")
    assert st["voice"] and not st["nurture"] and st["paused_at"] is None
    for bad in ({"days": "abc"}, {"days": "400"}, {"nurture_days": "5"}, {"mode": "loud"}, {"chat_number": "12"}, {"messenger": "a b"}):
        with db.tx() as conn:
            with pytest.raises(Invalid):
                LD.save_settings(conn, biz, "t", **bad)
    r = client.post("/app/leads/sources", data={"csrf": tok, "kind": "google", "name": "Perfil de Google"}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        s = LD.sources(conn, biz["id"])[0]
    page = client.get("/app/leads?tab=sources").text
    assert s["code"] in page and "embed.js" in page and "qr.svg" in page
    assert client.post("/app/leads/sources", data={"csrf": tok, "kind": "nope", "name": "x"}, follow_redirects=False).status_code == 303
    with db.tx() as conn:
        assert len(LD.sources(conn, biz["id"])) == 1
    wa_in(wa, "5219990001101", "hola", "Pia")
    age(lead_by_phone(biz, "+529990001101"), NOW() - timedelta(hours=1))
    client.post("/lang/en", data={"csrf": tok})
    r = client.post("/app/leads/preview", data={"csrf": tok}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/app/leads?tab=settings").text
    assert "Would send" in page or "Would wait" in page
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.lead_touches)).scalar() == 0


def test_staff_see_leads_but_not_sources_or_settings(client):
    biz, _ = setup()
    owner_session(client, biz, role="staff")
    r = client.get("/app/leads?tab=settings")
    assert r.status_code == 200 and 'action="/app/leads/settings"' not in r.text
    tok = csrf(client, "/app/leads")
    assert client.post("/app/leads/settings", data={"csrf": tok, "mode": "autopilot"}, follow_redirects=False).headers["location"] == "/app"


def test_the_api_lists_leads_report_and_sources(client):
    biz, key = make_business("MX", "salon")
    h = {"Authorization": f"Bearer {key}"}
    r = client.post("/v1/lead-sources", headers=h, json={"kind": "marketplace", "name": "Marketplace"})
    assert r.status_code == 201 and r.json()["link"].startswith("https://one.test/l/") and "embed.js" in r.json()["embed"]
    assert client.post("/v1/lead-sources", headers=h, json={"kind": "zeppelin", "name": "x"}).status_code == 422
    assert client.post("/v1/lead-sources", headers=h, json={"name": "x", "dest": "javascript:alert(1)"}).status_code == 422
    with db.tx() as conn:
        C.create(conn, biz, "website", {"name": "Web", "email": "web@ex.com"})
    leads = client.get("/v1/leads/people", headers=h).json()
    assert len(leads) == 1 and leads[0]["status"] == "new" and leads[0]["consent"] == "form"
    assert client.get("/v1/leads/report?days=30", headers=h).json()["total"]["leads"] == 1
    assert len(client.get("/v1/lead-sources", headers=h).json()) == 1
    assert client.get("/v1/leads/people").status_code == 401
    assert client.get("/v1/leads", headers=h).json() == []                             # the Lead Ads list is still its own


def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = [k for k in files["en"] if k.startswith("ld_") or k in ("ft_leads_d", "ev_lead.sent", "ev_lead.opened_app")]
    assert len(keys) > 240
    same_ok = {"ld_k_instagram", "ld_w_messenger", "ld_k_marketplace", "ld_total", "ld_menu", "ld_status", "ld_leads_n", "ld_r_leads",
               "ld_tab_sources", "ld_source", "ld_k_flyer", "ld_message", "ld_k_website", "ld_mode_autopilot"}
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert sorted(re.findall(r"\{\w+\}", files[lg][k])) == sorted(re.findall(r"\{\w+\}", files["en"][k])), (lg, k)
    for lg in ("es", "ja", "ar", "hi", "zh"):
        same = [k for k in keys if files[lg][k] == files["en"][k] and k not in same_ok and len(files["en"][k]) > 10]
        assert not same, (lg, same[:5])
    assert files["en"]["ld_menu"] != files["en"].get("leads")                         # not the Lead Ads word


def test_kits_registry_export_and_the_migration():
    assert all("leads" in ip.kit.tools for ip in packs.industries().values() if ip.kit)
    assert kits.TOOLS["leads"].label == "ld_menu" and kits.TOOLS["leads"].uses
    assert kits.check_kits() == []
    biz, wa = setup()
    wa_in(wa, "5219990001201", "hola", "Rosa")
    lead = lead_by_phone(biz, "+529990001201")
    with db.tx() as conn:
        LV.save(conn, biz, "t", lead["contact_id"], OGG, "audio/ogg")
        out = EX.business_data(conn, biz["id"], "t")
        assert "leads" in kits.in_use(conn, biz["id"])
    assert len(out["leads"]) == 1 and out["lead_voice"] and "data" not in out["lead_voice"][0]
    assert "lead_sources" in out and "lead_settings" in out and "lead_touches" in out
    path = next((ROOT / "alembic" / "versions").glob("*_lead27_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1 and mig.down_revision == "a6f2c9e4d8b1"     # one head; v0.28 sits on top (intentional)
    assert set(mig.USES) == {"leads"}
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(tools={}))
        for sql in mig.pin_statements():
            conn.execute(text(sql))
        assert conn.execute(select(db.businesses.c.tools).where(db.businesses.c.id == biz["id"])).scalar() == {"leads": True}
        names = {r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
    assert {"lead_settings", "lead_sources", "leads", "lead_touches", "lead_voice"} <= names
    src = path.read_text()
    assert "raise RuntimeError" in src and "drop_table('leads')" in src                 # downgrade refuses while any lead exists


# ---------------------------------------------------------------- the business's OWN number: one tap, the share sheet
def test_voice_goes_from_the_owners_own_whatsapp_by_the_share_sheet(client):
    biz, wa = setup()
    wa_in(wa, "5219990001301", "hola", "Sara")
    lead = lead_by_phone(biz, "+529990001301")
    owner_session(client, biz)
    page = client.get(f"/app/leads/{lead['id']}").text
    assert 'data-share-btn data-wa="https://wa.me/529990001301"' in page and f'data-shared="/app/leads/{lead["id"]}/shared"' in page
    js = (ROOT / "valeria_one" / "web" / "static" / "leads.js").read_text()
    assert "navigator.canShare" in js and "navigator.share({files:[file]" in js and "a.download=" in js and "window.open(" in js
    tok = csrf(client, f"/app/leads/{lead['id']}")
    assert client.post(f"/app/leads/{lead['id']}/shared", data={"csrf": "bad", "how": "share"}).status_code == 403
    with db.tx() as conn:                                                          # a follow-up is due: the share moves the plan on
        conn.execute(update(db.leads).where(db.leads.c.id == lead["id"]).values(next_at=NOW() - timedelta(minutes=1)))
    r = client.post(f"/app/leads/{lead['id']}/shared", data={"csrf": tok, "how": "share", "do": "followup"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    t = touches(lead)[-1]
    assert (t["how"], t["status"], t["channel"]) == ("voice", "opened", "whatsapp_voice_share")
    assert get_lead(lead)["step"] == 1
    client.post(f"/app/leads/{lead['id']}/shared", data={"csrf": tok, "how": "download", "do": "answer"})
    assert touches(lead)[-1]["channel"] == "whatsapp_voice_downlo"[:20]


def test_send_by_whatsapp_is_the_default_one_tap_from_the_owners_own_number(client):
    biz, _ = make_business("CO", "generic", name="Taller Sur")                     # nothing connected at all
    with db.tx() as conn:
        c = C.create(conn, biz, "website", {"name": "Tomás", "phone": "+573001234567"})
        lead = LD.lead_of(conn, biz["id"], c["id"])
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": csrf(client, "/app")})
    page = client.get(f"/app/leads/{lead['id']}").text
    assert "Send by WhatsApp" in page and "Send (" not in page                    # no API account: the owner's own WhatsApp
    tok = csrf(client, f"/app/leads/{lead['id']}")
    r = client.post(f"/app/leads/{lead['id']}/open", data={"csrf": tok, "do": "followup", "via": "whatsapp", "body": "Hola Tomás 👋"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://wa.me/573001234567?text=Hola%20Tom%C3%A1s%20%F0%9F%91%8B"
    assert client.post(f"/app/leads/{lead['id']}/open", data={"csrf": tok, "via": "telegram"},
                       follow_redirects=False).headers["location"] != "https://wa.me/"


def test_autopilot_never_automates_a_generic_gateway_only_official_accounts():
    biz, _ = make_business("MX", "salon", name="Solo Pasarela")
    with db.tx() as conn:
        gw = X.connect(conn, biz, "t", channel="webhook", name="Gateway", cfg={"url": "https://gw.example/send", "secret": "s3"})
        c = C.create(conn, biz, "website", {"name": "Uma", "email": "uma@ex.com"})
        C.link(conn, biz["id"], c["id"], "webhook", "id-uma")
        lead = LD.lead_of(conn, biz["id"], c["id"])
    settings(biz, mode="autopilot")
    net = Wire()
    got = LD.round_(now=NOW() + timedelta(minutes=10), http=net.http)
    assert net.gw == [] and get_lead(lead)["blocked"] in ("no_channel", None) and got["sent"] == 0
    res = LD.send(biz, "user:x", lead["id"], body="Hola Uma", do="answer", http=net.http)   # the owner's own tap may use it
    assert res["status"] == "sent" and net.gw[-1]["body"] == "Hola Uma"
    assert LD.AUTO_AVOID == frozenset({"webhook"})


def test_a_design_studio_sends_from_its_own_whatsapp_with_one_tap(client):
    from valeria_one.services import projects as PR
    biz, _ = make_business("MX", "design", name="Estudio Alba")
    with db.tx() as conn:
        c = C.create(conn, biz, "user:x", {"name": "Valeria Ruiz", "phone": "+529991239876"})
        p = PR.create(conn, biz, "t", {"name": "Casa Norte", "contact_id": c["id"]})
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": csrf(client, "/app")})
    page = client.get(f"/app/projects/{p['id']}").text
    assert 'name="via" value="whatsapp"' in page and "Send by WhatsApp" in page
    tok = csrf(client, f"/app/projects/{p['id']}")
    r = client.post(f"/app/projects/{p['id']}/send", data={"csrf": tok, "body": "Ya están los planos", "with_link": "1", "via": "whatsapp"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://wa.me/529991239876?text=Ya%20est%C3%A1n%20los%20planos")
    assert "one.test/portal/" in r.headers["location"]
    with db.tx() as conn:
        notes = [x.body for x in conn.execute(select(db.project_updates.c.body).where(db.project_updates.c.project_id == p["id"]))]
    assert any("Ya están los planos" in n for n in notes)


# ---------------------------------------------------------------- one phone number, one person (any country)
def test_whatsapp_521_and_52_are_one_person_and_stop_blocks_them():
    biz, wa = setup()
    with db.tx() as conn:                                                           # came first through a form, typed as +52
        c = C.create(conn, biz, "website", {"name": "Rocío", "phone": "+52 55 1234 5678"})
    assert c["phone"] == "+525512345678"
    wa_in(wa, "5215512345678", "hola, soy Rocío", "Rocío")                          # WhatsApp's wa_id carries the old "1"
    with db.tx() as conn:
        n = conn.execute(select(func.count()).select_from(db.contacts).where(db.contacts.c.business_id == biz["id"])).scalar()
        conv = conn.execute(select(db.conversations.c.contact_id).where(db.conversations.c.business_id == biz["id"])).scalars().all()
    assert n == 1 and conv == [c["id"]]                                            # no duplicate: the same customer
    lead = lead_by_phone(biz, "+525512345678")
    wa_in(wa, "5215512345678", "STOP", "Rocío")                                    # STOP from 521… blocks the 52… customer
    with db.tx() as conn:
        assert C.get(conn, biz["id"], c["id"])["unsubscribed_at"] is not None
    assert get_lead(lead)["status"] == "stopped"
    links = LD.deeplinks({"phone": "+525512345678"}, "Hola")
    assert links[0]["url"].startswith("https://wa.me/525512345678?")                # one number in every link


def test_a_wa_id_from_another_country_is_never_read_as_a_local_number():
    biz, wa = setup()
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(country="AR"))
    biz = dict(biz, country="AR")
    wa_in(wa, "5215512345678", "hola desde México", "Toño")                        # a Mexican customer of a business in Argentina
    assert lead_by_phone(biz, "+525512345678") is not None
    wa_in(wa, "5491123456789", "hola", "Lali")                                      # an Argentine mobile, as WhatsApp sends it
    assert lead_by_phone(biz, "+5491123456789") is not None
    with db.tx() as conn:
        c = C.create(conn, biz, "website", {"name": "Lali", "phone": "011 15 2345-6789"})   # typed the local way: the same number
    assert c["phone"] == "+5491123456789"
