"""The Marketplace view (mkt01, v0.22): things the business sells on Facebook Marketplace, and the buyers who write.

Only listings posted as the business's own Facebook Page reach it: Marketplace chats to a PERSONAL profile have no API.
Buyers write to the Page on Messenger; the chat arrives like any other (channels/meta.py, inbox.py), and here it is tied
to an item: by what Meta says about where the chat came from (a referral carrying the listing / product number the owner
pasted, when Meta sends one), else by the item's title words in what the buyer wrote, else Valeria asks "which item?"
listing the open ones; the owner can also tie a chat to an item with one tap.

What Valeria does by herself is only ANSWER A BUYER WHO WROTE FIRST, inside Messenger's 24 h: "Yes, still available!
$150, pickup in Centro. When could you come?" — from the item's own price, pickup and delivery (rules, in the buyer's
language and the owner's style; the business's own AI with the item's facts when its AI answers on "auto"). She never
accepts an offer. sch01 (v0.23): when the business takes bookings, "When could you come?" becomes the pickup times that are
really free, and the buyer's answer books the pickup in both calendars (chatbook.py / chatcal.py). The owner hears about each buyer on their chat (owner.py: WhatsApp, Telegram…) with a short number
("#3"); "#3 ok, 6pm works" — or plain words to her last question — goes to that buyer as written, only inside the 24 h
(outside it she says so plainly). Mark sold tells every other buyer of that item still inside the 24 h, and lists the
rest; nothing else is ever sent by itself.

Where a buyer stands comes from rules on what they wrote (an amount with offer words = offered, "on my way" = coming)
and the owner's taps; "quiet" = no word for QUIET_HOURS. The product stays the business's own (catalog_items: Library /
Inventory); a listing only adds what Marketplace needs (its own price, pickup, notes, the listing number, sold or not).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import blocks, channels, db, money, packs
from . import Blocked, Invalid, NotFound, log

IT, BY = db.marketplace_items, db.marketplace_buyers
CHANNELS = ("messenger", "sandbox")                          # Marketplace chats reach a Page on Messenger (sandbox: the demo)
QUIET_HOURS = 48
MAX_PER_HOUR = 3                                             # her own answers per chat per hour (like services/answers.py)
GAP_HOURS = 12                                               # "I'll check with the owner" once per chat per 12 h
MAX_WHICH = 10                                               # "which item?" lists at most this many
STATUSES = ("asking", "offered", "coming", "bought", "told_sold", "not_told")
TAPS = ("asking", "offered", "coming")                       # what the owner may set by hand (sold goes through mark_sold)
DEFAULT_ICON = "🏷️"

# "Is this still available?" — Marketplace's own first message, in the 11 languages of the screens
ASKS = ("still available", "is this available", "is it available", "available?", "still for sale", "still have",
        "sigue disponible", "está disponible", "esta disponible", "aún disponible", "aun disponible", "todavía lo tienes",
        "todavia lo tienes", "lo sigues vendiendo", "disponible?", "ainda está disponível", "ainda esta disponivel",
        "está disponível", "esta disponivel", "disponível?", "toujours disponible", "est-ce disponible", "encore disponible",
        "noch verfügbar", "noch verfugbar", "noch zu haben", "ist das verfügbar", "available pa", "meron pa", "mayroon pa",
        "masih ada", "masih tersedia", "apakah tersedia", "उपलब्ध है", "अभी भी उपलब्ध", "मिलेगा", "لا يزال متاح", "هل هو متاح",
        "متوفر", "متاح", "まだありますか", "まだ購入できますか", "在庫ありますか", "还有吗", "还在吗", "还在卖", "有货吗")
OFFERS = ("would you take", "will you take", "can you do", "could you do", "i'll give", "i will give", "i can give", "i can pay",
          "i offer", "my offer", "best price", "lowest", "me lo dejas", "lo dejas en", "lo dejarías", "te doy", "te ofrezco",
          "ofrezco", "último precio", "ultimo precio", "aceitaria", "aceita", "faz por", "deixa por", "ofereço", "dou",
          "je vous propose", "je propose", "je vous offre", "dernier prix", "würden sie", "nehmen sie", "biete", "letzter preis",
          "pwede ba", "tawad", "last price", "bisa kurang", "nego", "tawar", "ऑफर", "दूंगा", "दे सकता", "آخر سعر", "أعطيك",
          "أدفع", "値下げ", "で買えますか", "でどうですか", "便宜", "最低", "出价", "可以吗")
COMING = ("on my way", "omw", "coming now", "i'm coming", "im coming", "be there", "i'll come", "i will come", "heading over",
          "pick it up at", "pick up at", "voy en camino", "en camino", "voy para allá", "voy para alla", "llego", "paso a las",
          "paso por", "voy a las", "estoy llegando", "a caminho", "chego", "passo aí", "passo ai", "j'arrive", "je passe",
          "en route", "unterwegs", "ich komme", "bin gleich da", "papunta na", "otw", "darating ako", "dalam perjalanan",
          "saya datang", "आ रहा", "आ रही", "रास्ते में", "في الطريق", "قادم", "جاي", "向かって", "今行", "行きます", "在路上",
          "马上到", "我过来")
_STOP = {"the", "and", "for", "with", "this", "that", "una", "uno", "con", "para", "por", "los", "las", "del", "com", "des",
         "les", "une", "der", "die", "das", "und", "mit", "ang", "mga", "dan", "yang"}
_AMOUNT = re.compile(r"(?<![\d:.,])([$€£¥₱₹]\s*)?(\d{1,3}(?:[,.]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\s*(?:am|pm|a\.m|p\.m|hrs|h\b|:\d))",
                     re.I)
_REF = re.compile(r"^\s*#\s*(\d{1,6})\b[\s:,.\-]*(.*)$", re.S)


# ---------------------------------------------------------------- words
def _fold(text: str) -> str:
    """Lower case, accents off ("Sofá" = "sofa"), for matching only."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", _fold(text)) if len(w) >= 3 and w not in _STOP and not w.isdigit()]


def asks(text: str) -> bool:
    """Is this Marketplace's "is this still available?" (in any of the 11 languages)?"""
    t = _fold(text)
    return any(_fold(p) in t for p in ASKS)


def match_title(items: list[dict], text: str) -> dict | None:
    """The one open item this message names — deterministic: the whole title inside the message wins; else the item with
    the most of its title words in it (at least half of them, at least one); a tie = not sure (None)."""
    t = _fold(text)
    if not t.strip():
        return None
    whole = [i for i in items if len(_fold(i["title"]).strip()) >= 3 and _fold(i["title"]).strip() in t]
    if len(whole) == 1:
        return whole[0]
    said = set(_words(text))
    best, score, tie = None, 0.0, False
    for i in items:
        ws = set(_words(i["title"]))
        if not ws:
            continue
        hit = len(ws & said)
        if not hit or hit * 2 < len(ws):
            continue
        s = hit + hit / len(ws)                              # more words, then the bigger share of the title
        if s > score:
            best, score, tie = i, s, False
        elif s == score:
            tie = True
    return None if tie else best


def pick(items: list[dict], text: str) -> dict | None:
    """The answer to "which item?": its number in the list Valeria sent, or its title words."""
    t = (text or "").strip().strip(".)!#")
    if t.isdigit() and 1 <= int(t) <= len(items):
        return items[int(t) - 1]
    return match_title(items, text)


def read(biz: dict, text: str) -> dict:
    """What a buyer's message says, by rules: {"offer": minor or None, "coming": the time words or "" (None = not coming)}."""
    t = _fold(text)
    out = {"offer": None, "coming": None}
    if any(_fold(w) in t for w in COMING):
        from .commands import _TIME
        m = _TIME.search(text or "")
        out["coming"] = (m.group(0).strip() if m and (m.group(2) or m.group(3)) else "")[:60]
    elif any(_fold(w) in t for w in OFFERS):
        for m in _AMOUNT.finditer(text or ""):
            raw = re.sub(r"[,.](?=\d{3}\b)", "", m.group(2)) + (f".{m.group(3)}" if m.group(3) else "")
            try:
                n = money.to_minor(raw, biz["currency"])
            except Exception:                                    # noqa: BLE001
                continue
            if n > 0:
                out["offer"] = n
                break
    return out


def listing_id(v) -> str | None:
    """A Facebook listing link (…/marketplace/item/1234567890…) or its number -> the number."""
    m = re.search(r"(\d{6,30})", str(v or ""))
    return m.group(1) if m else None


# ---------------------------------------------------------------- items
def items(conn: Connection, business_id: UUID, *, status: str | tuple | None = None) -> list[dict]:
    q = select(IT).where(IT.c.business_id == business_id)
    if status:
        q = q.where(IT.c.status.in_((status,) if isinstance(status, str) else status))
    order = {"open": 0, "paused": 1, "sold": 2}
    rows = [dict(r) for r in conn.execute(q.order_by(IT.c.created_at.desc())).mappings()]
    return sorted(rows, key=lambda r: order.get(r["status"], 3))


def item(conn: Connection, business_id: UUID, item_id) -> dict:
    try:
        iid = UUID(str(item_id))
    except ValueError:
        raise NotFound("item") from None
    r = conn.execute(select(IT).where(IT.c.business_id == business_id, IT.c.id == iid)).mappings().first()
    if not r:
        raise NotFound("item")
    return dict(r)


def _vals(conn: Connection, biz: dict, data: dict, partial: bool) -> dict:
    vals: dict = {}
    if not partial or "title" in data:
        vals["title"] = " ".join(str(data.get("title") or "").split())[:200]
        if not vals["title"]:
            raise Invalid("what is it? (a name)", "title")
    if not partial or "price_minor" in data:
        p = data.get("price_minor")
        if p is not None and (not isinstance(p, int) or isinstance(p, bool) or p < 0):
            raise Invalid("check the price", "price")
        vals["price_minor"] = p
    for k, n in (("pickup", 200), ("notes", 2000)):
        if not partial or k in data:
            vals[k] = str(data.get(k) or "").strip()[:n]
    if not partial or "icon" in data:
        vals["icon"] = str(data.get("icon") or "").strip()[:16]
    if not partial or "delivery" in data:
        vals["delivery"] = bool(data.get("delivery"))
    if not partial or "listing" in data:
        vals["listing_id"] = listing_id(data.get("listing"))
    if not partial or "file_id" in data:
        fid = data.get("file_id") or None
        if fid:
            from . import files as FL
            FL.get(conn, biz["id"], fid)                     # this business's own picture
        vals["file_id"] = fid
    return vals


def add(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    """A new listing — typed, or picked from the business's products (catalog_items: its name, price and picture
    come along; what is typed wins)."""
    data = dict(data)
    cid = data.get("catalog_item_id") or None
    if cid:
        try:
            cid = UUID(str(cid))
        except ValueError:
            raise NotFound("item") from None
        C = db.catalog_items
        p = conn.execute(select(C).where(C.c.business_id == biz["id"], C.c.id == cid)).mappings().first()
        if not p:
            raise NotFound("item")
        if not str(data.get("title") or "").strip():
            data["title"] = f"{p['name']} · {p['variant']}" if p["variant"] else p["name"]
        if data.get("price_minor") is None:
            data["price_minor"] = p["price_minor"]
        if not data.get("file_id"):
            data["file_id"] = p["file_id"]
        if not data.get("notes") and p["description"]:
            data["notes"] = p["description"]
    vals = _vals(conn, biz, data, partial=False)
    row = dict(conn.execute(insert(IT).values(business_id=biz["id"], catalog_item_id=cid, created_by=actor[:100], **vals)
                            .returning(IT)).mappings().one())
    log(conn, biz["id"], "marketplace.added", actor, row["id"], {"title": row["title"]})
    return row


def patch(conn: Connection, biz: dict, actor: str, item_id, data: dict) -> dict:
    """Change what Valeria tells buyers (the price too: she uses the new one from the next answer)."""
    cur = item(conn, biz["id"], item_id)
    vals = _vals(conn, biz, data, partial=True)
    if not vals:
        return cur
    conn.execute(update(IT).where(IT.c.id == cur["id"]).values(**vals, updated_at=func.now()))
    log(conn, biz["id"], "marketplace.changed", actor, cur["id"],
        {"fields": sorted(vals), **({"price_from": cur["price_minor"], "price_to": vals["price_minor"]}
                                    if "price_minor" in vals and vals["price_minor"] != cur["price_minor"] else {})})
    return item(conn, biz["id"], cur["id"])


def pause(conn: Connection, biz: dict, actor: str, item_id, on: bool) -> dict:
    """Pause (hide from "which item?" and from matching; buyers who ask hear it's on hold) — or put it back."""
    cur = item(conn, biz["id"], item_id)
    if cur["status"] == "sold":
        raise Invalid("it is sold — put it back on sale first", "status")
    conn.execute(update(IT).where(IT.c.id == cur["id"]).values(status="paused" if on else "open", updated_at=func.now()))
    log(conn, biz["id"], "marketplace.paused" if on else "marketplace.unpaused", actor, cur["id"])
    return item(conn, biz["id"], cur["id"])


def reopen(conn: Connection, biz: dict, actor: str, item_id) -> dict:
    """Back on sale (the sale fell through): the one taken off the Inventory count when it was sold goes back."""
    cur = item(conn, biz["id"], item_id)
    if cur["status"] != "sold":
        raise Invalid("it isn't sold", "status")
    E = db.events
    last = conn.execute(select(E.c.data).where(E.c.business_id == biz["id"], E.c.type == "marketplace.sold", E.c.subject_id == cur["id"])
                        .order_by(E.c.id.desc()).limit(1)).scalar() or {}
    if last.get("stock") and cur["catalog_item_id"]:
        from . import inventory as INV
        INV.move(conn, biz["id"], actor, cur["catalog_item_id"], 1, "void", note="Marketplace: back on sale")
    conn.execute(update(IT).where(IT.c.id == cur["id"]).values(status="open", sold_at=None, updated_at=func.now()))
    log(conn, biz["id"], "marketplace.reopened", actor, cur["id"], {"stock": bool(last.get("stock"))})
    return item(conn, biz["id"], cur["id"])


# ---------------------------------------------------------------- buyers
def _buyer_q(business_id: UUID):
    C, CV, A = db.contacts, db.conversations, db.channel_accounts
    return (select(BY, C.c.name.label("name"), CV.c.last_inbound_at, CV.c.channel_account_id, A.c.channel)
            .join(C, C.c.id == BY.c.contact_id).join(CV, CV.c.id == BY.c.conversation_id).join(A, A.c.id == CV.c.channel_account_id)
            .where(BY.c.business_id == business_id))


def buyer(conn: Connection, business_id: UUID, buyer_id) -> dict:
    try:
        bid = UUID(str(buyer_id))
    except ValueError:
        raise NotFound("buyer") from None
    r = conn.execute(_buyer_q(business_id).where(BY.c.id == bid)).mappings().first()
    if not r:
        raise NotFound("buyer")
    return dict(r)


def by_ref(conn: Connection, business_id: UUID, ref: int) -> dict | None:
    r = conn.execute(_buyer_q(business_id).where(BY.c.ref == ref)).mappings().first()
    return dict(r) if r else None


def of_conversation(conn: Connection, business_id: UUID, conversation_id: UUID) -> dict | None:
    r = conn.execute(_buyer_q(business_id).where(BY.c.conversation_id == conversation_id)).mappings().first()
    return dict(r) if r else None


def _link(conn: Connection, biz: dict, conv: dict, item_id, lang: str | None = None) -> dict:
    """This chat is a Marketplace buyer (about item_id, or not known yet). Numbered once per business ("#3")."""
    cur = conn.execute(select(BY).where(BY.c.business_id == biz["id"], BY.c.conversation_id == conv["id"])).mappings().first()
    if cur:
        if item_id and cur["item_id"] != item_id:
            conn.execute(update(BY).where(BY.c.id == cur["id"]).values(item_id=item_id, status="asking", offer_minor=None,
                                                                        coming="", updated_at=func.now()))
    else:
        conn.execute(select(db.businesses.c.id).where(db.businesses.c.id == biz["id"]).with_for_update())   # one number at a time
        ref = (conn.execute(select(func.max(BY.c.ref)).where(BY.c.business_id == biz["id"])).scalar() or 0) + 1
        conn.execute(insert(BY).values(business_id=biz["id"], item_id=item_id, conversation_id=conv["id"],
                                       contact_id=conv["contact_id"], ref=ref, lang=lang))
        log(conn, biz["id"], "marketplace.buyer", "valeria", conv["id"], {"ref": ref, "item_id": str(item_id) if item_id else None})
    conn.execute(update(db.conversations).where(db.conversations.c.id == conv["id"]).values(source="marketplace"))
    return of_conversation(conn, biz["id"], conv["id"])


def referral(conn: Connection, biz: dict, channel: str, conv: dict, ref: dict) -> dict | None:
    """Meta said where this chat came from: an item whose listing / product number is in it ties the chat to it; a
    Marketplace source without a number we know makes it a buyer still to be asked "which item?". Anything else (an ad,
    an m.me link about something else) is left alone."""
    if channel not in CHANNELS or not ref:
        return None
    ids = {v for v in (ref.get("listing_id"), ref.get("product_id"), listing_id(ref.get("ref"))) if v}
    hit = None
    if ids:
        hit = conn.execute(select(IT).where(IT.c.business_id == biz["id"], IT.c.listing_id.in_(ids))
                           .order_by(IT.c.created_at.desc()).limit(1)).mappings().first()
    if not hit and ref.get("title"):
        hit = match_title(items(conn, biz["id"]), ref["title"])
    market = "marketplace" in " ".join(str(v) for v in ref.values()).lower()
    if not hit and not market:
        return None
    return _link(conn, biz, conv, hit["id"] if hit else None)


def assign(conn: Connection, biz: dict, actor: str, buyer_id, item_id) -> dict:
    """The owner ties a chat to an item with one tap (nothing is sent: her next answer uses that item)."""
    b = buyer(conn, biz["id"], buyer_id)
    it = item(conn, biz["id"], item_id)
    conn.execute(update(BY).where(BY.c.id == b["id"]).values(item_id=it["id"], status="asking", offer_minor=None, coming="",
                                                              asked_which_at=None, updated_at=func.now()))
    log(conn, biz["id"], "marketplace.assigned", actor, b["id"], {"item_id": str(it["id"])})
    return buyer(conn, biz["id"], b["id"])


def tie(conn: Connection, biz: dict, actor: str, conversation_id, item_id) -> dict:
    """From the inbox: this chat is about that item (a buyer the matching missed)."""
    CV = db.conversations
    conv = conn.execute(select(CV).where(CV.c.business_id == biz["id"], CV.c.id == conversation_id)).mappings().first()
    if not conv:
        raise NotFound("conversation")
    it = item(conn, biz["id"], item_id)
    b = _link(conn, biz, dict(conv), it["id"])
    log(conn, biz["id"], "marketplace.assigned", actor, b["id"], {"item_id": str(it["id"])})
    return b


def set_status(conn: Connection, biz: dict, actor: str, buyer_id, status: str) -> dict:
    """The owner's tap: asking / offered / coming (bought and "told it's sold" come from Mark sold)."""
    if status not in TAPS:
        raise Invalid("asking, offered or coming", "status")
    b = buyer(conn, biz["id"], buyer_id)
    conn.execute(update(BY).where(BY.c.id == b["id"]).values(status=status, updated_at=func.now()))
    log(conn, biz["id"], "marketplace.status", actor, b["id"], {"status": status})
    return buyer(conn, biz["id"], b["id"])


def shown_status(b: dict, now: datetime | None = None) -> str:
    """What the screen says: the stored status, or "quiet" when a buyer still deciding hasn't written for QUIET_HOURS."""
    now = now or datetime.now(timezone.utc)
    if b["status"] in TAPS and b.get("last_inbound_at") and now - b["last_inbound_at"] > timedelta(hours=QUIET_HOURS):
        return "quiet"
    return b["status"]


def window_open(b: dict, now: datetime | None = None) -> bool:
    """Inside the channel's reply window (Messenger: 24 h after the buyer's last message)?"""
    hours = channels.adapter(b["channel"]).reply_window_hours
    if not hours:
        return True
    last = b.get("last_inbound_at")
    return bool(last) and (now or datetime.now(timezone.utc)) - last <= timedelta(hours=hours)


def window_left(b: dict, now: datetime | None = None) -> int | None:
    """Whole hours left to answer (None: the channel has no window)."""
    hours = channels.adapter(b["channel"]).reply_window_hours
    if not hours or not b.get("last_inbound_at"):
        return None
    left = b["last_inbound_at"] + timedelta(hours=hours) - (now or datetime.now(timezone.utc))
    return max(0, int(left.total_seconds() // 3600))


def connected(conn: Connection, biz: dict) -> bool:
    """Is a Facebook Page connected (Messenger), or — in the sandbox — the sandbox inbox?"""
    A = db.channel_accounts
    want = ("messenger", "sandbox") if biz.get("sandbox") else ("messenger",)
    return bool(conn.execute(select(A.c.id).where(A.c.business_id == biz["id"], A.c.active, A.c.channel.in_(want)).limit(1)).first())


def board(conn: Connection, biz: dict, now: datetime | None = None) -> dict:
    """The Marketplace view: items (open, paused, sold) with their buyers, and the chats not tied to an item yet."""
    now = now or datetime.now(timezone.utc)
    M = db.messages
    its = items(conn, biz["id"])
    rows = [dict(r) for r in conn.execute(_buyer_q(biz["id"]).order_by(BY.c.updated_at.desc())).mappings()]
    for b in rows:
        b["shown"] = shown_status(b, now)
        b["open"] = window_open(b, now)
        b["left"] = window_left(b, now)
        b["last"] = conn.execute(select(M.c.body).where(M.c.conversation_id == b["conversation_id"], M.c.direction == "in", M.c.body != "")
                                 .order_by(M.c.created_at.desc()).limit(1)).scalar() or ""
    by_item: dict = {}
    for b in rows:
        by_item.setdefault(b["item_id"], []).append(b)
    for i in its:
        i["buyers"] = by_item.get(i["id"], [])
    return {"items": its, "unknown": by_item.get(None, []), "connected": connected(conn, biz)}


# ---------------------------------------------------------------- what the AI knows about the item (services/ai.py)
def ai_facts(conn: Connection, biz: dict, conversation_id: UUID) -> str:
    """Extra facts for the business's AI when it answers a Marketplace buyer: the item, its price and pickup."""
    b = of_conversation(conn, biz["id"], conversation_id)
    if not b or not b["item_id"]:
        return ""
    it = item(conn, biz["id"], b["item_id"])
    pack = packs.country(biz["country"])
    price = money.fmt(it["price_minor"], biz["currency"], pack.locale) if it["price_minor"] is not None else "ask the owner"
    state = {"open": "still available", "paused": "on hold (not available right now)", "sold": "SOLD"}[it["status"]]
    lines = [f"\n\nFACEBOOK MARKETPLACE: this customer is asking about \"{it['title']}\" — {state}. Price: {price}.",
             f"Pickup: {it['pickup'] or 'ask the owner'}." + (" Delivery is possible." if it["delivery"] else ""),
             "Never accept or make an offer and never promise to hold it: say you'll check with the owner. "
             "Ask when they could come to pick it up."]
    if it["notes"]:
        lines.append(f"Owner's notes about it: {it['notes'][:1500]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- after a buyer writes (services/autoreply.py)
def _t(key: str, lang: str) -> str:
    from .owner import t
    return t(key, lang)


def _price(biz: dict, minor) -> str:
    return money.fmt(int(minor), biz["currency"], packs.country(biz["country"]).locale)


def answer_text(biz: dict, it: dict, lang: str, ask_when: bool = True) -> str:
    """"Yes, it's still available! Black sofa: $150. Pickup in Centro. When could you come?" — the item's own facts."""
    head = (_t("mk_ans_available", lang).format(item=it["title"], price=_price(biz, it["price_minor"]))
            if it["price_minor"] is not None else _t("mk_ans_available_np", lang).format(item=it["title"]))
    out = [head]
    if it["pickup"]:
        out.append(_t("mk_ans_pickup", lang).format(area=it["pickup"]))
    if it["delivery"]:
        out.append(_t("mk_ans_delivery", lang))
    if ask_when:
        out.append(_t("mk_ans_when", lang))
    return " ".join(out)


def pickup_offer(conn: Connection, biz: dict, conversation_id: UUID, it: dict, lang: str, now: datetime) -> str | None:
    """sch01: instead of "When could you come?", the pickup times that are really free ("Today 5:00–7:00 PM or tomorrow
    …", numbered) — when the business takes bookings (Calendar on, opening hours). The buyer's answer books it
    (chatbook.py offer mode). None = not offered (no bookings, or nothing free)."""
    from . import chatbook as CB
    from . import chatcal as CC
    if not biz.get("booking_enabled") or not blocks.on(biz, "calendar") or not biz.get("hours"):
        return None
    what = f"{it['icon'] or DEFAULT_ICON} {it['title']} · {_t('sc_pickup', biz['language'])}"
    got = CB.offer_flow(conn, biz, conversation_id, what=what, minutes=CC.PICKUP_MINUTES, lang=lang, now=now, item_id=it["id"],
                        location=it["pickup"] or None)
    if not got:
        return None
    text, opts = got
    return text + "\n" + "\n".join(f"{n}) {o['title']}" for n, o in enumerate(opts, 1))


def which_text(biz: dict, open_items: list[dict], lang: str) -> str:
    lines = [f"{n}) {i['title']}" + (f" — {_price(biz, i['price_minor'])}" if i["price_minor"] is not None else "")
             for n, i in enumerate(open_items[:MAX_WHICH], 1)]
    return _t("mk_ans_which", lang) + "\n" + "\n".join(lines)


def respond(business_id: UUID, conversation_id: UUID, http=None, now: datetime | None = None) -> str:
    """After a buyer's message: off (not a Marketplace chat: the usual answers run) | which | answered | ai | paused |
    quiet | limit | blocked."""
    from . import ai as AI
    from . import owner as OW
    from . import style
    from .ai import detect_language
    from .answers import topic
    now = now or datetime.now(timezone.utc)
    M, CV, E = db.messages, db.conversations, db.events
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        conv = conn.execute(select(CV, db.channel_accounts.c.channel).join(db.channel_accounts, db.channel_accounts.c.id == CV.c.channel_account_id)
                            .where(CV.c.id == conversation_id, CV.c.business_id == business_id)).mappings().first()
        if not conv or conv["channel"] not in CHANNELS:
            return "off"
        conv = dict(conv)
        last = conn.execute(select(M).where(M.c.conversation_id == conversation_id).order_by(M.c.created_at.desc()).limit(1)).mappings().first()
        if not last or last["direction"] != "in":
            return "off"
        text = last["body"] or last["transcript"] or ""
        from . import contacts as CS
        if CS.is_stop(text):
            return "off"
        b = of_conversation(conn, business_id, conversation_id)
        every = items(conn, business_id)                     # a sold or paused one too: they hear so
        open_items = [i for i in every if i["status"] == "open"]
        if not b:                                            # a new chat: is it about one of the items?
            hit = match_title(every, text)
            if not hit and not (open_items and asks(text)):
                return "off"                                 # not about anything listed: the usual answers run
            b = _link(conn, biz, conv, hit["id"] if hit else None, detect_language(text))
        elif not b["item_id"]:                               # they were asked "which item?" (or wrote again)
            hit = (b["asked_which_at"] and pick(open_items, text)) or match_title(every, text)
            if hit:
                b = assign(conn, biz, "valeria", b["id"], hit["id"])
        lang = detect_language(text) or b["lang"] or biz["language"]
        if lang != b["lang"] and detect_language(text):
            conn.execute(update(BY).where(BY.c.id == b["id"]).values(lang=lang))
        it = item(conn, business_id, b["item_id"]) if b["item_id"] else None
        AA = db.assistant_actions                            # the first word about this item: the full answer + "new buyer"
        fresh = bool(it) and not conn.execute(select(AA.c.id).where(AA.c.business_id == business_id,
                                                                    AA.c.dedupe == _dedupe(b, "new", it))).first()
        said = read(biz, text) if it and not fresh else {"offer": None, "coming": None}
        if it and b["status"] in TAPS:                       # where they stand, from what they wrote
            if said["coming"] is not None:
                conn.execute(update(BY).where(BY.c.id == b["id"]).values(status="coming", coming=said["coming"], updated_at=func.now()))
            elif said["offer"]:
                conn.execute(update(BY).where(BY.c.id == b["id"]).values(status="offered", offer_minor=said["offer"], updated_at=func.now()))
            else:
                conn.execute(update(BY).where(BY.c.id == b["id"]).values(updated_at=func.now()))
        b = of_conversation(conn, business_id, conversation_id)
        on = blocks.on(biz, "assistant")
        stopped = OW.paused(conn, business_id)
        recent = conn.execute(select(func.count()).select_from(E).where(
            E.c.business_id == business_id, E.c.type == "marketplace.replied", E.c.subject_id == conversation_id,
            E.c.created_at > now - timedelta(hours=1))).scalar()
        p = style.get(conn, business_id)
        # what she says, and what the owner hears
        ping, offered = None, False
        if not it:
            if not open_items:
                return "off"
            if b["asked_which_at"] and now - b["asked_which_at"] < timedelta(hours=GAP_HOURS):
                body, kind, ping = None, "quiet", ("msg", text)  # asked already, still not clear: the owner (or a tap) decides
            else:
                body, kind = which_text(biz, open_items, lang), "which"
                conn.execute(update(BY).where(BY.c.id == b["id"]).values(asked_which_at=now))
        elif b["status"] == "bought":                        # the one who bought it: a person answers
            body, kind, ping = None, "quiet", ("msg", text)
        elif it["status"] == "sold":
            body, kind = _t("mk_ans_sold", lang).format(item=it["title"]), "answered"
            conn.execute(update(BY).where(BY.c.id == b["id"]).values(status="told_sold", updated_at=func.now()))
        elif it["status"] == "paused":
            body, kind, ping = _t("mk_ans_hold", lang).format(item=it["title"]), "answered", ("msg", text)
        elif fresh:
            body, kind, ping = answer_text(biz, it, lang), "answered", ("new", text)
            times = (pickup_offer(conn, biz, conversation_id, it, lang, now)   # sch01: the free pickup times, to pick from
                     if blocks.on(biz, "assistant") and not OW.paused(conn, business_id) else None)
            if times:
                body, offered = answer_text(biz, it, lang, ask_when=False) + "\n\n" + times, True
        elif said["coming"] is not None:
            body, kind, ping = _t("mk_ans_coming", lang), "answered", ("coming", said["coming"])
        elif said["offer"]:
            body, kind, ping = _t("mk_ans_offer", lang).format(amount=_price(biz, said["offer"])), "answered", ("offer", said["offer"])
        elif asks(text) or topic(text) in ("price", "location"):
            body, kind = answer_text(biz, it, lang), "answered"
        else:
            told = conn.execute(select(E.c.id).where(E.c.business_id == business_id, E.c.type == "marketplace.later",
                                                     E.c.subject_id == conversation_id,
                                                     E.c.created_at > now - timedelta(hours=GAP_HOURS))).first()
            body, kind, ping = (None if told else _t("mk_ans_later", lang)), "later", ("msg", text)
        ai_on = (AI.settings(conn, business_id) or {}).get("mode") == "auto" and blocks.on(biz, "ai")
    if ping and on:
        _ping(biz, b, it, ping[0], ping[1], http=http, now=now)
    if not on or stopped or body is None:
        return "paused" if stopped else "quiet"
    if recent >= MAX_PER_HOUR:
        return "limit"
    if ai_on and kind in ("answered", "later") and it and it["status"] == "open" and b["status"] != "bought" and not offered:
        res = AI.auto_reply(business_id, conversation_id, http=http, now=now)     # the business's own AI, with the item's facts
        if res not in ("off", "failed"):
            with db.tx() as conn:
                log(conn, business_id, "marketplace.replied", "ai", conversation_id, {"ref": b["ref"], "kind": "ai:" + res})
            return "ai:" + res
    try:
        msg = _send(biz, b, style.apply(p, body), "valeria", http, now)
    except Blocked:
        return "blocked"
    with db.tx() as conn:
        conn.execute(update(M).where(M.c.id == msg["id"]).values(by_ai=True))
        log(conn, business_id, "marketplace.later" if kind == "later" else "marketplace.replied", "valeria", conversation_id,
            {"ref": b["ref"], "kind": kind})
    return "which" if kind == "which" else "answered"


def _send(biz: dict, b: dict, body: str, actor: str, http, now) -> dict:
    from . import inbox as X
    msg = X.send(biz, actor, contact_id=b["contact_id"], body=body, channel_account_id=b["channel_account_id"], http=http, now=now)
    if msg["status"] == "failed":
        raise Blocked("failed", msg.get("error") or "the message didn't go out")
    return msg


# ---------------------------------------------------------------- the owner hears it, and answers by chat
def who(b: dict, lang: str) -> str:
    """The buyer's first name — Messenger doesn't send names, so usually "Buyer #3"."""
    first = (b.get("name") or "").split(" ")[0]
    return first or _t("mk_buyer_n", lang).format(ref=b["ref"])


def ping_text(biz: dict, b: dict, it: dict | None, kind: str, extra, lang: str) -> str:
    """"🛋 Black sofa: Juan says he's on his way." + how to answer (#3)."""
    icon = (it or {}).get("icon") or DEFAULT_ICON
    title = it["title"] if it else _t("mk_some_item", lang)
    name = who(b, lang)
    if kind == "offer":
        line = _t("mk_ping_offer", lang).format(icon=icon, item=title, name=name, amount=_price(biz, extra))
    elif kind == "coming":
        line = (_t("mk_ping_coming_at", lang).format(icon=icon, item=title, name=name, when=extra) if extra
                else _t("mk_ping_coming", lang).format(icon=icon, item=title, name=name))
    elif kind == "new":
        line = _t("mk_ping_new", lang).format(icon=icon, item=title, name=name)
    else:
        line = _t("mk_ping_msg", lang).format(icon=icon, item=title, name=name, text=str(extra or "…")[:300])
    return line + "\n" + _t("mk_ping_reply", lang).format(ref=b["ref"])


def _dedupe(b: dict, kind: str, it: dict | None, now: datetime | None = None) -> str:
    """One "new buyer" per buyer and item; the others once per buyer, kind and hour."""
    if kind == "new":
        return f"mkt:{b['id']}:new:{it['id'] if it else '-'}"
    return f"mkt:{b['id']}:{kind}:{(now or datetime.now(timezone.utc)).strftime('%Y%m%d%H')}"


def _ping(biz: dict, b: dict, it: dict | None, kind: str, extra, *, http=None, now: datetime | None = None) -> bool:
    """Tell the owner (and staff allowed to answer customers) on their chat, and on the installed app. It is also her
    open question to them, so plain words in reply go to this buyer. Once per buyer per kind per hour."""
    from . import owner as OW
    from . import push
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        people = OW.owners(conn, biz["id"], "reply")
        a = OW.ask(conn, biz, "reply", ping_text(biz, b, it, kind, extra, OW.lang_of(biz)),
                   {"conversation_id": str(b["conversation_id"]), "mkt": b["ref"], "question": str(extra or "")[:500]},
                   contact_id=b["contact_id"], by="marketplace", prompted=True, now=now, dedupe=_dedupe(b, kind, it, now))
    if not a:
        return False
    for person in people:
        OW.say(biz, person, ping_text(biz, b, it, kind, extra, OW.lang_of(biz, person)), http=http, now=now)
    push.notify(biz["id"], biz["name"], ping_text(biz, b, it, kind, extra, OW.lang_of(biz)), "/app/marketplace", http=http)
    return True


def owner_reply(biz: dict, number: dict | None, text: str, *, user: dict | None = None, http=None,
                now: datetime | None = None) -> dict | None:
    """The owner answers a buyer by chat: "#3 ok, 6pm works" — or plain words to her last Marketplace question. The words
    go to that buyer as written (no "send it?" step: they are the owner's own answer to someone who wrote first), only
    inside Messenger's 24 h. -> {"text"} for the owner, or None when this isn't a Marketplace answer."""
    from . import commands as CMD
    from . import owner as OW
    now = now or datetime.now(timezone.utc)
    lang = OW.lang_of(biz, number)
    m = _REF.match(text or "")
    with db.tx() as conn:
        if m:
            ref, words = int(m.group(1)), m.group(2).strip()
            b = by_ref(conn, biz["id"], ref)
            if not b:
                return {"text": _t("mk_no_ref", lang).format(ref=ref)}
        else:
            q = OW.open_question(conn, biz["id"], number, now)
            if not q or q["kind"] != "reply" or not (q["payload"] or {}).get("mkt"):
                return None
            if CMD.answer_of(text, "reply", q["payload"]) is not None or CMD.rules(text)["intent"] not in (None, "reply"):
                return None                                  # "1", "no", "later" or another command: the usual answer
            b = by_ref(conn, biz["id"], int(q["payload"]["mkt"]))
            if not b:
                return None
            words = (text or "").strip()
        if not OW.may(number, "reply"):
            return {"text": _t("va_not_allowed", lang)}
        it = item(conn, biz["id"], b["item_id"]) if b["item_id"] else None
        title = it["title"] if it else _t("mk_some_item", lang)
        if not words:
            return {"text": _t("mk_what_words", lang).format(name=who(b, lang), item=title, ref=b["ref"])}
        if not window_open(b, now):
            return {"text": _t("mk_window_closed", lang).format(name=who(b, lang),
                                                                  hours=channels.adapter(b["channel"]).reply_window_hours)}
    actor = f"owner:{number['id']}" if number and number.get("id") else (f"user:{user['id']}" if user else "owner:web")
    try:
        _send(biz, b, words[:2000], actor, http, now)
    except Blocked as e:
        return {"text": _t("mk_couldnt_send", lang).format(why=str(e))}
    AA = db.assistant_actions
    with db.tx() as conn:                                    # her questions about this buyer are answered
        conn.execute(update(AA).where(AA.c.business_id == biz["id"], AA.c.kind == "reply", AA.c.status.in_(("asked", "later")),
                                      AA.c.payload["conversation_id"].astext == str(b["conversation_id"]))
                     .values(status="done", decided_at=now, done_at=now, result={"say": "relayed"}))
        log(conn, biz["id"], "marketplace.relayed", actor, b["conversation_id"], {"ref": b["ref"]})
    return {"text": _t("mk_relayed", lang).format(name=who(b, lang), item=title)}


# ---------------------------------------------------------------- sold
def mark_sold(biz: dict, actor: str, item_id, buyer_id=None, *, http=None, now: datetime | None = None) -> dict:
    """Sold (to one of its buyers, or to someone else). Every other buyer of it still inside Messenger's 24 h is told,
    politely, in their language; the rest are listed ("couldn't tell: 24 h passed"). One taken off the Inventory count
    when it came from there. -> {"told": [names], "not_told": [names]}."""
    from . import style
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        it = conn.execute(select(IT).where(IT.c.business_id == biz["id"], IT.c.id == item(conn, biz["id"], item_id)["id"])
                          .with_for_update()).mappings().one()
        if it["status"] == "sold":
            raise Invalid("it is already sold", "status")
        if buyer_id:
            to = buyer(conn, biz["id"], buyer_id)
            if to["item_id"] != it["id"]:
                raise Invalid("that buyer asked about another item", "buyer")
            conn.execute(update(BY).where(BY.c.id == to["id"]).values(status="bought", updated_at=func.now()))
        stock = False
        if it["catalog_item_id"]:
            C = db.catalog_items
            cat = conn.execute(select(C.c.in_inventory, C.c.stock).where(C.c.business_id == biz["id"], C.c.id == it["catalog_item_id"])).first()
            if cat and cat.in_inventory and cat.stock is not None:
                from . import inventory as INV
                INV.move(conn, biz["id"], actor, it["catalog_item_id"], -1, "sale", note="Marketplace")
                stock = True
        conn.execute(update(IT).where(IT.c.id == it["id"]).values(status="sold", sold_at=now, updated_at=func.now()))
        q = _buyer_q(biz["id"]).where(BY.c.item_id == it["id"], BY.c.status.in_(TAPS))
        others = [dict(r) for r in conn.execute(q).mappings()]          # the buyer it went to is "bought" by now
        p = style.get(conn, biz["id"])
        log(conn, biz["id"], "marketplace.sold", actor, it["id"], {"buyer_id": str(buyer_id) if buyer_id else None, "stock": stock})
    told, missed = [], []
    for o in others:
        ok = False
        if window_open(o, now):
            lang = o["lang"] or biz["language"]
            try:
                _send(biz, o, style.apply(p, _t("mk_ans_sold", lang).format(item=it["title"])), actor, http, now)
                ok = True
            except Blocked:
                ok = False
        with db.tx() as conn:
            conn.execute(update(BY).where(BY.c.id == o["id"]).values(status="told_sold" if ok else "not_told", updated_at=func.now()))
        (told if ok else missed).append(who(o, biz["language"]))
    with db.tx() as conn:
        log(conn, biz["id"], "marketplace.told_sold", actor, it["id"], {"told": len(told), "not_told": len(missed)})
    return {"told": told, "not_told": missed}

