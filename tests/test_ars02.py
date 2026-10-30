"""ars02 (v0.21): funnels, email designs, the posting calendar, memberships + courses + community, affiliates, ads, connect
other apps — and a complete export.

Customer-facing pages wear the business's look and never run what the owner typed (escaped words, https addresses only,
videos by provider + id); visitors are counted without cookies; nothing is published or sent by itself (posts only when
the owner scheduled them and only on the business's own connected accounts; the rest is a reminder); a member signs in
with a one-time link and sees only that business; an affiliate earns on paid sales (exact integers) and loses it on a
refund; ads are words + a tracked link, $0 to us; webhooks are signed (HMAC), retried with back-off, never sent to a
private address; every business table is in the export; every query stays inside its business; every new tool is in
the registry, in Add more, in the kits it fits and in all 11 languages. Tests never call a live AI, SMTP or network."""
import hashlib
import importlib.util
import json
import re
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from _va import Net, owner_session, refuse_network, with_ai
from conftest import make_business
from test_campaigns import SMTP, polite, setup_biz
from test_posts import Meta, shop as fb_shop
from test_web import signup
from valeria_one import channels, crypto, db, kits, money, packs
from valeria_one.api import app
from valeria_one.services import Invalid, NotFound
from valeria_one.services import ads as ADS
from valeria_one.services import affiliates as AF
from valeria_one.services import businesses as B
from valeria_one.services import campaigns as CG
from valeria_one.services import contacts as C
from valeria_one.services import courses as CO
from valeria_one.services import emails as EM
from valeria_one.services import export as EXP
from valeria_one.services import followups as FU
from valeria_one.services import forms as FM
from valeria_one.services import funnels as FN
from valeria_one.services import inbound as IN
from valeria_one.services import inbox as X
from valeria_one.services import invoices as IVS
from valeria_one.services import mailer
from valeria_one.services import members as MS
from valeria_one.services import pos as POS
from valeria_one.services import posts as PO
from valeria_one.services import site as ST
from valeria_one.services import webhooks as WH

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
NEW = ("funnels", "emails", "schedule", "members", "courses", "affiliates", "ads", "connect_apps")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Addresses for the webhook tests (the real DNS is never asked)."""
    table = {"hooks.example.com": "93.184.215.14", "inside.example.com": "10.0.0.5", "meta.example.com": "169.254.169.254",
             "six.example.com": "::1", "localhost": "127.0.0.1", "flip.example.com": "93.184.215.15"}

    def fake(host, port, *a, **kw):
        if host in table:
            ip = table[host]
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]
        raise socket.gaierror("no such host in tests")
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


@pytest.fixture
def smtp(monkeypatch):
    SMTP.sent = []
    monkeypatch.setattr(channels.adapter("email").__class__, "smtp_factory", SMTP)
    return SMTP.sent


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def visitor():
    """Someone on their own phone (reading English)."""
    return TestClient(app, raise_server_exceptions=False, base_url="https://testserver", headers={"Accept-Language": "en"})


def signed_in(client, country="MX", industry="shop", name=None):
    biz, _ = make_business(country, industry, name=name or f"Tienda {country}")
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    return biz


def fresh(biz):
    with db.tx() as conn:
        return B.get(conn, biz["id"])


def count(table, **where):
    with db.tx() as conn:
        q = f"SELECT count(*) FROM {table}" + (" WHERE " + " AND ".join(f"{k} = :{k}" for k in where) if where else "")
        return conn.execute(text(q), where).scalar()


def nav_hrefs(client):
    return re.findall(r'<a href="([^"]+)"', client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0])


# ================================================================ funnels
def test_a_funnel_from_a_starter_goes_live_counts_people_and_leads(client):
    biz = signed_in(client, industry="professional", name="Coach Ana")
    r = client.post("/app/funnels", data={"csrf": tok(client, "/app/funnels"), "name": "Clase gratis", "template": "leads"},
                    follow_redirects=False)
    assert r.status_code == 303
    fid = r.headers["location"].rsplit("/", 1)[1]
    biz = fresh(biz)
    assert biz["slug"]                                                         # the public address was made for it
    with db.tx() as conn:
        ps = FN.pages(conn, biz["id"], fid)
        form = FM.list_(conn, biz["id"])[0]
    assert len(ps) == 2 and [b["t"] for b in ps[0]["blocks"]] == ["hero", "text", "form", "testimonial", "faq"]
    assert ps[0]["blocks"][2]["form"] == str(form["id"])                       # a form of its own, made for it
    url = f"/s/{biz['slug']}/p/{ps[0]['slug']}"
    v = visitor()
    assert v.get(url).status_code == 404                                       # off: only the owner sees it
    page = client.get(url)
    assert page.status_code == 200 and "only you can see this page" in page.text and "noindex" in page.headers["x-robots-tag"]
    assert count("funnel_hits") == 0                                           # a preview is never counted
    client.post(f"/app/funnels/{fid}", data={"csrf": tok(client, f"/app/funnels/{fid}"), "act": "on"})
    page = v.get(url)
    assert page.status_code == 200 and "Coach Ana" in page.text and "Get your free guide from Coach Ana" in page.text
    assert "only you can see" not in page.text and "/static/app.css" in page.text and "Valeria One" not in page.text.split("<body", 1)[1]
    v.get(url)
    assert count("funnel_hits", kind="view") == 1                              # one person, one day: counted once
    t = re.search(r'name="token" value="([^"]+)"', page.text).group(1)
    r = v.post(url, data={"token": t, "form": str(form["id"]), "name": "Luis", "phone": "+52 55 1234 5678", "consent_request": "1"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/s/{biz['slug']}/p/{ps[1]['slug']}"   # on to the next step
    assert "Thank you!" in v.get(r.headers["location"]).text
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.business_id == biz["id"])).mappings().one()
        st = FN.stats(conn, biz["id"], fid)
    assert c["name"] == "Luis" and c["source"].startswith("form:") and not c["marketing_consent"]
    assert (st[0]["views"], st[0]["leads"], st[0]["went_on"], st[1]["views"], st[1]["kept_pct"]) == (1, 1, 1, 1, 100)
    assert "1 people" in client.get(f"/app/funnels/{fid}").text and "1 leads" in client.get(f"/app/funnels/{fid}").text
    assert mailer.OUTBOX == [] and count("messages", direction="out") == 0    # nothing is sent by itself
    r = v.post(url, data={"token": "forged", "form": str(form["id"]), "name": "Bot", "phone": "+525512345679", "consent_request": "1"})
    assert r.status_code == 400 and count("contacts") == 1                    # a forged page token is refused


def test_the_block_editor_adds_fills_moves_and_removes_blocks(client):
    biz = signed_in(client)
    with db.tx() as conn:
        fn = FN.create(conn, biz, "t", "Oferta")
        pid = FN.pages(conn, biz["id"], fn["id"])[0]["id"]
    path = f"/app/funnels/pages/{pid}"
    client.post(path, data={"csrf": tok(client, path), "act": "add", "kind": "offer"}, follow_redirects=False)
    page = client.get(path).text
    assert 'name="b-0-t" value="offer"' in page and 'id="new"' in page        # the new block shows, empty, to be filled
    client.post(path, data={"csrf": tok(client, path), "act": "save", "title": "Oferta", "b-0-t": "offer", "b-0-title": "2x1",
                            "b-0-price": "199.50", "b-0-was": "399", "b-0-items": "Corte\nLavado", "b-0-button": "Lo quiero", "b-0-to": "next"})
    client.post(path, data={"csrf": tok(client, path), "act": "add", "kind": "faq"}, follow_redirects=False)
    client.post(path, data={"csrf": tok(client, path), "act": "save", "b-0-t": "offer", "b-0-title": "2x1", "b-0-price": "199.50",
                            "b-1-t": "faq", "b-1-items": "¿Cuándo? | Martes\n¿Dónde? | Centro"})
    with db.tx() as conn:
        bl = FN.page(conn, biz["id"], pid)["blocks"]
    assert bl[0] == {"t": "offer", "title": "2x1", "price": 19950, "was": None, "items": [], "button": ""}
    assert bl[1]["items"] == [{"q": "¿Cuándo?", "a": "Martes"}, {"q": "¿Dónde?", "a": "Centro"}]
    client.post(path, data={"csrf": tok(client, path), "act": "up:1", "b-0-t": "offer", "b-0-title": "2x1", "b-0-price": "199.50",
                            "b-1-t": "faq", "b-1-items": "¿Cuándo? | Martes"})
    with db.tx() as conn:
        assert [b["t"] for b in FN.page(conn, biz["id"], pid)["blocks"]] == ["faq", "offer"]
    client.post(path, data={"csrf": tok(client, path), "act": "remove:0", "b-0-t": "faq", "b-0-items": "x | y", "b-1-t": "offer",
                            "b-1-title": "2x1"})
    with db.tx() as conn:
        assert [b["t"] for b in FN.page(conn, biz["id"], pid)["blocks"]] == ["offer"]
    assert client.get(path, headers={"User-Agent": "Mozilla/5.0 (iPhone)"}).status_code == 200


def test_nothing_the_owner_types_can_run_on_a_visitors_phone(client):
    biz = signed_in(client)
    with db.tx() as conn:
        fn = FN.create(conn, biz, "t", "XSS")
        pid = FN.pages(conn, biz["id"], fn["id"])[0]["id"]
        for bad in ({"t": "button", "button": "x", "to": "url", "url": "javascript:alert(1)"},
                    {"t": "button", "button": "x", "to": "url", "url": 'https://ok.example/"onmouseover="alert(1)'},
                    {"t": "image", "url": "data:text/html,<script>alert(1)</script>"},
                    {"t": "pay", "url": "http://plain.example/pay"},
                    {"t": "video", "url": "javascript:alert(1)"},
                    {"t": "video", "url": "https://evil.example/embed/abc"}):
            with pytest.raises(Invalid):
                FN.clean_blocks(conn, biz["id"], [bad])
        other, _ = make_business("MX", "shop", name="Otra")
        with pytest.raises(NotFound):                                          # only the business's own forms and pictures
            FN.clean_blocks(conn, biz["id"], [{"t": "form", "form": str(FM.save(conn, other, "t", {"name": "F", "fields": [{"key": "phone"}]})["id"])}])
        assert FN.clean_blocks(conn, biz["id"], [{"t": "script", "text": "x"}, {"t": "text"}]) == []     # unknown / empty: dropped
        FN.save_page(conn, biz, "t", pid, blocks=[
            {"t": "hero", "title": "<script>alert('t')</script>", "text": "<img src=x onerror=alert(2)>"},
            {"t": "testimonial", "quote": "</blockquote><script>alert(3)</script>", "name": "\"><svg onload=alert(4)>"},
            {"t": "faq", "items": [{"q": "<b>q</b>", "a": "<iframe src=//evil>"}]},
            {"t": "video", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}])
        FN.save(conn, biz, "t", fn["id"], active=True)
        conn.execute(update(db.funnel_pages).where(db.funnel_pages.c.id == pid).values(      # even a row changed behind our back
            blocks=FN.page(conn, biz["id"], pid)["blocks"] + [{"t": "video", "p": "yt", "id": '"><script>alert(5)</script>'}]))
        slug = FN.page(conn, biz["id"], pid)["slug"]
    page = visitor().get(f"/s/{fresh(biz)['slug']}/p/{slug}").text
    body = page.split("<body", 1)[1]
    assert "<script>alert" not in body and "<img src=x" not in body and "<svg onload" not in body and "<iframe src=//evil" not in body
    assert "&lt;script&gt;alert(" in body and "&lt;img src=x onerror=alert(2)&gt;" in body
    assert body.count("<iframe") == 1 and 'src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in body   # the tampered one: not drawn


def test_funnel_pictures_and_unknown_pages(client):
    from test_posts import jpeg
    biz = signed_in(client)
    with db.tx() as conn:
        fn = FN.create(conn, biz, "t", "Fotos")
        pid = FN.pages(conn, biz["id"], fn["id"])[0]["id"]
    path = f"/app/funnels/pages/{pid}"
    client.post(path, data={"csrf": tok(client, path), "act": "add", "kind": "image"}, follow_redirects=False)
    client.post(path, data={"csrf": tok(client, path), "act": "save", "b-0-t": "image", "b-0-alt": "Local"},
                files={"b-0-upload": ("p.jpg", jpeg(), "image/jpeg")})
    with db.tx() as conn:
        b = FN.page(conn, biz["id"], pid)["blocks"][0]
    slug = fresh(biz)["slug"]
    img = f"/s/{slug}/fi/{b['file']}"
    v = visitor()
    assert v.get(img).status_code == 404                                       # not live: the picture isn't public
    with db.tx() as conn:
        FN.save(conn, biz, "t", fn["id"], active=True)
    r = v.get(img)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    with db.tx() as conn:                                                       # another file of the business: never through here
        other = conn.execute(db.files.insert().values(business_id=biz["id"], kind="photo", name="x", mime="image/jpeg", size=3,
                                                      data=b"abc", public=True, created_by="t").returning(db.files.c.id)).scalar()
    assert v.get(f"/s/{slug}/fi/{other}").status_code == 404
    assert v.get(f"/s/{slug}/p/nope").status_code == 404 and v.get("/s/nobody/p/x").status_code == 404
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(suspended_at=datetime.now(timezone.utc)))
        page = FN.page(conn, biz["id"], pid)
    assert v.get(f"/s/{slug}/p/{page['slug']}").status_code == 404           # a paused business: its pages rest


# ================================================================ email designs
def test_an_email_design_is_safe_tables_with_a_plain_part_and_goes_through_the_campaign_rules(client, smtp):
    biz, ppl = setup_biz()
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    with db.tx() as conn:
        ST.save(conn, biz, "t", enabled=True, instagram="rosa.salon", whatsapp="+525511112222")
    r = client.post("/app/emails", data={"csrf": tok(client, "/app/emails"), "name": "Noticias"}, follow_redirects=False)
    did = r.headers["location"].rsplit("/", 1)[1]
    path = f"/app/emails/{did}"
    client.post(path, data={"csrf": tok(client, path), "act": "save", "name": "Noticias", "subject": "Hola {name}",
                            "blk_t": ["heading", "quote", "divider", "spacer", "button", "social", "text"],
                            "blk_text": ["Novedades", "Me encantó <script>alert(1)</script>", "", "", "Reservar", "", "Te esperamos"],
                            "blk_url": ["", "", "", "", "https://rosa.mx/reservar", "", ""]})
    page = client.get(path).text
    assert 'id="em-pc"' in page and 'id="em-ph"' in page and 'sandbox=""' in page          # computer and phone, nothing runs
    with db.tx() as conn:
        d = EM.get(conn, biz["id"], did)
    assert [b["t"] for b in d["blocks"]] == ["heading", "quote", "divider", "spacer", "button", "social", "text"]
    html, plain = EM.render(fresh(biz), d, {"name": "Ana"})
    assert html.count("<table") >= 3 and 'role="presentation"' in html and "<style" not in html and "<script" not in html
    assert "Me encantó &lt;script&gt;" in html and "https://instagram.com/rosa.salon" in html and "https://wa.me/525511112222" in html
    assert "“Me encantó <script>alert(1)</script>”" in plain and "/u/preview" in plain and "Instagram: https://instagram.com/rosa.salon" in plain
    # used in a campaign: the campaign's own rules (consent, unsubscribe, the business's own account) still decide
    with db.tx() as conn:
        cp = CG.start(conn, biz, "t", "email")
    cpath = f"/app/campaigns/{cp['id']}"
    assert "Start from a saved design" in client.get(cpath + "?step=message").text
    client.post(cpath, data={"csrf": tok(client, cpath + "?step=message"), "act": "design", "design_id": did})
    with db.tx() as conn:
        cp = CG.get(conn, biz["id"], cp["id"])
        assert cp["blocks"] == d["blocks"] and cp["subject"] == "Hola {name}"
        CG.schedule(conn, biz, "t", cp["id"])
    assert SMTP.sent == []                                                       # nothing until the job's round
    now = polite(biz)
    FU.dispatch_campaigns(now)
    FU.send_due(now=now)
    assert sorted(m["To"] for m in smtp) == ["ana@example.com", "beto@example.com"]           # only those who said yes
    m = smtp[0]
    assert "Me encant" in m.get_body(("html",)).get_content() and "/u/" in m.get_body(("plain",)).get_content()
    # keep a campaign as a design; a drip step that uses a design
    client.post(cpath, data={"csrf": tok(client, cpath + "?step=message"), "act": "save_design", "design_name": "Copia"})
    with db.tx() as conn:
        assert {x["name"] for x in EM.list_(conn, biz["id"])} == {"Noticias", "Copia"}  # a scheduled campaign's email: kept as a design
        cp2 = CG.start(conn, biz, "t", "email")
        CG.save(conn, biz, "t", cp2["id"], {"subject": "S", "blocks": [{"t": "text", "text": "Hola"}]})
        EM.from_campaign(conn, biz, "t", cp2["id"], "Copia 2")
        assert {x["name"] for x in EM.list_(conn, biz["id"])} == {"Noticias", "Copia", "Copia 2"}
    t = tok(client, "/app/sequences/new")
    r = client.post("/app/sequences/new", data={"csrf": t, "act": "save", "name": "Bienvenida", "kind": "marketing", "trigger": "manual",
                                                "st_amount": ["0"], "st_unit": ["days"], "st_channel": ["email"], "st_subject": [""],
                                                "st_text": [""], "st_template": [""], "st_design": [did]}, follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[1]
    with db.tx() as conn:
        st = conn.execute(select(db.sequences.c.steps).where(db.sequences.c.id == sid)).scalar()[0]
    assert st["blocks"] == d["blocks"] and st["subject"] == "Hola {name}" and st["design_id"] == did and st["channel"] == "email"
    assert f'value="{did}" selected' in client.get(f"/app/sequences/{sid}").text


# ================================================================ the posting calendar
def test_only_what_the_owner_scheduled_goes_out_and_only_on_connected_accounts(client):
    biz = fb_shop()
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    m = Meta()
    now = datetime.now(timezone.utc)
    with db.tx() as conn:                                                       # a draft with a time is not scheduled: never out
        d = PO.create(conn, biz, "t", nets=["facebook"], texts={"facebook": "borrador"})
        conn.execute(update(db.posts).where(db.posts.c.id == d["id"]).values(publish_at=now - timedelta(hours=1)))
    assert PO.publish_due(http=m.http, now=now) == {"published": 0, "failed": 0, "interrupted": 0} and m.calls == []
    tz = ZoneInfo(biz["timezone"])
    when = (now + timedelta(days=2)).astimezone(tz).replace(minute=0, second=0, microsecond=0)
    r = client.post("/app/schedule", data={"csrf": tok(client, "/app/schedule"), "net": "facebook", "hand": ["tiktok", "instagram"],
                                           "text": "¡Martes 2x1!", "when": when.strftime("%Y-%m-%dT%H:%M")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/app/schedule?m=")
    with db.tx() as conn:
        p = conn.execute(select(db.posts).where(db.posts.c.status == "scheduled")).mappings().one()
    assert p["publish_at"] == when.astimezone(timezone.utc) and p["networks"] == ["facebook", "instagram", "tiktok"]
    assert p["by_hand"] == ["instagram", "tiktok"]                             # Instagram ticked "I post it", TikTok always by hand
    cal = client.get(f"/app/schedule?m={when.year}-{when.month:02d}").text
    assert "¡Martes 2x1!"[:5] in cal and "TikTok" in cal and "Good times to post" in cal
    assert PO.publish_due(http=m.http, now=now)["published"] == 0              # not its time yet
    out = PO.publish_due(http=m.http, now=when + timedelta(minutes=1))
    assert out["published"] == 1
    assert [c[1] for c in m.calls if c[0] == "POST"] == ["/v19.0/pg1/feed"] or [c[1].rsplit("/", 2)[-2:] for c in m.calls if c[0] == "POST"] == [["pg1", "feed"]]
    assert not any("/ig1/" in c[1] for c in m.calls)                           # Instagram by hand: we never posted it
    with db.tx() as conn:
        p = PO.get(conn, biz["id"], p["id"])
        due = PO.hand_due(conn, biz["id"])
    assert p["status"] == "published" and p["results"]["facebook"]["id"] and p["results"]["tiktok"]["hand"]
    assert due[0]["due"] == ["instagram", "tiktok"]
    today = client.get("/app").text
    assert 'id="hand"' in today and "Time to post these yourself" in today and "data-copy" in today
    page = client.get(f"/app/posts/{p['id']}").text
    assert "https://www.tiktok.com/upload" in page and "I posted it" in page
    client.post(f"/app/posts/{p['id']}?back=/app", data={"csrf": tok(client, f"/app/posts/{p['id']}"), "act": "posted:tiktok"})
    client.post(f"/app/posts/{p['id']}?back=https://evil.example", data={"csrf": tok(client), "act": "posted:instagram"},
                follow_redirects=False)
    with db.tx() as conn:
        assert PO.hand_due(conn, biz["id"]) == [] and PO.get(conn, biz["id"], p["id"])["results"]["tiktok"]["done_at"]
    assert PO.publish_due(http=m.http, now=when + timedelta(hours=1))["published"] == 0     # never twice


def test_an_unconnected_network_is_a_reminder_never_a_post():
    biz, _ = make_business("MX", "salon", name="Sin redes")
    with db.tx() as conn:
        p = PO.create(conn, biz, "t", nets=["facebook"], texts={"facebook": "hola"})
        with pytest.raises(Invalid):
            PO.approve(conn, biz, "t", p["id"])                                # not connected and not "by hand": refused
        q = PO.create(conn, biz, "t", nets=["facebook", "whatsapp"], texts={"facebook": "hola", "whatsapp": "hola"}, by_hand=["facebook"])
        PO.approve(conn, biz, "t", q["id"])
    calls = []
    out = PO.publish_due(http=httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500))))
    assert out["published"] == 1 and calls == []                              # nothing went to any network
    with db.tx() as conn:
        assert PO.hand_due(conn, biz["id"])[0]["due"] == ["facebook", "whatsapp"]


# ================================================================ memberships, courses, community
def _area_biz(name, email_account=True, industry="fitness"):
    biz, _ = make_business("MX", industry, name=name)
    with db.tx() as conn:
        if email_account:
            X.connect(conn, biz, "t", channel="email", name="Correo", cfg={"smtp_host": "smtp.x", "username": "u", "password": "p",
                                                                           "from_email": "hola@gym.mx", "secret": "s"})
        mp = MS.save_plan(conn, biz, "t", {"name": "Club", "about": "Clases y comunidad", "community": True, "open_join": True})
        biz = B.get(conn, biz["id"])
    return biz, mp


def _link_from(mail) -> str:
    body = mail.get_body(("plain",)).get_content()
    return urlsplit(re.search(r"https://one\.test(/s/\S+/members/in/\S+)", body).group(0)).path


def test_members_sign_in_with_a_one_time_link_and_see_only_their_business(client, smtp):
    a, mpa = _area_biz("Gym Uno")
    b, mpb = _area_biz("Gym Dos")
    with db.tx() as conn:
        ana = C.create(conn, a, "t", {"name": "Ana Ruiz", "email": "ana@example.com"})
        beto = C.create(conn, b, "t", {"name": "Beto", "email": "beto@example.com"})
        MS.add(conn, a, "t", mpa["id"], ana["id"])
        MS.add(conn, b, "t", mpb["id"], beto["id"])
        course = CO.save(conn, a, "t", {"title": "Fuerza 101", "member_plan_id": mpa["id"], "published": True})
        mod = CO.add_module(conn, a, "t", course["id"], "Semana 1")
        l1 = CO.save_lesson(conn, a, "t", mod["id"], {"title": "Bienvenida", "kind": "text", "body": "Hola <b>equipo</b>"})
        l2 = CO.save_lesson(conn, a, "t", mod["id"], {"title": "Sentadilla", "kind": "video",
                                                     "video_url": "https://youtu.be/dQw4w9WgXcQ", "drip_days": 7})
        CO.save(conn, b, "t", {"title": "Curso de B", "member_plan_id": mpb["id"], "published": True})
    v = visitor()
    home = v.get(f"/s/{a['slug']}/members")
    assert home.status_code == 200 and "Sign in" in home.text
    t = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)
    r = v.post(f"/s/{a['slug']}/members/login", data={"csrf": t, "email": "nobody@example.com"}, follow_redirects=False)
    assert r.headers["location"].endswith("?sent=link") and smtp == []        # not a member: nothing sent, the same answer
    v.post(f"/s/{a['slug']}/members/login", data={"csrf": t, "email": "ANA@example.com"})
    assert len(smtp) == 1 and smtp[0]["To"] == "ana@example.com" and "hola@gym.mx" in smtp[0]["From"]   # their own account
    link = _link_from(smtp[0])
    with db.tx() as conn:
        assert not conn.execute(select(db.member_links.c.id).where(db.member_links.c.token_hash == link.rsplit("/", 1)[1])).first()
    r = v.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/s/{a['slug']}/members"
    page = v.get(f"/s/{a['slug']}/members").text
    assert "Ana" in page and "Fuerza 101" in page and "Curso de B" not in page
    assert v.get(link).status_code == 404                                       # works once
    # the same phone at business B: not signed in there, can't open B's things
    assert "Sign in" in v.get(f"/s/{b['slug']}/members").text
    with db.tx() as conn:
        cb = CO.list_(conn, b["id"])[0]
    assert v.get(f"/s/{b['slug']}/members/c/{cb['id']}", follow_redirects=False).status_code == 303
    assert v.get(f"/s/{b['slug']}/members/board/{mpb['id']}", follow_redirects=False).status_code == 303
    assert v.get(f"/s/{a['slug']}/members/c/{cb['id']}").status_code == 404   # B's course through A's address: no
    assert v.get(f"/s/{a['slug']}/members/board/{mpb['id']}").status_code == 404
    # drip: lesson 2 opens on day 7
    c = v.get(f"/s/{a['slug']}/members/c/{course['id']}").text
    assert "Bienvenida" in c and "🔒 Sentadilla" in c
    assert v.get(f"/s/{a['slug']}/members/l/{l2['id']}").status_code == 404
    lp = v.get(f"/s/{a['slug']}/members/l/{l1['id']}")
    assert lp.status_code == 200 and "Hola &lt;b&gt;equipo&lt;/b&gt;" in lp.text
    v.post(f"/s/{a['slug']}/members/l/{l1['id']}", data={"csrf": re.search(r'name="csrf" value="([^"]+)"', lp.text).group(1)})
    with db.tx() as conn:
        conn.execute(update(db.members).where(db.members.c.contact_id == ana["id"]).values(
            joined_at=datetime.now(timezone.utc) - timedelta(days=8)))
        assert CO.progress(conn, a, course["id"])[0]["done"] == 1
    l2p = v.get(f"/s/{a['slug']}/members/l/{l2['id']}").text
    assert 'src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in l2p
    # the board: members post, the owner hides (kept, not shown)
    bt = re.search(r'name="csrf" value="([^"]+)"', v.get(f"/s/{a['slug']}/members/board/{mpa['id']}").text).group(1)
    v.post(f"/s/{a['slug']}/members/board/{mpa['id']}", data={"csrf": bt, "body": "¡Hoy entrené! <script>x</script>"})
    board = v.get(f"/s/{a['slug']}/members/board/{mpa['id']}").text
    assert "¡Hoy entrené! &lt;script&gt;" in board and "<b dir=\"auto\">Ana</b>" in board
    with db.tx() as conn:
        post = conn.execute(select(db.community_posts)).mappings().one()
        MS.hide(conn, a, "owner", "post", post["id"])
        with pytest.raises(NotFound):
            MS.post(conn, a, mpb["id"], "hola", conn.execute(select(db.members.c.id).where(db.members.c.contact_id == ana["id"])).scalar(), "m")
    assert "Hoy entren" not in v.get(f"/s/{a['slug']}/members/board/{mpa['id']}").text
    owner_session(client, a)
    client.post("/lang/en", data={"csrf": tok(client)})
    ob = client.get(f"/app/members/board/{mpa['id']}").text
    assert "Hoy entren" in ob and "Hidden" in ob
    # removed: their session ends, no new link
    with db.tx() as conn:
        mid = conn.execute(select(db.members.c.id).where(db.members.c.contact_id == ana["id"])).scalar()
        MS.set_status(conn, a, "t", mid, "remove")
    assert "Sign in" in v.get(f"/s/{a['slug']}/members").text
    v.post(f"/s/{a['slug']}/members/login", data={"csrf": t, "email": "ana@example.com"})
    assert len(smtp) == 1


def test_join_requests_wait_for_the_owner_and_a_paid_membership_starts_its_subscription(client):
    biz, mp = _area_biz("Yoga Sol", email_account=False)
    with db.tx() as conn:
        plan = IVS.create_plan(conn, biz, "t", name="Mensual", amount_minor=49900)
        mp = MS.save_plan(conn, biz, "t", {"name": "Club", "plan_id": plan["id"], "community": True, "open_join": True}, mp["id"])
    v = visitor()
    home = v.get(f"/s/{biz['slug']}/members").text
    assert "Ask to join" in home and "$499.00" in home
    t = re.search(r'name="csrf" value="([^"]+)"', home).group(1)
    v.post(f"/s/{biz['slug']}/members/join/{mp['id']}", data={"csrf": t, "name": "Lía", "email": "lia@example.com"})
    with db.tx() as conn:
        m = conn.execute(select(db.members)).mappings().one()
    assert m["status"] == "pending" and m["subscription_id"] is None and count("subscriptions") == 0 and count("invoices") == 0
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    page = client.get("/app/members").text
    assert "Asked to join" in page and "Lía" in page
    client.post("/app/members", data={"csrf": tok(client, "/app/members"), "act": "yes", "member_id": str(m["id"])})
    with db.tx() as conn:
        m = conn.execute(select(db.members)).mappings().one()
        sub = conn.execute(select(db.subscriptions)).mappings().one()
    assert m["status"] == "active" and m["joined_at"] and m["subscription_id"] == sub["id"] and sub["plan_id"] == plan["id"]
    assert count("messages", direction="out") == 0                            # its invoices go the usual way, by the invoices job
    page = client.post("/app/members", data={"csrf": tok(client, "/app/members"), "act": "link", "member_id": str(m["id"])}).text
    link = re.search(r'id="ms-link" dir="ltr">([^<]+)<', page).group(1)
    assert "wa.me/?text=" in page and v.get(urlsplit(link).path, follow_redirects=False).status_code == 303   # the owner's shared link
    client.post("/app/members", data={"csrf": tok(client, "/app/members"), "act": "remove", "member_id": str(m["id"])})
    with db.tx() as conn:
        assert conn.execute(select(db.subscriptions.c.status)).scalar() == "cancelled"
        assert conn.execute(select(db.members.c.status)).scalar() == "removed"


# ================================================================ affiliates
def test_affiliate_commission_math_is_exact():
    assert AF.commission({"kind": "pct", "rate_bp": 1250, "fixed_minor": 0}, 10000) == 1250
    assert AF.commission({"kind": "pct", "rate_bp": 5000, "fixed_minor": 0}, 15) == 8                   # 7.5 -> 8, half up
    assert AF.commission({"kind": "pct", "rate_bp": 333, "fixed_minor": 0}, 1001) == 33                 # 33.33 -> 33
    assert AF.commission({"kind": "fixed", "rate_bp": 0, "fixed_minor": 5000}, 1) == 5000
    assert money.part(1001, 333) == 33


def test_an_affiliate_link_brings_a_customer_who_pays_and_the_owner_pays_the_affiliate(client):
    biz = signed_in(client, name="Tienda Luz")
    with db.tx() as conn:
        biz = ST.save(conn, fresh(biz), "t", enabled=True)
        old = C.create(conn, biz, "t", {"name": "Ya clienta", "email": "old@example.com"})
    r = client.post("/app/affiliates", data={"csrf": tok(client, "/app/affiliates"), "name": "Influencer Mía", "kind": "pct",
                                             "rate": "12.5"}, follow_redirects=False)
    aid = r.headers["location"].rsplit("/", 1)[1]
    with db.tx() as conn:
        a = AF.get(conn, biz["id"], aid)
    assert (a["kind"], a["rate_bp"]) == ("pct", 1250) and "12.5" in client.get(f"/app/affiliates/{aid}").text
    v = visitor()
    r = v.get(f"/a/{a['code']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/s/{biz['slug']}"
    st = re.search(r'name="csrf" value="([^"]+)"', v.get(f"/s/{biz['slug']}").text).group(1)
    v.post(f"/s/{biz['slug']}", data={"csrf": st, "name": "Nueva", "reach": "nueva@example.com", "message": "Hola"})
    v.post(f"/s/{biz['slug']}", data={"csrf": st, "name": "Ya clienta", "reach": "old@example.com", "message": "Otra vez"})
    with db.tx() as conn:
        refs = conn.execute(select(db.affiliate_referrals)).mappings().all()
        new = conn.execute(select(db.contacts).where(db.contacts.c.email == "nueva@example.com")).mappings().one()
        assert AF.get(conn, biz["id"], aid)["clicks"] == 1
    assert len(refs) == 1 and refs[0]["contact_id"] == new["id"] and refs[0]["how"] == "link"   # an old customer is not theirs
    with db.tx() as conn:
        inv = IVS.create(conn, biz, "t", title="Pedido", lines=[{"description": "Lámpara", "qty": 1, "unit_minor": 10000}],
                         contact_id=new["id"])
        assert (inv["subtotal_minor"], inv["tax_minor"], inv["total_minor"]) == (10000, 1600, 11600)
        IVS.mark_paid(conn, biz, "t", inv["id"])
        IVS.mark_paid(conn, biz, "t", inv["id"]) if False else None
    sale = POS.sell(biz, "t", lines=[{"description": "Foco", "qty": 2, "unit_minor": 5800}], method="cash", contact_id=new["id"])
    with db.tx() as conn:
        assert AF.owed(conn, biz["id"], aid) == 1250 + 1250                    # 12.5 % of 100.00 before tax, twice
        POS.refund(conn, biz, "t", sale["id"], "devuelto")
        lines = AF.lines(conn, biz["id"], aid)
        assert sorted(x["amount_minor"] for x in lines) == [-1250, 1250, 1250] and AF.owed(conn, biz["id"], aid) == 1250
        AF.on_event(conn, biz["id"], "invoice.paid", inv["id"], {})            # the same sale again: earned once
        assert AF.owed(conn, biz["id"], aid) == 1250
    page = client.get(f"/app/affiliates/{aid}").text
    assert "$12.50" in page and "Refunded" in page
    client.post(f"/app/affiliates/{aid}", data={"csrf": tok(client, f"/app/affiliates/{aid}"), "act": "paid",
                                                "paid_on": date.today().isoformat(), "note": "transferencia"})
    with db.tx() as conn:
        assert AF.owed(conn, biz["id"], aid) == 0 and AF.payouts(conn, biz["id"], aid)[0]["amount_minor"] == 1250
        assert all(x["payout_id"] for x in AF.lines(conn, biz["id"], aid))
        with pytest.raises(Invalid):
            AF.pay(conn, biz, "t", aid, paid_on=date.today())                 # nothing owed: nothing to mark
    csv_ = client.get("/app/affiliates.csv").text
    assert "Influencer Mía" in csv_ and ",12.5%," in csv_ and ",12.5,12.5,0,MXN" in csv_


def test_fixed_first_only_window_and_another_businesss_link():
    biz, _ = make_business("MX", "shop", name="Uno")
    other, _ = make_business("MX", "shop", name="Dos")
    with db.tx() as conn:
        a = AF.save(conn, biz, "t", {"name": "Socio", "kind": "fixed", "fixed_minor": 5000, "first_only": True})
        foreign = AF.save(conn, other, "t", {"name": "Ajeno", "kind": "pct", "rate_bp": 1000})
        c = C.create(conn, biz, "t", {"name": "Cliente"})
        assert AF.claim(conn, biz, AF.remember(foreign), c) is None            # another business's link: nothing
        AF.assign(conn, biz, "t", a["id"], c["id"])
        with pytest.raises(Invalid):
            AF.assign(conn, biz, "t", a["id"], c["id"])                        # first one wins, once
    for _ in range(2):
        POS.sell(biz, "t", lines=[{"description": "x", "qty": 1, "unit_minor": 100}], method="card", contact_id=c["id"])
    with db.tx() as conn:
        assert AF.owed(conn, biz["id"], a["id"]) == 5000                       # fixed, first purchase only
        conn.execute(update(db.affiliate_referrals).values(created_at=datetime.now(timezone.utc) - timedelta(days=AF.COMMISSION_DAYS + 1)))
        conn.execute(update(db.affiliates).where(db.affiliates.c.id == a["id"]).values(first_only=False))
    POS.sell(biz, "t", lines=[{"description": "x", "qty": 1, "unit_minor": 100}], method="card", contact_id=c["id"])
    with db.tx() as conn:
        assert AF.owed(conn, biz["id"], a["id"]) == 5000                       # out of the window: nothing more


# ================================================================ ads
def test_the_ads_helper_drafts_without_live_ai_and_tracks_clicks_and_new_customers(client):
    biz = signed_in(client, name="Barbería Max")
    with db.tx() as conn:
        biz = ST.save(conn, fresh(biz), "t", enabled=True)
    got = ADS.draft(biz, idea="20% en cortes los martes", platform="google")
    assert got["by_ai"] is False and all(len(h) <= ADS.LIMITS["google"][0] for h in got["headlines"]) and "Barbería Max" in got["body"]
    net = Net(ai_text=json.dumps({"headlines": ["Cortes 20% martes", "Barbería Max"], "text": "Ven el martes.", "audience": "Hombres 18-45"}))
    biz = with_ai(biz)
    got = ADS.draft(biz, idea="20% en cortes los martes", platform="meta", http=net.http)
    assert got == {"headlines": ["Cortes 20% martes", "Barbería Max"], "body": "Ven el martes.", "audience": "Hombres 18-45", "by_ai": True}
    assert len(net.ai_calls) == 1                                               # the business's own key, through the mock only
    with db.tx() as conn:
        ad = ADS.save(conn, biz, "t", {"name": "Martes", "platform": "meta", "dest": f"https://one.test/s/{biz['slug']}?x=1&utm_source=old",
                                       "headlines": got["headlines"], "body": got["body"], "audience": got["audience"]})
    page = client.get(f"/app/ads/{ad['id']}").text
    assert "https://adsmanager.facebook.com/adsmanager/" in page and f"/go/{ad['code']}" in page and 'data-copy="#ad-body"' in page
    v = visitor()
    r = v.get(f"/go/{ad['code']}", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.status_code == 302 and q == {"x": ["1"], "utm_source": ["meta"], "utm_medium": ["paid_social"], "utm_campaign": ["martes"],
                                          "utm_content": [ad["code"]]}
    st = re.search(r'name="csrf" value="([^"]+)"', v.get(f"/s/{biz['slug']}").text).group(1)
    v.post(f"/s/{biz['slug']}", data={"csrf": st, "name": "Pepe", "reach": "pepe@example.com", "message": "¿Hay lugar?"})
    with db.tx() as conn:
        assert conn.execute(select(db.contacts.c.source).where(db.contacts.c.email == "pepe@example.com")).scalar() == f"ad:{ad['code']}"
        a = ADS.get(conn, biz["id"], ad["id"])
    assert (a["clicks"], a["leads"]) == (1, 1) and "1 new customers" in client.get("/app/ads").text
    assert v.get("/go/nope-nope-nope").status_code == 404
    with db.tx() as conn, pytest.raises(Invalid):
        ADS.save(conn, biz, "t", {"name": "x", "dest": "javascript:alert(1)"})


# ================================================================ connect other apps
def test_webhooks_are_signed_retried_with_backoff_and_never_sent_inside(client, fake_dns):
    biz = signed_in(client)
    for bad in ("http://hooks.example.com/x", "https://localhost/x", "https://inside.example.com/x", "https://meta.example.com/x",
                "https://six.example.com/x", "https://127.0.0.1/x", "https://[::1]/x", "https://169.254.169.254/latest", "https://0.0.0.0/"):
        with db.tx() as conn, pytest.raises(Invalid):
            WH.create(conn, biz, "t", bad)
    r = client.post("/app/connect-apps", data={"csrf": tok(client, "/app/connect-apps"), "act": "hook", "url": "https://hooks.example.com/vo",
                                               "event": ["contact.created", "invoice.paid", "nope"]})
    secret = re.search(r'id="ca-sec" dir="ltr">(whsec_[^<]+)<', r.text).group(1)
    assert secret not in client.get("/app/connect-apps").text                   # shown once
    with db.tx() as conn:
        ep = WH.endpoints(conn, biz["id"])[0]
        assert ep["events"] == ["contact.created", "invoice.paid"] and crypto.open_({"secret": conn.execute(
            select(db.webhook_endpoints.c.secret)).scalar()})["secret"] == secret
        C.create(conn, biz, "t", {"name": "Ana"})
        C.create(conn, other_biz := make_business("MX", "shop", name="Otra")[0], "t", {"name": "No"}) if False else None
    seen, answers = [], [503, 200]

    def handler(req):
        assert req.headers["x-vo-signature"] == crypto.hmac_sha256_hex(secret, req.content)
        assert req.headers["x-vo-event"] == "contact.created"
        seen.append(json.loads(req.content))
        return httpx.Response(answers.pop(0))
    h = httpx.Client(transport=httpx.MockTransport(handler))
    now = datetime.now(timezone.utc)
    assert WH.deliver_due(http=h, now=now) == {"delivered": 0, "retry": 1, "gave_up": 0}
    with db.tx() as conn:
        d = WH.deliveries(conn, biz["id"], ep["id"])[0]
    assert d["attempts"] == 1 and d["next_at"] == now + timedelta(seconds=WH.BACKOFF[0]) and d["last_error"] == "answered 503"
    assert WH.deliver_due(http=h, now=now + timedelta(seconds=30))["retry"] == 0   # not before its time
    assert WH.deliver_due(http=h, now=now + timedelta(seconds=61))["delivered"] == 1 and len(seen) == 2 and seen[0] == seen[1]
    log = client.get(f"/app/connect-apps?hook={ep['id']}").text
    assert "contact.created" in log and "✓ 200" in log
    # a name that later points inside: the delivery is not sent, and is retried / given up, never sent there
    fake_dns["hooks.example.com"] = "10.1.2.3"
    with db.tx() as conn:
        C.create(conn, biz, "t", {"name": "Beto"})
    assert WH.deliver_due(http=h, now=now + timedelta(seconds=120)) == {"delivered": 0, "retry": 1, "gave_up": 0} and len(seen) == 2
    with db.tx() as conn:                                                       # given up: "send again" puts it back in line
        did = WH.deliveries(conn, biz["id"], ep["id"])[0]["id"]
        conn.execute(update(db.webhook_deliveries).where(db.webhook_deliveries.c.id == did).values(given_up_at=now))
    fake_dns["hooks.example.com"] = "93.184.215.14"
    client.post("/app/connect-apps", data={"csrf": tok(client, "/app/connect-apps"), "act": "redeliver", "id": str(ep["id"]), "delivery": str(did)})
    answers.append(200)
    assert WH.deliver_due(http=h, now=datetime.now(timezone.utc) + timedelta(seconds=1))["delivered"] == 1


def test_other_apps_send_customers_in_with_a_hashed_address(client):
    biz = signed_in(client)
    other, _ = make_business("MX", "shop", name="Otra")
    page = client.post("/app/connect-apps", data={"csrf": tok(client, "/app/connect-apps"), "act": "in", "name": "Zapier",
                                                  "makes": "deal", "stage": packs.industry("shop").stages[0].key, "tag": "zap"}).text
    url = re.search(r'id="ca-in" dir="ltr"[^>]*>([^<]+)<', page).group(1)
    token = url.rsplit("/", 1)[1]
    assert token.startswith("vin_") and token not in client.get("/app/connect-apps").text
    with db.tx() as conn:
        assert conn.execute(select(db.inbound_hooks.c.token_hash)).scalar() == hashlib.sha256(token.encode()).hexdigest()
    v = visitor()
    r = v.post(f"/hooks/in/{token}", json={"name": "Ana Web", "email": "ana@web.example", "message": "Quiero precio", "tags": "web",
                                           "amount_minor": 150000})
    assert r.status_code == 201 and r.json()["ok"] and r.json()["deal_id"]
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.email == "ana@web.example")).mappings().one()
        deal = conn.execute(select(db.deals)).mappings().one()
    assert c["business_id"] == biz["id"] and sorted(c["tags"]) == ["web", "zap"] and not c["marketing_consent"]
    assert deal["contact_id"] == c["id"] and deal["amount_minor"] == 150000
    assert v.post(f"/hooks/in/{token}", data={"name": "Ana Web", "email": "ana@web.example"}).status_code == 201
    assert count("contacts", business_id=biz["id"]) == 1                      # the same person: found, not made twice
    assert count("contacts", business_id=other["id"]) == 0
    assert v.post(f"/hooks/in/{token}", json={"name": "Sin datos"}).status_code == 422
    assert v.post(f"/hooks/in/{token}", content=b"[1,2]", headers={"content-type": "application/json"}).status_code == 400
    assert v.post("/hooks/in/vin_nope", json={"name": "x", "email": "x@x.co"}).status_code == 404
    with db.tx() as conn:
        IN.delete(conn, biz["id"], "t", conn.execute(select(db.inbound_hooks.c.id)).scalar())
    assert v.post(f"/hooks/in/{token}", json={"name": "x", "email": "x@x.co"}).status_code == 404


def test_api_keys_from_connect_apps_are_shown_once_and_kept_hashed(client):
    biz = signed_in(client)
    page = client.post("/app/connect-apps", data={"csrf": tok(client, "/app/connect-apps"), "act": "key", "name": "Mi web"}).text
    key = re.search(r'id="ca-key" dir="ltr">([^<]+)<', page).group(1)
    assert key not in client.get("/app/connect-apps").text
    with db.tx() as conn:
        assert conn.execute(select(db.api_keys.c.key_hash).where(db.api_keys.c.name == "Mi web")).scalar() == crypto.sha256(key)
    assert client.get("/v1/business", headers={"Authorization": f"Bearer {key}"}).status_code == 200


# ================================================================ the export holds everything
def test_every_business_table_is_in_the_export_and_no_secret_is():
    scoped = {n for n, t in db.metadata.tables.items() if "business_id" in t.c and n != "businesses"}
    assert scoped == set(EXP.TABLES) | set(EXP.SKIP)                            # a new table can't be forgotten
    assert not set(EXP.TABLES) & set(EXP.SKIP)
    for t, cols in EXP.HIDDEN.items():
        assert set(cols) <= set(db.metadata.tables[t].c.keys()), t
    biz, ppl = setup_biz()
    with db.tx() as conn:
        plan = IVS.create_plan(conn, biz, "t", name="Mensual", amount_minor=30000)
        IVS.subscribe(conn, biz, "t", plan_id=plan["id"], contact_id=ppl["ana"]["id"])
        IVS.create(conn, biz, "t", title="Uno", lines=[{"description": "x", "qty": 1, "unit_minor": 100}])
        FN.create(conn, biz, "t", "Embudo", "book", "es")
        EM.save(conn, biz, "t", name="Diseño", blocks=[{"t": "text", "text": "hola"}])
        mp = MS.save_plan(conn, biz, "t", {"name": "Club"})
        MS.add(conn, biz, "t", mp["id"], ppl["ana"]["id"])
        AF.save(conn, biz, "t", {"name": "Socia", "kind": "pct", "rate_bp": 500})
        IN.create(conn, biz, "t", name="Zap")
        WH.create(conn, biz, "t", "https://hooks.example.com/x")
        out = EXP.business_data(conn, biz["id"], "t")
    for k in ("invoices", "plans", "subscriptions", "funnels", "funnel_pages", "email_designs", "member_plans", "members",
              "affiliates", "inbound_hooks", "webhook_endpoints", "files", "sequences", "workflows", "projects", "suppliers"):
        assert k in out, k
    assert len(out["plans"]) == 1 and len(out["subscriptions"]) == 1 and len(out["invoices"]) >= 1 and len(out["funnel_pages"]) == 1
    assert "secret" not in out["webhook_endpoints"][0] and "token_hash" not in out["inbound_hooks"][0]
    assert all(k not in out for k in EXP.SKIP)
    json.dumps(out, default=str)


# ================================================================ every query stays inside its business
def test_one_business_never_opens_anothers_things(client):
    a, _ = make_business("MX", "professional", name="A")
    with db.tx() as conn:
        fn = FN.create(conn, a, "t", "F")
        pg = FN.pages(conn, a["id"], fn["id"])[0]
        d = EM.save(conn, a, "t", name="D", blocks=[{"t": "text", "text": "x"}])
        mp = MS.save_plan(conn, a, "t", {"name": "M"})
        co = CO.save(conn, a, "t", {"title": "C"})
        af = AF.save(conn, a, "t", {"name": "Af", "kind": "pct", "rate_bp": 100})
        ad = ADS.save(conn, a, "t", {"name": "Ad", "dest": "https://a.example/x"})
    b = signed_in(client, industry="professional", name="B")
    for path in (f"/app/funnels/{fn['id']}", f"/app/funnels/pages/{pg['id']}", f"/app/emails/{d['id']}", f"/app/members/board/{mp['id']}",
                 f"/app/courses/{co['id']}", f"/app/affiliates/{af['id']}", f"/app/ads/{ad['id']}"):
        assert client.get(path).status_code == 404, path
    for path, data in ((f"/app/funnels/{fn['id']}", {"act": "delete"}), (f"/app/emails/{d['id']}", {"act": "delete"}),
                       (f"/app/courses/{co['id']}", {"act": "delete"}), (f"/app/ads/{ad['id']}", {"act": "delete"}),
                       (f"/app/affiliates/{af['id']}", {"act": "paid", "paid_on": "2026-01-01"})):
        assert client.post(path, data={"csrf": tok(client), **data}).status_code == 404, path
    with db.tx() as conn:
        for fn_, args in ((FN.get, (fn["id"],)), (EM.get, (d["id"],)), (MS.plan, (mp["id"],)), (CO.get, (co["id"],)),
                          (AF.get, (af["id"],)), (ADS.get, (ad["id"],))):
            with pytest.raises(NotFound):
                fn_(conn, b["id"], *args)
        assert FN.list_(conn, b["id"]) == [] and EM.list_(conn, b["id"]) == [] and AF.list_(conn, b["id"]) == []
        assert MS.plans(conn, b["id"]) == [] and CO.list_(conn, b["id"]) == [] and ADS.list_(conn, b["id"]) == []
        assert count("funnels", business_id=a["id"]) == 1 and count("courses", business_id=a["id"]) == 1   # nothing touched


# ================================================================ the registry, the kits, the menu, the words
def test_every_new_tool_is_registered_offered_in_add_more_and_in_the_kits_where_it_fits():
    for k in NEW:
        t = kits.TOOLS[k]
        assert t.group in kits.GROUPS and t.uses and t.owner, k
    assert kits.check_kits() == []
    kit = lambda ind: set(packs.industry(ind).kit.tools)                         # noqa: E731
    assert {"members", "courses"} <= kit("fitness") and {"members", "courses", "affiliates", "funnels"} <= kit("professional")
    assert "affiliates" in kit("shop") and "funnels" in kit("generic") and "connect_apps" in kit("generic")
    for ind in packs.industries():
        k_ = kit(ind)
        assert ("emails" in k_) == ("campaigns" in k_) and ("schedule" in k_) == ("posts" in k_), ind   # beside what they work with
        assert "ads" in k_ or ind in ("design", "professional"), ind


@pytest.mark.parametrize("industry", sorted(packs.industries()))
def test_every_new_menu_link_opens_for_every_kit(client, industry):
    signup(client, industry=industry, email=f"{industry}@example.com")
    client.post("/lang/en", data={"csrf": tok(client)})
    hrefs = nav_hrefs(client)
    page = client.get("/app/add").text
    for k in NEW:
        t = kits.TOOLS[k]
        assert f'id="t-{k}"' in page, (industry, k)
        assert (t.href in hrefs) == (k in packs.industry(industry).kit.tools and kits.visible(
            dict(zip(("industry", "tools", "features", "plan"), (industry, {}, {}, "starter"))), k) or t.href in hrefs), (industry, k)
        for ua in ("Mozilla/5.0 (iPhone)", "Mozilla/5.0 (X11; Linux x86_64)"):
            r = client.get(t.href, headers={"User-Agent": ua}, follow_redirects=False)
            assert r.status_code in (200, 303), (industry, k, r.status_code)
            if r.status_code == 303:
                assert client.get(r.headers["location"]).status_code == 200, (industry, k)
            else:
                assert 'name="viewport"' in r.text and not re.search(r"\{\w+\}", r.text.split("<main", 1)[1]), (industry, k)


def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {k for k in files["en"] if k.startswith(("fn_", "em_", "sc_", "ms_", "co_", "af_", "ad_", "ca_"))}
    keys |= {kits.TOOLS[k].word for k in NEW} | {f"ft_{k}_d" for k in NEW}
    keys |= {f"cg_blk_{k}" for k in ("divider", "spacer", "quote", "social")} | {f"po_net_{n}" for n in PO.HAND}
    assert len(keys) >= 400
    brands = {"po_net_tiktok", "po_net_linkedin", "po_net_x", "ad_p_google", "ad_p_tiktok", "ad_p_meta"}
    for lg in LANGS:
        same = 0
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
            same += lg != "en" and k not in brands and files[lg][k] == files["en"][k]
        assert same <= 6, (lg, same)
    for k in keys - brands:
        if len(files["en"][k]) > 14:
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k
    assert len(set(map(lambda f: frozenset(f), files.values()))) == 1           # every language has exactly the same keys


# ================================================================ one migration, and what is used stays
def _migration():
    path = next((ROOT / "alembic" / "versions").glob("*_ars02_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_one_migration_one_head_and_its_pins_keep_what_a_business_uses(fake_dns):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    mig = _migration()
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1                                     # one head; v0.22 sits on top of this one (intentional)
    assert script.get_revision(mig.revision).down_revision == "4ef016779862"
    assert set(mig.USES) == set(NEW)
    biz, _ = make_business("MX", "design", name="Estudio")                     # a design kit has no Connect other apps
    other, _ = make_business("MX", "design", name="Otro")
    with db.tx() as conn:
        WH.create(conn, biz, "t", "https://hooks.example.com/x")
        p = PO.create(conn, biz, "t", nets=["tiktok"], texts={"tiktok": "hola"})
        PO.approve(conn, biz, "t", p["id"], datetime.now(timezone.utc) + timedelta(days=1))
        conn.execute(update(db.businesses).values(tools={}))
        for sql in mig.pin_statements():
            conn.execute(text(sql))
        assert kits.in_use(conn, biz["id"]) >= {"connect_apps", "schedule"}
    with db.tx() as conn:
        b = B.get(conn, biz["id"])
        assert b["tools"] == {"connect_apps": True, "schedule": True} and B.get(conn, other["id"])["tools"] == {}
        for sql in mig.pin_statements():
            conn.execute(text(sql))
        assert B.get(conn, biz["id"])["tools"] == b["tools"]
