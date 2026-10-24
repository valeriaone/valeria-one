import re
from datetime import datetime, timedelta, timezone

import pytest

from valeria_one import db
from valeria_one.services import Invalid, NotFound
from valeria_one.services import team as TM


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def owner_signup(client, email="dueña@example.com", name="Salón Lupita"):
    tok = csrf(client, "/signup")
    client.post("/signup", data={"csrf": tok, "business_name": name, "country": "MX", "industry": "salon",
                                 "name": "Lupita", "email": email, "password": "una-clave-larga"})


def invite_link(client, email, role="staff"):
    tok = csrf(client, "/app/settings")
    r = client.post("/app/team/invite", data={"csrf": tok, "email": email, "role": role})
    return re.search(r"https://one\.test(/join/[A-Za-z0-9_-]+)", r.text).group(1)


def test_invite_join_and_staff_limits(client):
    owner_signup(client)
    link = invite_link(client, "ana@example.com")
    assert link not in client.get("/app/settings").text                         # the link is shown once
    client.cookies.clear()
    page = client.get(link)
    assert page.status_code == 200 and "Salón Lupita" in page.text and "ana@example.com" in page.text
    tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post(link, data={"csrf": tok, "mode": "new", "name": "Ana", "password": "otra-clave-larga"}, follow_redirects=False)
    assert r.headers["location"] == "/app"
    home = client.get("/app").text
    assert "/app/inbox" in home and "/app/settings" not in home and "/app/channels" not in home
    r = client.get("/app/settings", follow_redirects=True)
    assert "Solo los dueños" in r.text or "Only owners" in r.text
    assert client.get("/app/contacts").status_code == 200
    assert client.get(link).status_code == 404                                  # used once


def test_existing_user_accepts_and_switches(client):
    owner_signup(client, email="a@example.com", name="Negocio A")
    client.cookies.clear()
    owner_signup(client, email="b@example.com", name="Negocio B")
    link = invite_link(client, "a@example.com", role="owner")
    client.cookies.clear()
    tok = csrf(client, link)
    client.post(link, data={"csrf": tok, "mode": "login", "password": "una-clave-larga"})
    page = client.get("/app").text
    assert "Negocio A" in page and "Negocio B" in page and 'action="/app/switch"' in page
    bid_b = re.search(r'<option value="([0-9a-f-]{36})"[^>]*>Negocio B<', page).group(1)
    client.post("/app/switch", data={"csrf": csrf(client, "/app"), "business_id": bid_b})
    assert re.search(r'<div class="biz"><b>Negocio B</b>', client.get("/app").text)


def test_wrong_person_expired_and_revoked(client):
    owner_signup(client)
    with db.tx() as conn:
        biz = conn.execute(db.businesses.select()).mappings().one()
        _, token = TM.invite(conn, dict(biz), "t", "x@example.com")
        conn.execute(db.invitations.update().values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert client.get(f"/join/{token}").status_code == 404
    with db.tx() as conn:
        inv, token = TM.invite(conn, dict(biz), "t", "y@example.com")
        TM.revoke(conn, biz["id"], "t", inv["id"])
    assert client.get(f"/join/{token}").status_code == 404
    with db.tx() as conn:
        _, token = TM.invite(conn, dict(biz), "t", "z@example.com")
        owner = conn.execute(db.users.select()).mappings().first()
        with pytest.raises(Invalid):
            TM.accept(conn, token, dict(owner))                                  # invitation is for another email


def test_last_owner_stays_and_duplicates(client):
    owner_signup(client)
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select()).mappings().one())
        owner = conn.execute(db.users.select()).mappings().one()
        with pytest.raises(Invalid):
            TM.remove(conn, biz["id"], "t", owner["id"])
        with pytest.raises(Invalid):
            TM.invite(conn, biz, "t", owner["email"])                          # already on the team
        with pytest.raises(NotFound):
            TM.remove(conn, biz["id"], "t", biz["id"])


def test_staff_cannot_use_owner_actions(client):
    owner_signup(client)
    link = invite_link(client, "staff@example.com")
    client.cookies.clear()
    tok = csrf(client, link)
    client.post(link, data={"csrf": tok, "mode": "new", "password": "otra-clave-larga"})
    tok = csrf(client, "/app")
    for path, data in [("/app/keys", {"name": "x"}), ("/app/ai", {"mode": "off"}), ("/app/team/invite", {"email": "q@example.com"}),
                       ("/app/channels", {"channel": "telegram", "cfg_bot_token": "1", "cfg_secret": "2"}),
                       ("/app/booking", {"enabled": "1"})]:
        r = client.post(path, data={"csrf": tok, **data}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app", path
    with db.tx() as conn:
        assert conn.execute(db.channel_accounts.select()).first() is None
        assert len(conn.execute(db.api_keys.select()).all()) == 0
