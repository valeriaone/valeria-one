"""ars02 (v0.21) on screen: Memberships (/app/members: what the business offers, who is in, who asked, the community board
to look after), Courses (/app/courses: modules -> lessons, drip, progress) — and the member area at the business's own
address (/s/<business>/members), in its look: sign in with a one-time link, the courses, the board."""
from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import db
from ..services import Invalid, NotFound
from ..services import courses as CO
from ..services import members as MS
from . import _flash, _form, _go, _guard, _lang, _limit, _multi_form, _not_shared_demo, _own, render
from .i18n import t

router = APIRouter(include_in_schema=False)
_login_hits: dict = {}
_join_hits: dict = {}


def _uuid(v, what: str = "member") -> UUID:
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound(what) from None


# ---------------------------------------------------------------- the office: memberships and members
@router.get("/app/members")
@_guard
async def members_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    from ..services import contacts as C
    from ..services import invoices as IVS
    with db.tx() as conn:
        mps = MS.plans(conn, biz["id"])
        rows = MS.members(conn, biz["id"])
        asked = MS.asked(conn, biz["id"])
        prices = IVS.plans(conn, biz["id"])
        people = C.search(conn, biz["id"], limit=300)
        from ..services import businesses as B
        biz = B.get(conn, biz["id"])
    shared = request.session.pop("ms_link", None)
    return render(request, "members.html", {**ctx, "biz": biz}, mps=mps, rows=rows, prices=prices, people=people, shared=shared, asked=asked,
                  area=f"/s/{biz['slug']}/members" if biz.get("slug") else None, page="members")


@router.post("/app/members")
@_guard
async def members_act(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    act = str(f.get("act") or "")
    biz = ctx["biz"]
    with db.tx() as conn:
        if act == "plan":
            data = {k: f.get(k) for k in ("name", "about", "plan_id")}
            data.update(community=f.get("community") == "1", open_join=f.get("open_join") == "1", active=f.get("active", "1") == "1")
            mp = MS.save_plan(conn, biz, ctx["actor"], data, _uuid(f.get("id"), "membership") if f.get("id") else None)
            _flash(request, "saved")
            return _go(f"/app/members#mp-{mp['id']}")
        if act == "add":
            MS.add(conn, biz, ctx["actor"], _uuid(f.get("member_plan_id"), "membership"), _uuid(f.get("contact_id"), "contact"),
                   auto_send=f.get("auto_send") == "1")
            _flash(request, "ms_added")
        elif act in ("yes", "pause", "resume", "remove"):
            MS.set_status(conn, biz, ctx["actor"], _uuid(f.get("member_id")), act)
        elif act == "link":                                  # the owner copies a member's sign-in link to share by hand
            m = MS.get(conn, biz["id"], _uuid(f.get("member_id")))
            link = MS.share_link(conn, biz, ctx["actor"], m["id"])
            request.session["ms_link"] = {"link": link, "member_id": str(m["id"])}
            return _go(f"/app/members#m-{m['id']}")
        else:
            raise Invalid("?", "act")
    return _go("/app/members")


@router.get("/app/members/board/{mp_id}")
@_guard
async def board_owner(request: Request, mp_id: str):
    ctx = _own(request)
    with db.tx() as conn:
        mp = MS.plan(conn, ctx["biz"]["id"], _uuid(mp_id, "membership"))
        posts = MS.board(conn, ctx["biz"], mp["id"], with_hidden=True)
    return render(request, "member_board.html", ctx, mp=mp, posts=posts, owner_view=True, page="members")


@router.post("/app/members/board/{mp_id}")
@_guard
async def board_owner_act(request: Request, mp_id: str):
    """The owner posts or answers as the business, and hides (or shows again) any post or comment."""
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    act, biz = f.get("act") or "", ctx["biz"]
    mpu = _uuid(mp_id, "membership")
    with db.tx() as conn:
        MS.plan(conn, biz["id"], mpu)
        if act == "post":
            MS.post(conn, biz, mpu, f.get("body", ""), None, ctx["actor"])
        elif act == "comment":
            MS.comment(conn, biz, _uuid(f.get("post_id"), "post"), f.get("body", ""), None, ctx["actor"])
        elif act in ("hide", "show"):
            what = "post" if f.get("what") == "post" else "comment"
            MS.hide(conn, biz, ctx["actor"], what, _uuid(f.get("item_id"), what), act == "hide")
        else:
            raise Invalid("?", "act")
    return _go(f"/app/members/board/{mp_id}")


# ---------------------------------------------------------------- the office: courses
@router.get("/app/courses")
@_guard
async def courses_page(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        rows = CO.list_(conn, ctx["biz"]["id"])
        mps = MS.plans(conn, ctx["biz"]["id"])
    return render(request, "courses.html", ctx, rows=rows, mps=mps, page="courses")


@router.post("/app/courses")
@_guard
async def courses_new(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        c = CO.save(conn, ctx["biz"], ctx["actor"], {"title": f.get("title"), "about": f.get("about"),
                                                     "member_plan_id": f.get("member_plan_id")})
        CO.add_module(conn, ctx["biz"], ctx["actor"], c["id"], t("co_first_module", _lang(request, ctx["biz"])))
    return _go(f"/app/courses/{c['id']}")


@router.get("/app/courses/{cid}")
@_guard
async def course_page(request: Request, cid: str):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        c = CO.get(conn, biz["id"], _uuid(cid, "course"))
        mods = CO.modules(conn, biz["id"], c["id"])
        mps = MS.plans(conn, biz["id"])
        prog = CO.progress(conn, biz, c["id"])
    edit = request.query_params.get("lesson")
    return render(request, "course.html", ctx, c=c, mods=mods, mps=mps, prog=prog, edit=edit, kinds=CO.KINDS, page="courses")


@router.post("/app/courses/{cid}")
@_guard
async def course_act(request: Request, cid: str):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    biz = ctx["biz"]
    from .studio import _upload
    f, ups = await _upload(request)
    act, _, arg = str(f.get("act") or "").partition(":")
    cu = _uuid(cid, "course")
    back = f"/app/courses/{cid}"
    with db.tx() as conn:
        CO.get(conn, biz["id"], cu)
        if act == "save":
            CO.save(conn, biz, ctx["actor"], {"title": f.get("title"), "about": f.get("about"), "member_plan_id": f.get("member_plan_id"),
                                              "published": f.get("published") == "1"}, cu)
            _flash(request, "saved")
        elif act == "delete":
            CO.delete(conn, biz, ctx["actor"], cu)
            return _go("/app/courses")
        elif act == "module":
            CO.add_module(conn, biz, ctx["actor"], cu, str(f.get("module_title") or ""))
        elif act in ("mod_up", "mod_down", "mod_drop", "mod_name"):
            mid = _uuid(arg, "module")
            if CO.module(conn, biz["id"], mid)["course_id"] != cu:
                raise NotFound("module")
            if act == "mod_drop":
                CO.delete_module(conn, biz, ctx["actor"], mid)
            elif act == "mod_name":
                CO.rename_module(conn, biz, mid, str(f.get(f"mt-{mid}") or ""))
            else:
                CO.move_module(conn, biz, mid, 1 if act == "mod_down" else -1)
        elif act == "lesson":                               # new (arg = module) or changed (lesson_id field)
            from ..services import files as FL
            mid = _uuid(arg or f.get("module_id"), "module")
            if CO.module(conn, biz["id"], mid)["course_id"] != cu:
                raise NotFound("module")
            fid = None
            if ups:
                raw, mime, name = ups[0]
                fid = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name or "lesson",
                              kind="doc" if mime == "application/pdf" else "photo")["id"]
            le = CO.save_lesson(conn, biz, ctx["actor"], mid, {"title": f.get("title_l"), "kind": f.get("kind"), "body": f.get("body"),
                                                                "video_url": f.get("video_url"), "drip_days": f.get("drip_days"),
                                                                "file_id": fid},
                                _uuid(f.get("lesson_id"), "lesson") if f.get("lesson_id") else None)
            _flash(request, "saved")
            return _go(f"{back}#l-{le['id']}")
        elif act in ("les_up", "les_down", "les_drop"):
            lid = _uuid(arg, "lesson")
            le = CO.lesson(conn, biz["id"], lid)
            if CO.module(conn, biz["id"], le["module_id"])["course_id"] != cu:
                raise NotFound("lesson")
            if act == "les_drop":
                CO.delete_lesson(conn, biz, ctx["actor"], lid)
            else:
                CO.move_lesson(conn, biz, lid, 1 if act == "les_down" else -1)
        else:
            raise Invalid("?", "act")
    return _go(back)


# ---------------------------------------------------------------- the member area (customer-facing, the business's look)
def _area(request: Request, slug: str):
    """(business, the signed-in member at THIS business or None). A member of another business is not signed in here."""
    from sqlalchemy import select
    with db.tx() as conn:
        biz = conn.execute(select(db.businesses).where(db.businesses.c.slug == (slug or "").lower()[:60])).mappings().first()
        if not biz or biz["suspended_at"]:
            raise NotFound("page")
        biz = dict(biz)
        if not MS.plans(conn, biz["id"], active_only=True):
            raise NotFound("page")
        me = MS.signed_in(conn, biz, request.session.get("member"))
    return biz, me


def _page(request: Request, biz: dict, name: str, status: int = 200, **kw):
    ctx = {"biz": biz, "user": None, "actor": "member_area", "role": None, "businesses": []}
    resp = render(request, name, ctx, s=biz["site"] or {}, base=f"/s/{biz['slug']}/members", **kw)
    resp.status_code = status
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/s/{slug}/members")
@_guard
async def area_home(request: Request, slug: str):
    biz, me = _area(request, slug)
    with db.tx() as conn:
        open_plans = [p for p in MS.plans(conn, biz["id"], active_only=True) if p["open_join"]]
        courses = CO.for_member(conn, biz["id"], me) if me else []
        prog = {c["id"]: CO.outline(conn, biz, me, c)["pct"] for c in courses}
    return _page(request, biz, "member_area.html", me=me, open_plans=open_plans, courses=courses, prog=prog, view="home",
                 sent=request.query_params.get("sent"))


@router.post("/s/{slug}/members/login")
@_guard
async def area_login(request: Request, slug: str):
    biz, _ = _area(request, slug)
    f = await _form(request)
    if f.get("website"):
        return _go(f"/s/{slug}/members?sent=link")
    if _limit(_login_hits, request, 10):
        MS.send_link(biz, f.get("email", ""))
    return _go(f"/s/{slug}/members?sent=link")                # the same answer either way: nobody learns who is a member


@router.get("/s/{slug}/members/in/{token}")
@_guard
async def area_in(request: Request, slug: str, token: str):
    try:
        biz, m = MS.use_link(slug, token)
    except NotFound:
        biz, _ = _area(request, slug)
        return _page(request, biz, "member_area.html", me=None, open_plans=[], courses=[], prog={}, view="expired", status=404)
    request.session["member"] = MS.session_value(biz, m)
    return _go(f"/s/{slug}/members")


@router.post("/s/{slug}/members/out")
@_guard
async def area_out(request: Request, slug: str):
    await _form(request)
    request.session.pop("member", None)
    return _go(f"/s/{slug}/members")


@router.post("/s/{slug}/members/join/{mp_id}")
@_guard
async def area_join(request: Request, slug: str, mp_id: str):
    """A visitor asks to join: the owner says yes (nothing else happens by itself)."""
    from . import _attribute
    biz, _ = _area(request, slug)
    f = await _form(request)
    if f.get("website"):
        return _go(f"/s/{slug}/members?sent=join")
    if not _limit(_join_hits, request, 5):
        raise Invalid(t("fm_too_many", _lang(request, biz)), "rate")
    with db.tx() as conn:
        res = MS.ask_to_join(conn, biz, _uuid(mp_id, "membership"), name=f.get("name", ""), email=f.get("email", ""),
                             phone=f.get("phone", ""), note=f.get("note", ""))
        _attribute(request, conn, biz, res["contact"])
    return _go(f"/s/{slug}/members?sent=join")


def _need_me(me):
    if not me:
        raise NotFound("page")
    return me


@router.get("/s/{slug}/members/c/{cid}")
@_guard
async def area_course(request: Request, slug: str, cid: str):
    biz, me = _area(request, slug)
    if not me:
        return _go(f"/s/{slug}/members")
    with db.tx() as conn:
        c = CO.can_open(conn, biz["id"], me, _uuid(cid, "course"))
        out = CO.outline(conn, biz, me, c)
    return _page(request, biz, "member_area.html", me=me, view="course", c=c, out=out, open_plans=[], courses=[], prog={})


@router.get("/s/{slug}/members/l/{lid}")
@_guard
async def area_lesson(request: Request, slug: str, lid: str):
    biz, me = _area(request, slug)
    if not me:
        return _go(f"/s/{slug}/members")
    with db.tx() as conn:
        le, c = CO.open_lesson(conn, biz, me, _uuid(lid, "lesson"))
        out = CO.outline(conn, biz, me, c)
    flat = [x for m in out["modules"] for x in m["lessons"]]
    i = next(n for n, x in enumerate(flat) if x["id"] == le["id"])
    nxt = next((x for x in flat[i + 1:] if x["open"]), None)
    return _page(request, biz, "member_area.html", me=me, view="lesson", c=c, le=le, done=flat[i]["done"], nxt=nxt,
                 open_plans=[], courses=[], prog={})


@router.post("/s/{slug}/members/l/{lid}")
@_guard
async def area_lesson_done(request: Request, slug: str, lid: str):
    biz, me = _area(request, slug)
    await _form(request)
    _need_me(me)
    with db.tx() as conn:
        CO.mark_done(conn, biz, me, _uuid(lid, "lesson"))
    return _go(f"/s/{slug}/members/l/{lid}")


@router.get("/s/{slug}/members/f/{lid}")
async def area_lesson_file(request: Request, slug: str, lid: UUID):
    """A lesson's file: only for a signed-in member who may open that lesson now."""
    from sqlalchemy import select
    from .studio import _file
    try:
        biz, me = _area(request, slug)
        _need_me(me)
        with db.tx() as conn:
            le, _ = CO.open_lesson(conn, biz, me, lid)
            row = conn.execute(select(db.files.c.data, db.files.c.mime).where(db.files.c.business_id == biz["id"],
                                                                              db.files.c.id == le["file_id"])).first() if le["file_id"] else None
    except NotFound:
        row = None
    if not row:
        return Response("Not found", status_code=404)
    return _file(row.data, row.mime, private=True)


@router.get("/s/{slug}/members/board/{mp_id}")
@_guard
async def area_board(request: Request, slug: str, mp_id: str):
    biz, me = _area(request, slug)
    if not me:
        return _go(f"/s/{slug}/members")
    mpu = _uuid(mp_id, "membership")
    if mpu not in me["plan_ids"]:
        raise NotFound("page")
    with db.tx() as conn:
        mp = MS.plan(conn, biz["id"], mpu)
        if not mp["community"]:
            raise NotFound("page")
        posts = MS.board(conn, biz, mpu)
    return _page(request, biz, "member_area.html", me=me, view="board", mp=mp, posts=posts, open_plans=[], courses=[], prog={})


@router.post("/s/{slug}/members/board/{mp_id}")
@_guard
async def area_board_post(request: Request, slug: str, mp_id: str):
    biz, me = _area(request, slug)
    f = await _form(request)
    _need_me(me)
    mpu = _uuid(mp_id, "membership")
    if mpu not in me["plan_ids"]:
        raise NotFound("page")
    mid = me["member_ids"][mpu]
    with db.tx() as conn:
        if f.get("post_id"):
            MS.comment(conn, biz, _uuid(f.get("post_id"), "post"), f.get("body", ""), mid, "member")
        else:
            MS.post(conn, biz, mpu, f.get("body", ""), mid, "member")
    return _go(f"/s/{slug}/members/board/{mp_id}")


def wa_share(link: str, text: str) -> str:
    """One tap: the owner's WhatsApp with the words and the link (nothing is sent until they press send there)."""
    return "https://wa.me/?text=" + quote(f"{text} {link}")
