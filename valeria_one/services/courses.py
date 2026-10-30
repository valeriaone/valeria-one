"""Courses (ars02, v0.21): modules -> lessons, for the members of one membership (or of any), in the member area.

A lesson is text, a video (YouTube or Vimeo, played from there — or any https link) or a file (a PDF or a picture kept
here, opened only by a member who can see the lesson). Drip: a lesson opens N days after the member joined (0 = at
once). Progress: the member ticks "done"; the owner sees, per course, how far each member got. A course is seen only
once it is published, and only by active members it is for.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import db
from . import Invalid, NotFound, log
from .funnels import https, video, video_src

CO, MO, LE, LP = db.courses, db.course_modules, db.lessons, db.lesson_progress
KINDS = ("text", "video", "file")
MAX_MODULES = 30
MAX_LESSONS = 60                                   # per module


# ---------------------------------------------------------------- courses
def list_(conn: Connection, business_id: UUID) -> list[dict]:
    rows = [dict(r) for r in conn.execute(select(CO).where(CO.c.business_id == business_id).order_by(CO.c.created_at)).mappings()]
    n = dict(conn.execute(select(MO.c.course_id, func.count(LE.c.id)).join(LE, LE.c.module_id == MO.c.id)
                          .where(MO.c.business_id == business_id).group_by(MO.c.course_id)).all())
    for r in rows:
        r["lessons"] = n.get(r["id"], 0)
    return rows


def get(conn: Connection, business_id: UUID, course_id: UUID) -> dict:
    r = conn.execute(select(CO).where(CO.c.business_id == business_id, CO.c.id == course_id)).mappings().first()
    if not r:
        raise NotFound("course")
    return dict(r)


def save(conn: Connection, biz: dict, actor: str, data: dict, course_id: UUID | None = None) -> dict:
    title = " ".join(str(data.get("title") or "").split())[:160]
    if not title:
        raise Invalid("give the course a name", "title")
    mp = data.get("member_plan_id") or None
    if mp:
        from . import members as MS
        try:
            mp = MS.plan(conn, biz["id"], UUID(str(mp)))["id"]
        except ValueError:
            raise NotFound("membership") from None
    vals = {"title": title, "about": str(data.get("about") or "").strip()[:4000], "member_plan_id": mp,
            "published": bool(data.get("published"))}
    if course_id:
        get(conn, biz["id"], course_id)
        conn.execute(update(CO).where(CO.c.business_id == biz["id"], CO.c.id == course_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "course.updated", actor, course_id, {"published": vals["published"]})
        return get(conn, biz["id"], course_id)
    row = conn.execute(insert(CO).values(business_id=biz["id"], **vals).returning(CO)).mappings().one()
    log(conn, biz["id"], "course.created", actor, row["id"])
    return dict(row)


def delete(conn: Connection, biz: dict, actor: str, course_id: UUID) -> None:
    get(conn, biz["id"], course_id)
    conn.execute(CO.delete().where(CO.c.business_id == biz["id"], CO.c.id == course_id))
    log(conn, biz["id"], "course.deleted", actor, course_id)


# ---------------------------------------------------------------- modules and lessons
def modules(conn: Connection, business_id: UUID, course_id: UUID) -> list[dict]:
    """The course's modules in order, each with its lessons in order."""
    mods = [dict(r) for r in conn.execute(select(MO).where(MO.c.business_id == business_id, MO.c.course_id == course_id)
                                          .order_by(MO.c.position, MO.c.created_at)).mappings()]
    les = conn.execute(select(LE).where(LE.c.business_id == business_id, LE.c.module_id.in_([m["id"] for m in mods] or [None]))
                       .order_by(LE.c.position, LE.c.created_at)).mappings()
    by: dict = {}
    for r in les:
        by.setdefault(r["module_id"], []).append(dict(r))
    for m in mods:
        m["lessons"] = by.get(m["id"], [])
    return mods


def add_module(conn: Connection, biz: dict, actor: str, course_id: UUID, title: str) -> dict:
    get(conn, biz["id"], course_id)
    title = " ".join(str(title or "").split())[:160]
    if not title:
        raise Invalid("name the module", "title")
    have = conn.execute(select(func.count(), func.coalesce(func.max(MO.c.position), -1)).where(MO.c.course_id == course_id)).one()
    if have[0] >= MAX_MODULES:
        raise Invalid(f"at most {MAX_MODULES} modules", "title")
    return dict(conn.execute(insert(MO).values(business_id=biz["id"], course_id=course_id, title=title, position=have[1] + 1)
                             .returning(MO)).mappings().one())


def module(conn: Connection, business_id: UUID, module_id: UUID) -> dict:
    r = conn.execute(select(MO).where(MO.c.business_id == business_id, MO.c.id == module_id)).mappings().first()
    if not r:
        raise NotFound("module")
    return dict(r)


def rename_module(conn: Connection, biz: dict, module_id: UUID, title: str) -> None:
    module(conn, biz["id"], module_id)
    title = " ".join(str(title or "").split())[:160]
    if title:
        conn.execute(update(MO).where(MO.c.id == module_id).values(title=title))


def delete_module(conn: Connection, biz: dict, actor: str, module_id: UUID) -> None:
    module(conn, biz["id"], module_id)
    conn.execute(MO.delete().where(MO.c.business_id == biz["id"], MO.c.id == module_id))


def _swap(conn: Connection, T, rows: list[dict], row_id: UUID, step: int) -> None:
    i = next((n for n, x in enumerate(rows) if x["id"] == row_id), None)
    if i is None:
        raise NotFound("item")
    j = i + (1 if step > 0 else -1)
    if 0 <= j < len(rows):
        rows[i], rows[j] = rows[j], rows[i]
        for n, x in enumerate(rows):
            conn.execute(update(T).where(T.c.id == x["id"]).values(position=n))


def move_module(conn: Connection, biz: dict, module_id: UUID, step: int) -> None:
    m = module(conn, biz["id"], module_id)
    _swap(conn, MO, modules(conn, biz["id"], m["course_id"]), module_id, step)


def lesson(conn: Connection, business_id: UUID, lesson_id: UUID) -> dict:
    r = conn.execute(select(LE).where(LE.c.business_id == business_id, LE.c.id == lesson_id)).mappings().first()
    if not r:
        raise NotFound("lesson")
    return dict(r)


def save_lesson(conn: Connection, biz: dict, actor: str, module_id: UUID, data: dict, lesson_id: UUID | None = None) -> dict:
    module(conn, biz["id"], module_id)
    title = " ".join(str(data.get("title") or "").split())[:160]
    if not title:
        raise Invalid("name the lesson", "title")
    kind = data.get("kind") if data.get("kind") in KINDS else "text"
    url = str(data.get("video_url") or "").strip()
    if kind == "video":
        if not url:
            raise Invalid("the video's address (YouTube, Vimeo or any https://)", "video_url")
        try:
            video(url)
        except Invalid:
            url = https(url, "video_url")                     # not YouTube/Vimeo: shown as a link to open
    try:
        drip = int(data.get("drip_days") or 0)
    except (TypeError, ValueError):
        raise Invalid("days: a whole number", "drip_days") from None
    if not 0 <= drip <= 3650:
        raise Invalid("days: 0 to 3650", "drip_days")
    fid = data.get("file_id")
    if fid:
        if not conn.execute(select(db.files.c.id).where(db.files.c.business_id == biz["id"], db.files.c.id == fid)).first():
            raise NotFound("file")
    vals = {"title": title, "kind": kind, "body": str(data.get("body") or "").strip()[:20000], "video_url": url[:500] or None,
            "drip_days": drip}
    if fid or kind != "file":
        vals["file_id"] = fid if kind == "file" else None
    if lesson_id:
        cur = lesson(conn, biz["id"], lesson_id)
        if kind == "file" and not (fid or cur["file_id"]):
            raise Invalid("add the file", "file")
        conn.execute(update(LE).where(LE.c.business_id == biz["id"], LE.c.id == lesson_id).values(**vals, updated_at=func.now()))
        return lesson(conn, biz["id"], lesson_id)
    if kind == "file" and not fid:
        raise Invalid("add the file", "file")
    have = conn.execute(select(func.count(), func.coalesce(func.max(LE.c.position), -1)).where(LE.c.module_id == module_id)).one()
    if have[0] >= MAX_LESSONS:
        raise Invalid(f"at most {MAX_LESSONS} lessons in a module", "title")
    return dict(conn.execute(insert(LE).values(business_id=biz["id"], module_id=module_id, position=have[1] + 1, **vals)
                             .returning(LE)).mappings().one())


def move_lesson(conn: Connection, biz: dict, lesson_id: UUID, step: int) -> None:
    le = lesson(conn, biz["id"], lesson_id)
    rows = [dict(r) for r in conn.execute(select(LE).where(LE.c.module_id == le["module_id"]).order_by(LE.c.position, LE.c.created_at))
            .mappings()]
    _swap(conn, LE, rows, lesson_id, step)


def delete_lesson(conn: Connection, biz: dict, actor: str, lesson_id: UUID) -> None:
    lesson(conn, biz["id"], lesson_id)
    conn.execute(LE.delete().where(LE.c.business_id == biz["id"], LE.c.id == lesson_id))


# ---------------------------------------------------------------- what a member sees
def for_member(conn: Connection, business_id: UUID, me: dict) -> list[dict]:
    """The published courses this signed-in member may open (members.signed_in())."""
    rows = conn.execute(select(CO).where(CO.c.business_id == business_id, CO.c.published).order_by(CO.c.created_at)).mappings()
    return [dict(r) for r in rows if r["member_plan_id"] is None or r["member_plan_id"] in me["plan_ids"]]


def can_open(conn: Connection, business_id: UUID, me: dict, course_id: UUID) -> dict:
    c = get(conn, business_id, course_id)
    if not c["published"] or (c["member_plan_id"] is not None and c["member_plan_id"] not in me["plan_ids"]):
        raise NotFound("course")
    return c


def _joined(me: dict, course: dict) -> datetime:
    """Drip counts from the day they joined the membership the course is for (any course: the first one they joined)."""
    if course["member_plan_id"] is not None:
        j = me["joined"].get(course["member_plan_id"])
    else:
        j = min((x for x in me["joined"].values() if x), default=None)
    return j or datetime.now(timezone.utc)


def _member_id(me: dict, course: dict) -> UUID:
    if course["member_plan_id"] is not None:
        return me["member_ids"][course["member_plan_id"]]
    return sorted(me["member_ids"].values(), key=str)[0]


def outline(conn: Connection, biz: dict, me: dict, course: dict, now: datetime | None = None) -> dict:
    """The course as this member sees it: every lesson with open (drip) / done, and how far they are."""
    now = now or datetime.now(timezone.utc)
    joined = _joined(me, course)
    mods = modules(conn, biz["id"], course["id"])
    done = {r[0] for r in conn.execute(select(LP.c.lesson_id).where(LP.c.business_id == biz["id"],
                                                                    LP.c.member_id.in_(list(me["member_ids"].values()))))}
    total = n_done = 0
    for m in mods:
        for le in m["lessons"]:
            opens = joined + timedelta(days=le["drip_days"])
            le.update(open=opens <= now, opens=opens, done=le["id"] in done)
            total += 1
            n_done += le["done"]
    return {"modules": mods, "total": total, "done": n_done, "pct": round(100 * n_done / total) if total else 0}


def open_lesson(conn: Connection, biz: dict, me: dict, lesson_id: UUID, now: datetime | None = None) -> tuple[dict, dict]:
    """A lesson a member may open now -> (lesson, course); NotFound when not theirs or not open yet (drip)."""
    le = lesson(conn, biz["id"], lesson_id)
    m = module(conn, biz["id"], le["module_id"])
    c = can_open(conn, biz["id"], me, m["course_id"])
    if _joined(me, c) + timedelta(days=le["drip_days"]) > (now or datetime.now(timezone.utc)):
        raise NotFound("lesson")
    le["src"] = video_src(video(le["video_url"])) if le["kind"] == "video" and _embeddable(le["video_url"]) else None
    return le, c


def _embeddable(url: str | None) -> bool:
    try:
        return bool(video(url))
    except Invalid:
        return False


def mark_done(conn: Connection, biz: dict, me: dict, lesson_id: UUID) -> None:
    le, c = open_lesson(conn, biz, me, lesson_id)
    conn.execute(pg_insert(LP).values(business_id=biz["id"], member_id=_member_id(me, c), lesson_id=le["id"])
                 .on_conflict_do_nothing(constraint="uq_lesson_done"))


def progress(conn: Connection, biz: dict, course_id: UUID) -> list[dict]:
    """The owner's view: each member who can open the course, how many lessons done of how many."""
    from . import members as MS
    c = get(conn, biz["id"], course_id)
    total = conn.execute(select(func.count(LE.c.id)).join(MO, MO.c.id == LE.c.module_id).where(MO.c.course_id == course_id)).scalar()
    ms = MS.members(conn, biz["id"], c["member_plan_id"], "active") if c["member_plan_id"] else MS.members(conn, biz["id"], None, "active")
    done = dict(conn.execute(select(LP.c.member_id, func.count()).join(LE, LE.c.id == LP.c.lesson_id).join(MO, MO.c.id == LE.c.module_id)
                             .where(LP.c.business_id == biz["id"], MO.c.course_id == course_id).group_by(LP.c.member_id)).all())
    seen, out = set(), []
    for m in ms:                                              # one row per customer (someone in two memberships counts once)
        if m["contact_id"] in seen:
            continue
        seen.add(m["contact_id"])
        n = sum(v for k, v in done.items() if k in {x["id"] for x in ms if x["contact_id"] == m["contact_id"]})
        out.append({"name": m["name"], "contact_id": m["contact_id"], "done": n, "total": total,
                    "pct": round(100 * n / total) if total else 0})
    return out
