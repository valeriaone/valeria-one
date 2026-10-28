"""Projects and the design studio: the office side, the customer's portal, the AI tools (mocked) and the website."""
import base64
import io
import json
import re
from datetime import date, timedelta

import httpx
import pytest
from PIL import Image
from sqlalchemy import select, update

from conftest import make_business
from test_web import csrf, signup
from valeria_one import blocks, db, packs
from valeria_one.services import Invalid, NotFound
from valeria_one.services import ai as AI
from valeria_one.services import contacts as C
from valeria_one.services import files as FL
from valeria_one.services import projects as PR
from valeria_one.services import studio as ST


def jpeg(w=1200, h=900, color=(200, 180, 150)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


def setup_project(industry="design", country="MX"):
    biz, _ = make_business(country, industry)
    with db.tx() as conn:
        c = C.create(conn, biz, "test", {"name": "Ana Díaz", "phone": "+525512345678"})
        p = PR.create(conn, biz, "test", {"name": "Casa Valle", "contact_id": c["id"], "budget_minor": 10_000_000,
                                          "fee_minor": 1_500_000, "markup_bp": 2000})
        r = PR.add_room(conn, biz, "test", p["id"], {"name": "Sala", "width_cm": 500, "length_cm": 400})
    return biz, c, p, r


# ---------------------------------------------------------------- packs and blocks
def test_design_pack_and_blocks():
    ip = packs.industry("design")
    assert ip.studio and [s.key for s in ip.project_stages][0] == "brief" and ip.project_stages[-1].key == "done"
    assert [s.key for s in packs.industry("salon").project_stages] == ["plan", "work", "review", "done"]
    d, _ = make_business("MX", "design")
    s, _ = make_business("MX", "salon")
    assert blocks.on(d, "studio") and not blocks.on(s, "studio")              # design tools: on only where the pack says
    assert blocks.on(d, "projects") and blocks.on(s, "projects")              # projects: every business
    assert blocks.on({**s, "features": {"studio": True}}, "studio")           # a salon can switch them on itself


# ---------------------------------------------------------------- projects, rooms, plan
def test_project_stages_story_and_isolation():
    biz, c, p, r = setup_project()
    other, _ = make_business("MX", "design")
    with db.tx() as conn:
        assert p["stage"] == "brief" and len(p["token"]) >= 20
        PR.patch(conn, biz, "test", p["id"], {"stage": "concept"})
        with pytest.raises(Invalid):
            PR.patch(conn, biz, "test", p["id"], {"stage": "nope"})
        with pytest.raises(NotFound):
            PR.get(conn, other["id"], p["id"])                               # one business never sees another's
        with pytest.raises(NotFound):
            PR.room(conn, other["id"], r["id"])
        story = PR.story(conn, biz["id"], p["id"])
        assert [s["body"] for s in story if s["kind"] == "stage"] == ["concept", "brief"]
        old = p["token"]
        PR.new_link(conn, biz, "test", p["id"])
        with pytest.raises(NotFound):
            PR.by_token(conn, old)                                             # a renewed link kills the old one


def test_plan_and_palette_are_validated():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        r2 = PR.patch_room(conn, biz, "test", r["id"], {"plan": {"items": [{"label": "Sofá", "x": 10, "y": 20, "w": 220, "h": 90,
                                                                             "color": "#aabbcc", "junk": "<script>"}]}})
        assert r2["plan"] == {"items": [{"label": "Sofá", "shape": "rect", "x": 10.0, "y": 20.0, "w": 220.0, "h": 90.0, "r": 0.0,
                                         "color": "#aabbcc"}]}
        for bad in ({"items": [{"shape": "triangle"}]}, {"items": [{"w": "x"}]}, {"items": [{"w": 0}]}, "nope"):
            with pytest.raises(Invalid):
                PR.patch_room(conn, biz, "test", r["id"], {"plan": bad})
        PR.patch_room(conn, biz, "test", r["id"], {"palette": [{"hex": "#D8C3A5", "name": "Comex Arena"}]})
        assert PR.room(conn, biz["id"], r["id"])["palette"] == [{"hex": "#d8c3a5", "name": "Comex Arena"}]
        with pytest.raises(Invalid):
            PR.patch_room(conn, biz, "test", r["id"], {"palette": [{"hex": "red"}]})


# ---------------------------------------------------------------- selections, money, approvals
def test_selections_markup_totals_and_client_approval():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        sofa = PR.add_item(conn, biz, "test", p["id"], {"name": "Sofá lino", "category": "furniture", "room_id": r["id"],
                                                        "cost_minor": 2_000_000, "qty": 1})
        assert sofa["price_minor"] == 2_400_000                                # cost + the project's 20 % margin
        lamp = PR.add_item(conn, biz, "test", p["id"], {"name": "Lámpara", "category": "lighting", "price_minor": 300_000, "qty": 2})
        t = PR.totals(conn, biz, PR.get(conn, biz["id"], p["id"]))
        assert (t["proposed"], t["approved"], t["fee"], t["all_in"]) == (3_000_000, 0, 1_500_000, 4_500_000)
        assert t["left"] == 10_000_000 - 4_500_000 and t["pct"] == 45
        # the customer, from the portal
        PR.decide(conn, p["token"], sofa["id"], "approved", "¡me encanta!")
        PR.decide(conn, p["token"], lamp["id"], "rejected")
        t = PR.totals(conn, biz, PR.get(conn, biz["id"], p["id"]))
        assert (t["approved"], t["proposed"], t["rejected"]) == (2_400_000, 0, 600_000)
        assert t["margin"] == 2_400_000 - 2_000_000 + 1_500_000
        assert PR.item(conn, biz["id"], sofa["id"])["client_note"] == "¡me encanta!"
        kinds = [s["kind"] for s in PR.story(conn, biz["id"], p["id"], shared_only=True)]
        assert "approved" in kinds and "rejected" in kinds
        # what the customer sees never carries the business's cost or supplier
        seen = PR.items(conn, biz["id"], p["id"], for_client=True)
        assert all("cost_minor" not in i and "supplier_id" not in i for i in seen)
        with pytest.raises(NotFound):
            PR.decide(conn, "wrong-token", sofa["id"], "approved")
        with pytest.raises(Invalid):
            PR.decide(conn, p["token"], sofa["id"], "maybe")


def test_quote_from_selections_and_orders():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        sup = PR.add_supplier(conn, biz, "test", {"name": "Muebles MTY", "phone": "8112345678", "email": "ventas@muebles.mx"})
        assert sup["phone"].startswith("+52")
        a = PR.add_item(conn, biz, "test", p["id"], {"name": "Mesa", "category": "furniture", "cost_minor": 1_000_000,
                                                     "price_minor": 1_300_000, "room_id": r["id"], "supplier_id": sup["id"]})
        b = PR.add_item(conn, biz, "test", p["id"], {"name": "Silla", "category": "furniture", "cost_minor": 100_000,
                                                     "price_minor": 150_000, "qty": 4, "supplier_id": sup["id"]})
        with pytest.raises(Invalid):
            PR.order(conn, biz, "test", p["id"], sup["id"], [a["id"]])           # only approved pieces are ordered
        PR.patch_item(conn, biz, "test", a["id"], {"status": "approved"})
        PR.patch_item(conn, biz, "test", b["id"], {"status": "approved"})
        q = PR.quote_from_selections(conn, biz, "test", p["id"])
        assert [ln["description"] for ln in q["lines"]] == ["Casa Valle", "Sala · Mesa", "Silla"]
        assert q["subtotal_minor"] == 1_500_000 + 1_300_000 + 4 * 150_000 and q["project_id"] == p["id"]
        o = PR.order(conn, biz, "test", p["id"], sup["id"], [a["id"], b["id"]], (date.today() - timedelta(days=1)).isoformat())
        assert o["number"] == 1 and o["total_minor"] == 1_000_000 + 4 * 100_000
        assert {i["status"] for i in PR.po_items(conn, biz["id"], o["id"])} == {"ordered"}
        assert [x["id"] for x in PR.late_orders(conn, biz["id"])] == [o["id"]]
        PR.patch_po(conn, biz, "test", o["id"], {"status": "delivered"})
        assert {i["status"] for i in PR.po_items(conn, biz["id"], o["id"])} == {"delivered"}
        assert PR.late_orders(conn, biz["id"]) == []


def test_cancelled_order_puts_pieces_back():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        a = PR.add_item(conn, biz, "test", p["id"], {"name": "Cortina", "category": "textile", "cost_minor": 50_000})
        PR.patch_item(conn, biz, "test", a["id"], {"status": "approved"})
        o = PR.order(conn, biz, "test", p["id"], None, [a["id"]])
        PR.patch_po(conn, biz, "test", o["id"], {"status": "cancelled"})
        it = PR.item(conn, biz["id"], a["id"])
        assert it["status"] == "approved" and it["po_id"] is None


def test_library_tasks_and_hours():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        cat = PR.add_catalog(conn, biz, "test", {"name": "Tapete yute", "category": "textile", "cost_minor": 400_000,
                                                 "price_minor": 550_000, "width_cm": 200, "depth_cm": 300})
        it = PR.add_item(conn, biz, "test", p["id"], {"catalog_id": cat["id"], "room_id": r["id"], "qty": 2})
        assert (it["name"], it["price_minor"], it["qty"], it["catalog_id"]) == ("Tapete yute", 550_000, 2, cat["id"])
        assert [x["name"] for x in PR.catalog(conn, biz["id"], q="yute")] == ["Tapete yute"]
        t = PR.add_task(conn, biz, "test", {"title": "Medir ventanas", "project_id": p["id"], "kind": "install"})
        PR.done_task(conn, biz, "test", t["id"])
        assert PR.tasks(conn, biz["id"], project_id=p["id"]) == []
        PR.log_hours(conn, biz, "test", p["id"], None, 90)
        with pytest.raises(Invalid):
            PR.log_hours(conn, biz, "test", p["id"], None, 0)
        assert PR.hours(conn, biz["id"], p["id"])["total"] == 90


# ---------------------------------------------------------------- files
def test_files_are_shrunk_and_palette_reads_colours():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        f = FL.save(conn, biz, "test", raw=jpeg(4000, 3000), mime="image/jpeg", project_id=p["id"], room_id=r["id"])
        data, mime = FL.content(conn, biz["id"], f["id"])
        im = Image.open(io.BytesIO(data))
        assert mime == "image/jpeg" and max(im.size) == FL.MAX_SIDE
        th, _ = FL.content(conn, biz["id"], f["id"], thumb=True)
        assert max(Image.open(io.BytesIO(th)).size) == FL.THUMB_SIDE
        with pytest.raises(Invalid):
            FL.save(conn, biz, "test", raw=b"MZ\x90 not a picture", mime="application/octet-stream", project_id=p["id"])
        with pytest.raises(Invalid):
            FL.save(conn, biz, "test", raw=b"<html>", mime="application/pdf", project_id=p["id"])   # a PDF must be a PDF
    two = io.BytesIO()
    im2 = Image.new("RGB", (100, 100), (255, 255, 255))
    for x in range(50):
        for y in range(100):
            im2.putpixel((x, y), (20, 40, 60))
    im2.save(two, "PNG")
    pal = FL.palette(two.getvalue(), 2)
    assert pal == ["#14283c", "#ffffff"]


# ---------------------------------------------------------------- the AI (mocked: tests never call a real provider)
def _gemini_draws(request):
    body = json.loads(request.content)
    assert "generateContent" in str(request.url) and body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    assert body["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "image/jpeg"
    out = base64.b64encode(jpeg(800, 600, (90, 120, 90))).decode()
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "here"}, {"inlineData": {"mimeType": "image/jpeg", "data": out}}]}}]})


def test_visualizer_with_the_business_key():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        f = FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", project_id=p["id"], room_id=r["id"])
    http = httpx.Client(transport=httpx.MockTransport(_gemini_draws))
    with pytest.raises(AI.AIError):
        ST.render_room(biz, "test", f["id"], style="japandi", http=http)        # no key yet: a clear message, no call
    with db.tx() as conn:
        AI.save(conn, biz, "test", provider="gemini", api_key="test-key", mode="suggest")
    out = ST.render_room(biz, "test", f["id"], style="japandi", notes="piso de roble", http=http)
    assert out["kind"] == "render" and out["source_id"] == f["id"] and out["room_id"] == r["id"] and "Japandi" in out["caption"]
    with pytest.raises(Invalid):
        ST.render_room(biz, "test", f["id"], style="bogus", http=http)
    with db.tx() as conn:
        AI.save(conn, biz, "test", provider="anthropic", api_key="k2")
    with pytest.raises(AI.AIError, match="Gemini or OpenAI"):
        ST.render_room(biz, "test", f["id"], style="modern", http=http)       # Claude doesn't draw: said plainly


def test_daily_picture_cap():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        f = FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", project_id=p["id"])
        AI.save(conn, biz, "test", provider="gemini", api_key="k", mode="suggest")
        from valeria_one.services import log
        for _ in range(ST.DAILY_PICTURES):
            log(conn, biz["id"], "studio.rendered", "test", f["id"])
    with pytest.raises(AI.AIError, match="tomorrow"):
        ST.render_room(biz, "test", f["id"], style="modern", http=httpx.Client(transport=httpx.MockTransport(_gemini_draws)))


def test_design_assistant_uses_only_the_facts():
    biz, c, p, r = setup_project()
    seen = {}

    def fake(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Concepto: calma cálida."}]}}]})
    with db.tx() as conn:
        AI.save(conn, biz, "test", provider="gemini", api_key="k", mode="suggest")
        PR.add_item(conn, biz, "test", p["id"], {"name": "Sofá lino", "category": "furniture", "price_minor": 2_400_000})
    out = ST.assist(biz, "test", p["id"], "concept", lang="es", http=httpx.Client(transport=httpx.MockTransport(fake)))
    assert out == "Concepto: calma cálida."
    prompt = seen["body"]["contents"][0]["parts"][0]["text"]
    assert "Casa Valle" in prompt and "Sofá lino" in prompt and "Sala (500×400 cm)" in prompt
    assert "Spanish" in seen["body"]["systemInstruction"]["parts"][0]["text"]
    with db.tx() as conn:
        assert PR.story(conn, biz["id"], p["id"])[0]["body"].startswith("[concept]\n")
        assert not PR.story(conn, biz["id"], p["id"])[0]["shared"]              # drafts are for the team
    with pytest.raises(Invalid):
        ST.assist(biz, "test", p["id"], "shortlist", http=httpx.Client(transport=httpx.MockTransport(fake)))   # empty library


def test_coordinator_reminds_once():
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        a = PR.add_item(conn, biz, "test", p["id"], {"name": "Mesa", "category": "furniture", "cost_minor": 1})
        PR.patch_item(conn, biz, "test", a["id"], {"status": "approved"})
        PR.order(conn, biz, "test", p["id"], None, [a["id"]], (date.today() - timedelta(days=2)).isoformat())
    assert ST.coordinator_round()["reminders"] == 1
    assert ST.coordinator_round()["reminders"] == 0                            # idempotent
    with db.tx() as conn:
        assert [t["created_by"] for t in PR.tasks(conn, biz["id"])] == ["ai:coordinator"]
        ST.set_staff(conn, biz, "test", [])
        conn.execute(update(db.tasks).values(done_at=db.func.now()))
    biz = {**biz, "ai_staff": {k: False for k in ST.STAFF}}
    assert ST.coordinator_round()["reminders"] == 0                            # switched off: nothing


# ---------------------------------------------------------------- boards
def test_boards_validate_their_pieces():
    biz, c, p, r = setup_project()
    other, _ = make_business("MX", "design")
    with db.tx() as conn:
        f = FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", project_id=p["id"])
        b = ST.new_board(conn, biz, "test", p["id"], "Sala cálida", r["id"])
        ST.save_board(conn, biz, "test", b["id"], items=[{"t": "img", "file": str(f["id"]), "x": 1, "y": 2, "w": 30, "h": 40},
                                                         {"t": "color", "hex": "#C8B6A6", "name": "Arena", "x": 50, "y": 2, "w": 10, "h": 10},
                                                         {"t": "text", "text": "Lino y roble", "x": 60, "y": 60, "w": 20, "h": 10}])
        assert len(ST.board(conn, biz["id"], b["id"])["items"]) == 3
        with pytest.raises(Invalid):
            ST.save_board(conn, biz, "test", b["id"], items=[{"t": "script"}])
        with pytest.raises(Invalid):
            ST.save_board(conn, biz, "test", b["id"], items=[{"t": "link", "url": "javascript:alert(1)"}])
        of = FL.save(conn, other, "test", raw=jpeg(), mime="image/jpeg")
        with pytest.raises(Invalid):                                           # another business's picture: refused
            ST.save_board(conn, biz, "test", b["id"], items=[{"t": "img", "file": str(of["id"])}])


# ---------------------------------------------------------------- on screen
def owner_with_project(client):
    signup(client, industry="design", email="vale@example.com")
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        c = C.create(conn, biz, "test", {"name": "Ana", "phone": "+525512345678"})
    tok = csrf(client, "/app/projects")
    r = client.post("/app/projects", data={"csrf": tok, "name": "Casa Ónix", "contact_id": str(c["id"]), "budget": "250,000.50"},
                    follow_redirects=False)
    assert r.status_code == 303
    pid = r.headers["location"].rsplit("/", 1)[1]
    with db.tx() as conn:
        p = PR.get(conn, biz["id"], pid)
    assert p["budget_minor"] == 25_000_050
    return biz, p, tok


def test_owner_screens_render(client):
    biz, p, tok = owner_with_project(client)
    assert "Casa Ónix" in client.get("/app/projects").text
    for tab in PR_TABS:
        r = client.get(f"/app/projects/{p['id']}?tab={tab}")
        assert r.status_code == 200, (tab, r.text[:300])
    client.post(f"/app/projects/{p['id']}/rooms", data={"csrf": tok, "name": "Recámara", "width_cm": "400", "length_cm": "350"})
    with db.tx() as conn:
        room = PR.rooms(conn, biz["id"], p["id"])[0]
    r = client.post(f"/app/projects/{p['id']}/files", data={"csrf": tok, "room_id": str(room["id"]), "tab": "rooms"},
                    files={"file": ("sala.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200
    with db.tx() as conn:
        f = FL.list_(conn, biz["id"], project_id=p["id"])[0]
    assert client.get(f"/app/files/{f['id']}?t=1").headers["content-type"] == "image/jpeg"
    for path in (f"/app/rooms/{room['id']}/plan", f"/app/projects/{p['id']}/present", f"/app/projects/{p['id']}/specs",
                 "/app/tasks", "/app/suppliers", "/app/library", "/app/ai-team"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    r = client.post(f"/app/rooms/{room['id']}/plan", data={"csrf": tok, "plan": json.dumps({"items": [{"label": "Cama", "w": 160, "h": 200}]})},
                    headers={"X-Requested-With": "fetch"})
    assert r.json() == {"ok": True}
    r = client.post(f"/app/projects/{p['id']}/boards", data={"csrf": tok, "title": "Recámara serena"}, follow_redirects=False)
    assert client.get(r.headers["location"]).status_code == 200
    client.post(f"/app/projects/{p['id']}/items", data={"csrf": tok, "name": "Cabecera", "category": "furniture", "price": "8,500",
                                                        "qty": "1", "room_id": str(room["id"]), "to_library": "1"})
    with db.tx() as conn:
        it = PR.items(conn, biz["id"], p["id"])[0]
        assert it["price_minor"] == 850_000 and PR.catalog(conn, biz["id"])[0]["name"] == "Cabecera"


PR_TABS = ("overview", "rooms", "design", "selections", "orders", "tasks", "money")


def test_customer_portal(client):
    biz, p, tok = owner_with_project(client)
    with db.tx() as conn:
        it = PR.add_item(conn, biz, "test", p["id"], {"name": "Sofá verde", "category": "furniture", "price_minor": 1_000_000,
                                                      "cost_minor": 600_000})
        secret = FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", project_id=p["id"])
        shown = FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", project_id=p["id"], shared=True)
    client.cookies.clear()                                                    # the customer: no account, just the link
    page = client.get(f"/portal/{p['token']}")
    assert page.status_code == 200 and "Sofá verde" in page.text and "Casa Ónix" in page.text
    assert "6,000" not in page.text and "600,000" not in page.text           # never the business's cost
    assert client.get(f"/portal/{p['token']}/f/{shown['id']}").status_code == 200
    assert client.get(f"/portal/{p['token']}/f/{secret['id']}").status_code == 404   # not shared: not there
    assert client.get(f"/portal/nope/f/{shown['id']}").status_code == 404
    assert client.get("/portal/nope").status_code == 404
    t2 = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    client.post(f"/portal/{p['token']}", data={"csrf": t2, "act": "approved", "item_id": str(it["id"]), "note": "sí"})
    client.post(f"/portal/{p['token']}", data={"csrf": t2, "act": "comment", "body": "¿Cuándo instalan?"})
    client.post(f"/portal/{p['token']}", data={"csrf": t2, "act": "like", "what": "file", "id": str(shown["id"]), "note": "wow"})
    with db.tx() as conn:
        assert PR.item(conn, biz["id"], it["id"])["status"] == "approved"
        assert FL.get(conn, biz["id"], shown["id"])["reaction"] == "like"
        kinds = [s["kind"] for s in PR.story(conn, biz["id"], p["id"], shared_only=True)]
        assert kinds.count("comment") == 2 and "approved" in kinds
    assert client.get(f"/portal/{p['token']}/present").status_code == 200
    assert client.get(f"/app/projects/{p['id']}", follow_redirects=False).headers["location"] == "/login"


def test_studio_pages_follow_blocks(client):
    signup(client, industry="salon", email="salon@example.com")
    assert client.get("/app/projects").status_code == 200                     # projects: every business
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        p = PR.create(conn, biz, "test", {"name": "Boda"})
        r = PR.add_room(conn, biz, "test", p["id"], {"name": "Jardín", "width_cm": 100, "length_cm": 100})
    assert client.get(f"/app/rooms/{r['id']}/plan", follow_redirects=False).status_code == 303   # design tools off for a salon


def test_must_change_password(client):
    signup(client, industry="design", email="new@example.com")
    with db.tx() as conn:
        conn.execute(update(db.users).values(must_change_password=True))
    assert client.get("/app/projects", follow_redirects=False).headers["location"] == "/app/new-password"
    tok = csrf(client, "/app/new-password")
    client.post("/app/new-password", data={"csrf": tok, "password": "short"})
    with db.tx() as conn:
        assert conn.execute(select(db.users.c.must_change_password)).scalar() is True
    client.post("/app/new-password", data={"csrf": tok, "password": "una-clave-nueva-larga"})
    with db.tx() as conn:
        assert conn.execute(select(db.users.c.must_change_password)).scalar() is False
    assert client.get("/app/projects").status_code == 200


def test_website_work_and_preview(client):
    from valeria_one.services import site as SI
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(slug="av-test"))
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        SI.save(conn, biz, "test", enabled=True, headline="Interiorismo")
        f = FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", project_id=p["id"])
        hidden = FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", project_id=p["id"])
        FL.patch(conn, biz["id"], "test", f["id"], public=True)
        PR.patch(conn, biz, "test", p["id"], {"published": True})
    page = client.get("/s/av-test")
    assert page.status_code == 200 and "Casa Valle" in page.text and f"/s/av-test/f/{f['id']}" in page.text
    assert client.get(f"/s/av-test/f/{f['id']}").status_code == 200
    assert client.get(f"/s/av-test/f/{hidden['id']}").status_code == 404
    assert 'id="preview"' not in page.text                                    # the visitor preview is opt-in
    t = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/s/av-test/preview", data={"csrf": t}, files={"file": ("r.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 404


# ---------------------------------------------------------------- the website assistant (chat bubble)
def _site_biz(assistant=True):
    from valeria_one.services import site as SI
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(slug="av-chat"))
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        SI.save(conn, biz, "test", enabled=True, whatsapp="+528112345678", **({"assistant": "1"} if assistant else {}))
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    return biz


def test_assistant_script_and_offline_answers(client):
    _site_biz(assistant=False)
    assert client.get("/w/av-chat/assistant.js").status_code == 404           # opt-in
    from valeria_one.services import site as SI
    with db.tx() as conn:
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.slug == "av-chat")).mappings().one())
        SI.save(conn, b, "test", assistant="1")
    js = client.get("/w/av-chat/assistant.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/javascript")
    assert "https://wa.me/528112345678" in js.text and "__CFG__" not in js.text and "</script" not in js.text
    r = client.options("/w/av-chat/ask")
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "*"
    r = client.post("/w/av-chat/ask", json={"turns": [{"r": "user", "t": "¿Cuánto cuesta un proyecto?"}]})
    assert r.json() == {"text": None, "reason": "offline"}                    # no AI key: the widget offers its buttons
    assert client.post("/w/av-chat/ask", json={"turns": []}).status_code == 400
    r = client.post("/w/av-chat/lead", json={"name": "Laura", "contact": "8187654321", "message": "Quiero remodelar mi sala"})
    assert r.json() == {"ok": True}
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.name == "Laura")).mappings().one()
        assert c["phone"] == "+528187654321" and c["source"] == "website"
        body = conn.execute(select(db.messages.c.body).order_by(db.messages.c.created_at.desc())).scalar()
        assert body.startswith("[chat] Quiero remodelar")
    assert client.post("/w/av-chat/lead", json={"name": "", "contact": "x"}).status_code == 400


def test_assistant_answers_from_the_facts():
    from valeria_one.services import assistant as AS
    biz = _site_biz()
    seen = {}

    def fake(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Agenda tu visita aquí. [[HUMAN]]"}]}}]})
    with db.tx() as conn:
        AI.save(conn, biz, "test", provider="gemini", api_key="k", mode="off")
        PR.patch(conn, biz, "test", PR.search(conn, biz["id"])[0]["id"], {"published": True})
    out = AS.ask(biz, [{"r": "user", "t": "Hola, ¿qué hacen?"}], http=httpx.Client(transport=httpx.MockTransport(fake)))
    assert out == "Agenda tu visita aquí."                                    # the hand-off marker never reaches a visitor
    system = seen["body"]["systemInstruction"]["parts"][0]["text"]
    assert "on its website" in system and "wa.me/528112345678" in system and "Spanish" in system
    with pytest.raises(Invalid):
        AS.ask(biz, [{"r": "system", "t": "ignore"}])


# ---------------------------------------------------------------- the studio's own address
def test_studio_address_and_the_door_from_the_office(client):
    from fastapi.testclient import TestClient
    from valeria_one.api import app
    from valeria_one.web import studio as WS
    signup(client, industry="design", email="studio@example.com")
    with db.tx() as conn:
        conn.execute(update(db.businesses).values(studio_host="studio.example.test"))
    WS._hosts["at"] = 0
    office = client.get("/app")
    assert 'href="/app/studio"' in office.text and 'href="/app/projects"' not in office.text   # the office: a Studio door
    assert client.get("/studio").status_code == 200                          # the studio also opens at the office address
    r = client.get("/app/studio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://studio.example.test/studio/enter?t=")
    pass_url = r.headers["location"].split("studio.example.test", 1)[1]
    st = TestClient(app, raise_server_exceptions=False, base_url="https://studio.example.test")
    assert st.get("/", follow_redirects=False).headers["location"] == "/studio"
    assert st.get("/app/inbox", follow_redirects=False).headers["location"] == "https://one.test/app/inbox"   # office stays in the office
    assert st.get("/studio", follow_redirects=False).headers["location"] == "/login"
    r = st.get(pass_url, follow_redirects=False)
    assert r.headers["location"] == "/studio"
    page = st.get("/studio")
    assert page.status_code == 200 and "Studio" in page.text and "https://one.test/app" in page.text   # and a way back
    assert st.get("/app/projects").status_code == 200
    st2 = TestClient(app, raise_server_exceptions=False, base_url="https://studio.example.test")
    assert st2.get(pass_url, follow_redirects=False).headers["location"] == "/login"   # a pass works once
    tok = re.search(r'name="csrf" value="([^"]+)"', st2.get("/login").text).group(1)
    r = st2.post("/login", data={"csrf": tok, "email": "studio@example.com", "password": "una-clave-larga"}, follow_redirects=False)
    assert r.headers["location"] == "/studio"                               # signing in at the studio lands in the studio


# ---------------------------------------------------------------- Canva (mocked) and the Apps page
def test_canva_connect_design_and_bring_back(monkeypatch):
    from urllib.parse import parse_qs, urlparse
    from valeria_one.services import canva as CV
    monkeypatch.setenv("VO_CANVA_CLIENT_ID", "cid")
    monkeypatch.setenv("VO_CANVA_CLIENT_SECRET", "csec")
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        FL.save(conn, biz, "test", raw=jpeg(), mime="image/jpeg", kind="render", project_id=p["id"])
        url = CV.start(conn, biz["id"], None, "/app/apps")
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["s256"] and q["client_id"] == ["cid"] and "asset:write" in q["scope"][0]
    state = q["state"][0]
    seen = []

    def canva(request):
        seen.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/oauth/token"):
            body = parse_qs(request.content.decode())
            assert request.headers["authorization"].startswith("Basic ") and body["code_verifier"][0]
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 14400})
        if path.endswith("/users/me/profile"):
            return httpx.Response(200, json={"profile": {"display_name": "Valeria A."}})
        if path.endswith("/asset-uploads"):
            assert request.headers["authorization"] == "Bearer AT" and "name_base64" in request.headers["asset-upload-metadata"]
            return httpx.Response(200, json={"job": {"id": "j1", "status": "in_progress"}})
        if path.endswith("/asset-uploads/j1"):
            return httpx.Response(200, json={"job": {"id": "j1", "status": "success", "asset": {"id": "A1"}}})
        if path.endswith("/designs"):
            assert json.loads(request.content)["asset_id"] == "A1"
            return httpx.Response(200, json={"design": {"id": "D1", "title": "Casa Valle — Moodboard", "urls": {"edit_url": "https://www.canva.com/design/D1/edit"}}})
        if path.endswith("/exports"):
            return httpx.Response(200, json={"job": {"id": "e1", "status": "success", "urls": ["https://export-download.canva.com/x/1.png"]}})
        if request.url.host == "export-download.canva.com":
            b = io.BytesIO(); Image.new("RGB", (800, 600), (10, 20, 30)).save(b, "PNG"); return httpx.Response(200, content=b.getvalue())
        return httpx.Response(404, json={"message": "no"})
    http = httpx.Client(transport=httpx.MockTransport(canva))
    bid, back = CV.finish(state, "CODE", http=http)
    assert bid == biz["id"] and back == "/app/apps"
    with pytest.raises(CV.CanvaError):
        CV.finish(state, "CODE", http=http)                                # a state works once
    with db.tx() as conn:
        lk = CV.link(conn, biz["id"])
        raw = conn.execute(select(db.app_links.c.tokens)).scalar()
    assert lk["account_name"] == "Valeria A." and "AT" not in json.dumps(raw)   # tokens are kept encrypted
    d = CV.new_design(biz, "test", p["id"], "board", http=http)
    assert d["design_id"] == "D1" and d["edit_url"].startswith("https://www.canva.com/")
    assert CV.bring_back(biz, "test", d["id"], http=http) == 1
    with db.tx() as conn:
        back_in = [f for f in FL.list_(conn, biz["id"], project_id=p["id"]) if f["kind"] == "canva"]
        assert len(back_in) == 1 and back_in[0]["caption"] == "Casa Valle — Moodboard"
        assert CV.designs(conn, biz["id"], p["id"])[0]["imported_at"] is not None


def test_canva_refuses_downloads_from_elsewhere(monkeypatch):
    from valeria_one.services import canva as CV
    with pytest.raises(CV.CanvaError):
        CV._download(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"))), "https://evil.example/x.png")
    with pytest.raises(CV.CanvaError):
        CV._download(httpx.Client(), "http://export-download.canva.com/x.png")


def test_apps_page_and_import(client):
    biz, p, tok = owner_with_project(client)
    page = client.get("/app/apps")
    assert page.status_code == 200 and "app.sketchup.com" in page.text and "planner5d.com" in page.text
    assert "/app/canva/connect" not in page.text                              # no Canva app on this server: honest note instead
    r = client.post("/app/apps/import", data={"csrf": tok, "project_id": str(p["id"]), "app": "SketchUp", "kind": "render"},
                    files={"file": ("render.jpg", jpeg(), "image/jpeg")}, follow_redirects=False)
    assert r.headers["location"] == f"/app/projects/{p['id']}?tab=design"
    with db.tx() as conn:
        f = FL.list_(conn, biz["id"], project_id=p["id"])[0]
    assert (f["kind"], f["caption"]) == ("render", "SketchUp")
    assert client.get(f"/app/projects/{p['id']}?tab=design").status_code == 200


def test_assistant_photo(client):
    signup(client, industry="design", email="photo@example.com")
    from valeria_one.services import site as SI
    with db.tx() as conn:
        conn.execute(update(db.businesses).values(slug="av-photo"))
        b = dict(conn.execute(select(db.businesses)).mappings().one())
        SI.save(conn, b, "test", enabled=True, assistant="1")
    tok = csrf(client, "/app/website")
    for _ in range(2):                                                       # a new photo replaces the old one
        r = client.post("/app/website/assistant-photo", data={"csrf": tok}, files={"file": ("me.jpg", jpeg(600, 600), "image/jpeg")},
                        follow_redirects=False)
        assert r.headers["location"] == "/app/website#assistant"
    with db.tx() as conn:
        fid = conn.execute(select(db.businesses.c.site)).scalar()["assistant_photo"]
        assert conn.execute(select(db.func.count()).select_from(db.files).where(db.files.c.caption == "assistant")).scalar() == 1
    assert client.get(f"/s/av-photo/f/{fid}?t=1").status_code == 200
    assert f"/s/av-photo/f/{fid}" in client.get("/w/av-photo/assistant.js").text
    assert client.get("/app/website").status_code == 200


# ---------------------------------------------------------------- Valeria inside the office (any AI: tools are plain JSON)
@pytest.mark.parametrize("provider", ["gemini", "anthropic", "openai"])
def test_copilot_reads_then_proposes_and_the_person_confirms(provider):
    from valeria_one.services import copilot as CP
    biz, c, p, r = setup_project()
    other, _ = make_business("MX", "design")
    calls = []

    def reply(text):
        if provider == "gemini":
            return {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        if provider == "anthropic":
            return {"content": [{"type": "text", "text": text}]}
        return {"choices": [{"message": {"content": text}}]}

    def fake(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200, json=reply('{"say": "Busco…", "read": [{"tool": "projects", "args": {}}]}'))
        seen = json.dumps(body, ensure_ascii=False)
        assert "TOOL RESULTS" in seen and "Casa Valle" in seen                 # the real data reached the AI
        return httpx.Response(200, json=reply('Claro. {"say": "Tienes Casa Valle. ¿Agrego la tarea?", "do": [{"tool": "create_task", '
                                              '"args": {"title": "Medir ventanas", "project_id": "' + str(p["id"]) + '"}}]}'))
    with db.tx() as conn:
        AI.save(conn, biz, "test", provider=provider, api_key="k", mode="off")
    out = CP.chat(biz, None, [{"r": "user", "t": "¿Qué proyectos tengo? agrega tarea medir ventanas"}],
                  http=httpx.Client(transport=httpx.MockTransport(fake)))
    assert out["say"].startswith("Tienes Casa Valle") and len(out["actions"]) == 1
    assert "Medir ventanas" in out["actions"][0]["label"]
    with db.tx() as conn:
        assert PR.tasks(conn, biz["id"]) == []                                 # nothing changed until the person taps
    assert CP.run_write(biz, "user:x", None, out["actions"][0]["token"]).startswith("✓")
    with db.tx() as conn:
        assert [t["title"] for t in PR.tasks(conn, biz["id"])] == ["Medir ventanas"]
    with pytest.raises(Invalid):
        CP.run_write(other, "user:y", None, out["actions"][0]["token"])        # a button only works for its own business
    with pytest.raises(Invalid):
        CP.run_write(biz, "user:x", None, out["actions"][0]["token"] + "x")


def test_copilot_without_a_key_and_on_screen(client):
    from valeria_one.services import copilot as CP
    biz, p, tok = owner_with_project(client)
    assert CP.chat(biz, None, [{"r": "user", "t": "hola"}]) == {"say": None, "reason": "no_ai"}
    page = client.get("/app")
    assert 'id="cp"' in page.text and "/static/copilot.js" in page.text
    assert 'id="cp"' in client.get("/studio").text
    r = client.post("/app/assistant", json={"turns": [{"r": "user", "t": "hola"}]}, headers={"X-CSRF": tok})
    assert r.json() == {"say": None, "reason": "no_ai"}
    assert client.post("/app/assistant", json={"turns": []}, headers={"X-CSRF": "wrong"}).status_code == 403
    assert client.post("/app/assistant/do", json={"token": "nope"}, headers={"X-CSRF": tok}).status_code == 400


def test_pages_open_in_the_business_language(client):
    from valeria_one.services import site as SI
    biz, c, p, r = setup_project()
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(slug="av-es", studio_host="studio.es.test"))
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        SI.save(conn, b, "test", enabled=True, assistant="1")
    en = {"Accept-Language": "en-US,en;q=0.9"}
    assert "Escríbenos" not in client.get("/s/av-es", headers=en).text                   # follows the browser by default
    with db.tx() as conn:
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        SI.save(conn, b, "test", lang_fixed="1")
    from valeria_one.web import studio as WS
    WS._hosts["at"] = 0
    assert 'lang="es"' in client.get("/s/av-es", headers=en).text                         # now: Spanish whatever the browser
    assert 'lang="es"' in client.get(f"/portal/{p['token']}", headers=en).text
    assert '"fixed": true' in client.get("/w/av-es/assistant.js").text
    from fastapi.testclient import TestClient
    from valeria_one.api import app
    st = TestClient(app, raise_server_exceptions=False, base_url="https://studio.es.test")
    assert 'lang="es"' in st.get("/login", headers=en).text                                # the studio's sign-in too
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/s/av-es").text).group(1)
    client.post("/lang/en", data={"csrf": tok})
    assert 'lang="en"' in client.get("/s/av-es").text                                      # a person's own choice still wins
