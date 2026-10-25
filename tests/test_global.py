"""Any business, any country: every country can sign up (YAML pack or safe defaults from world data), any
currency, any bank, cash, STOP in many languages, the owner's data export, each business's privacy notice,
the launch window (free months, then paused until it pays), the first-business-from-a-new-country alert,
and Viber on the business's own bot."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from conftest import make_business
from valeria_one import db, packs
from valeria_one.services import Invalid
from valeria_one.services import billing as BL
from valeria_one.services import businesses as B
from valeria_one.services import contacts as C
from valeria_one.services import inbox as X
from valeria_one.services import instance as I
from valeria_one.services import mailer
from valeria_one.services import payments as PM

PW = "una-clave-larga"


def _csrf(client, path):
    m = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def _signup(client, name, country, email):
    r = client.post("/signup", data={"csrf": _csrf(client, "/signup"), "business_name": name, "country": country,
                                     "industry": "generic", "email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    with db.tx() as conn:
        return dict(conn.execute(db.businesses.select().where(db.businesses.c.name == name)).mappings().one())


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- every country
def test_every_country_can_sign_up_with_safe_defaults():
    codes = packs.open_codes()
    assert len(codes) >= 236 and set(packs.countries()) <= set(codes) and not set(codes) & packs.blocked()
    with db.tx() as conn:
        for code in codes:
            biz = B.create(conn, name=f"Biz {code}", country=code)
            p = packs.country(code)
            assert (biz["country"], biz["currency"], biz["timezone"]) == (code, p.currency, p.timezone)
            assert biz["language"] in p.languages
    for code in ("PL", "VN", "ZW", "SA", "KR", "XK"):         # no YAML: made from world data, strict defaults
        p = packs.country(code)
        assert p.auto and p.payments == ["bank_transfer"] and p.messaging.quiet_hours == (21, 8)
        assert p.messaging.marketing_consent == "opt_in" and p.tax.mode == "none" and "viber" in p.channels
    assert (packs.country("PL").currency, packs.country("VN").locale, packs.country("SA").languages[0]) == ("PLN", "vi_VN", "ar")
    assert not packs.country("PH").auto and packs.country("ph").code == "PH"
    with db.tx() as conn:
        for bad in ("QQ", "ZZ", "", "MEX", "AQ", "CU", "IR", "KP", "SY"):
            try:
                B.create(conn, name="x", country=bad)
                raise AssertionError(bad)
            except Invalid as e:
                assert e.field == "country"


def test_signup_lists_every_country_and_guesses_from_the_browser(client):
    h = client.get("/signup", headers={"Accept-Language": "en-PH,en;q=0.8"}).text
    opts = re.findall(r'<option value="([A-Z]{2})"', h)
    assert len(opts) == len(packs.open_codes()) and 'value="PH" selected' in h and 'value="MX" selected' not in h
    assert not {"CU", "IR", "KP", "SY"} & set(opts)
    h = client.get("/signup", headers={"Accept-Language": "en"}).text
    assert "selected" not in re.search(r'<select id="co".*?</select>', h, re.S).group(0)


# ---------------------------------------------------------------- currency + language
def test_currency_is_the_business_choice_and_old_records_keep_theirs(client, mx):
    biz, h = mx
    d1 = client.post("/v1/deals", headers=h, json={"title": "Old", "amount_minor": 50000}).json()
    assert d1["currency"] == "MXN"
    r = client.patch("/v1/business", headers=h, json={"currency": "eur", "language": "fr", "tax_id": " FR 123 "})
    assert r.status_code == 200 and (r.json()["currency"], r.json()["language"], r.json()["tax_id"]) == ("EUR", "fr", "FR 123")
    d2 = client.post("/v1/deals", headers=h, json={"title": "New", "amount_minor": 1000}).json()
    assert d2["currency"] == "EUR"
    assert client.get(f"/v1/deals/{d1['id']}", headers=h).json()["currency"] == "MXN"
    assert client.patch("/v1/business", headers=h, json={"currency": "XYZ"}).status_code == 422
    assert client.patch("/v1/business", headers=h, json={"language": "xx"}).status_code == 422
    assert client.patch("/v1/business", headers=h, json={"language": "zz-QQ"}).status_code == 422
    with db.tx() as conn:
        for c in ("JPY", "KWD", "INR", "PHP"):
            assert B.create(conn, name=f"c{c}", country="US", currency=c)["currency"] == c


# ---------------------------------------------------------------- getting paid
def test_bank_details_of_any_country_and_cash():
    biz, _ = make_business("IN")
    with db.tx() as conn:
        PM.connect(conn, biz, "t", "bank_transfer", {"bank": "HDFC", "holder": "Asha", "account": "50100123",
                                                     "ifsc": "HDFC0001234", "iban": "", "note": "UPI ok"})
        c = C.create(conn, biz, "t", {"name": "Ravi"})
    pay = PM.create(biz, "t", amount_minor=150000, description="Haircut", contact_id=c["id"])
    with db.tx() as conn:
        _, _, bank = PM.by_token(conn, pay["token"])
    assert bank["ifsc"] == "HDFC0001234" and "iban" not in bank
    from fastapi.testclient import TestClient
    from valeria_one.api import app
    page = TestClient(app, base_url="https://testserver").get(f"/p/{pay['token']}").text
    assert "IFSC" in page and "HDFC0001234" in page and "CLABE" not in page and "₹" in page
    got = PM.record_received(biz, "t", amount_minor=50000, description="Cash at the counter", contact_id=c["id"])
    assert (got["status"], got["provider"], got["currency"], got["account_id"]) == ("paid", "cash", "INR", None)


def test_record_received_by_api(client, mx):
    _, h = mx
    r = client.post("/v1/payments/received", headers=h, json={"amount_minor": 35000, "description": "Efectivo"})
    assert r.status_code == 201 and r.json()["status"] == "paid" and r.json()["provider"] == "cash"
    assert client.post("/v1/payments/received", headers=h, json={"amount_minor": 0, "description": "x"}).status_code == 422


# ---------------------------------------------------------------- privacy
def test_stop_in_many_languages_but_never_inside_a_sentence():
    for w in ("STOP", "Tigil", "berhenti", "बंद", "توقف", "退订", "配信停止", "hủy", "opt out", "Désabonner", "Stopp!"):
        assert C.is_stop(w), w
    for w in ("cancel my appointment", "stop by tomorrow?", "no", "tigil muna ako bukas"):
        assert not C.is_stop(w), w


def test_owner_downloads_everything_without_secrets(client, mx):
    biz, h = mx
    client.post("/v1/contacts", headers=h, json={"name": "Ana", "phone": "9991234567"})
    with db.tx() as conn:
        PM.connect(conn, biz, "t", "stripe", {"secret_key": "sk_live_SECRET", "webhook_secret": "whsec_SECRET"})
    r = client.get("/v1/export", headers=h)
    assert r.status_code == 200
    j = r.json()
    assert j["format"] == "valeria-one-export" and j["business"]["id"] == str(biz["id"])
    assert [c["name"] for c in j["contacts"]] == ["Ana"] and "messages" in j and "payments" in j
    raw = r.text
    assert "SECRET" not in raw and "password_hash" not in raw and "key_hash" not in raw and "stripe_customer_id" not in raw


def test_each_business_has_a_privacy_notice(client, mx):
    biz, h = mx
    with db.tx() as conn:
        conn.execute(db.businesses.update().where(db.businesses.c.id == biz["id"]).values(slug="salon-luz", site={"email": "hola@luz.mx"}))
    r = client.get("/privacy/salon-luz", headers={"Accept-Language": "es"})
    assert r.status_code == 200 and "Aviso de privacidad" in r.text and "hola@luz.mx" in r.text
    assert packs.country("MX").privacy_law.name in r.text and r.headers["x-robots-tag"] == "noindex"
    assert "Privacy notice" in client.get("/privacy/salon-luz", headers={"Accept-Language": "en"}).text
    assert client.get("/privacy/nobody-here").status_code == 404


# ---------------------------------------------------------------- the launch window
def test_launch_window_free_months_then_paused_until_it_pays(client, monkeypatch):
    monkeypatch.setenv("VO_BILLING", "on")
    monkeypatch.setenv("VO_LAUNCH_MONTHS", "12")
    h = client.get("/signup", headers={"Accept-Language": "en"}).text
    assert "free for 12 months, then US$39 a month" in h
    biz = _signup(client, "Launch Co", "PH", "launch@example.com")
    now = datetime.now(timezone.utc)
    assert biz["promo"] == "launch" and timedelta(days=360) < biz["trial_ends_at"] - now < timedelta(days=370)
    assert BL.effective(biz) == "starter" and BL.status(biz)["promo"] == "launch"
    with db.tx() as conn:                                     # the 12 months ran out, nothing paid
        conn.execute(db.businesses.update().where(db.businesses.c.id == biz["id"]).values(trial_ends_at=now - timedelta(hours=1)))
        key, _ = B.new_key(conn, biz["id"])
        c = C.create(conn, biz, "t", {"name": "Maria", "email": "m@x.ph"})
        biz = B.get(conn, biz["id"])
    assert BL.effective(biz) == "paused" and BL.paused(biz)
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/billing"
    assert client.get("/app/billing").status_code == 200 and "/app/export" in client.get("/app/billing").text
    assert client.get("/app/export").status_code == 200 and client.get("/app/settings").status_code == 200
    hk = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/business", headers=hk).status_code == 200 and client.get("/v1/export", headers=hk).status_code == 200
    r = client.get("/v1/contacts", headers=hk)
    assert r.status_code == 402 and r.json()["error"]["code"] == "payment_required"
    try:
        X.send(biz, "t", contact_id=c["id"], body="hola", channel="sandbox")
        raise AssertionError("sent while paused")
    except Exception as e:                                    # noqa: BLE001
        assert getattr(e, "reason", None) == "paused"
    monkeypatch.setenv("VO_AFTER_PROMO", "free")
    assert BL.effective(biz) == "free"
    monkeypatch.delenv("VO_AFTER_PROMO")
    with db.tx() as conn:                                     # paying opens everything at once
        conn.execute(db.businesses.update().where(db.businesses.c.id == biz["id"]).values(paid_until=now + timedelta(days=30)))
        biz = B.get(conn, biz["id"])
    assert BL.effective(biz) == "starter" and client.get("/app", follow_redirects=False).status_code == 200
    monkeypatch.delenv("VO_LAUNCH_MONTHS")                    # the window closed: new sign-ups get the 30-day trial
    with db.tx() as conn:
        b2 = B.create(conn, name="After launch", country="PH")
    assert b2["promo"] is None and b2["trial_ends_at"] - now < timedelta(days=31)


def test_launch_notices_30_days_ahead_then_paused(monkeypatch):
    monkeypatch.setenv("VO_BILLING", "on")
    monkeypatch.setenv("VO_LAUNCH_MONTHS", "12")
    from valeria_one.services import users
    with db.tx() as conn:
        _, biz = users.register(conn, email="owner-l@example.com", name="Owner", password=PW, business_name="Nairobi Cuts",
                                country="KE", industry="generic")
        biz = B.get(conn, biz["id"])
    assert biz["promo"] == "launch"
    mailer.OUTBOX.clear()
    now = datetime.now(timezone.utc)                          # real now: the notices are recorded at real time

    def ends(at):
        with db.tx() as conn:
            conn.execute(db.businesses.update().where(db.businesses.c.id == biz["id"]).values(trial_ends_at=at))
    ends(now + timedelta(days=20))                            # 20 days left: inside the 30-day notice
    assert BL.notices().get("launch_ending") == 1
    assert BL.notices().get("launch_ending") is None          # once
    ends(now - timedelta(hours=2))
    assert BL.notices().get("paused") == 1
    assert BL.notices().get("paused") is None
    mine = [m for m in mailer.OUTBOX if m["to"] == "owner-l@example.com"]
    assert [m["subject"] for m in mine] == ["Nairobi Cuts: your free launch months end in 30 days", "Nairobi Cuts is paused"]
    assert "nothing was deleted" in mine[1]["body"] and "/app/billing" in mine[1]["body"]


# ---------------------------------------------------------------- the first business from a new country
def test_first_business_from_a_new_country_alerts_the_admin_once():
    IE = db.instance_events
    with db.tx() as conn:
        conn.execute(db.users.insert().values(email="admin@server.test", name="Admin", password_hash="x", is_admin=True))
        before = conn.execute(IE.select().where(IE.c.type == "server.new_country")).all()
        B.create(conn, name="Pierogi", country="PL")
        B.create(conn, name="Pierogi 2", country="PL")              # the second one is not new
        B.create(conn, name="Demo PL", country="PL", sandbox=True)
        rows = conn.execute(IE.select().where(IE.c.type == "server.new_country")).mappings().all()
    new = [r for r in rows if r["data"]["country"] == "PL"]
    assert len(rows) - len(before) == 1 and len(new) == 1 and new[0]["data"]["auto_pack"] is True
    mailer.OUTBOX.clear()
    assert I.country_alerts()["sent"] >= 1
    pl = [m for m in mailer.OUTBOX if "Poland" in m["subject"]]
    assert len(pl) == 1 and pl[0]["to"] == "admin@server.test" and "Pierogi" in pl[0]["body"] and "safe defaults" in pl[0]["body"]
    assert I.country_alerts()["sent"] == 0                      # once
    with db.tx() as conn:
        summary = {c["country"]: c for c in I.countries_summary(conn)}
    assert summary["PL"]["n"] == 2 and summary["PL"]["auto"]


# ---------------------------------------------------------------- Viber
def test_viber_on_the_business_own_bot(client, mx):
    biz, h = mx
    r = client.post("/v1/channels", headers=h, json={"channel": "viber", "name": "Viber", "config": {"auth_token": "VTOK", "sender_name": "Salon Luz"}})
    assert r.status_code == 201, r.text
    path = r.json()["webhook_url"].replace("https://one.test", "")
    ev = json.dumps({"event": "message", "timestamp": 1761300000000, "message_token": 4912661846655238145,
                     "sender": {"id": "01234567890A=", "name": "Maria"}, "message": {"type": "text", "text": "Kumusta po"}}).encode()
    sig = hmac.new(b"VTOK", ev, hashlib.sha256).hexdigest()
    assert client.post(path, content=ev, headers={"X-Viber-Content-Signature": "bad"}).status_code == 403
    assert client.post(path, content=ev, headers={"X-Viber-Content-Signature": sig}).status_code == 200
    hello = json.dumps({"event": "webhook", "timestamp": 1, "message_token": 1}).encode()
    assert client.post(path, content=hello, headers={"X-Viber-Content-Signature": hmac.new(b"VTOK", hello, hashlib.sha256).hexdigest()}).status_code == 200
    cs = client.get("/v1/contacts", headers=h).json()
    assert [c["name"] for c in cs] == ["Maria"]
    seen = []

    def handler(req):
        seen.append((req.url.path, req.headers["x-viber-auth-token"], json.loads(req.content)))
        return httpx.Response(200, json={"status": 0, "status_message": "ok", "message_token": 555})
    m = X.send(biz, "t", contact_id=cs[0]["id"], body="Salamat!", channel="viber", http=_mock(handler))
    assert m["status"] == "sent" and m["provider_id"] == "555"
    path_, tok, body = seen[-1]
    assert path_ == "/pa/send_message" and tok == "VTOK" and body["receiver"] == "01234567890A=" and body["sender"]["name"] == "Salon Luz"
    assert body["type"] == "text" and body["text"] == "Salamat!"
    st = json.dumps({"event": "delivered", "timestamp": 2, "message_token": 555, "user_id": "01234567890A="}).encode()
    assert client.post(path, content=st, headers={"X-Viber-Content-Signature": hmac.new(b"VTOK", st, hashlib.sha256).hexdigest()}).status_code == 200


def test_viber_registers_its_own_webhook(client, mx, monkeypatch):
    from valeria_one import channels as CH
    from valeria_one.channels.viber import Viber
    _, h = mx
    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content or b"{}")))
        if req.headers["x-viber-auth-token"] == "BAD":
            return httpx.Response(200, json={"status": 2, "status_message": "invalidAuthToken"})
        return httpx.Response(200, json={"status": 0, "status_message": "ok"})
    monkeypatch.setenv("VO_REGISTER_WEBHOOKS", "1")
    monkeypatch.setattr(CH, "adapter", lambda ch, http=None: Viber(http=_mock(handler)) if ch == "viber" else CH._REGISTRY[ch]())
    r = client.post("/v1/channels", headers=h, json={"channel": "viber", "name": "V", "config": {"auth_token": "OK", "sender_name": "S"}})
    assert r.status_code == 201, r.text
    assert seen[-1][0] == "/pa/set_webhook" and seen[-1][1]["url"] == r.json()["webhook_url"]
    bad = client.post("/v1/channels", headers=h, json={"channel": "viber", "name": "V", "config": {"auth_token": "BAD", "sender_name": "S"}})
    assert bad.status_code == 422 and "invalidAuthToken" in bad.json()["error"]["message"]


# ---------------------------------------------------------------- follow-ups use whatever the business connected
def test_followups_can_use_a_channel_outside_the_country_list(client, mx):
    biz, h = mx
    from valeria_one.services import followups as F
    assert "telegram" not in packs.country("MX").channels
    j = client.post("/v1/channels", headers=h, json={"channel": "telegram", "name": "Bot", "config": {"bot_token": "BT", "secret": "S"}}).json()
    upd = {"update_id": 1, "message": {"message_id": 1, "date": 1761300000, "chat": {"id": 77}, "from": {"first_name": "Kenji"}, "text": "hi"}}
    client.post(j["webhook_url"].replace("https://one.test", ""), json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "S"})
    cid = client.get("/v1/contacts", headers=h).json()[0]["id"]
    from uuid import UUID
    with db.tx() as conn:
        assert str(F._pick_account(conn, B.get(conn, biz["id"]), UUID(cid))) == j["id"]


def test_public_pages_say_the_launch_months_while_the_window_is_open(client, monkeypatch):
    from test_selfhost import do_setup
    do_setup(client)
    client.cookies.clear()
    monkeypatch.setenv("VO_MARKETING", "on")
    monkeypatch.setenv("VO_BILLING", "on")
    pages = ("/", "/crm-for/hvac", "/mx", "/notes", "/llms.txt")
    for path in pages:
        h = client.get(path, headers={"Accept-Language": "en"}).text
        assert "30 days free" in h or "30 días gratis" in h, path
    monkeypatch.setenv("VO_LAUNCH_MONTHS", "12")
    for path in pages:
        h = client.get(path, headers={"Accept-Language": "en"}).text
        assert "12 months free" in h or "12 meses gratis" in h, path
        assert "30 days free" not in h and "30 días gratis" not in h, path



def test_local_prices_by_country_never_by_chosen_currency(client, monkeypatch):
    monkeypatch.setenv("VO_BILLING", "on")
    monkeypatch.setenv("VO_SELLER_COUNTRY", "MX")
    q = {c: BL.quote({"country": c, "currency": "USD"}, "starter") for c in ("PH", "IN", "MX", "US", "PL")}
    assert (q["PH"]["currency"], q["PH"]["amount_minor"]) == ("PHP", 79900)
    assert (q["IN"]["currency"], q["IN"]["amount_minor"]) == ("INR", 99900)
    assert (q["MX"]["currency"], q["MX"]["price_minor"], q["MX"]["tax_name"]) == ("MXN", 24900, "IVA")
    assert (q["US"]["currency"], q["US"]["amount_minor"]) == ("USD", 3900) and q["PL"]["currency"] == "USD"
    assert BL.quote({"country": "US", "currency": "PHP"}, "starter")["currency"] == "USD"      # no shopping for a cheaper price
    monkeypatch.setenv("VO_LAUNCH_MONTHS", "12")
    h = client.get("/signup", headers={"Accept-Language": "en-PH"}).text
    assert "free for 12 months, then ₱799 a month" in h and 'data-usd="US$39"' in h
    assert re.search(r'<option value="IN"[^>]*data-price="₹999"', h) and re.search(r'<option value="MX"[^>]*data-price="\$249 MXN \+ IVA"', h)
    assert not re.search(r'<option value="US"[^>]*data-price', h)
    hi = client.get("/signup", headers={"Accept-Language": "hi-IN"}).text          # in Hindi, with India's price
    assert 'lang="hi"' in hi and "12 महीने मुफ़्त, फिर ₹999 प्रति माह" in hi
