"""claim01 (v0.32): a business set up ahead of time, claimed by its owner with one link."""
import re

import pytest
from sqlalchemy import select

from valeria_one import db, kits
from valeria_one.services import Invalid
from valeria_one.services import claims as CL

KIT = {"business_name": "Malik Hasnain", "country": "US", "industry": "generic", "timezone": "America/Los_Angeles",
       "site": {"headline": "Private chauffeur in Corona, California", "about": "Black cars, fixed prices.",
                "address": "Corona, CA", "color": "#111111", "whatsapp": "+14243944457", "assistant_photo": "nope"},
       "services": [{"name": "Airport transfer · LAX", "minutes": 90, "price_minor": 19500},
                    {"name": "SUV by the hour", "minutes": 180, "price_minor": 37500}, {"name": "  "}],
       "hours": {"mon": [["06:00", "22:00"]], "sat": [["06:00", "22:00"]]},
       "tools": {"staff": True, "pos": True, "voice_agent": True, "no_such_tool": True}}


def csrf(client, path):
    m = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def new_kit(kit=KIT):
    with db.tx() as conn:
        return CL.create(conn, kit, "test")


def claim(client, code, email="malik@example.com"):
    tok = csrf(client, f"/claim/{code}")
    return client.post("/signup", data={"csrf": tok, "claim": code, "business_name": "Malik Hasnain", "country": "US",
                                        "industry": "generic", "name": "Malik", "email": email,
                                        "password": "una-clave-larga"}, follow_redirects=False)


def test_clean_keeps_only_known_parts():
    k = CL.clean(KIT)
    assert k["country"] == "US" and k["industry"] == "generic" and k["timezone"] == "America/Los_Angeles"
    assert "assistant_photo" not in k["site"] and k["site"]["color"] == "#111111"
    assert [s["name"] for s in k["services"]] == ["Airport transfer · LAX", "SUV by the hour"]   # the blank one is dropped
    assert set(k["tools"]) == {"staff", "pos", "voice_agent"} and set(k["tools"]) <= set(kits.OPTIONAL)
    for bad in ({}, {"business_name": "X"}, {"business_name": "X", "country": "ZZ"},
                {"business_name": "X", "country": "US", "industry": "nope"},
                {"business_name": "X", "country": "US", "timezone": "Mars/Base"}):
        with pytest.raises(Invalid):
            CL.clean(bad)


def test_claim_page_shows_the_business(client):
    k = new_kit()
    page = client.get(f"/claim/{k['code']}").text
    assert "Claim Malik Hasnain" in page and 'name="claim"' in page and 'value="Malik Hasnain"' in page
    assert "2 services" in page


def test_claim_signs_up_with_everything_ready(client):
    k = new_kit()
    r = claim(client, k["code"])
    assert r.status_code == 303 and r.headers["location"] == "/app/onboard", r.text[:300]
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.name == "Malik Hasnain")).mappings().one())
        svcs = conn.execute(select(db.booking_services.c.name, db.booking_services.c.price_minor)
                            .where(db.booking_services.c.business_id == biz["id"])).all()
        row = dict(conn.execute(select(db.claim_kits).where(db.claim_kits.c.id == k["id"])).mappings().one())
        ev = conn.execute(select(db.events.c.type).where(db.events.c.business_id == biz["id"],
                                                         db.events.c.type == "business.claimed")).all()
    assert biz["timezone"] == "America/Los_Angeles" and biz["site_enabled"]
    assert biz["site"]["headline"] == "Private chauffeur in Corona, California"
    assert biz["tools"]["staff"] is True and biz["tools"]["voice_agent"] is True
    assert biz["slug"] == "malik-hasnain"
    assert sorted(svcs) == [("Airport transfer · LAX", 19500), ("SUV by the hour", 37500)]
    assert row["claimed_at"] is not None and row["business_id"] == biz["id"] and len(ev) == 1
    assert client.get("/b/malik-hasnain").status_code == 200                         # the booking page is live
    assert client.get("/s/malik-hasnain").status_code == 200                         # and so is the website


def test_a_link_works_once(client):
    k = new_kit()
    assert claim(client, k["code"]).status_code == 303
    client.cookies.clear()
    r = client.get(f"/claim/{k['code']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signup"               # used: the plain sign-up, with a note
    tok = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": tok, "claim": k["code"], "business_name": "Someone Else", "country": "US",
                                     "industry": "generic", "email": "other@example.com", "password": "una-clave-larga"},
                    headers={"referer": "/signup"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signup"               # refused, back to the form with the reason
    assert "already used" in client.get("/signup").text
    assert client.get("/app", follow_redirects=False).headers["location"] == "/login"   # and signed in to nothing
    with db.tx() as conn:                                                            # and no second account was made
        assert conn.execute(select(db.businesses.c.id).where(db.businesses.c.name == "Someone Else")).first() is None


def test_unknown_code_goes_to_plain_signup(client):
    r = client.get("/claim/not-a-real-code", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signup"


def test_signup_without_claim_is_unchanged(client):
    tok = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": tok, "business_name": "Plain Co", "country": "US", "industry": "generic",
                                     "email": "plain@example.com", "password": "una-clave-larga"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/onboard"
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.name == "Plain Co")).mappings().one())
    assert not biz["site_enabled"] and (biz["tools"] or {}) == {}
