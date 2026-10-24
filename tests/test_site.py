import json
import re

import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import booking as BK
from valeria_one.services import site as ST


def published(name="Taller Don Beto", **extra):
    biz, key = make_business("MX", "contractor", name=name)
    with db.tx() as conn:
        biz = BK.save_settings(conn, biz, "t", enabled=True, slug=BK.slugify(name), hours={"mon": [["09:00", "18:00"]]})
        BK.add_service(conn, biz, "t", name="Visita y presupuesto", minutes=60, price_minor=0)
        biz = ST.save(conn, biz, "t", enabled=True, headline="Plomería y electricidad en Mérida",
                      about="20 años de experiencia.", whatsapp="999 123 4567", instagram="@donbeto.taller",
                      address="Calle 50, Mérida", map_url="https://maps.example.com/x", color="#0e7490", **extra)
    return biz, key


def test_page_renders_with_seo_and_actions(client):
    biz, _ = published()
    r = client.get(f"/s/{biz['slug']}")
    assert r.status_code == 200
    t = r.text
    assert "Taller Don Beto" in t and "Plomería y electricidad en Mérida" in t and "Visita y presupuesto" in t
    assert 'href="https://wa.me/529991234567"' in t and "instagram.com/donbeto.taller" in t and f"/b/{biz['slug']}" in t
    assert '<link rel="canonical" href="https://one.test/s/taller-don-beto">' in t and "--brand:#0e7490" in t
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', t, re.S).group(1))
    assert ld["@type"] == "LocalBusiness" and ld["name"] == "Taller Don Beto" and ld["openingHours"] == ["Mo 09:00-18:00"]


def test_contact_form_lands_in_the_inbox(client):
    biz, key = published()
    page = client.get(f"/s/{biz['slug']}").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post(f"/s/{biz['slug']}", data={"csrf": tok, "name": "Lucía", "reach": "999 765 4321",
                                               "message": "Se me inundó el baño, ¿pueden venir hoy?"}, follow_redirects=True)
    assert "¡Gracias!" in r.text or "Thanks!" in r.text
    h = {"Authorization": f"Bearer {key}"}
    conv = client.get("/v1/conversations", headers=h).json()[0]
    assert conv["channel"] == "webchat" and conv["contact_name"] == "Lucía"
    c = client.get(f"/v1/contacts/{conv['contact_id']}", headers=h).json()
    assert c["phone"] == "+529997654321" and c["source"] == "website"
    msgs = client.get(f"/v1/conversations/{conv['id']}", headers=h).json()["messages"]
    assert msgs[0]["body"].startswith("Se me inundó")
    # the same person writing again is the same customer
    client.post(f"/s/{biz['slug']}", data={"csrf": tok, "name": "Lucía", "reach": "+52 999 765 4321", "message": "¿?"})
    assert len(client.get("/v1/contacts", headers=h).json()) == 1


def test_form_needs_a_way_back_and_blocks_bots(client):
    biz, key = published()
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get(f"/s/{biz['slug']}").text).group(1)
    r = client.post(f"/s/{biz['slug']}", data={"csrf": tok, "name": "X", "reach": "nope", "message": "hola"}, follow_redirects=True)
    assert "phone" in r.text.lower() or "teléfono" in r.text.lower()
    client.post(f"/s/{biz['slug']}", data={"csrf": tok, "name": "Bot", "reach": "a@b.co", "message": "spam", "website": "x"})
    assert client.get("/v1/contacts", headers={"Authorization": f"Bearer {key}"}).json() == []


def test_unpublished_is_not_found_and_validation(client):
    biz, key = make_business("MX")
    with db.tx() as conn:
        biz = BK.save_settings(conn, biz, "t", slug="oculto")
        assert client.get("/s/oculto").status_code == 404
        for bad in ({"color": "red"}, {"photo_url": "http://x"}, {"photo_url": 'https://x.com/a") ;x'}, {"instagram": "a b"},
                    {"whatsapp": "123"}, {"map_url": "javascript:alert(1)"}):
            with pytest.raises(Invalid):
                ST.save(conn, biz, "t", **bad)


def test_owner_editor(client):
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Panadería Sol", "country": "CO", "industry": "shop",
                                 "email": "sol@example.com", "password": "una-clave-larga"})
    page = client.get("/app/website").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.post("/app/website", data={"csrf": tok, "enabled": "1", "headline": "Pan recién horneado", "about": "Desde 1990",
                                      "color": "#b45309", "whatsapp": "300 123 4567"})
    page = client.get("/app/website").text
    assert "/s/panaderia-sol" in page
    site = client.get("/s/panaderia-sol").text
    assert "Pan recién horneado" in site and "wa.me/573001234567" in site
