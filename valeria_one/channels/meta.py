"""Meta: WhatsApp Business Platform (Cloud API), Messenger and Instagram Direct.

The business connects its OWN Meta app / number / page. Config per channel account:
  whatsapp : phone_number_id, token, app_secret, verify_token
  messenger: page_id, page_token, app_secret, verify_token
  instagram: ig_user_id, page_token, app_secret, verify_token
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import config
from ..crypto import hmac_sha256_hex, same
from . import Adapter, BadSignature, HookRequest, Inbound, Outbound, SendResult, StatusUpdate, register


def graph() -> str:
    return f"https://graph.facebook.com/{config.get('VO_GRAPH_VERSION', 'v23.0')}"


def _check_sig(cfg: dict, req: HookRequest) -> None:
    sig = req.headers.get("x-hub-signature-256", "")
    if not cfg.get("app_secret") or not sig.startswith("sha256="):
        raise BadSignature("missing signature")
    if not same(sig[7:], hmac_sha256_hex(cfg["app_secret"], req.body)):
        raise BadSignature("bad signature")


def _handshake(cfg: dict, req: HookRequest) -> str | None:
    q = req.query
    if q.get("hub.mode") != "subscribe":
        return None
    if cfg.get("verify_token") and same(q.get("hub.verify_token", ""), cfg["verify_token"]):
        return q.get("hub.challenge", "")
    raise BadSignature("bad verify token")


def _ts(v) -> datetime | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(n / 1000 if n > 10**11 else n, tz=timezone.utc)


def _err(r) -> str:
    try:
        e = r.json().get("error", {})
        return f"{r.status_code} {e.get('code', '')} {e.get('message', '')}".strip()
    except ValueError:
        return f"{r.status_code} {r.text[:200]}"


@register
class WhatsApp(Adapter):
    channel = "whatsapp"
    reply_window_hours = 24
    address_kind = "phone"
    needs = ("phone_number_id", "token", "app_secret", "verify_token")

    def send(self, cfg, address, out: Outbound) -> SendResult:
        to = (address or "").lstrip("+")
        if out.template:
            t = out.template
            msg = {"type": "template", "template": {"name": t["name"], "language": {"code": t.get("language", "en")}}}
            if t.get("params"):
                msg["template"]["components"] = [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in t["params"]]}]
        elif out.options and len(out.options) <= 10:
            rows = [{"id": str(o["id"])[:200], "title": str(o["title"])[:24],
                     **({"description": str(o["description"])[:72]} if o.get("description") else {})} for o in out.options]
            body = out.body.split("\n\n", 1)[0][:1024] or "…"
            msg = {"type": "interactive", "interactive": {"type": "list", "body": {"text": body},
                                                          "action": {"button": str(out.options[0].get("button") or "…")[:20],
                                                                     "sections": [{"title": "·", "rows": rows}]}}}
        elif out.media:
            m = out.media[0]
            kind = m.get("type", "image") if m.get("type") in ("image", "document", "audio", "video") else "document"
            msg = {"type": kind, kind: {"link": m["url"], **({"caption": out.body} if out.body and kind != "audio" else {})}}
        else:
            msg = {"type": "text", "text": {"body": out.body, "preview_url": True}}
        r = self.http.post(f"{graph()}/{cfg['phone_number_id']}/messages",
                           headers={"Authorization": f"Bearer {cfg['token']}"},
                           json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **msg})
        if r.status_code >= 300:
            return SendResult("failed", error=_err(r))
        return SendResult("sent", provider_id=(r.json().get("messages") or [{}])[0].get("id"))

    def verify(self, cfg, req):
        return _handshake(cfg, req)

    def download(self, cfg, media):
        """WhatsApp media: the id -> a short-lived URL (Graph) -> the file, both with the business's token."""
        mid = media.get("provider_media_id")
        if not mid:
            raise ValueError("no media id")
        auth = {"Authorization": f"Bearer {cfg['token']}"}
        r = self.http.get(f"{graph()}/{mid}", headers=auth)
        if r.status_code >= 300:
            raise ValueError(_err(r))
        j = r.json()
        f = self.http.get(j["url"], headers=auth)
        if f.status_code >= 300:
            raise ValueError(f"{f.status_code}")
        return f.content, (j.get("mime_type") or media.get("mime") or f.headers.get("content-type") or "")

    def parse(self, cfg, req):
        _check_sig(cfg, req)
        return self.items(cfg, json.loads(req.body or b"{}"))

    def items(self, cfg, data: dict) -> list:
        """Messages and statuses for THIS number, from an already-verified webhook payload."""
        out: list = []
        for entry in data.get("entry", []):
            for ch in entry.get("changes", []):
                v = ch.get("value", {})
                if v.get("metadata", {}).get("phone_number_id") not in (None, cfg.get("phone_number_id")):
                    continue                      # another number on the same app
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in v.get("contacts", [])}
                for m in v.get("messages", []):
                    t = m.get("type")
                    body = (m.get("text") or {}).get("body") or (m.get("button") or {}).get("text") or ""
                    if t == "interactive":
                        it = m.get("interactive", {})
                        pick = it.get("button_reply") or it.get("list_reply") or {}
                        body = pick.get("title", "")
                    media = []
                    if t in ("image", "document", "audio", "video", "sticker"):
                        mm = m.get(t, {})
                        media = [{"type": t, "provider_media_id": mm.get("id"), "mime": mm.get("mime_type"),
                                  **({"voice": True} if mm.get("voice") else {})}]
                        body = body or mm.get("caption", "")
                    out.append(Inbound(address="+" + m["from"], body=body, provider_id=m.get("id"),
                                       name=names.get(m["from"]), media=media, at=_ts(m.get("timestamp"))))
                for s in v.get("statuses", []):
                    errs = s.get("errors") or []
                    out.append(StatusUpdate(provider_id=s.get("id"), status=s.get("status"),
                                            error=(errs[0].get("title") if errs else None)))
        return out


def _referral(ev: dict) -> dict | None:
    """mkt01: where a Messenger chat came from, when Meta says it — the message's own referral (an ad, a shop product), a
    referral event (m.me link, ad, chat plugin) or a "Get started" postback's. Only the fields we use, as plain strings;
    every one may be missing (Meta does not document a Marketplace listing field for Pages, so we read what is there)."""
    msg = ev.get("message") or {}
    r = msg.get("referral") or ev.get("referral") or (ev.get("postback") or {}).get("referral")
    if not isinstance(r, dict):
        return None
    product = r.get("product") if isinstance(r.get("product"), dict) else {}
    ads = r.get("ads_context_data") if isinstance(r.get("ads_context_data"), dict) else {}
    out = {"source": str(r.get("source") or "")[:40], "type": str(r.get("type") or "")[:40], "ref": str(r.get("ref") or "")[:200],
           "product_id": str(product.get("id") or ads.get("product_id") or "")[:40],
           "listing_id": str(r.get("listing_id") or r.get("marketplace_listing_id") or "")[:40],
           "title": str(ads.get("ad_title") or "")[:200]}
    out = {k: v for k, v in out.items() if v}
    return out or None


class _MessengerLike(Adapter):
    reply_window_hours = 24
    address_kind = "id"
    obj = "page"
    own_id_field = "page_id"

    def send(self, cfg, address, out: Outbound) -> SendResult:
        if out.media:
            m = out.media[0]
            message = {"attachment": {"type": m.get("type", "image") if m.get("type") in ("image", "video", "audio", "file") else "file",
                                      "payload": {"url": m["url"], "is_reusable": False}}}
        else:
            message = {"text": out.body}
        payload = {"recipient": {"id": address}, "message": message, "messaging_type": "RESPONSE"}
        if out.template and out.template.get("tag"):          # e.g. HUMAN_AGENT: a person answering up to 7 days later
            payload.update(messaging_type="MESSAGE_TAG", tag=out.template["tag"])
        r = self.http.post(f"{graph()}/me/messages", params={"access_token": cfg["page_token"]}, json=payload)
        if r.status_code >= 300:
            return SendResult("failed", error=_err(r))
        return SendResult("sent", provider_id=r.json().get("message_id"))

    def verify(self, cfg, req):
        return _handshake(cfg, req)

    def parse(self, cfg, req):
        _check_sig(cfg, req)
        return self.items(cfg, json.loads(req.body or b"{}"))

    def items(self, cfg, data: dict) -> list:
        """Messages and receipts for THIS page / Instagram account, from an already-verified payload."""
        if data.get("object") != self.obj:
            return []
        me = cfg.get(self.own_id_field)
        out: list = []
        for entry in data.get("entry", []):
            if me and entry.get("id") not in (None, me):
                continue                      # another page / account on the same app
            for ev in entry.get("messaging", []):
                sender = (ev.get("sender") or {}).get("id")
                msg = ev.get("message")
                ref = _referral(ev)
                if msg and not msg.get("is_echo") and sender != me:
                    media = [{"type": a.get("type"), "url": (a.get("payload") or {}).get("url")} for a in msg.get("attachments", [])]
                    out.append(Inbound(address=sender, body=msg.get("text", ""), provider_id=msg.get("mid"),
                                       media=media, at=_ts(ev.get("timestamp")), ref=ref))
                elif ref and not msg and sender and sender != me:      # a referral on its own (the chat was already open)
                    out.append(Inbound(address=sender, at=_ts(ev.get("timestamp")), ref=ref))
                for mid in (ev.get("delivery") or {}).get("mids", []):
                    out.append(StatusUpdate(provider_id=mid, status="delivered"))
                if ev.get("read") and ev["read"].get("mid"):
                    out.append(StatusUpdate(provider_id=ev["read"]["mid"], status="read"))
        return out


@register
class Messenger(_MessengerLike):
    channel = "messenger"
    obj = "page"
    own_id_field = "page_id"
    needs = ("page_id", "page_token", "app_secret", "verify_token")


@register
class Instagram(_MessengerLike):
    channel = "instagram"
    obj = "instagram"
    own_id_field = "ig_user_id"
    needs = ("ig_user_id", "page_token", "app_secret", "verify_token")
