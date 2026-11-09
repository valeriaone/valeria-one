"""page01 (Tad 2026-11-09 «edit their websites in their office … upload videos, pics … the look and feel»): «Mi página» — the look,
the order of the parts, a video link, photos straight into «Our work», a top video served in byte ranges, the live preview."""
import io
import re

import pytest
from PIL import Image
from sqlalchemy import select, update

from _va import owner_session
from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import site as ST

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(range(256)) * 40


def _jpg(color="red"):
    b = io.BytesIO(); Image.new("RGB", (64, 48), color).save(b, "JPEG"); return b.getvalue()


def _owner(client):
    biz, _ = make_business("MX", "design", name="Taller Página")
    owner_session(client, biz)
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app/website").text).group(1)
    client.post("/lang/es", data={"csrf": tok})
    client.post("/app/website", data={"csrf": tok, "enabled": "1", "headline": "Cabeceras a la medida"})
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    return biz, tok


def test_settings_are_checked():
    biz, _ = make_business("MX", "salon")
    with db.tx() as conn:
        b = ST.save(conn, biz, "t", look="warm", sections="contact,work", video_url="https://youtu.be/dQw4w9WgXcQ")
        assert b["site"]["look"] == "warm" and b["site"]["sections"] == "contact,work"
        for bad in ({"look": "neon"}, {"sections": "work,work"}, {"sections": "footer"}, {"video_url": "https://evil.com/x.mp4"},
                    {"video_url": "http://youtube.com/watch?v=dQw4w9WgXcQ"}, {"hero_video": "not-a-file"}):
            with pytest.raises(Invalid):
                ST.save(conn, b, "t", **bad)
    assert ST.sections({"sections": "contact,work"}) == ["contact", "work", "about", "services", "video"]
    assert ST.youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ" and ST.youtube_id("https://vimeo.com/1") is None


def test_photos_and_video_go_onto_the_page(client):
    biz, tok = _owner(client)
    r = client.post("/app/website/media", data={"csrf": tok}, files=[("file", ("a.jpg", _jpg(), "image/jpeg")), ("file", ("b.jpg", _jpg("blue"), "image/jpeg"))],
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#media")
    with db.tx() as conn:
        p = conn.execute(select(db.projects).where(db.projects.c.business_id == biz["id"])).mappings().one()
        pics = conn.execute(select(db.files).where(db.files.c.project_id == p["id"])).mappings().all()
        tools = conn.execute(select(db.businesses.c.tools).where(db.businesses.c.id == biz["id"])).scalar()
    assert p["name"] == "Nuestro trabajo" and p["published"] and len(pics) == 2 and all(x["public"] for x in pics) and tools["projects"]
    page = client.get(f"/s/{biz['slug']}").text
    assert "Nuestro trabajo" in page and str(pics[0]["id"]) in page
    # a video becomes the top video; a second one replaces it and the old file goes
    client.post("/app/website/media", data={"csrf": tok}, files=[("file", ("v.mp4", MP4, "video/mp4"))])
    with db.tx() as conn:
        v1 = conn.execute(select(db.businesses.c.site).where(db.businesses.c.id == biz["id"])).scalar()["hero_video"]
    client.post("/app/website/media", data={"csrf": tok}, files=[("file", ("v2.mov", MP4, "video/quicktime"))])
    with db.tx() as conn:
        v2 = conn.execute(select(db.businesses.c.site).where(db.businesses.c.id == biz["id"])).scalar()["hero_video"]
        gone = conn.execute(select(db.files.c.id).where(db.files.c.id == v1)).first()
    assert v1 != v2 and gone is None
    page = client.get(f"/s/{biz['slug']}").text
    assert f'<video class="hero-vid" src="/s/{biz["slug"]}/f/{v2}"' in page and "autoplay muted loop playsinline" in page
    # a «video» that isn't one is refused
    r = client.post("/app/website/media", data={"csrf": tok}, files=[("file", ("x.mp4", b"not a video at all" * 10, "video/mp4"))])
    assert r.status_code in (200, 303, 400) and "videos: MP4, MOV or WebM" in r.text
    with db.tx() as conn:
        assert conn.execute(select(db.businesses.c.site).where(db.businesses.c.id == biz["id"])).scalar()["hero_video"] == v2


def _site(biz):
    with db.tx() as conn:
        return conn.execute(select(db.businesses.c.site).where(db.businesses.c.id == biz["id"])).scalar()


def test_video_plays_in_pieces(client):
    biz, tok = _owner(client)
    client.post("/app/website/media", data={"csrf": tok}, files=[("file", ("v.mp4", MP4, "video/mp4"))])
    vid = _site(biz)["hero_video"]
    u = f"/s/{biz['slug']}/f/{vid}"
    full = client.get(u)
    assert full.status_code == 200 and full.content == MP4 and full.headers["accept-ranges"] == "bytes" and full.headers["content-type"] == "video/mp4"
    r = client.get(u, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == MP4[:10] and r.headers["content-range"] == f"bytes 0-9/{len(MP4)}"
    r = client.get(u, headers={"Range": "bytes=-5"})
    assert r.status_code == 206 and r.content == MP4[-5:]
    r = client.get(u, headers={"Range": f"bytes={len(MP4)}-"})
    assert r.status_code == 416


def test_look_order_and_video_link_on_the_page(client):
    biz, tok = _owner(client)
    client.post("/app/website/media", data={"csrf": tok}, files=[("file", ("v.mp4", MP4, "video/mp4"))])
    vid = _site(biz)["hero_video"]
    r = client.post("/app/website", data={"csrf": tok, "enabled": "1", "headline": "Cabeceras", "about": "Taller CNC", "look": "elegant",
                                          "sections": "contact,video,about,services,work", "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert r.status_code in (200, 303)
    s = _site(biz)
    assert s["look"] == "elegant" and s["hero_video"] == vid                    # saving the form never drops the uploaded video
    page = client.get(f"/s/{biz['slug']}").text
    assert 'class="look-elegant"' in page and "youtube-nocookie.com/embed/dQw4w9WgXcQ" in page
    body = page.split("<main", 1)[1]
    assert body.index('id="contact"') < body.index('id="video"') < body.index("Taller CNC")
    tik = client.post("/app/website", data={"csrf": tok, "enabled": "1", "video_url": "https://www.tiktok.com/@decomex/video/123"})
    assert tik.status_code in (200, 303) and "Vea nuestro video" in client.get(f"/s/{biz['slug']}").text


def test_editor_shows_preview_and_removes(client):
    biz, tok = _owner(client)
    client.post("/app/website/media", data={"csrf": tok}, files=[("file", ("a.jpg", _jpg(), "image/jpeg")), ("file", ("v.mp4", MP4, "video/mp4"))])
    ed = client.get("/app/website").text
    assert "Mi página" in ed and f'<iframe src="https://' in ed and 'id="media"' in ed and 'id="look"' in ed and 'id="pg-order"' in ed
    pic = re.search(r'/app/website/media/([0-9a-f-]{36})/remove', ed.split('class="pg-gal"')[1].split("</video>")[1]).group(1)
    vid = _site(biz)["hero_video"]
    client.post(f"/app/website/media/{pic}/remove", data={"csrf": tok})
    client.post(f"/app/website/media/{vid}/remove", data={"csrf": tok})
    with db.tx() as conn:
        assert conn.execute(select(db.files.c.id).where(db.files.c.id.in_([pic, vid]))).first() is None
    assert "hero_video" not in _site(biz)
    other, _ = make_business("MX", "salon", name="Otro")             # never someone else's file
    from valeria_one.services import files as FL
    with db.tx() as conn:
        f = FL.save(conn, other, "t", raw=_jpg(), mime="image/jpeg", name="x.jpg")
    assert client.post(f"/app/website/media/{f['id']}/remove", data={"csrf": tok}).status_code == 404
