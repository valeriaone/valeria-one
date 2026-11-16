"""realty01 (v0.66.0): a realtor's properties on their Valeria One site, sent by their portal (valeria.valai.mx) — one source.
The partner sends the whole active set; the site shows exactly that set (added, updated, removed), never duplicates."""
import pytest

from valeria_one import db
from valeria_one.services import partner as PT

PHOTO = "https://valeria.valai.mx/media/sites/x/listings/EB-1/00.jpg"


@pytest.fixture
def pk(client):
    with db.tx() as conn:
        key, _ = PT.new_key(conn, "valeria.valai.mx", "test")
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def office(client, pk):
    biz = client.post("/v1/partner/businesses", headers=pk, json={"name": "KW Regio", "country": "MX", "owner_email": "eli@example.com",
                                                                  "industry": "realtor", "partner_ref": "valai-agency-77"}).json()["business"]
    r = client.post(f"/v1/partner/businesses/{biz['id']}/assistant", headers=pk,
                    json={"whatsapp": "81 1912 5436", "headline": "Alta gama en Monterrey", "about": "Asesora en San Pedro.",
                          "photo_url": "https://valeria.valai.mx/media/a.jpg", "instagram": "https://instagram.com/eli.kw",
                          "facebook": "facebook.com/eli.kw", "email": "eli@example.com"})
    assert r.status_code == 200
    return biz["id"], r.json()["slug"]


def _home(ref, **k):
    return {"ref": ref, "title": k.pop("title", f"Casa {ref}"), "operation": "sale", "kind": "casa", "price_minor": 850000000,
            "currency": "MXN", "beds": 3, "baths": "2.5", "area_m2": 220, "place": "Del Valle, San Pedro", "photos": [PHOTO],
            "url": f"https://kw-regio.valai.mx/p/{ref}", **k}


def test_profile_fills_the_site_blanks(client, office):
    _, slug = office
    h = client.get(f"/s/{slug}").text
    assert "Asesora en San Pedro." in h and "Alta gama en Monterrey" in h and "instagram.com/eli.kw" in h
    assert "https://facebook.com/eli.kw" in h and "https://valeria.valai.mx/media/a.jpg" in h


def test_owner_text_wins_over_the_partner(client, pk, office):
    biz_id, slug = office
    client.post(f"/v1/partner/businesses/{biz_id}/assistant", headers=pk, json={"about": "OTRO TEXTO", "headline": "OTRO"})
    h = client.get(f"/s/{slug}").text
    assert "Asesora en San Pedro." in h and "OTRO TEXTO" not in h


def test_bad_photo_or_handle_never_blocks_the_site(client, pk):
    biz = client.post("/v1/partner/businesses", headers=pk, json={"name": "X", "country": "MX", "owner_email": "x@example.com",
                                                                  "industry": "realtor", "partner_ref": "valai-agency-78"}).json()["business"]
    r = client.post(f"/v1/partner/businesses/{biz['id']}/assistant", headers=pk, json={"instagram": "no válido!!", "photo_url": "https://a b"})
    assert r.status_code == 200 and r.json()["slug"]


def test_listings_replace_add_update_remove(client, pk, office):
    biz_id, slug = office
    u = f"/v1/partner/businesses/{biz_id}/listings"
    r = client.put(u, headers=pk, json={"listings": [_home("a1"), _home("a2", operation="rent", price_minor=2500000)]})
    assert r.status_code == 200 and r.json() == {"listings": 2, "added": 2, "removed": 0}
    h = client.get(f"/s/{slug}").text
    assert h.count('class="card home"') == 2 and "Casa a1" in h and "Casa a2" in h and PHOTO in h and "https://kw-regio.valai.mx/p/a1" in h
    assert "En renta" in h or "For rent" in h
    r = client.put(u, headers=pk, json={"listings": [_home("a1"), _home("a2", operation="rent", price_minor=2500000)]})
    assert r.json() == {"listings": 2, "added": 0, "removed": 0}                       # same set again: nothing doubles
    with db.tx() as conn:
        assert conn.execute(db.listings.select().where(db.listings.c.business_id == biz_id)).rowcount == 2
    r = client.put(u, headers=pk, json={"listings": [_home("a2", title="Depa nuevo"), _home("a3")]})
    assert r.json() == {"listings": 2, "added": 1, "removed": 1}
    h = client.get(f"/s/{slug}").text
    assert "Casa a1" not in h and "Depa nuevo" in h and "Casa a3" in h
    assert h.index("Depa nuevo") < h.index("Casa a3")                                   # the portal's order
    r = client.put(u, headers=pk, json={"listings": []})
    assert r.json()["removed"] == 2 and 'class="card home"' not in client.get(f"/s/{slug}").text


def test_listings_bad_input(client, pk, office):
    biz_id, _ = office
    u = f"/v1/partner/businesses/{biz_id}/listings"
    assert client.put(u, json={"listings": []}).status_code == 401
    assert client.put(u, headers=pk, json={"listings": [_home("a"), _home("a")]}).status_code == 422
    assert client.put(u, headers=pk, json={"listings": [_home("a", operation="swap")]}).status_code == 422
    assert client.put(u, headers=pk, json={"listings": [_home("a", price_minor=-1)]}).status_code == 422
    assert client.put(u, headers=pk, json={"listings": [_home("a", photos=["http://x/a.jpg", "javascript:x"], url="javascript:alert(1)")]}).status_code == 200
    with db.tx() as conn:
        row = conn.execute(db.listings.select().where(db.listings.c.business_id == biz_id)).mappings().one()
    assert row["photos"] == [] and row["url"] == ""                                     # only https photos and links
    assert client.put("/v1/partner/businesses/00000000-0000-0000-0000-000000000000/listings", headers=pk,
                      json={"listings": []}).status_code == 404
