"""ctr01 (v0.24) on screen: Contracts (/app/contracts) — the business's contract templates (from its kit, drafted by
Valeria from the owner's own words, typed or uploaded), one copy per customer filled in and sent by the channel the
customer uses, its status (draft, sent, viewed, signed, declined, voided), a printable copy for a notary; and the
customer's own signing page (/sign/<id>/<sig>: read, consent, type the name, draw the signature, sign — or decline).
The team sends and follows contracts; templates, the "booking waits for it" setting, voiding and deposits are the owner's."""
from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from .. import db
from ..services import Invalid, NotFound
from ..services import contracts as CT
from . import _ctx, _flash, _form, _go, _guard, _lang, _limit, _multi_form, _own, render
from .staff import _amt

router = APIRouter(include_in_schema=False)
_sign_hits: dict = {}
SIGN_PER_HOUR = 30                                           # tries from one connection on the public signing pages


def _uuid(v, what: str = "contract") -> UUID:
    try:
        return UUID(str(v))
    except (ValueError, TypeError):
        raise NotFound(what) from None


def _opt(v) -> str | None:
    v = str(v or "").strip()
    return v or None


# ---------------------------------------------------------------- the list, the templates, the setting
@router.get("/app/contracts")
@_guard
async def contracts_page(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    status = request.query_params.get("status") or ""
    with db.tx() as conn:
        rows = CT.list_(conn, biz["id"], status=status if status in CT.STATUSES else None)
        tpls = CT.templates(conn, biz["id"])
        old = CT.templates(conn, biz["id"], archived=True) if ctx["role"] == "owner" else []
        counts = CT.counts(conn, biz["id"])
        st = CT.settings(conn, biz["id"])
    return render(request, "contracts.html", ctx, page="contracts", rows=rows, tpls=tpls, old_tpls=old, counts=counts, st=st,
                  status=status, statuses=CT.STATUSES, kit_blocks=CT.kit_blocks(biz["industry"]))


@router.post("/app/contracts/templates")
@_guard
async def template_new(request: Request):
    """act=kit (the base + the kit's blocks), words (Valeria drafts from the owner's description), text (typed / pasted),
    upload (their own PDF or text file)."""
    ctx = _own(request)
    biz = ctx["biz"]
    from .studio import _upload
    f, ups = await _upload(request)
    act = f.get("act")
    name = str(f.get("name") or "")
    if act == "words":
        tpl, by_ai = CT.from_words(biz, ctx["actor"], str(f.get("words") or ""))
        _flash(request, "ct_drafted_ai" if by_ai else "ct_drafted_plain", "ok" if by_ai else "warn")
        return _go(f"/app/contracts/templates/{tpl['id']}")
    with db.tx() as conn:
        if act == "upload":
            if not ups:
                raise Invalid(CT._t("ct_pdf_or_text", biz["language"]), "file")
            raw, mime, fname = ups[0]
            tpl = CT.from_upload(conn, biz, ctx["actor"], name, raw, mime, fname)
        elif act == "text":
            tpl = CT.from_text(conn, biz, ctx["actor"], name, str(f.get("text") or ""))
        else:
            tpl = CT.from_kit(conn, biz, ctx["actor"], name)
    _flash(request, "saved")
    return _go(f"/app/contracts/templates/{tpl['id']}")


@router.post("/app/contracts/settings")
@_guard
async def contracts_settings(request: Request):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        CT.save_settings(conn, ctx["biz"], ctx["actor"], require_signed=f.get("require_signed") == "1",
                         require_deposit=f.get("require_deposit") == "1")
    _flash(request, "saved")
    return _go("/app/contracts#settings")


@router.get("/app/contracts/templates/{tid}")
@_guard
async def template_page(request: Request, tid: str):
    ctx = _own(request)
    with db.tx() as conn:
        tpl = CT.template(conn, ctx["biz"]["id"], tid)
    return render(request, "contract_template.html", ctx, page="contracts", tpl=tpl, blocks=CT.BLOCKS, on=set(CT.blocks_of(tpl["sections"])),
                  kit_blocks=set(CT.kit_blocks(ctx["biz"]["industry"])), fields=CT.FIELDS)


@router.post("/app/contracts/templates/{tid}")
@_guard
async def template_save(request: Request, tid: str):
    ctx = _own(request)
    biz = ctx["biz"]
    f = await _multi_form(request)
    act = f.get("act")
    with db.tx() as conn:
        if act == "blocks":
            CT.set_blocks(conn, biz, ctx["actor"], tid, [str(b) for b in f.getlist("block")])
        elif act in ("archive", "restore"):
            CT.archive_template(conn, biz, ctx["actor"], tid, act == "archive")
            _flash(request, "saved")
            return _go("/app/contracts#templates")
        else:
            keys, titles, texts = f.getlist("s_key"), f.getlist("s_title"), f.getlist("s_text")
            secs = [{"key": str(k), "title": str(ti), "text": str(tx)} for k, ti, tx in zip(keys, titles, texts)]
            if act == "add" or str(f.get("new_title") or "").strip() or str(f.get("new_text") or "").strip():
                n = 1 + sum(1 for s in secs if s["key"].startswith("own:"))
                new = {"key": f"own:{n}", "title": str(f.get("new_title") or ""), "text": str(f.get("new_text") or "")}
                at = next((i for i, s in enumerate(secs) if s["key"] == "signatures"), len(secs))
                secs.insert(at, new)
            CT.save_template(conn, biz, ctx["actor"], tid, name=str(f.get("name") or ""), sections=secs)
    _flash(request, "saved")
    return _go(f"/app/contracts/templates/{tid}")


# ---------------------------------------------------------------- one contract
@router.get("/app/contracts/new")
@_guard
async def contract_new_page(request: Request):
    """Pick the template (and the customer) — started from a customer, a booking, a quote or an invoice."""
    ctx = _ctx(request)
    biz = ctx["biz"]
    q = request.query_params
    from ..services import contacts as C
    with db.tx() as conn:
        tpls = CT.templates(conn, biz["id"])
        pre = CT.prefill(conn, biz, contact_id=_opt(q.get("contact")), appointment_id=_opt(q.get("appointment")),
                         invoice_id=_opt(q.get("invoice")), quote_id=_opt(q.get("quote")))
        people = C.search(conn, biz["id"], limit=500)
    return render(request, "contract_new.html", ctx, page="contracts", tpls=tpls, pre=pre, people=people)


@router.post("/app/contracts")
@_guard
async def contract_create(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    f = await _form(request)
    with db.tx() as conn:
        tid = _opt(f.get("template_id"))
        if not tid:                                          # no template yet: the kit's own, made now
            if ctx["role"] != "owner" and not CT.templates(conn, biz["id"]):
                raise Invalid(CT._t("ct_no_templates_staff", _lang(request, biz)), "template")
            tid = (CT.templates(conn, biz["id"]) or [CT.from_kit(conn, biz, ctx["actor"])])[0]["id"]
        c = CT.create(conn, biz, ctx["actor"], template_id=tid, contact_id=_opt(f.get("contact_id")),
                      appointment_id=_opt(f.get("appointment_id")), invoice_id=_opt(f.get("invoice_id")),
                      quote_id=_opt(f.get("quote_id")))
    return _go(f"/app/contracts/{c['id']}")


def _share(c: dict, biz: dict, lang: str) -> dict:
    """Ways for the owner to pass the link on by hand (when no message could go)."""
    url = CT.link(c)
    if not url:
        return {}
    text = CT._t("ct_msg", c["lang"]).format(name="", business=biz["name"], title=c["title"], link=url)
    text = " ".join(text.split()).replace(" ,", ",")
    return {"url": url, "wa": "https://wa.me/?text=" + quote(text), "mail": f"mailto:?subject={quote(c['title'])}&body={quote(text)}"}


@router.get("/app/contracts/{cid}")
@_guard
async def contract_page(request: Request, cid: str):
    ctx = _ctx(request)
    biz = ctx["biz"]
    from ..services import inbox as X
    from ..services import payments as PM
    with db.tx() as conn:
        c = CT.get(conn, biz["id"], cid)
        contact = CT._contact(conn, biz["id"], c["contact_id"])
        vers = CT.versions(conn, biz["id"], c["number"])
        tpl = None
        if c["template_id"]:
            try:
                tpl = CT.template(conn, biz["id"], c["template_id"])
            except NotFound:
                tpl = None
        reach = []
        if contact:
            ids = {r[0] for r in conn.execute(db.identities.select().with_only_columns(db.identities.c.channel).where(
                db.identities.c.business_id == biz["id"], db.identities.c.contact_id == contact["id"]))}
            reach = [a for a in X.accounts(conn, biz["id"]) if a["channel"] in ids and a["active"]]
        pay = conn.execute(db.payments.select().where(db.payments.c.id == c["payment_id"])).mappings().first() if c["payment_id"] else None
        dep_in = CT.deposit_paid(conn, c)
        has_pay = bool(PM.accounts(conn, biz["id"]))
    lang = _lang(request, biz)
    return render(request, "contract.html", ctx, page="contracts", c=c, contact=contact, vers=vers, tpl=tpl, reach=reach,
                  share=_share(c, biz, lang), paras=CT.paragraphs(c["body"]), pay=pay, dep_in=dep_in, has_pay=has_pay,
                  fv=c["fields"] or {}, sample=bool(tpl and tpl["source"] in ("kit", "ai")))


@router.post("/app/contracts/{cid}")
@_guard
async def contract_act(request: Request, cid: str):
    ctx = _ctx(request)
    biz = ctx["biz"]
    f = await _form(request)
    act = f.get("act")
    if act in ("void", "deposit", "delete") and ctx["role"] != "owner":
        _flash(request, "owners_only", "warn")
        return _go(f"/app/contracts/{cid}")
    if act == "send":
        acc = _opt(f.get("account_id"))
        res = CT.send(biz, ctx["actor"], cid, account_id=_uuid(acc, "channel") if acc and acc != "none" else None,
                      message=acc != "none")
        if res["problem"] and acc != "none":
            from .i18n import has, t
            lang = _lang(request, biz)
            why = t(f"r_{res['problem']}", lang) if has(f"r_{res['problem']}") else ""
            _flash(request, "ct_sent_share", "warn", raw=" ".join(x for x in (t("ct_sent_share", lang), why) if x))
        else:
            _flash(request, "ct_sent")
        return _go(f"/app/contracts/{res['id']}")
    with db.tx() as conn:
        if act in ("save", "refill"):
            fields = {k: f.get(k) for k in ("client", "client_contact", "service", "date", "due") if k in f}
            for k in ("price", "deposit"):
                if k in f:
                    fields[f"{k}_minor"] = _amt(biz, f.get(k), k)
            CT.save(conn, biz, ctx["actor"], cid, title=f.get("title"), body=f.get("body") if act == "save" else None,
                    fields=fields, refill=act == "refill")
            _flash(request, "saved")
        elif act == "void":
            CT.void(conn, biz, ctx["actor"], cid, str(f.get("reason") or ""))
            _flash(request, "ct_voided_done")
        elif act == "new_version":
            new = CT.new_version(conn, biz, ctx["actor"], cid)
            _flash(request, "ct_new_version_done")
            return _go(f"/app/contracts/{new['id']}")
        elif act == "deposit":
            CT.mark_deposit(conn, biz, ctx["actor"], cid)
            _flash(request, "saved")
        elif act == "delete":
            CT.delete_draft(conn, biz, ctx["actor"], cid)
            _flash(request, "ct_deleted")
            return _go("/app/contracts")
        else:
            raise Invalid("?", "act")
    return _go(f"/app/contracts/{cid}")


def _print_ctx(c: dict, biz: dict) -> dict:
    return {"paras": CT.paragraphs(c["body"]), "fv": c["fields"] or {}}


@router.get("/app/contracts/{cid}/print")
@_guard
async def contract_print(request: Request, cid: str):
    """A printable copy (the browser's "Save as PDF"): the words, the signature and its record, a blank notary part."""
    ctx = _ctx(request)
    with db.tx() as conn:
        c = CT.get(conn, ctx["biz"]["id"], cid)
    return render(request, "contract_print.html", {**ctx, "role": None}, c=c, notary=request.query_params.get("notary") == "1",
                  file_url=f"/app/files/{c['file_id']}" if c["file_id"] else None, **_print_ctx(c, ctx["biz"]))


# ---------------------------------------------------------------- the customer's page /sign/<id>/<sig>
def _pub_ctx(biz: dict) -> dict:
    return {"biz": biz, "user": None, "actor": "contract_page", "role": None, "businesses": []}


def _nf() -> HTMLResponse:
    return HTMLResponse("Not found", status_code=404)


@router.get("/sign/{cid}/{sig}")
@_guard
async def sign_page(request: Request, cid: str, sig: str):
    with db.tx() as conn:
        got = CT.public(conn, cid, sig)
        if not got:
            return _nf()
        c, biz = got
        CT.viewed(conn, c)
        newer = conn.execute(db.contracts.select().with_only_columns(db.contracts.c.version).where(
            db.contracts.c.business_id == biz["id"], db.contracts.c.number == c["number"],
            db.contracts.c.version > c["version"])).first() is not None
        st = CT.settings(conn, biz["id"])
        pay = conn.execute(db.payments.select().where(db.payments.c.id == c["payment_id"])).mappings().first() if c["payment_id"] else None
    base = f"/sign/{c['id']}/{sig}"
    return render(request, "contract_sign.html", _pub_ctx(biz), c=c, base=base, newer=newer, st=st, pay=pay,
                  paras=CT.paragraphs(c["body"]), fv=c["fields"] or {})


@router.post("/sign/{cid}/{sig}")
@_guard
async def sign_post(request: Request, cid: str, sig: str):
    with db.tx() as conn:
        if not CT.public(conn, cid, sig):
            return _nf()
    if not _limit(_sign_hits, request, SIGN_PER_HOUR):
        return HTMLResponse("Too many tries from this connection; try again in an hour.", status_code=429)
    f = await _form(request)
    ip = request.client.host if request.client else ""
    if f.get("act") == "decline":
        CT.decline(cid, sig, str(f.get("reason") or ""))
    else:
        CT.sign(cid, sig, name=str(f.get("name") or ""), privacy=f.get("privacy") == "1", agree=f.get("agree") == "1",
                signature=str(f.get("signature") or ""), ip=ip, ua=request.headers.get("user-agent", ""))
    return _go(f"/sign/{cid}/{sig}")


@router.get("/sign/{cid}/{sig}/print")
@_guard
async def sign_print(request: Request, cid: str, sig: str):
    with db.tx() as conn:
        got = CT.public(conn, cid, sig)
    if not got:
        return _nf()
    c, biz = got
    return render(request, "contract_print.html", _pub_ctx(biz), c=c, notary=request.query_params.get("notary") == "1",
                  file_url=f"/sign/{c['id']}/{sig}/file" if c["file_id"] else None, **_print_ctx(c, biz))


@router.get("/sign/{cid}/{sig}/file")
async def sign_file(request: Request, cid: str, sig: str):
    """The owner's own PDF, for the customer who has the link (only that contract's file)."""
    with db.tx() as conn:
        got = CT.public(conn, cid, sig)
        out = CT.file_bytes(conn, got[0]) if got else None
    if not out:
        return _nf()
    data, mime = out
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff",
                                                    "Content-Disposition": 'inline; filename="contract.pdf"'})
