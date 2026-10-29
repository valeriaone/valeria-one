"""easy01: sign up with the accounts a business already has (Facebook / Google), the one Connect screen, the invite
link, the "— Valeria One" line under the AI's first answer, TikTok on the website."""
import base64
import json
import re

import httpx
import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.channels import Inbound, SendResult
from valeria_one.channels.webhook import Webhook
from valeria_one.services import Invalid
from valeria_one.services import ai as AIS
from valeria_one.services import inbox as X
from valeria_one.services import meta as MT
from valeria_one.services import site as ST
from valeria_one.services import social as SO

APP_SECRET = "PLATFORM-APP-SECRET"
PAGE = {"id": "1001", "name": "Sari Salon Manila", "access_token": "PAGE-TOKEN-1", "category": "Beauty Salon",
        "phone": "0917 123 4567", "emails": ["hello@sarisalon.ph"], "about": "Hair, nails and lashes in Makati.",
        "single_line_address": "123 Ayala Ave, Makati", "location": {"country": "Philippines"},
        "hours": {"mon_1_open": "09:00", "mon_1_close": "19:00", "sat_1_open": "10:00", "sat_1_close": "00:00"},
        "instagram_business_account": {"id": "17841400000000001", "username": "sarisalon"}}


def csrf(client, path):
    r = client.get(path)
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, (path, r.status_code, r.text[:300])
    return m.group(1)


@pytest.fixture
def fb(monkeypatch):
    monkeypatch.setenv("VO_META_APP_ID", "1234567890")
    monkeypatch.setenv("VO_META_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("VO_META_VERIFY_TOKEN", "PLATFORM-VT")
    monkeypatch.setenv("VO_META_WA_CONFIG_ID", "WA-CONFIG")
    monkeypatch.setenv("VO_META_LOGIN_CONFIG_ID", "LOGIN-CONFIG")
    g = FakeFacebook()
    monkeypatch.setattr(SO, "_http", lambda http=None: http or g.client())
    monkeypatch.setattr(MT, "_http", lambda http=None: http or g.client())
    return g


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setenv("VO_GOOGLE_CLIENT_ID", "GID.apps.googleusercontent.com")
    monkeypatch.setenv("VO_GOOGLE_CLIENT_SECRET", "GSECRET")
    g = FakeGoogle()
    monkeypatch.setattr(SO, "_http", lambda http=None: http or g.client())
    return g


class FakeFacebook:
    def __init__(self):
        self.calls, self.me, self.pages = [], {"id": "fb-555", "name": "Liza Cruz", "email": "liza@example.ph"}, [PAGE]

    def __call__(self, req: httpx.Request):
        p, q = req.url.path, dict(req.url.params)
        self.calls.append((req.method, p, q))
        if p.endswith("/oauth/access_token"):
            if q.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": "LONG-USER-TOKEN"})
            assert q["client_secret"] == APP_SECRET and q["redirect_uri"] == "https://one.test/auth/facebook/callback"
            return httpx.Response(200, json={"access_token": "SHORT"}) if q["code"] == "good" else \
                httpx.Response(400, json={"error": {"message": "bad code"}})
        if p.endswith("/me"):
            return httpx.Response(200, json=self.me)
        if p.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": self.pages})
        if p.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": {"message": f"unexpected {p}"}})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")   # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeGoogle:
    def __init__(self):
        self.claims = {"sub": "g-777", "aud": "GID.apps.googleusercontent.com", "email": "Ravi@Example.in",
                       "email_verified": True, "name": "Ravi Kumar"}

    def __call__(self, req: httpx.Request):
        assert req.url.host == "oauth2.googleapis.com"
        form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
        assert form["client_secret"] == "GSECRET" and form["grant_type"] == "authorization_code"
        return httpx.Response(200, json={"id_token": _jwt(self.claims), "access_token": "x"})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def _start(client, provider):
    r = client.get(f"/auth/{provider}", follow_redirects=False)
    assert r.status_code == 303, r.text[:200]
    return r.headers["location"], re.search(r"state=([\w-]+)", r.headers["location"]).group(1)


def _back(client, provider, state, code="good"):
    return client.get(f"/auth/{provider}/callback", params={"code": code, "state": state}, follow_redirects=False)


# ---------------------------------------------------------------- off until the server has the apps
def test_buttons_only_when_the_server_has_the_apps(client, monkeypatch):
    for path in ("/signup", "/login"):
        assert "/auth/facebook" not in client.get(path).text and "/auth/google" not in client.get(path).text
    assert client.get("/auth/facebook", follow_redirects=False).status_code == 404
    monkeypatch.setenv("VO_META_APP_ID", "1"); monkeypatch.setenv("VO_META_APP_SECRET", "s")     # noqa: E702
    monkeypatch.setenv("VO_GOOGLE_CLIENT_ID", "g"); monkeypatch.setenv("VO_GOOGLE_CLIENT_SECRET", "s")   # noqa: E702
    for path in ("/signup", "/login"):
        h = client.get(path).text
        assert 'href="/auth/facebook"' in h and 'href="/auth/google"' in h and "Continue with Facebook" in h
    assert "Continuar con Facebook" in client.get("/signup", headers={"Accept-Language": "es-MX"}).text
    monkeypatch.setenv("VO_SANDBOX", "1")                                                    # the sandbox: never real accounts
    assert SO.available() == [] and client.get("/auth/google", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------- Facebook: a new business, filled in from its Page
def test_facebook_signup_fills_the_business_from_its_page(client, fb):
    url, state = _start(client, "facebook")
    assert url.startswith("https://www.facebook.com/v23.0/dialog/oauth?") and "client_id=1234567890" in url
    assert "redirect_uri=https%3A%2F%2Fone.test%2Fauth%2Ffacebook%2Fcallback" in url and "config_id=LOGIN-CONFIG" in url
    r = _back(client, "facebook", state)
    assert r.status_code == 303 and r.headers["location"] == "/signup/finish"
    page = client.get("/signup/finish").text
    assert "One last step" in page and 'value="Sari Salon Manila"' in page and "Instagram @sarisalon" in page
    assert re.search(r'<option value="PH" selected', page)                               # country from the Page
    assert re.search(r'<option value="salon" selected', page)                            # type from the Page's category
    assert 'name="email"' not in page and "liza@example.ph" in page                       # Facebook gave the email
    assert "PAGE-TOKEN" not in page and "PAGE-TOKEN" not in str(client.cookies)            # tokens stay on the server
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/signup/finish", data={"csrf": tok, "business_name": "Sari Salon Manila", "country": "PH",
                                             "industry": "salon", "page": "1001"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/connect"
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select()).mappings().one())
        user = dict(conn.execute(db.users.select()).mappings().one())
        logins = conn.execute(db.user_logins.select()).mappings().all()
        accs = conn.execute(db.channel_accounts.select().order_by(db.channel_accounts.c.channel)).mappings().all()
    assert (biz["country"], biz["currency"], biz["industry"]) == ("PH", "PHP", "salon")
    assert user["email"] == "liza@example.ph" and user["name"] == "Liza Cruz"
    assert [(x["provider"], x["subject"]) for x in logins] == [("facebook", "fb-555")]
    s = biz["site"]
    assert s["phone"] == "+639171234567" and s["email"] == "hello@sarisalon.ph" and s["address"].startswith("123 Ayala")
    assert s["about"].startswith("Hair, nails") and s["instagram"] == "sarisalon" and s["facebook"].endswith("/1001")
    assert biz["hours"] == {"mon": [["09:00", "19:00"]], "sat": [["10:00", "23:59"]]}
    assert [(a["channel"], a["address"]) for a in accs] == [("instagram", "17841400000000001"), ("messenger", "1001")]
    subs = [c for c in fb.calls if c[1].endswith("/1001/subscribed_apps")]
    assert subs and subs[0][2]["access_token"] == "PAGE-TOKEN-1"
    h = client.get("/app/connect").text                                                   # lands on Connect, signed in
    assert "Connect your accounts" in h and "Sari Salon Manila" in h and "@sarisalon" in h
    # next time: straight in, no form
    client.post("/logout", data={"csrf": csrf(client, "/app")})
    _, state = _start(client, "facebook")
    r = _back(client, "facebook", state)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    with db.tx() as conn:
        assert conn.execute(db.businesses.select()).mappings().all().__len__() == 1


def test_facebook_without_pages_or_email(client, fb):
    fb.me, fb.pages = {"id": "fb-9", "name": "Kofi"}, []
    _, state = _start(client, "facebook")
    assert _back(client, "facebook", state).headers["location"] == "/signup/finish"
    page = client.get("/signup/finish", headers={"Accept-Language": "en-GH"}).text
    assert 'name="email"' in page and 'name="page"' not in page and re.search(r'<option value="GH" selected', page)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/signup/finish", data={"csrf": tok, "business_name": "Kofi Barbers", "country": "GH", "industry": "salon"},
                    headers={"Referer": "https://testserver/signup/finish"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/signup/finish")
    page = client.get("/signup/finish").text                                              # still held: try again with the email
    assert "enter your email" in page
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/signup/finish", data={"csrf": tok, "business_name": "Kofi Barbers", "country": "GH", "industry": "salon",
                                             "email": "kofi@example.com"}, follow_redirects=False)
    assert r.headers["location"] == "/app/connect"
    with db.tx() as conn:
        assert conn.execute(db.users.select()).mappings().one()["email"] == "kofi@example.com"
        assert not conn.execute(db.channel_accounts.select()).first()


def test_a_page_that_wont_connect_never_stops_the_signup(client, fb, monkeypatch):
    def boom(*a, **k):
        raise MT.MetaError("Meta: (#200) permissions error")
    monkeypatch.setattr(MT, "connect_pages", boom)
    _, state = _start(client, "facebook")
    _back(client, "facebook", state)
    tok = csrf(client, "/signup/finish")
    r = client.post("/signup/finish", data={"csrf": tok, "business_name": "Sari", "country": "PH", "industry": "salon",
                                             "page": "1001"}, follow_redirects=False)
    assert r.headers["location"] == "/app/connect"
    assert "connect this time" in client.get("/app/connect").text
    with db.tx() as conn:
        assert conn.execute(db.businesses.select()).mappings().one()["site"]["phone"] == "+639171234567"   # still filled in


def test_same_email_links_to_the_existing_account(client, fb):
    client.post("/signup", data={"csrf": csrf(client, "/signup"), "business_name": "Liza's", "country": "PH", "industry": "salon",
                                 "email": "LIZA@example.ph", "password": "una-clave-larga"})
    client.post("/logout", data={"csrf": csrf(client, "/app")})
    _, state = _start(client, "facebook")
    r = _back(client, "facebook", state)
    assert r.headers["location"] == "/app"                                                # signed in, no second business
    with db.tx() as conn:
        assert len(conn.execute(db.businesses.select()).all()) == 1
        assert conn.execute(db.user_logins.select()).mappings().one()["provider"] == "facebook"


def test_bad_state_or_code_or_cancel(client, fb):
    _, state = _start(client, "facebook")
    assert _back(client, "facebook", "forged").headers["location"] == "/login"            # someone else's link
    _, state = _start(client, "facebook")
    assert _back(client, "facebook", state, code="bad").headers["location"] == "/login"  # Facebook refused the code
    assert "sign-in didn&#39;t finish" in client.get("/login").text
    _, state = _start(client, "facebook")
    r = client.get("/auth/facebook/callback", params={"error": "access_denied", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert client.get("/signup/finish", follow_redirects=False).headers["location"] == "/signup"   # nothing held
    _, state = _start(client, "facebook")
    assert _back(client, "facebook", state).headers["location"] == "/signup/finish"
    tok = csrf(client, "/signup/finish")
    client.post("/signup/finish", data={"csrf": tok, "business_name": "Sari", "country": "PH", "industry": "salon"})
    r = client.post("/signup/finish", data={"csrf": tok, "business_name": "Sari 2", "country": "PH", "industry": "salon"},
                    follow_redirects=False)
    assert r.headers["location"] in ("/signup", "/app")                                   # used once
    with db.tx() as conn:
        assert len(conn.execute(db.businesses.select()).all()) == 1


def test_closed_signups_still_let_members_in(client, fb, monkeypatch):
    monkeypatch.setenv("VO_SIGNUP", "closed")
    _, state = _start(client, "facebook")
    assert _back(client, "facebook", state).headers["location"] == "/login"               # nobody new
    assert "Sign-ups are closed" in client.get("/login").text
    monkeypatch.setenv("VO_SIGNUP", "open")
    client.post("/signup", data={"csrf": csrf(client, "/signup"), "business_name": "Liza's", "country": "PH", "industry": "salon",
                                 "email": "liza@example.ph", "password": "una-clave-larga"})
    client.post("/logout", data={"csrf": csrf(client, "/app")})
    monkeypatch.setenv("VO_SIGNUP", "closed")
    _, state = _start(client, "facebook")
    assert _back(client, "facebook", state).headers["location"] == "/app"                  # an existing member: in
    with db.tx() as conn:
        conn.execute(db.businesses.update().values(suspended_at=db.func.now()))
    client.cookies.clear()
    _, state = _start(client, "facebook")
    assert _back(client, "facebook", state).headers["location"] == "/login"               # paused by the host


# ---------------------------------------------------------------- Google: sign-in only
def test_google_signup_and_sign_in(client, google):
    url, state = _start(client, "google")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?") and "scope=openid+email+profile" in url
    assert _back(client, "google", state).headers["location"] == "/signup/finish"
    page = client.get("/signup/finish", headers={"Accept-Language": "en-IN"}).text
    assert "ravi@example.in" in page and 'name="email"' not in page and re.search(r'<option value="IN" selected', page)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/signup/finish", data={"csrf": tok, "business_name": "Ravi Clinic", "country": "IN", "industry": "generic"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/connect"
    with db.tx() as conn:
        assert conn.execute(db.businesses.select()).mappings().one()["currency"] == "INR"
    client.post("/logout", data={"csrf": csrf(client, "/app")})
    _, state = _start(client, "google")
    assert _back(client, "google", state).headers["location"] == "/app"


def test_google_token_for_another_app_or_unverified_email(client, google):
    google.claims["aud"] = "someone-else"
    _, state = _start(client, "google")
    assert _back(client, "google", state).headers["location"] == "/login"
    google.claims.update(aud="GID.apps.googleusercontent.com", email_verified=False)
    _, state = _start(client, "google")
    assert _back(client, "google", state).headers["location"] == "/signup/finish"
    assert 'name="email"' in client.get("/signup/finish").text                            # unverified: asked, not trusted


def test_the_password_nobody_knows_can_be_reset(client, google):
    _, state = _start(client, "google")
    _back(client, "google", state)
    tok = csrf(client, "/signup/finish")
    client.post("/signup/finish", data={"csrf": tok, "business_name": "Ravi Clinic", "country": "IN", "industry": "generic"})
    client.post("/logout", data={"csrf": csrf(client, "/app")})
    r = client.post("/login", data={"csrf": csrf(client, "/login"), "email": "ravi@example.in", "password": ""},
                    follow_redirects=False)
    assert r.headers["location"] == "/login"
    from valeria_one.services import mailer
    client.post("/forgot", data={"csrf": csrf(client, "/forgot"), "email": "ravi@example.in"})
    assert mailer.OUTBOX and "/reset/" in mailer.OUTBOX[-1]["body"]


# ---------------------------------------------------------------- the small pieces
def test_facebook_dialog_by_configuration_or_scope(monkeypatch):
    monkeypatch.setenv("VO_META_APP_ID", "1"); monkeypatch.setenv("VO_META_APP_SECRET", "s")     # noqa: E702
    assert "scope=email%2Cpublic_profile%2Cpages_show_list" in SO.auth_url("facebook", "st")     # a consumer app
    monkeypatch.setenv("VO_META_LOGIN_CONFIG_ID", "PAGES-CFG")
    assert "config_id=PAGES-CFG" in SO.auth_url("facebook", "st") and "scope=" not in SO.auth_url("facebook", "st")
    monkeypatch.setenv("VO_META_SIGNUP_CONFIG_ID", "SIGNUP-CFG")
    assert "config_id=SIGNUP-CFG" in SO.auth_url("facebook", "st")


def test_hours_and_country_helpers():
    assert SO.fb_hours({"mon_1_open": "09:00", "mon_1_close": "13:00", "mon_2_open": "15:00", "mon_2_close": "19:00",
                        "sun_1_open": "x"}) == {"mon": [["09:00", "13:00"], ["15:00", "19:00"]]}
    assert SO.country_code("Philippines") == "PH" and SO.country_code("india") == "IN" and SO.country_code("ke") == "KE"
    assert SO.country_code("Cuba") == "" and SO.country_code("Atlantis") == "" and SO.country_code("") == ""
    assert [SO.industry_guess(c) for c in ("Beauty Salon", "Dentist & Dental Office", "Gym/Physical Fitness Center", "Coffee Shop",
                                           "Real Estate Agent", "Plumbing Service", "Lawyer & Law Firm", "Clothing Store", "Musician", "")] == \
        ["salon", "clinic", "fitness", "restaurant", "realtor", "contractor", "professional", "shop", "generic", "generic"]


def test_held_sign_ups_expire_and_never_leak(fb):
    with db.tx() as conn:
        key = SO.hold(conn, {"provider": "facebook", "subject": "1", "pages": [{"token": "SECRET-T"}]})
        raw = conn.execute(db.instance_kv.select().where(db.instance_kv.c.key == key)).mappings().one()["value"]
        assert "SECRET-T" not in json.dumps(raw)                                         # encrypted at rest
        assert SO.held(conn, key)["subject"] == "1"
        assert SO.held(conn, key, take=True) and SO.held(conn, key) is None              # once
        assert SO.held(conn, "instance:other") is None


# ---------------------------------------------------------------- Connect
def _owner(client, country="PH"):
    client.post("/signup", data={"csrf": csrf(client, "/signup"), "business_name": "Sari Salon", "country": country,
                                 "industry": "salon", "email": "o@example.com", "password": "una-clave-larga"})
    with db.tx() as conn:
        return dict(conn.execute(db.businesses.select()).mappings().all()[-1])


def test_connect_lists_every_account_and_what_is_live(client):
    biz = _owner(client)
    h = client.get("/app/connect").text
    for row in ("WhatsApp", "Facebook + Instagram", "Email", "SMS, Telegram, Viber", "Payments", "Calendar", "Website", "TikTok",
                "Google Business Profile", "Directory"):
        assert f"<b>{row}</b>" in h, row
    assert ">0/10<" in h and 'href="/app/connect" class="on"' in h and "data-meta=" not in h   # Meta off: forms only
    assert 'href="/app/channels?c=email#manual"' in h
    with db.tx() as conn:
        X.connect(conn, biz, "t", channel="webhook", name="Gateway", cfg={"url": "https://g.example/x", "secret": "s"})
        ST.save(conn, biz, "t", tiktok="https://www.tiktok.com/@sari.salon?lang=en")
    h = client.get("/app/connect").text
    assert ">2/10<" in h and "@sari.salon" in h and "Gateway" in h
    c = client.get("/app/channels?c=email").text
    assert '<option value="email" selected' in c and 'data-smtp="smtp.gmail.com"' in c


def test_connect_one_tap_buttons_when_meta_is_on(client, fb):
    _owner(client)
    h = client.get("/app/connect").text
    assert h.count('data-meta="wa"') == 1 and h.count('data-meta="pages"') == 1 and "/static/meta.js" in h
    assert h.count('name="back" value="/app/connect"') == 2


def test_one_tap_returns_to_connect(client, fb, monkeypatch):
    _owner(client)
    monkeypatch.setattr(MT, "connect_whatsapp", lambda *a, **k: {})
    tok = csrf(client, "/app/connect")
    r = client.post("/app/channels/meta/whatsapp", data={"csrf": tok, "code": "c", "waba_id": "1", "phone_number_id": "2",
                                                          "back": "/app/connect"}, follow_redirects=False)
    assert r.headers["location"] == "/app/connect"
    r = client.post("/app/channels/meta/whatsapp", data={"csrf": tok, "code": "c", "waba_id": "1", "phone_number_id": "2",
                                                          "back": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/app/channels"                                       # only our own pages


def test_invite_link_and_short_link(client, monkeypatch):
    monkeypatch.setenv("VO_BILLING", "on")
    biz = _owner(client)
    h = client.get("/app/connect").text
    link = f"https://one.test/i/{biz['billing_ref']}"
    assert link in h and "https://wa.me/?text=" in h and "It%27s%20free%3A%20https%3A//one.test/i/" in h
    r = client.get(f"/i/{biz['billing_ref']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/signup?ref={biz['billing_ref']}"
    assert client.get("/i/%3Cscript%3E", follow_redirects=False).headers["location"] == "/signup?ref=SCRIPT"


def test_staff_never_see_connect(client):
    _owner(client)
    with db.tx() as conn:
        db_user = conn.execute(db.memberships.select()).mappings().one()
        conn.execute(db.memberships.update().where(db.memberships.c.user_id == db_user["user_id"]).values(role="staff"))
    assert client.get("/app/connect", follow_redirects=False).headers["location"] == "/app"
    assert 'href="/app/connect"' not in client.get("/app").text


# ---------------------------------------------------------------- "— Valeria One" under the AI's first answer
def _ai_biz(monkeypatch, sent):
    monkeypatch.setenv("VO_BILLING", "on")
    biz, _ = make_business("PH", "salon")
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="webhook", name="GW", cfg={"url": "https://g.example/x", "secret": "s"})
        AIS.save(conn, biz, "t", provider="gemini", api_key="AIza-KEY", mode="auto")
        acc = conn.execute(db.channel_accounts.select().where(db.channel_accounts.c.id == acc["id"])).mappings().one()
    monkeypatch.setattr(AIS, "draft", lambda b, cid, http=None: ("Hi! A haircut is ₱350.", False))
    monkeypatch.setattr(Webhook, "send", lambda self, cfg, a, out: (sent.append(out.body), SendResult("sent"))[1])
    return biz, acc


def _say(acc, frm, body):
    st = X.receive_items(acc, [Inbound(address=frm, body=body)])
    return AIS.auto_reply(acc["business_id"], st["conversation_ids"][0])


def test_first_ai_answer_carries_the_line(monkeypatch):
    sent = []
    biz, acc = _ai_biz(monkeypatch, sent)
    assert _say(acc, "c1", "How much?") == "replied" and _say(acc, "c1", "And nails?") == "replied"
    line = f"— Valeria One · https://one.test/i/{biz['billing_ref']}"
    assert sent[0] == f"Hi! A haircut is ₱350.\n\n{line}" and sent[1] == "Hi! A haircut is ₱350."   # once per conversation
    _say(acc, "c2", "Hello")
    assert sent[2].endswith(line)                                                        # a new person sees it once too
    with db.tx() as conn:
        from valeria_one.services import businesses as B
        B.set_powered_tag(conn, biz["id"], "t", False)
    _say(acc, "c3", "Hi")
    assert sent[3] == "Hi! A haircut is ₱350."                                           # switched off


def test_no_line_without_billing_or_with_badge_off(monkeypatch):
    sent = []
    biz, acc = _ai_biz(monkeypatch, sent)
    monkeypatch.setenv("VO_BADGE", "off")
    _say(acc, "c1", "Hi")
    monkeypatch.delenv("VO_BADGE")
    monkeypatch.setenv("VO_BILLING", "off")
    _say(acc, "c2", "Hi")
    assert sent == ["Hi! A haircut is ₱350."] * 2
    assert AIS.powered_line({**biz, "sandbox": True}) is None


def test_owner_switches_the_line_in_settings(client, monkeypatch):
    monkeypatch.setenv("VO_BILLING", "on")
    biz = _owner(client)
    page = client.get("/app/settings").text
    assert 'name="powered_tag" value="1" checked' in page
    client.post("/app/ai", data={"csrf": csrf(client, "/app/settings"), "mode": "off", "provider": "gemini", "powered_shown": "1"})
    with db.tx() as conn:
        assert conn.execute(db.businesses.select()).mappings().one()["powered_tag"] is False
    client.post("/app/ai", data={"csrf": csrf(client, "/app/settings"), "mode": "off", "provider": "gemini"})   # not on the form
    with db.tx() as conn:
        assert conn.execute(db.businesses.select()).mappings().one()["powered_tag"] is False
        assert [e["data"] for e in conn.execute(db.events.select().where(db.events.c.type == "business.powered_tag")).mappings()] == [{"on": False}]
    assert 'name="powered_tag" value="1" checked' not in client.get("/app/settings").text


# ---------------------------------------------------------------- TikTok on the website
def test_tiktok_on_the_website(client):
    biz = _owner(client)
    with db.tx() as conn:
        for raw in ("@sari.salon", "https://www.tiktok.com/@sari.salon", "sari.salon"):
            assert ST.save(conn, biz, "t", tiktok=raw)["site"]["tiktok"] == "sari.salon"
        with pytest.raises(Invalid):
            ST.save(conn, biz, "t", tiktok="no spaces allowed")
        biz = ST.save(conn, biz, "t", enabled=True, tiktok="sari.salon")
        assert "https://www.tiktok.com/@sari.salon" in ST.json_ld(biz, [], "u")["sameAs"]
    assert 'href="https://www.tiktok.com/@sari.salon"' in client.get(f"/s/{biz['slug']}").text
    assert 'name="tiktok" value="sari.salon"' in client.get("/app/website").text
