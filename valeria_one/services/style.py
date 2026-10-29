"""Talks like them: how the owner sounds, in plain words — and every draft Valeria writes follows it.

The profile: tone (formal / friendly / super casual), emoji (none / some / lots), the greeting and sign-off they
use, local manners ("po", "ji", "usted", "Sie"…), and the language or dialect. Day one asks ONE question (formal,
friendly or super casual); after that she learns from the owner's edits: each time the owner changes one of her
drafts before sending (a suggested reply, a post, a message she proposed), the draft and what was really sent are
kept, and once a day the profile is updated from them — with the business's own AI when it has a key, otherwise
with simple counts (emoji or not, the sign-off that keeps coming back, "usted" or "tú"). The owner can read it
("Valeria, how do I sound?") and change it on the screen or by chat ("be more formal", "no emojis").

Applied to every AI draft (replies, the assistant's own messages, posts) as a line in the instructions, and to the
fixed messages she sends without AI (review asks, loyalty, offers) as light edits: no emoji when they use none,
their sign-off at the end.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db
from . import Invalid, log

SS = db.style_samples
TONES = ("formal", "friendly", "casual")
EMOJI = ("none", "some", "lots")
FIELDS = ("tone", "emoji", "greeting", "signoff", "manners", "dialect", "notes")
DEFAULT = {"tone": "friendly", "emoji": "some", "greeting": "", "signoff": "", "manners": "", "dialect": "", "notes": ""}
TONE_EMOJI = {"formal": "none", "friendly": "some", "casual": "lots"}
LEARN_AFTER = 3                                              # edits before the profile is updated
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF⭐⬆↔-↪️]")


def get(conn: Connection, business_id: UUID) -> dict:
    from .owner import settings
    s = settings(conn, business_id)["style"] or {}
    return {**DEFAULT, **{k: v for k, v in s.items() if k in FIELDS and v not in (None,)}, "set": bool(s)}


def save(conn: Connection, biz: dict, actor: str, fields: dict) -> dict:
    from .owner import _upsert
    cur = {k: v for k, v in get(conn, biz["id"]).items() if k in FIELDS}
    for k in FIELDS:
        if fields.get(k) is None:
            continue
        v = " ".join(str(fields[k]).split())[:200]
        if k == "tone" and v not in TONES:
            raise Invalid("formal, friendly or super casual", "tone")
        if k == "emoji" and v not in EMOJI:
            raise Invalid("none, some or lots", "emoji")
        cur[k] = v
    _upsert(conn, biz["id"], {"style": cur})
    log(conn, biz["id"], "style.updated", actor, biz["id"], {"fields": sorted(k for k in FIELDS if fields.get(k) is not None)})
    return get(conn, biz["id"])


def set_tone(conn: Connection, biz: dict, actor: str, tone: str) -> dict:
    """Day one: one question. The emoji follows the tone until she learns otherwise."""
    if tone not in TONES:
        raise Invalid("formal, friendly or super casual", "tone")
    return save(conn, biz, actor, {"tone": tone, "emoji": TONE_EMOJI[tone]})


# ---------------------------------------------------------------- using it
def prompt(p: dict) -> str:
    """The line every AI draft gets."""
    bits = {"formal": "polite and formal (use the formal 'you' where the language has one)", "friendly": "warm and friendly",
            "casual": "very casual and relaxed, like a friend"}[p.get("tone") or "friendly"]
    out = [f"Write the way the owner does: {bits}"]
    out.append({"none": "never use emoji", "some": "at most one emoji", "lots": "emoji are welcome"}[p.get("emoji") or "some"])
    for k, label in (("greeting", "they greet with"), ("signoff", "they sign off with"), ("manners", "local manners they use"),
                     ("dialect", "language / dialect"), ("notes", "also")):
        if p.get(k):
            out.append(f"{label}: {p[k]}")
    return "; ".join(out) + "."


def apply(p: dict, text: str) -> str:
    """A fixed message (no AI): no emoji for someone who uses none; their sign-off at the end."""
    out = text or ""
    if p.get("emoji") == "none":
        out = re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", out)).replace(" \n", "\n").strip()
    so = (p.get("signoff") or "").strip()
    if so and so.lower() not in out.lower()[-max(len(so) + 10, 20):]:
        out = f"{out}\n{so}"
    return out


def describe(p: dict, lang: str) -> str:
    """How do I sound? — in the owner's language."""
    from .owner import t
    lines = [t("va_style_tone_" + (p.get("tone") or "friendly"), lang), t("va_style_emoji_" + (p.get("emoji") or "some"), lang)]
    for k in ("greeting", "signoff", "manners", "dialect"):
        if p.get(k):
            lines.append(t("va_style_" + k, lang).format(v=p[k]))
    return "\n".join(lines)


# "be more formal", "no emojis"… in the 11 languages (the whole meaning, from words inside the message)
CHANGES = {                                                 # casual first: "informal" contains "formal"
    ("tone", "casual"): ("more casual", "casual", "relaxed", "más casual", "relajad", "informal", "mais descontraíd", "descontraíd",
                         "décontract", "plus cool", "lockerer", "locker", "mas kaswal", "kaswal", "lebih santai", "santai",
                         "अनौपचारिक", "दोस्ताना", "ودي", "عفوي", "カジュアル", "くだけ", "随意", "轻松"),
    ("tone", "formal"): ("more formal", "formal", "más formal", "mas formal", "mais formal", "plus formel", "formeller", "förmlicher",
                         "mas pormal", "pormal", "lebih formal", "सभ्य", "औपचारिक", "رسمي", "丁寧", "正式"),
    ("tone", "friendly"): ("friendly", "amable", "amigable", "simpátic", "amigáve", "amical", "chaleureux", "freundlich",
                           "magiliw", "ramah", "मिलनसार", "لطيف", "親しみ", "友好", "亲切"),
    ("emoji", "none"): ("no emoji", "without emoji", "sin emoji", "sem emoji", "sans emoji", "pas d'emoji", "keine emoji", "ohne emoji",
                        "walang emoji", "huwag emoji", "tanpa emoji", "jangan emoji", "इमोजी नहीं", "बिना इमोजी", "بدون إيموجي",
                        "بدون رموز", "絵文字なし", "絵文字は使わない", "不要表情", "别用表情", "不用表情"),
    ("emoji", "lots"): ("more emoji", "más emoji", "mas emoji", "mais emoji", "plus d'emoji", "mehr emoji", "maraming emoji",
                        "lebih banyak emoji", "ज़्यादा इमोजी", "زيد الإيموجي", "المزيد من الإيموجي", "絵文字を多く", "多用表情", "多点表情"),
}


def change_from(text: str) -> dict:
    """{"tone": "formal"} / {"emoji": "none"} from what the owner wrote; {} when it isn't a style change."""
    low = (text or "").lower()
    out = {}
    for (k, v), words in CHANGES.items():
        if k not in out and any(w in low for w in words):
            out[k] = v
    return out


# ---------------------------------------------------------------- learning from the owner's edits
def record(conn: Connection, business_id: UUID, kind: str, draft: str, sent: str) -> bool:
    """The owner changed one of her drafts before sending it: kept to learn from. Identical = nothing to learn."""
    d, s = (draft or "").strip(), (sent or "").strip()
    if not d or not s or " ".join(d.split()) == " ".join(s.split()):
        return False
    conn.execute(insert(SS).values(business_id=business_id, kind=kind[:12], draft=d[:2000], sent=s[:2000]))
    return True


def _heuristic(p: dict, samples: list[tuple[str, str]]) -> dict:
    out = dict(p)
    sent = [s for _, s in samples]
    drafts = [d for d, _ in samples]
    e_sent = sum(len(_EMOJI.findall(s)) for s in sent)
    e_draft = sum(len(_EMOJI.findall(d)) for d in drafts)
    if e_sent == 0 and e_draft > 0:
        out["emoji"] = "none"
    elif e_sent >= 2 * len(sent):
        out["emoji"] = "lots"
    elif e_sent > 0 and out.get("emoji") == "none":
        out["emoji"] = "some"
    lasts = [s.strip().splitlines()[-1].strip() for s in sent if s.strip()]
    common = max(set(lasts), key=lasts.count) if lasts else ""
    if common and lasts.count(common) >= max(2, len(lasts) // 2 + 1) and len(common) <= 60:
        out["signoff"] = common
    firsts = [re.split(r"[,!.\n]", s.strip(), maxsplit=1)[0].strip() for s in sent if s.strip()]
    greet = max(set(firsts), key=firsts.count) if firsts else ""
    if greet and firsts.count(greet) >= max(2, len(firsts) // 2 + 1) and len(greet) <= 30:
        out["greeting"] = greet
    low = " ".join(sent).lower()
    if re.search(r"\busted(es)?\b", low) and not re.search(r"\b(tú|tu)\b", low):
        out["tone"] = "formal"
    manners = [m for m in ("po", "opo", "ji", "usted", "Sie") if re.search(rf"\b{m}\b", " ".join(sent))]
    if manners:
        out["manners"] = ", ".join(sorted(set(manners)))[:200]
    return out


def _with_ai(biz: dict, p: dict, samples: list[tuple[str, str]], http=None) -> dict | None:
    from . import ai as AI
    with db.tx() as conn:
        creds = AI.own_key(conn, biz)
    if not creds:
        return None
    prov, key, model = creds
    pairs = "\n\n".join(f"DRAFT: {d[:500]}\nSENT: {s[:500]}" for d, s in samples[:12])
    system = ("You read how a small-business owner edits an assistant's drafts and describe their writing style. Answer with "
              "JSON only: {\"tone\": \"formal|friendly|casual\", \"emoji\": \"none|some|lots\", \"greeting\": \"\", "
              "\"signoff\": \"\", \"manners\": \"\", \"dialect\": \"\", \"notes\": \"\"}. Keep each value short (under 80 "
              "characters), empty when unsure.")
    try:
        raw = AI._call(prov, key, model.split(",")[0].strip(), system,
                       [("user", f"Current profile: {json.dumps(p, ensure_ascii=False)}\n\n{pairs}")], AI._http(http), max_tokens=400)
        m = re.search(r"\{.*\}", raw or "", re.S)
        d = json.loads(m.group(0)) if m else {}
    except Exception:                                         # noqa: BLE001 — the counts still learn
        return None
    out = dict(p)
    for k in FIELDS:
        v = d.get(k)
        if isinstance(v, str):
            v = " ".join(v.split())[:200]
            if (k == "tone" and v not in TONES) or (k == "emoji" and v not in EMOJI):
                continue
            out[k] = v
    return out


def learn(biz: dict, http=None, now: datetime | None = None) -> dict | None:
    """Update the profile from the edits not learned yet (LEARN_AFTER at least). -> the new profile, or None."""
    from .owner import _upsert
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        rows = conn.execute(select(SS.c.id, SS.c.draft, SS.c.sent).where(SS.c.business_id == biz["id"], SS.c.learned.is_(False))
                            .order_by(SS.c.created_at).limit(30)).all()
        if len(rows) < LEARN_AFTER:
            return None
        p = {k: v for k, v in get(conn, biz["id"]).items() if k in FIELDS}
    samples = [(r.draft, r.sent) for r in rows]
    new = _with_ai(biz, p, samples, http) or _heuristic(p, samples)
    with db.tx() as conn:
        _upsert(conn, biz["id"], {"style": new, "style_learned_on": now.astimezone(ZoneInfo(biz["timezone"])).date()})
        conn.execute(update(SS).where(SS.c.id.in_([r.id for r in rows])).values(learned=True))
        log(conn, biz["id"], "style.learned", "valeria", biz["id"], {"samples": len(rows)})
    return new


def learn_due(now: datetime | None = None, http=None) -> dict:
    """Job: once a day per business with enough new edits."""
    now = now or datetime.now(timezone.utc)
    stats = {"learned": 0}
    AS = db.assistant_settings
    with db.tx() as conn:
        bids = [r[0] for r in conn.execute(select(SS.c.business_id).where(SS.c.learned.is_(False)).group_by(SS.c.business_id)
                                           .having(func.count() >= LEARN_AFTER).limit(200))]
    for bid in bids:
        with db.tx() as conn:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
            on = conn.execute(select(AS.c.style_learned_on).where(AS.c.business_id == bid)).scalar()
        if on == now.astimezone(ZoneInfo(biz["timezone"])).date():
            continue
        if learn(biz, http, now):
            stats["learned"] += 1
    return stats
