"""mon01 (v0.16): the owner's forms — a public page at /form/<slug> (never /f/, the friend links' address) in the
business's own look, an embed, the website button; a sent form = one customer (deduped), the inbox, the fields, a deal."""
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select

from test_web import signup
from valeria_one import db, packs
from valeria_one.api import app
from valeria_one.services import fields as CF
from valeria_one.services import forms as FM
from valeria_one.services import site as SI

LANGS = ("ar", "de", "en", "es", "fil", "fr", "hi", "id", "ja", "pt", "zh")


@pytest.fixture(autouse=True)
def no_limits():
    from valeria_one.web import crm
    crm._form_hits.clear()
    yield
    crm._form_hits.clear()


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def the_biz():
    with db.tx() as conn:
        return dict(conn.execute(select(db.businesses)).mappings().one())


def make_form(client, **over):
    t = tok(client)
    biz = the_biz()
    with db.tx() as conn:
        CF.create(conn, biz, "test", obj="contact", label="Talla", kind="choice", options=["Chica", "Grande"])
        key = CF.list_(conn, biz["id"], "contact")[0]["key"]
    stage = [s.key for s in packs.industry(biz["industry"]).stages if s.outcome == "open"][1]
    data = {"csrf": t, "name": "Pide tu cita", "intro": "Te respondemos hoy.", "thanks": "¡Gracias! Te escribimos por WhatsApp.",
            "field": ["name", "phone", "email", "message", f"cf:{key}"], "required": ["phone", f"cf:{key}"], "stage": stage,
            "tag": "Formulario", "on_site": "1", "active": "1", **over}
    r = client.post("/app/forms", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    with db.tx() as conn:
        fm = dict(conn.execute(select(db.forms)).mappings().one())
    return fm, key, stage


def public_token(client, slug, q=""):
    return re.search(r'name="token" value="([^"]+)"', client.get(f"/form/{slug}{q}").text).group(1)


def send(client, slug, **data):
    base = {"token": public_token(client, slug), "name": "Ana López", "phone": "999 123 4567", "consent_request": "1"}
    return client.post(f"/form/{slug}", data={**base, **data}, follow_redirects=False)


def counts():
    with db.tx() as conn:
        return {t: conn.execute(select(func.count()).select_from(db.metadata.tables[t])).scalar()
                for t in ("contacts", "form_submissions", "deals", "messages", "conversations")}


def _paths(routes, prefix=""):
    for r in routes:
        if hasattr(r, "path"):
            yield from ((prefix + r.path, m) for m in (getattr(r, "methods", None) or {"*"}))
        elif hasattr(r, "original_router"):                                        # an included router (FastAPI >= 0.120)
            yield from _paths(r.original_router.routes, prefix + (getattr(getattr(r, "include_context", None), "prefix", "") or ""))


def test_the_public_address_never_collides():
    pm = list(_paths(app.routes))
    paths = [p for p, _ in pm]
    assert ("/form/{slug}", "GET") in pm and ("/form/{slug}", "POST") in pm and "/f/{code}" in paths   # friend links keep /f/
    assert sorted(m for p, m in pm if p.startswith("/form")) == ["GET", "POST"]     # nothing else under /form
    assert not any(p.startswith("/f/") and p != "/f/{code}" for p in paths)


def test_unknown_or_switched_off_form_is_404_not_500(client):
    assert client.get("/form/nada-por-aqui").status_code == 404
    assert client.post("/form/nada-por-aqui", data={"name": "x"}).status_code == 404
    signup(client)
    fm, _, _ = make_form(client)
    t = tok(client)
    client.post(f"/app/forms/{fm['id']}", data={"csrf": t, "name": fm["name"], "field": ["name", "phone"], "active": "0"})
    assert client.get(f"/form/{fm['slug']}").status_code == 404


def test_editor_link_embed_and_menu(client):
    signup(client)
    assert 'href="/app/forms"' in client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>")[0]
    assert 'name="field" value="phone"' in client.get("/app/forms/new").text
    fm, key, stage = make_form(client)
    assert fm["slug"].startswith("salon-lupita-pide-tu-cita") and fm["stage"] == stage and fm["tag"] == "formulario"
    assert fm["fields"][0] == {"key": "name", "required": True} and {"key": f"cf:{key}", "required": True} in fm["fields"]
    page = client.get(f"/app/forms/{fm['id']}").text
    assert f"https://one.test/form/{fm['slug']}" in page and "&lt;iframe src=&#34;https://one.test/form/" in page and "?embed=1" in page
    assert "wa.me/?text=" in page
    t = tok(client)
    r = client.post("/app/forms", data={"csrf": t, "name": "Sin forma de responder", "field": ["name", "message"]}, follow_redirects=False)
    assert r.status_code == 303                                                     # refused with a message: phone or email needed
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.forms)).scalar() == 1


def test_public_page_wears_the_business_look_never_the_office(client):
    signup(client)
    biz = the_biz()
    with db.tx() as conn:
        SI.save(conn, biz, "test", enabled=True, color="#aa3355")
    fm, key, _ = make_form(client)
    client.cookies.clear()
    t = client.get(f"/form/{fm['slug']}").text
    assert "Salón Lupita" in t and "Pide tu cita" in t and "--brand:#aa3355" in t and "Te respondemos hoy." in t
    assert "/static/vo.css" not in t and '<nav class="nav">' not in t and '<body class="vo">' not in t
    assert "Valeria One" not in t                                                  # not even in the title
    assert 'name="website"' in t and 'name="consent_request"' in t and 'name="consent_marketing"' in t
    assert 'name="consent_marketing" value="1" >' in t or 'name="consent_marketing" value="1"  >' in t or \
        re.search(r'name="consent_marketing" value="1"\s*>', t)                     # never ticked for them
    assert f'name="cf:{key}"' in t and "Grande" in t
    e = client.get(f"/form/{fm['slug']}?embed=1").text
    assert "fm-hero" not in e.split("</style>")[1] and 'action="/form/' + fm["slug"] + '?embed=1"' in e


def test_sent_form_lands_in_the_inbox_with_contact_fields_deal_and_tag(client):
    signup(client)
    fm, key, stage = make_form(client)
    client.cookies.clear()
    r = send(client, fm["slug"], email="ana@example.com", message="Quiero una cita el sábado", **{f"cf:{key}": "grande"})
    assert r.status_code == 303 and r.headers["location"] == f"/form/{fm['slug']}?sent=1"
    assert "¡Gracias! Te escribimos por WhatsApp." in client.get(r.headers["location"]).text
    biz = the_biz()
    with db.tx() as conn:
        c = dict(conn.execute(select(db.contacts)).mappings().one())
        msg = conn.execute(select(db.messages.c.body)).scalar()
        deal = dict(conn.execute(select(db.deals)).mappings().one())
        sub = dict(conn.execute(select(db.form_submissions)).mappings().one())
        ev = conn.execute(select(db.events.c.data).where(db.events.c.type == "form.submitted")).scalar()
        acc = conn.execute(select(db.channel_accounts.c.channel).join(db.conversations, db.conversations.c.channel_account_id
                                                                     == db.channel_accounts.c.id)).scalar()
    assert c["name"] == "Ana López" and c["phone"] == "+529991234567" and c["source"] == "form:Pide tu cita"
    assert c["custom"] == {key: "Grande"} and c["tags"] == ["formulario"] and not c["marketing_consent"]
    assert "Pide tu cita" in msg and "Quiero una cita el sábado" in msg and "Talla: Grande" in msg and acc == "webchat"
    assert deal["stage"] == stage and deal["contact_id"] == c["id"]
    assert sub["request_consent"] is True and sub["marketing_consent"] is False and sub["deal_id"] == deal["id"]
    assert ev["form_id"] == str(fm["id"]) and ev["deal_id"] == str(deal["id"])
    with db.tx() as conn:
        assert FM.get(conn, biz["id"], fm["id"])["submissions"] == 1


def test_same_phone_or_email_is_the_same_customer(client):
    signup(client)
    fm, key, _ = make_form(client, required=[])                                  # only the name: email alone is enough
    client.cookies.clear()
    send(client, fm["slug"], **{f"cf:{key}": "Chica"})
    send(client, fm["slug"], phone="+52 999 123 4567", email="ana@example.com", **{f"cf:{key}": "Chica"})
    send(client, fm["slug"], phone="", email="ana@example.com", **{f"cf:{key}": "Chica"})
    n = counts()
    assert n["contacts"] == 1 and n["form_submissions"] == 3 and n["conversations"] == 1


def test_consent_boxes_are_separate(client):
    signup(client)
    fm, key, _ = make_form(client)
    client.cookies.clear()
    r = send(client, fm["slug"], consent_request="", **{f"cf:{key}": "Chica"})
    assert r.status_code == 400 and counts()["contacts"] == 0                       # agreeing to be answered is required
    r = send(client, fm["slug"], consent_marketing="1", **{f"cf:{key}": "Chica"})
    assert r.status_code == 303
    with db.tx() as conn:
        c = dict(conn.execute(select(db.contacts)).mappings().one())
        sub = dict(conn.execute(select(db.form_submissions)).mappings().one())
    assert c["marketing_consent"] and c["marketing_consent_source"].startswith("form:Pide tu cita (https://one.test/form/")
    assert sub["request_consent"] and sub["marketing_consent"]


def test_required_and_typed_answers(client):
    signup(client)
    fm, key, _ = make_form(client)
    client.cookies.clear()
    r = send(client, fm["slug"])                                                     # Talla is required
    assert r.status_code == 400 and "Talla" in r.text and 'value="Ana López"' in r.text   # what they typed stays
    r = send(client, fm["slug"], **{f"cf:{key}": "Enorme"})
    assert r.status_code == 400 and counts()["contacts"] == 0
    r = send(client, fm["slug"], phone="12", **{f"cf:{key}": "Chica"})
    assert r.status_code == 400 and counts()["contacts"] == 0


def test_spam_guard_honeypot_token_and_rate_limit(client):
    signup(client)
    fm, key, _ = make_form(client)
    client.cookies.clear()
    r = send(client, fm["slug"], website="http://spam.example", **{f"cf:{key}": "Chica"})
    assert r.status_code == 303 and counts()["contacts"] == 0                       # bots get "thanks", nothing is kept
    r = client.post(f"/form/{fm['slug']}", data={"token": "forged", "name": "x", "phone": "9991234567", "consent_request": "1"})
    assert r.status_code == 400 and counts()["contacts"] == 0
    other = client.post(f"/form/{fm['slug']}", data={"token": public_token(client, fm["slug"]).replace(fm["slug"][:3], "zzz"),
                                                     "name": "x", "phone": "9991234567", "consent_request": "1"})
    assert other.status_code == 400
    codes = [send(client, fm["slug"], phone=f"99912345{n:02d}", **{f"cf:{key}": "Chica"}).status_code for n in range(12)]
    assert codes.count(303) <= 10 and codes[-1] == 429


def test_embed_works_without_the_session_cookie(client):
    """On the business's own website the page sits in an iframe where our session cookie isn't sent: the signed token is enough."""
    signup(client)
    fm, key, _ = make_form(client)
    token = public_token(client, fm["slug"], "?embed=1")
    client.cookies.clear()
    r = client.post(f"/form/{fm['slug']}?embed=1", data={"token": token, "name": "Bea", "phone": "9997654321", "consent_request": "1",
                                                         f"cf:{key}": "Chica"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/form/{fm['slug']}?embed=1&sent=1" and counts()["contacts"] == 1


def test_the_form_sits_on_the_website(client):
    signup(client)
    biz = the_biz()
    with db.tx() as conn:
        SI.save(conn, biz, "test", enabled=True)
        slug = conn.execute(select(db.businesses.c.slug)).scalar()
    fm, _, _ = make_form(client)
    assert f'href="/form/{fm["slug"]}"' in client.get(f"/s/{slug}").text
    t = tok(client)
    client.post(f"/app/forms/{fm['id']}", data={"csrf": t, "name": fm["name"], "field": ["name", "phone"], "active": "1"})
    assert f'href="/form/{fm["slug"]}"' not in client.get(f"/s/{slug}").text        # on_site unticked


def test_new_words_are_in_all_11_languages_with_their_placeholders():
    base = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    new = [k for k in files["en"] if k.startswith(("cf_", "rc_", "kb_", "fm_"))] + [
        "recipes", "forms", "nx_recipes", "nx_fields", "nx_forms", "ev_lead.form"]
    assert len(new) > 240
    same_ok = {"kb_board", "cf_csv_title", "cf_filter_field", "fm_email", "cf_kind_link", "fm_link"}
    for lg in LANGS:
        for k in new:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
        if lg != "en":
            copied = [k for k in new if files[lg][k] == files["en"][k] and k not in same_ok and re.search(r"[a-z]{4}", files["en"][k])]
            assert copied == [], (lg, copied)
