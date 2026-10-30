"""Posts: the AI writes the business's post, the owner approves it, Valeria One publishes it — now or later.

The owner types an idea (or picks one: an offer, a new product, a holiday, a review to highlight) and the AI drafts
it in the business's language and tone, one version per network: the Facebook Page, Instagram and the Google
Business Profile. With no AI (no key, or the AI is busy) the owner simply writes it. Nothing is published until the
owner approves: "publish now" or a date and time.

Publishing, with the business's OWN accounts (connected in Connect):
* Facebook Page: POST /{page}/feed (text) or /{page}/photos (picture + text). Needs pages_manage_posts.
* Instagram: POST /{ig}/media (a public JPEG image_url + caption), then /{ig}/media_publish. Instagram has no
  text-only posts, so a picture is required; the daily quota (content_publishing_limit) is checked first. Needs
  instagram_content_publish. Accounts connected before v0.12 must reconnect to grant both permissions.
* Google: a local post (services/gbp.py), text + optional picture.

The picture must be reachable by Meta and Google: the website's own public file address when the website is on
and the picture is public there, otherwise a signed address that works for 3 days (/pf/<file>/<expires>/<sig>).

By hand (v0.21, the posting calendar /app/schedule): TikTok, WhatsApp status, LinkedIn, X — and any of the three above the
owner has not connected, or would rather post themselves — are planned the same way, but never published by us: at
their time the owner gets a reminder (Today, and a notification on their phone) with the words ready to copy or share in
one tap, and ticks "posted". A post's own approval is still the only thing that puts it on the calendar.

The job (every minute) publishes what is due. A post is claimed first (status scheduled -> publishing, in one
UPDATE), so two workers never publish it twice; each network's result is saved the moment it is known, so a retry
only tries the networks that failed. A post interrupted in the middle is never retried by itself: the owner
checks the network and taps "try again".
"""
from __future__ import annotations

import io
import json
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy import func, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

from .. import blocks, config, crypto, db
from ..channels.meta import graph
from . import Invalid, NotFound, log

P, A = db.posts, db.channel_accounts
NETWORKS = ("facebook", "instagram", "google")
HAND = ("tiktok", "whatsapp", "linkedin", "x")          # v0.21: no posting door: always by hand (a reminder at the time)
ALL = NETWORKS + HAND
KINDS = ("idea", "offer", "product", "holiday", "review")
POST_SCOPES = {"facebook": "pages_manage_posts", "instagram": "instagram_content_publish"}
MAX_TEXT = {"facebook": 5000, "instagram": 2200, "google": 1500, "tiktok": 2200, "whatsapp": 700, "linkedin": 3000, "x": 280}
MAX_IMAGE = 5 * 1024 * 1024           # Google's and Instagram's limit for a picture
SIGNED_DAYS = 3                       # a signed picture address works this long (Meta and Google fetch it right away)
STALE_MINUTES = 15                    # "publishing" longer than this = the worker died: shown as failed, never retried alone
PER_ROUND = 20
EDITABLE = ("draft", "scheduled", "failed")


class PostError(Exception):
    def __init__(self, message: str, reconnect: bool = False):
        super().__init__(message)
        self.reconnect = reconnect


# ---------------------------------------------------------------- where it can go
def _accounts(conn: Connection, business_id: UUID) -> dict:
    rows = conn.execute(select(A).where(A.c.business_id == business_id, A.c.active, A.c.channel.in_(("messenger", "instagram")))
                        .order_by(A.c.created_at)).mappings().all()
    out = {}
    for r in rows:
        net = "facebook" if r["channel"] == "messenger" else "instagram"
        cfg = r["config"] or {}
        if net in out or not cfg.get("page_token") or not cfg.get("page_id" if net == "facebook" else "ig_user_id"):
            continue
        out[net] = dict(r)
    return out


def needs_reconnect(acc: dict, network: str) -> bool:
    """Meta must be asked again for posting: the account was connected before posting existed (no permissions on
    record), the permissions on record lack it, or Meta already refused a post for lack of it."""
    cfg = acc.get("config") or {}
    if cfg.get("post_denied"):
        return True
    scopes = cfg.get("scopes")
    return scopes is None or POST_SCOPES[network] not in scopes


def networks(conn: Connection, biz: dict) -> list[dict]:
    """Each network: connected or not, ready to post or 'reconnect to allow posting'."""
    from . import gbp
    accs = _accounts(conn, biz["id"])
    out = []
    for net in ("facebook", "instagram"):
        acc = accs.get(net)
        out.append({"network": net, "connected": bool(acc), "name": acc["name"] if acc else None,
                    "reconnect": bool(acc) and needs_reconnect(acc, net)})
    lk = gbp.link(conn, biz["id"]) if gbp.configured() else None
    out.append({"network": "google", "connected": bool(lk and lk["location"]), "name": (lk or {}).get("location_title"),
                "reconnect": bool(lk) and lk["status"] == "reconnect", "available": gbp.configured()})
    return out


# ---------------------------------------------------------------- the AI writer
SUGGEST = {
    "idea": "Write a post about this idea from the owner.",
    "offer": "Write a post announcing this offer. Say clearly what it is and until when, if the owner said so. Never invent "
             "prices, dates or conditions the owner did not give.",
    "product": "Write a post presenting this new product or service: what it is and why a customer would like it.",
    "holiday": "Write a warm post for this holiday or date (greetings from the business, and its hours if the facts give them).",
    "review": "Write a post thanking customers, quoting this real review word for word (with the reviewer's first name "
              "only). Do not change, shorten into new words, or invent any review.",
}
NET_RULES = {"facebook": "facebook: 2-5 short lines, friendly, may include the booking link if given.",
             "instagram": "instagram: a caption of 3-6 short lines, then 5-10 relevant hashtags on one line (the city if known).",
             "google": "google: at most 1,200 characters, plain and useful (what, when, how to book or contact), no hashtags."}


def draft(biz: dict, actor: str, *, kind: str, idea: str, nets: list[str], http=None) -> dict:
    """The AI's versions -> {network: text}. Raises ai.AIError when there is no AI (the screen offers the textarea)."""
    from . import ai as AI
    from . import studio as ST
    if kind not in KINDS:
        raise Invalid("unknown kind of post", "kind")
    idea = (idea or "").strip()[:2000]
    if not idea:
        raise Invalid("write your idea first (a few words are enough)", "idea")
    nets = [n for n in NETWORKS if n in nets] or ["facebook"]
    from . import style
    with db.tx() as conn:
        facts = AI._facts(conn, biz)
        tone = (AI.settings(conn, biz["id"]) or {}).get("instructions") or ""
        voice = style.prompt(style.get(conn, biz["id"]))                # how the owner sounds (Valeria learns it)
    system = (f"You write social media posts for {biz['name']}, a small business. Write in {ST._lang_name(biz, None)}. "
              "Use only the facts given; never invent prices, dates, reviews or promises. " + voice + "\n"
              + (f"The owner's notes on tone: {tone[:1500]}\n" if tone else "") + f"Facts about the business:\n{facts[:3000]}")
    prompt = (f"{SUGGEST[kind]}\n\nThe owner's words: {idea}\n\nAnswer with JSON only, one key per network: "
              + ", ".join(nets) + ".\n" + "\n".join(NET_RULES[n] for n in nets))
    raw = ST._write(biz, system, prompt, http=http, max_tokens=1500)
    return {n: t[:MAX_TEXT[n]] for n, t in _parse(raw, nets).items()}


def _parse(raw: str, nets: list[str]) -> dict:
    """The AI's JSON (in a ``` block or not). Plain text instead = the same words for every network."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            out = {n: str(d.get(n) or "").strip() for n in nets if isinstance(d, dict)}
            if any(out.values()):
                return {n: v for n, v in out.items() if v}
        except ValueError:
            pass
    text = (raw or "").strip().strip("`").strip()
    return {n: text for n in nets} if text else {}


# ---------------------------------------------------------------- drafts, approval
def get(conn: Connection, business_id: UUID, post_id: UUID) -> dict:
    r = conn.execute(select(P).where(P.c.business_id == business_id, P.c.id == post_id)).mappings().first()
    if not r:
        raise NotFound("post")
    return dict(r)


def list_(conn: Connection, business_id: UUID, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(select(P).where(P.c.business_id == business_id, P.c.status != "cancelled")
                                          .order_by(func.coalesce(P.c.publish_at, P.c.created_at).desc()).limit(limit)).mappings()]


def _texts(texts: dict | None, nets: list[str]) -> dict:
    out = {}
    for n in nets:
        t = str((texts or {}).get(n) or "").strip()
        if len(t) > MAX_TEXT[n]:
            raise Invalid(f"{n}: at most {MAX_TEXT[n]} characters", n)
        if t:
            out[n] = t
    return out


def _nets(nets) -> list[str]:
    bad = [n for n in nets or [] if n not in ALL]
    if bad:
        raise Invalid(f"networks: {', '.join(ALL)}", "networks")
    return [n for n in ALL if n in (nets or [])]


def _hand(nets: list[str], hand) -> list[str]:
    """The networks the owner posts on themselves: every HAND network, and the others they ticked "by hand"."""
    return [n for n in nets if n in HAND or n in (hand or [])]


def _file_ok(conn: Connection, business_id: UUID, file_id: UUID | None) -> None:
    if not file_id:
        return
    F = db.files
    f = conn.execute(select(F.c.mime, F.c.size).where(F.c.business_id == business_id, F.c.id == file_id)).first()
    if not f:
        raise NotFound("file")
    if f.mime not in ("image/jpeg", "image/png") or f.size > MAX_IMAGE:
        raise Invalid("the picture must be a JPG or PNG up to 5 MB", "file")


def create(conn: Connection, biz: dict, actor: str, *, kind: str = "idea", idea: str = "", nets: list[str] | None = None,
           texts: dict | None = None, file_id: UUID | None = None, by_ai: bool = False, by_hand: list[str] | None = None) -> dict:
    if kind not in KINDS:
        raise Invalid("unknown kind of post", "kind")
    nets = _nets(nets or [])
    _file_ok(conn, biz["id"], file_id)
    row = conn.execute(insert(P).values(business_id=biz["id"], kind=kind, idea=(idea or "")[:2000], networks=nets,
                                        texts=_texts(texts, nets), file_id=file_id, by_ai=by_ai, created_by=actor,
                                        by_hand=_hand(nets, by_hand))
                       .returning(P)).mappings().one()
    log(conn, biz["id"], "post.drafted", actor, row["id"], {"networks": nets, "by_ai": by_ai})
    return dict(row)


def save(conn: Connection, biz: dict, actor: str, post_id: UUID, *, nets: list[str] | None = None, texts: dict | None = None,
         file_id: UUID | None | str = "keep", by_hand: list[str] | None = None) -> dict:
    p = get(conn, biz["id"], post_id)
    if p["status"] not in EDITABLE:
        raise Invalid("a published post can't be changed here", "status")
    nets = _nets(nets) if nets is not None else p["networks"]
    vals = {"networks": nets, "texts": _texts({**p["texts"], **(texts or {})}, nets), "updated_at": func.now(),
            "by_hand": _hand(nets, by_hand if by_hand is not None else p["by_hand"])}
    if file_id != "keep":
        _file_ok(conn, biz["id"], file_id or None)
        vals["file_id"] = file_id or None
    if p["status"] == "scheduled":                         # changed after approval: it waits for a new OK
        vals["status"] = "draft"
    if p["by_ai"]:                                         # the owner changed the AI's words: Valeria learns their voice
        from . import style
        for n, txt in vals["texts"].items():
            if p["texts"].get(n) and txt != p["texts"][n]:
                style.record(conn, biz["id"], "post", p["texts"][n], txt)
                break
    conn.execute(update(P).where(P.c.id == post_id).values(**vals))
    return get(conn, biz["id"], post_id)


def approve(conn: Connection, biz: dict, actor: str, post_id: UUID, when: datetime | None = None, now: datetime | None = None) -> dict:
    """The owner's OK: publish at `when` (None = now). Everything each network needs is checked here, not later."""
    now = now or datetime.now(timezone.utc)
    p = get(conn, biz["id"], post_id)
    if p["status"] not in ("draft", "failed"):
        raise Invalid("this post is already approved", "status")
    if not p["networks"]:
        raise Invalid("pick at least one network", "networks")
    missing = [n for n in p["networks"] if not p["texts"].get(n)]
    if missing:
        raise Invalid(f"write the text for {', '.join(missing)}", missing[0])
    auto = [n for n in p["networks"] if n not in (p["by_hand"] or [])]            # v0.21: by hand = a reminder, no door needed
    if "instagram" in auto and not p["file_id"]:
        raise Invalid("Instagram needs a picture: add one, or leave Instagram out", "file")
    nets = {n["network"]: n for n in networks(conn, biz)}
    for n in auto:
        if not nets[n]["connected"]:
            raise Invalid(f"connect {n} first (Connect), or post it yourself (by hand)", n)
    if when and when.tzinfo is None:
        raise Invalid("pick a date and time", "when")
    if when and when > now + timedelta(days=365):
        raise Invalid("up to one year ahead", "when")
    at = when if when and when > now else now
    conn.execute(update(P).where(P.c.id == post_id).values(status="scheduled", publish_at=at, claimed_at=None, updated_at=func.now()))
    log(conn, biz["id"], "post.scheduled", actor, post_id, {"at": at.isoformat(), "networks": p["networks"]})
    return get(conn, biz["id"], post_id)


def cancel(conn: Connection, biz: dict, actor: str, post_id: UUID) -> dict:
    p = get(conn, biz["id"], post_id)
    if p["status"] in ("publishing", "published"):
        raise Invalid("it is already out; remove it on the network itself", "status")
    conn.execute(update(P).where(P.c.id == post_id).values(status="cancelled", updated_at=func.now()))
    log(conn, biz["id"], "post.cancelled", actor, post_id)
    return get(conn, biz["id"], post_id)


def back_to_draft(conn: Connection, biz: dict, actor: str, post_id: UUID) -> dict:
    p = get(conn, biz["id"], post_id)
    if p["status"] != "scheduled":
        raise Invalid("only a scheduled post goes back to draft", "status")
    conn.execute(update(P).where(P.c.id == post_id, P.c.status == "scheduled").values(status="draft", updated_at=func.now()))
    return get(conn, biz["id"], post_id)


# ---------------------------------------------------------------- the picture, where Meta and Google can fetch it
def _sig(file_id, exp: int) -> str:
    return crypto.hmac_sha256_hex(config.get("VO_SECRET_KEY", required=True), f"pf:{file_id}:{exp}".encode())[:32]


def signed_url(file_id, now: datetime | None = None) -> str:
    exp = int(((now or datetime.now(timezone.utc)) + timedelta(days=SIGNED_DAYS)).timestamp())
    return f"{config.PUBLIC_URL()}/pf/{file_id}/{exp}/{_sig(file_id, exp)}"


def check_signed(file_id, exp: int, sig: str) -> bool:
    return crypto.same(sig, _sig(file_id, exp)) and exp > time.time()


def image_url(conn: Connection, biz: dict, file_id: UUID) -> str:
    """The website's own public address when the site is on and the picture is public there; else a signed one."""
    F = db.files
    pub = conn.execute(select(F.c.public).where(F.c.business_id == biz["id"], F.c.id == file_id)).scalar()
    if pub and biz.get("site_enabled") and biz.get("slug") and blocks.on(biz, "website"):
        return f"{config.PUBLIC_URL()}/s/{biz['slug']}/f/{file_id}"
    return signed_url(file_id)


def save_picture(conn: Connection, biz: dict, actor: str, raw: bytes, mime: str, name: str = "") -> dict:
    """A picture for a post: kept as a JPEG (what Instagram takes), no bigger than 5 MB."""
    from PIL import Image, UnidentifiedImageError

    from . import files as FL
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise Invalid("that file is not a picture we can open (use JPG or PNG)", "file") from None
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=88)
    f = FL.save(conn, biz, actor, raw=out.getvalue(), mime="image/jpeg", name=name or "post.jpg", kind="post")
    if f["size"] > MAX_IMAGE:
        raise Invalid("the picture must be up to 5 MB", "file")
    return f


# ---------------------------------------------------------------- publishing
def _meta_ok(r: httpx.Response) -> dict:
    try:
        j = r.json()
    except ValueError:
        raise PostError(f"Meta answered {r.status_code}") from None
    if r.status_code >= 300 or "error" in j:
        e = j.get("error") or {}
        code = e.get("code")
        if code in (10, 190) or (isinstance(code, int) and 200 <= code < 300):
            raise PostError("Meta needs your permission to post: reconnect Facebook and Instagram (Connect)", reconnect=True)
        raise PostError(f"Meta: {e.get('message') or r.status_code}")
    return j


def _facebook(cfg: dict, text: str, image: str | None, h: httpx.Client) -> dict:
    tok = {"access_token": cfg["page_token"]}
    if image:
        j = _meta_ok(h.post(f"{graph()}/{cfg['page_id']}/photos", params=tok, data={"url": image, "message": text, "published": "true"}))
        pid = j.get("post_id") or j.get("id")
    else:
        j = _meta_ok(h.post(f"{graph()}/{cfg['page_id']}/feed", params=tok, data={"message": text}))
        pid = j.get("id")
    return {"id": pid, "url": f"https://www.facebook.com/{pid}" if pid else None}


def _instagram(cfg: dict, text: str, image: str, by_ai: bool, h: httpx.Client) -> dict:
    tok = {"access_token": cfg["page_token"]}
    ig = cfg["ig_user_id"]
    lim = _meta_ok(h.get(f"{graph()}/{ig}/content_publishing_limit", params={**tok, "fields": "config,quota_usage"}))
    row = (lim.get("data") or [{}])[0]
    total = (row.get("config") or {}).get("quota_total")
    if total is not None and int(row.get("quota_usage") or 0) >= int(total):
        raise PostError(f"Instagram's limit for today is reached ({total} posts in 24 h); try again tomorrow")
    body = {"image_url": image, "caption": text}
    if by_ai and (config.get("VO_META_AI_LABEL") or "off").lower() == "on":
        body["is_ai_generated"] = "true"                      # only where the operator confirmed the Graph version takes it
    c = _meta_ok(h.post(f"{graph()}/{ig}/media", params=tok, data=body)).get("id")
    if not c:
        raise PostError("Instagram did not take the picture")
    for _ in range(5):                                        # a picture is usually ready at once; give it a few seconds
        st = _meta_ok(h.get(f"{graph()}/{c}", params={**tok, "fields": "status_code"})).get("status_code")
        if st in (None, "FINISHED", "PUBLISHED"):
            break
        if st in ("ERROR", "EXPIRED"):
            raise PostError("Instagram could not use the picture (it must be a JPG anyone can open)")
        time.sleep(2)
    mid = _meta_ok(h.post(f"{graph()}/{ig}/media_publish", params=tok, data={"creation_id": c})).get("id")
    link = None
    try:
        link = _meta_ok(h.get(f"{graph()}/{mid}", params={**tok, "fields": "permalink"})).get("permalink")
    except PostError:
        pass
    return {"id": mid, "url": link}


def _one(biz: dict, p: dict, net: str, image: str | None, http: httpx.Client) -> dict:
    from . import gbp
    text = p["texts"][net]
    if net == "google":
        try:
            return gbp.create_post(biz["id"], text, image, language=(biz["language"] or "en").split("-")[0], http=http)
        except gbp.GoogleError as e:
            raise PostError(str(e), reconnect=e.reconnect) from None
    with db.tx() as conn:
        acc = _accounts(conn, biz["id"]).get(net)
    if not acc:
        raise PostError(f"{net} is no longer connected")
    if (acc["config"] or {}).get("post_denied"):
        raise PostError("Meta needs your permission to post: reconnect Facebook and Instagram (Connect)", reconnect=True)
    cfg = crypto.open_(acc["config"])
    try:
        if net == "facebook":
            return _facebook(cfg, text, image, http)
        if not image:
            raise PostError("Instagram needs a picture")
        return _instagram(cfg, text, image, p["by_ai"], http)
    except PostError as e:
        if e.reconnect:                                       # remember it, so the screens say "reconnect to allow posting"
            with db.tx() as conn:
                conn.execute(update(A).where(A.c.id == acc["id"]).values(config=A.c.config.op("||")(
                    func.jsonb_build_object("post_denied", True))))
        raise


def _result(post_id: UUID, net: str, res: dict) -> None:
    with db.tx() as conn:
        conn.execute(update(P).where(P.c.id == post_id).values(
            results=P.c.results.op("||")(literal({net: res}, JSONB))))


def _claim(conn: Connection, now: datetime, post_id: UUID | None = None, business_id: UUID | None = None) -> list[dict]:
    """scheduled -> publishing, in one statement with SKIP LOCKED: a post is published by one worker only."""
    pick = select(P.c.id).where(P.c.status == "scheduled", P.c.publish_at <= now)
    if post_id:
        pick = pick.where(P.c.id == post_id, P.c.business_id == business_id)
    pick = pick.order_by(P.c.publish_at).limit(PER_ROUND).with_for_update(skip_locked=True).scalar_subquery()
    rows = conn.execute(update(P).where(P.c.id.in_(pick), P.c.status == "scheduled")
                        .values(status="publishing", claimed_at=now).returning(P)).mappings().all()
    return [dict(r) for r in rows]


def _publish(p: dict, http=None, now: datetime | None = None) -> dict:
    """Publish one claimed post on each of its networks that doesn't have it yet. -> the post."""
    h = http or httpx.Client(timeout=30)
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == p["business_id"])).mappings().one())
        image = image_url(conn, biz, p["file_id"]) if p["file_id"] else None
    done = dict(p["results"] or {})
    hand = [n for n in p["networks"] if n in (p["by_hand"] or [])]
    for net in hand:                                          # v0.21: never published by us — the owner is reminded, now
        if not (done.get(net) or {}).get("hand"):
            done[net] = {"hand": True, "id": None, "url": None, "error": None, "due_at": now.isoformat(), "done_at": None}
            _result(p["id"], net, done[net])
    for net in p["networks"]:
        if net in hand or (done.get(net) or {}).get("id"):
            continue                                          # by hand, or already out on this network: never twice
        try:
            res = {**_one(biz, p, net, image, h), "error": None, "at": now.isoformat()}
            if not res.get("id"):
                raise PostError("the network did not confirm the post")
        except PostError as e:
            res = {"id": None, "url": None, "error": str(e)[:300], "reconnect": e.reconnect, "at": now.isoformat()}
        except httpx.HTTPError as e:
            res = {"id": None, "url": None, "error": f"could not reach {net} ({type(e).__name__}); try again", "at": now.isoformat()}
        done[net] = res
        _result(p["id"], net, res)
    ok = all((done.get(n) or {}).get("id") for n in p["networks"] if n not in hand)
    with db.tx() as conn:
        conn.execute(update(P).where(P.c.id == p["id"], P.c.status == "publishing").values(
            status="published" if ok else "failed", published_at=now if ok else None, updated_at=func.now()))
        log(conn, p["business_id"], "post.published" if ok else "post.failed", "system", p["id"],
            {n: bool((done.get(n) or {}).get("id")) for n in p["networks"] if n not in hand})
        if hand:
            log(conn, p["business_id"], "post.hand_due", "system", p["id"], {"networks": hand})
        out = get(conn, p["business_id"], p["id"])
    if hand:
        _remind(biz, out, hand, http)
    return out


def _remind(biz: dict, p: dict, hand: list[str], http=None) -> None:
    """The owner's phone says it is time to post (Web Push to the devices they turned on; Today shows it too)."""
    from ..web.i18n import t, ui_lang
    from . import push
    lang = ui_lang(biz["language"])
    names = ", ".join(t(f"po_net_{n}", lang) for n in hand)
    words = next((p["texts"].get(n) for n in hand if p["texts"].get(n)), "")
    push.notify(biz["id"], t("sc_push_title", lang).format(networks=names), words[:150], f"/app/posts/{p['id']}", http=http)


def hand_due(conn: Connection, business_id: UUID) -> list[dict]:
    """Posts whose time came for a network the owner posts on themselves, not ticked "posted" yet (Today, the calendar)."""
    rows = conn.execute(select(P).where(P.c.business_id == business_id, P.c.by_hand != text("'{}'"),
                                        P.c.status.in_(("published", "failed"))).order_by(P.c.publish_at.desc()).limit(50)).mappings()
    out = []
    for r in rows:
        due = [n for n in r["by_hand"] if (r["results"].get(n) or {}).get("hand") and not r["results"][n].get("done_at")]
        if due:
            out.append({**dict(r), "due": due})
    return out


def posted_by_hand(conn: Connection, biz: dict, actor: str, post_id: UUID, net: str, now: datetime | None = None) -> dict:
    """The owner ticks "posted" for one network they posted on themselves."""
    p = get(conn, biz["id"], post_id)
    res = (p["results"] or {}).get(net) or {}
    if net not in (p["by_hand"] or []) or not res.get("hand"):
        raise Invalid("this one isn't waiting to be posted by hand", "network")
    if not res.get("done_at"):
        conn.execute(update(P).where(P.c.id == post_id).values(results=P.c.results.op("||")(
            literal({net: {**res, "done_at": (now or datetime.now(timezone.utc)).isoformat()}}, JSONB))))
        log(conn, biz["id"], "post.posted_by_hand", actor, post_id, {"network": net})
    return get(conn, biz["id"], post_id)


def publish_now(biz: dict, actor: str, post_id: UUID, http=None, now: datetime | None = None) -> dict:
    """'Publish now': approve and publish in this request — through the same claim as the job, so never twice."""
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        approve(conn, biz, actor, post_id, None, now)
        claimed = _claim(conn, now, post_id, biz["id"])
    if not claimed:
        with db.tx() as conn:
            return get(conn, biz["id"], post_id)
    return _publish(claimed[0], http, now)


def retry(conn: Connection, biz: dict, actor: str, post_id: UUID, now: datetime | None = None) -> dict:
    """Try the networks that failed again (the ones that have it are skipped)."""
    p = get(conn, biz["id"], post_id)
    if p["status"] != "failed":
        raise Invalid("only a post that failed can be tried again", "status")
    conn.execute(update(P).where(P.c.id == post_id, P.c.status == "failed").values(
        status="scheduled", publish_at=now or datetime.now(timezone.utc), claimed_at=None, updated_at=func.now()))
    log(conn, biz["id"], "post.retried", actor, post_id)
    return get(conn, biz["id"], post_id)


def publish_due(http=None, now: datetime | None = None) -> dict:
    """Background job (every minute): publish what is due; a post stuck in publishing is marked failed, not retried."""
    now = now or datetime.now(timezone.utc)
    stats = {"published": 0, "failed": 0, "interrupted": 0}
    with db.tx() as conn:
        stuck = conn.execute(update(P).where(P.c.status == "publishing", P.c.claimed_at < now - timedelta(minutes=STALE_MINUTES))
                             .values(status="failed", updated_at=func.now()).returning(P.c.id, P.c.business_id)).all()
        for pid, bid in stuck:
            log(conn, bid, "post.failed", "system", pid, {"interrupted": True})
        stats["interrupted"] = len(stuck)
        claimed = _claim(conn, now)
    for p in claimed:
        with db.tx() as conn:
            b = blocks.state(conn, p["business_id"])
        if not blocks.on(b, "posts"):                      # switched off (or paused) after it was approved: it waits as a draft
            with db.tx() as conn:
                conn.execute(update(P).where(P.c.id == p["id"]).values(status="draft", claimed_at=None))
            continue
        out = _publish(p, http, now)
        stats["published" if out["status"] == "published" else "failed"] += 1
    return stats


def calendar(rows: list[dict], tz) -> list[dict]:
    """Scheduled posts grouped by local day, soonest first (the calendar view)."""
    days: dict = {}
    for r in sorted((r for r in rows if r["status"] == "scheduled" and r["publish_at"]), key=lambda r: r["publish_at"]):
        days.setdefault(r["publish_at"].astimezone(tz).date(), []).append(r)
    return [{"day": d, "posts": v} for d, v in days.items()]
