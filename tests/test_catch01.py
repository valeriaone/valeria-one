"""catch01 (v0.49.0): the website snippet becomes a full lead catcher (data-ask: WhatsApp + email, Instagram / TikTok /
Facebook optional; data-title) — one line on ANY app, ours and every client's. Old snippets keep working as before."""
import json
import shutil
import subprocess

from sqlalchemy import select

from test_lead27 import ROOT, lead_by_phone, setup, source
from valeria_one import db
from valeria_one.services import leads as LD


def post(client, code, **d):
    body = {"name": "Hugo", "message": "Quiero mi oficina", "consent": True, "ms": 6000, **d}
    return client.post(f"/l/{code}/lead", content=json.dumps(body), headers={"Content-Type": "application/json",
                                                                              "Origin": "https://decoraconhugo.valai.mx"})


def test_the_catcher_snippet_and_its_script(client):
    biz, _ = setup()
    s = source(biz, "website", "Sitio de ejemplo")
    tag = LD.embed_catch(s, "¿Es su negocio?")
    assert tag == (f'<script src="https://one.test/l/{s["code"]}/embed.js" data-ask="whatsapp,email,instagram,tiktok,facebook" '
                   'data-title="¿Es su negocio?" async></script>')
    js = client.get(f"/l/{s['code']}/embed.js").text
    assert "data-ask" in js and "data-title" in js and "has('whatsapp')" in js and "has('instagram')" in js and "</script" not in js.lower()
    if shutil.which("node"):
        p = ROOT / "tests" / f".catch01_{s['code']}.js"
        p.write_text(js)
        try:
            assert subprocess.run(["node", "--check", str(p)]).returncode == 0
        finally:
            p.unlink()
    assert '<script src=' in LD.embed(s) and "data-ask" not in LD.embed(s)               # the plain one stays as it was


def test_a_catch_keeps_whatsapp_email_and_their_socials(client):
    biz, _ = setup()
    s = source(biz, "website", "Sitio de ejemplo")
    r = post(client, s["code"], whatsapp="999 115 1095", email="Hugo@Ejemplo.mx", instagram="@decoraconhugo",
             tiktok=" @hugo.cnc ", facebook="facebook.com/decoraconhugo")
    assert r.json() == {"ok": True}
    lead = lead_by_phone(biz, "+529991151095")
    assert lead and lead["source_id"] == s["id"]
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.id == lead["contact_id"])).mappings().one()
        last = conn.execute(select(db.messages.c.body).join(db.conversations, db.conversations.c.id == db.messages.c.conversation_id)
                            .where(db.conversations.c.contact_id == c["id"]).order_by(db.messages.c.created_at.desc())).first()
    assert c["email"] == "hugo@ejemplo.mx" and c["phone"] == "+529991151095"
    assert "Instagram @decoraconhugo · TikTok @hugo.cnc · Facebook facebook.com/decoraconhugo" in c["notes"]
    assert "Instagram @decoraconhugo" in last.body                                       # the owner sees them in the inbox too
    post(client, s["code"], whatsapp="9991151095", email="hugo@ejemplo.mx", instagram="@decoraconhugo")
    with db.tx() as conn:
        c2 = conn.execute(select(db.contacts).where(db.contacts.c.id == lead["contact_id"])).mappings().one()
    assert c2["notes"].count("Instagram @decoraconhugo") == 1                             # same person again: no duplicate line


def test_only_required_fields_and_the_old_single_field_still_work(client):
    biz, _ = setup()
    s = source(biz, "website", "Web")
    assert post(client, s["code"], whatsapp="9992223333").json() == {"ok": True}         # socials absent: fine
    assert lead_by_phone(biz, "+529992223333")
    assert post(client, s["code"], name="Rita", contact="rita@ex.com").json() == {"ok": True}   # the old snippet
    assert post(client, s["code"], name="Nadie", instagram="@solo").status_code == 400      # a handle alone can't reach them


def test_a_facebook_address_is_never_doubled():
    from conftest import make_business
    from valeria_one.services import site as ST
    biz, _ = make_business("MX", "design", name="Decora con Hugo")
    for typed, want in (("facebook.com/decoraconhugo", "https://facebook.com/decoraconhugo"),
                        ("www.facebook.com/decoraconhugo", "https://facebook.com/decoraconhugo"),
                        ("@decoraconhugo", "https://facebook.com/decoraconhugo"),
                        ("https://www.facebook.com/decoraconhugo", "https://www.facebook.com/decoraconhugo")):
        with db.tx() as conn:
            b = ST.save(conn, dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one()), "t",
                        facebook=typed)
        assert b["site"]["facebook"] == want, (typed, b["site"]["facebook"])
