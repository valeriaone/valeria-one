"""One-click Meta connection (Valeria One as a Meta Tech Provider) and Lead Ads.

The operator of a Valeria One server owns ONE Meta app. Businesses connect their own WhatsApp number,
Facebook Pages and Instagram accounts to it with Meta's own pop-ups (Embedded Signup / Facebook Login for
Business); we never see their Facebook password. All their events then arrive at one webhook,
/hooks/meta, signed with the app secret, and are routed here to the right business.

Settings (environment): VO_META_APP_ID, VO_META_APP_SECRET, VO_META_VERIFY_TOKEN,
VO_META_WA_CONFIG_ID (Embedded Signup configuration), VO_META_LOGIN_CONFIG_ID (Login for Business
configuration for Pages + Instagram + leads). Without them the feature stays off and businesses can still
connect channels by hand.
"""
from __future__ import annotations

import json
import secrets
from uuid import UUID

import httpx
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import channels, config, crypto, db, packs, phones
from ..channels import BadSignature, HookRequest
from ..channels.meta import graph
from . import Invalid, contacts, log
from . import inbox as X

A = db.channel_accounts
PAGE_FIELDS = "messages,messaging_postbacks,message_deliveries,message_reads,leadgen"
LOGIN_SCOPES = ("pages_show_list,pages_messaging,pages_manage_metadata,pages_read_engagement,"
                "instagram_basic,instagram_manage_messages,leads_retrieval,business_management")


class MetaError(Exception):
    pass


def settings() -> dict:
    return {"app_id": config.get("VO_META_APP_ID"), "app_secret": config.get("VO_META_APP_SECRET"),
            "verify_token": config.get("VO_META_VERIFY_TOKEN"), "wa_config_id": config.get("VO_META_WA_CONFIG_ID"),
            "login_config_id": config.get("VO_META_LOGIN_CONFIG_ID")}


def configured() -> bool:
    s = settings()
    return bool(s["app_id"] and s["app_secret"] and s["verify_token"])


def _http(http: httpx.Client | None) -> httpx.Client:
    return http or httpx.Client(timeout=20)


def _ok(r: httpx.Response) -> dict:
    try:
        j = r.json()
    except ValueError:
        raise MetaError(f"Meta answered {r.status_code}") from None
    if r.status_code >= 300 or "error" in j:
        e = j.get("error", {})
        raise MetaError(f"Meta: {e.get('message') or r.status_code}")
    return j


def exchange_code(code: str, http=None) -> str:
    s = settings()
    j = _ok(_http(http).get(f"{graph()}/oauth/access_token",
                            params={"client_id": s["app_id"], "client_secret": s["app_secret"], "code": code}))
    if not j.get("access_token"):
        raise MetaError("Meta did not return a token")
    return j["access_token"]


# ---------------------------------------------------------------- WhatsApp (Embedded Signup)
def connect_whatsapp(conn: Connection, biz: dict, actor: str, *, code: str, waba_id: str, phone_number_id: str,
                     http=None) -> dict:
    """After the business finishes Meta's WhatsApp pop-up: get its token, subscribe our app to its WhatsApp
    account, register the number for the Cloud API, and add it to the inbox."""
    _check_allowed(biz)
    if not (code and waba_id.isdigit() and phone_number_id.isdigit()):
        raise Invalid("Meta did not finish the WhatsApp connection", "whatsapp")
    _not_taken(conn, biz, "whatsapp", "phone_number_id", phone_number_id)
    h = _http(http)
    token = exchange_code(code, h)
    auth = {"Authorization": f"Bearer {token}"}
    _ok(h.post(f"{graph()}/{waba_id}/subscribed_apps", headers=auth))
    pin = f"{secrets.randbelow(10**6):06d}"                     # two-step PIN for the number; kept encrypted
    reg = h.post(f"{graph()}/{phone_number_id}/register", headers=auth, json={"messaging_product": "whatsapp", "pin": pin})
    if reg.status_code >= 300 and "already registered" not in reg.text.lower():
        _ok(reg)
    info = _ok(h.get(f"{graph()}/{phone_number_id}", headers=auth, params={"fields": "display_phone_number,verified_name"}))
    cfg = {"via": "meta_platform", "phone_number_id": phone_number_id, "waba_id": waba_id, "token": token, "pin": pin}
    name = info.get("verified_name") or "WhatsApp"
    acc = _add(conn, biz, actor, "whatsapp", f"{name} · {info.get('display_phone_number', '')}".strip(" ·"),
               info.get("display_phone_number"), cfg)
    return acc


# ---------------------------------------------------------------- Pages + Instagram (Login for Business)
def list_pages(code: str, http=None) -> tuple[str, list[dict]]:
    """-> (long-lived user token, the business's Pages with their Instagram accounts)."""
    h = _http(http)
    s = settings()
    short = exchange_code(code, h)
    long_ = _ok(h.get(f"{graph()}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": s["app_id"], "client_secret": s["app_secret"],
        "fb_exchange_token": short})).get("access_token") or short
    pages = _ok(h.get(f"{graph()}/me/accounts", params={
        "access_token": long_, "fields": "id,name,access_token,instagram_business_account{id,username}", "limit": 100}))
    return long_, [{"id": p["id"], "name": p.get("name", ""), "token": p.get("access_token"),
                    "ig": p.get("instagram_business_account")} for p in pages.get("data", []) if p.get("access_token")]


def connect_pages(conn: Connection, biz: dict, actor: str, pages: list[dict], chosen: list[str], http=None) -> list[dict]:
    """Subscribe our app to each chosen Page (messages + leads) and add Messenger and, when linked, Instagram."""
    _check_allowed(biz)
    h = _http(http)
    out = []
    for p in pages:
        if p["id"] not in chosen:
            continue
        _not_taken(conn, biz, "messenger", "page_id", p["id"])
        _ok(h.post(f"{graph()}/{p['id']}/subscribed_apps",
                   params={"subscribed_fields": PAGE_FIELDS, "access_token": p["token"]}))
        out.append(_add(conn, biz, actor, "messenger", p["name"] or "Facebook Page", p["id"],
                        {"via": "meta_platform", "page_id": p["id"], "page_token": p["token"]}))
        ig = p.get("ig") or {}
        if ig.get("id"):
            _not_taken(conn, biz, "instagram", "ig_user_id", ig["id"])
            out.append(_add(conn, biz, actor, "instagram", "@" + (ig.get("username") or ig["id"]), ig["id"],
                            {"via": "meta_platform", "ig_user_id": ig["id"], "page_id": p["id"], "page_token": p["token"]}))
    if not out:
        raise Invalid("pick at least one Page", "pages")
    return out


def _check_allowed(biz: dict) -> None:
    if not configured():
        raise Invalid("the Meta connection is not set up on this server yet", "meta")
    if biz["sandbox"] or config.SANDBOX():
        raise Invalid("sandbox businesses can't connect real Meta accounts", "meta")


def _not_taken(conn: Connection, biz: dict, channel: str, key: str, value: str) -> None:
    other = conn.execute(select(A.c.business_id).where(A.c.channel == channel, A.c.active,
                                                       A.c.config[key].astext == value)).scalar()
    if other and other != biz["id"]:
        raise Invalid(f"this {channel} account is already connected to another business", channel)
    if other == biz["id"]:
        raise Invalid(f"this {channel} account is already connected", channel)


def _add(conn: Connection, biz: dict, actor: str, channel: str, name: str, address: str | None, cfg: dict) -> dict:
    row = conn.execute(insert(A).values(business_id=biz["id"], channel=channel, name=name[:100], address=address,
                                        config=crypto.seal(cfg), hook_id=secrets.token_urlsafe(18)).returning(A)).mappings().one()
    log(conn, biz["id"], "channel.connected", actor, row["id"], {"channel": channel, "via": "meta"})
    return X.public_account(row)


# ---------------------------------------------------------------- the one webhook for every business
def handshake(req: HookRequest) -> str | None:
    q = req.query
    if q.get("hub.mode") != "subscribe":
        return None
    if configured() and crypto.same(q.get("hub.verify_token", ""), settings()["verify_token"]):
        return q.get("hub.challenge", "")
    raise BadSignature("bad verify token")


def receive(req: HookRequest, http=None) -> dict:
    """Verify Meta's signature once, then hand each piece to the business it belongs to."""
    if not configured():
        raise BadSignature("not configured")
    sig = req.headers.get("x-hub-signature-256", "")
    if not sig.startswith("sha256=") or not crypto.same(sig[7:], crypto.hmac_sha256_hex(settings()["app_secret"], req.body)):
        raise BadSignature("bad signature")
    data = json.loads(req.body or b"{}")
    obj = data.get("object")
    stats = {"routed": 0, "unknown": 0, "leads": 0}
    for entry in data.get("entry", []):
        if obj == "whatsapp_business_account":
            for ch in entry.get("changes", []):
                pnid = (ch.get("value") or {}).get("metadata", {}).get("phone_number_id")
                _route(stats, "whatsapp", "phone_number_id", pnid, {"entry": [{"changes": [ch]}]})
        elif obj == "page":
            if entry.get("messaging"):
                _route(stats, "messenger", "page_id", entry.get("id"), {"object": "page", "entry": [entry]})
            for ch in entry.get("changes", []):
                if ch.get("field") == "leadgen":
                    stats["leads"] += _lead(ch.get("value") or {}, http)
        elif obj == "instagram":
            _route(stats, "instagram", "ig_user_id", entry.get("id"), {"object": "instagram", "entry": [entry]})
    return stats


def _account_for(channel: str, key: str, value: str | None):
    if not value:
        return None
    with db.tx() as conn:
        return conn.execute(select(A).where(A.c.channel == channel, A.c.active, A.c.config[key].astext == str(value))
                            ).mappings().first()


def _route(stats: dict, channel: str, key: str, value, sub: dict) -> None:
    acc = _account_for(channel, key, value)
    if not acc:
        stats["unknown"] += 1
        return
    items = channels.adapter(channel).items(crypto.open_(acc["config"]), sub)
    X.receive_items(acc, items)
    stats["routed"] += 1


# ---------------------------------------------------------------- Lead Ads
_NAME = ("full_name", "name", "nombre_completo", "nome_completo")
_EMAIL = ("email", "correo", "e-mail")
_PHONE = ("phone_number", "phone", "telefono", "teléfono", "telefone", "whatsapp")


def _lead(v: dict, http=None) -> int:
    page_id, leadgen_id = v.get("page_id"), v.get("leadgen_id")
    acc = _account_for("messenger", "page_id", page_id)
    if not acc or not leadgen_id:
        return 0
    with db.tx() as conn:
        seen = conn.execute(select(db.meta_leads.c.id).where(db.meta_leads.c.business_id == acc["business_id"],
                                                             db.meta_leads.c.leadgen_id == str(leadgen_id))).first()
    if seen:
        return 0                                                  # Meta retries: one lead, one customer
    cfg = crypto.open_(acc["config"])
    try:
        lead = _ok(_http(http).get(f"{graph()}/{leadgen_id}", params={
            "access_token": cfg["page_token"], "fields": "created_time,field_data,form_id,ad_id"}))
    except (MetaError, httpx.HTTPError):
        return 0                                                  # Meta will retry the webhook
    return save_lead(acc, page_id, str(leadgen_id), lead)


def save_lead(acc, page_id: str, leadgen_id: str, lead: dict) -> int:
    fields = {(f.get("name") or "").strip().lower(): ", ".join(str(x) for x in (f.get("values") or []))
              for f in lead.get("field_data", [])}
    pick = lambda keys: next((fields[k] for k in keys if fields.get(k)), "")   # noqa: E731
    name = pick(_NAME) or " ".join(x for x in (fields.get("first_name"), fields.get("last_name")) if x)
    email, phone = pick(_EMAIL), pick(_PHONE)
    extra = {k: v for k, v in fields.items() if k not in (*_NAME, *_EMAIL, *_PHONE, "first_name", "last_name")}
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == acc["business_id"])).mappings().one())
        ins = pg_insert(db.meta_leads).values(business_id=biz["id"], leadgen_id=leadgen_id, page_id=str(page_id),
                                              form_id=lead.get("form_id"), ad_id=lead.get("ad_id"), data=fields)
        lid = conn.execute(ins.on_conflict_do_nothing(constraint="uq_meta_lead").returning(db.meta_leads.c.id)).scalar()
        if not lid:
            return 0
        contact = _match_or_create(conn, biz, name, email, phone, extra)
        conn.execute(db.meta_leads.update().where(db.meta_leads.c.id == lid).values(contact_id=contact["id"]))
        log(conn, biz["id"], "lead.received", "webhook:meta", contact["id"],
            {"form_id": lead.get("form_id"), "ad_id": lead.get("ad_id")})
    return 1


def _match_or_create(conn: Connection, biz: dict, name: str, email: str, phone: str, extra: dict) -> dict:
    I = db.identities
    e164 = None
    if phone:
        try:
            e164 = phones.e164(phone, packs.country(biz["country"]))
        except phones.BadPhone:
            e164 = None
    for ch, addr in (("whatsapp", e164), ("email", (email or "").strip().lower() or None)):
        if addr:
            cid = conn.execute(select(I.c.contact_id).where(I.c.business_id == biz["id"], I.c.channel == ch,
                                                            I.c.address == addr)).scalar()
            if cid:
                row = contacts.get(conn, biz["id"], cid)
                contacts.patch(conn, biz, "webhook:meta", cid, {"tags": sorted(set(row["tags"]) | {"lead-ad"})})
                return row
    notes = "\n".join(f"{k}: {v}" for k, v in extra.items())
    data = {"name": name or (email or e164 or "Lead"), "source": "lead_ad", "tags": ["lead-ad"], "notes": notes}
    if e164:
        data["phone"] = e164
    if email and "@" in email:
        data["email"] = email
    try:
        return contacts.create(conn, biz, "webhook:meta", data)
    except Invalid:
        data.pop("email", None)                                   # a typo'd email never loses the lead
        return contacts.create(conn, biz, "webhook:meta", data)


def leads(conn: Connection, business_id: UUID, limit: int = 50) -> list[dict]:
    L = db.meta_leads
    return [dict(r) for r in conn.execute(select(L, db.contacts.c.name.label("contact_name"))
                                          .outerjoin(db.contacts, db.contacts.c.id == L.c.contact_id)
                                          .where(L.c.business_id == business_id).order_by(L.c.created_at.desc())
                                          .limit(max(1, min(limit, 200)))).mappings()]
