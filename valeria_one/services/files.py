"""Files: photos, renders, plans and documents a business keeps with its projects.

They live in the database (not on a disk next to it), so the nightly backup holds them and a move to another
server takes them along. Photos are shrunk to a sensible size on the way in, with a small thumbnail beside
them; the original phone photo (often 5-10 MB) is not kept.

des01 (v0.26.1): design work files — AutoCAD (.dwg, .dxf), SketchUp (.skp), Photoshop (.psd) — go into a project too.
They are checked by their name AND their first bytes (a .dwg must really be a drawing), kept byte for byte (never
shrunk or converted) with their SHA-256, which the database checks again before every download; they are sent in
pieces (never the whole file in memory) and always as a download, never opened in the browser. Up to 100 MB each,
inside the business's same 2 GB. The customer sees the preview the designer picked; the work file itself only when
the designer ticks "client can download".
"""
from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Iterator
from uuid import UUID

from sqlalchemy import LargeBinary, delete, func, insert, select, type_coerce, update
from sqlalchemy.engine import Connection

from .. import db
from . import Invalid, NotFound, log

F = db.files
KINDS = ("photo", "render", "plan", "doc", "canva", "site", "post", "work")
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "image/gif"}
DOC_TYPES = {"application/pdf"}
VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}  # page01: a short video for the business's own page (kind «site» only)


def _video_ok(raw: bytes) -> str | None:
    """The video's real type from its first bytes (MP4/MOV «ftyp» box, WebM EBML header), else None."""
    if raw[4:8] == b"ftyp":
        return "video/quicktime" if raw[8:10] == b"qt" else "video/mp4"
    if raw[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    return None
MAX_IN = 25 * 1024 * 1024          # what we accept from a phone
MAX_SIDE = 2000                    # what we keep (pixels on the long side)
THUMB_SIDE = 480
MAX_PER_BUSINESS = 2 * 1024 ** 3   # 2 GB of files per business
META = [c for c in F.c if c.name not in ("data", "thumb")]
# des01: design work files, by extension: (the type we store and send, its label on screen)
WORK_TYPES = {"dwg": ("application/acad", "AutoCAD DWG"), "dxf": ("application/dxf", "AutoCAD DXF"),
              "skp": ("application/vnd.sketchup.skp", "SketchUp"), "psd": ("application/x-photoshop", "Photoshop")}
WORK_ACCEPT = ",".join("." + e for e in WORK_TYPES)
MAX_WORK = 100 * 1024 * 1024       # a work file: kept whole in the database, sent in pieces
CHUNK = 4 * 1024 * 1024            # one piece of a download
_SKP = b"\xff\xfe\xff\x0e" + "SketchUp".encode("utf-16-le")     # SketchUp: "SketchUp Model" in UTF-16 after a marker
_SKP_OLD = b"\x0eSketchUp Model"                                  # very old SketchUp files: the same words in ASCII


def ext(name: str) -> str:
    n = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return n.rsplit(".", 1)[-1].lower() if "." in n else ""


def is_work_name(name: str) -> bool:
    return ext(name) in WORK_TYPES


def _work_ok(e: str, raw: bytes) -> bool:
    """Is it really what its name says? Checked on the first bytes of the file itself."""
    head = raw[:4096]
    if e == "dwg":                                          # "AC1015", "AC1032"… (or AC1.50 / AC2.10 from the 1980s)
        return re.match(rb"AC(1\d{3}|[12]\.\d{2})", head) is not None
    if e == "psd":                                          # "8BPS", version 1 (or 2 = large document)
        return head[:4] == b"8BPS" and head[4:6] in (b"\x00\x01", b"\x00\x02")
    if e == "skp":                                          # newer SketchUp versions may keep the model in a ZIP container
        return head.startswith(_SKP) or head.startswith(_SKP_OLD) or head[:4] == b"PK\x03\x04"
    if e == "dxf":
        if head.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00"):
            return True
        if b"\x00" in head:                                  # a text DXF has no zero bytes
            return False
        txt = head.decode("latin-1").lstrip("\ufeff\xef\xbb\xbf")
        return re.match(r"\s*(999\s*\r?\n[^\n]*\n\s*)*0\s*\r?\n\s*SECTION\s*\r?\n", txt) is not None
    return False


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
         source_id: UUID | None = None, work: bool = False) -> dict:
    """work=True: this upload may be a design work file (the project's own upload forms); every other caller keeps
    taking only pictures and PDF, as before."""
    if kind not in KINDS:
        raise Invalid("unknown kind of file", "kind")
    if not raw:
        raise Invalid("the file is empty", "file")
    wext = ext(name) if work and is_work_name(name) else ""
    if wext:
        if len(raw) > MAX_WORK:
            raise Invalid("work files up to 100 MB", "file")
    elif len(raw) > MAX_IN:
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
    if wext:
        if not _work_ok(wext, raw):
            raise Invalid(f"this is not a real .{wext} file (its contents don't match its name)", "file")
        data, thumb, mime, kind = raw, None, WORK_TYPES[wext][0], "work"
    elif mime in IMAGE_TYPES or (not mime and raw[:3] == b"\xff\xd8\xff"):
        data, thumb, mime = _image(raw)
    elif mime in DOC_TYPES and raw[:5] == b"%PDF-":
        data, thumb = raw, None
    elif kind == "site" and (mime in VIDEO_TYPES or mime.startswith("video/")):
        real = _video_ok(raw)                                # page01: what it really is, never what it claims
        if not real:
            raise Invalid("videos: MP4, MOV or WebM", "file")
        data, thumb, mime = raw, None, real
    else:
        raise Invalid(("pictures (JPG, PNG, HEIC), PDF, or work files (AutoCAD, SketchUp, Photoshop)" if work
                       else "pictures (JPG, PNG, HEIC) or PDF"), "file")
    if kind == "work" and not wext:
        raise Invalid("work files are AutoCAD (.dwg, .dxf), SketchUp (.skp) or Photoshop (.psd)", "file")
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute(insert(F).values(business_id=biz["id"], project_id=project_id, room_id=room_id, kind=kind,
                                        name=(name or "")[:200], mime=mime, size=len(data), data=data, thumb=thumb,
                                        caption=(caption or "")[:500], shared=bool(shared), source_id=source_id,
                                        sha256=sha, created_by=actor).returning(*META)).mappings().one()
    if wext and _db_sha(conn, biz["id"], row["id"]) != sha:  # what the database holds is exactly what came in
        raise Invalid("the file did not save whole; try again", "file")
    log(conn, biz["id"], "file.added", actor, row["id"], {"kind": kind, "size": len(data), "sha256": sha,
                                                          "project_id": str(project_id) if project_id else None})
    return dict(row)


def _db_sha(conn: Connection, business_id: UUID, file_id: UUID) -> str | None:
    """The SHA-256 of the bytes as the database holds them now (worked out inside the database: nothing loaded here)."""
    return conn.execute(select(func.encode(func.sha256(F.c.data), "hex")).where(F.c.business_id == business_id,
                                                                                 F.c.id == file_id)).scalar()


def verified(conn: Connection, business_id: UUID, file_id: UUID) -> dict:
    """A file's details once the database has checked its bytes still match the SHA-256 kept when it came in."""
    f = get(conn, business_id, file_id)
    if f["sha256"] and _db_sha(conn, business_id, file_id) != f["sha256"]:
        with db.tx() as c2:                                   # kept even though this request stops here
            log(c2, business_id, "file.damaged", "system", file_id, {"sha256": f["sha256"]})
        raise Invalid("this file looks damaged, so it was not sent; restore it from a backup", "file")
    return f


def pieces(business_id: UUID, file_id: UUID, size: int) -> Iterator[bytes]:
    """A big file in pieces of CHUNK bytes, each read on its own (a slow download never holds the database or the memory)."""
    for off in range(0, size, CHUNK):
        with db.tx() as conn:
            part = conn.execute(select(type_coerce(func.substring(F.c.data, off + 1, CHUNK), LargeBinary)).where(
                F.c.business_id == business_id, F.c.id == file_id)).scalar()
        if not part:                                         # deleted while downloading: stop (the size tells it is short)
            return
        yield bytes(part)


def with_previews(conn: Connection, business_id: UUID, rows: list[dict]) -> list[dict]:
    """Each work file gets its preview's type (preview_mime: '' without one), so a page shows a picture or a PDF link."""
    ids = [r["preview_id"] for r in rows if r.get("kind") == "work" and r.get("preview_id")]
    mimes = dict(conn.execute(select(F.c.id, F.c.mime).where(F.c.business_id == business_id, F.c.id.in_(ids))).all()) if ids else {}
    for r in rows:
        if r.get("kind") == "work":
            r["preview_mime"] = mimes.get(r["preview_id"], "")
    return rows


def work_label(mime: str) -> str:
    """'AutoCAD DWG', 'SketchUp'… for a work file's type ('' for anything else)."""
    return next((v[1] for v in WORK_TYPES.values() if v[0] == mime), "")


def download_name(f: dict) -> str:
    """The file's own name for a download, safe in a header, with its right extension."""
    n = (f.get("name") or "").replace("\\", "/").rsplit("/", 1)[-1]
    n = re.sub(r'[\x00-\x1f\x7f"\\;]', "", n).strip(" .") or "file"
    e = next((k for k, v in WORK_TYPES.items() if v[0] == f.get("mime")), "")
    if e and ext(n) != e:
        n = f"{n}.{e}"
    return n[:200]


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
          shared: bool | None = None, public: bool | None = None, room_id: UUID | None | str = "keep",
          client_download: bool | None = None, preview_id: UUID | None | str = "keep") -> dict:
    f = get(conn, business_id, file_id)
    vals = {}
    if f["kind"] == "work" and public:
        raise Invalid("a work file is never on the website; use its preview picture", "public")
    if client_download is not None:
        if f["kind"] != "work":
            raise Invalid("only work files have this", "client_download")
        vals["client_download"] = bool(client_download)
    if preview_id != "keep":
        if f["kind"] != "work":
            raise Invalid("only work files have a preview", "preview_id")
        if preview_id:
            pv = conn.execute(select(F.c.kind, F.c.mime, F.c.project_id).where(F.c.business_id == business_id,
                                                                              F.c.id == preview_id)).first()
            if not pv or pv.project_id != f["project_id"] or preview_id == file_id:
                raise NotFound("file")
            if pv.kind == "work" or not (pv.mime.startswith("image/") or pv.mime == "application/pdf"):
                raise Invalid("a preview is a picture or a PDF", "preview_id")
        vals["preview_id"] = preview_id or None
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
    for t in (db.contract_templates, db.contracts):          # ctr01: a contract's own document stays (what was signed)
        if conn.execute(select(t.c.id).where(t.c.business_id == business_id, t.c.file_id == file_id).limit(1)).first():
            raise Invalid("this document belongs to a contract, so it is kept", "file")
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
