"""realty03 (v0.66.2): the realtor's banner is their best property; a brand that is only their own name is not repeated."""
import re

import pytest

from valeria_one import db
from valeria_one.services import partner as PT


@pytest.fixture
def pk(client):
    with db.tx() as conn:
        key, _ = PT.new_key(conn, "valeria.valai.mx", "test")
    return {"Authorization": f"Bearer {key}"}


def _site(client, pk, ref, name, agent, homes=()):
    biz = client.post("/v1/partner/businesses", headers=pk, json={"name": name, "country": "MX", "owner_email": f"{ref}@example.com",
                      "industry": "realtor", "language": "es", "partner_ref": ref}).json()["business"]
    slug = client.post(f"/v1/partner/businesses/{biz['id']}/assistant", headers=pk, json={"agent": agent}).json()["slug"]
    client.put(f"/v1/partner/businesses/{biz['id']}/listings", headers=pk, json={"listings": list(homes)})
    return client.get(f"/s/{slug}").text


def test_banner_is_the_best_property(client, pk):
    h = _site(client, pk, "valai-agency-b1", "Cámara Bienes Raíces", "Luis Cámara", [
        {"ref": "1", "title": "Casa chica", "operation": "sale", "price_minor": 180000000, "photos": ["https://x.mx/small.jpg"]},
        {"ref": "2", "title": "Depa renta", "operation": "rent", "price_minor": 9900000000, "photos": ["https://x.mx/rent.jpg"]},
        {"ref": "3", "title": "Residencia", "operation": "sale", "price_minor": 1500000000, "photos": ["https://x.mx/best.jpg"]},
        {"ref": "4", "title": "Sin foto", "operation": "sale", "price_minor": 9999999999}])
    assert re.search(r'class="bgimg" style="background-image:url\(\'https://x.mx/best.jpg\'\)', h)
    assert '<span class="k">Cámara Bienes Raíces</span>' in h                       # a real brand stays


def test_own_name_brand_not_repeated(client, pk):
    h = _site(client, pk, "valai-agency-b2", "Alma Aragón", "Alma Patricia Aragón")
    assert '<span class="k">Bienes raíces</span>' in h and "© Alma Patricia Aragón</span>" in h and "· Alma Aragón" not in h
    assert "html{background:var(--bg)}" in h


def test_name_fix_on_the_portal_reaches_the_site(client, pk):
    biz = client.post("/v1/partner/businesses", headers=pk, json={"name": "Cámara", "country": "MX", "owner_email": "c@example.com",
                      "industry": "realtor", "partner_ref": "valai-agency-b3"}).json()["business"]
    u = f"/v1/partner/businesses/{biz['id']}/assistant"
    slug = client.post(u, headers=pk, json={"agent": "Luis Camara", "about": "Primero"}).json()["slug"]
    client.post(u, headers=pk, json={"agent": "Luis Cámara", "about": "Segundo"})
    h = client.get(f"/s/{slug}").text
    assert "<h1>Luis Cámara</h1>" in h and "Primero" in h and "Segundo" not in h        # name follows the portal; texts never overwritten
