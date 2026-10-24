"""Valeria One public API (v1). Run: uvicorn valeria_one.api:app

Auth: `Authorization: Bearer vo_live_…` (or vo_test_… in the sandbox). Each key belongs to one business
and only ever sees that business's data. Interactive docs at /docs.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import __version__, channels, config, db, packs
from ..channels import BadSignature, HookRequest
from ..services import Blocked, Invalid, NotFound
from ..services import businesses as B
from ..services import contacts as C
from ..services import deals as D
from ..services import inbox as X
from . import schemas as S

app = FastAPI(
    title="Valeria One API",
    version=__version__,
    description="The back office for any business, in any country, on any channel. "
                "Created by Tadewan Pascua. Licensed AGPL-3.0; commercial licenses available.",
    license_info={"name": "AGPL-3.0", "url": "https://www.gnu.org/licenses/agpl-3.0.html"},
)


# ---------------------------------------------------------------- web app (screens) + sessions
def _wire_web() -> None:
    import hashlib
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.sessions import SessionMiddleware

    from ..web import router as web_router
    secret = hashlib.sha256(("session:" + config.get("VO_SECRET_KEY", required=True)).encode()).hexdigest()
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="vo_session", max_age=14 * 86400,
                       same_site="lax", https_only=config.PUBLIC_URL().startswith("https://"))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "web" / "static")), name="static")
    app.include_router(web_router)


_wire_web()


# ---------------------------------------------------------------- errors: always JSON, never a stack trace
def _err(status: int, code: str, message: str, field: str | None = None):
    e = {"code": code, "message": message}
    if field:
        e["field"] = field
    return JSONResponse({"error": e}, status_code=status)


@app.exception_handler(NotFound)
async def _nf(_, e: NotFound):
    return _err(404, "not_found", f"{e} not found")


@app.exception_handler(Invalid)
async def _inv(_, e: Invalid):
    return _err(422, "invalid", str(e), e.field)


@app.exception_handler(Blocked)
async def _blk(_, e: Blocked):
    return _err(409, e.reason, str(e))


@app.exception_handler(Exception)
async def _boom(request: Request, e: Exception):
    logging.getLogger("valeria_one").exception("unhandled error on %s %s", request.method, request.url.path)
    return _err(500, "internal", "something went wrong on our side; it has been logged")


# ---------------------------------------------------------------- auth
class Ctx:
    def __init__(self, biz: dict, key: dict):
        self.biz, self.key = biz, key
        self.actor = f"api_key:{key['prefix']}"


def auth(authorization: str = Header("", description="Bearer vo_live_… / vo_test_…")) -> Ctx:
    tok = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    with db.tx() as conn:
        found = B.by_key(conn, tok)
    if not found:
        raise HTTPException(401, "missing or unknown API key")
    return Ctx(*found)


def ok(v, status: int = 200):
    return JSONResponse(jsonable_encoder(v), status_code=status)


# ---------------------------------------------------------------- packs (public)
@app.get("/v1/packs/countries", tags=["packs"])
def list_countries():
    return ok([p.model_dump() for p in packs.countries().values()])


@app.get("/v1/packs/countries/{code}", tags=["packs"])
def get_country(code: str):
    try:
        return ok(packs.country(code).model_dump())
    except KeyError:
        raise NotFound("country pack") from None


@app.get("/v1/packs/industries", tags=["packs"])
def list_industries():
    return ok([p.model_dump() for p in packs.industries().values()])


@app.get("/v1/packs/industries/{code}", tags=["packs"])
def get_industry(code: str):
    try:
        return ok(packs.industry(code).model_dump())
    except KeyError:
        raise NotFound("industry pack") from None


@app.get("/v1/channels/available", tags=["packs"])
def channels_available():
    return ok([{"channel": c, "needs": list(channels.adapter(c).needs),
                "reply_window_hours": channels.adapter(c).reply_window_hours} for c in channels.available()])


# ---------------------------------------------------------------- business
@app.get("/v1/business", tags=["business"])
def get_business(ctx: Ctx = Depends(auth)):
    cp = packs.country(ctx.biz["country"])
    return ok({**ctx.biz, "country_pack": cp.model_dump(), "industry_pack": packs.industry(ctx.biz["industry"]).model_dump()})


@app.patch("/v1/business", tags=["business"])
def patch_business(body: S.BusinessPatch, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(B.patch(conn, ctx.biz["id"], ctx.actor, **body.model_dump(exclude_unset=True)))


# ---------------------------------------------------------------- contacts
@app.get("/v1/contacts", tags=["contacts"])
def list_contacts(q: str | None = None, tag: str | None = None, limit: int = 50, before: datetime | None = None,
                  ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(C.search(conn, ctx.biz["id"], q=q, tag=tag, limit=limit, after=before))


@app.post("/v1/contacts", tags=["contacts"], status_code=201)
def create_contact(body: S.ContactIn, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(C.create(conn, ctx.biz, ctx.actor, body.model_dump(exclude_unset=True)), 201)


@app.get("/v1/contacts/{contact_id}", tags=["contacts"])
def get_contact(contact_id: UUID, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        c = C.get(conn, ctx.biz["id"], contact_id)
        return ok({**c, "identities": C.identities(conn, ctx.biz["id"], contact_id)})


@app.patch("/v1/contacts/{contact_id}", tags=["contacts"])
def patch_contact(contact_id: UUID, body: S.ContactIn, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(C.patch(conn, ctx.biz, ctx.actor, contact_id, body.model_dump(exclude_unset=True)))


@app.delete("/v1/contacts/{contact_id}", tags=["contacts"], status_code=204)
def erase_contact(contact_id: UUID, ctx: Ctx = Depends(auth)):
    """Privacy erasure (GDPR, LGPD, LFPDPPP…): personal data and message text are wiped."""
    with db.tx() as conn:
        C.delete(conn, ctx.biz["id"], ctx.actor, contact_id)
    return PlainTextResponse("", status_code=204)


# ---------------------------------------------------------------- channels
@app.get("/v1/channels", tags=["inbox"])
def list_channels(ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(X.accounts(conn, ctx.biz["id"]))


@app.post("/v1/channels", tags=["inbox"], status_code=201)
def connect_channel(body: S.ChannelIn, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(X.connect(conn, ctx.biz, ctx.actor, channel=body.channel, name=body.name, address=body.address,
                            cfg=body.config), 201)


@app.delete("/v1/channels/{account_id}", tags=["inbox"], status_code=204)
def disconnect_channel(account_id: UUID, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        X.disconnect(conn, ctx.biz["id"], ctx.actor, account_id)
    return PlainTextResponse("", status_code=204)


# ---------------------------------------------------------------- conversations + messages
@app.get("/v1/conversations", tags=["inbox"])
def list_conversations(status: str | None = None, contact_id: UUID | None = None, limit: int = 50, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(X.conversations(conn, ctx.biz["id"], status=status, contact_id=contact_id, limit=limit))


@app.get("/v1/conversations/{conversation_id}", tags=["inbox"])
def get_conversation(conversation_id: UUID, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(X.conversation(conn, ctx.biz["id"], conversation_id))


@app.post("/v1/conversations/{conversation_id}/status", tags=["inbox"])
def conversation_status(conversation_id: UUID, body: S.StatusIn, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        X.set_status(conn, ctx.biz["id"], ctx.actor, conversation_id, body.status)
        return ok(X.conversation(conn, ctx.biz["id"], conversation_id, limit=1))


@app.post("/v1/messages", tags=["inbox"], status_code=201)
def send_message(body: S.MessageIn, ctx: Ctx = Depends(auth)):
    """Send on any connected channel. The country's rules are enforced (consent, quiet hours, reply windows)."""
    return ok(X.send(ctx.biz, ctx.actor, **body.model_dump()), 201)


# ---------------------------------------------------------------- deals + pipeline
@app.get("/v1/pipeline", tags=["deals"])
def get_pipeline(lang: str | None = None, ctx: Ctx = Depends(auth)):
    """Stage labels in `lang` when given (e.g. ja, ar), else in the business's language."""
    with db.tx() as conn:
        return ok(D.pipeline(conn, ctx.biz, lang))


@app.get("/v1/deals", tags=["deals"])
def list_deals(stage: str | None = None, contact_id: UUID | None = None, limit: int = 50, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(D.search(conn, ctx.biz["id"], stage=stage, contact_id=contact_id, limit=limit))


@app.post("/v1/deals", tags=["deals"], status_code=201)
def create_deal(body: S.DealIn, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(D.create(conn, ctx.biz, ctx.actor, body.model_dump(exclude_unset=True)), 201)


@app.get("/v1/deals/{deal_id}", tags=["deals"])
def get_deal(deal_id: UUID, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(D.get(conn, ctx.biz["id"], deal_id))


@app.patch("/v1/deals/{deal_id}", tags=["deals"])
def patch_deal(deal_id: UUID, body: S.DealPatch, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(D.patch(conn, ctx.biz, ctx.actor, deal_id, body.model_dump(exclude_unset=True)))


# ---------------------------------------------------------------- appointments
@app.get("/v1/appointments", tags=["appointments"])
def list_appointments(start: datetime | None = None, end: datetime | None = None, limit: int = 100, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(D.appointments(conn, ctx.biz["id"], start=start, end=end, limit=limit))


@app.post("/v1/appointments", tags=["appointments"], status_code=201)
def book_appointment(body: S.AppointmentIn, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(D.book(conn, ctx.biz, ctx.actor, body.model_dump()), 201)


@app.post("/v1/appointments/{appt_id}/status", tags=["appointments"])
def appointment_status(appt_id: UUID, body: S.StatusIn, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(D.set_appointment_status(conn, ctx.biz["id"], ctx.actor, appt_id, body.status))


# ---------------------------------------------------------------- Lead Ads
@app.get("/v1/leads", tags=["contacts"])
def list_leads(limit: int = 50, ctx: Ctx = Depends(auth)):
    """Facebook / Instagram Lead Ads received (each one is also a customer)."""
    from ..services import meta as MT
    with db.tx() as conn:
        return ok(MT.leads(conn, ctx.biz["id"], limit=limit))


# ---------------------------------------------------------------- public booking
@app.get("/v1/booking", tags=["booking"])
def get_booking(ctx: Ctx = Depends(auth)):
    from ..services import booking as BK
    with db.tx() as conn:
        b = B.get(conn, ctx.biz["id"])
        return ok({"enabled": b["booking_enabled"], "slug": b["slug"], "hours": b["hours"],
                   "notice_minutes": b["booking_notice_minutes"], "days_ahead": b["booking_days_ahead"],
                   "url": f"{config.PUBLIC_URL()}/b/{b['slug']}" if b["slug"] else None,
                   "services": BK.services(conn, ctx.biz["id"], active_only=True)})


@app.patch("/v1/booking", tags=["booking"])
def patch_booking(body: S.BookingSettingsIn, ctx: Ctx = Depends(auth)):
    from ..services import booking as BK
    with db.tx() as conn:
        BK.save_settings(conn, ctx.biz, ctx.actor, **body.model_dump(exclude_unset=True))
    return get_booking(ctx)


@app.post("/v1/booking/services", tags=["booking"], status_code=201)
def add_booking_service(body: S.ServiceIn, ctx: Ctx = Depends(auth)):
    from ..services import booking as BK
    with db.tx() as conn:
        return ok(BK.add_service(conn, ctx.biz, ctx.actor, **body.model_dump()), 201)


@app.delete("/v1/booking/services/{service_id}", tags=["booking"], status_code=204)
def remove_booking_service(service_id: UUID, ctx: Ctx = Depends(auth)):
    from ..services import booking as BK
    with db.tx() as conn:
        BK.remove_service(conn, ctx.biz["id"], ctx.actor, service_id)
    return PlainTextResponse("", status_code=204)


@app.get("/v1/public/{slug}", tags=["booking"])
def public_booking_info(slug: str):
    """No key needed: what a business offers on its booking page (for WhatsApp, a website, any app)."""
    from ..services import booking as BK
    with db.tx() as conn:
        b = BK.public_business(conn, slug)
        return ok({"name": b["name"], "timezone": b["timezone"], "currency": b["currency"], "language": b["language"],
                   "services": [{k: s[k] for k in ("id", "name", "minutes", "price_minor")}
                                for s in BK.services(conn, b["id"], active_only=True)]})


@app.get("/v1/public/{slug}/slots", tags=["booking"])
def public_slots(slug: str, service_id: UUID, day: str | None = None):
    from datetime import date as _date
    from ..services import booking as BK
    with db.tx() as conn:
        b = BK.public_business(conn, slug)
        if day:
            try:
                d = _date.fromisoformat(day)
            except ValueError:
                raise Invalid("day is YYYY-MM-DD", "day") from None
            return ok({"day": d.isoformat(), "slots": BK.slots(conn, b, service_id, d)})
        return ok({"days": [d.isoformat() for d in BK.open_days(conn, b, service_id)]})


_bookings: dict[str, deque] = defaultdict(deque)


def _allow(store: dict, key: str, n: int, window: int) -> bool:
    q, now = store[key], time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= n:
        return False
    q.append(now)
    return True


@app.post("/v1/public/{slug}/book", tags=["booking"], status_code=201)
def public_book(slug: str, body: S.PublicBookIn, request: Request):
    from ..services import booking as BK
    ip = request.client.host if request.client else "?"
    if not _allow(_bookings, ip, 10, 3600):
        return _err(429, "rate_limited", "too many bookings from this address; try later")
    with db.tx() as conn:
        b = BK.public_business(conn, slug)
        res = BK.book(conn, b, **body.model_dump())
    a = res["appointment"]
    return ok({"appointment_id": a["id"], "starts_at": a["starts_at"], "ends_at": a["ends_at"], "service": res["service"]["name"]}, 201)


# ---------------------------------------------------------------- AI with the business's own key
@app.get("/v1/ai", tags=["ai"])
def get_ai(ctx: Ctx = Depends(auth)):
    from ..services import ai as AIS
    with db.tx() as conn:
        return ok(AIS.public_settings(conn, ctx.biz["id"]))


@app.patch("/v1/ai", tags=["ai"])
def patch_ai(body: S.AISettingsIn, ctx: Ctx = Depends(auth)):
    from ..services import ai as AIS
    with db.tx() as conn:
        return ok(AIS.save(conn, ctx.biz, ctx.actor, **body.model_dump(exclude_unset=True)))


@app.post("/v1/conversations/{conversation_id}/suggest", tags=["ai"])
def suggest_reply(conversation_id: UUID, ctx: Ctx = Depends(auth)):
    """A draft reply from the business's AI (nothing is sent)."""
    from ..services import ai as AIS
    with db.tx() as conn:
        X.conversation(conn, ctx.biz["id"], conversation_id, limit=1)
    try:
        return ok(AIS.suggest(ctx.biz, ctx.actor, conversation_id))
    except AIS.AIError as e:
        return _err(502, "ai_unavailable", str(e))


@app.post("/v1/conversations/{conversation_id}/resume-ai", tags=["ai"])
def resume_ai(conversation_id: UUID, ctx: Ctx = Depends(auth)):
    """Let the AI answer this conversation again after it was handed to a person."""
    from ..services import ai as AIS
    with db.tx() as conn:
        X.conversation(conn, ctx.biz["id"], conversation_id, limit=1)
        AIS.clear_human(conn, ctx.biz["id"], ctx.actor, conversation_id)
        return ok(X.conversation(conn, ctx.biz["id"], conversation_id, limit=1))


# ---------------------------------------------------------------- comments -> private message
@app.get("/v1/comment-rules", tags=["comments"])
def list_comment_rules(ctx: Ctx = Depends(auth)):
    from ..services import comments as CM
    with db.tx() as conn:
        return ok(CM.rules(conn, ctx.biz["id"]))


@app.post("/v1/comment-rules", tags=["comments"], status_code=201)
def create_comment_rule(body: S.CommentRuleIn, ctx: Ctx = Depends(auth)):
    """When someone comments one of these words on a Facebook or Instagram post, answer them privately."""
    from ..services import comments as CM
    chans = list(body.channels) + (["sandbox"] if ctx.biz["sandbox"] else [])
    with db.tx() as conn:
        return ok(CM.create_rule(conn, ctx.biz, ctx.actor, keywords=body.keywords, reply=body.reply, channels=chans), 201)


@app.delete("/v1/comment-rules/{rule_id}", tags=["comments"], status_code=204)
def delete_comment_rule(rule_id: UUID, ctx: Ctx = Depends(auth)):
    from ..services import comments as CM
    with db.tx() as conn:
        CM.delete_rule(conn, ctx.biz["id"], ctx.actor, rule_id)
    return PlainTextResponse("", status_code=204)


@app.get("/v1/comments", tags=["comments"])
def list_comments(limit: int = 50, ctx: Ctx = Depends(auth)):
    from ..services import comments as CM
    with db.tx() as conn:
        return ok(CM.comments(conn, ctx.biz["id"], limit=limit))


@app.post("/v1/sandbox/simulate-comment", tags=["sandbox"])
def sandbox_simulate_comment(body: S.SimulateCommentIn, ctx: Ctx = Depends(auth)):
    """Pretend someone commented on this sandbox business's post."""
    if not ctx.biz["sandbox"]:
        raise NotFound("page")
    import uuid as _uuid
    from ..services import comments as CM
    with db.tx() as conn:
        acc = X._account(conn, ctx.biz["id"], None, "sandbox")
    slug = "".join(ch for ch in body.name.lower() if ch.isalnum())[:40] or "someone"
    result = CM.ingest(acc, "sandbox", {"comment_id": f"sbx_c_{_uuid.uuid4().hex[:12]}", "post_id": "sbx_post",
                                        "author_id": slug, "author_name": body.name, "text": body.text})
    return ok({"result": result})


# ---------------------------------------------------------------- outgoing webhooks
@app.get("/v1/webhooks", tags=["webhooks"])
def list_webhooks(ctx: Ctx = Depends(auth)):
    from ..services import webhooks as WH
    with db.tx() as conn:
        return ok(WH.endpoints(conn, ctx.biz["id"]))


@app.post("/v1/webhooks", tags=["webhooks"], status_code=201)
def create_webhook(body: S.WebhookIn, ctx: Ctx = Depends(auth)):
    """Get every event (or the ones you list) POSTed to your https address, signed. The secret is shown once."""
    from ..services import webhooks as WH
    with db.tx() as conn:
        ep, secret = WH.create(conn, ctx.biz, ctx.actor, body.url, body.events)
    return ok({**ep, "secret": secret}, 201)


@app.delete("/v1/webhooks/{endpoint_id}", tags=["webhooks"], status_code=204)
def delete_webhook(endpoint_id: UUID, ctx: Ctx = Depends(auth)):
    from ..services import webhooks as WH
    with db.tx() as conn:
        WH.delete(conn, ctx.biz["id"], ctx.actor, endpoint_id)
    return PlainTextResponse("", status_code=204)


@app.get("/v1/webhooks/{endpoint_id}/deliveries", tags=["webhooks"])
def webhook_deliveries(endpoint_id: UUID, ctx: Ctx = Depends(auth)):
    from ..services import webhooks as WH
    with db.tx() as conn:
        return ok(WH.deliveries(conn, ctx.biz["id"], endpoint_id))


@app.post("/v1/webhooks/{endpoint_id}/test", tags=["webhooks"], status_code=202)
def test_webhook(endpoint_id: UUID, ctx: Ctx = Depends(auth)):
    from ..services import webhooks as WH
    with db.tx() as conn:
        WH.test(conn, ctx.biz, ctx.actor, endpoint_id)
    return ok({"queued": True}, 202)


# ---------------------------------------------------------------- audit trail
@app.get("/v1/events", tags=["business"])
def list_events(after_id: int = 0, limit: int = 100, ctx: Ctx = Depends(auth)):
    """Everything that happened in this business, oldest first. Poll with after_id for a feed."""
    from sqlalchemy import select
    E = db.events
    with db.tx() as conn:
        rows = conn.execute(select(E).where(E.c.business_id == ctx.biz["id"], E.c.id > after_id)
                            .order_by(E.c.id).limit(max(1, min(limit, 500)))).mappings()
        return ok([dict(r) for r in rows])


# ---------------------------------------------------------------- sandbox
_signups: dict[str, deque] = defaultdict(deque)


def _sandbox_only():
    if not config.SANDBOX():
        raise NotFound("page")


@app.post("/v1/sandbox/signup", tags=["sandbox"], status_code=201)
def sandbox_signup(body: S.SandboxSignup, request: Request):
    """Sandbox server only: create a throwaway business and get an API key. Nothing real is ever sent."""
    _sandbox_only()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    q, now = _signups[ip], time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= 10:
        return _err(429, "rate_limited", "10 sandbox businesses per hour per address")
    q.append(now)
    with db.tx() as conn:
        biz = B.create(conn, name=body.name, country=body.country, industry=body.industry, sandbox=True)
        key, _ = B.new_key(conn, biz["id"], "sandbox signup")
        X.connect(conn, biz, "system", channel="sandbox", name="Sandbox inbox")
        from ..services import ai as AIS
        AIS.save(conn, biz, "system", provider="demo", mode="auto")       # visitors see the AI answer straight away
    return ok({"business": biz, "api_key": key, "note": "Keep this key: it is shown only once."}, 201)


@app.post("/v1/sandbox/simulate", tags=["sandbox"])
def sandbox_simulate(body: S.SimulateIn, ctx: Ctx = Depends(auth)):
    """Pretend a customer wrote to this business on its sandbox channel."""
    if not ctx.biz["sandbox"]:
        raise NotFound("page")
    with db.tx() as conn:
        acc = X._account(conn, ctx.biz["id"], body.channel_account_id, "sandbox")
    raw = json.dumps({"from": body.from_, "body": body.body, "name": body.name}).encode()
    stats = X.receive(acc, HookRequest("POST", "", {}, {}, raw))
    from ..services import autoreply
    stats["ai"] = [autoreply.respond(ctx.biz["id"], cid) for cid in stats.pop("conversation_ids", [])]
    return ok(stats)


# ---------------------------------------------------------------- inbound webhooks (providers -> us)
def _hook_req(request: Request, body: bytes, form: dict | None) -> HookRequest:
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return HookRequest(request.method, config.PUBLIC_URL() + path, {k.lower(): v for k, v in request.headers.items()},
                       dict(request.query_params), body, form)


@app.get("/hooks/meta", include_in_schema=False)
async def meta_hook_verify(request: Request):
    """Meta's one webhook for every business connected through the app (handshake)."""
    from ..services import meta as MT
    try:
        ans = MT.handshake(_hook_req(request, b"", None))
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    return PlainTextResponse(ans if ans is not None else "ok")


@app.post("/hooks/meta", include_in_schema=False)
async def meta_hook_receive(request: Request, bg: BackgroundTasks):
    from ..services import meta as MT
    try:
        st = MT.receive(_hook_req(request, await request.body(), None))
        for bid, cid in st.get("ai", []):
            from ..services import autoreply
            bg.add_task(autoreply.respond, bid, cid)
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    return PlainTextResponse("ok")


@app.get("/hooks/{hook_id}", include_in_schema=False)
async def hook_verify(hook_id: str, request: Request):
    acc = X.account_by_hook(hook_id)
    if not acc:
        return PlainTextResponse("not found", 404)
    try:
        ans = X.handshake(acc, _hook_req(request, b"", None))
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    return PlainTextResponse(ans if ans is not None else "ok")


def _after_inbound(bg: BackgroundTasks, business_id, stats: dict) -> None:
    """Let the business's AI answer (if it's on 'auto') after we've told the provider 'ok'."""
    from ..services import autoreply
    for cid in (stats or {}).get("conversation_ids", []):
        bg.add_task(autoreply.respond, business_id, cid)


@app.post("/hooks/{hook_id}", include_in_schema=False)
async def hook_receive(hook_id: str, request: Request, bg: BackgroundTasks):
    acc = X.account_by_hook(hook_id)
    if not acc or acc["channel"] == "sandbox":
        return PlainTextResponse("not found", 404)
    body = await request.body()
    form = None
    if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qsl
        form = dict(parse_qsl(body.decode(), keep_blank_values=True))
    try:
        stats = X.receive(acc, _hook_req(request, body, form))
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    _after_inbound(bg, acc["business_id"], stats)
    if acc["channel"] == "sms":
        return PlainTextResponse("<Response/>", media_type="application/xml")
    return PlainTextResponse("ok")


@app.get("/health", include_in_schema=False)
def health():
    with db.tx() as conn:
        from sqlalchemy import text
        conn.execute(text("SELECT 1"))
    return {"ok": True, "version": __version__, "sandbox": config.SANDBOX()}
