import io
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image
from sqlalchemy import select, update

from conftest import make_business
from valeria_one import blocks, crypto, db
from valeria_one.services import Invalid
from valeria_one.services import ai as AI
from valeria_one.services import businesses as B
from valeria_one.services import meta as MT
from valeria_one.services import posts as PO
from valeria_one.services import social as SO

LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
NOW = lambda: datetime.now(timezone.utc)                     # noqa: E731
ALL = ["pages_manage_posts", "instagram_content_publish", "pages_messaging"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(self, request):
        raise RuntimeError(f"real network call in a test: {request.url}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(PO.time, "sleep", lambda s: None)


class Meta:
    """The Graph API for one Page + its Instagram account, answered in-process."""

    def __init__(self, deny=False, quota=(0, 50)):
        self.calls, self.deny, self.quota, self.n = [], deny, quota, 0

    def __call__(self, req: httpx.Request):
        p = req.url.path
        data = {k: v[0] for k, v in parse_qs(req.content.decode()).items()} if req.content else {}
        self.calls.append((req.method, p, data))
        if self.deny and req.method == "POST":
            return httpx.Response(403, json={"error": {"code": 200, "message": "(#200) Requires pages_manage_posts permission"}})
        self.n += 1
        if p.endswith("/content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"quota_usage": self.quota[0], "config": {"quota_total": self.quota[1]}}]})
        if p.endswith("/feed"):
            return httpx.Response(200, json={"id": f"pg1_{self.n}"})
        if p.endswith("/photos"):
            return httpx.Response(200, json={"id": f"ph{self.n}", "post_id": f"pg1_{self.n}"})
        if p.endswith("/ig1/media"):
            return httpx.Response(200, json={"id": "cont1"})
        if p.endswith("/cont1"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if p.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "igm1"})
        if p.endswith("/igm1"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/abc/"})
        return httpx.Response(404, json={"error": {"code": 100, "message": f"unexpected {p}"}})

    @property
    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def jpeg(w=800, h=600, mode="RGB"):
    b = io.BytesIO()
    Image.new(mode, (w, h), (200, 120, 80) if mode == "RGB" else (200, 120, 80, 128)).save(b, "JPEG" if mode == "RGB" else "PNG")
    return b.getvalue()


def shop(scopes=ALL, ig=True, name="Estética Sol"):
    biz, _ = make_business("MX", "salon", name=name)
    with db.tx() as conn:
        perms = {"scopes": scopes} if scopes is not None else {}
        MT._add(conn, biz, "t", "messenger", "Sol Page", "pg1", {"via": "meta_platform", "page_id": "pg1", "page_token": "ptok", **perms})
        if ig:
            MT._add(conn, biz, "t", "instagram", "@sol", "ig1", {"via": "meta_platform", "ig_user_id": "ig1", "page_id": "pg1",
                                                                "page_token": "ptok", **perms})
        return B.get(conn, biz["id"])


def draft(biz, nets=("facebook", "instagram"), picture=True, texts=None):
    with db.tx() as conn:
        fid = PO.save_picture(conn, biz, "t", jpeg(), "image/jpeg")["id"] if picture else None
        return PO.create(conn, biz, "t", nets=list(nets), file_id=fid,
                         texts=texts or {"facebook": "¡2x1 los martes!", "instagram": "2x1 los martes ✂️ #merida"})


def owner_session(client, biz):
    from valeria_one.services import users as U
    email = f"owner-{str(biz['id'])[:6]}@example.com"
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email=email, name="Dueña", password_hash=U._ph.hash("una-clave-larga"))
                           .returning(db.users.c.id)).scalar()
        U.add_member(conn, biz["id"], uid)
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"csrf": tok, "email": email, "password": "una-clave-larga"})


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


# ---------------------------------------------------------------- the pieces: scopes, block, networks
def test_posting_scopes_are_asked_for():
    for scopes in (MT.LOGIN_SCOPES, SO.FB_SCOPES):
        assert "pages_manage_posts" in scopes and "instagram_content_publish" in scopes
    assert "posts" in blocks.BLOCKS


def test_networks_say_connect_or_reconnect():
    biz, _ = make_business("MX", "salon")
    with db.tx() as conn:
        nets = {n["network"]: n for n in PO.networks(conn, biz)}
    assert not nets["facebook"]["connected"] and not nets["instagram"]["connected"] and not nets["google"]["available"]
    old = shop(scopes=None, name="Conectada antes")                            # connected before v0.12: no permissions on record
    with db.tx() as conn:
        assert all(n["reconnect"] for n in PO.networks(conn, old) if n["network"] != "google")
    new = shop(name="Conectada hoy")
    with db.tx() as conn:
        assert not any(n["reconnect"] for n in PO.networks(conn, new))
    p = draft(biz)
    with db.tx() as conn, pytest.raises(Invalid):
        PO.approve(conn, biz, "t", p["id"])                                    # nothing connected: not approvable


def test_reconnecting_a_page_renews_it_in_place():
    biz = shop(scopes=None)
    pages = [{"id": "pg1", "name": "Sol Page", "token": "ptok-2", "scopes": ALL, "ig": {"id": "ig1", "username": "sol"}}]

    def graph(req):
        return httpx.Response(200, json={"success": True})
    with db.tx() as conn:
        conn.execute(update(db.channel_accounts).values(config=db.channel_accounts.c.config.op("||")(db.func.jsonb_build_object("post_denied", True))))
    import os
    os.environ.update(VO_META_APP_ID="app", VO_META_APP_SECRET="sec", VO_META_VERIFY_TOKEN="vt")
    try:
        with db.tx() as conn:
            MT.connect_pages(conn, biz, "t", pages, ["pg1"], http=httpx.Client(transport=httpx.MockTransport(graph)))
            accs = conn.execute(select(db.channel_accounts).where(db.channel_accounts.c.business_id == biz["id"])).mappings().all()
            nets = {n["network"]: n for n in PO.networks(conn, biz)}
    finally:
        for k in ("VO_META_APP_ID", "VO_META_APP_SECRET", "VO_META_VERIFY_TOKEN"):
            os.environ.pop(k, None)
    assert len(accs) == 2                                                      # the same two accounts, not new ones
    assert all(crypto.open_(a["config"])["page_token"] == "ptok-2" and "post_denied" not in a["config"] for a in accs)
    assert not nets["facebook"]["reconnect"] and not nets["instagram"]["reconnect"]


# ---------------------------------------------------------------- the AI writer (and no AI)
def test_ai_drafts_one_version_per_network_in_the_business_language():
    biz = shop()
    seen = {}

    def gemini(req):
        seen["body"] = json.loads(req.content)
        out = {"facebook": "¡Martes de 2x1 en Estética Sol!", "instagram": "2x1 cada martes ✂️\n#merida #estetica"}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "```json\n" + json.dumps(out) + "\n```"}]}}]})
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="k", mode="suggest", instructions="Tono cercano, tuteamos")
    texts = PO.draft(biz, "t", kind="offer", idea="2x1 los martes de noviembre", nets=["facebook", "instagram"],
                     http=httpx.Client(transport=httpx.MockTransport(gemini)))
    assert texts == {"facebook": "¡Martes de 2x1 en Estética Sol!", "instagram": "2x1 cada martes ✂️\n#merida #estetica"}
    system = seen["body"]["systemInstruction"]["parts"][0]["text"]
    assert "Spanish" in system and "Tono cercano" in system and "never invent" in system
    assert "Never invent prices" in seen["body"]["contents"][0]["parts"][0]["text"]
    assert PO._parse("Solo texto", ["facebook", "google"]) == {"facebook": "Solo texto", "google": "Solo texto"}


def test_without_ai_the_owner_writes_it(client):
    biz = shop()
    owner_session(client, biz)
    page = client.get("/app/posts").text
    assert 'value="ai"' not in page and 'name="text"' in page                # no key: the textarea, no AI button
    tok = csrf(client, "/app/posts")
    r = client.post("/app/posts", data={"csrf": tok, "act": "ai", "kind": "idea", "idea": "Nuevo horario", "net": "facebook"},
                    follow_redirects=False)                                    # asked anyway: a calm note and an empty draft
    assert r.status_code == 303 and r.headers["location"].startswith("/app/posts/")
    with db.tx() as conn:
        p = conn.execute(select(db.posts)).mappings().one()
    assert p["status"] == "draft" and p["texts"] == {} and not p["by_ai"]
    r = client.post("/app/posts", data={"csrf": tok, "act": "manual", "idea": "", "net": "facebook", "text": "Abrimos el domingo"},
                    files={"picture": ("p.png", jpeg(mode="RGBA"), "image/png")}, follow_redirects=False)
    with db.tx() as conn:
        p = conn.execute(select(db.posts).order_by(db.posts.c.created_at.desc())).mappings().first()
        f = conn.execute(select(db.files.c.mime, db.files.c.kind).where(db.files.c.id == p["file_id"])).one()
    assert p["texts"] == {"facebook": "Abrimos el domingo"} and f.mime == "image/jpeg" and f.kind == "post"   # PNG -> JPEG for Instagram


# ---------------------------------------------------------------- publishing
def test_publish_now_on_facebook_and_instagram(client):
    biz = shop()
    m = Meta()
    p = draft(biz)
    out = PO.publish_now(biz, "t", p["id"], http=m.http)
    assert out["status"] == "published" and out["results"]["facebook"]["id"].startswith("pg1_")
    assert out["results"]["facebook"]["url"].startswith("https://www.facebook.com/pg1_")
    assert out["results"]["instagram"]["id"] == "igm1" and out["results"]["instagram"]["url"] == "https://www.instagram.com/p/abc/"
    photo = m.posts("/pg1/photos")[0][2]
    assert photo["message"] == "¡2x1 los martes!" and "/pf/" in photo["url"]
    ig = m.posts("/ig1/media")[0][2]
    assert ig["caption"].startswith("2x1 los martes") and ig["image_url"] == photo["url"] and "is_ai_generated" not in ig
    assert [c[1] for c in m.calls if c[0] == "GET"][0].endswith("/ig1/content_publishing_limit")   # the quota first
    pic = client.get(photo["url"].replace("https://one.test", ""))                                   # Meta can fetch it
    assert pic.status_code == 200 and pic.headers["content-type"] == "image/jpeg"
    bad = re.sub(r"/[0-9a-f]{32}$", "/" + "0" * 32, photo["url"].replace("https://one.test", ""))
    assert client.get(bad).status_code == 404
    expired = PO.signed_url(p["file_id"], NOW() - timedelta(days=PO.SIGNED_DAYS + 1)).replace("https://one.test", "")
    assert client.get(expired).status_code == 404
    with pytest.raises(Invalid):
        PO.publish_now(biz, "t", p["id"], http=m.http)                        # out already: never twice
    assert len(m.posts("/photos")) == 1 and len(m.posts("/media_publish")) == 1


def test_text_only_on_facebook_and_instagram_needs_a_picture():
    biz = shop()
    m = Meta()
    p = draft(biz, nets=("facebook",), picture=False, texts={"facebook": "Hoy cerramos a las 3"})
    out = PO.publish_now(biz, "t", p["id"], http=m.http)
    assert out["status"] == "published" and m.posts("/pg1/feed")[0][2] == {"message": "Hoy cerramos a las 3"}
    q = draft(biz, picture=False)
    with db.tx() as conn, pytest.raises(Invalid):
        PO.approve(conn, biz, "t", q["id"])


def test_scheduled_posts_go_out_once_by_the_job():
    biz = shop()
    m = Meta()
    p = draft(biz)
    with db.tx() as conn:
        PO.approve(conn, biz, "t", p["id"], NOW() + timedelta(hours=2))
    assert PO.publish_due(m.http) == {"published": 0, "failed": 0, "interrupted": 0}       # not yet
    later = NOW() + timedelta(hours=2, minutes=1)
    assert PO.publish_due(m.http, later)["published"] == 1
    assert PO.publish_due(m.http, later + timedelta(minutes=1))["published"] == 0         # every minute, never twice
    assert len(m.posts("/photos")) == 1 and len(m.posts("/media_publish")) == 1
    with db.tx() as conn:
        assert PO.get(conn, biz["id"], p["id"])["status"] == "published"


def test_two_workers_cannot_claim_the_same_post():
    biz = shop()
    p = draft(biz)
    with db.tx() as conn:
        PO.approve(conn, biz, "t", p["id"])
    with db.tx() as a:
        first = PO._claim(a, NOW())
        with db.tx() as b:
            second = PO._claim(b, NOW())                                       # the row is locked / already taken
    assert [x["id"] for x in first] == [p["id"]] and second == []


def test_a_post_interrupted_halfway_is_never_retried_by_itself():
    biz = shop()
    m = Meta()
    p = draft(biz)
    with db.tx() as conn:
        PO.approve(conn, biz, "t", p["id"])
        conn.execute(update(db.posts).where(db.posts.c.id == p["id"]).values(status="publishing", claimed_at=NOW() - timedelta(minutes=20)))
    assert PO.publish_due(m.http)["interrupted"] == 1 and m.calls == []
    with db.tx() as conn:
        assert PO.get(conn, biz["id"], p["id"])["status"] == "failed"
    assert PO.publish_due(m.http)["published"] == 0 and m.calls == []


def test_retry_only_tries_the_network_that_failed():
    biz = shop()
    m = Meta(quota=(50, 50))                                                   # Instagram's daily limit reached
    p = draft(biz)
    out = PO.publish_now(biz, "t", p["id"], http=m.http)
    assert out["status"] == "failed" and out["results"]["facebook"]["id"] and "limit" in out["results"]["instagram"]["error"]
    assert m.posts("/ig1/media") == []                                         # the quota was checked before trying
    m.quota = (3, 50)
    with db.tx() as conn:
        PO.retry(conn, biz, "t", p["id"])
    PO.publish_due(m.http)
    with db.tx() as conn:
        assert PO.get(conn, biz["id"], p["id"])["status"] == "published"
    assert len(m.posts("/photos")) == 1 and len(m.posts("/media_publish")) == 1


def test_missing_permission_says_reconnect_to_allow_posting(client):
    biz = shop()
    m = Meta(deny=True)
    p = draft(biz, nets=("facebook",), picture=False, texts={"facebook": "Hola"})
    out = PO.publish_now(biz, "t", p["id"], http=m.http)
    assert out["status"] == "failed" and out["results"]["facebook"]["reconnect"] and "reconnect" in out["results"]["facebook"]["error"]
    with db.tx() as conn:
        assert {n["network"]: n["reconnect"] for n in PO.networks(conn, biz)}["facebook"]
    owner_session(client, biz)
    page = client.get("/app/posts").text
    assert "Reconnect to allow posting" in page or "Vuelve a conectar para permitir publicar" in page
    today = client.get("/app").text
    assert 'href="/app/posts"' in today                                        # "a post didn't go out"


def test_google_is_one_of_the_networks(monkeypatch):
    from test_gbp import ACC, LOC, Google
    from valeria_one.services import gbp as G
    monkeypatch.setenv("VO_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("VO_GOOGLE_CLIENT_SECRET", "cs")
    monkeypatch.setenv("VO_GOOGLE_BUSINESS", "on")
    g = Google()
    biz = shop()
    with db.tx() as conn:
        G.connect(conn, biz, "t", "c", http=g.http)
        G.choose(conn, biz, "t", ACC, LOC, http=g.http)
    p = draft(biz, nets=("google",), texts={"google": "Abrimos el domingo de 10 a 2"})
    out = PO.publish_now(biz, "t", p["id"], http=g.http)
    assert out["status"] == "published" and out["results"]["google"]["url"] == "https://g.co/post/9"
    body = json.loads([c for c in g.calls if c[2].endswith("/localPosts")][0][3])
    assert body["summary"] == "Abrimos el domingo de 10 a 2" and body["languageCode"] == "es" and "/pf/" in body["media"][0]["sourceUrl"]


def test_public_site_picture_uses_the_site_address():
    from valeria_one.services import files as FL
    from valeria_one.services import site as SI
    biz = shop()
    p = draft(biz)
    with db.tx() as conn:
        assert "/pf/" in PO.image_url(conn, biz, p["file_id"])                # not public: signed and expiring
        biz = SI.save(conn, biz, "t", enabled=True)
        FL.patch(conn, biz["id"], "t", p["file_id"], public=True)
        assert PO.image_url(conn, biz, p["file_id"]) == f"https://one.test/s/{biz['slug']}/f/{p['file_id']}"


def test_editing_an_approved_post_needs_a_new_ok():
    biz = shop()
    p = draft(biz)
    with db.tx() as conn:
        PO.approve(conn, biz, "t", p["id"], NOW() + timedelta(days=1))
        out = PO.save(conn, biz, "t", p["id"], texts={"facebook": "Otro texto"})
    assert out["status"] == "draft" and out["texts"]["facebook"] == "Otro texto"
    assert PO.publish_due(Meta().http, NOW() + timedelta(days=2))["published"] == 0


def test_block_off_hides_posts_and_holds_the_queue(client):
    biz = shop()
    owner_session(client, biz)
    assert 'href="/app/posts"' in client.get("/app").text
    p = draft(biz)
    with db.tx() as conn:
        PO.approve(conn, biz, "t", p["id"])
        B.set_blocks(conn, biz["id"], "t", [b for b in blocks.BLOCKS if b != "posts"])
    m = Meta()
    assert PO.publish_due(m.http)["published"] == 0 and m.calls == []
    with db.tx() as conn:
        assert PO.get(conn, biz["id"], p["id"])["status"] == "draft"
    assert 'href="/app/posts"' not in client.get("/app").text
    assert client.get("/app/posts", follow_redirects=False).headers["location"] == "/app/start"


# ---------------------------------------------------------------- screens
def test_schedule_from_the_screen_and_every_language(client):
    biz = shop()
    owner_session(client, biz)
    p = draft(biz)
    tok = csrf(client, f"/app/posts/{p['id']}")
    day = (NOW() + timedelta(days=30)).date()
    r = client.post(f"/app/posts/{p['id']}", data={"csrf": tok, "act": "schedule", "when": f"{day}T10:30", "net": ["facebook", "instagram"],
                                                   "text_facebook": "¡2x1!", "text_instagram": "2x1 #merida"}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        q = PO.get(conn, biz["id"], p["id"])
    assert q["status"] == "scheduled" and q["publish_at"] == datetime(day.year, day.month, day.day, 16, 30, tzinfo=timezone.utc)   # Mérida time
    assert q["texts"] == {"facebook": "¡2x1!", "instagram": "2x1 #merida"}
    draft(biz)
    for lang in LANGS:
        tok = csrf(client, "/app")
        client.post(f"/lang/{lang}", data={"csrf": tok})
        for path in ("/app/posts", f"/app/posts/{p['id']}"):
            page = client.get(path)
            assert page.status_code == 200 and not re.search(r"\{(network)\}", page.text), (lang, path)
            assert 'href="/app/posts" class="on"' in page.text
