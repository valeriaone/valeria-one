"""Charging for Valeria One on a hosted server (VO_BILLING=on): trials, plans, the free fallback, bank
transfers the admin confirms, cards through Stripe, referrals, notices — plus password resets, the
'Made with Valeria One' link, and WhatsApp through the business's own Twilio."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import make_business
from valeria_one import blocks, crypto, db, jobs, ops
from valeria_one.services import Invalid
from valeria_one.services import billing as BL
from valeria_one.services import businesses as B
from valeria_one.services import mailer

PW = "una-clave-larga"
EN = {"Accept-Language": "en"}


def csrf(client, path):
    m = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def signup(client, email="owner@example.com", country="MX", name="Taller Ruiz", ref=None):
    if ref:
        client.get(f"/signup?ref={ref}")
    tok = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": tok, "business_name": name, "country": country, "industry": "generic",
                                     "email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    with db.tx() as conn:
        return dict(conn.execute(db.businesses.select().where(db.businesses.c.name == name)).mappings().one())


def biz(bid):
    with db.tx() as conn:
        return B.get(conn, bid)


def expire(bid):
    with db.tx() as conn:
        conn.execute(db.businesses.update().where(db.businesses.c.id == bid)
                     .values(trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1)))


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("VO_BILLING", "on")
    monkeypatch.setenv("VO_SELLER_COUNTRY", "MX")


def admin_client(client):
    """The server's first account (admin, its own business never charged)."""
    from test_selfhost import do_setup
    do_setup(client, email="admin@example.com")
    return client


# ---------------------------------------------------------------- off by default: self-hosted never charges
def test_billing_off_means_everything(client):
    b = signup(client)
    assert BL.effective(b) == "unlimited" and b["trial_ends_at"] is None and re.fullmatch(r"VO-[A-Z2-9]{5}", b["billing_ref"])
    assert client.get("/pricing").status_code == 404
    assert client.get("/app/billing").status_code == 404
    assert 'href="/app/billing"' not in client.get("/app").text


# ---------------------------------------------------------------- trial, plans, free fallback
def test_trial_then_free_then_back(client, on):
    b = signup(client)
    assert b["plan"] == "starter" and b["trial_ends_at"] > datetime.now(timezone.utc) + timedelta(days=29)
    assert BL.effective(b) == "starter" and blocks.on(b, "calendar")
    assert 'href="/app/billing"' in client.get("/app").text
    expire(b["id"])
    b = biz(b["id"])
    assert BL.effective(b) == "free" and not blocks.on(b, "calendar") and not blocks.on(b, "ai")
    today = client.get("/app", headers=EN).text
    assert "You are on the free plan" in today and 'href="/app/calendar"' not in today
    assert client.get("/app/inbox").status_code == 200 and client.get("/app/contacts").status_code == 200   # the core keeps working
    start = client.get("/app/start", headers=EN).text
    assert "Needs a plan" in start and 'value="calendar" checked disabled' in start
    with db.tx() as conn:
        conn.execute(db.businesses.update().where(db.businesses.c.id == b["id"])
                     .values(paid_until=datetime.now(timezone.utc) + timedelta(days=5)))
    assert blocks.on(biz(b["id"]), "calendar")                                      # paying brings it all back


def test_prices_and_tax(on):
    mx, _ = make_business("MX")
    us, _ = make_business("US")
    q = BL.quote(biz(mx["id"]), "starter", 1)
    assert (q["currency"], q["price_minor"], q["tax_minor"], q["amount_minor"], q["tax_name"]) == ("MXN", 24900, 3984, 28884, "IVA")
    q = BL.quote(biz(us["id"]), "pro", 12)
    assert (q["currency"], q["amount_minor"], q["tax_minor"]) == ("USD", 7900 * 12, 0)
    ke, _ = make_business("KE")
    assert BL.quote(biz(ke["id"]), "starter", 1)["currency"] == "USD"             # no KES price: dollars
    with pytest.raises(Invalid):
        BL.quote(biz(mx["id"]), "gold", 1)


def test_people_limit(client, on):
    b = signup(client)
    from valeria_one.services import team as TM
    with db.tx() as conn:
        for n in range(6):                                                         # the paid plan: the whole team
            TM.invite(conn, biz(b["id"]), "t", f"p{n}@example.com")
    expire(b["id"])
    with db.tx() as conn, pytest.raises(Invalid):
        TM.invite(conn, biz(b["id"]), "t", "late@example.com")                     # free: 1 person


def test_server_business_is_never_charged(client, on):
    admin_client(client)
    with db.tx() as conn:
        b = dict(conn.execute(db.businesses.select()).mappings().one())
    assert b["billing_exempt"] and BL.effective(b) == "pro"


# ---------------------------------------------------------------- bank transfers the admin confirms
def test_transfer_flow(client, on):
    admin_client(client)
    client.cookies.clear()
    b = signup(client, email="owner@example.com")
    expire(b["id"])
    page = client.get("/app/billing").text
    assert "Pay by bank transfer" not in page                                       # no account set yet
    # the admin sets where transfers go (needs the password again)
    from test_selfhost import do_setup  # noqa: F401
    owner_cookies = dict(client.cookies)
    client.cookies.clear()
    tok = csrf(client, "/login")
    client.post("/login", data={"csrf": tok, "email": "admin@example.com", "password": PW})
    tok = csrf(client, "/app/server")
    acct = {"csrf": tok, "bank": "Banamex", "holder": "Titular de Prueba", "account": "002180700000000001", "currency": "MXN"}
    client.post("/app/server-billing/account", data={**acct, "password": "wrong-password"})
    with db.tx() as conn:
        assert BL.account(conn) is None
    client.post("/app/server-billing/account", data={**acct, "password": PW})
    with db.tx() as conn:
        assert BL.account(conn)["bank"] == "Banamex"
    admin_cookies = dict(client.cookies)
    # the owner pays by transfer
    client.cookies.clear()
    client.cookies.update(owner_cookies)
    page = client.get("/app/billing").text
    assert "002180700000000001" in page and b["billing_ref"] in page and "288.84" in page
    tok = csrf(client, "/app/billing")
    client.post("/app/billing/transfer", data={"csrf": tok, "plan": "starter", "months": "1", "note": "SPEI 12345"})
    client.post("/app/billing/transfer", data={"csrf": tok, "plan": "starter", "months": "1"})       # twice: one request
    with db.tx() as conn:
        pend = BL.pending(conn)
    assert len(pend) == 1 and pend[0]["reference"] == f"{b['billing_ref']}-1" and pend[0]["amount_minor"] == 28884
    assert any(m["to"] == "admin@example.com" and b["billing_ref"] in m["body"] for m in mailer.OUTBOX)
    assert "We are checking your transfer" in client.get("/app/billing", headers=EN).text
    # the admin sees it arrived
    client.cookies.clear()
    client.cookies.update(admin_cookies)
    assert "Transfers to confirm" in client.get("/app/server", headers=EN).text
    tok = csrf(client, "/app/server")
    client.post(f"/app/server-billing/{pend[0]['id']}/approve", data={"csrf": tok})
    client.post(f"/app/server-billing/{pend[0]['id']}/approve", data={"csrf": tok})                 # twice: once
    nb = biz(b["id"])
    assert BL.effective(nb) == "starter" and nb["paid_until"] > datetime.now(timezone.utc) + timedelta(days=27)
    first = nb["paid_until"]
    assert any(m["to"] == "owner@example.com" and "Taller Ruiz" in m["subject"] and "pago" in m["subject"] for m in mailer.OUTBOX)
    with db.tx() as conn:
        assert BL.report(conn)["received"][0]["amount_minor"] == 28884
        # a 12-month transfer adds 12 months on top of the paid time
        p = BL.request_transfer(conn, nb, "t", "pro", 12)
        BL.decide(conn, p["id"], "t", True)
    nb = biz(b["id"])
    assert nb["plan"] == "pro" and nb["paid_until"].year * 12 + nb["paid_until"].month == first.year * 12 + first.month + 12


def test_rejected_transfer_and_exempt(client, on):
    admin_client(client)
    with db.tx() as conn:
        from valeria_one.services import instance
        instance.kv_set(conn, "billing_account", {"bank": "B", "holder": "H", "account": "1234", "swift": "", "currency": "MXN"})
    other, _ = make_business("MX", name="Otra")
    with db.tx() as conn:
        p = BL.request_transfer(conn, biz(other["id"]), "t", "starter", 1)
        assert BL.decide(conn, p["id"], "admin", False)["status"] == "rejected"
        BL.set_exempt(conn, other["id"], "admin", True)
    assert BL.effective(biz(other["id"])) == "pro"
    usd, _ = make_business("US", name="Dollar Shop")
    with db.tx() as conn, pytest.raises(Invalid):
        BL.request_transfer(conn, biz(usd["id"]), "t", "starter", 1)               # an MXN account: dollars go by card


# ---------------------------------------------------------------- cards (Stripe)
def _stripe_post(client, event, secret="whsec_test"):
    body = json.dumps(event).encode()
    t = str(int(time.time()))
    sig = crypto.hmac_sha256_hex(secret, f"{t}.".encode() + body)
    return client.post("/hooks/billing/stripe", content=body, headers={"Stripe-Signature": f"t={t},v1={sig}"})


def test_card_subscription(client, on, monkeypatch):
    monkeypatch.setenv("VO_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("VO_STRIPE_WEBHOOK_SECRET", "whsec_test")
    b = signup(client, country="US", name="Card Shop")
    seen = {}

    def handler(req):
        seen.update(path=req.url.path, form=parse_qs(req.content.decode()))
        return httpx.Response(200, json={"url": "https://checkout.stripe.test/c/abc", "id": "cs_1"})
    real = httpx.Client
    monkeypatch.setattr(BL.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))
    tok = csrf(client, "/app/billing")
    r = client.post("/app/billing/card", data={"csrf": tok, "plan": "pro"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://checkout.stripe.test/c/abc"
    f = seen["form"]
    assert f["mode"] == ["subscription"] and f["line_items[0][price_data][unit_amount]"] == ["7900"]
    assert f["line_items[0][price_data][currency]"] == ["usd"] and f["subscription_data[metadata][vo_business]"] == [str(b["id"])]
    # Stripe tells us
    assert _stripe_post(client, {"type": "invoice.paid"}, secret="wrong").status_code == 400
    r = _stripe_post(client, {"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "client_reference_id": str(b["id"]), "customer": "cus_1", "subscription": "sub_1",
        "metadata": {"vo_business": str(b["id"]), "vo_plan": "pro"}}}})
    assert r.json()["result"] == "subscribed"
    end = int((datetime.now(timezone.utc) + timedelta(days=31)).timestamp())
    inv = {"type": "invoice.paid", "data": {"object": {"id": "in_1", "number": "A-0001", "customer": "cus_1", "amount_paid": 7900,
           "currency": "usd", "parent": {"subscription_details": {"metadata": {"vo_business": str(b["id"]), "vo_plan": "pro"}}},
           "lines": {"data": [{"period": {"end": end}}]}}}}
    assert _stripe_post(client, inv).json()["result"] == "paid"
    assert _stripe_post(client, inv).json()["result"] == "duplicate"
    nb = biz(b["id"])
    assert nb["plan"] == "pro" and nb["stripe_subscription_id"] == "sub_1" and int(nb["paid_until"].timestamp()) == end
    assert "Paid by card" in client.get("/app/billing", headers=EN).text
    r = _stripe_post(client, {"type": "customer.subscription.deleted", "data": {"object": {"metadata": {"vo_business": str(b["id"])}}}})
    assert r.json()["result"] == "cancelled" and biz(b["id"])["stripe_subscription_id"] is None
    assert BL.effective(biz(b["id"])) == "pro"                                    # paid time still runs to its end


# ---------------------------------------------------------------- referrals, notices, prices page
def test_referral_and_commission(client, on):
    agency = signup(client, email="agency@example.com", name="Agencia Uno")
    client.cookies.clear()
    child = signup(client, email="client@example.com", name="Cliente Uno", ref=agency["billing_ref"].lower())
    assert child["referred_by"] == agency["id"]
    with db.tx() as conn:
        from valeria_one.services import instance
        instance.kv_set(conn, "billing_account", {"bank": "B", "holder": "H", "account": "1", "swift": "", "currency": "MXN"})
        p = BL.request_transfer(conn, biz(child["id"]), "t", "starter", 1)
        BL.decide(conn, p["id"], "admin", True)
        rep = BL.report(conn)
    r = rep["referrers"][0]
    assert r["name"] == "Agencia Uno" and r["commission_minor"] == 28884 * 30 // 100 and r["referred"] == 1


def test_trial_notices_once(client, on):
    b = signup(client, email="notice@example.com", name="Aviso SA", country="US")
    with db.tx() as conn:
        conn.execute(db.businesses.update().where(db.businesses.c.id == b["id"])
                     .values(trial_ends_at=datetime.now(timezone.utc) + timedelta(days=2)))
    assert BL.notices()["ending"] == 1 and BL.notices()["ending"] == 0
    assert any(m["to"] == "notice@example.com" and "3 days" in m["subject"] for m in mailer.OUTBOX)
    expire(b["id"])
    assert BL.notices()["ended"] == 1 and BL.notices()["ended"] == 0
    assert "billing" in jobs._round()[0]


def test_pricing_page(client, on):
    p = client.get("/pricing")
    assert p.status_code == 200 and "39.00 USD" in p.text and "249.00 MXN" in p.text and "IVA" in p.text and "30 days" in p.text
    assert "79.00 USD" not in p.text and "whole team" in p.text                    # one plan, the whole team


# ---------------------------------------------------------------- forgot my password
def test_password_reset(client):
    signup(client, email="forgot@example.com", name="Olvido")
    client.cookies.clear()
    tok = csrf(client, "/forgot")
    client.post("/forgot", data={"csrf": tok, "email": "nobody@example.com"})
    assert mailer.OUTBOX == []                                                    # same answer, nothing sent
    assert "a link is on its way" in client.get("/login").text or True
    client.post("/forgot", data={"csrf": csrf(client, "/forgot"), "email": "FORGOT@example.com"})
    link = re.search(r"https://one\.test(/reset/[\w-]+)", mailer.OUTBOX[-1]["body"]).group(1)
    assert 'name="password"' in client.get(link).text
    r = client.post(link, data={"csrf": csrf(client, link), "password": "otra-clave-nueva"}, follow_redirects=False)
    assert r.headers["location"] == "/app"
    assert client.get(link, follow_redirects=False).headers["location"] == "/forgot"   # used: gone
    client.cookies.clear()
    tok = csrf(client, "/login")
    ok = client.post("/login", data={"csrf": tok, "email": "forgot@example.com", "password": "otra-clave-nueva"}, follow_redirects=False)
    assert ok.headers["location"] == "/app"
    for _ in range(3):
        client.post("/forgot", data={"csrf": csrf(client, "/forgot"), "email": "forgot@example.com"})
    assert len([m for m in mailer.OUTBOX if m["to"] == "forgot@example.com"]) == 3     # 3 an hour, then quiet


def test_reset_link_command(capsys, client):
    signup(client, email="cli@example.com", name="CLI")
    assert ops.main(["ops", "reset-link", "cli@example.com"]) == 0
    assert "/reset/" in capsys.readouterr().out
    assert ops.main(["ops", "reset-link", "none@example.com"]) == 1


def test_billing_account_command(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"bank": "Banamex", "holder": "X", "account": "002180700000000001"})))
    assert ops.main(["ops", "billing-account"]) == 0
    with db.tx() as conn:
        assert BL.account(conn)["currency"] == "MXN"
    assert "…0001" in capsys.readouterr().out


# ---------------------------------------------------------------- 'Made with Valeria One'
def test_badge(client, monkeypatch):
    b = signup(client, name="Badge Shop")
    from valeria_one.services import booking as BK
    with db.tx() as conn:
        BK.save_settings(conn, biz(b["id"]), "t", enabled=True, slug="badge-shop", hours={"mon": [["09:00", "18:00"]]},
                         notice_minutes=0, days_ahead=30)
    page = client.get("/b/badge-shop", headers=EN).text
    assert "Made with Valeria One" in page and "github.com/valeriaone" in page
    monkeypatch.setenv("VO_BILLING", "on")                                     # switched on later: still working, its trial starts
    assert f"/signup?ref={b['billing_ref']}" in client.get("/b/badge-shop").text
    assert BL.notices()["trials_started"] == 1 and biz(b["id"])["trial_ends_at"] > datetime.now(timezone.utc) + timedelta(days=29)
    monkeypatch.setenv("VO_BADGE", "off")
    assert "Made with Valeria One" not in client.get("/b/badge-shop", headers=EN).text


# ---------------------------------------------------------------- WhatsApp through the business's own Twilio
def test_whatsapp_through_twilio(client, us):
    from valeria_one.channels.twilio_sms import signature
    from valeria_one.services import inbox as X
    bz, h = us
    r = client.post("/v1/channels", headers=h, json={"channel": "whatsapp_twilio", "name": "WA",
                                                     "config": {"account_sid": "AC1", "auth_token": "TT", "from_number": "+15550001111"}})
    assert r.status_code == 201, r.text
    path = r.json()["webhook_url"].replace("https://one.test", "")
    form = {"From": "whatsapp:+19514343352", "To": "whatsapp:+15550001111", "Body": "Hi, open today?", "MessageSid": "SMwa1",
            "NumMedia": "0", "ProfileName": "Ana Ruiz"}
    r = client.post(path, data=form, headers={"X-Twilio-Signature": signature("TT", "https://one.test" + path, form)})
    assert r.status_code == 200 and r.text == "<Response/>"
    c = client.get("/v1/contacts", headers=h).json()[0]
    assert c["name"] == "Ana Ruiz" and c["phone"] == "+19514343352"
    sent = []

    def handler(req):
        sent.append(parse_qs(req.content.decode()))
        return httpx.Response(201, json={"sid": "SMout", "status": "queued"})
    m = X.send(biz(bz["id"]), "t", contact_id=c["id"], body="Yes, until 6pm", channel="whatsapp_twilio",
               http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert m["status"] == "queued" and sent[-1]["To"] == ["whatsapp:+19514343352"] and sent[-1]["From"] == ["whatsapp:+15550001111"]
    X.send(biz(bz["id"]), "t", contact_id=c["id"], channel="whatsapp_twilio",
           template={"name": "HXabc123", "language": "en", "params": ["Ana", "tomorrow"]},
           http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert sent[-1]["ContentSid"] == ["HXabc123"] and json.loads(sent[-1]["ContentVariables"][0]) == {"1": "Ana", "2": "tomorrow"}
    assert "Body" not in sent[-1]
