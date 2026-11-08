"""claim03 (v0.43.0): the claim page shows what we already have of them (✓), a box for each thing missing, and «if it's all
correct, just create your account»; what they add lands on their website at once. No passwords to other accounts, ever."""
from sqlalchemy import select

from valeria_one import db

from test_claim01 import KIT, csrf, new_kit


def test_the_page_shows_what_we_have_and_asks_only_for_what_is_missing(client):
    k = new_kit({**KIT, "country": "MX", "site": {**KIT["site"], "facebook": "facebook.com/malik"}})
    page = client.get(f"/claim/{k['code']}", headers={"Accept-Language": "es-MX"}).text
    assert 'id="claim-check"' in page and "✓ WhatsApp: <bdi" in page and "+14243944457" in page and "facebook.com/malik" in page
    assert 'name="cl_instagram"' in page and 'name="cl_tiktok"' in page and 'name="cl_whatsapp"' not in page
    assert "Nunca te pedimos contraseñas" in page


def test_what_they_add_lands_on_their_website(client):
    k = new_kit()
    tok = csrf(client, f"/claim/{k['code']}")
    r = client.post("/signup", data={"csrf": tok, "claim": k["code"], "business_name": "Malik Hasnain", "country": "US",
                                      "industry": "generic", "name": "Malik", "email": "malik3@example.com", "password": "una-clave-larga",
                                      "cl_instagram": "@malikrides", "cl_tiktok": "", "cl_whatsapp": "+14243944457"},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    with db.tx() as conn:
        site = conn.execute(select(db.businesses.c.site).join(db.claim_kits, db.claim_kits.c.business_id == db.businesses.c.id)
                            .where(db.claim_kits.c.code == k["code"])).scalar()
    assert site["instagram"] == "malikrides"   # the site keeps handles without «@»
    assert site["whatsapp"] and "tiktok" not in site
