"""ghl01 (v0.18): campaigns (email, SMS, WhatsApp to the business's own customers) and drip sequences.

Every rule on every message: marketing consent only (and the page says who is left out and why), a way out in every
message (unsubscribe link + one-click List-Unsubscribe header; "Reply STOP"), quiet/polite hours, WhatsApp's template
rule outside the 24-hour window, frequency caps, throttling, and never twice. Nothing leaves the machine: SMTP and
Twilio are fakes, and the network is refused."""
import json
import re
from datetime import datetime, time, timedelta, timezone
from email import message_from_bytes
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from _va import refuse_network
from conftest import make_business
from test_web import signup
from valeria_one import channels, db
from valeria_one.channels import Inbound
from valeria_one.services import Invalid
from valeria_one.services import campaigns as CG
from valeria_one.services import contacts as C
from valeria_one.services import followups as FU
from valeria_one.services import inbox as X
from valeria_one.services import sequences as SQ

LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


class SMTP:
    sent: list = []

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def send_message(self, m):
        SMTP.sent.append(m)


@pytest.fixture
def smtp(monkeypatch):
    SMTP.sent = []
    monkeypatch.setattr(channels.adapter("email").__class__, "smtp_factory", SMTP)
    return SMTP.sent


def twilio(log: list):
    def handler(request):
        log.append(dict(x.split("=", 1) for x in request.content.decode().split("&") if "=" in x))
        return httpx.Response(201, json={"sid": f"SM{len(log):030d}", "status": "queued"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def polite(biz, hour=15) -> datetime:
    """The next {hour}:00 in the business's own time, after now (never a pinned day: seeds use the real clock)."""
    tz = ZoneInfo(biz["timezone"])
    loc = datetime.now(tz)
    t = datetime.combine(loc.date(), time(hour), tz)
    if t <= loc:
        t += timedelta(days=1)
    return t.astimezone(timezone.utc)


def setup_biz(country="MX"):
    biz, _ = make_business(country, "salon", name="Salón Rosa")
    with db.tx() as conn:
        X.connect(conn, biz, "t", channel="email", name="Correo", cfg={"smtp_host": "smtp.x", "smtp_port": "587", "username": "u",
                                                                       "password": "p", "from_email": "hola@rosa.mx", "secret": "s"})
        X.connect(conn, biz, "t", channel="sms", name="SMS", cfg={"account_sid": "AC1", "auth_token": "tok", "from_number": "+525511112222"})
        X.connect(conn, biz, "t", channel="whatsapp_twilio", name="WA", cfg={"account_sid": "AC1", "auth_token": "tok",
                                                                              "from_number": "+525511112222"})
        yes = {"marketing_consent": True, "marketing_consent_source": "web form"}
        ppl = {
            "ana": C.create(conn, biz, "t", {"name": "Ana López", "email": "ana@example.com", "phone": "+525511110001", "tags": ["vip"], **yes}),
            "beto": C.create(conn, biz, "t", {"name": "Beto", "email": "beto@example.com", "phone": "+525511110002", **yes}),
            "caro": C.create(conn, biz, "t", {"name": "Caro", "email": "caro@example.com", "phone": "+525511110003"}),      # never said yes
            "dani": C.create(conn, biz, "t", {"name": "Dani", "phone": "+525511110004", **yes}),                          # no email
            "eva": C.create(conn, biz, "t", {"name": "Eva", "email": "eva@example.com", "phone": "+525511110005", **yes}),
        }
        C.unsubscribe(conn, biz["id"], ppl["eva"]["id"], "t")                                                      # said STOP
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    return biz, ppl


def acc_of(biz, channel):
    with db.tx() as conn:
        return next(a for a in X.accounts(conn, biz["id"]) if a["channel"] == channel)


def email_campaign(biz, **extra):
    with db.tx() as conn:
        cp = CG.start(conn, biz, "user:x", "email")
        CG.save(conn, biz, "user:x", cp["id"], {"subject": "Hola {name}", "blocks": [
            {"t": "heading", "text": "Esta semana"}, {"t": "text", "text": "Hola {name}, 20% en cortes. Más en https://rosa.mx/promo"},
            {"t": "button", "text": "Reservar", "url": "https://rosa.mx/reservar"}], **extra})
        return CG.get(conn, biz["id"], cp["id"])


def outbox(cid=None):
    with db.tx() as conn:
        q = select(db.scheduled_messages)
        if cid:
            q = q.where(db.scheduled_messages.c.campaign_id == cid)
        return [dict(r) for r in conn.execute(q.order_by(db.scheduled_messages.c.send_at)).mappings()]


# ---------------------------------------------------------------- the audience and who is left out
def test_audience_counts_who_is_left_out_and_why():
    biz, ppl = setup_biz()
    with db.tx() as conn:
        a = CG.audience(conn, biz, {}, "email")
        assert a == {"total": 5, "ready": 2, "excluded": 3, "why": {"no_consent": 1, "unsubscribed": 1, "no_address": 1}}
        assert CG.audience(conn, biz, {}, "sms")["ready"] == 3                       # Dani has a phone
        assert CG.audience(conn, biz, {"tags": ["vip"]}, "email")["ready"] == 1
        assert CG.audience(conn, biz, CG.clean_audience(conn, biz, {"seen": {"op": "over", "days": 30}}), "email")["total"] == 0
        with pytest.raises(Invalid):
            CG.clean_audience(conn, biz, {"stage": "nope"})


# ---------------------------------------------------------------- email: blocks, unsubscribe, headers, never twice
def test_email_campaign_goes_once_with_unsubscribe_link_and_one_click_header(smtp):
    biz, ppl = setup_biz()
    cp = email_campaign(biz)
    with db.tx() as conn:
        assert CG.problems(conn, biz, cp) == []
        CG.schedule(conn, biz, "user:x", cp["id"])
    now = polite(biz)
    assert FU.dispatch_campaigns(now) == 1
    rows = outbox(cp["id"])
    assert {r["contact_id"] for r in rows} == {ppl["ana"]["id"], ppl["beto"]["id"]} and all(r["token"] for r in rows)
    assert FU.send_due(now=now)["sent"] == 2
    assert FU.dispatch_campaigns(now) == 0 and FU.send_due(now=now + timedelta(minutes=30))["sent"] == 0   # never twice
    assert len(smtp) == 2
    m = next(x for x in smtp if x["To"] == "ana@example.com")
    tok = next(r["token"] for r in outbox(cp["id"]) if r["contact_id"] == ppl["ana"]["id"])
    assert m["Subject"] == "Hola Ana"
    assert m["List-Unsubscribe"] == f"<https://one.test/u/{tok}>" and m["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    html = m.get_body(("html",)).get_content()
    text = m.get_body(("plain",)).get_content()
    assert f"https://one.test/u/{tok}" in html and f"https://one.test/u/{tok}" in text           # the way out, both parts
    assert f"https://one.test/c/{tok}/1" in html and "https://rosa.mx/reservar" not in html     # the button is counted
    assert "/o/" not in html                                                                     # opens: off by default
    assert 'max-width:600px' in html and "<script" not in html
    with db.tx() as conn:
        msgs = [dict(r) for r in conn.execute(select(db.messages).where(db.messages.c.direction == "out")).mappings()]
    assert {x["kind"] for x in msgs} == {"marketing"}


def test_unsubscribe_link_asks_and_one_click_post_unsubscribes(client, smtp):
    biz, ppl = setup_biz()
    cp = email_campaign(biz)
    with db.tx() as conn:
        CG.schedule(conn, biz, "user:x", cp["id"])
    now = polite(biz)
    FU.dispatch_campaigns(now)
    FU.send_due(now=now)
    tok = next(r["token"] for r in outbox(cp["id"]) if r["contact_id"] == ppl["ana"]["id"])
    page = client.get(f"/u/{tok}")
    assert page.status_code == 200 and "Salón Rosa" in page.text and f'action="/u/{tok}"' in page.text
    with db.tx() as conn:
        assert C.get(conn, biz["id"], ppl["ana"]["id"])["unsubscribed_at"] is None         # a GET (link scanners) changes nothing
    r = client.post(f"/u/{tok}", content=b"List-Unsubscribe=One-Click", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200
    with db.tx() as conn:
        c = C.get(conn, biz["id"], ppl["ana"]["id"])
        assert c["unsubscribed_at"] and not c["marketing_consent"]
        assert CG.stats(conn, biz["id"], campaign_id=cp["id"])["unsubscribed"] == 1
        assert CG.audience(conn, biz, {}, "email")["why"]["unsubscribed"] == 2
    assert client.post(f"/u/{tok}").status_code == 200                                        # safe to repeat
    assert client.get("/u/not-a-token").status_code == 404


def test_click_goes_only_to_the_campaigns_own_links_and_opens_count_when_on(client, smtp):
    biz, ppl = setup_biz()
    cp = email_campaign(biz, track_opens=True)
    with db.tx() as conn:
        CG.schedule(conn, biz, "user:x", cp["id"])
    now = polite(biz)
    FU.dispatch_campaigns(now)
    FU.send_due(now=now)
    tok = next(r["token"] for r in outbox(cp["id"]) if r["contact_id"] == ppl["ana"]["id"])
    assert f"/o/{tok}.gif" in next(m for m in smtp if m["To"] == "ana@example.com").get_body(("html",)).get_content()
    r = client.get(f"/c/{tok}/1", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://rosa.mx/reservar"
    assert client.get(f"/c/{tok}/9", follow_redirects=False).status_code == 404               # never anywhere else
    g = client.get(f"/o/{tok}.gif")
    assert g.status_code == 200 and g.headers["content-type"] == "image/gif" and "no-store" in g.headers["cache-control"]
    with db.tx() as conn:
        st = CG.stats(conn, biz["id"], campaign_id=cp["id"])
    assert st["clicked"] == 1 and st["opened"] == 1 and st["sent"] == 2


# ---------------------------------------------------------------- SMS: STOP and the segment counter
def test_sms_carries_stop_and_counts_segments():
    biz, ppl = setup_biz()
    log = []
    with db.tx() as conn:
        cp = CG.start(conn, biz, "user:x", "sms")
        CG.save(conn, biz, "user:x", cp["id"], {"message": "Hola {name}! 2x1 hoy."})
        CG.schedule(conn, biz, "user:x", cp["id"])
    now = polite(biz)
    FU.dispatch_campaigns(now)
    assert FU.send_due(now=now, http=twilio(log))["sent"] == 3
    from urllib.parse import unquote_plus
    bodies = sorted(unquote_plus(x["Body"]) for x in log)
    assert all(b.endswith(CG.stop_line(biz, "sms")) for b in bodies) and "Hola Ana! 2x1 hoy." in bodies[0]
    assert CG.sms_segments("a" * 160) == {"chars": 160, "segments": 1, "encoding": "GSM-7", "per": 160}
    assert CG.sms_segments("a" * 161)["segments"] == 2
    assert CG.sms_segments("€" * 80)["chars"] == 160                                          # the euro sign counts twice
    assert CG.sms_segments("¿Cuándo? 😀")["encoding"] == "Unicode"
    assert CG.sms_segments("x" * 71 + "😀")["segments"] == 2


# ---------------------------------------------------------------- quiet hours, polite hours, frequency caps, throttle
def test_night_waits_for_the_morning_and_caps_hold_back_a_second_offer(smtp):
    biz, ppl = setup_biz()                                  # Mexico sets no quiet hours: polite hours 21–08 apply
    cp = email_campaign(biz)
    with db.tx() as conn:
        CG.schedule(conn, biz, "user:x", cp["id"])
    night = polite(biz, 23)
    FU.dispatch_campaigns(night)
    assert FU.send_due(now=night)["later"] == 2 and smtp == []
    morning = outbox(cp["id"])[0]["send_at"].astimezone(ZoneInfo(biz["timezone"]))
    assert morning.hour == 8 and all(r["status"] == "pending" and r["error"] == "quiet hours" for r in outbox(cp["id"]))
    day = polite(biz, 10) if polite(biz, 10) > night else polite(biz, 10) + timedelta(days=1)
    assert FU.send_due(now=day)["sent"] == 2
    cp2 = email_campaign(biz)                                # a second offer the same day: the cap holds it a day
    with db.tx() as conn:
        CG.schedule(conn, biz, "user:x", cp2["id"])
    later = day + timedelta(hours=2)
    FU.dispatch_campaigns(later)
    assert FU.send_due(now=later)["later"] == 2
    assert all(r["error"] == "frequency cap" and r["send_at"] >= morning + timedelta(days=1) for r in outbox(cp2["id"]))


def test_country_quiet_hours_too(smtp):
    biz, ppl = setup_biz("US")
    cp = email_campaign(biz)
    with db.tx() as conn:
        CG.schedule(conn, biz, "user:x", cp["id"])
    late = polite(biz, 22)
    FU.dispatch_campaigns(late)
    assert FU.send_due(now=late)["later"] == 2 and smtp == []


def test_big_lists_are_throttled(monkeypatch, smtp):
    biz, ppl = setup_biz()
    monkeypatch.setitem(CG.THROTTLE, "email", 1)
    cp = email_campaign(biz)
    with db.tx() as conn:
        CG.schedule(conn, biz, "user:x", cp["id"])
    now = polite(biz)
    FU.dispatch_campaigns(now)
    times = [r["send_at"] for r in outbox(cp["id"])]
    assert times == [now, now + timedelta(minutes=1)]
    assert FU.send_due(now=now)["sent"] == 1 and FU.send_due(now=now + timedelta(minutes=1))["sent"] == 1


# ---------------------------------------------------------------- WhatsApp: the approved template outside the window
def test_whatsapp_needs_the_template_outside_the_24h_window():
    biz, ppl = setup_biz()
    with db.tx() as conn:
        cp = CG.start(conn, biz, "user:x", "whatsapp")
        CG.save(conn, biz, "user:x", cp["id"], {"message": "Hola {name}, ¡promo!"})
        assert "cg_need_template" in CG.problems(conn, biz, CG.get(conn, biz["id"], cp["id"]))
        with pytest.raises(Invalid):
            CG.schedule(conn, biz, "user:x", cp["id"])
    # Ana wrote an hour ago (inside the window): free text may go; Beto and Dani never wrote: only the template
    acc = acc_of(biz, "whatsapp_twilio")
    now = polite(biz)
    X.receive_items(acc,
                    [Inbound(address="+525511110001", body="hola", provider_id="in1", at=now - timedelta(hours=1))])
    with db.tx() as conn:
        CG.save(conn, biz, "user:x", cp["id"], {"template": "HXpromo123"})
        CG.schedule(conn, biz, "user:x", cp["id"])
    FU.dispatch_campaigns(now)
    log = []
    assert FU.send_due(now=now, http=twilio(log))["sent"] == 3
    assert sum(1 for x in log if x.get("ContentSid") == "HXpromo123") == 2 and sum(1 for x in log if "Body" in x) == 1
    with db.tx() as conn:                                   # a campaign whose template was removed: those outside are skipped
        cp2 = CG.start(conn, biz, "user:x", "whatsapp")
    row = {"id": None, "token": "t", "created_at": now}
    res = CG.deliver(row, biz, ppl["beto"], {"channel": "whatsapp", "text": "hola"}, kind="marketing", track_opens=False,
                     http=twilio([]), now=now + timedelta(days=2))
    assert res[0] == "skipped" and res[1] == "needs_template"
    _ = cp2


# ---------------------------------------------------------------- "send me a test", AI drafts, the screens
def test_send_me_a_test_goes_only_to_the_owner(smtp):
    biz, ppl = setup_biz()
    cp = email_campaign(biz)
    assert CG.send_test(biz, "user:x", cp, "owner@example.com") == "sent"
    assert smtp[-1]["To"] == "owner@example.com" and smtp[-1]["Subject"].startswith(CG._t("cg_test_prefix", "es"))
    assert outbox() == []                                     # never a customer row, never counted
    with pytest.raises(Invalid):
        CG.send_test(biz, "user:x", cp, "")


def test_ai_writes_in_the_owners_style_or_a_plain_start(monkeypatch):
    biz, ppl = setup_biz()
    assert CG.ai_write(biz, "2x1 en uñas el viernes", "sms")["by_ai"] is False                # no key: a plain draft
    from valeria_one.services import ai as AI
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="k", mode="suggest")
    seen = {}

    def fake(provider, key, model, system, turns, http, max_tokens=None):
        seen["system"] = system
        return "Subject: Viernes de uñas\n\nHola {name}, este viernes 2x1 en uñas."
    monkeypatch.setattr(AI, "_call", fake)
    got = CG.ai_write(biz, "2x1 en uñas el viernes", "email")
    assert got == {"text": "Hola {name}, este viernes 2x1 en uñas.", "subject": "Viernes de uñas", "by_ai": True}
    assert "never invent" in seen["system"] and "Write the way the owner does" in seen["system"]


def test_the_builder_screens_one_question_at_a_time(client, smtp):
    signup(client, email="cg@example.com")
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        X.connect(conn, biz, "t", channel="email", name="Correo", cfg={"smtp_host": "smtp.x", "username": "u", "password": "p",
                                                                       "from_email": "hola@x.mx", "secret": "s"})
        C.create(conn, biz, "t", {"name": "Ana", "email": "ana@example.com", "marketing_consent": True, "marketing_consent_source": "x"})
    t = re.search(r'name="csrf" value="([^"]+)"', client.get("/app/campaigns").text).group(1)
    page = client.get("/app/campaigns").text
    assert 'name="channel" value="email"' in page and 'value="sms" disabled' in page          # not connected: can't pick it
    r = client.post("/app/campaigns/new", data={"csrf": t, "channel": "email"}, follow_redirects=False)
    url = r.headers["location"].split("?")[0]
    who = client.get(url + "?step=who").text
    assert "1 will get it" in who or "1 " in who
    client.post(url, data={"csrf": t, "act": "who", "tags": "", "name": "Promo"})
    client.post(url, data={"csrf": t, "act": "message", "subject": "Hola", "blk_t": ["heading", "text"], "blk_text": ["Hola", "Texto"],
                           "blk_url": ["", ""]})
    chk = client.get(url + "?step=check").text
    assert "srcdoc=" in chk and 'sandbox=""' in chk
    assert client.get(url + "/email").headers["content-security-policy"] == "script-src 'none'"
    r = client.post(url, data={"csrf": t, "act": "schedule", "when": "now"}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        assert conn.execute(select(db.campaigns.c.status)).scalar() == "scheduled"
    assert "How it went" in client.get(url).text or client.get(url).status_code == 200


# ---------------------------------------------------------------- drips: in, steps, out
def test_sequence_enrolls_on_tag_sends_steps_and_stops_on_reply(smtp):
    biz, ppl = setup_biz()
    with db.tx() as conn:
        seq = SQ.save(conn, biz, "user:x", {"name": "Bienvenida", "kind": "marketing", "trigger": "tag.added", "trigger_arg": {"tag": "nueva"},
                                            "steps": [{"amount": 0, "unit": "minutes", "channel": "email", "subject": "Hola", "text": "Paso 1"},
                                                      {"amount": 2, "unit": "days", "channel": "email", "subject": "Hola", "text": "Paso 2"}],
                                            "exit_on": ["reply"], "active": True})
        assert seq["exit_on"] == ["reply", "unsubscribe"]                                     # an unsubscribe always ends offers
        C.patch(conn, biz, "user:x", ppl["ana"]["id"], {"tags": ["vip", "nueva"]})
        C.patch(conn, biz, "user:x", ppl["caro"]["id"], {"tags": ["nueva"]})                  # no consent: not enrolled
        en = SQ.enrollments(conn, biz["id"], seq["id"])
    assert [e["contact_id"] for e in en] == [ppl["ana"]["id"]]
    now = polite(biz)
    assert SQ.round_(now)["queued"] == 1 and SQ.round_(now)["queued"] == 0                     # once
    assert FU.send_due(now=now)["sent"] == 1 and smtp[-1]["Subject"] == "Hola"
    X.receive_items(acc_of(biz, "email"), [Inbound(address="ana@example.com", body="gracias", provider_id="r1",
                                                                at=now + timedelta(hours=1))])
    with db.tx() as conn:
        e = SQ.enrollments(conn, biz["id"], seq["id"])[0]
        assert e["status"] == "exited" and e["exit_reason"] == "reply"
    assert SQ.round_(now + timedelta(days=3))["queued"] == 0
    with db.tx() as conn:
        assert CG.stats(conn, biz["id"], sequence_id=seq["id"])["replied"] == 1
        with pytest.raises(Invalid):
            SQ.save(conn, biz, "t", {"name": "x", "steps": []})


def test_sequence_by_form_booking_exit_and_the_recipe_action(smtp):
    biz, ppl = setup_biz()
    from valeria_one.services import recipes as RC
    with db.tx() as conn:
        seq = SQ.save(conn, biz, "user:x", {"name": "Cotización", "kind": "service", "trigger": "manual",
                                            "steps": [{"amount": 1, "unit": "hours", "channel": "auto", "text": "¿Te ayudo con algo?"}],
                                            "exit_on": ["booking"], "active": True})
        r = RC.save(conn, biz, "user:x", {"trigger": "lead.new", "actions": [{"do": "sequence", "sequence_id": str(seq["id"])}]})
        assert "Cotización" in RC.describe(conn, biz, r, "es")
        c = C.create(conn, biz, "webhook:sms", {"name": "Fer", "phone": "+525511110009"})        # a new lead: the recipe adds them
        en = SQ.enrollments(conn, biz["id"], seq["id"])
        assert [e["contact_id"] for e in en] == [c["id"]]                                     # service: no marketing consent needed
        assert SQ.enroll(conn, biz, seq, c["id"], "user:x") is None                           # never twice
        from valeria_one.services import deals as D
        st = datetime.now(timezone.utc) + timedelta(days=2)
        D.book(conn, biz, "t", {"title": "Corte", "contact_id": c["id"], "starts_at": st, "ends_at": st + timedelta(hours=1)})
        assert SQ.enrollments(conn, biz["id"], seq["id"])[0]["exit_reason"] == "booking"


def test_new_words_are_in_all_11_languages():
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    new = [k for k in files["en"] if k.startswith(("cg_", "sq_"))] + ["campaigns"]
    assert len(new) > 150
    for lg in LANGS:
        for k in new:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
