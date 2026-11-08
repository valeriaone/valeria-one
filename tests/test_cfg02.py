"""cfg02 (v0.48.0): Valeria opens the office FOR the owner (from their claim kit, a one-use 7-day link, no password), and the
setup talk takes their logo + photos (Valeria puts them on their site)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from _va import csrf, owner_session, refuse_network
from conftest import make_business
from valeria_one import db
from valeria_one.services import claims as CL
from valeria_one.services import onboard as OB
from valeria_one.services import partner as PT


def _png():
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (30, 30), (10, 120, 200)).save(b, "PNG")
    return b.getvalue()


PNG = _png()
KIT = {"business_name": "Armor Gym", "country": "MX", "industry": "fitness", "timezone": "America/Merida",
       "services": [{"name": "Mensualidad", "minutes": 60, "price_minor": 65000}]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def test_valeria_opens_the_office_for_the_owner(client):
    with db.tx() as conn:
        kit = CL.create(conn, KIT, "test")
        biz, tok = PT.concierge(conn, "host:concierge", kit["code"], email="dueno@armor.mx", name="Luis Pérez")
    assert biz["name"] == "Armor Gym"
    with db.tx() as conn:
        k = conn.execute(select(db.claim_kits).where(db.claim_kits.c.code == kit["code"])).mappings().one()
        svc = conn.execute(select(db.booking_services).where(db.booking_services.c.business_id == biz["id"])).mappings().all()
        u = conn.execute(select(db.users).where(db.users.c.email == "dueno@armor.mx")).mappings().one()
        m = conn.execute(select(db.memberships).where(db.memberships.c.user_id == u["id"])).mappings().one()
    assert k["claimed_at"] and str(k["business_id"]) == str(biz["id"])                 # the claim link is used
    assert any(s["price_minor"] == 65000 for s in svc)                                  # loaded from the kit
    assert m["role"] == "owner" and u["name"] == "Luis Pérez"
    r = client.get(f"/sso/{tok}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/onboard"             # straight into «Valeria lo configura»
    client.cookies.clear()
    assert client.get(f"/sso/{tok}", follow_redirects=False).headers["location"] == "/login"   # one use
    with db.tx() as conn:
        with pytest.raises(Exception):
            PT.concierge(conn, "t", kit["code"], email="otro@x.mx")                    # a used kit can't make a second office


def test_the_owner_link_lasts_7_days_never_more():
    with db.tx() as conn:
        kit = CL.create(conn, {**KIT, "business_name": "Fitality"}, "test")
        biz, _ = PT.concierge(conn, "t", kit["code"], email="hola@fitality.mx")
        old = PT.login_link(conn, "t", biz["id"], email="hola@fitality.mx", seconds=10**9,
                            now=datetime.now(timezone.utc) - timedelta(days=7, minutes=1))
        fresh = PT.login_link(conn, "t", biz["id"], email="hola@fitality.mx", seconds=10**9,
                              now=datetime.now(timezone.utc) - timedelta(days=6))
        assert PT.use_login(conn, old) is None and PT.use_login(conn, fresh) is not None


def test_logo_and_photos_step(client):
    biz, _ = make_business("MX", "fitness", name="Up Fitness")
    owner_session(client, biz)
    page = client.get("/app/onboard?q=look", headers={"Accept-Language": "es-MX"}).text
    assert "Mándame tu logo y tus fotos" in page and 'name="photos"' in page and "multiple" in page
    tok = csrf(client, "/app/onboard?q=look")
    r = client.post("/app/onboard", data={"csrf": tok, "q": "look", "act": "answer"}, follow_redirects=False)
    assert r.headers["location"] == "/app/onboard?q=look"                               # nothing sent: asked again
    r = client.post("/app/onboard", data={"csrf": tok, "q": "look", "act": "answer", "note": "La primera es mi favorita"},
                    files=[("logo", ("logo.png", PNG, "image/png")), ("photos", ("a.png", PNG, "image/png")),
                           ("photos", ("b.png", PNG, "image/png"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] != "/app/onboard?q=look"
    with db.tx() as conn:
        it = OB.state(conn, biz, links=False)["qs"]["look"]
    assert it["status"] == "ready" and it["answer"]["logo"] and len(it["answer"]["photos"]) == 2
    assert it["answer"]["value"] == "La primera es mi favorita"
    r = client.post("/app/onboard", data={"csrf": tok, "q": "look", "act": "answer", "note": "Hunter2!Xq9#zz"},
                    files=[("photos", ("d.png", PNG, "image/png"))], follow_redirects=False)
    with db.tx() as conn:
        assert len(OB.answers(conn, biz["id"])["look"]["photos"]) == 2                  # a typed password is still refused
    r = client.post("/app/onboard", data={"csrf": tok, "q": "look", "act": "answer"},
                    files=[("photos", ("c.png", PNG, "image/png"))], follow_redirects=False)
    with db.tx() as conn:
        it = OB.state(conn, biz, links=False)["qs"]["look"]
    assert len(it["answer"]["photos"]) == 3 and it["answer"]["logo"]                    # more photos add, nothing lost
    r = client.post("/app/onboard", data={"csrf": tok, "q": "look", "act": "answer"},
                    files=[("photos", ("rota.png", b"\x89PNG\r\n\x1a\nbroken", "image/png"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/onboard?q=look"     # a broken picture: a calm word
