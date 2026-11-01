"""des01 (v0.26.1): design work files in a project — AutoCAD (.dwg, .dxf), SketchUp (.skp), Photoshop (.psd).

Each is checked by its name AND its first bytes, kept byte for byte with its SHA-256 (checked by the database again
before every download), sent in pieces as a download (never opened in the browser), up to 100 MB inside the same 2 GB.
The customer sees the preview the designer picked; the file itself only when "client can download" is ticked.
Photoshop (web) and Photopea on the Apps page."""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import select, text, update

from _va import LANGS
from test_studio import jpeg, owner_with_project, setup_project
from valeria_one import db
from valeria_one.services import Invalid, NotFound
from valeria_one.services import export as EX
from valeria_one.services import files as FL
from valeria_one.web import studio as WS

ROOT = Path(__file__).resolve().parent.parent


def dwg(n=4000):
    return b"AC1032" + b"\x00" * 6 + os.urandom(n)


def dxf_text():
    return b"999\r\nmade by hand\r\n  0\r\nSECTION\r\n  2\r\nHEADER\r\n  9\r\n$ACADVER\r\n  1\r\nAC1027\r\n  0\r\nENDSEC\r\n  0\r\nEOF\r\n"


def dxf_binary():
    return b"AutoCAD Binary DXF\r\n\x1a\x00" + os.urandom(2000)


def skp():
    return b"\xff\xfe\xff\x0e" + "SketchUp Model".encode("utf-16-le") + b"\xff\xfe\xff\x0e{\x00" + os.urandom(3000)


def skp_zip():                                                # a newer SketchUp in a ZIP container: a real ZIP
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("model.dat", os.urandom(2000))
    return b.getvalue()


def psd():
    return b"8BPS\x00\x01" + b"\x00" * 6 + os.urandom(3000)


REAL = {"plano.dwg": (dwg, "application/acad"), "plano.dxf": (dxf_text, "application/dxf"),
        "binario.DXF": (dxf_binary, "application/dxf"), "sala.skp": (skp, "application/vnd.sketchup.skp"),
        "nueva.skp": (skp_zip, "application/vnd.sketchup.skp"),
        "render.psd": (psd, "application/x-photoshop")}


def work(biz, p, name="plano.dwg", raw=None, **kw):
    with db.tx() as conn:
        return FL.save(conn, biz, "test", raw=raw if raw is not None else REAL[name][0](), mime="application/octet-stream",
                       name=name, project_id=p["id"], work=True, **kw)


# ---------------------------------------------------------------- what is accepted, kept and refused
def test_each_type_is_accepted_and_kept_byte_for_byte():
    biz, c, p, r = setup_project()
    for name, (make, mime) in REAL.items():
        raw = make()
        with db.tx() as conn:
            f = FL.save(conn, biz, "test", raw=raw, mime="", name=name, project_id=p["id"], kind="photo", work=True)
            assert (f["kind"], f["mime"], f["size"]) == ("work", mime, len(raw)), name
            assert f["sha256"] == hashlib.sha256(raw).hexdigest() and not f["client_download"] and f["preview_id"] is None
            assert FL.content(conn, biz["id"], f["id"])[0] == raw                      # unchanged: not shrunk, not converted
            assert FL._db_sha(conn, biz["id"], f["id"]) == f["sha256"]
            assert FL.work_label(f["mime"]) and FL.download_name(f).lower().endswith(name.rsplit(".", 1)[1].lower())
        assert b"".join(FL.pieces(biz["id"], f["id"], f["size"])) == raw          # (each piece read on its own, once saved)


def test_a_wrong_signature_is_refused():
    biz, c, p, r = setup_project()
    bad = {"plano.dwg": jpeg(), "virus.psd": b"MZ\x90\x00\x03" + os.urandom(500), "sala.skp": b"MZ\x90\x00" + os.urandom(500),
           "otra.skp": b"PK\x05\x06" + os.urandom(100),
           "plano.dxf": b"\x00\x01binary junk SECTION", "texto.dxf": b"hello world, no sections here\n",
           "old.dwg": b"AC9999", "fake.psd": b"8BPS\x00\x07" + b"\x00" * 50}
    for name, raw in bad.items():
        with pytest.raises(Invalid, match="not a real"):
            work(biz, p, name, raw=raw)
    with db.tx() as conn:                                     # a picture or PDF still goes the old way, a renamed one too
        assert FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", name="foto.jpg", project_id=p["id"], work=True)["kind"] == "photo"
        with pytest.raises(Invalid):                          # every other upload (members, POS, books…) never takes work files
            FL.save(conn, biz, "test", raw=dwg(), mime="application/acad", name="plano.dwg", project_id=p["id"])
        with pytest.raises(Invalid):                          # "work" is only for the real thing
            FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", name="a.jpg", kind="work", work=True)
        assert not [x for x in FL.list_(conn, biz["id"], project_id=p["id"]) if x["kind"] == "work"]


def test_a_newer_sketchup_in_a_zip_container_is_never_refused(client):
    biz, p, tok = owner_with_project(client)
    raw = skp_zip()
    assert raw[:4] == b"PK\x03\x04"
    r = client.post(f"/app/projects/{p['id']}/files", data={"csrf": tok, "kind": "work", "tab": "design"},
                    files={"file": ("Casa 2025.skp", raw, "application/octet-stream")}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        f = FL.list_(conn, biz["id"], project_id=p["id"])[0]
    assert (f["kind"], f["mime"], f["size"], f["sha256"]) == ("work", "application/vnd.sketchup.skp", len(raw), hashlib.sha256(raw).hexdigest())
    d = client.get(f"/app/files/{f['id']}")
    assert d.content == raw and d.headers["content-disposition"].startswith('attachment; filename="Casa 2025.skp"')
    assert d.headers["x-content-type-options"] == "nosniff"
    with pytest.raises(Invalid, match="not a real"):          # the ZIP header is for .skp only: a .psd that is a ZIP is still refused
        work(biz, p, "zip.psd", raw=raw)


def test_size_limit_and_the_business_quota(monkeypatch):
    assert FL.MAX_WORK == 100 * 1024 * 1024 and FL.MAX_IN == 25 * 1024 * 1024
    biz, c, p, r = setup_project()
    big = dwg(30 * 1024 * 1024)                              # above the 25 MB of a photo: a work file may be
    f = work(biz, p, raw=big)
    assert f["size"] == len(big)
    monkeypatch.setattr(FL, "MAX_WORK", 5000)
    with pytest.raises(Invalid, match="100 MB"):
        work(biz, p, raw=dwg(6000))
    monkeypatch.setattr(FL, "MAX_WORK", 100 * 1024 * 1024)
    monkeypatch.setattr(FL, "MAX_PER_BUSINESS", len(big) + 3000)
    with pytest.raises(Invalid, match="2 GB"):                # counted in the same files allowance
        work(biz, p, raw=dwg(4000))
    work(biz, p, raw=dwg(1000))


def test_web_upload_and_download_headers(client):
    biz, p, tok = owner_with_project(client)
    raw = dwg(9 * 1024 * 1024 + 123)                          # three pieces of 4 MB
    r = client.post(f"/app/projects/{p['id']}/files", data={"csrf": tok, "kind": "work", "tab": "design"},
                    files=[("file", ("Plano Ñandú 1.dwg", raw, "application/octet-stream")),
                           ("file", ("render.psd", psd(), "image/vnd.adobe.photoshop"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/app/projects/{p['id']}?tab=design")
    with db.tx() as conn:
        rows = {x["name"]: x for x in FL.list_(conn, biz["id"], project_id=p["id"])}
    f = rows["Plano Ñandú 1.dwg"]
    assert f["kind"] == "work" and rows["render.psd"]["kind"] == "work" and f["created_by"]
    d = client.get(f"/app/files/{f['id']}")
    assert d.status_code == 200 and d.content == raw and hashlib.sha256(d.content).hexdigest() == f["sha256"]
    h = d.headers
    assert h["content-type"] == "application/acad" and h["x-content-type-options"] == "nosniff"
    assert h["content-disposition"] == f"attachment; filename=\"Plano _and_ 1.dwg\"; filename*=UTF-8''{quote('Plano Ñandú 1.dwg', safe='')}"
    assert h["content-length"] == str(len(raw)) and "sandbox" in h["content-security-policy"] and "no-store" in h["cache-control"]
    assert client.get(f"/app/files/{f['id']}?t=1").status_code == 404     # no preview yet: no small picture
    page = client.get(f"/app/projects/{p['id']}?tab=design")
    assert page.status_code == 200 and "Plano Ñandú 1.dwg" in page.text and "AutoCAD DWG" in page.text and "Photoshop" in page.text
    assert f'accept="{FL.WORK_ACCEPT}"' in page.text
    assert FL.WORK_ACCEPT == ".dwg,.dxf,.skp,.psd"
    r = client.post(f"/app/projects/{p['id']}/files", data={"csrf": tok, "kind": "work"},     # a lie: refused, nothing kept
                    files={"file": ("plano.dwg", jpeg(), "image/jpeg")})
    assert "not a real .dwg file" in r.text
    with db.tx() as conn:
        assert len(FL.list_(conn, biz["id"], project_id=p["id"])) == 2


def test_web_refuses_a_too_big_file_before_reading_it(client, monkeypatch):
    biz, p, tok = owner_with_project(client)
    monkeypatch.setattr(FL, "MAX_WORK", 10_000)
    r = client.post(f"/app/projects/{p['id']}/files", data={"csrf": tok},
                    files={"file": ("plano.dwg", dwg(20_000), "application/octet-stream")})
    assert "work files up to 100 MB" in r.text
    with db.tx() as conn:
        assert FL.list_(conn, biz["id"], project_id=p["id"]) == []
    r = client.post(f"/app/projects/{p['id']}/files", headers={"content-length": str(20 * FL.MAX_IN + 1)}, content=b"x")
    assert "too much in one go" in r.text                     # far too much in one form: refused before it is read


def test_damaged_bytes_are_never_sent(client):
    biz, p, tok = owner_with_project(client)
    f = work(biz, p)
    with db.tx() as conn:                                     # (test database) something changed the bytes behind our back
        conn.execute(update(db.files).where(db.files.c.id == f["id"]).values(data=b"AC1032 tampered"))
    r = client.get(f"/app/files/{f['id']}")
    assert "looks damaged" in r.text and "tampered" not in r.text
    with db.tx() as conn:
        assert conn.execute(select(db.events.c.id).where(db.events.c.type == "file.damaged", db.events.c.subject_id == f["id"])).first()
        FL.patch(conn, biz["id"], "test", f["id"], shared=True, client_download=True)
    client.cookies.clear()
    assert client.get(f"/portal/{p['token']}/f/{f['id']}").status_code == 404


# ---------------------------------------------------------------- the customer's portal
def test_client_sees_the_preview_and_downloads_only_when_ticked(client):
    biz, p, tok = owner_with_project(client)
    raw = skp()
    f = work(biz, p, "sala.skp", raw=raw)
    with db.tx() as conn:
        pic = FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", project_id=p["id"])     # not shared itself
    portal = f"/portal/{p['token']}"
    r = client.post(f"/app/files/{f['id']}", data={"csrf": tok, "preview_id": str(pic["id"]), "tab": "design"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/app/files/{f['id']}?t=1").headers["content-type"] == "image/jpeg"    # its preview's small picture
    owner_cookies = dict(client.cookies)
    client.cookies.clear()
    assert client.get(f"{portal}/f/{f['id']}").status_code == 404                       # not shared: nothing
    assert client.get(f"{portal}/f/{pic['id']}").status_code == 404
    assert "sala.skp" not in client.get(portal).text
    with db.tx() as conn:
        FL.patch(conn, biz["id"], "test", f["id"], shared=True)
    page = client.get(portal).text
    assert "sala.skp" in page and f"{portal}/f/{pic['id']}" in page and f'href="{portal}/f/{f["id"]}"' not in page
    assert client.get(f"{portal}/f/{pic['id']}?t=1").status_code == 200                 # the preview: yes
    assert client.get(f"{portal}/f/{f['id']}").status_code == 404                       # the file: not until ticked
    client.cookies.update(owner_cookies)
    r = client.post(f"/app/files/{f['id']}", data={"csrf": tok, "client_download": "1", "tab": "design"}, follow_redirects=False)
    assert r.status_code == 303
    client.cookies.clear()
    page = client.get(portal).text
    assert f'href="{portal}/f/{f["id"]}"' in page
    d = client.get(f"{portal}/f/{f['id']}")
    assert d.status_code == 200 and d.content == raw and d.headers["x-content-type-options"] == "nosniff"
    assert d.headers["content-disposition"].startswith('attachment; filename="sala.skp"')
    assert client.get(f"{portal}/f/{f['id']}?t=1").status_code == 404
    with db.tx() as conn:                                     # un-ticked: gone again; not shared: gone too
        FL.patch(conn, biz["id"], "test", f["id"], client_download=False)
    assert client.get(f"{portal}/f/{f['id']}").status_code == 404


def test_other_businesses_and_clients_never_get_them(client):
    biz, p, tok = owner_with_project(client)
    f = work(biz, p, shared=True)
    with db.tx() as conn:
        FL.patch(conn, biz["id"], "test", f["id"], client_download=True)
    other, oc, op, orr = setup_project()
    of = work(other, op, "render.psd", shared=True)
    with db.tx() as conn:
        FL.patch(conn, other["id"], "test", of["id"], client_download=True)
        p2 = dict(conn.execute(select(db.projects).where(db.projects.c.id == op["id"])).mappings().one())
    assert client.get(f"/app/files/{of['id']}").status_code == 404                    # signed in to the first business
    assert client.post(f"/app/files/{of['id']}", data={"csrf": tok, "client_download": "0"}).status_code == 404
    client.cookies.clear()
    assert client.get(f"/portal/{p['token']}/f/{of['id']}").status_code == 404         # its link, the other's file
    assert client.get(f"/portal/{p2['token']}/f/{f['id']}").status_code == 404
    assert client.get(f"/portal/nope/f/{f['id']}").status_code == 404
    assert client.get(f"/app/files/{f['id']}", follow_redirects=False).status_code == 303   # signed out: the sign-in
    with db.tx() as conn:                                     # even forced public, never on the website
        conn.execute(update(db.files).where(db.files.c.id == f["id"]).values(public=True))
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(site_enabled=True))
        slug = conn.execute(select(db.businesses.c.slug).where(db.businesses.c.id == biz["id"])).scalar()
    assert client.get(f"/s/{slug}/f/{f['id']}").status_code == 404


def test_preview_rules_and_upload(client):
    biz, p, tok = owner_with_project(client)
    f = work(biz, p)
    f2 = work(biz, p, "render.psd")
    other, oc, op, orr = setup_project()
    with db.tx() as conn:
        pdf = FL.save(conn, biz, "test", raw=b"%PDF-1.4 plano", mime="application/pdf", project_id=p["id"], kind="doc")
        elsewhere = FL.save(conn, other, "test", raw=jpeg(), mime="image/jpeg", project_id=op["id"])
        FL.patch(conn, biz["id"], "test", f["id"], preview_id=pdf["id"])
        assert FL.get(conn, biz["id"], f["id"])["preview_id"] == pdf["id"]
        with pytest.raises(NotFound):
            FL.patch(conn, biz["id"], "test", f["id"], preview_id=elsewhere["id"])    # another business's picture
        with pytest.raises(Invalid):
            FL.patch(conn, biz["id"], "test", f["id"], preview_id=f2["id"])           # a work file is no preview
        with pytest.raises(NotFound):
            FL.patch(conn, biz["id"], "test", f["id"], preview_id=f["id"])
        with pytest.raises(Invalid):
            FL.patch(conn, biz["id"], "test", pdf["id"], client_download=True)        # only work files have these
        with pytest.raises(Invalid):
            FL.patch(conn, biz["id"], "test", pdf["id"], preview_id=None)
        with pytest.raises(Invalid):
            FL.patch(conn, biz["id"], "test", f["id"], public=True)
    page = client.get(f"/app/projects/{p['id']}?tab=design").text                     # a PDF preview: a link, not a picture
    assert f'<a href="/app/files/{pdf["id"]}" target="_blank">' in page and f'src="/app/files/{f["id"]}?t=1"' not in page
    r = client.post(f"/app/files/{f2['id']}/preview", data={"csrf": tok, "tab": "design"},
                    files={"file": ("vista.jpg", jpeg(), "image/jpeg")}, follow_redirects=False)
    assert r.headers["location"] == f"/app/projects/{p['id']}?tab=design#work"
    with db.tx() as conn:
        pv = FL.get(conn, biz["id"], FL.get(conn, biz["id"], f2["id"])["preview_id"])
        assert pv["kind"] == "photo" and pv["project_id"] == p["id"] and pv["caption"] == "render.psd"
    assert f'src="/app/files/{f2["id"]}?t=1"' in client.get(f"/app/projects/{p['id']}?tab=design").text   # a picture: shown
    with db.tx() as conn:
        FL.remove(conn, biz["id"], "test", pv["id"])          # the picture deleted: the work file stays, without a preview
        assert FL.get(conn, biz["id"], f2["id"])["preview_id"] is None
    assert client.post(f"/app/files/{pdf['id']}/preview", data={"csrf": tok}, files={"file": ("v.jpg", jpeg(), "image/jpeg")}).status_code == 404


def test_apps_page_has_photoshop_and_photopea_and_import_takes_work_files(client):
    keys = {a[0]: a for a in WS.APPS}
    assert keys["photoshop"][1:] == ("Photoshop (web)", "https://photoshop.adobe.com/", "https://photoshop.adobe.com/", True)
    assert keys["photopea"][1:] == ("Photopea", "https://www.photopea.com/", "https://www.photopea.com/", False)
    biz, p, tok = owner_with_project(client)
    page = client.get("/app/apps").text
    assert "https://photoshop.adobe.com/" in page and "https://www.photopea.com/" in page and 'id="app-photopea"' in page
    client.post("/app/apps/ready", data={"csrf": tok, "app": "photoshop", "on": "1"})
    with db.tx() as conn:
        assert conn.execute(select(db.businesses.c.apps_ready).where(db.businesses.c.id == biz["id"])).scalar() == ["photoshop"]
    raw = skp()
    r = client.post("/app/apps/import", data={"csrf": tok, "project_id": str(p["id"]), "app": "SketchUp", "kind": "render"},
                    files={"file": ("modelo.skp", raw, "application/octet-stream")}, follow_redirects=False)
    assert r.headers["location"] == f"/app/projects/{p['id']}?tab=design"
    with db.tx() as conn:
        f = FL.list_(conn, biz["id"], project_id=p["id"])[0]
        assert (f["kind"], f["caption"], f["size"]) == ("work", "SketchUp", len(raw))


def test_export_holds_the_new_columns_not_the_bytes():
    biz, c, p, r = setup_project()
    f = work(biz, p)
    with db.tx() as conn:
        out = EX.business_data(conn, biz["id"], "t")
    row = next(x for x in out["files"] if x["id"] == f["id"])
    assert row["sha256"] == f["sha256"] and row["client_download"] is False and "preview_id" in row and "data" not in row


def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {k for k in files["en"] if k.startswith("des_")} | {"st_app_photoshop", "st_app_photopea"}
    assert len(keys) == 16
    used = set()
    for p in ("project.html", "portal.html", "_file_tile.html", "apps.html"):
        used |= set(re.findall(r"\bdes_[a-z0-9_]+[a-z0-9]", (ROOT / "valeria_one" / "web" / "templates" / p).read_text(encoding="utf-8")))
    assert used | {"st_app_photoshop", "st_app_photopea"} == keys
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
    for k in keys:
        if len(files["en"][k]) > 14:
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k
    assert len(set(map(frozenset, files.values()))) == 1


def test_every_template_compiles():
    from valeria_one.web import templates
    for n in [n for n in templates.env.list_templates() if n.endswith(".html")]:
        templates.env.get_template(n)


def test_one_migration_on_top_of_booking_that_changes_nothing_existing():
    import importlib.util
    path = next((ROOT / "alembic" / "versions").glob("*_des01_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1 and mig.down_revision == "d5a9c3e7b1f4"     # one head; v0.27 sits on top of this one (intentional)
    with db.tx() as conn:
        cols = {r.column_name: (r.column_default, r.is_nullable) for r in conn.execute(text(
            "SELECT column_name, column_default, is_nullable FROM information_schema.columns WHERE table_name = 'files'"))}
        ck = conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_files_kind'")).scalar()
        fk = conn.execute(text("SELECT confdeltype FROM pg_constraint WHERE conname = 'fk_files_preview_id_files'")).scalar()
    assert cols["client_download"] == ("false", "NO") and cols["sha256"] == (None, "YES") and cols["preview_id"] == (None, "YES")
    assert "'work'" in ck and "'post'" in ck and fk == "n"                     # SET NULL: deleting a preview keeps the work file
