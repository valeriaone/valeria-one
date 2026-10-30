"""ghl01 (v0.18): ready setups ("snapshots") — one tap per industry, a preview that is the very apply rolled back, merge
that never overwrites, undo within 24 hours, and the owner's own setup exported WITHOUT customer data and copied into
another business they own."""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from _va import refuse_network
from conftest import make_business
from test_web import signup
from valeria_one import db, packs
from valeria_one.services import Invalid, NotFound
from valeria_one.services import booking as BK
from valeria_one.services import contacts as C
from valeria_one.services import fields as CF
from valeria_one.services import snapshots as SN

LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
TABLES = ("custom_fields", "recipes", "workflows", "sequences", "campaigns", "forms", "saved_replies", "booking_services")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def counts(bid):
    with db.tx() as conn:
        return {t: conn.execute(select(func.count()).select_from(getattr(db, t)).where(getattr(db, t).c.business_id == bid)).scalar()
                for t in TABLES}


def fresh(industry="salon", country="MX"):
    biz, _ = make_business(country, industry, name="Estética Mar")
    return biz


def test_every_setup_is_valid_and_speaks_all_11_languages():
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    ship = SN.shipped()
    assert {"salon", "clinic", "florist", "restaurant", "realestate", "design", "fitness", "homeservices", "retail", "generic"} <= set(ship)
    covered = {i for d in ship.values() for i in d["industries"]}
    assert covered == set(packs.industries())                                    # every industry pack has its setup
    keys = set().union(*(SN.keys_in(d) | {f"sn_{k}", f"sn_{k}_d"} for k, d in ship.items()))
    keys |= {k for k in files["en"] if k.startswith("sn_")}
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
    es = SN.resolve(ship["salon"], "es")
    assert not SN.keys_in(es) and es["services"][0]["name"] == files["es"]["sn_sv_salon_cut"]


@pytest.mark.parametrize("key", sorted(SN.shipped()))
def test_each_setup_applies_cleanly_to_its_own_trade(key):
    biz = fresh(SN.shipped()[key]["industries"][0])
    with db.tx() as conn:
        res = SN.apply_shipped(conn, biz, "user:x", key)
    assert sum(len(v) for v in res["added"].values()) >= 8, res
    assert res["added"]["forms"] and res["added"]["replies"] and res["added"]["services"] and res["added"]["hours"]


def test_preview_is_the_apply_rolled_back():
    biz = fresh()
    before = counts(biz["id"])
    with db.tx() as conn:
        pre = SN.apply_shipped(conn, biz, "user:x", "salon", dry=True)
    assert counts(biz["id"]) == before
    with db.tx() as conn:
        assert conn.execute(select(db.snapshot_applies)).first() is None
        res = SN.apply_shipped(conn, biz, "user:x", "salon")
    assert pre["added"] == res["added"] and pre["skipped"] == res["skipped"]
    after = counts(biz["id"])
    assert after["custom_fields"] == 2 and after["saved_replies"] == 3 and after["booking_services"] == 3 and after["forms"] == 1
    assert after["recipes"] == 2 and after["sequences"] == 1 and after["campaigns"] == 1
    with db.tx() as conn:
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        assert b["hours"]["mon"] == [["10:00", "19:00"]] and b["site"]["headline"]
        assert all(not w["active"] for w in conn.execute(select(db.workflows)).mappings())   # nothing starts by itself
        assert all(s["price_minor"] is None for s in BK.services(conn, biz["id"]))           # the owner adds the prices


def test_merge_never_overwrites_and_skips_what_is_there():
    biz = fresh()
    with db.tx() as conn:
        BK.save_settings(conn, biz, "t", hours={"mon": [["07:00", "11:00"]]})
        CF.create(conn, biz, "t", obj="contact", label="Cumpleaños", kind="date")
        from valeria_one.services import site as SI
        SI.save(conn, dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one()), "t",
                headline="Mi frase")
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        res = SN.apply_shipped(conn, b, "user:x", "salon")
        assert "Cumpleaños" in res["skipped"]["fields"] and res["skipped"]["hours"] and res["skipped"]["site"] == ["headline"]
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        assert b["hours"] == {"mon": [["07:00", "11:00"]]} and b["site"]["headline"] == "Mi frase" and b["site"]["about"]
    before = counts(biz["id"])
    with db.tx() as conn:
        again = SN.apply_shipped(conn, b, "user:x", "salon")
    assert sum(len(v) for v in again["added"].values()) == 0 and counts(biz["id"]) == before


def test_undo_within_a_day_keeps_what_was_used():
    biz = fresh()
    with db.tx() as conn:
        res = SN.apply_shipped(conn, biz, "user:x", "salon")
        hair = next(f for f in CF.list_(conn, biz["id"], "contact") if f["kind"] == "choice")
        C.create(conn, biz, "user:x", {"name": "Ana", "custom": {hair["key"]: hair["options"][0]}})   # the owner used one field
        out = SN.undo(conn, biz, "user:x", res["id"])
        with pytest.raises(Invalid):
            SN.undo(conn, biz, "user:x", res["id"])                                     # once
    assert hair["label"] in out["kept"] and out["removed"] >= 10
    left = counts(biz["id"])
    assert left["custom_fields"] == 1 and left["saved_replies"] == 0 and left["forms"] == 0 and left["recipes"] == 0
    with db.tx() as conn:
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        assert b["hours"] == {} and not b["site"].get("headline")
        res2 = SN.apply_shipped(conn, b, "user:x", "generic")
        with pytest.raises(Invalid):
            SN.undo(conn, b, "user:x", res2["id"], now=datetime.now(timezone.utc) + timedelta(hours=25))


def test_export_carries_no_customer_data_and_copies_into_another_business_i_own(client):
    signup(client, email="agency@example.com")
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        SN.apply_shipped(conn, biz, "user:x", "salon")
        C.create(conn, biz, "t", {"name": "Zoila Secreta", "email": "zoila@private.example", "phone": "+525588887777",
                                  "notes": "alergia al tinte", "tags": ["vip"]})
    r = client.get("/app/setup-export.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    raw = r.text
    data = json.loads(raw)
    for secret in ("Zoila", "zoila@private.example", "5588887777", "alergia"):
        assert secret not in raw
    assert data["format"] == "valeria-one-setup" and "contacts" not in data and data["forms"][0]["fields"]
    # a second business of the same owner gets it, merged
    with db.tx() as conn:
        user = conn.execute(select(db.users.c.id)).scalar()
        other, _ = make_business("CO", "salon", name="Sucursal Bogotá")
        conn.execute(db.memberships.insert().values(business_id=other["id"], user_id=user, role="owner"))
        res = SN.copy_to(conn, user, biz, other["id"], f"user:{user}")
        stranger, _ = make_business("CO", "salon", name="Ajeno")
        with pytest.raises(NotFound):
            SN.copy_to(conn, user, biz, stranger["id"], f"user:{user}")
    assert res["added"]["fields"] and res["added"]["forms"] and counts(other["id"])["custom_fields"] == 2
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.contacts).where(db.contacts.c.business_id == other["id"])).scalar() == 0


def test_the_screens_and_a_file_import(client):
    signup(client, email="sn@example.com")
    page = client.get("/app/setups")
    assert page.status_code == 200 and 'href="/app/setups/salon"' in page.text
    assert 'href="/app/setups"' in client.get("/app/start").text
    pre = client.get("/app/setups/salon").text
    assert "Set it all up" in pre or "sn_do_it" not in pre
    t = re.search(r'name="csrf" value="([^"]+)"', pre).group(1)
    assert client.post("/app/setups/salon", data={"csrf": t}, follow_redirects=False).status_code == 303
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.snapshot_applies)).scalar() == 1
    bundle = json.dumps({"format": "valeria-one-setup", "replies": [{"title": "Horario nuevo", "body": "De 9 a 5"}]})
    r = client.post("/app/setup-import", data={"csrf": t}, files={"file": ("s.json", bundle, "application/json")})
    assert r.status_code == 200 and "Horario nuevo" in r.text and 'name="go" value="1"' in r.text
    with db.tx() as conn:
        assert conn.execute(select(db.saved_replies.c.id).where(db.saved_replies.c.title == "Horario nuevo")).first() is None
    client.post("/app/setup-import", data={"csrf": t, "bundle": bundle, "go": "1"})
    with db.tx() as conn:
        assert conn.execute(select(db.saved_replies.c.id).where(db.saved_replies.c.title == "Horario nuevo")).first()
    bad = client.post("/app/setup-import", data={"csrf": t}, files={"file": ("s.json", b'{"hello": 1}', "application/json")},
                      follow_redirects=False)
    assert bad.status_code == 303
    # saved replies show in the inbox reply box and on their own page
    assert client.get("/app/replies").status_code == 200
