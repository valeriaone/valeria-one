"""face01: the operator's default face on Valeria's button (VO_ASSISTANT_PHOTO = "<business id>/<file id>"), for every
business without its own photo. The picture lives in the server's database, never in the code."""
import io

from PIL import Image
from sqlalchemy import select

from test_web import signup
from valeria_one import db
from valeria_one.services import files as FS


def _photo(biz):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 90)).save(buf, "JPEG")
    with db.tx() as conn:
        return FS.save(conn, biz, "test", raw=buf.getvalue(), mime="image/jpeg", name="valeria.jpg", kind="photo")


def _biz():
    with db.tx() as conn:
        return dict(conn.execute(select(db.businesses)).mappings().first())


def test_no_setting_means_the_v(client, monkeypatch):
    monkeypatch.delenv("VO_ASSISTANT_PHOTO", raising=False)
    signup(client)
    t = client.get("/app").text
    assert 'class="cp-b"' in t and "<i>V</i>" in t and "/assistant-face.jpg" not in t
    assert client.get("/assistant-face.jpg").status_code == 404


def test_default_face_shows_on_every_business_without_its_own(client, monkeypatch):
    signup(client)
    b = _biz()
    f = _photo(b)
    monkeypatch.setenv("VO_ASSISTANT_PHOTO", f"{b['id']}/{f['id']}")
    t = client.get("/app").text
    assert t.count('<img src="/assistant-face.jpg" alt="">') == 2          # the button and the panel header
    r = client.get("/assistant-face.jpg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/") and len(r.content) > 100


def test_bad_or_missing_setting_is_a_quiet_404(client, monkeypatch):
    for v in ("nonsense", "not-a-uuid/also-not", "00000000-0000-0000-0000-000000000000/00000000-0000-0000-0000-000000000000"):
        monkeypatch.setenv("VO_ASSISTANT_PHOTO", v)
        assert client.get("/assistant-face.jpg").status_code == 404


def test_website_phone_shows_the_face_too(client, monkeypatch):
    # face02 (Tad 2026-10-30 "also in valeriaone"): the hero phone on the public website, every language
    from test_selfhost import do_setup
    monkeypatch.setenv("VO_MARKETING", "on")
    monkeypatch.setenv("VO_BILLING", "off")
    monkeypatch.delenv("VO_ASSISTANT_PHOTO", raising=False)
    do_setup(client)
    client.cookies.clear()
    h = {"Accept-Language": "en", "User-Agent": "Mozilla/5.0 Chrome/126.0"}
    assert '<span class="dv-av">V</span>' in client.get("/", headers=h).text
    b = _biz()
    f = _photo(b)
    monkeypatch.setenv("VO_ASSISTANT_PHOTO", f"{b['id']}/{f['id']}")
    for path in ("/", "/mx", "/fil", "/ar"):
        assert '<span class="dv-av"><img src="/assistant-face.jpg" alt=""></span>' in client.get(path, headers=h).text, path
