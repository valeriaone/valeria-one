"""mem01 (v0.21.1): a member can always get in. Their sign-in link goes by the business's own email; with none
connected, by the server's email in the business's name (replies to the owner); with neither, the owner hears who
asked (push + Memberships) and shares the link with one tap. The page answers the same either way."""
import re

from sqlalchemy import func, select

from _va import owner_session
from test_ars02 import _area_biz, _link_from, smtp, tok, visitor  # noqa: F401  (smtp is a fixture)
from valeria_one import db
from valeria_one.services import contacts as C
from valeria_one.services import mailer, push
from valeria_one.services import members as MS


def _member(biz, mp, email="ana@example.com"):
    with db.tx() as conn:
        c = C.create(conn, biz, "t", {"name": "Ana Ruiz", "email": email})
        MS.add(conn, biz, "t", mp["id"], c["id"])
    return c


def _ask(biz, email="ana@example.com"):
    v = visitor()
    home = v.get(f"/s/{biz['slug']}/members")
    t = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)
    r = v.post(f"/s/{biz['slug']}/members/login", data={"csrf": t, "email": email})
    return v, r


def test_no_email_connected_the_server_sends_it_in_the_business_name(client, smtp, monkeypatch):
    biz, mp = _area_biz("Gym Sin Correo", email_account=False)
    _member(biz, mp)
    owner_session(client, biz)
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **k: sent.append((a, k)) or True)
    v, r = _ask(biz, "ANA@example.com")
    assert "Gym Sin Correo will send it" in r.text and smtp == []
    (to, subject, body), k = sent[0]
    assert to == "ana@example.com" and "Gym Sin Correo" in subject and k["from_name"] == "Gym Sin Correo"
    assert k["reply_to"].startswith("owner-")                                  # replies reach the owner, not us
    link = re.search(r"https://one\.test(/s/\S+/members/in/\S+)", body).group(1)
    assert v.get(link, follow_redirects=False).status_code == 303 and "Ana" in v.get(f"/s/{biz['slug']}/members").text


def test_nothing_can_send_the_owner_hears_and_shares_it(client, smtp, monkeypatch):
    biz, mp = _area_biz("Gym Sin Nada", email_account=False)
    _member(biz, mp)
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    monkeypatch.setattr(mailer, "configured", lambda: False)
    pushed = []
    monkeypatch.setattr(push, "notify", lambda bid, title, body, url="/app", **k: pushed.append((bid, title, body, url)) or 1)
    _ask(biz, "nobody@example.com")                                             # not a member: nothing, nobody told
    assert pushed == [] and mailer.OUTBOX == []
    _ask(biz)
    assert len(pushed) == 1 and pushed[0][0] == biz["id"] and "Ana Ruiz" in pushed[0][2] and pushed[0][3].startswith("/app/members#m-")
    page = client.get("/app/members").text
    assert "Asked for their sign-in link" in page and "ana@example.com" in page
    mid = re.search(r'#m-([0-9a-f-]{36})', pushed[0][3]).group(1)
    t = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/app/members", data={"csrf": t, "act": "link", "member_id": mid})
    assert "/members/in/" in r.text and "wa.me" in r.text                       # one tap: the link, ready for WhatsApp
    assert "Asked for their sign-in link" not in client.get("/app/members").text   # given: off the list


def test_asking_again_and_again_never_floods(client, smtp, monkeypatch):
    biz, mp = _area_biz("Gym Insistente", email_account=False)
    _member(biz, mp)
    monkeypatch.setattr(mailer, "configured", lambda: False)
    pushed = []
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushed.append(a) or 1)
    for _ in range(6):
        MS.send_link(biz, "ana@example.com")
    with db.tx() as conn:
        n = conn.execute(select(func.count()).select_from(db.events).where(db.events.c.type == "member.link_asked")).scalar()
    assert n == 3 and len(pushed) == 3


def test_own_email_still_comes_first(client, smtp, monkeypatch):
    biz, mp = _area_biz("Gym Con Correo")
    _member(biz, mp)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("server mail used")))
    assert MS.send_link(biz, "ana@example.com") is True
    assert len(smtp) == 1 and "hola@gym.mx" in smtp[0]["From"] and _link_from(smtp[0])


def test_the_server_mail_shows_the_business_name(monkeypatch):
    got = {}

    class S:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, m): got["m"] = m
    monkeypatch.setenv("VO_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("VO_SMTP_FROM", "no-reply@one.test")
    monkeypatch.setattr(mailer.smtplib, "SMTP", S)
    assert mailer.send("a@example.com", "Hola", "cuerpo", reply_to="dueña@example.com", from_name='Flores "Rosa"\nX')
    assert "Flores 'Rosa' X" in got["m"]["From"] and "no-reply@one.test" in got["m"]["From"]
    assert mailer.send("a@example.com", "Hola", "cuerpo") and "Valeria One" in got["m"]["From"]
