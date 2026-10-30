"""mkt01 (v0.22): the Marketplace view — things sold on Facebook Marketplace as the business's Page, and their buyers.

A buyer who writes gets the item's own answer at once (price, pickup, "when could you come?"), from rules in their
language — or from the business's own AI with the item's facts; a chat is tied to its item by Meta's referral (the
listing / product number), else by the title's words, else Valeria asks "which item?"; the owner hears about each buyer
on their chat, naming the item, and answers with "#3 …" (or plain words to her last question) — passed on only inside
Messenger's 24 h, else she says so plainly; Mark sold tells only the other buyers still inside the 24 h and lists the
rest; a new price is used from the next answer; one business never sees another's items or buyers; the page opens for
every kit, says "connect your Facebook Page" until one is, and every word is in all 11 languages. Meta, Telegram and
the AI are all mocked: nothing leaves the machine."""
import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text, update

from _va import TG_SECRET, Net, link_owner, owner_session, raw_acc, refuse_network, tg_in
from conftest import make_business
from test_web import signup
from valeria_one import db, kits, packs
from valeria_one.channels import HookRequest
from valeria_one.crypto import hmac_sha256_hex
from valeria_one.services import NotFound
from valeria_one.services import ai as AI
from valeria_one.services import autoreply
from valeria_one.services import inbox as X
from valeria_one.services import inventory as INV
from valeria_one.services import marketplace as MK

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
APP_SECRET = "page-app-secret"
NOW = lambda: datetime.now(timezone.utc)                     # noqa: E731


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


class Meta(Net):
    """Net (the owner's Telegram, the business's Gemini) + the Page's Messenger send API."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fb = []

    def __call__(self, req: httpx.Request):
        if "graph.facebook.com" in str(req.url) and req.url.path.endswith("/me/messages"):
            assert req.url.params.get("access_token") == "page-token"
            self.fb.append(json.loads(req.content))
            return httpx.Response(200, json={"recipient_id": self.fb[-1]["recipient"]["id"], "message_id": f"m_out_{len(self.fb)}"})
        return super().__call__(req)

    def to(self, psid) -> list[str]:
        return [m["message"].get("text", "") for m in self.fb if m["recipient"]["id"] == psid]


def setup(industry="shop", country="US", name="Muebles Ana"):
    """A business with its own Facebook Page (Messenger) and its owner on Telegram."""
    biz, _ = make_business(country, industry, name=name)
    with db.tx() as conn:
        tg = X.connect(conn, biz, "t", channel="telegram", name="Bot", cfg={"bot_token": "123:abc", "secret": TG_SECRET})
        fb = X.connect(conn, biz, "t", channel="messenger", name="Muebles Ana",
                       cfg={"page_id": "PAGE1", "page_token": "page-token", "app_secret": APP_SECRET, "verify_token": "vt"})
    net = Meta()
    owner = link_owner(biz, tg, net=net)
    net.tg.clear()
    return biz, tg, fb, owner, net


def add(biz, **data):
    with db.tx() as conn:
        return MK.add(conn, biz, "t", {"title": "Black sofa", "price_minor": 15000, "pickup": "Centro", "icon": "🛋", **data})


_mid = [0]


def fb_in(fb, psid, text=None, net=None, ref=None, at=None, only_ref=False):
    """A buyer writes to the Page on Messenger (signed like Meta does); what happens next runs in line."""
    _mid[0] += 1
    ev = {"sender": {"id": psid}, "recipient": {"id": "PAGE1"}, "timestamp": int((at or NOW()).timestamp() * 1000)}
    if not only_ref:
        ev["message"] = {"mid": f"m_in_{_mid[0]}", "text": text or ""}
        if ref:
            ev["message"]["referral"] = ref
    else:
        ev["referral"] = ref
    raw = json.dumps({"object": "page", "entry": [{"id": "PAGE1", "time": 1, "messaging": [ev]}]}).encode()
    st = X.receive(raw_acc(fb["id"]), HookRequest("POST", "", {"x-hub-signature-256": "sha256=" + hmac_sha256_hex(APP_SECRET, raw)},
                                                  {}, raw))
    h = (net or Meta()).http
    return st, [autoreply.respond(fb["business_id"], cid, http=h) for cid in st.get("conversation_ids", [])]


def buyers(biz):
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.marketplace_buyers).where(db.marketplace_buyers.c.business_id == biz["id"])
                                              .order_by(db.marketplace_buyers.c.ref)).mappings()]


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


# ================================================================ the instant answer
def test_a_buyer_gets_the_items_price_and_pickup_at_once_and_the_owner_hears_which_item():
    biz, tg, fb, owner, net = setup()
    sofa = add(biz)
    st, out = fb_in(fb, "PSID-JUAN", "Hi! Is the black sofa still available?", net)
    assert out == ["marketplace:answered"]
    said = net.to("PSID-JUAN")[-1]
    assert "still available" in said and "$150.00" in said and "Centro" in said and "When could you come?" in said
    b = buyers(biz)[0]
    assert b["item_id"] == sofa["id"] and b["status"] == "asking" and b["ref"] == 1
    with db.tx() as conn:
        assert conn.execute(select(db.conversations.c.source).where(db.conversations.c.id == b["conversation_id"])).scalar() == "marketplace"
        assert conn.execute(select(db.messages.c.by_ai).where(db.messages.c.direction == "out")).scalar() is True
    ping = net.owner_texts()[-1]
    assert ping.startswith("🛋 Black sofa: Buyer #1 is asking about it") and "#1" in ping
    fb_in(fb, "PSID-JUAN", "Is it still available?", net)                         # the same buyer again: no second "new buyer"
    assert sum("is asking about it" in (t or "") for t in net.owner_texts()) == 1


def test_offers_and_on_my_way_set_where_the_buyer_stands_and_she_never_accepts():
    biz, tg, fb, owner, net = setup()
    add(biz)
    fb_in(fb, "PSID-ANA", "black sofa still available?", net)
    st, out = fb_in(fb, "PSID-ANA", "Would you take $100?", net)
    assert out == ["marketplace:answered"]
    assert "offer of $100.00" in net.to("PSID-ANA")[-1] and "check with the owner" in net.to("PSID-ANA")[-1]
    b = buyers(biz)[0]
    assert b["status"] == "offered" and b["offer_minor"] == 10000
    assert "🛋 Black sofa: Buyer #1 offers $100.00." in net.owner_texts()[-1]
    fb_in(fb, "PSID-ANA", "ok I'm on my way, there at 6pm", net)
    b = buyers(biz)[0]
    assert b["status"] == "coming" and b["coming"] == "6pm"
    assert "🛋 Black sofa: Buyer #1 says they're coming 6pm." in net.owner_texts()[-1]
    assert "see you" in net.to("PSID-ANA")[-1].lower()


def test_the_businesss_own_ai_answers_with_the_items_facts(monkeypatch):
    biz, tg, fb, owner, net = setup()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, req: net(req))   # the AI's send uses its own client
    add(biz, notes="Cash only")
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="AIza-own-key", mode="auto")
    net.ai_text = "Yes! It's $150, pickup in Centro. When can you come by?"
    st, out = fb_in(fb, "PSID-AI", "Hi, is the black sofa available?", net)
    assert out == ["marketplace:ai:replied"]
    system = net.ai_calls[-1]["systemInstruction"]["parts"][0]["text"]
    assert "Black sofa" in system and "$150.00" in system and "Centro" in system and "Cash only" in system
    assert "Never accept or make an offer" in system
    assert net.to("PSID-AI")[-1].startswith("Yes! It's $150")
    net.ai_text = None                                                           # the AI busy: the rules answer
    st, out = fb_in(fb, "PSID-AI", "is it available still?", net)
    assert out == ["marketplace:answered"] and "$150.00" in net.to("PSID-AI")[-1]


# ================================================================ which item
def test_a_referral_with_the_listing_number_ties_the_chat_to_its_item():
    biz, tg, fb, owner, net = setup()
    add(biz, title="Bike", price_minor=8000)
    sofa = add(biz, listing="https://www.facebook.com/marketplace/item/1234567890123/?ref=x")
    assert sofa["listing_id"] == "1234567890123"
    st, out = fb_in(fb, "PSID-R", "Hello, is this available?", net, ref={"source": "ADS", "type": "OPEN_THREAD", "product": {"id": "1234567890123"}})
    assert out == ["marketplace:answered"] and buyers(biz)[0]["item_id"] == sofa["id"]
    assert "$150.00" in net.to("PSID-R")[-1]
    # a referral on its own (an open chat): tied, nothing stored as a message, nothing answered
    st, out = fb_in(fb, "PSID-R2", ref={"source": "MARKETPLACE", "listing_id": "1234567890123"}, only_ref=True, net=net)
    assert out == [] and st["messages"] == 0
    b = buyers(biz)[1]
    assert b["item_id"] == sofa["id"]
    with db.tx() as conn:
        assert conn.execute(select(db.messages.c.id).where(db.messages.c.conversation_id == b["conversation_id"])).first() is None
    # a Marketplace referral without a number we know: a buyer still to be asked which item
    fb_in(fb, "PSID-R3", ref={"source": "MARKETPLACE"}, only_ref=True, net=net)
    assert buyers(biz)[2]["item_id"] is None
    # an ad that isn't about any item: not a Marketplace chat
    fb_in(fb, "PSID-R4", ref={"source": "ADS", "ref": "summer"}, only_ref=True, net=net)
    assert len(buyers(biz)) == 3


def test_title_words_match_one_item_deterministically():
    its = [{"id": 1, "title": "Black leather sofa"}, {"id": 2, "title": "Kids bike"}, {"id": 3, "title": "Mesa de comedor"},
           {"id": 4, "title": "Red bike"}, {"id": 5, "title": "黑色沙发"}]
    assert MK.match_title(its, "is the black sofa still for sale?")["id"] == 1       # 2 of 3 words
    assert MK.match_title(its, "¿La mesa de comedor sigue disponible?")["id"] == 3
    assert MK.match_title(its, "la MESA DE COMÉDOR")["id"] == 3                       # case and accents don't matter
    assert MK.match_title(its, "is the bike available?") is None                     # two bikes: not sure
    assert MK.match_title(its, "the red bike please")["id"] == 4
    assert MK.match_title(its, "黑色沙发还在吗")["id"] == 5                              # no spaces: the whole title
    assert MK.match_title(its, "is this available?") is None
    assert MK.pick(its, "2")["id"] == 2 and MK.pick(its, "9") is None
    assert MK.asks("Hola, ¿sigue disponible?") and MK.asks("まだありますか？") and not MK.asks("what are your hours?")


def test_when_its_not_clear_she_asks_which_item_and_the_answer_ties_it():
    biz, tg, fb, owner, net = setup()
    add(biz)
    bike = add(biz, title="Kids bike", price_minor=8000, icon="🚲", pickup="Norte")
    lamp = add(biz, title="Lamp", price_minor=500)
    with db.tx() as conn:
        MK.pause(conn, biz, "t", lamp["id"], True)                               # paused items aren't offered
    st, out = fb_in(fb, "PSID-W", "Hi, is this still available?", net)
    assert out == ["marketplace:which"]
    q = net.to("PSID-W")[-1]
    assert q.startswith("Hi! Which item are you asking about?") and "1) Kids bike — $80.00" in q and "2) Black sofa — $150.00" in q
    assert "Lamp" not in q and net.owner_texts() == []                          # the owner hears once the item is known
    st, out = fb_in(fb, "PSID-W", "the blue one", net)                          # still not clear: asked once, then the owner
    assert out == ["marketplace:quiet"] and len(net.to("PSID-W")) == 1
    assert net.owner_texts()[-1].startswith("🏷️ an item: Buyer #1 wrote: “the blue one”")
    st, out = fb_in(fb, "PSID-W", "1", net)
    assert out == ["marketplace:answered"] and buyers(biz)[0]["item_id"] == bike["id"]
    assert "$80.00" in net.to("PSID-W")[-1] and "Norte" in net.to("PSID-W")[-1]
    assert net.owner_texts()[-1].startswith("🚲 Kids bike: Buyer #1 is asking about it")


def test_a_chat_about_something_else_is_left_to_the_usual_answers_and_the_owner_can_tie_one():
    biz, tg, fb, owner, net = setup()
    sofa = add(biz)
    st, out = fb_in(fb, "PSID-H", "What time do you open on Sunday?", net)
    assert not out[0].startswith("marketplace:") and buyers(biz) == []
    with db.tx() as conn:
        b = MK.tie(conn, biz, "t", st["conversation_ids"][0], sofa["id"])
    assert b["item_id"] == sofa["id"] and b["ref"] == 1


# ================================================================ the owner answers by chat
def test_the_owners_reply_goes_to_the_right_buyer():
    biz, tg, fb, owner, net = setup()
    add(biz)
    bike = add(biz, title="Kids bike", price_minor=8000)
    fb_in(fb, "PSID-1", "black sofa available?", net)                            # #1
    fb_in(fb, "PSID-2", "is the kids bike still available?", net)                # #2
    sent = len(net.to("PSID-1"))
    st, out = tg_in(tg, "900", "#1 ok, 6pm works", net=net)
    assert out == ["answered"] and net.to("PSID-1")[-1] == "ok, 6pm works" and len(net.to("PSID-1")) == sent + 1
    assert net.last() == "✓ Sent to Buyer #1 (Black sofa)."
    assert net.to("PSID-2")[-1].startswith("Yes, it's still available! Kids bike")      # #2 untouched
    # plain words answer her last question (the bike's buyer)
    tg_in(tg, "900", "Perfect, I will be home all afternoon", net=net)
    assert net.to("PSID-2")[-1] == "Perfect, I will be home all afternoon" and "Kids bike" in net.last()
    # a command is still a command; an unknown number says so
    tg_in(tg, "900", "#7 hello", net=net)
    assert "#7" in net.last() and len(net.fb) == 4
    with db.tx() as conn:
        assert conn.execute(text("SELECT count(*) FROM events WHERE type = 'marketplace.relayed' AND business_id = :b"),
                            {"b": biz["id"]}).scalar() == 2
    assert bike["id"] == buyers(biz)[1]["item_id"]


def test_outside_messengers_24_hours_nothing_is_sent_and_the_owner_is_told_plainly():
    biz, tg, fb, owner, net = setup()
    add(biz)
    fb_in(fb, "PSID-OLD", "black sofa available?", net, at=NOW() - timedelta(hours=25))
    assert net.to("PSID-OLD") == []                                              # even Valeria can't answer that late
    before = len(net.fb)
    st, out = tg_in(tg, "900", "#1 still there?", net=net)
    assert len(net.fb) == before
    assert "more than 24 hours ago" in net.last() and "Marketplace" in net.last()


# ================================================================ sold, price, pause
def test_mark_sold_tells_only_the_other_buyers_inside_the_24_hours(client, monkeypatch):
    biz, tg, fb, owner, net = setup()
    sofa = add(biz)
    fb_in(fb, "PSID-A", "black sofa still available?", net)                     # #1 buys it
    fb_in(fb, "PSID-B", "is the black sofa available?", net)                    # #2 inside the window
    fb_in(fb, "PSID-C", "black sofa available?", net, at=NOW() - timedelta(hours=30))   # #3 outside it
    other = add(biz, title="Kids bike")
    fb_in(fb, "PSID-D", "kids bike available?", net)                            # another item's buyer
    a = buyers(biz)[0]
    sent = {p: len(net.to(p)) for p in ("PSID-A", "PSID-B", "PSID-C", "PSID-D")}
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, req: net(req))   # the page's own client, mocked
    r = client.post(f"/app/marketplace/{sofa['id']}", data={"csrf": tok(client, "/app/marketplace"), "act": "sold",
                                                             "buyer_id": str(a["id"])}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/app/marketplace").text
    assert "Told: Buyer #2" in page and "Couldn&#39;t tell (24 h passed): Buyer #3" in page
    assert len(net.to("PSID-A")) == sent["PSID-A"] and len(net.to("PSID-C")) == sent["PSID-C"]
    assert len(net.to("PSID-D")) == sent["PSID-D"]                               # nothing else goes out
    assert net.to("PSID-B")[-1] == "Sorry, Black sofa has been sold. Thank you for your interest!"
    st = {b["ref"]: b["status"] for b in buyers(biz)}
    assert st == {1: "bought", 2: "told_sold", 3: "not_told", 4: "asking"}
    with db.tx() as conn:
        assert MK.item(conn, biz["id"], sofa["id"])["status"] == "sold" and MK.item(conn, biz["id"], other["id"])["status"] == "open"
    client.post(f"/app/marketplace/{sofa['id']}", data={"csrf": tok(client, "/app/marketplace"), "act": "sold"}, follow_redirects=False)
    assert "already sold" in client.get("/app/marketplace").text
    st, out = fb_in(fb, "PSID-E", "is the black sofa still available?", net)  # a new buyer of a sold item: told it's sold
    assert net.to("PSID-E")[-1].startswith("Sorry, Black sofa has been sold")


def test_a_new_price_is_used_from_the_next_answer_and_a_paused_item_is_on_hold(client):
    biz, tg, fb, owner, net = setup()
    sofa = add(biz)
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    client.post(f"/app/marketplace/{sofa['id']}", data={"csrf": tok(client, "/app/marketplace"), "act": "price", "price": "120"})
    fb_in(fb, "PSID-P", "is the black sofa available?", net)
    assert "$120.00" in net.to("PSID-P")[-1] and "$150.00" not in net.to("PSID-P")[-1]
    client.post(f"/app/marketplace/{sofa['id']}", data={"csrf": tok(client, "/app/marketplace"), "act": "pause"})
    fb_in(fb, "PSID-P", "still available?", net)
    assert net.to("PSID-P")[-1] == "Black sofa is on hold right now. I'll let the owner know you asked."
    client.post(f"/app/marketplace/{sofa['id']}", data={"csrf": tok(client, "/app/marketplace"), "act": "unpause"})
    with db.tx() as conn:
        assert MK.item(conn, biz["id"], sofa["id"])["status"] == "open"
        assert conn.execute(text("SELECT data FROM events WHERE type = 'marketplace.changed'")).scalar()["price_to"] == 12000


def test_an_item_from_inventory_comes_with_its_name_price_and_photo_and_selling_counts_it():
    biz, tg, fb, owner, net = setup()
    with db.tx() as conn:
        p = INV.add(conn, biz, "t", {"name": "Lámpara", "price_minor": 4500, "stock": "3"})
        it = MK.add(conn, biz, "t", {"catalog_item_id": p["id"], "pickup": "Sur"})
    assert it["title"] == "Lámpara" and it["price_minor"] == 4500 and it["catalog_item_id"] == p["id"]
    MK.mark_sold(biz, "t", it["id"], http=net.http)
    with db.tx() as conn:
        assert INV.item(conn, biz["id"], p["id"])["stock"] == 2
        MK.reopen(conn, biz, "t", it["id"])
        assert INV.item(conn, biz["id"], p["id"])["stock"] == 3                   # back on sale: back on the shelf
    with db.tx() as conn, pytest.raises(NotFound):
        other, _ = make_business("US", "shop", name="Otra")
        MK.add(conn, other, "t", {"catalog_item_id": p["id"]})                   # another business's product: never


# ================================================================ one business, its own
def test_one_business_never_sees_or_touches_anothers_items_or_buyers(client):
    biz, tg, fb, owner, net = setup(name="A")
    sofa = add(biz)
    fb_in(fb, "PSID-X", "black sofa available?", net)
    b = buyers(biz)[0]
    other, otg, ofb, oowner, onet = setup(name="B")
    with db.tx() as conn:
        for fn, args in ((MK.item, (sofa["id"],)), (MK.buyer, (b["id"],))):
            with pytest.raises(NotFound):
                fn(conn, other["id"], *args)
        assert MK.items(conn, other["id"]) == [] and MK.board(conn, other)["unknown"] == [] and MK.by_ref(conn, other["id"], 1) is None
        with pytest.raises(NotFound):
            MK.assign(conn, other, "t", b["id"], sofa["id"])
    tg_in(otg, "900", "#1 hello from B", net=onet)                               # B's owner can't reach A's buyer #1
    assert onet.fb == [] and "#1" in onet.last()
    owner_session(client, other)
    assert f"m-{sofa['id']}" not in client.get("/app/marketplace").text
    t = tok(client, "/app/marketplace")
    for path, data in ((f"/app/marketplace/{sofa['id']}", {"act": "sold"}), (f"/app/marketplace/{sofa['id']}", {"act": "price", "price": "1"}),
                       (f"/app/marketplace/buyers/{b['id']}", {"status": "coming"})):
        assert client.post(path, data={"csrf": t, **data}).status_code == 404, path
    with db.tx() as conn:
        assert MK.item(conn, biz["id"], sofa["id"])["status"] == "open" and MK.buyer(conn, biz["id"], b["id"])["status"] == "asking"


# ================================================================ the page, the menu, the words
def test_until_a_page_is_connected_it_says_how_and_the_team_can_set_a_status(client):
    biz, _ = make_business("US", "shop", name="Sin página")
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    page = client.get("/app/marketplace", headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
    assert page.status_code == 200 and "Connect your Facebook Page" in page.text and 'href="/app/connect"' in page.text
    assert "personal Facebook profile" in page.text
    biz2, tg, fb, owner, net = setup(name="Con página")
    add(biz2)
    fb_in(fb, "PSID-S", "black sofa available?", net)
    from fastapi.testclient import TestClient
    from valeria_one.api import app
    c2 = TestClient(app, raise_server_exceptions=False, base_url="https://testserver")
    owner_session(c2, biz2, role="staff")
    c2.post("/lang/en", data={"csrf": tok(c2)})
    page = c2.get("/app/marketplace").text
    assert "Connect your Facebook Page" not in page and "Black sofa" in page and "Buyer #1" in page and "Mark sold" not in page
    b = buyers(biz2)[0]
    c2.post(f"/app/marketplace/buyers/{b['id']}", data={"csrf": tok(c2, "/app/marketplace"), "status": "coming"})
    assert buyers(biz2)[0]["status"] == "coming"
    assert c2.post(f"/app/marketplace/{b['item_id']}", data={"csrf": tok(c2, "/app/marketplace"), "act": "sold"},
                   follow_redirects=False).status_code == 303                   # owners only
    with db.tx() as conn:
        assert MK.item(conn, biz2["id"], b["item_id"])["status"] == "open"
        conn.execute(update(db.conversations).values(last_inbound_at=NOW() - timedelta(hours=60)))
    assert "Quiet" in c2.get("/app/marketplace").text


@pytest.mark.parametrize("industry", sorted(packs.industries()))
def test_the_menu_link_opens_for_every_kit(client, industry):
    signup(client, industry=industry, email=f"mk-{industry}@example.com")
    client.post("/lang/en", data={"csrf": tok(client)})
    nav = client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
    assert ('href="/app/marketplace"' in nav) == (industry in ("shop", "florist", "generic", "contractor"))
    assert 'id="t-marketplace"' in client.get("/app/add").text
    for ua in ("Mozilla/5.0 (iPhone)", "Mozilla/5.0 (X11; Linux x86_64)"):
        r = client.get("/app/marketplace", headers={"User-Agent": ua})
        assert r.status_code == 200 and 'name="viewport"' in r.text and not re.search(r"\{\w+\}", r.text.split("<main", 1)[1]), industry
    r = client.post("/app/marketplace", data={"csrf": tok(client, "/app/marketplace"), "title": "Silla", "price": "30", "pickup": "Centro"},
                    follow_redirects=False)
    assert r.status_code == 303 and "Silla" in client.get("/app/marketplace").text


def test_the_tool_is_registered_and_kept_by_its_own_migration():
    t = kits.TOOLS["marketplace"]
    assert t.group == "sell" and t.href == "/app/marketplace" and t.uses and not t.owner
    assert kits.check_kits() == []
    path = next((ROOT / "alembic" / "versions").glob("*_mkt01_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    assert ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads() == [mig.revision]
    assert set(mig.USES) == {"marketplace"}
    biz, _ = make_business("MX", "realtor", name="Casas")                       # a realtor kit has no Marketplace
    with db.tx() as conn:
        MK.add(conn, biz, "t", {"title": "Escritorio", "price_minor": 100000})
        conn.execute(update(db.businesses).values(tools={}))
        for sql in mig.pin_statements():
            conn.execute(text(sql))
        assert conn.execute(select(db.businesses.c.tools).where(db.businesses.c.id == biz["id"])).scalar() == {"marketplace": True}


def test_every_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {k for k in files["en"] if k.startswith("mk_")} | {"marketplace", "ft_marketplace_d"}
    assert len(keys) >= 80
    used = set()
    for p in ("valeria_one/services/marketplace.py", "valeria_one/web/marketplace.py", "valeria_one/web/templates/marketplace.html",
              "valeria_one/web/templates/_mk_fields.html"):
        used |= set(re.findall(r"mk_[a-z0-9_]+[a-z0-9]", (ROOT / p).read_text(encoding="utf-8")))
    used = {k for k in used if k not in ("mk_fields", "mk_st")} | {f"mk_st_{s}" for s in (*MK.STATUSES, "quiet")}
    assert used <= keys, used - keys
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
    for k in keys:
        if len(files["en"][k]) > 14:
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k
    assert len(set(map(frozenset, files.values()))) == 1
