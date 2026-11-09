"""ponla01 (Tad 2026-11-09 «build the whatsapp ponla en mi pagina»): the owner sends Valeria a photo or a video saying «ponla en mi
página» → it goes straight onto their page (photo → «Nuestro trabajo», video → the top). Without those words a photo stays a post
idea and a video gets a hint. Through the owner's own chat (Telegram here; WhatsApp is the same path)."""
import io

import pytest
from PIL import Image
from sqlalchemy import select

from _va import Net, link_owner, refuse_network, shop
from test_ow01 import tg_raw
from valeria_one import db
from valeria_one.services import site as SI
from valeria_one.web.i18n import t

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(range(256)) * 40


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def _jpg():
    b = io.BytesIO(); Image.new("RGB", (80, 60), "green").save(b, "JPEG"); return b.getvalue()


def _biz(biz):
    with db.tx() as conn:
        return dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())


def test_the_words():
    for yes in ("ponla en mi página", "Ponla en mi pagina porfa", "súbela a la web", "add it to my page", "pa la galería", "para la página"):
        assert SI.wants_page(yes), yes
    for no in ("nueva cabecera queen $2800", "esta es la página 3 del menú", "post this on instagram", ""):
        assert not SI.wants_page(no), no


def test_photo_with_the_words_goes_onto_the_page():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    net.files["p1"] = _jpg()
    assert tg_raw(tg, {"photo": [{"file_id": "p1", "width": 80, "height": 60}], "caption": "ponla en mi página"}, net) == ["page_photo"]
    b = _biz(biz)
    assert b["site_enabled"] and b["slug"]
    assert net.last() == t("va_page_photo", "es").format(link=f"https://one.test/s/{b['slug']}#work") or "Nuestro trabajo" in net.last()
    with db.tx() as conn:
        p = conn.execute(select(db.projects).where(db.projects.c.business_id == biz["id"])).mappings().one()
        f = conn.execute(select(db.files).where(db.files.c.project_id == p["id"])).mappings().one()
    assert p["name"] == "Nuestro trabajo" and p["published"] and f["public"] and f["mime"] == "image/jpeg"
    assert not net.gw                                                               # nobody else was messaged


def test_photo_without_the_words_stays_a_post_idea():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    net.files["p2"] = _jpg()
    assert tg_raw(tg, {"photo": [{"file_id": "p2", "width": 80, "height": 60}], "caption": "nueva cabecera queen"}, net) == ["photo"]
    with db.tx() as conn:
        assert conn.execute(select(db.projects.c.id).where(db.projects.c.business_id == biz["id"])).first() is None


def test_video_with_the_words_plays_at_the_top_and_replaces_the_old():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    net.files["v1"] = MP4
    net.files["v2"] = MP4 + b"x"
    assert tg_raw(tg, {"video": {"file_id": "v1", "mime_type": "video/mp4"}, "caption": "ponla en mi página"}, net) == ["page_video"]
    v1 = _biz(biz)["site"]["hero_video"]
    assert "arriba de su página" in net.last()
    assert tg_raw(tg, {"video": {"file_id": "v2", "mime_type": "video/mp4"}, "caption": "y esta también en mi página"}, net) == ["page_video"]
    b = _biz(biz)
    assert b["site"]["hero_video"] != v1
    with db.tx() as conn:
        assert conn.execute(select(db.files.c.id).where(db.files.c.id == v1)).first() is None


def test_video_without_the_words_gets_a_hint_and_nothing_changes():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    net.files["v3"] = MP4
    assert tg_raw(tg, {"video": {"file_id": "v3", "mime_type": "video/mp4"}}, net) == ["video_hint"]
    assert net.last() == t("va_page_hint", "es") and "hero_video" not in (_biz(biz)["site"] or {})


def test_a_fake_video_is_refused_plainly():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    net.files["v4"] = b"this is not a video" * 20
    assert tg_raw(tg, {"video": {"file_id": "v4", "mime_type": "video/mp4"}, "caption": "ponla en mi página"}, net) == ["page_video"]
    assert net.last().startswith("No pude ponerla en su página") and "hero_video" not in (_biz(biz)["site"] or {})
