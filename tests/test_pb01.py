"""pb01 (v0.45.0): Valeria One powers the partner's own app (first: valeria.valai.mx) — «Abrir mi oficina» signs the realtor
in with a one-use 2-minute link (no second password), and the app's leads land as contacts, never twice."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from valeria_one import db
from valeria_one.services import partner as PT


@pytest.fixture
def pk(client):
    with db.tx() as conn:
        key, _ = PT.new_key(conn, "valeria.valai.mx", "test")
    return {"Authorization": f"Bearer {key}"}


def office(client, pk, ref="valai-agency-1"):
    r = client.post("/v1/partner/businesses", headers=pk, json={"name": "Luis Cámara Bienes Raíces", "country": "MX",
                                                                  "owner_email": "luis@example.com", "industry": "realtor",
                                                                  "partner_ref": ref})
    assert r.status_code == 201, r.text
    return r.json()["business"]["id"]


def link(client, pk, biz, **kw):
    r = client.post(f"/v1/partner/businesses/{biz}/login-link", headers=pk,
                    json={"email": "luis@example.com", "name": "Luis Cámara", **kw})
    assert r.status_code == 201, r.text
    return r.json()["url"].split("/sso/", 1)[1]


def test_open_my_office_signs_in_once_with_no_password(client, pk):
    biz = office(client, pk)
    tok = link(client, pk, biz, next="/app/channels")
    r = client.get(f"/sso/{tok}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/channels"
    assert client.get("/app", follow_redirects=False).status_code == 200                # signed in
    with db.tx() as conn:
        u = conn.execute(select(db.users).where(db.users.c.email == "luis@example.com")).mappings().one()
        m = conn.execute(select(db.memberships).where(db.memberships.c.user_id == u["id"])).mappings().one()
    assert str(m["business_id"]) == biz and m["role"] == "owner" and u["name"] == "Luis Cámara"
    client.cookies.clear()
    r = client.get(f"/sso/{tok}", follow_redirects=False)                               # used: never again
    assert r.status_code == 303 and r.headers["location"] == "/login"
    tok2 = link(client, pk, biz)                                                        # a second link: same member, no dup
    assert client.get(f"/sso/{tok2}", follow_redirects=False).headers["location"] == "/app"
    with db.tx() as conn:
        assert len(conn.execute(select(db.memberships).where(db.memberships.c.user_id == u["id"])).all()) == 1


def test_the_link_expires_is_tamper_proof_and_stays_inside_the_app(client, pk):
    biz = office(client, pk)
    with db.tx() as conn:
        old = PT.login_link(conn, "t", biz, email="luis@example.com", now=datetime.now(timezone.utc) - timedelta(minutes=3))
        evil = PT.login_link(conn, "t", biz, email="luis@example.com", next_="https://evil.example/app")
    assert client.get(f"/sso/{old}", follow_redirects=False).headers["location"] == "/login"
    body, sig = evil.split(".")
    assert client.get(f"/sso/{body}.{'0' * len(sig)}", follow_redirects=False).headers["location"] == "/login"
    assert client.get(f"/sso/{evil}", follow_redirects=False).headers["location"] == "/app"
    with db.tx() as conn:
        PT.suspend(conn, "t", biz, "unpaid")
        paused = PT.login_link(conn, "t", biz, email="luis@example.com")
    client.cookies.clear()
    assert client.get(f"/sso/{paused}", follow_redirects=False).headers["location"] == "/login"


def test_a_lead_lands_once_tags_add_and_consent_only_adds(client, pk):
    biz = office(client, pk)
    url = f"/v1/partner/businesses/{biz}/contacts"
    lead = {"name": "Ana Ruiz", "phone": "999 555 0001", "source": "valeria.valai.mx", "tags": ["altabrisa"],
            "notes": "Casa en Altabrisa: ¿sigue disponible?"}
    r = client.post(url, headers=pk, json=lead)
    assert r.status_code == 201 and r.json()["created"] is True
    cid = r.json()["contact_id"]
    r = client.post(url, headers=pk, json={**lead, "email": "ana@x.mx", "tags": ["montebello"], "notes": "Otra casa",
                                           "marketing_consent": True, "marketing_consent_source": "formulario valeria.valai.mx"})
    assert r.status_code == 200 and r.json() == {"contact_id": cid, "created": False}
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.business_id == biz)).mappings().one()
    assert c["phone"] == "+529995550001" and c["email"] == "ana@x.mx" and c["tags"] == ["altabrisa", "montebello"]
    assert "Altabrisa" in c["notes"] and "Otra casa" in c["notes"] and c["marketing_consent"]
    r = client.post(url, headers=pk, json={"phone": "+529995550001", "tags": []})              # nothing taken away
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.business_id == biz)).mappings().one()
    assert r.status_code == 200 and c["tags"] == ["altabrisa", "montebello"] and c["marketing_consent"]
    assert client.post(url, headers=pk, json={"name": "Nadie"}).status_code == 422
    r = client.post(url, headers=pk, json={"name": "T", "email": "t@e.com", "tags": ["marketplace"]})     # v0.46.1: no notes
    assert r.status_code == 201 and r.json()["created"] is True
    assert client.post(url, json=lead).status_code == 401                                      # partner key only


def test_partner_routes_need_the_partner_key(client, pk):
    biz = office(client, pk)
    assert client.post(f"/v1/partner/businesses/{biz}/login-link", json={"email": "x@y.mx"}).status_code == 401
    assert client.post(f"/v1/partner/businesses/{biz}/login-link", headers={"Authorization": "Bearer vo_partner_nope"},
                       json={"email": "x@y.mx"}).status_code == 401
