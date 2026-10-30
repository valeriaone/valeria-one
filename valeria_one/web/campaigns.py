"""v0.18 on screen: campaigns (one question per screen: where → who → the message → check it → when), drip sequences,
and the customer's own links (unsubscribe, a counted click, the optional open pixel)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db, packs
from ..services import Invalid, NotFound
from ..services import campaigns as CG
from ..services import sequences as SQ
from . import _ctx, _flash, _form, _go, _guard, _lang, _multi_form, _need, _own, render
from .i18n import t

router = APIRouter(include_in_schema=False)
STEPS = ("who", "message", "check", "when")
PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
         b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def _owner(request: Request) -> dict:
    ctx = _own(request)
    _need(request, ctx, "followups")
    return ctx


def _uuid(v) -> UUID:
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound("campaign") from None


def _aud_from(f) -> dict:
    """The audience from the form (tags, one field, a stage, a source, last visit)."""
    tags = [x.strip() for x in str(f.get("tags") or "").replace(";", ",").split(",") if x.strip()]
    aud: dict = {"tags": tags}
    if f.get("field_key"):
        aud["field"] = {"key": f.get("field_key"), "value": f.get("field_value") or ""}
    if f.get("stage"):
        aud["stage"] = f.get("stage")
    if f.get("source"):
        aud["source"] = f.get("source")
    if f.get("seen_op") in CG.SEEN_OPS and str(f.get("seen_days") or "").strip():
        aud["seen"] = {"op": f.get("seen_op"), "days": f.get("seen_days")}
    return aud


def _blocks_from(f) -> list[dict]:
    ts, texts, urls = f.getlist("blk_t"), f.getlist("blk_text"), f.getlist("blk_url")
    return [{"t": a, "text": b, "url": c} for a, b, c in zip(ts, texts, urls)]


# ---------------------------------------------------------------- the list
@router.get("/app/campaigns")
@_guard
async def campaigns_page(request: Request):
    ctx = _owner(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        cps = CG.list_(conn, biz["id"])
        for c in cps:
            c["stats"] = CG.stats(conn, biz["id"], campaign_id=c["id"]) if c["status"] in ("done", "sending") else None
        seqs = SQ.list_(conn, biz["id"])
        ready = CG.ready_channels(conn, biz)
    return render(request, "campaigns.html", ctx, cps=cps, seqs=seqs, ready=ready, page="campaigns")


@router.post("/app/campaigns/new")
@_guard
async def campaign_new(request: Request):
    ctx = _owner(request)
    f = await _form(request)
    with db.tx() as conn:
        cp = CG.start(conn, ctx["biz"], ctx["actor"], f.get("channel", ""))
    return _go(f"/app/campaigns/{cp['id']}?step=who")


# ---------------------------------------------------------------- the builder
@router.get("/app/campaigns/{cid}")
@_guard
async def campaign_page(request: Request, cid: str):
    ctx = _owner(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    from ..services import fields as CF
    with db.tx() as conn:
        cp = CG.get(conn, biz["id"], _uuid(cid))
        ready = CG.ready_channels(conn, biz)
        if cp["status"] != "draft":
            st = CG.stats(conn, biz["id"], campaign_id=cp["id"])
            why = CG.skipped_why(conn, biz["id"], cp["id"])
            return render(request, "campaign.html", ctx, cp=cp, step="report", st=st, why=why, page="campaigns")
        q = request.query_params
        step = q.get("step") if q.get("step") in STEPS else "who"
        aud = cp["audience"] or {}
        count = CG.audience(conn, biz, aud, cp["channel"])
        fields = CF.list_(conn, biz["id"], "contact")
        problems = CG.problems(conn, biz, cp)
    content = CG.content_of(cp)
    content["links"] = CG.links_of(content)
    html, text = CG.render_email(biz, content, {"name": t("cg_test_name", lang)}, None, False)
    sms = CG.body_for(biz, content, cp["channel"], {"name": t("cg_test_name", lang)}, None)
    tz = ZoneInfo(biz["timezone"])
    return render(request, "campaign.html", ctx, cp=cp, step=step, steps=STEPS, aud=aud, count=count, fields=fields,
                  stages=packs.industry(biz["industry"]).stages, problems=problems, ready=ready, email_html=html, email_text=text,
                  sms=sms, seg=CG.sms_segments(sms), subject=CG._fill(cp.get("subject") or "", biz, {"name": t("cg_test_name", lang)}), now_local=datetime.now(tz).strftime("%Y-%m-%dT%H:%M"),
                  me=(ctx["user"] or {}).get("email") or "", page="campaigns")


@router.post("/app/campaigns/{cid}")
@_guard
async def campaign_save(request: Request, cid: str):
    ctx = _owner(request)
    biz = ctx["biz"]
    f = await _multi_form(request)
    act = f.get("act") or ""
    cid_u = _uuid(cid)
    back = f"/app/campaigns/{cid}"
    if act in ("who", "count"):
        with db.tx() as conn:
            CG.save(conn, biz, ctx["actor"], cid_u, {"audience": _aud_from(f), "name": f.get("name")})
        return _go(back + ("?step=message" if act == "who" else "?step=who"))
    if act == "message":
        data = {"message": f.get("message", ""), "subject": f.get("subject"), "template": f.get("template"),
                "track_opens": f.get("track_opens") == "1"}
        if f.getlist("blk_t"):
            data["blocks"] = _blocks_from(f)
        with db.tx() as conn:
            CG.save(conn, biz, ctx["actor"], cid_u, data)
        return _go(back + ("?step=message#ai" if f.get("stay") else "?step=check"))
    if act == "ai":
        with db.tx() as conn:
            cp = CG.get(conn, biz["id"], cid_u)
        got = CG.ai_write(biz, f.get("idea", ""), cp["channel"])
        data = {"message": got["text"], "by_ai": got["by_ai"]}
        if cp["channel"] == "email":
            data["blocks"] = [{"t": "text", "text": got["text"]}]
            if got.get("subject"):
                data["subject"] = got["subject"]
        with db.tx() as conn:
            CG.save(conn, biz, ctx["actor"], cid_u, data)
        _flash(request, "cg_ai_done" if got["by_ai"] else "cg_ai_plain")
        return _go(back + "?step=message")
    if act == "test":
        with db.tx() as conn:
            cp = CG.get(conn, biz["id"], cid_u)
        res = CG.send_test(biz, ctx["actor"], cp, f.get("to", ""))
        if res == "sent":
            _flash(request, "cg_test_sent")
        else:
            _flash(request, "cg_test_failed", "warn", raw=t("cg_test_failed", _lang(request, biz)) + " " + res.split(":", 1)[-1])
        return _go(back + "?step=check")
    if act == "schedule":
        when = None
        if f.get("when") == "later":
            try:
                when = datetime.fromisoformat(str(f.get("send_at") or "")).replace(tzinfo=ZoneInfo(biz["timezone"]))
            except ValueError:
                raise Invalid(t("cg_when_pick", _lang(request, biz)), "send_at") from None
        with db.tx() as conn:
            CG.schedule(conn, biz, ctx["actor"], cid_u, when)
        _flash(request, "cg_scheduled")
        return _go(back)
    with db.tx() as conn:
        if act == "unschedule":
            CG.unschedule(conn, biz, ctx["actor"], cid_u)
            return _go(back + "?step=when")
        if act == "cancel":
            from ..services import followups as FU
            FU.cancel_campaign(conn, biz["id"], ctx["actor"], cid_u)
            return _go(back)
        if act == "duplicate":
            cp = CG.duplicate(conn, biz, ctx["actor"], cid_u)
            return _go(f"/app/campaigns/{cp['id']}?step=who")
        if act == "delete":
            CG.delete_draft(conn, biz, ctx["actor"], cid_u)
            return _go("/app/campaigns")
    raise Invalid("?", "act")


@router.get("/app/campaigns/{cid}/email")
@_guard
async def campaign_email(request: Request, cid: str):
    """The email as a customer gets it, inside the builder's preview frame (sandboxed: no scripts run)."""
    ctx = _owner(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        cp = CG.get(conn, biz["id"], _uuid(cid))
    content = CG.content_of(cp)
    content["links"] = CG.links_of(content)
    html, _ = CG.render_email(biz, content, {"name": t("cg_test_name", _lang(request, biz))}, None, False)
    return HTMLResponse(html, headers={"Content-Security-Policy": "script-src 'none'", "X-Frame-Options": "SAMEORIGIN"})


# ---------------------------------------------------------------- drip sequences
def _steps_from(f) -> list[dict]:
    out = []
    for amount, unit, ch, subject, text, template in zip(f.getlist("st_amount"), f.getlist("st_unit"), f.getlist("st_channel"),
                                                         f.getlist("st_subject"), f.getlist("st_text"), f.getlist("st_template")):
        out.append({"amount": amount or 0, "unit": unit, "channel": ch, "subject": subject, "text": text, "template": template})
    return out


@router.get("/app/sequences/{sid}")
@_guard
async def sequence_page(request: Request, sid: str):
    ctx = _owner(request)
    biz = ctx["biz"]
    from ..services import forms as FM
    with db.tx() as conn:
        seq = None if sid == "new" else SQ.get(conn, biz["id"], _uuid(sid))
        people = SQ.enrollments(conn, biz["id"], seq["id"]) if seq else []
        st = CG.stats(conn, biz["id"], sequence_id=seq["id"]) if seq else None
        forms = FM.list_(conn, biz["id"])
        ready = CG.ready_channels(conn, biz)
    steps = []
    for s in (seq or {}).get("steps") or [{"wait": 0, "channel": "auto"}, {"wait": 2880, "channel": "auto"}]:
        w = int(s.get("wait") or 0)
        unit, amount = ("days", w // 1440) if w and w % 1440 == 0 else (("hours", w // 60) if w and w % 60 == 0 else ("minutes", w))
        steps.append({**s, "unit": unit, "amount": amount})
    return render(request, "sequence.html", ctx, seq=seq, steps=steps, people=people, st=st, forms=forms, ready=ready,
                  triggers=SQ.TRIGGERS, exits=SQ.EXITS, page="campaigns")


@router.post("/app/sequences/{sid}")
@_guard
async def sequence_save(request: Request, sid: str):
    ctx = _owner(request)
    biz = ctx["biz"]
    f = await _multi_form(request)
    act = f.get("act") or "save"
    with db.tx() as conn:
        if act in ("on", "off"):
            SQ.set_active(conn, biz, ctx["actor"], _uuid(sid), act == "on")
            return _go(f"/app/sequences/{sid}")
        if act == "delete":
            SQ.delete(conn, biz, ctx["actor"], _uuid(sid))
            return _go("/app/campaigns#sequences")
        if act == "stop":
            SQ.stop(conn, biz["id"], _uuid(f.get("enrollment_id")), "owner", ctx["actor"])
            return _go(f"/app/sequences/{sid}#people")
        data = {"name": f.get("name"), "kind": f.get("kind"), "trigger": f.get("trigger"),
                "trigger_arg": {"tag": f.get("tag"), "form_id": f.get("form_id") or None},
                "steps": _steps_from(f), "exit_on": f.getlist("exit_on"), "active": f.get("active") == "1"}
        seq = SQ.save(conn, biz, ctx["actor"], data, None if sid == "new" else _uuid(sid))
    _flash(request, "saved")
    return _go(f"/app/sequences/{seq['id']}")


@router.post("/app/contacts/{contact_id}/sequence")
@_guard
async def contact_sequence(request: Request, contact_id: UUID):
    """Add one customer to a drip by hand (from their page)."""
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        seq = SQ.get(conn, ctx["biz"]["id"], _uuid(f.get("sequence_id")))
        from ..services import contacts as C
        C.get(conn, ctx["biz"]["id"], contact_id)
        got = SQ.enroll(conn, ctx["biz"], seq, contact_id, ctx["actor"])
    _flash(request, "sq_added" if got else "sq_not_added", "ok" if got else "warn")
    return _go(f"/app/contacts/{contact_id}#sequences")


# ---------------------------------------------------------------- the customer's own links
@router.get("/u/{token}")
@_guard
async def unsubscribe_page(request: Request, token: str):
    with db.tx() as conn:
        row = CG.by_token(conn, token)
        biz = dict(conn.execute(db.businesses.select().where(db.businesses.c.id == row["business_id"])).mappings().one())
    ctx = {"biz": biz, "user": None, "actor": "unsubscribe", "role": None, "businesses": []}
    return render(request, "unsub.html", ctx, token=token, done=bool(row["unsubscribed_at"]))


@router.post("/u/{token}")
async def unsubscribe_do(request: Request, token: str):
    """One click: the List-Unsubscribe-Post from a mail app (RFC 8058) or the button on the page. The token is the key."""
    try:
        biz = CG.unsubscribe(token, "one-click" if b"One-Click" in (await request.body()) else "link")
    except NotFound:
        return HTMLResponse("Not found", status_code=404)
    if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded") and b"List-Unsubscribe" in (await request.body()):
        return Response(status_code=200)
    ctx = {"biz": biz, "user": None, "actor": "unsubscribe", "role": None, "businesses": []}
    return render(request, "unsub.html", ctx, token=token, done=True)


@router.get("/o/{token}.gif")
async def open_pixel(token: str):
    try:
        CG.opened(token)
    except Exception:                                         # noqa: BLE001 — the picture is shown whatever happens
        pass
    return Response(PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/c/{token}/{n}")
async def click(token: str, n: int):
    try:
        to = CG.clicked(token, n)
    except NotFound:
        return HTMLResponse("Not found", status_code=404)
    return RedirectResponse(to, status_code=302)

