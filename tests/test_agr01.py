"""agr01 (v0.25): our agreement with every business — the provider (whoever runs this server) and each business sign it.

Dormant until the operator fills in the provider's details AND publishes a version (old tests run that way: nothing is
asked). Publishing freezes the words in every language + their SHA-256 (the database refuses changes; new details = a new
version). A business made after a version is published is asked at once; an existing owner is asked once per sign-in and may
tap Later for the grace days, then their /app pages wait for it; team members are never blocked; the export, sign-out, the
language, the public site, bookings, /sign, calendars, hooks, /v1, health and the jobs never are. The record (who, for which
business, the exact words shown and their SHA-256, the version's fingerprint, the drawing or the typed name, IP, device,
time) is never changed, is in the business's export, and only that business sees it. A new version asks again; old ones are
kept; "I don't agree" deletes nothing. The operator sees who signed which version. Every word is in all 11 languages."""
import base64
import hashlib
import importlib.util
import io
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from _va import appt, customer, owner_session, refuse_network, shop
from conftest import make_business
from test_web import signup
from valeria_one import db
from valeria_one.api import app
from valeria_one.services import Invalid
from valeria_one.services import agreement as AG
from valeria_one.services import export as EXP
from valeria_one.services import mailer

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
FULL = {"legal_name": "Isla Hosting Inc.", "address": "1 Ayala Avenue, Makati", "country": "PH", "email": "legal@isla.example",
        "law": "the laws of the Republic of the Philippines", "courts": "the courts of Makati City", "delete_days": "45",
        "grace_days": "14", "subprocessors": "Hetzner Online — servers and backups — Germany\nZoho — email — India"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def png(w=400, h=150) -> str:
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).line([(20, 100), (120, 40), (220, 110), (360, 30)], fill=(10, 10, 10, 255), width=5)
    b = io.BytesIO()
    im.save(b, "PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


def tok(client, path="/app/agreement"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def new_client(**headers):
    return TestClient(app, raise_server_exceptions=False, base_url="https://testserver", headers=headers)


def configure(**over):
    with db.tx() as conn:
        return AG.save_settings(conn, "user:op", {**FULL, **over})


def publish(**over):
    configure(**over)
    with db.tx() as conn:
        return AG.publish(conn, "user:op")


def st(biz):
    with db.tx() as conn:
        return AG.state(conn, dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one()))


def rows(biz):
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.agreement_acceptances).where(
            db.agreement_acceptances.c.business_id == biz["id"]).order_by(db.agreement_acceptances.c.version)).mappings()]


def form_of(page_text):
    """The hidden fields of the signing form as the page drew them."""
    f = re.search(r'<form method="post" action="/app/agreement" class="ct-sign" id="ag-sign".*?</form>', page_text, re.S).group(0)
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', f))


def sign_web(client, name="Lupita Pérez", signature=None, **over):
    page = client.get("/app/agreement")
    assert page.status_code == 200, page.text[:300]
    data = {**form_of(page.text), "agree": "1", "name": name, "signature": png() if signature is None else signature, **over}
    return client.post("/app/agreement", data={k: v for k, v in data.items() if v is not None},
                       headers={"referer": "https://testserver/app/agreement"}, follow_redirects=False)


def existing_owner(client, name="Sol Salon"):
    """A business that was here before the version was published, and its owner signed in."""
    biz, key = make_business("PH", "salon", name=name)
    uid = owner_session(client, biz)
    return biz, key, uid


def back_days(biz, days):
    with db.tx() as conn:
        conn.execute(update(db.agreement_asks).where(db.agreement_asks.c.business_id == biz["id"])
                     .values(first_asked_at=func.now() - timedelta(days=days)))


# ================================================================ dormant until the operator fills it in and publishes
def test_dormant_until_the_operator_fills_the_details_and_publishes(client):
    biz, key, uid = existing_owner(client)
    assert client.get("/app", follow_redirects=False).status_code == 200 and st(biz) is None
    with db.tx() as conn:
        s = AG.save_settings(conn, "user:op", {"legal_name": "Isla Hosting Inc.", "email": "legal@isla.example"})
        assert set(AG.missing(s)) == {"address", "country", "law", "subprocessors"}
        with pytest.raises(Invalid):
            AG.publish(conn, "user:op")
    assert client.get("/app", follow_redirects=False).status_code == 200 and st(biz) is None
    assert client.get("/app/settings").status_code == 200 and "/app/agreement" in client.get("/app/settings").text
    page = client.get("/app/agreement")
    assert page.status_code == 200 and 'id="ag-sign"' not in page.text                 # nothing to sign yet
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.provider_agreements)).scalar() == 0


def test_settings_are_checked_and_the_provider_is_never_written_in_the_code():
    with db.tx() as conn:
        with pytest.raises(Invalid):
            AG.save_settings(conn, "t", {**FULL, "country": "XX"})
        with pytest.raises(Invalid):
            AG.save_settings(conn, "t", {**FULL, "email": "not-an-email"})
        with pytest.raises(Invalid):
            AG.save_settings(conn, "t", {**FULL, "delete_days": "0"})
    src = (ROOT / "valeria_one" / "services" / "agreement.py").read_text() + json.dumps(
        {k: v for k, v in json.loads((ROOT / "valeria_one/web/locales/en.json").read_text()).items() if k.startswith("ag_s_")})
    assert "Tadewan" not in src and "valai" not in src.lower() and "Inc." not in src


# ================================================================ publishing: frozen words + SHA-256, versions
def test_publishing_freezes_the_words_in_every_language_and_their_fingerprint():
    a = publish()
    assert a["version"] == 1 and set(a["texts"]) == set(LANGS) and a["doc_hash"] == AG.fingerprint(a)
    en = a["texts"]["en"]["title"] + "\n" + a["texts"]["en"]["body"]
    for words in ("Isla Hosting Inc.", "1 Ayala Avenue, Makati", "Philippines", "legal@isla.example", "the laws of the Republic of the "
                  "Philippines", "the courts of Makati City", "45 days", "Hetzner Online — servers and backups — Germany",
                  "Zoho — email — India", "https://one.test/app/export", "Version 1", "It is free", "controller", "processor",
                  "without undue delay", "Data processing annex", "Electronic signature", "12 months"):
        assert words in en, words
    assert re.findall(r"\{\w+\}", en) and set(re.findall(r"\{\w+\}", en)) == {"{business}"}          # only the signer's own name is left
    assert AG.BLANK not in en and not re.search(r"\b(Article|Section|Act|GDPR|Republic Act)\s+\d", en)   # no statute cited
    for lg in LANGS:
        body = a["texts"][lg]["body"]
        assert set(re.findall(r"\{\w+\}", body + a["texts"][lg]["title"])) == {"{business}"}, lg
        assert "Isla Hosting Inc." in body and "Hetzner Online" in body and AG.BLANK not in body, lg
    with db.tx() as conn:                                                                   # nothing changed = no new version
        with pytest.raises(Invalid):
            AG.publish(conn, "t")
    with pytest.raises(DBAPIError):                                                         # the database refuses changes
        with db.tx() as conn:
            conn.execute(update(db.provider_agreements).values(texts={}))
    with pytest.raises(DBAPIError):
        with db.tx() as conn:
            conn.execute(db.provider_agreements.delete())
    configure(delete_days="60")                                                             # new details = a new version
    with db.tx() as conn:
        assert AG.changed(conn) and AG.current(conn)["version"] == 1                       # saving never changes a published one
        b = AG.publish(conn, "t")
        assert b["version"] == 2 and "60 days" in b["texts"]["en"]["body"] and "45 days" in AG.version(conn, 1)["texts"]["en"]["body"]


def test_a_paid_server_says_so_in_the_price_section(monkeypatch):
    monkeypatch.setenv("VO_BILLING", "on")
    a = publish()
    assert "Billing page" in a["texts"]["en"]["body"] and "It is free" not in a["texts"]["en"]["body"]
    assert a["provider"]["price"] == "paid"


# ================================================================ the business side
def test_a_new_sign_up_is_asked_at_once_and_signs(client):
    publish()
    signup(client, country="PH")
    r = client.get("/app/connect", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/agreement"
    page = client.get("/app/agreement")
    assert page.status_code == 200 and "I agree, for Salón Lupita" in page.text and 'value="later"' not in page.text
    assert "Isla Hosting Inc." in page.text and "lawyer" not in page.text                   # the sample note is the operator's only
    assert client.get("/app/contacts", follow_redirects=False).headers["location"] == "/app/agreement"     # no Later: it waits
    r = sign_web(client)
    assert r.status_code == 303 and r.headers["location"] == "/app/connect"               # back where they were going
    assert client.get("/app/contacts", follow_redirects=False).status_code == 200
    with db.tx() as conn:
        biz = conn.execute(select(db.businesses).where(db.businesses.c.name == "Salón Lupita")).mappings().one()
    (row,) = rows(biz)
    assert row["business_name"] == "Salón Lupita" and row["signed_name"] == "Lupita Pérez" and row["method"] == "drawn"
    assert any(m["to"] == "lupita@example.com" and "Isla Hosting Inc." in m["subject"] and "Salón Lupita" in m["body"]
               for m in mailer.OUTBOX)                                                     # a copy (kept in the outbox: no SMTP here)
    copies = client.get("/app/agreement")
    assert "Version 1" in copies.text and f"/app/agreement/{row['id']}/print" in copies.text


def test_an_existing_owner_is_asked_once_at_sign_in_then_later_then_it_waits(client):
    biz, key, uid = existing_owner(client)
    publish()
    r = client.get("/app/calendar", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/agreement"
    page = client.get("/app/agreement")
    s = st(biz)
    assert 'value="later"' in page.text and s["can_later"] and not s["blocked"] and s["ask"]
    assert s["deadline"] - s["ask"]["first_asked_at"] == timedelta(days=14)
    r = client.post("/app/agreement", data={"csrf": tok(client), "act": "later", "agreement_id": s["id"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/calendar"
    assert client.get("/app", follow_redirects=False).status_code == 200                  # asked once: free to work
    assert "/app/agreement" in client.get("/app").text                                     # the small reminder stays
    again = new_client()                                                                   # the next sign-in: asked once more
    again.post("/login", data={"csrf": tok(again, "/login"), "email": f"owner-{str(biz['id'])[:6]}@example.com",
                               "password": "una-clave-larga"}, follow_redirects=False)
    assert again.get("/app", follow_redirects=False).headers["location"] == "/app/agreement"
    back_days(biz, 15)                                                                     # the grace is over
    assert client.get("/app", follow_redirects=False).headers["location"] == "/app/agreement"
    page = client.get("/app/agreement")
    assert 'value="later"' not in page.text and st(biz)["blocked"]
    r = client.post("/app/agreement", data={"csrf": tok(client), "act": "later", "agreement_id": s["id"]},
                    headers={"referer": "https://testserver/app/agreement"}, follow_redirects=False)
    assert r.headers["location"].endswith("/app/agreement") and not rows(biz)
    assert sign_web(client, name="Sol Reyes").status_code == 303
    assert client.get("/app", follow_redirects=False).status_code == 200 and rows(biz)[0]["signed_name"] == "Sol Reyes"


def test_team_members_are_never_blocked_and_cannot_sign_for_the_owner(client):
    biz, key = make_business("PH", "salon", name="Sol Salon")
    publish()
    with db.tx() as conn:
        AG.asked(conn, {"id": biz["id"]}, AG.current(conn)["id"])
    back_days(biz, 30)
    owner_session(client, biz, role="staff")
    page = client.get("/app", follow_redirects=False)
    assert page.status_code == 200 and "still has to sign the agreement" in page.text
    ag = client.get("/app/agreement")
    assert ag.status_code == 200 and 'id="ag-sign"' not in ag.text and "Isla Hosting Inc." in ag.text
    r = client.post("/app/agreement", data={"csrf": tok(client), "act": "sign", "agreement_id": st(biz)["id"], "agree": "1",
                                            "name": "Staff Person"}, follow_redirects=False)
    assert r.status_code == 303 and not rows(biz)


def test_while_the_owner_is_gated_everything_customers_and_machines_use_keeps_working(client):
    import valeria_one.jobs as J
    biz, tg, gw = shop(country="PH", name="Sol Salon")
    ana = customer(biz, gw, "Ana López", phone="+639171234567")
    from datetime import datetime, timezone
    a = appt(biz, ana, datetime.now(timezone.utc) + timedelta(days=2))
    with db.tx() as conn:
        from valeria_one.services import businesses as BS
        key, _ = BS.new_key(conn, biz["id"])
    from valeria_one.web import _sig
    slug = biz["slug"]
    pub = new_client()
    paths = [f"/b/{slug}", f"/s/{slug}", f"/privacy/{slug}", f"/b/{slug}/ics/{a['id']}/{_sig(a['id'])}", f"/b/{slug}/ics/{a['id']}/bad",
             "/sign/00000000-0000-0000-0000-000000000000/bad", f"/hooks/{gw['id']}", "/health", "/login", "/directory"]
    before = {p: pub.get(p, follow_redirects=False).status_code for p in paths}
    api_before = pub.get("/v1/business", headers={"Authorization": f"Bearer {key}"}).status_code
    jobs_before = set(J._round()[1])
    owner_session(client, biz)
    publish()
    with db.tx() as conn:
        AG.asked(conn, {"id": biz["id"]}, AG.current(conn)["id"])
    back_days(biz, 30)
    assert client.get("/app", follow_redirects=False).headers["location"] == "/app/agreement"     # the owner is gated
    assert {p: pub.get(p, follow_redirects=False).status_code for p in paths} == before            # customers: unchanged
    assert {p: client.get(p, follow_redirects=False).status_code for p in paths} == before         # even in the owner's browser
    assert before["/health"] == 200 and before[f"/b/{slug}"] == 200 and before["/sign/00000000-0000-0000-0000-000000000000/bad"] == 404
    assert pub.get("/v1/business", headers={"Authorization": f"Bearer {key}"}).status_code == api_before == 200
    assert set(J._round()[1]) == jobs_before                                                       # the jobs: no new errors
    exp = client.get("/app/export", follow_redirects=False)
    assert exp.status_code == 200 and exp.headers["content-type"].startswith("application/json")
    assert client.get("/app/agreement").status_code == 200
    r = client.post("/lang/es", data={"csrf": tok(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/logout", data={"csrf": tok(client)}, follow_redirects=False).headers["location"] == "/login"
    assert client.get("/app", follow_redirects=False).headers["location"] == "/login"


def test_signing_keeps_every_audit_field_and_the_record_never_changes(client):
    biz, key, uid = existing_owner(client)
    a = publish()
    c = new_client(**{"User-Agent": "Mozilla/5.0 (iPhone) TestPhone"})
    c.post("/login", data={"csrf": tok(c, "/login"), "email": f"owner-{str(biz['id'])[:6]}@example.com", "password": "una-clave-larga"})
    c.post("/lang/fil", data={"csrf": tok(c, "/login")})
    fields = form_of(c.get("/app/agreement").text)
    assert sign_web(c, name="  Sol   Reyes ").status_code == 303
    (row,) = rows(biz)
    lang, words = AG.text_for(a, "fil", "Sol Salon")
    assert row["lang"] == "fil" and row["text"] == words and row["text_sha256"] == hashlib.sha256(words.encode()).hexdigest()
    assert row["business_id"] == biz["id"] and row["user_id"] == uid and row["agreement_id"] == a["id"] and row["version"] == 1
    assert row["doc_hash"] == a["doc_hash"] and row["signed_name"] == "Sol Reyes" and row["business_name"] == "Sol Salon"
    assert row["signature"].startswith("data:image/png;base64,") and row["method"] == "drawn" and row["ip"]
    assert "TestPhone" in row["ua"] and row["accepted_at"] and "Sol Salon" in row["text"] and "{business}" not in row["text"]
    with db.tx() as conn:
        ev = conn.execute(select(db.events).where(db.events.c.business_id == biz["id"], db.events.c.type == "agreement.accepted")).mappings().one()
    assert ev["data"]["text_sha256"] == row["text_sha256"] and ev["data"]["version"] == 1
    with pytest.raises(DBAPIError):
        with db.tx() as conn:
            conn.execute(update(db.agreement_acceptances).values(signed_name="Someone Else"))
    r = c.post("/app/agreement", data={**fields, "agree": "1", "name": "Other Name", "signature": png()}, follow_redirects=False)
    assert r.status_code == 303 and len(rows(biz)) == 1 and rows(biz)[0]["signed_name"] == "Sol Reyes"   # twice = the first record
    pr = c.get(f"/app/agreement/{row['id']}/print")
    assert pr.status_code == 200 and row["text_sha256"] in pr.text and "Sol Reyes" in pr.text and row["signature"] in pr.text
    with db.tx() as conn:                                                                    # the signer's account goes; the record stays
        conn.execute(db.memberships.delete().where(db.memberships.c.user_id == uid))
        conn.execute(db.users.delete().where(db.users.c.id == uid))
    (kept,) = rows(biz)
    assert kept["user_id"] is None and kept["signer_email"] and kept["text_sha256"] == row["text_sha256"]


def test_the_typed_name_alone_is_kept_when_the_pad_cannot_be_used(client):
    biz, key, uid = existing_owner(client)
    publish()
    page = client.get("/app/agreement")
    assert "<noscript>" in page.text and "your typed full name is your signature" in page.text
    assert sign_web(client, signature="").status_code == 303                                # no drawing came (no script / no pad)
    (row,) = rows(biz)
    assert row["method"] == "typed" and row["signature"] is None and row["signed_name"] == "Lupita Pérez"


def test_signing_is_refused_without_the_tick_a_name_a_good_drawing_or_with_old_words(client):
    biz, key, uid = existing_owner(client)
    publish()
    for over in ({"agree": None}, {"name": "S"}, {"signature": "data:image/png;base64,AAAA"}, {"shown": "0" * 64},
                 {"agreement_id": "00000000-0000-0000-0000-000000000000"}):
        r = sign_web(client, **over)
        assert r.status_code == 303 and r.headers["location"].endswith("/app/agreement"), over
    assert not rows(biz)
    page = client.get("/app/agreement")
    assert 'id="ag-sign"' in page.text


def test_a_new_version_asks_again_and_the_old_signature_is_kept(client):
    biz, key, uid = existing_owner(client)
    publish()
    client.get("/app/agreement")
    assert sign_web(client).status_code == 303
    v1 = rows(biz)[0]
    publish(subprocessors=FULL["subprocessors"] + "\nCloudflare — network — United States")
    assert client.get("/app", follow_redirects=False).headers["location"] == "/app/agreement"
    page = client.get("/app/agreement")
    assert "Cloudflare" in page.text and st(biz)["version"] == 2 and st(biz)["need"]
    assert sign_web(client).status_code == 303
    r = rows(biz)
    assert [x["version"] for x in r] == [1, 2] and r[0] == v1 and "Cloudflare" not in r[0]["text"] and "Cloudflare" in r[1]["text"]


def test_i_dont_agree_shows_how_to_leave_and_deletes_nothing(client):
    biz, tg, gw = shop(country="PH", name="Sol Salon")
    customer(biz, gw, "Ana López")
    owner_session(client, biz)
    publish()
    s = st(biz)
    client.get("/app/agreement")
    r = client.post("/app/agreement", data={"csrf": tok(client), "act": "decline", "agreement_id": s["id"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/agreement?declined=1"
    page = client.get("/app/agreement?declined=1")
    assert "/app/export" in page.text and "legal@isla.example" in page.text and "Nothing was deleted" in page.text
    assert 'action="/logout"' in page.text and 'id="ag-sign"' in page.text                  # they can still change their mind
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.contacts).where(db.contacts.c.business_id == biz["id"])).scalar() == 1
        assert conn.execute(select(db.agreement_asks.c.declined_at).where(db.agreement_asks.c.business_id == biz["id"])).scalar()
        assert conn.execute(select(db.businesses.c.id).where(db.businesses.c.id == biz["id"])).scalar()
    assert sign_web(client).status_code == 303 and len(rows(biz)) == 1


# ================================================================ the operator
def _admin(client):
    biz, key = make_business("PH", "professional", name="Isla Hosting")
    uid = owner_session(client, biz)
    with db.tx() as conn:
        conn.execute(update(db.users).where(db.users.c.id == uid).values(is_admin=True))
    return biz


def test_the_operator_fills_previews_publishes_and_sees_who_signed(client):
    _admin(client)
    other, _ = make_business("MX", "salon", name="Estética Luna")
    page = client.get("/app/server-agreement")
    assert page.status_code == 200 and "have a lawyer review it" in page.text and "Fill these to switch on the agreement" in page.text
    assert "Fill these to switch on the agreement" in client.get("/app/server").text and "/app/server-agreement" in client.get("/app/server").text
    assert 'action="/app/server-agreement/publish"' not in page.text                         # nothing to publish yet
    r = client.post("/app/server-agreement", data={"csrf": tok(client, "/app/server-agreement"), **FULL}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/app/server-agreement")
    assert "Fill these to switch on" not in page.text and 'action="/app/server-agreement/publish"' in page.text
    pv = client.get("/app/server-agreement/preview?lang=es")
    assert pv.status_code == 200 and "Isla Hosting Inc." in pv.text and "Anexo" in pv.text
    assert client.get("/app", follow_redirects=False).status_code == 200                     # still dormant
    r = client.post("/app/server-agreement/publish", data={"csrf": tok(client, "/app/server-agreement")}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/app", follow_redirects=False).status_code == 200                     # the operator's own business: not gated
    page = client.get("/app/server-agreement")
    assert "Estética Luna" in page.text and "Not asked yet" in page.text and "/app/server-agreement/v/1" in page.text
    assert client.get("/app/server-agreement/v/1").status_code == 200 and client.get("/app/server-agreement/v/9").status_code == 404
    luna = new_client()
    owner_session(luna, other)
    assert sign_web(luna, name="Luna Díaz").status_code == 303
    page = client.get("/app/server-agreement")
    assert "Luna Díaz" in page.text and "1 signed" in page.text
    with db.tx() as conn:
        rep = AG.report(conn)
    assert rep["signed"] == 1 and rep["waiting"] == 1 and {r["name"] for r in rep["rows"] if r["current"]} == {"Estética Luna"}


def test_operator_pages_are_hidden_from_others_and_signed_out_visitors(client):
    biz, key, uid = existing_owner(client)
    for p in ("/app/server-agreement", "/app/server-agreement/preview", "/app/server-agreement/v/1"):
        assert client.get(p, follow_redirects=False).status_code == 404, p
    assert client.post("/app/server-agreement", data={"csrf": tok(client, "/app")}, follow_redirects=False).status_code == 404
    out = new_client()
    for p in ("/app/server-agreement", "/app/agreement", "/app/agreement/00000000-0000-0000-0000-000000000000/print"):
        assert out.get(p, follow_redirects=False).status_code == 303, p
    for p in ("/app/server-agreement", "/app/server-agreement/publish", "/app/agreement"):
        assert out.post(p, follow_redirects=False).status_code == 303, p


def test_a_business_never_sees_another_business_s_signed_copy(client):
    biz, key, uid = existing_owner(client, name="Sol Salon")
    publish()
    assert sign_web(client).status_code == 303
    (row,) = rows(biz)
    other, _ = make_business("PH", "salon", name="Luna Salon")
    c2 = new_client()
    owner_session(c2, other)
    assert c2.get(f"/app/agreement/{row['id']}/print").status_code == 404
    assert c2.get("/app/agreement/not-an-id/print").status_code == 404
    assert "Sol Salon" not in c2.get("/app/agreement").text.split("<main", 1)[1]
    assert client.get(f"/app/agreement/{row['id']}/print").status_code == 200


def test_the_export_holds_the_signed_agreement():
    biz, key = make_business("PH", "salon", name="Sol Salon")
    a = publish()
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email="o@example.com", name="O", password_hash="x").returning(db.users.c.id)).scalar()
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        AG.accept(conn, b, {"id": uid, "email": "o@example.com"}, agreement_id=a["id"], lang="en", shown="", name="Sol Reyes",
                  agree=True, signature="", ip="1.2.3.4", ua="t")
        out = EXP.business_data(conn, biz["id"], "t")
    assert {"agreement_acceptances", "agreement_asks"} <= set(EXP.TABLES)
    (r,) = out["agreement_acceptances"]
    assert r["signed_name"] == "Sol Reyes" and r["text_sha256"] and r["text"].startswith("# ") and r["ip"] == "1.2.3.4"
    json.dumps(out, default=str)


def test_sandbox_and_demo_businesses_are_never_asked():
    publish()
    biz, _ = make_business("PH", "salon", sandbox=True)
    assert st(biz) is None


# ================================================================ the words, the templates, the migration
def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {k for k in files["en"] if k.startswith("ag_")} | {"agreement"}
    assert len(keys) >= 130
    used = set()
    for p in ("valeria_one/services/agreement.py", "valeria_one/web/agreement.py", "valeria_one/web/templates/agreement.html",
              "valeria_one/web/templates/agreement_print.html", "valeria_one/web/templates/agreement_view.html",
              "valeria_one/web/templates/server_agreement.html", "valeria_one/web/templates/server.html",
              "valeria_one/web/templates/base.html", "valeria_one/web/templates/settings.html"):
        used |= set(re.findall(r"\bag_[a-z0-9_]+[a-z0-9]", (ROOT / p).read_text(encoding="utf-8")))
    used = {k for k in used if k not in {"ag_s", "ag_f", "ag_method", "ag_price"} or k in keys}
    used |= {f"ag_s_{s}{x}" for s in AG.SECTIONS for x in ("", "_t")} | {"ag_s_extra", "ag_price_free", "ag_price_paid"}
    used |= {f"ag_f_{k}" for k in AG.REQUIRED} | {f"ag_method_{m}" for m in AG.METHODS}
    assert used <= keys, used - keys
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
    for k in keys:
        if len(files["en"][k]) > 14:
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k
    assert len(set(map(frozenset, files.values()))) == 1
    s = {**AG.DEFAULTS, **{k: v for k, v in FULL.items()}, "subprocessors": FULL["subprocessors"].split("\n"), "delete_days": 45}
    for lg in LANGS:
        one = AG.compose(s, lg, version=3, date="2026-10-31", paid=False)
        assert set(re.findall(r"\{\w+\}", one["title"] + one["body"])) == {"{business}"} and one["body"].count("## ") == len(AG.SECTIONS), lg


def test_every_template_compiles_and_the_signature_pad_script_is_valid_js():
    import shutil
    import subprocess
    from valeria_one.web import templates
    names = [n for n in templates.env.list_templates() if n.endswith(".html")]
    for n in names:
        templates.env.get_template(n)
    assert {"agreement.html", "agreement_print.html", "agreement_view.html", "server_agreement.html"} <= set(names)
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", str(ROOT / "valeria_one" / "web" / "static" / "sign.js")]).returncode == 0
    js = (ROOT / "valeria_one" / "web" / "static" / "sign.js").read_text()
    assert "'ag-sign'" in js and "'ct-sign'" in js


def test_one_migration_on_top_of_contracts():
    path = next((ROOT / "alembic" / "versions").glob("*_agr01_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == [mig.revision] and mig.down_revision == "a8d3f6c2e1b9"
    with db.tx() as conn:
        names = {r[0] for r in conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))}
    assert {"provider_agreements_frozen", "agreement_acceptances_frozen"} <= names
