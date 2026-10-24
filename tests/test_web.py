import re
from datetime import date, timedelta

import pytest

from valeria_one import db
from valeria_one.services import users as U


def csrf(client, path="/login"):
    r = client.get(path)
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, f"no csrf on {path}"
    return m.group(1)


def signup(client, country="MX", industry="salon", email="lupita@example.com"):
    tok = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": tok, "business_name": "Salón Lupita", "country": country, "industry": industry,
                                     "name": "Lupita Pérez", "email": email, "password": "una-clave-larga"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app", r.text[:500]
    return r


def test_signed_out_pages(client):
    assert client.get("/").status_code == 200
    assert "Valeria One" in client.get("/login").text
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/app/inbox", follow_redirects=False).headers["location"] == "/login"


def test_signup_then_every_screen_renders(client):
    signup(client)
    for path, word in [("/app", "Hola"), ("/app/inbox", "Bandeja"), ("/app/contacts", "Clientes"),
                       ("/app/pipeline", "Clienta frecuente"), ("/app/calendar", "America/Mexico_City"),
                       ("/app/channels", "whatsapp"), ("/app/settings", "Protección de Datos Personales")]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
        assert word in r.text, (path, word)


def test_csrf_is_required(client):
    signup(client)
    r = client.post("/app/contacts", data={"name": "X"}, follow_redirects=False)
    assert r.status_code == 303
    assert "X" not in [c for c in client.get("/app/contacts").text.split() if c == "X"]


def test_login_logout_and_lockout(client):
    signup(client)
    tok = csrf(client, "/app")
    client.post("/logout", data={"csrf": tok})
    assert client.get("/app", follow_redirects=False).status_code == 303
    tok = csrf(client)
    bad = client.post("/login", data={"csrf": tok, "email": "lupita@example.com", "password": "wrong-password"}, follow_redirects=False)
    assert bad.headers["location"] == "/login"
    tok = csrf(client)
    ok = client.post("/login", data={"csrf": tok, "email": "LUPITA@example.com", "password": "una-clave-larga"}, follow_redirects=False)
    assert ok.headers["location"] == "/app"
    with db.tx() as conn:
        for _ in range(5):
            assert U.authenticate(conn, "lupita@example.com", "nope") is None
        assert U.authenticate(conn, "lupita@example.com", "una-clave-larga") is None   # locked for 15 min
    assert U.MAX_TRIES == 5


def test_duplicate_email_and_short_password(client):
    signup(client)
    client.cookies.clear()
    tok = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": tok, "business_name": "Otro", "country": "MX", "industry": "generic",
                                     "email": "lupita@example.com", "password": "una-clave-larga"})
    assert "already an account" in r.text
    tok = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": tok, "business_name": "Otro", "country": "MX", "industry": "generic",
                                     "email": "new@example.com", "password": "short"})
    assert "10 characters" in r.text


def test_customer_deal_appointment_flow(client):
    signup(client)
    tok = csrf(client, "/app/contacts")
    r = client.post("/app/contacts", data={"csrf": tok, "name": "Mariana", "phone": "999 123 4567", "tags": "vip, novia"},
                    follow_redirects=False)
    cid = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/app/contacts/{cid}").text
    assert "+529991234567" in page and "vip" in page
    r = client.post(f"/app/contacts/{cid}", data={"csrf": tok, "name": "Mariana López", "phone": "+529991234567",
                                                  "consent": "yes", "consent_source": ""}, follow_redirects=True)
    assert "marketing_consent_source" in r.text or "agreed" in r.text          # consent needs a source
    tok = csrf(client, "/app/pipeline")
    client.post("/app/deals", data={"csrf": tok, "title": "Paquete novia", "contact_id": cid, "amount": "3,500.00"})
    board = client.get("/app/pipeline").text
    assert "Paquete novia" in board and "$3,500.00" in board
    did = re.search(r'/app/deals/([0-9a-f-]{36})/stage', board).group(1)
    client.post(f"/app/deals/{did}/stage", data={"csrf": tok, "stage": "won"})
    with db.tx() as conn:
        assert conn.execute(db.deals.select()).mappings().one()["stage"] == "won"
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tok = csrf(client, "/app/calendar")
    client.post("/app/appointments", data={"csrf": tok, "title": "Prueba de peinado", "contact_id": cid, "date": tomorrow,
                                           "time": "11:30", "minutes": "60"})
    cal = client.get("/app/calendar").text
    assert "Prueba de peinado" in cal
    clash = client.post("/app/appointments", data={"csrf": tok, "title": "Otra", "date": tomorrow, "time": "12:00", "minutes": "30"})
    assert "overlaps" in clash.text
    tok = csrf(client, f"/app/contacts/{cid}")
    client.post(f"/app/contacts/{cid}/erase", data={"csrf": tok})
    assert client.get(f"/app/contacts/{cid}").status_code == 404


def test_channels_and_keys(client):
    signup(client)
    tok = csrf(client, "/app/channels")
    client.post("/app/channels", data={"csrf": tok, "channel": "telegram", "name": "Bot", "cfg_bot_token": "123:ABC",
                                       "cfg_secret": "s3cret"})
    page = client.get("/app/channels").text
    assert "/hooks/" in page and "123:ABC" not in page and "s3cret" not in page
    tok = csrf(client, "/app/settings")
    r = client.post("/app/keys", data={"csrf": tok, "name": "zapier"})
    key = re.search(r"(vo_live_[A-Za-z0-9_-]{20,})", r.text).group(1)
    assert client.get("/v1/business", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert key not in client.get("/app/settings").text                   # shown once
    kid = re.search(r'/app/keys/([0-9a-f-]{36})/revoke', client.get("/app/settings").text).group(1)
    client.post(f"/app/keys/{kid}/revoke", data={"csrf": tok})
    assert client.get("/v1/business", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_two_owners_never_cross(client):
    signup(client, email="a@example.com")
    tok = csrf(client, "/app/contacts")
    r = client.post("/app/contacts", data={"csrf": tok, "name": "Private Ana"}, follow_redirects=False)
    cid = r.headers["location"].rsplit("/", 1)[1]
    client.cookies.clear()
    signup(client, country="US", industry="contractor", email="b@example.com")
    assert client.get(f"/app/contacts/{cid}").status_code == 404
    assert "Private Ana" not in client.get("/app/contacts").text
    tok = csrf(client, "/app/contacts")
    assert client.post(f"/app/contacts/{cid}/erase", data={"csrf": tok}).status_code == 404


def test_english_and_language_switch(client):
    signup(client, country="US", industry="contractor", email="us@example.com")
    assert "Waiting for a reply" in client.get("/app").text
    tok = csrf(client, "/app")
    client.post("/lang/es", data={"csrf": tok})
    assert "Esperan respuesta" in client.get("/app").text


def test_demo_and_simulate_on_sandbox(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    demo = seed.seed()
    home = client.get("/login").text
    assert "Salón Lupita" in home and "Kissa Hanami" in home
    bid = demo[0]["business_id"]
    assert client.get(f"/demo/{bid}", follow_redirects=False).headers["location"] == "/app"
    inbox = client.get("/app/inbox").text
    assert "Mariana López" in inbox
    tok = csrf(client, "/app/inbox")
    r = client.post("/app/inbox/simulate", data={"csrf": tok, "name": "Nueva Clienta", "body": "¿Precio de pestañas?"},
                    follow_redirects=False)
    conv = r.headers["location"].split("c=")[1]
    client.post(f"/app/inbox/{conv}/send", data={"csrf": tok, "body": "¡Hola! $350", "kind": "service"})
    thread = client.get(f"/app/inbox?c={conv}").text
    assert "¿Precio de pestañas?" in thread and "¡Hola! $350" in thread and "delivered" in thread
    blocked = client.post(f"/app/inbox/{conv}/send", data={"csrf": tok, "body": "Promo 2x1", "kind": "marketing"},
                          follow_redirects=True)
    assert "promociones" in blocked.text                                  # no consent recorded -> blocked, explained


def test_demo_door_closed_on_real_server(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    demo = seed.seed()
    monkeypatch.setenv("VO_SANDBOX", "0")
    assert client.get(f"/demo/{demo[0]['business_id']}").status_code == 404


@pytest.mark.parametrize("path", ["/app", "/app/inbox", "/app/contacts", "/app/pipeline", "/app/calendar", "/app/channels", "/app/settings"])
def test_every_seeded_country_renders(client, monkeypatch, path):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    for d in seed.seed():
        client.cookies.clear()
        client.get(f"/demo/{d['business_id']}")
        r = client.get(path)
        assert r.status_code == 200, (d["country"], path, r.text[:300])


def test_every_event_type_has_words():
    import pathlib
    import re as _re
    from valeria_one.web.i18n import T
    src = "".join(p.read_text() for p in pathlib.Path("valeria_one/services").glob("*.py"))
    names = set(_re.findall(r'"([a-z_]+\.[a-z_]+)"', src)) & {n for n in _re.findall(r'"([a-z_]+\.[a-z_]+)"', src) if n.split(".")[0] in
        ("business", "api_key", "user", "contact", "channel", "message", "conversation", "deal", "appointment")}
    names |= {f"appointment.{s}" for s in ("booked", "confirmed", "done", "no_show", "cancelled")}
    names |= {"conversation.open", "conversation.closed"}
    missing = sorted(n for n in names if f"ev_{n}" not in T)
    assert missing == []
