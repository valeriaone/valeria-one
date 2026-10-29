"""Valeria on screen (/app/valeria): talk to her here too, answer her questions with one tap, link your phone, and
decide what she may do on her own. Plus the 3-question setup (/app/valeria/setup), loyalty, and "help me set it
up" (/app/helpers) with the helpers' own public pages (/helpers) and the admin's approval (Server › Helpers)."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import select

from .. import db
from ..services import Invalid
from ..services import actions as ACT
from ..services import commands as CMD
from ..services import helpers as HP
from ..services import loyalty as LY
from ..services import owner as OW
from ..services import reminders as RM
from ..services import style as STY
from . import _admin, _ctx, _flash, _form, _go, _guard, _lang, _multi_form, _need, _not_shared_demo, _own, render

router = APIRouter(include_in_schema=False)
AM = db.assistant_messages


def _vctx(request: Request) -> dict:
    ctx = _ctx(request)
    _need(request, ctx, "assistant")
    return ctx


def _me(conn, ctx) -> dict | None:
    """Who is talking on the web: the owner (None = everything), or a staff member with their number's permissions."""
    if ctx["role"] == "owner":
        return None
    row = None
    if ctx["user"]:
        row = conn.execute(select(db.owner_numbers).where(db.owner_numbers.c.business_id == ctx["biz"]["id"],
                                                          db.owner_numbers.c.user_id == ctx["user"]["id"],
                                                          db.owner_numbers.c.active)).mappings().first()
    return dict(row) if row else {"id": None, "role": "staff", "perms": list(OW.STAFF_DEFAULT), "lang": None}


def _chat(conn, business_id) -> list[dict]:
    rows = conn.execute(select(AM).where(AM.c.business_id == business_id, AM.c.channel_account_id.is_(None))
                        .order_by(AM.c.created_at.desc()).limit(30)).mappings()
    return [dict(r) for r in reversed(list(rows))]


@router.get("/app/valeria")
@_guard
async def valeria_page(request: Request):
    ctx = _vctx(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    from ..services import ai as AI
    from ..services import holidays as HO
    with db.tx() as conn:
        me = _me(conn, ctx)
        s = OW.settings(conn, biz["id"])
        qs = OW.waiting(conn, biz["id"], me)
        nums = OW.numbers(conn, biz["id"]) if ctx["role"] == "owner" else []
        log = OW.done_today(conn, biz)
        chat = _chat(conn, biz["id"])
        prof = STY.get(conn, biz["id"])
        rem = RM.rule(conn, biz["id"])
        loy = LY.rule(conn, biz["id"])
        rewards = LY.rewards(conn, biz["id"], open_only=True, limit=30)
        has_ai = bool(AI.own_key(conn, biz))
        open_q = OW.open_question(conn, biz["id"], me)
    year = datetime.now(timezone.utc).year
    hols = HO.year_list(biz["country"], year)
    pending = request.session.pop("va_reply", None)
    return render(request, "valeria.html", ctx, s=s, qs=[(q, ACT.question(biz, q, lang)[1]) for q in qs], nums=nums,
                  log=[a for a in log if a["kind"] not in ("pick", "summary")], chat=chat, prof=prof, rem=rem, loy=loy,
                  rewards=rewards, has_ai=has_ai, hols=hols, HO=HO, grantable=OW.GRANTABLE, always=OW.ALWAYS_ASK, perms=OW.PERMS,
                  tones=STY.TONES, emojis=STY.EMOJI, pending=pending, open_q=open_q, choices=ACT.choices,
                  pair=request.session.pop("va_pair", None), page="valeria")


@router.post("/app/valeria/say")
@_guard
async def valeria_say(request: Request):
    """The web chat: the same Valeria as on WhatsApp (a picture here becomes a post too)."""
    ctx = _vctx(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    biz = ctx["biz"]
    text = str(f.get("text") or "").strip()[:4000]
    up = f.get("photo")
    raw = await up.read() if hasattr(up, "read") else b""
    with db.tx() as conn:
        me = _me(conn, ctx)
        conn.execute(AM.insert().values(business_id=biz["id"], number_id=(me or {}).get("id"), direction="in", body=text or ("📷" if raw else ""),
                                        status="received", handled_at=datetime.now(timezone.utc)))
    if raw:
        out = CMD.photo(biz, me, raw, getattr(up, "content_type", "") or "image/jpeg", text, user=ctx["user"])
    else:
        out = CMD.handle(biz, me, text, user=ctx["user"])
    body = OW.numbered(out["text"], out.get("options"))
    with db.tx() as conn:
        conn.execute(AM.insert().values(business_id=biz["id"], number_id=(me or {}).get("id"), direction="out", body=body[:4000], status="sent"))
    request.session["va_reply"] = {"options": out.get("options") or []}
    return _go("/app/valeria#chat")


@router.post("/app/valeria/q/{action_id}")
@_guard
async def valeria_answer(request: Request, action_id: UUID):
    """One tap on a question: yes (or a choice) · no · later. A reply can be changed before saying yes."""
    ctx = _vctx(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    ans = f.get("answer") if f.get("answer") in ("yes", "no", "later") else "yes"
    with db.tx() as conn:
        me = _me(conn, ctx)
        q = OW.get(conn, biz["id"], action_id)
        if not OW.may(me, q["kind"]):
            _flash(request, "va_not_allowed_web", "warn")
            return _go("/app/valeria")
        text = (f.get("text") or "").strip() or None
        if q["kind"] == "reply" and ans == "yes" and not (text or (q["payload"] or {}).get("text")):
            raise Invalid(OW.t("va_q_reply_what", lang).format(name="—"), "text")
        if text and q["kind"] == "reply" and (q["payload"] or {}).get("draft"):
            STY.record(conn, biz["id"], "assistant", q["payload"]["draft"], text)
        choice = f.get("choice") if f.get("choice") in ACT.choices(q["kind"]) else None
        if choice == "no":
            ans = "no"
        a = OW.decide(conn, biz, action_id, ans, ctx["actor"], choice=choice or (ACT.choices(q["kind"])[0] if ans == "yes" else None),
                      text=text)
    if a["status"] == "queued" and a["send_at"] and a["send_at"] <= datetime.now(timezone.utc):
        say = OW.run_now(biz, action_id)
        if say:
            _flash(request, "ok", "ok", raw=say)
        else:
            _flash(request, "saved")
    elif a["status"] == "queued":
        _flash(request, "va_queued_web")
    else:
        _flash(request, "saved")
    return _go("/app/valeria#questions")


@router.post("/app/valeria/{act}")
@_guard
async def valeria_act(request: Request, act: str):
    """stop · resume · undo · settings · autonomy · style · tone · reminders · holidays · loyalty · pair · phone."""
    if act not in ("stop", "resume", "undo", "settings", "autonomy", "style", "tone", "reminders", "holidays", "loyalty", "pair", "phone"):
        from ..services import NotFound
        raise NotFound("page")
    ctx = _vctx(request) if act in ("stop", "undo") else _own(request)
    _need(request, ctx, "assistant")
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    go = "/app/valeria"
    with db.tx() as conn:
        if act == "stop":
            OW.pause(conn, biz, ctx["actor"])
            _flash(request, "va_stopped_web")
        elif act == "resume":
            OW.resume(conn, biz, ctx["actor"])
            _flash(request, "va_resumed_web")
        elif act == "undo":
            n = len(OW.undo(conn, biz, ctx["actor"]))
            _flash(request, "ok", "ok", raw=OW.t("va_undone", lang).format(n=n) if n else OW.t("va_nothing_to_undo", lang))
        elif act == "settings":
            OW.save_settings(conn, biz, ctx["actor"], **{k: f.get(k) == "1" for k in OW.BOOLS})
            _flash(request, "saved")
            go += "#settings"
        elif act == "autonomy":
            OW.grant(conn, biz, ctx["actor"], str(f.get("kind") or ""), f.get("on") == "1")
            _flash(request, "saved")
            go += "#autonomy"
        elif act == "tone":
            STY.set_tone(conn, biz, ctx["actor"], str(f.get("tone") or ""))
            _flash(request, "saved")
            go = str(f.get("next") or "/app/valeria#style")
        elif act == "style":
            STY.save(conn, biz, ctx["actor"], {k: f.get(k) for k in STY.FIELDS if f.get(k) is not None})
            _flash(request, "saved")
            go += "#style"
        elif act == "reminders":
            RM.set_on(conn, biz, ctx["actor"], f.get("on") == "1", hours=int(f.get("hours") or 24) if f.get("hours") else None)
            _flash(request, "saved")
            go += "#reminders"
        elif act == "holidays":
            from ..services import holidays as HO
            keys = {h["key"] for h in HO.entries(biz["country"])}
            on = {str(k) for k in f.getlist("on")}
            OW.save_settings(conn, biz, ctx["actor"], holidays=f.get("holidays") == "1", holidays_skip=sorted(keys - on))
            _flash(request, "saved")
            go += "#holidays"
        elif act == "loyalty":
            LY.set_rule(conn, biz, ctx["actor"], int(f.get("every") or 0) if f.get("on") == "1" else None, str(f.get("reward") or ""))
            _flash(request, "saved")
            go += "#loyalty"
        elif act == "pair":
            role = "staff" if f.get("role") == "staff" else "owner"
            row = OW.start_pairing(conn, biz, ctx["actor"], name=str(f.get("name") or "")[:200], role=role,
                                   perms=[str(p) for p in f.getlist("perm")], user_id=ctx["user"]["id"] if ctx["user"] and role == "owner" else None,
                                   lang=lang)
            request.session["va_pair"] = row["pair_code"]
            go = str(f.get("next") or "/app/valeria#phones")
        elif act == "phone":
            role = "staff" if f.get("role") == "staff" else "owner"
            OW.add_phone(conn, biz, ctx["actor"], phone=str(f.get("phone") or ""), name=str(f.get("name") or ""), role=role,
                         perms=[str(p) for p in f.getlist("perm")], user_id=ctx["user"]["id"] if ctx["user"] and role == "owner" else None,
                         lang=lang)
            _flash(request, "va_linked")
            go = str(f.get("next") or "/app/valeria#phones")
    if not go.startswith("/app/"):
        go = "/app/valeria"
    return _go(go)


@router.post("/app/valeria/numbers/{number_id}/{act}")
@_guard
async def valeria_number(request: Request, number_id: UUID, act: str):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    with db.tx() as conn:
        if act == "remove":
            OW.remove_number(conn, ctx["biz"]["id"], ctx["actor"], number_id)
        elif act == "perms":
            OW.set_perms(conn, ctx["biz"]["id"], ctx["actor"], number_id, [str(p) for p in f.getlist("perm")])
        else:
            from ..services import NotFound
            raise NotFound("page")
    _flash(request, "saved")
    return _go("/app/valeria#phones")


# ---------------------------------------------------------------- setup: one question per screen
@router.get("/app/valeria/setup")
@_guard
async def valeria_setup(request: Request, step: int = 1):
    ctx = _own(request)
    _need(request, ctx, "assistant")
    biz = ctx["biz"]
    with db.tx() as conn:
        nums = [n for n in OW.numbers(conn, biz["id"]) if n["channel"]]
        from ..services import inbox as X
        accs = [a for a in X.accounts(conn, biz["id"]) if a["channel"] in OW.CHANNELS]
        prof = STY.get(conn, biz["id"])
    step = step if step in (1, 2, 3, 4) else 1
    return render(request, "valeria_setup.html", ctx, step=step, nums=nums, accs=accs, prof=prof,
                  pair=request.session.pop("va_pair", None), page="valeria")


# ---------------------------------------------------------------- loyalty on a customer's page
@router.post("/app/contacts/{contact_id}/visit")
@_guard
async def contact_visit(request: Request, contact_id: UUID):
    ctx = _ctx(request)
    _need(request, ctx, "loyalty")
    _not_shared_demo(request, ctx)
    await _form(request)
    with db.tx() as conn:
        r = LY.mark_visit(conn, ctx["biz"], ctx["actor"], contact_id)
    lang = _lang(request, ctx["biz"])
    _flash(request, "ok", "ok", raw=OW.t("va_visit_marked", lang).format(name="✓", n=r["visits"])
           + (" " + OW.t("va_visit_reward", lang).format(reward=r["reward"]["reward"]) if r["reward"] else ""))
    return _go(f"/app/contacts/{contact_id}")


@router.post("/app/loyalty/{reward_id}/used")
@_guard
async def loyalty_used(request: Request, reward_id: UUID):
    ctx = _ctx(request)
    _need(request, ctx, "loyalty")
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        LY.mark_used(conn, ctx["biz"]["id"], ctx["actor"], reward_id)
    _flash(request, "saved")
    back = str(f.get("next") or "/app/valeria#loyalty")
    return _go(back if back.startswith("/app/") else "/app/valeria#loyalty")


# ---------------------------------------------------------------- "help me set it up" (owners)
@router.get("/app/helpers")
@_guard
async def helpers_page(request: Request, step: int = 0, needs: str = "", helper: str = ""):
    ctx = _own(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    from ..services import directory as DR
    with db.tx() as conn:
        city = DR.settings(conn, biz["id"])["city"]
        found = HP.matches(conn, biz, lang, city)
        mine = HP.leads_of_business(conn, biz["id"])
    picked = next((h for h in found if str(h["id"]) == helper), None)
    return render(request, "helpers.html", ctx, step=step if step in (0, 1, 2, 3) else 0, found=found, mine=mine,
                  needs=[n for n in needs.split(",") if n in HP.NEEDS], all_needs=HP.NEEDS, picked=picked, city=city,
                  page="valeria")


@router.post("/app/helpers")
@_guard
async def helpers_request(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    biz = ctx["biz"]
    hid = None
    if f.get("helper"):
        try:
            hid = UUID(str(f.get("helper")))
        except ValueError:
            hid = None
    with db.tx() as conn:
        HP.request(conn, biz, ctx["actor"], consent=f.get("consent") == "1", name=str(f.get("name") or ""),
                   city=str(f.get("city") or ""), needs=[str(n) for n in f.getlist("need")], note=str(f.get("note") or ""),
                   helper_id=hid, lang=_lang(request, biz))
    _flash(request, "hp_sent" if hid else "hp_soon")
    return _go("/app/helpers")


@router.post("/app/helpers/{lead_id}/rate")
@_guard
async def helpers_rate(request: Request, lead_id: UUID):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    try:
        stars = int(f.get("stars") or 0)
    except ValueError:
        stars = 0
    with db.tx() as conn:
        HP.rate(conn, ctx["biz"]["id"], ctx["actor"], lead_id, stars)
    _flash(request, "hp_thanks")
    return _go("/app/helpers")


# ---------------------------------------------------------------- helpers' own pages (public, no sign-in)
_helper_hits: dict = {}


@router.get("/helpers")
@_guard
async def helper_signup_page(request: Request):
    from . import _countries
    return render(request, "helper_public.html", None, mode="signup", countries=_countries(request))


@router.post("/helpers")
@_guard
async def helper_signup(request: Request):
    from ..services import mailer
    from . import _limit, t
    f = await _multi_form(request)
    lang = _lang(request)
    if not _limit(_helper_hits, request, 10):
        _flash(request, "hp_too_many", "warn")
        return _go("/helpers")
    with db.tx() as conn:
        if f.get("mode") == "link":
            got = HP.new_link(conn, str(f.get("email") or ""))
        else:
            got = HP.signup(conn, name=str(f.get("name") or ""), email=str(f.get("email") or ""), country=str(f.get("country") or ""),
                            city=str(f.get("city") or ""), company=str(f.get("company") or ""), phone=str(f.get("phone") or ""),
                            languages=[str(x) for x in f.getlist("lang")] or [lang], about=str(f.get("about") or ""))
    if got:
        h, token = got
        mailer.send(h["email"], t("hp_link_subject", lang), t("hp_link_body", lang).format(name=h["name"], link=HP.dashboard_url(token)))
    _flash(request, "hp_check_email")                       # the same answer either way: no way to probe who signed up
    return _go("/helpers")


@router.get("/helpers/d/{token}")
@_guard
async def helper_dashboard(request: Request, token: str):
    with db.tx() as conn:
        h = HP.by_token(conn, token)
        leads = HP.leads_of_helper(conn, h["id"])
        rt = HP.rating(conn, h["id"])
    return render(request, "helper_public.html", None, mode="dashboard", h=h, leads=leads, rt=rt, token=token, free=HP.FREE_LEADS)


@router.post("/helpers/d/{token}/{lead_id}/{act}")
@_guard
async def helper_decide(request: Request, token: str, lead_id: UUID, act: str):
    await _form(request)
    if act not in ("accept", "decline"):
        from ..services import NotFound
        raise NotFound("page")
    with db.tx() as conn:
        h = HP.by_token(conn, token)
        HP.decide(conn, h, lead_id, act == "accept")
    _flash(request, "saved")
    return _go(f"/helpers/d/{token}")


# ---------------------------------------------------------------- the server's admin approves helpers
@router.get("/app/server/helpers")
@_guard
async def server_helpers(request: Request):
    ctx = _admin(request)
    with db.tx() as conn:
        rows = HP.all_(conn)
        rec = HP.recorded(conn)
    return render(request, "server_helpers.html", ctx, rows=rows, rec=rec, page="server")


@router.post("/app/server/helpers/{helper_id}/{act}")
@_guard
async def server_helper_act(request: Request, helper_id: UUID, act: str):
    ctx = _admin(request)
    f = await _form(request)
    with db.tx() as conn:
        if act in ("approve", "unapprove"):
            HP.approve(conn, helper_id, ctx["actor"], act == "approve")
        elif act == "price":
            from .. import money
            try:
                minor = money.to_minor(str(f.get("price") or "0").strip() or "0", str(f.get("currency") or "USD"))
            except Exception:                                   # noqa: BLE001
                raise Invalid("check the amount", "price") from None
            HP.set_price(conn, helper_id, ctx["actor"], minor)
        else:
            from ..services import NotFound
            raise NotFound("page")
    _flash(request, "saved")
    return _go("/app/server/helpers")
