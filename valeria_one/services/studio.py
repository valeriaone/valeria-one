"""The design studio: mood boards, the AI visualizer, the AI design assistant and the AI helpers that work in the office.

All AI here runs on the business's OWN key (Settings › AI), like the AI replies — so it costs this server nothing.
Pictures need a key that can draw: Google Gemini (free tier) or OpenAI. Claude reads and writes but doesn't draw.
Nothing here sends a message to a customer by itself: the AI drafts, a person taps send.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db, money, packs
from . import Invalid, NotFound, log
from . import ai as AI
from . import files as FL
from . import projects as PR

BD = db.boards
BOARD_MAX_ITEMS = 120
IMAGE_MODELS = {"gemini": "gemini-2.5-flash-image", "openai": "gpt-image-1"}
STYLES = ("modern", "minimal", "warm_minimal", "scandinavian", "japandi", "industrial", "mid_century", "mediterranean",
          "rustic_mexican", "coastal", "boho", "classic", "art_deco", "tropical")
_STYLE_WORDS = {"modern": "modern", "minimal": "minimalist", "warm_minimal": "warm minimalist with natural wood and linen",
                "scandinavian": "Scandinavian", "japandi": "Japandi", "industrial": "industrial loft", "mid_century": "mid-century modern",
                "mediterranean": "Mediterranean", "rustic_mexican": "contemporary Mexican hacienda with natural stone, clay and wood",
                "coastal": "coastal", "boho": "bohemian", "classic": "classic elegant", "art_deco": "Art Deco",
                "tropical": "tropical modern with plants and natural fibres"}
STAFF = ("receptionist", "designer", "coordinator", "collections", "marketing")
DAILY_PICTURES = 40                                    # per business per day: a slip on a loop can't empty the owner's credit


# ---------------------------------------------------------------- mood boards
def _items(items) -> list:
    if not isinstance(items, list):
        raise Invalid("the board is a list of pieces", "items")
    out = []
    for it in items[:BOARD_MAX_ITEMS]:
        if not isinstance(it, dict):
            raise Invalid("the board is a list of pieces", "items")
        t = it.get("t")
        if t not in ("img", "color", "text", "link"):
            raise Invalid("unknown piece on the board", "items")
        d = {"t": t, "x": PR._num(it.get("x", 0), 0, 100), "y": PR._num(it.get("y", 0), 0, 100),
             "w": PR._num(it.get("w", 20), 2, 100), "h": PR._num(it.get("h", 20), 2, 100)}
        if t == "img":
            try:
                d["file"] = str(UUID(str(it.get("file"))))
            except ValueError:
                raise Invalid("a picture on the board is missing", "items") from None
        elif t == "color":
            h = str(it.get("hex") or "").lower()
            if not (len(h) == 7 and h.startswith("#") and PR._hex(h)):
                raise Invalid("colours look like #d8c3a5", "items")
            d["hex"], d["name"] = h, PR._txt(it.get("name"), 80)
        elif t == "text":
            d["text"] = PR._txt(it.get("text"), 500)
        else:
            d["url"], d["text"] = PR._url(it.get("url")), PR._txt(it.get("text"), 200)
            if not d["url"]:
                raise Invalid("a link needs its address", "items")
        out.append(d)
    return out


def new_board(conn: Connection, biz: dict, actor: str, project_id: UUID, title: str, room_id: UUID | None = None) -> dict:
    PR.get(conn, biz["id"], project_id)
    title = PR._txt(title, 200)
    if not title:
        raise Invalid("name the board", "title")
    if room_id and PR.room(conn, biz["id"], room_id)["project_id"] != project_id:
        raise NotFound("room")
    row = dict(conn.execute(insert(BD).values(business_id=biz["id"], project_id=project_id, room_id=room_id, title=title)
                            .returning(BD)).mappings().one())
    PR.touch(conn, project_id)
    log(conn, biz["id"], "board.created", actor, row["id"], {"project_id": str(project_id)})
    return row


def board(conn: Connection, business_id: UUID, board_id: UUID) -> dict:
    row = conn.execute(select(BD).where(BD.c.business_id == business_id, BD.c.id == board_id)).mappings().first()
    if not row:
        raise NotFound("board")
    return dict(row)


def boards(conn: Connection, business_id: UUID, project_id: UUID, *, shared_only: bool = False) -> list[dict]:
    q = select(BD).where(BD.c.business_id == business_id, BD.c.project_id == project_id)
    if shared_only:
        q = q.where(BD.c.shared.is_(True))
    return [dict(r) for r in conn.execute(q.order_by(BD.c.created_at)).mappings()]


def save_board(conn: Connection, biz: dict, actor: str, board_id: UUID, *, items=None, title: str | None = None,
               shared: bool | None = None) -> dict:
    b = board(conn, biz["id"], board_id)
    vals: dict = {}
    if items is not None:
        clean = _items(items)
        ids = {UUID(i["file"]) for i in clean if i["t"] == "img"}
        if ids:
            ok = set(conn.execute(select(db.files.c.id).where(db.files.c.business_id == biz["id"], db.files.c.id.in_(ids))).scalars())
            if ids - ok:
                raise Invalid("a picture on the board is missing", "items")
        vals["items"] = clean
    if title is not None:
        vals["title"] = PR._txt(title, 200) or b["title"]
    if shared is not None:
        vals["shared"] = bool(shared)
    if vals:
        vals["updated_at"] = func.now()
        conn.execute(update(BD).where(BD.c.id == board_id).values(**vals))
        PR.touch(conn, b["project_id"])
        if shared and not b["shared"]:
            PR._story(conn, biz["id"], b["project_id"], "update", b["title"], actor, shared=True)
        log(conn, biz["id"], "board.saved", actor, board_id, {"fields": sorted(k for k in vals if k != "updated_at")})
    return board(conn, biz["id"], board_id)


def remove_board(conn: Connection, biz: dict, actor: str, board_id: UUID) -> None:
    b = board(conn, biz["id"], board_id)
    conn.execute(BD.delete().where(BD.c.id == board_id))
    log(conn, biz["id"], "board.deleted", actor, board_id, {"title": b["title"]})


def board_file_ids(b: dict) -> set[str]:
    return {i["file"] for i in b["items"] if i.get("t") == "img"}


# ---------------------------------------------------------------- the business's own AI
def _key(conn: Connection, biz: dict) -> tuple[str, str, str]:
    s = AI.settings(conn, biz["id"])
    if not s or not (s["api_key"] or s["provider"] == "demo"):
        raise AI.AIError("add your AI key in Settings › AI first (Google Gemini has a free tier)")
    return AI._credentials(conn, biz, s)


def _pictures_today(conn: Connection, business_id: UUID) -> int:
    E = db.events
    return conn.execute(select(func.count()).select_from(E).where(
        E.c.business_id == business_id, E.c.type == "studio.rendered", E.c.created_at > func.now() - timedelta(days=1))).scalar()


def render_room(biz: dict, actor: str, file_id: UUID, *, style: str = "", notes: str = "", keep_layout: bool = True,
                http: httpx.Client | None = None) -> dict:
    """The customer's real room, redesigned: one new picture made from their photo, saved next to it (kind render)."""
    if style and style not in STYLES:
        raise Invalid("unknown style", "style")
    notes = PR._txt(notes, 600)
    if not style and not notes:
        raise Invalid("pick a style or say what to change", "style")
    with db.tx() as conn:
        src = FL.get(conn, biz["id"], file_id)
        if not src["mime"].startswith("image/"):
            raise Invalid("the visualizer needs a photo", "file")
        raw, mime = FL.content(conn, biz["id"], file_id)
        prov, key, _ = _key(conn, biz)
        if _pictures_today(conn, biz["id"]) >= DAILY_PICTURES:
            raise AI.AIError(f"{DAILY_PICTURES} pictures today already — more tomorrow")
    prompt = ("Redesign this exact room as a photorealistic interior design visualization. "
              + ("Keep the walls, windows, doors, ceiling height and camera angle exactly as they are. " if keep_layout else "")
              + (f"Style: {_STYLE_WORDS[style]}. " if style else "")
              + (f"The designer asks: {notes}. " if notes else "")
              + "Natural daylight, realistic materials and proportions, no people, no text or watermarks.")
    out, out_mime = _draw(prov, key, prompt, raw, mime, AI._http(http))
    with db.tx() as conn:
        row = FL.save(conn, biz, actor, raw=out, mime=out_mime, kind="render", project_id=src["project_id"],
                      room_id=src["room_id"], caption=(_STYLE_WORDS.get(style, "") + (f" · {notes}" if notes else "")).strip(" ·")[:500],
                      source_id=file_id, name=f"render-{style or 'custom'}.jpg")
        log(conn, biz["id"], "studio.rendered", actor, row["id"], {"provider": prov, "style": style, "source": str(file_id)})
    return row


def _draw(provider: str, key: str, prompt: str, raw: bytes, mime: str, http: httpx.Client) -> tuple[bytes, str]:
    try:
        if provider == "gemini":
            model = config.get("VO_IMAGE_MODEL_GEMINI") or IMAGE_MODELS["gemini"]
            r = http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          headers={"x-goog-api-key": key}, timeout=120,
                          json={"contents": [{"role": "user", "parts": [
                              {"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}}, {"text": prompt}]}],
                              "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}})
            j = AI._json(r)
            for c in j.get("candidates", []):
                for p in (c.get("content") or {}).get("parts", []):
                    d = p.get("inlineData") or p.get("inline_data")
                    if d and d.get("data"):
                        return base64.b64decode(d["data"]), d.get("mimeType") or d.get("mime_type") or "image/png"
            raise AI.AIError("Gemini didn't send a picture back — try again, or change the words")
        if provider == "openai":
            model = config.get("VO_IMAGE_MODEL_OPENAI") or IMAGE_MODELS["openai"]
            r = http.post("https://api.openai.com/v1/images/edits", headers={"Authorization": f"Bearer {key}"}, timeout=180,
                          data={"model": model, "prompt": prompt, "size": "1536x1024", "n": "1"},
                          files={"image": ("room.jpg" if mime == "image/jpeg" else "room.png", raw, mime)})
            j = AI._json(r)
            try:
                return base64.b64decode(j["data"][0]["b64_json"]), "image/png"
            except (KeyError, IndexError, TypeError):
                raise AI.AIError("OpenAI didn't send a picture back") from None
    except httpx.HTTPError as e:
        raise AI.AIError(f"could not reach the AI service ({type(e).__name__})", busy=True) from None
    raise AI.AIError("pictures need a Google Gemini or OpenAI key (Claude doesn't draw); change it in Settings › AI")


def _write(biz: dict, system: str, prompt: str, http=None, max_tokens: int = 1500) -> str:
    with db.tx() as conn:
        prov, key, model = _key(conn, biz)
    last = None
    for m in [x.strip() for x in model.split(",") if x.strip()]:
        try:
            return AI._call(prov, key, m, system, [("user", prompt)], AI._http(http), max_tokens=max_tokens)
        except httpx.HTTPError as e:
            last = AI.AIError(f"could not reach the AI service ({type(e).__name__})", busy=True)
        except AI.AIError as e:
            last = e
            if not e.busy:
                break
    raise last or AI.AIError("no model to ask")


def _lang_name(biz: dict, lang: str | None) -> str:
    return AI._LANG.get((lang or biz["language"] or "en").split("-")[0], "the business's language")


def _brief(conn: Connection, biz: dict, p: dict) -> str:
    """What the AI is told about a project: the facts the business wrote down, nothing invented."""
    cur = biz["currency"]
    rooms = PR.rooms(conn, biz["id"], p["id"])
    its = PR.items(conn, biz["id"], p["id"])
    t = PR.totals(conn, biz, p)
    fm = lambda v: "—" if v is None else money.fmt(v, cur, packs.country(biz["country"]).locale)  # noqa: E731
    lines = [f"Project: {p['name']}", f"Stage: {p['stage']}", f"Style wanted: {p['style'] or '—'}",
             f"Client brief: {p['brief'] or '—'}", f"Budget: {fm(p['budget_minor'])}", f"Approved so far: {fm(t['approved'])}",
             f"Proposed, waiting for the client: {fm(t['proposed'])}"]
    for r in rooms:
        dims = "×".join(str(v) for v in (r["width_cm"], r["length_cm"], r["height_cm"]) if v)
        lines.append(f"Room: {r['name']}" + (f" ({dims} cm)" if dims else "") + (f" — {r['notes']}" if r["notes"] else ""))
    for i in its[:80]:
        lines.append(f"Piece [{i['status']}]: {i['name']} · {i['category']}" + (f" · {i['finish']}" if i["finish"] else "")
                     + (f" · {fm(i['price_minor'])}" if i["price_minor"] is not None else ""))
    return "\n".join(lines)


ASSIST = {
    "concept": "Write the design concept for this project: a title, 2-3 short paragraphs on the idea, mood and materials, "
               "and a palette of 5 colours as hex codes with names. Base it only on the brief, rooms and pieces given.",
    "shortlist": "From the business's own library below, pick the pieces that best fit this project's style and remaining "
                 "budget, room by room, with one line on why each fits. Use only pieces from the library; say if the budget "
                 "doesn't stretch. Library:\n{library}",
    "proposal": "Write the client proposal text: a warm opening, the scope room by room, what is included, the next steps "
                "and how approvals and payments work. No prices that aren't in the facts.",
    "update": "Write a short WhatsApp update for the client about where their project is this week: what was done, what is "
              "waiting for them (approvals), and what comes next. 4-6 lines, friendly, no emojis overload.",
    "post": "Write an Instagram post for this finished project: a caption (3-5 short lines, inviting, no client names or "
            "addresses), then 8-12 relevant hashtags on one line, including the city if the facts give it.",
}


def assist(biz: dict, actor: str, project_id: UUID, what: str, *, lang: str | None = None, http=None) -> str:
    """The AI design assistant: drafts a person reads and edits. Saved to the project's story (team only)."""
    if what not in ASSIST:
        raise Invalid("unknown request", "what")
    with db.tx() as conn:
        p = PR.get(conn, biz["id"], project_id)
        facts = _brief(conn, biz, p)
        library = ""
        if what == "shortlist":
            lib = PR.catalog(conn, biz["id"], limit=150)
            if not lib:
                raise Invalid("add pieces to your library first", "what")
            library = "\n".join(f"- {c['name']} · {c['category']}" + (f" · {c['finish']}" if c["finish"] else "")
                                + (f" · {money.fmt(c['price_minor'], biz['currency'], packs.country(biz['country']).locale)}"
                                   if c["price_minor"] is not None else "") for c in lib)
        name = biz["name"]
    system = (f"You are the design assistant at {name}, a {packs.industry(biz['industry']).name.get('en', 'design')} business. "
              f"Write in {_lang_name(biz, lang)}. Use only the facts given; never invent prices, products, dates or promises. "
              "Plain text, no markdown headings.")
    text = _write(biz, system, ASSIST[what].replace("{library}", library) + "\n\nFacts:\n" + facts, http=http).strip()[:6000]
    with db.tx() as conn:
        PR._story(conn, biz["id"], project_id, "ai", f"[{what}]\n{text}", actor, shared=False)
        log(conn, biz["id"], "studio.assist", actor, project_id, {"what": what})
    return text


# ---------------------------------------------------------------- the AI helpers (who does what in the office)
def staff(biz: dict) -> dict:
    """Which AI helpers are on. The receptionist is the AI reply setting (Settings › AI); the others live here."""
    own = biz.get("ai_staff") or {}
    return {k: bool(own.get(k, k in ("designer", "coordinator"))) for k in STAFF if k != "receptionist"}


def set_staff(conn: Connection, biz: dict, actor: str, on: list[str]) -> dict:
    vals = {k: k in on for k in STAFF if k != "receptionist"}
    conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(ai_staff=vals, updated_at=func.now()))
    log(conn, biz["id"], "ai_staff.saved", actor, biz["id"], vals)
    return vals


def coordinator_round(today: date | None = None) -> dict:
    """Each jobs round, for every business using projects: late supplier orders (coordinator) and payment links
    unpaid after 3 days (collections) become one to-do each for the team; a project just finished gets its Instagram
    post drafted (marketing). Idempotent: an open reminder or a drafted post is never added twice."""
    today = today or datetime.now(timezone.utc).date()
    made = 0
    with db.tx() as conn:
        bizs = [dict(r) for r in conn.execute(select(db.businesses).where(db.businesses.c.suspended_at.is_(None))).mappings()]
    for biz in bizs:
        from .. import blocks
        if not blocks.on(biz, "projects"):
            continue
        with db.tx() as conn:
            open_titles = set(conn.execute(select(db.tasks.c.title).where(db.tasks.c.business_id == biz["id"],
                                                                          db.tasks.c.done_at.is_(None),
                                                                          db.tasks.c.created_by == "ai:coordinator")).scalars())
            for o in (PR.late_orders(conn, biz["id"], today) if staff(biz).get("coordinator") else []):
                title = f"PO #{o['number']} · {o['supplier_name'] or '—'} · {o['project_name']}: {o['expected_on'].isoformat()} ⚠"
                if title not in open_titles:
                    PR.add_task(conn, biz, "ai:coordinator", {"title": title, "project_id": o["project_id"], "due_on": today})
                    made += 1
            if staff(biz).get("collections"):                 # payment links still unpaid after 3 days: someone follows up
                PY = db.payments
                for pay in conn.execute(select(PY).where(PY.c.business_id == biz["id"], PY.c.status == "pending",
                                                         PY.c.project_id.is_not(None),
                                                         PY.c.created_at < func.now() - timedelta(days=3))).mappings():
                    amount = money.fmt(pay["amount_minor"], pay["currency"], packs.country(biz["country"]).locale)
                    title = f"$ {pay['description']} · {amount} · {pay['created_at'].date().isoformat()}"
                    if title not in open_titles:
                        PR.add_task(conn, biz, "ai:coordinator", {"title": title, "project_id": pay["project_id"], "due_on": today})
                        made += 1
        if staff(biz).get("marketing"):                       # a project just finished: its Instagram post, drafted once
            with db.tx() as conn:
                P, UP = db.projects, db.project_updates
                done = conn.execute(select(P.c.id).where(P.c.business_id == biz["id"], P.c.status == "done",
                                                         P.c.updated_at > func.now() - timedelta(days=2))).scalars().all()
                drafted = set(conn.execute(select(UP.c.project_id).where(UP.c.business_id == biz["id"], UP.c.kind == "ai",
                                                                         UP.c.body.like("[post]%"))).scalars())
            for pid in done:
                if pid in drafted:
                    continue
                try:
                    assist(biz, "ai:marketing", pid, "post")
                    made += 1
                except (AI.AIError, Invalid):
                    break                                      # no key, or the AI is busy: tomorrow
    with db.tx() as conn:                                     # "see your room" pictures from the website: kept a week
        F = db.files
        gone = conn.execute(F.delete().where(F.c.kind == "site", F.c.project_id.is_(None),
                                             F.c.created_at < func.now() - timedelta(days=7))).rowcount
    return {"reminders": made, "previews_removed": gone}


def stats(conn: Connection, business_id: UUID) -> dict:
    """The numbers for the studio's front page."""
    P = db.projects
    act = conn.execute(select(func.count()).select_from(P).where(P.c.business_id == business_id, P.c.status == "active")).scalar()
    IT = db.project_items
    waiting = conn.execute(select(func.count()).select_from(IT).join(P, P.c.id == IT.c.project_id).where(
        IT.c.business_id == business_id, IT.c.status == "proposed", P.c.status == "active")).scalar()
    return {"active": act, "waiting": waiting, "late": len(PR.late_orders(conn, business_id)),
            "tasks": len(PR.tasks(conn, business_id))}
