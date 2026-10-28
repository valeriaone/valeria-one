"""Files: photos, renders, plans and documents a business keeps with its projects.

They live in the database (not on a disk next to it), so the nightly backup holds them and a move to another
server takes them along. Photos are shrunk to a sensible size on the way in, with a small thumbnail beside
them; the original phone photo (often 5-10 MB) is not kept.
"""
from __future__ import annotations

import io
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db
from . import Invalid, NotFound, log

F = db.files
KINDS = ("photo", "render", "plan", "doc", "canva", "site")
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "image/gif"}
DOC_TYPES = {"application/pdf"}
MAX_IN = 25 * 1024 * 1024          # what we accept from a phone
MAX_SIDE = 2000                    # what we keep (pixels on the long side)
THUMB_SIDE = 480
MAX_PER_BUSINESS = 2 * 1024 ** 3   # 2 GB of files per business
META = [c for c in F.c if c.name not in ("data", "thumb")]


def _image(raw: bytes) -> tuple[bytes, bytes, str]:
    """-> (picture, thumbnail, mime). Turned upright, shrunk, saved as JPEG (PNG when it has transparency)."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        try:
            import pillow_heif  # type: ignore  # noqa: F401  — iPhone photos, when installed
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
    except (UnidentifiedImageError, OSError, ValueError):
        raise Invalid("that file is not a picture we can open (use JPG or PNG)", "file") from None
    alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if alpha else "RGB")

    def out(img, side, q):
        img = img.copy()
        img.thumbnail((side, side))
        b = io.BytesIO()
        img.save(b, "PNG" if alpha else "JPEG", **({} if alpha else {"quality": q, "optimize": True}))
        return b.getvalue()

    return out(im, MAX_SIDE, 85), out(im, THUMB_SIDE, 78), "image/png" if alpha else "image/jpeg"


def save(conn: Connection, biz: dict, actor: str, *, raw: bytes, mime: str, name: str = "", kind: str = "photo",
         project_id: UUID | None = None, room_id: UUID | None = None, caption: str = "", shared: bool = False,
         source_id: UUID | None = None) -> dict:
    if kind not in KINDS:
        raise Invalid("unknown kind of file", "kind")
    if not raw:
        raise Invalid("the file is empty", "file")
    if len(raw) > MAX_IN:
        raise Invalid("files up to 25 MB", "file")
    mime = (mime or "").split(";")[0].strip().lower()
    used = conn.execute(select(func.coalesce(func.sum(F.c.size), 0)).where(F.c.business_id == biz["id"])).scalar()
    if used + len(raw) > MAX_PER_BUSINESS:
        raise Invalid("this business has used its 2 GB for files; delete some old ones first", "file")
    if project_id:
        _project(conn, biz["id"], project_id)
    if room_id:
        r = conn.execute(select(db.project_rooms.c.project_id).where(db.project_rooms.c.business_id == biz["id"],
                                                                     db.project_rooms.c.id == room_id)).scalar()
        if not r or (project_id and r != project_id):
            raise NotFound("room")
    if mime in IMAGE_TYPES or (not mime and raw[:3] == b"\xff\xd8\xff"):
        data, thumb, mime = _image(raw)
    elif mime in DOC_TYPES and raw[:5] == b"%PDF-":
        data, thumb = raw, None
    else:
        raise Invalid("pictures (JPG, PNG, HEIC) or PDF", "file")
    row = conn.execute(insert(F).values(business_id=biz["id"], project_id=project_id, room_id=room_id, kind=kind,
                                        name=(name or "")[:200], mime=mime, size=len(data), data=data, thumb=thumb,
                                        caption=(caption or "")[:500], shared=bool(shared), source_id=source_id,
                                        created_by=actor).returning(*META)).mappings().one()
    log(conn, biz["id"], "file.added", actor, row["id"], {"kind": kind, "size": len(data), "project_id": str(project_id) if project_id else None})
    return dict(row)


def _project(conn: Connection, business_id: UUID, project_id: UUID) -> None:
    if not conn.execute(select(db.projects.c.id).where(db.projects.c.business_id == business_id,
                                                       db.projects.c.id == project_id)).scalar():
        raise NotFound("project")


def get(conn: Connection, business_id: UUID, file_id: UUID) -> dict:
    row = conn.execute(select(*META).where(F.c.business_id == business_id, F.c.id == file_id)).mappings().first()
    if not row:
        raise NotFound("file")
    return dict(row)


def content(conn: Connection, business_id: UUID, file_id: UUID, thumb: bool = False) -> tuple[bytes, str]:
    row = conn.execute(select(F.c.data, F.c.thumb, F.c.mime).where(F.c.business_id == business_id, F.c.id == file_id)).first()
    if not row:
        raise NotFound("file")
    if thumb and row.thumb:
        return row.thumb, "image/png" if row.mime == "image/png" else "image/jpeg"
    return row.data, row.mime


def list_(conn: Connection, business_id: UUID, *, project_id: UUID | None = None, kind: str | None = None,
          shared_only: bool = False, limit: int = 300) -> list[dict]:
    q = select(*META).where(F.c.business_id == business_id)
    if project_id:
        q = q.where(F.c.project_id == project_id)
    if kind:
        q = q.where(F.c.kind == kind)
    if shared_only:
        q = q.where(F.c.shared.is_(True))
    return [dict(r) for r in conn.execute(q.order_by(F.c.created_at.desc()).limit(limit)).mappings()]


def patch(conn: Connection, business_id: UUID, actor: str, file_id: UUID, *, caption: str | None = None,
          shared: bool | None = None, public: bool | None = None, room_id: UUID | None | str = "keep") -> dict:
    f = get(conn, business_id, file_id)
    vals = {}
    if caption is not None:
        vals["caption"] = caption.strip()[:500]
    if shared is not None:
        vals["shared"] = bool(shared)
    if public is not None:
        vals["public"] = bool(public)
    if room_id != "keep":
        if room_id and not conn.execute(select(db.project_rooms.c.id).where(db.project_rooms.c.business_id == business_id,
                                                                            db.project_rooms.c.id == room_id,
                                                                            db.project_rooms.c.project_id == f["project_id"])).scalar():
            raise NotFound("room")
        vals["room_id"] = room_id or None
    if vals:
        conn.execute(update(F).where(F.c.id == file_id).values(**vals))
        log(conn, business_id, "file.updated", actor, file_id, {k: str(v) for k, v in vals.items() if k != "caption"})
    return get(conn, business_id, file_id)


def remove(conn: Connection, business_id: UUID, actor: str, file_id: UUID) -> None:
    get(conn, business_id, file_id)
    conn.execute(delete(F).where(F.c.business_id == business_id, F.c.id == file_id))
    log(conn, business_id, "file.deleted", actor, file_id)


def palette(raw: bytes, n: int = 6) -> list[str]:
    """The main colours of a picture, darkest first, as #rrggbb."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((200, 200))
    q = im.quantize(colors=max(2, min(12, n)), method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)
    out = []
    for _count, idx in counts[:n]:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        out.append((0.299 * r + 0.587 * g + 0.114 * b, f"#{r:02x}{g:02x}{b:02x}"))
    return [h for _, h in sorted(out)]
