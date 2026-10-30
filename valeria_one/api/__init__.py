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
from starlette.exceptions import HTTPException as StarletteHTTPException

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
                "Created by Tadewan Pascua (tad.pascua@valai.mx). Licensed AGPL-3.0; commercial licenses available.",
    license_info={"name": "AGPL-3.0", "url": "https://www.gnu.org/licenses/agpl-3.0.html"},
    contact={"name": "Tadewan Pascua", "email": "tad.pascua@valai.mx", "url": "https://github.com/valeriaone/valeria-one"},
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
    from ..web.crm import router as crm_router
    app.include_router(crm_router)                      # v0.16 first: /app/contacts/import must not reach /app/contacts/{id}
    from ..web.campaigns import router as campaigns_router
    from ..web.invoices import router as invoices_router
    from ..web.pwa import router as pwa_router
    from ..web.setups import router as setups_router
    from ..web.voice import router as voice_router
    from ..web.workflows import router as workflows_router
    for r in (campaigns_router, workflows_router, invoices_router, voice_router, setups_router, pwa_router):
        app.include_router(r)                           # v0.18 (ghl01): campaigns, workflows, invoices, phone agent, setups, the app
    from ..web.addmore import router as addmore_router
    app.include_router(addmore_router)                  # v0.19 (kit01): Add more (tools on/off) and Valeria's suggestion
    app.include_router(web_router)
    from ..web.studio import router as studio_router
    from ..web.studio import studio_host_guard
    app.include_router(studio_router)
    app.middleware("http")(studio_host_guard)
    from ..web.assistant import router as assistant_router
    app.include_router(assistant_router)
    from ..web.aisearch import router as aisearch_router
    app.include_router(aisearch_router)                 # before the public pages: /notes/x.md must not reach /notes/{slug}
    from ..web.directory import router as directory_router
    from ..web.google import router as google_router
    from ..web.posts import router as posts_router
    from ..web.tiktok import router as tiktok_router
    for r in (directory_router, google_router, posts_router, tiktok_router):   # v0.12: directory, Google, posts, TikTok
        app.include_router(r)
    from ..web.valeria import router as valeria_router
    app.include_router(valeria_router)                  # v0.13: Valeria, the owner's assistant; helpers
    from ..web.marketing import router as marketing_router
    app.include_router(marketing_router)
    from ..web.notes import router as notes_router
    app.include_router(notes_router)


_wire_web()


@app.middleware("http")
async def _sandbox_visits(request: Request, call_next):
    """Sandbox only: count page views (anonymously) and notice visits from Meta's network."""
    response = await call_next(request)
    if config.SANDBOX() and request.method == "GET" and response.status_code == 200 \
            and response.headers.get("content-type", "").startswith("text/html"):
        from ..sandbox import visits
        visits.record(request.url.path, request.client.host if request.client else "",
                      request.headers.get("referer"), request.headers.get("user-agent"))
    return response


# ---------------------------------------------------------------- errors: always JSON, never a stack trace
def _err(status: int, code: str, message: str, field: str | None = None):
    e = {"code": code, "message": message}
    if field:
        e["field"] = field
    return JSONResponse({"error": e}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def _http(_, e: StarletteHTTPException):
    code = {401: "unauthorized", 402: "payment_required", 403: "forbidden", 404: "not_found",
            405: "method_not_allowed"}.get(e.status_code, "http_error")
    if e.status_code == 403 and "paused" in str(e.detail):
        code = "suspended"
    r = _err(e.status_code, code, str(e.detail))
    for k, v in (getattr(e, "headers", None) or {}).items():
        r.headers[k] = v
    return r


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


_PAUSED_OPEN = ("/v1/business", "/v1/export")             # what a paused business can still reach: itself and its data


def auth(request: Request, authorization: str = Header("", description="Bearer vo_live_… / vo_test_…")) -> Ctx:
    tok = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    with db.tx() as conn:
        found = B.by_key(conn, tok)
    if not found:
        raise HTTPException(401, "missing or unknown API key")
    if found[0]["suspended_at"]:
        raise HTTPException(403, "this business is paused by its provider")
    from ..services import billing
    if billing.paused(found[0]) and not (request.url.path in _PAUSED_OPEN and request.method == "GET"):
        raise HTTPException(402, "the free launch months are over; choose a plan in Billing to use the API again "
                                 "(GET /v1/business and GET /v1/export still work; nothing was deleted)")
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


@app.get("/v1/export", tags=["business"])
def export_business(ctx: Ctx = Depends(auth)):
    """Everything this business has here, as one JSON document (no secrets). Works even when it is paused."""
    from ..services import export as EX
    with db.tx() as conn:
        data = EX.business_data(conn, ctx.biz["id"], ctx.actor)
    return ok(data)


@app.patch("/v1/business", tags=["business"])
def patch_business(body: S.BusinessPatch, ctx: Ctx = Depends(auth)):
    fields = body.model_dump(exclude_unset=True)
    blocks_on, words = fields.pop("blocks", None), fields.pop("words", None)
    with db.tx() as conn:
        biz = B.patch(conn, ctx.biz["id"], ctx.actor, **fields)
        if blocks_on is not None:
            biz = B.set_blocks(conn, ctx.biz["id"], ctx.actor, blocks_on)
        if words is not None:
            biz = B.set_words(conn, ctx.biz["id"], ctx.actor, words)
        return ok(biz)


# ---------------------------------------------------------------- contacts
@app.get("/v1/contacts", tags=["contacts"])
def list_contacts(q: str | None = None, tag: str | None = None, limit: int = 50, before: datetime | None = None,
                  field: str | None = None, value: str | None = None, ctx: Ctx = Depends(auth)):
    """`field` (a key from /v1/fields, or its name) + `value`: only the ones whose field is that (no value: has one)."""
    with db.tx() as conn:
        return ok(C.search(conn, ctx.biz["id"], q=q, tag=tag, limit=limit, after=before, field=field, value=value))


@app.get("/v1/fields", tags=["contacts"])
def list_fields(object: str | None = None, ctx: Ctx = Depends(auth)):
    """The business's own fields on contacts and deals (their values are in each record's `custom`, by key)."""
    from ..services import fields as CF
    with db.tx() as conn:
        return ok([{k: f[k] for k in ("id", "object", "key", "label", "kind", "options", "show_in_list", "show_on_card", "position")}
                   for f in CF.list_(conn, ctx.biz["id"], object if object in CF.OBJECTS else None)])


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
def list_deals(stage: str | None = None, contact_id: UUID | None = None, limit: int = 50, field: str | None = None,
               value: str | None = None, ctx: Ctx = Depends(auth)):
    with db.tx() as conn:
        return ok(D.search(conn, ctx.biz["id"], stage=stage, contact_id=contact_id, limit=limit, field=field, value=value))


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


# ---------------------------------------------------------------- payments (the business's own provider)
@app.get("/v1/payment-accounts", tags=["payments"])
def list_payment_accounts(ctx: Ctx = Depends(auth)):
    from ..services import payments as PM
    with db.tx() as conn:
        return ok(PM.accounts(conn, ctx.biz["id"]))


@app.post("/v1/payment-accounts", tags=["payments"], status_code=201)
def connect_payment_account(body: S.PaymentAccountIn, ctx: Ctx = Depends(auth)):
    from ..services import payments as PM
    with db.tx() as conn:
        return ok(PM.connect(conn, ctx.biz, ctx.actor, body.provider, body.config), 201)


@app.get("/v1/payments", tags=["payments"])
def list_payments(contact_id: UUID | None = None, deal_id: UUID | None = None, ctx: Ctx = Depends(auth)):
    from ..services import payments as PM
    with db.tx() as conn:
        return ok(PM.list_(conn, ctx.biz["id"], contact_id=contact_id, deal_id=deal_id))


@app.post("/v1/payments", tags=["payments"], status_code=201)
def create_payment(body: S.PaymentIn, ctx: Ctx = Depends(auth)):
    """A payment link on the business's own provider; optionally sent straight to the customer."""
    from ..services import payments as PM
    try:
        pay = PM.create(ctx.biz, ctx.actor, amount_minor=body.amount_minor, description=body.description,
                        contact_id=body.contact_id, deal_id=body.deal_id, provider=body.provider)
    except PM.PayError as e:
        return _err(502, "provider_error", str(e))
    if body.send and body.contact_id:
        X.send(ctx.biz, ctx.actor, contact_id=body.contact_id, body=f"{body.description}: {pay['url']}",
               channel_account_id=body.channel_account_id, channel=body.channel)
    return ok(pay, 201)


@app.post("/v1/payments/received", tags=["payments"], status_code=201)
def record_payment(body: S.ReceivedIn, ctx: Ctx = Depends(auth)):
    """Money already received outside a link (cash, a transfer, a card machine): written down as paid."""
    from ..services import payments as PM
    return ok(PM.record_received(ctx.biz, ctx.actor, amount_minor=body.amount_minor, description=body.description,
                                 contact_id=body.contact_id, deal_id=body.deal_id), 201)


@app.post("/v1/payments/{payment_id}/confirm", tags=["payments"])
def confirm_payment(payment_id: UUID, ctx: Ctx = Depends(auth)):
    """The owner confirms a bank transfer (or any payment) arrived."""
    from ..services import payments as PM
    with db.tx() as conn:
        PM.owner_confirms(conn, ctx.biz["id"], ctx.actor, payment_id)
    return ok({"id": payment_id, "status": "paid"})


@app.post("/hooks/pay/{hook_id}", include_in_schema=False)
async def payment_hook(hook_id: str, request: Request):
    from ..services import NotFound as NF
    from ..services import payments as PM
    body = await request.body()
    try:
        PM.webhook(hook_id, {k.lower(): v for k, v in request.headers.items()}, body, dict(request.query_params))
    except PM.BadSig:
        return PlainTextResponse("forbidden", 403)
    except NF:
        return PlainTextResponse("not found", 404)
    return PlainTextResponse("ok")


# ---------------------------------------------------------------- quotes with e-signature
@app.get("/v1/quotes", tags=["quotes"])
def list_quotes(contact_id: UUID | None = None, deal_id: UUID | None = None, ctx: Ctx = Depends(auth)):
    from ..services import quotes as QS
    with db.tx() as conn:
        return ok(QS.list_(conn, ctx.biz["id"], contact_id=contact_id, deal_id=deal_id))


@app.post("/v1/quotes", tags=["quotes"], status_code=201)
def create_quote(body: S.QuoteIn, ctx: Ctx = Depends(auth)):
    """Frozen when created (SHA-256 fingerprint); the customer signs at url."""
    from ..services import quotes as QS
    with db.tx() as conn:
        q = QS.create(conn, ctx.biz, ctx.actor, title=body.title, lines=[ln.model_dump() for ln in body.lines],
                      contact_id=body.contact_id, deal_id=body.deal_id, notes=body.notes, valid_days=body.valid_days)
    return ok({**q, "url": f"{config.PUBLIC_URL()}/q/{q['token']}"}, 201)


@app.post("/v1/quotes/{quote_id}/cancel", tags=["quotes"])
def cancel_quote(quote_id: UUID, ctx: Ctx = Depends(auth)):
    from ..services import quotes as QS
    with db.tx() as conn:
        QS.cancel(conn, ctx.biz["id"], ctx.actor, quote_id)
    return ok({"id": quote_id, "status": "cancelled"})


# ---------------------------------------------------------------- follow-ups + campaigns
@app.get("/v1/automations", tags=["follow-ups"])
def list_automations(ctx: Ctx = Depends(auth)):
    from ..services import followups as FU
    with db.tx() as conn:
        return ok(FU.automations(conn, ctx.biz["id"]))


@app.post("/v1/automations", tags=["follow-ups"], status_code=201)
def create_automation(body: S.AutomationIn, ctx: Ctx = Depends(auth)):
    from ..services import followups as FU
    with db.tx() as conn:
        return ok(FU.create_automation(conn, ctx.biz, ctx.actor, **body.model_dump()), 201)


@app.delete("/v1/automations/{automation_id}", tags=["follow-ups"], status_code=204)
def delete_automation(automation_id: UUID, ctx: Ctx = Depends(auth)):
    from ..services import followups as FU
    with db.tx() as conn:
        FU.delete_automation(conn, ctx.biz["id"], ctx.actor, automation_id)
    return PlainTextResponse("", status_code=204)


@app.get("/v1/campaigns", tags=["follow-ups"])
def list_campaigns(ctx: Ctx = Depends(auth)):
    from ..services import followups as FU
    with db.tx() as conn:
        return ok(FU.campaigns(conn, ctx.biz["id"]))


@app.post("/v1/campaigns", tags=["follow-ups"], status_code=201)
def create_campaign(body: S.CampaignIn, ctx: Ctx = Depends(auth)):
    """Only customers who agreed to promotions get it; quiet hours and WhatsApp rules apply."""
    from ..services import followups as FU
    with db.tx() as conn:
        return ok(FU.create_campaign(conn, ctx.biz, ctx.actor, **body.model_dump()), 201)


@app.post("/v1/campaigns/{campaign_id}/cancel", tags=["follow-ups"])
def cancel_campaign(campaign_id: UUID, ctx: Ctx = Depends(auth)):
    from ..services import followups as FU
    with db.tx() as conn:
        FU.cancel_campaign(conn, ctx.biz["id"], ctx.actor, campaign_id)
    return ok({"id": campaign_id, "status": "cancelled"})


# ---------------------------------------------------------------- grow: win-back + bring a friend
@app.get("/v1/growth", tags=["grow"])
def get_growth(ctx: Ctx = Depends(auth)):
    """Win-back and bring-a-friend at a glance: settings, who went quiet, results, referrals."""
    from ..services import growth as GR
    with db.tx() as conn:
        return ok(GR.overview(conn, ctx.biz))


@app.patch("/v1/growth", tags=["grow"])
def patch_growth(body: S.GrowthIn, ctx: Ctx = Depends(auth)):
    """Change the numbers and rewards; switch the win-back or the bring-a-friend message on or off."""
    from ..services import growth as GR
    d = body.model_dump(exclude_unset=True)
    with db.tx() as conn:
        GR.save_settings(conn, ctx.biz, ctx.actor, **{k: d[k] for k in (*GR.LIMITS, *GR.TEXTS) if k in d})
        if d.get("winback") is not None or "winback_message" in d or "winback_template" in d:
            on = d["winback"] if d.get("winback") is not None else bool(GR.ready_made(conn, ctx.biz["id"], "winback"))
            GR.set_winback(conn, ctx.biz, ctx.actor, on, message=d.get("winback_message"), template=d.get("winback_template"))
        if d.get("share") is not None or "share_when" in d or "share_message" in d:
            on = d["share"] if d.get("share") is not None else bool(GR.ready_made(conn, ctx.biz["id"], "referral"))
            GR.set_share(conn, ctx.biz, ctx.actor, on, when=d.get("share_when"), message=d.get("share_message"))
        return ok(GR.overview(conn, ctx.biz))


@app.get("/v1/growth/quiet", tags=["grow"])
def growth_quiet(limit: int = 50, ctx: Ctx = Depends(auth)):
    """Customers who have gone quiet, newest first; `can` says whether the win-back can reach them (else `why`)."""
    from ..services import growth as GR
    with db.tx() as conn:
        return ok(GR.quiet(conn, ctx.biz, limit=limit))


@app.get("/v1/referrals", tags=["grow"])
def list_referrals(limit: int = 100, ctx: Ctx = Depends(auth)):
    """Friends who came through a customer's link, with both rewards and whether each was given."""
    from ..services import growth as GR
    with db.tx() as conn:
        return ok(GR.referrals(conn, ctx.biz["id"], limit=limit))


@app.post("/v1/referrals/{referral_id}/given", tags=["grow"])
def referral_given(referral_id: UUID, body: S.RewardGivenIn, ctx: Ctx = Depends(auth)):
    from ..services import growth as GR
    with db.tx() as conn:
        return ok(GR.mark_given(conn, ctx.biz["id"], ctx.actor, referral_id, body.who))


@app.post("/v1/contacts/{contact_id}/referral-link", tags=["grow"])
def referral_link(contact_id: UUID, ctx: Ctx = Depends(auth)):
    """The customer's own bring-a-friend link (made once; the same link after that)."""
    from ..services import growth as GR
    with db.tx() as conn:
        ln = GR.link_for(conn, ctx.biz, contact_id)
    return ok({**ln, "url": GR.url(ln["code"])})


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
    stats["owner"] = [autoreply.respond_owner(ctx.biz["id"], mid) for mid in stats.pop("owner_ids", [])]
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
        from ..services import autoreply
        for bid, mid in st.get("owner", []):
            bg.add_task(autoreply.respond_owner, bid, mid)
        for bid, cid in st.get("ai", []):
            bg.add_task(autoreply.respond, bid, cid)
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    return PlainTextResponse("ok")


@app.post("/hooks/tiktok", include_in_schema=False)
async def tiktok_hook_receive(request: Request, bg: BackgroundTasks):
    """TikTok's one webhook (Business Messaging) for every business connected through the app."""
    from ..services import tiktok as TT
    try:
        st = TT.receive(_hook_req(request, await request.body(), None))
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    from ..services import autoreply
    for bid, cid in st.get("ai", []):
        bg.add_task(autoreply.respond, bid, cid)
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
    for mid in (stats or {}).get("owner_ids", []):              # the owner's own phone: Valeria answers them
        bg.add_task(autoreply.respond_owner, business_id, mid)
    for cid in (stats or {}).get("conversation_ids", []):
        bg.add_task(autoreply.respond, business_id, cid)


@app.post("/hooks/{hook_id}/voice", include_in_schema=False)
async def hook_voice(hook_id: str, request: Request):
    """A call to the business's Twilio number: ring the owner, or text the caller back."""
    from ..services import calls
    acc = X.account_by_hook(hook_id)
    if not acc or acc["channel"] != "sms":
        return PlainTextResponse("not found", 404)
    body = await request.body()
    from urllib.parse import parse_qsl
    try:
        xml = calls.voice(acc, _hook_req(request, body, dict(parse_qsl(body.decode(), keep_blank_values=True))))
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    return PlainTextResponse(xml, media_type="application/xml")


@app.post("/hooks/{hook_id}/voice/done", include_in_schema=False)
async def hook_voice_done(hook_id: str, request: Request):
    from ..services import calls
    acc = X.account_by_hook(hook_id)
    if not acc or acc["channel"] != "sms":
        return PlainTextResponse("not found", 404)
    body = await request.body()
    from urllib.parse import parse_qsl
    try:
        xml = calls.voice_done(acc, _hook_req(request, body, dict(parse_qsl(body.decode(), keep_blank_values=True))))
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    return PlainTextResponse(xml, media_type="application/xml")


@app.post("/hooks/{hook_id}/voice/agent", include_in_schema=False)
async def hook_voice_agent(hook_id: str, request: Request):
    """v0.18: one turn of a call the phone agent is answering (signature-checked like every Twilio webhook)."""
    return await _voice_hook(hook_id, request, "agent_turn")


@app.post("/hooks/{hook_id}/voice/agent/recording", include_in_schema=False)
async def hook_voice_agent_recording(hook_id: str, request: Request):
    return await _voice_hook(hook_id, request, "agent_recording")


async def _voice_hook(hook_id: str, request: Request, fn: str):
    from ..services import calls
    acc = X.account_by_hook(hook_id)
    if not acc or acc["channel"] != "sms":
        return PlainTextResponse("not found", 404)
    body = await request.body()
    from urllib.parse import parse_qsl
    try:
        xml = getattr(calls, fn)(acc, _hook_req(request, body, dict(parse_qsl(body.decode(errors="replace"), keep_blank_values=True))))
    except BadSignature:
        return PlainTextResponse("forbidden", 403)
    return PlainTextResponse(xml, media_type="application/xml")


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
    if acc["channel"] in ("sms", "whatsapp_twilio"):
        return PlainTextResponse("<Response/>", media_type="application/xml")
    return PlainTextResponse("ok")


# ---------------------------------------------------------------- this server's own billing (Stripe)
@app.post("/hooks/billing/stripe", include_in_schema=False)
async def billing_stripe_hook(request: Request):
    from ..services import billing as BL
    body = await request.body()
    try:
        res = BL.stripe_webhook({k.lower(): v for k, v in request.headers.items()}, body)
    except BL.BillingError:
        return PlainTextResponse("bad signature", 400)
    return ok({"result": res})


# ---------------------------------------------------------------- Partner API: hosts and agencies
class PCtx:
    def __init__(self, key: dict):
        self.key = key
        self.actor = f"partner:{key['prefix']}"


def partner_auth(authorization: str = Header("", description="Bearer vo_partner_…")) -> PCtx:
    """A partner key runs the whole server (never on the public sandbox). Create one on the Server page
    or with `valeria-one partner-key NAME`."""
    if config.SANDBOX():
        raise HTTPException(404, "not found")
    from ..services import partner as PT
    tok = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    with db.tx() as conn:
        k = PT.by_key(conn, tok)
    if not k:
        raise HTTPException(401, "missing or unknown partner key")
    return PCtx(k)


def _invite_url(token: str) -> str:
    return f"{config.PUBLIC_URL()}/join/{token}"


@app.get("/v1/partner/server", tags=["partner"])
def partner_server(p: PCtx = Depends(partner_auth)):
    """Version, newest release, background jobs, database and disk, backups, last backup/update results."""
    from ..services import partner as PT
    with db.tx() as conn:
        return ok(PT.server(conn))


@app.post("/v1/partner/server/{op}", tags=["partner"], status_code=202)
def partner_server_request(op: str, p: PCtx = Depends(partner_auth)):
    """op = backup | update. The server's host runs it within a minute; GET /v1/partner/server shows the result.
    An update backs up first and rolls back by itself if the new version does not start."""
    from ..services import instance, partner as PT
    if op not in instance.OPS:
        raise NotFound("operation")
    if not instance.managed():
        return _err(409, "not_managed", "backups and updates are not managed by the app on this server")
    with db.tx() as conn:
        PT.request(conn, op, p.actor)
    return ok({"requested": op}, 202)


@app.get("/v1/partner/businesses", tags=["partner"])
def partner_businesses(partner_ref: str | None = None, limit: int = 200, offset: int = 0, p: PCtx = Depends(partner_auth)):
    from ..services import partner as PT
    with db.tx() as conn:
        return ok(PT.list_(conn, partner_ref=partner_ref, limit=limit, offset=offset))


@app.post("/v1/partner/businesses", tags=["partner"], status_code=201)
def partner_business_create(body: S.PartnerBusinessIn, p: PCtx = Depends(partner_auth)):
    """Create a business for your customer. Send them `invite_url` (works once, 7 days) to own it."""
    from ..services import partner as PT
    with db.tx() as conn:
        biz, token = PT.create(conn, p.actor, name=body.name, country=body.country, owner_email=body.owner_email,
                               industry=body.industry, language=body.language, timezone=body.timezone,
                               partner_ref=body.partner_ref, blocks_on=body.blocks)
    return ok({"business": biz, "invite_url": _invite_url(token)}, 201)


@app.get("/v1/partner/businesses/{business_id}", tags=["partner"])
def partner_business_get(business_id: UUID, p: PCtx = Depends(partner_auth)):
    from ..services import partner as PT
    with db.tx() as conn:
        return ok(PT.get(conn, business_id))


@app.post("/v1/partner/businesses/{business_id}/suspend", tags=["partner"])
def partner_business_suspend(business_id: UUID, body: S.PartnerSuspendIn | None = None, p: PCtx = Depends(partner_auth)):
    """Pause: nobody signs in, its API keys stop, nothing is sent; customers' messages still arrive and are kept."""
    from ..services import partner as PT
    with db.tx() as conn:
        return ok(PT.suspend(conn, p.actor, business_id, (body.reason if body else "") or ""))


@app.post("/v1/partner/businesses/{business_id}/resume", tags=["partner"])
def partner_business_resume(business_id: UUID, p: PCtx = Depends(partner_auth)):
    from ..services import partner as PT
    with db.tx() as conn:
        return ok(PT.resume(conn, p.actor, business_id))


@app.post("/v1/partner/businesses/{business_id}/invite", tags=["partner"], status_code=201)
def partner_business_invite(business_id: UUID, body: S.PartnerInviteIn, p: PCtx = Depends(partner_auth)):
    """A new invitation link (e.g. the owner lost theirs, or a second owner)."""
    from ..services import partner as PT
    with db.tx() as conn:
        token = PT.invite(conn, p.actor, business_id, body.email, body.role)
    return ok({"invite_url": _invite_url(token)}, 201)


@app.post("/v1/partner/businesses/{business_id}/delete", tags=["partner"], status_code=204)
def partner_business_delete(business_id: UUID, body: S.PartnerDeleteIn, p: PCtx = Depends(partner_auth)):
    """Delete the business and all its data for good (it disappears from backups as they rotate)."""
    from ..services import partner as PT
    with db.tx() as conn:
        PT.remove(conn, p.actor, business_id, body.confirm)
    return PlainTextResponse("", status_code=204)


@app.get("/v1/partner/usage", tags=["partner"])
def partner_usage(month: str | None = None, business_id: UUID | None = None, p: PCtx = Depends(partner_auth)):
    """Counts per business for a month (YYYY-MM, UTC), for billing: messages in/out, customers, people,
    appointments, payments received. Never content."""
    from ..services import partner as PT
    with db.tx() as conn:
        return ok(PT.usage(conn, month, business_id))


@app.get("/health", include_in_schema=False)
def health():
    with db.tx() as conn:
        from sqlalchemy import text
        conn.execute(text("SELECT 1"))
    return {"ok": True, "version": __version__, "sandbox": config.SANDBOX()}
