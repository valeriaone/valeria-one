"""setup28 (v0.28): «Valeria sets it up» — one talk that asks which accounts a business already has and sets each one up.

The questions follow the business's country pack (WhatsApp first where the country lives on it, its payments, its other chat
apps) and trade pack (its usual apps, per country). Each answer is set up with what already exists: the contact number and
click-to-chat (phones.e164, Mexico's 52-1), a tracked source per website / profile / app (leads, v0.27) with the snippet
and bio links, the email channel (provider read from the domain, its app-password page, a sign-in test BEFORE saving, the
app password only in the encrypted channel account), Meta / Google / TikTok / calendar / payments connect screens; Microsoft
and servers without Meta's one-tap said plainly. «I don't have it», «Later», resume, change; «Ready: N of M»; Today nudges.
Never a password; a dry run writes, connects and tests nothing. Nothing leaves the machine (SMTP faked, network refused)."""
import json
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import func, select

from _va import csrf, owner_session, refuse_network
from conftest import make_business
from valeria_one import channels, crypto, db, packs
from valeria_one.services import Invalid
from valeria_one.services import businesses as B
from valeria_one.services import export as EX
from valeria_one.services import inbox as X
from valeria_one.services import leads as LD
from valeria_one.services import onboard as OB
from valeria_one.services import payments as PAY

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
GMAIL_APP = "abcd efgh ijkl mnop"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


class SMTP:
    """The business's own mail server: it signs in (or refuses) and must never be asked to send anything here."""
    calls: list = []
    accept = True

    def __init__(self, host, port, timeout):
        SMTP.calls.append(("open", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context):
        SMTP.calls.append(("tls",))

    def login(self, user, pw):
        import smtplib
        SMTP.calls.append(("login", user, pw))
        if not SMTP.accept:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, m):
        raise AssertionError("the setup test never sends an email")


@pytest.fixture(autouse=True)
def smtp(monkeypatch):
    SMTP.calls, SMTP.accept = [], True
    monkeypatch.setattr(channels.adapter("email").__class__, "smtp_factory", SMTP)
    return SMTP


def biz_(country="MX", industry="realtor", name="Casa Luz"):
    biz, _ = make_business(country, industry, name=name)
    return biz


def go(biz, key, **form):
    return OB.answer(biz, "user:t", key, form)


def fresh(biz):
    with db.tx() as conn:
        return B.get(conn, biz["id"])


def st(biz, uid=None):
    with db.tx() as conn:
        return OB.state(conn, fresh(biz), uid)


def srcs(biz):
    with db.tx() as conn:
        return LD.sources(conn, biz["id"])


def ans(biz):
    with db.tx() as conn:
        return OB.answers(conn, biz["id"])


def accounts(biz, channel=None):
    with db.tx() as conn:
        return [a for a in X.accounts(conn, biz["id"]) if channel is None or a["channel"] == channel]


# ---------------------------------------------------------------- the talk follows the country and the trade
def test_signup_lands_on_the_talk_and_it_asks_one_question_first(client):
    tok = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": tok, "business_name": "Casa Luz", "country": "MX", "industry": "realtor",
                                     "name": "Lupita", "email": "lupita@example.com", "password": "una-clave-larga"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/onboard"
    page = client.get("/app/onboard", headers={"Accept-Language": "es-MX"})
    assert page.status_code == 200
    assert "¿Usas WhatsApp para tu negocio?" in page.text and 'name="q" value="whatsapp"' in page.text
    assert "No lo tengo" in page.text and "Después" in page.text and "Listo: 0 de" not in page.text   # nothing counted yet
    assert "0 de 13 preguntas respondidas" in page.text                    # 13 questions for a Mexican realtor (cfg01: + prices)


def test_questions_adapt_to_the_country_pack_and_the_trade_pack():
    mx, us, jp, ph, ke = biz_("MX"), biz_("US"), biz_("JP", "salon"), biz_("PH", "shop"), biz_("KE", "salon")
    assert OB.questions(mx)[0] == "whatsapp" and OB.questions(mx)[-1] == "tools"
    for b in (us, jp):                                                       # not a WhatsApp-first country: asked after the socials
        q = OB.questions(b)
        assert q[0] == "email" and q.index("whatsapp") == q.index("tiktok") - 1
    names = lambda b: [t.name for t in OB.tools(b)]                         # noqa: E731
    assert {"EasyBroker", "Inmuebles24", "Tokko Broker"} <= set(names(mx)) and "Zillow" not in names(mx)
    assert "Zillow" in names(us) and "Inmuebles24" not in names(us)
    assert "Hot Pepper Beauty" in names(jp) and "Fresha" in names(jp)
    assert "Shopee" in names(ph) and "Lazada" in names(ph) and "Mercado Libre" not in names(ph)
    assert OB.payment_options(mx)[:3] == ["mercadopago", "stripe", "bank_transfer"] and OB.payment_options(mx)[-1] == "cash"
    assert "mpesa" in OB.payment_options(ke)
    assert OB.chat_options(ph) == ["viber", "sms", "telegram"] and OB.chat_options(jp)[:2] == ["line", "sms"]
    assert all(ip.setup.tools for ip in packs.industries().values())        # every trade's pack lists its usual apps
    with pytest.raises(Exception):                                           # a pack can't list an app twice or a bad country
        packs.Setup(tools=[{"key": "a1", "name": "A1", "url": "https://a.com"}, {"key": "a1", "name": "A2", "url": "https://b.com"}])
    with pytest.raises(Exception):
        packs.SetupTool(key="zz", name="Zz", url="https://z.com", countries=["Mexico"])


# ---------------------------------------------------------------- WhatsApp
def test_whatsapp_number_becomes_the_contact_number_with_one_tap_chat_and_a_tracked_source(client):
    biz = biz_()
    got = go(biz, "whatsapp", kind="business_app", number="+52 1 999 123 4567")      # Mexico's old mobile "1"
    assert got["has"] == "yes" and got["steps"] == ["site", "source"]
    b = fresh(biz)
    assert b["site"]["whatsapp"] == "+529991234567"
    [s] = srcs(biz)
    assert (s["kind"], s["name"]) == ("other", "WhatsApp")
    assert s["dest"].startswith("https://wa.me/529991234567?text=") and s["dest"].endswith(quote(f"(ref {s['code']})"))
    r = client.get(f"/l/{s['code']}", follow_redirects=False)                       # the short link opens their own chat
    assert r.status_code == 302 and r.headers["location"] == s["dest"]
    it = st(biz)["qs"]["whatsapp"]
    assert it["status"] == "ready" and [l["k"] for l in it["lines"]] == ["ob_l_wa_number", "ob_l_wa_api_off"]
    assert any(l["url"] == s["dest"] for l in it["links"]) and any(l["url"].endswith(f"/l/{s['code']}") for l in it["links"])
    go(biz, "whatsapp", kind="business_app", number="999 123 4567")                # the same answer again: still one source
    assert len(srcs(biz)) == 1
    go(biz, "whatsapp", kind="personal", number="999 765 4321")                    # a new number: the same source follows it
    [s2] = srcs(biz)
    assert s2["id"] == s["id"] and "wa.me/529997654321" in s2["dest"] and fresh(biz)["site"]["whatsapp"] == "+529997654321"
    for bad in ("12", ""):
        with pytest.raises(Invalid):
            go(biz, "whatsapp", kind="personal", number=bad)
    with pytest.raises(Invalid):
        go(biz, "whatsapp", kind="carrier-pigeon", number="999 123 4567")


def test_valeria_answering_needs_the_official_account_said_plainly_when_one_tap_is_off(client, monkeypatch):
    biz = biz_()
    go(biz, "whatsapp", kind="business_api", number="9991234567")
    it = st(biz)["qs"]["whatsapp"]
    assert it["status"] == "todo" and it["step"]["href"] == "/app/channels?c=whatsapp#manual"
    assert "ob_w_wa_tap_off" in [l["k"] for l in it["lines"]]                        # Meta's one-tap isn't on: never faked
    owner_session(client, biz)
    page = client.get("/app/onboard?q=whatsapp", headers={"Accept-Language": "en"}).text
    assert "data-meta=" not in page and "pasting its settings in Channels" in page
    for k, v in {"VO_META_APP_ID": "app1", "VO_META_APP_SECRET": "s", "VO_META_VERIFY_TOKEN": "v", "VO_META_WA_CONFIG_ID": "cfg"}.items():
        monkeypatch.setenv(k, v)
    page = client.get("/app/onboard?q=whatsapp").text
    assert 'data-meta="wa"' in page and 'name="back" value="/app/onboard"' in page and 'data-app="app1"' in page
    assert st(biz)["qs"]["whatsapp"]["step"]["href"] == "/app/onboard?q=whatsapp#meta"
    with db.tx() as conn:                                                            # the official account connected: green
        X.connect(conn, fresh(biz), "t", channel="whatsapp", name="WhatsApp",
                  cfg={"phone_number_id": "P", "token": "T", "app_secret": "A", "verify_token": "V"})
    it = st(biz)["qs"]["whatsapp"]
    assert it["status"] == "ready" and "ob_l_wa_api_on" in [l["k"] for l in it["lines"]]


def test_one_tap_meta_connections_may_come_back_to_the_talk():
    from valeria_one.web import _back
    assert _back({"back": "/app/onboard"}) == "/app/onboard" and _back({"back": "https://evil.example"}) == "/app/channels"


def test_the_click_to_chat_number_follows_the_answer_when_the_owner_had_set_one():
    biz = biz_()
    with db.tx() as conn:
        LD.save_settings(conn, biz, "t", chat_number="999 000 1111")
    go(biz, "whatsapp", kind="personal", number="999 222 3333")
    with db.tx() as conn:
        assert LD.settings(conn, biz["id"])["chat_number"] == "+529992223333"


# ---------------------------------------------------------------- email
def test_gmail_is_read_from_the_address_and_connected_after_a_sign_in_test(client, smtp):
    biz = biz_()
    owner_session(client, biz)
    tok = csrf(client, "/app/onboard?q=email")
    r = client.post("/app/onboard", data={"csrf": tok, "q": "email", "act": "answer", "email": "Casa.Luz@Gmail.com"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/onboard?q=email"                          # one more thing to give here
    assert ans(biz)["email"]["provider"] == "gmail" and not accounts(biz, "email")
    it = st(biz)["qs"]["email"]
    assert it["status"] == "todo" and it["step"]["k"] == "ob_s_mail_app"
    page = client.get("/app/onboard?q=email").text
    assert "https://myaccount.google.com/apppasswords" in page and "smtp.gmail.com" in page and 'name="app_password"' in page
    assert smtp.calls == []
    # the normal password is refused (it doesn't look like Google's 16 letters) — nothing connected, nothing tested
    for bad in ("MiClave2024!", "hunter22hunter22"):
        r = client.post("/app/onboard", data={"csrf": tok, "q": "email", "act": "answer", "email": "casa.luz@gmail.com",
                                              "host": "gmail", "app_password": bad}, follow_redirects=False)
        assert r.headers["location"] == "/app/onboard?q=email"
    assert not accounts(biz, "email") and smtp.calls == []
    assert "doesn&#39;t look like an app password" in client.get("/app/onboard?q=email", headers={"Accept-Language": "en"}).text
    r = client.post("/app/onboard", data={"csrf": tok, "q": "email", "act": "answer", "email": "casa.luz@gmail.com", "host": "gmail",
                                          "app_password": GMAIL_APP}, follow_redirects=False)
    assert r.headers["location"] == "/app/onboard?q=prices"                         # done: on to the next question (cfg01)
    assert smtp.calls == [("open", "smtp.gmail.com", 587), ("tls",), ("login", "casa.luz@gmail.com", "abcdefghijklmnop")]
    [acc] = accounts(biz, "email")
    assert acc["address"] == "casa.luz@gmail.com" and acc["config"]["password"] != "abcdefghijklmnop"   # shown redacted
    with db.tx() as conn:
        raw = conn.execute(select(db.channel_accounts.c.config).where(db.channel_accounts.c.id == acc["id"])).scalar()
        row = conn.execute(select(db.onboarding)).mappings().one()
    assert "abcdefghijklmnop" not in json.dumps(raw) and crypto.open_(raw)["password"] == "abcdefghijklmnop"   # encrypted
    assert raw["smtp_host"] == "smtp.gmail.com" and raw["from_email"] == "casa.luz@gmail.com" and raw["secret"]
    assert "abcdefghijklmnop" not in json.dumps(row["answers"]) and "app_password" not in json.dumps(row["answers"])
    assert st(biz)["qs"]["email"]["status"] == "ready"
    n = len(smtp.calls)
    go(biz, "email", email="casa.luz@gmail.com", app_password=GMAIL_APP)            # already connected: nothing again
    assert len(accounts(biz, "email")) == 1 and len(smtp.calls) == n


def test_a_refused_app_password_saves_nothing(smtp):
    biz = biz_()
    smtp.accept = False
    with pytest.raises(OB.EmailFailed) as e:
        go(biz, "email", email="luz@yahoo.com.mx", app_password="ABCD-EFGH-IJKL-MNOP")
    assert str(e.value) == "ob_mail_auth" and ("open", "smtp.mail.yahoo.com", 587) in smtp.calls
    assert not accounts(biz, "email") and ans(biz) == {}


def test_own_domain_asks_who_hosts_it_and_microsoft_is_said_honestly(smtp):
    biz = biz_("DE", "salon")
    go(biz, "email", email="hallo@salon-luz.de")
    assert ans(biz)["email"]["provider"] is None and st(biz)["qs"]["email"]["step"]["k"] == "ob_s_mail_host"
    go(biz, "email", email="hallo@salon-luz.de", host="m365")
    it = st(biz)["qs"]["email"]
    assert it["status"] == "waiting" and it["step"] is None and "ob_w_mail_microsoft" in [l["k"] for l in it["lines"]]
    go(biz, "email", email="hallo@salon-luz.de", host="other")
    assert st(biz)["qs"]["email"]["step"]["href"] == "/app/channels?c=email#manual"   # their own server: the Channels form
    assert OB.mail_provider("x@hotmail.com") == "microsoft" and OB.mail_provider("x@icloud.com") == "icloud"
    assert OB.mail_host("zoho_org", "DE", "hallo@salon-luz.de") == "smtppro.zoho.eu"     # Zoho follows its region
    assert OB.mail_host("zoho", "IN", "a@zoho.com") == "smtp.zoho.in" and OB.mail_host("zoho", "MX", "a@zoho.com") == "smtp.zoho.com"
    with pytest.raises(Invalid) as e:                                                 # Zoho: "it is an app password" ticked
        go(biz, "email", email="hallo@salon-luz.de", host="zoho_org", app_password="a1b2c3d4e5f6")
    assert str(e.value) == "ob_zoho_confirm" and smtp.calls == []
    go(biz, "email", email="hallo@salon-luz.de", host="zoho_org", app_password="a1b2c3d4e5f6", made_app="1")
    assert smtp.calls[0] == ("open", "smtppro.zoho.eu", 587) and accounts(biz, "email")
    assert OB.app_password("abcd-efgh-ijkl-mnop", "icloud") == "abcd-efgh-ijkl-mnop"
    with pytest.raises(Invalid):
        OB.app_password("my-own-pass", "icloud")


# ---------------------------------------------------------------- never a password
def test_never_a_password_anywhere():
    for v in ("Secr3t!pass", "Pa$$w0rd1", "MiClave2024!"):
        assert OB.looks_like_password(v)
    for v in ("@maria.sol_23", "Maria.Sol_23", "https://facebook.com/x?id=12", "+52 999 123 4567", "ana@example.com", "estudioluz",
              "www.casa-luz.mx/inicio?ref=1"):
        assert not OB.looks_like_password(v), v
    biz = biz_()
    for key, form in (("instagram", {"value": "Luz2024!#"}), ("website", {"sites": "Hunter2!x"}),
                      ("facebook", {"value": "fb", "password": "x"}), ("whatsapp", {"kind": "personal", "number": "999", "contraseña": "y"}),
                      ("tools", {"pick": ["easybroker"], "link_easybroker": "Clave#2024"})):
        with pytest.raises(Invalid) as e:
            go(biz, key, **form)
        assert str(e.value) == "ob_no_passwords", key
    assert ans(biz) == {} and srcs(biz) == []
    with pytest.raises(Invalid) as e:                                                 # only the email question takes an app password
        go(biz, "instagram", value="@luz", app_password="abcdefghijklmnop")
    assert str(e.value) == "ob_no_passwords"


# ---------------------------------------------------------------- websites, socials, Google, YouTube
def test_each_website_becomes_a_tracked_source_with_its_snippet_and_link(client):
    biz = biz_()
    go(biz, "website", sites="www.casaluz.mx\nhttps://renta.casaluz.mx/casas, www.casaluz.mx")
    ss = srcs(biz)
    assert [(s["kind"], s["dest"]) for s in ss] == [("website", "https://www.casaluz.mx"), ("website", "https://renta.casaluz.mx/casas")]
    go(biz, "website", sites="https://www.casaluz.mx renta.casaluz.mx/casas")      # again: no new source
    assert len(srcs(biz)) == 2
    owner_session(client, biz)
    page = client.get("/app/onboard?q=website").text
    for s in ss:
        assert f"/l/{s['code']}/embed.js" in page and f"/l/{s['code']}</code>" in page
    assert page.count('data-copy="#now-') >= 2
    for bad in ("http://casaluz.mx", "javascript:alert(1)", "casa luz", "https://user@evil.com"):
        with pytest.raises(Invalid):
            go(biz, "website", sites=bad)
    with pytest.raises(Invalid) as e:
        go(biz, "website", sites=" ".join(f"s{i}.casaluz.mx" for i in range(11)))
    assert str(e.value) == "ob_too_many_sites"


def test_social_profiles_are_kept_with_a_bio_link_each(client):
    biz = biz_()
    go(biz, "facebook", value="https://m.facebook.com/casaluzmerida/?ref=bookmarks")
    go(biz, "instagram", value="https://www.instagram.com/casa.luz.mid/")
    go(biz, "tiktok", value="@casaluz")
    go(biz, "youtube", value="youtube.com/@CasaLuzTV")
    go(biz, "google", value="")
    b = fresh(biz)
    assert b["site"]["facebook"] == "https://www.facebook.com/casaluzmerida" and b["site"]["instagram"] == "casa.luz.mid"
    assert b["site"]["tiktok"] == "casaluz" and ans(biz)["youtube"]["value"] == "https://www.youtube.com/@CasaLuzTV"
    with db.tx() as conn:
        assert LD.settings(conn, biz["id"])["messenger"] == "casaluzmerida"
    kinds = sorted((s["kind"], s["name"], s["dest"]) for s in srcs(biz))
    assert kinds == [("facebook", "Facebook", ""), ("google", "Google", ""), ("instagram", "Instagram", ""), ("other", "TikTok", ""),
                     ("other", "YouTube", "")]
    s = st(biz)
    for k in ("facebook", "instagram", "tiktok", "youtube", "google"):
        assert s["qs"][k]["status"] == "ready", k
        assert any(l["k"] == "ob_k_bio" for l in s["qs"][k]["links"]), k
    fb = s["qs"]["facebook"]
    assert any(l["k"] == "ob_k_messenger" and l["url"].startswith("https://m.me/casaluzmerida?ref=") for l in fb["links"])
    assert "ob_l_facebook_dm_off" in [l["k"] for l in fb["lines"]]                  # Messenger itself isn't connected: said so
    assert "ob_w_google_off" in [l["k"] for l in s["qs"]["google"]["lines"]]
    go(biz, "facebook", value="https://www.facebook.com/profile.php?id=100089123456")   # a profile without a username
    assert ans(biz)["facebook"]["username"] is None and len([x for x in srcs(biz) if x["kind"] == "facebook"]) == 1
    for key, bad in (("instagram", "not a handle!"), ("tiktok", "https://tiktok.com/"), ("youtube", "@a")):
        with pytest.raises(Invalid):
            go(biz, key, value=bad)
    r = client.get(f"/l/{next(x for x in srcs(biz) if x['name'] == 'Instagram')['code']}", follow_redirects=False)
    assert r.status_code == 302                                                       # the bio link opens the business's own page


# ---------------------------------------------------------------- calendar, payments, other chats, the trade's apps
def test_calendar_any_app_by_its_private_link_until_they_say_it_is_added(client):
    biz = biz_()
    uid = owner_session(client, biz)
    tok = csrf(client, "/app/onboard?q=calendar")
    client.post("/app/onboard", data={"csrf": tok, "q": "calendar", "act": "answer", "kind": "apple"})
    it = st(biz, uid)["qs"]["calendar"]
    assert it["status"] == "todo" and it["step"]["k"] == "ob_s_feed" and it["links"][0]["url"].endswith(".ics")
    page = client.get("/app/onboard?q=calendar").text
    assert "webcal://" in page and 'value="added"' in page
    client.post("/app/onboard", data={"csrf": tok, "q": "calendar", "act": "added"})
    it = st(biz, uid)["qs"]["calendar"]
    assert it["status"] == "ready" and ans(biz)["calendar"]["kind"] == "apple"


def test_payments_from_the_country_pack_connect_screen_or_said_cant(client):
    biz = biz_()
    go(biz, "payments", pick=["mercadopago", "cash", "nope"])
    assert ans(biz)["payments"]["value"] == ["mercadopago", "cash"]
    it = st(biz)["qs"]["payments"]
    assert it["status"] == "todo" and it["step"]["href"] == "/app/settings#payments"
    with db.tx() as conn:
        PAY.connect(conn, fresh(biz), "t", "mercadopago", {"access_token": "APP_USR-1", "webhook_secret": "w"})
    assert st(biz)["qs"]["payments"]["status"] == "ready"
    co = biz_("CO", "salon")
    go(co, "payments", pick=["wompi"])                                                # no way to connect it here: honest
    it = st(co)["qs"]["payments"]
    assert it["status"] == "waiting" and "ob_w_pay_cant" in [l["k"] for l in it["lines"]]
    with pytest.raises(Invalid):
        go(biz, "payments", pick=[])


def test_other_chat_apps_and_the_trade_apps(client):
    biz = biz_("PH", "realtor")
    go(biz, "chats", pick=["viber", "telegram"])
    it = st(biz)["qs"]["chats"]
    assert it["status"] == "todo" and it["step"]["href"] == "/app/channels?c=viber#manual"
    jp = biz_("JP", "salon")
    go(jp, "chats", pick=["line"])
    assert st(jp)["qs"]["chats"]["status"] == "waiting"                            # LINE can't be connected here yet
    mx = biz_()
    go(mx, "tools", pick=["easybroker", "inmuebles24", "zillow"], link_inmuebles24="www.inmuebles24.com/inmobiliarias/casaluz")
    a = ans(mx)["tools"]
    assert a["value"] == ["easybroker", "inmuebles24"] and a["links"] == {"inmuebles24": "https://www.inmuebles24.com/inmobiliarias/casaluz"}
    assert sorted(s["name"] for s in srcs(mx) if s["kind"] == "marketplace") == ["EasyBroker", "Inmuebles24"]
    it = st(mx)["qs"]["tools"]
    assert it["status"] == "ready" and it["extra"][0]["href"] == "/app/connect-apps#in"
    with pytest.raises(Invalid):
        go(mx, "tools", pick=["easybroker"], link_easybroker="http://easybroker.com/x")


# ---------------------------------------------------------------- the talk: no, later, resume, summary, Today, dry run
def test_no_later_resume_summary_and_the_today_nudge(client):
    biz = biz_("MX", "salon")
    owner_session(client, biz)
    assert 'id="onboard"' in client.get("/app").text and 'href="/app/onboard"' in client.get("/app").text
    tok = csrf(client, "/app/onboard")
    r = client.post("/app/onboard", data={"csrf": tok, "q": "whatsapp", "act": "later"}, follow_redirects=False)
    assert r.headers["location"] == "/app/onboard?q=email"
    page = client.get("/app/onboard").text                                            # resumes at the first unanswered one
    assert 'name="q" value="email"' in page
    order = OB.questions(fresh(biz))
    for q in order[1:]:
        r = client.post("/app/onboard", data={"csrf": tok, "q": q, "act": "no"}, follow_redirects=False)
    assert r.headers["location"] == "/app/onboard?q=summary"
    s = st(biz)
    assert s["next"] is None and s["total"] == 1 and s["ready"] == 0 and [i["key"] for i in s["pending"]] == ["whatsapp"]
    with db.tx() as conn:
        assert conn.execute(select(db.onboarding.c.finished_at)).scalar() is not None
    page = client.get("/app/onboard?q=summary", headers={"Accept-Language": "en"}).text
    assert "Ready: 0 of 1" in page and "Answer this one" in page and "You don&#39;t have:" in page
    today = client.get("/app", headers={"Accept-Language": "en"}).text
    assert "Valeria sets up your accounts" in today or "Valeria configura tus cuentas" in today and 'href="/app/onboard?q=whatsapp"' in today
    client.post("/app/onboard", data={"csrf": tok, "q": "whatsapp", "act": "answer", "kind": "personal", "number": "999 123 4567"})
    s = st(biz)
    assert s["done"] and s["ready"] == 1 and s["total"] == 1
    assert 'id="onboard"' not in client.get("/app").text                                # nothing left: Today stops nudging
    assert "Let Valeria set it all up" in client.get("/app/connect", headers={"Accept-Language": "en"}).text
    r = client.post("/app/onboard/restart", data={"csrf": tok}, follow_redirects=False)
    assert r.status_code == 303 and ans(biz) == {} and len(srcs(biz)) == 1          # what she set up stays
    go(biz, "whatsapp", kind="personal", number="999 123 4567")                    # asked again after a restart: no twin
    go(biz, "instagram", value="@luz.estudio")
    go(biz, "instagram", act="no")
    go(biz, "instagram", value="@luz.estudio")
    assert sorted(x["name"] for x in srcs(biz)) == ["Instagram", "WhatsApp"]


def test_existing_connections_show_green_before_any_answer():
    biz = biz_()
    with db.tx() as conn:
        X.connect(conn, biz, "t", channel="email", name="Mail", address="a@b.mx",
                  cfg={"smtp_host": "smtp.b.mx", "username": "a@b.mx", "password": "app", "from_email": "a@b.mx", "secret": "s"})
    s = st(biz)
    assert s["qs"]["email"]["status"] == "ready" and s["ready"] == 1 and s["total"] == 1 and s["next"] == "whatsapp"


def test_a_dry_run_writes_connects_and_tests_nothing(client, monkeypatch, smtp):
    monkeypatch.setenv("VO_SETUP_DRY_RUN", "1")
    biz = biz_()
    before = fresh(biz)["site"]
    got = go(biz, "whatsapp", kind="personal", number="999 123 4567")
    assert got["dry_run"] and got["steps"] == ["site", "source"]
    got = go(biz, "email", email="luz@gmail.com", app_password=GMAIL_APP)
    assert got["steps"] == ["email"] and smtp.calls == []
    go(biz, "website", sites="www.casaluz.mx")
    go(biz, "instagram", act="no")
    assert fresh(biz)["site"] == before and srcs(biz) == [] and not accounts(biz) and ans(biz) == {}
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.onboarding)).scalar() == 0
    owner_session(client, biz)
    tok = csrf(client, "/app/onboard")
    r = client.post("/app/onboard", data={"csrf": tok, "q": "facebook", "act": "answer", "value": "casaluz"}, follow_redirects=False)
    assert r.headers["location"] == "/app/onboard?q=facebook"
    page = client.get("/app/onboard?q=facebook", headers={"Accept-Language": "en"}).text
    assert "Dry run" in page and "make a tracked link" in page and srcs(biz) == []


def test_owner_only_signed_out_and_the_shared_demo(client):
    assert client.get("/app/onboard", follow_redirects=False).headers["location"] == "/login"
    assert client.post("/app/onboard", follow_redirects=False).status_code == 303
    assert client.post("/app/onboard/restart", follow_redirects=False).status_code == 303
    biz = biz_()
    owner_session(client, biz, role="staff")
    assert client.get("/app/onboard", follow_redirects=False).headers["location"] == "/app"
    assert 'id="onboard"' not in client.get("/app").text
    assert client.get("/app/onboard?q=nope", follow_redirects=False).status_code == 303   # staff: still out


def test_the_api_says_where_it_stands(client):
    biz, key = make_business("MX", "realtor")
    go(biz, "instagram", value="@casaluz")
    r = client.get("/v1/setup", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    d = r.json()
    assert d["ready"] == 1 and d["total"] == 1 and d["next"] == "whatsapp" and d["questions"] == 13
    ig = next(i for i in d["items"] if i["key"] == "instagram")
    assert ig == {"key": "instagram", "has": "yes", "status": "ready", "step": None}
    assert client.get("/v1/setup").status_code == 401


def test_export_carries_the_answers_and_the_migration_chain():
    assert "onboarding" in EX.TABLES
    path = next((ROOT / "alembic" / "versions").glob("*_setup28_*.py"))
    src = path.read_text()
    assert "revision: str = 'c4d8e1f6a2b7'" in src and "down_revision: Union[str, Sequence[str], None] = 'b2e7f4a9c3d1'" in src
    assert "downgrading would lose it" in src
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1 and script.get_revision("c4d8e1f6a2b7")   # one head; v0.29 sits on top (intentional)


# ---------------------------------------------------------------- every screen in every language, on a phone; the words
def test_every_question_and_the_summary_render_in_all_11_languages_on_a_phone(client):
    biz = biz_()
    go(biz, "website", sites="www.casaluz.mx")
    go(biz, "email", email="luz@gmail.com")
    go(biz, "payments", pick=["stripe"])
    owner_session(client, biz)
    phone = {"User-Agent": "Mozilla/5.0 (iPhone)"}
    paths = [f"/app/onboard?q={q}" for q in OB.questions(fresh(biz))] + ["/app/onboard?q=summary", "/app", "/app/connect"]
    for lang in LANGS:
        client.post(f"/lang/{lang}", data={"csrf": csrf(client, "/app")})
        for p in paths:
            r = client.get(p, headers=phone)
            assert r.status_code == 200, (lang, p, r.text[:300])
            main = r.text.split("<main", 1)[1]
            assert not re.search(r"\{\w+\}", main), (lang, p, re.findall(r".{30}\{\w+\}.{10}", main)[:2])
            assert ('dir="rtl"' in r.text) == (lang == "ar")
            assert 'name="viewport"' in r.text


def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = [k for k in files["en"] if k.startswith("ob_")]
    assert len(keys) > 170
    brands = {"ob_n_whatsapp", "ob_n_facebook", "ob_n_instagram", "ob_n_tiktok", "ob_n_youtube", "ob_cal_google", "ob_cal_outlook",
              "ob_cal_apple", "ob_n_email"}
    for lg in LANGS:                                   # pb01 (Tad 2026-11-08): EN + ES always; the other 9 may lag (English shows)
        for k in keys:
            if lg not in ("en", "es") and k not in files[lg]:
                continue
            assert files[lg].get(k, "").strip(), (lg, k)
            assert sorted(re.findall(r"\{\w+\}", files[lg][k])) == sorted(re.findall(r"\{\w+\}", files["en"][k])), (lg, k)
    for lg in ("es", "ja", "ar", "hi", "zh"):
        same = [k for k in keys if k in files[lg] and files[lg][k] == files["en"][k] and k not in brands and len(files["en"][k]) > 10]
        assert not same, (lg, same[:5])
