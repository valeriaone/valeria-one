"""The phone agent (v0.18): calls to the business's OWN Twilio number answered with the business's OWN AI key.

Dormant until both are there: a Twilio number connected as the SMS channel (calls already come to Valeria One for
the missed-call text-back, services/calls.py) and an AI key (Settings › AI) — and the owner switched it on. Off,
calls work exactly as before (ring the owner, text back if missed).

A call, turn by turn (Twilio Programmable Voice, <Gather input="speech">, every webhook signature-checked):
  greet (in the business's language; "this call is recorded" first, when the owner turned recording on)
  → listen → understand (the AI turns the caller's words into an intent: price, hours, where, book, message,
    transfer, a question, goodbye)
  → answer ONLY from the business's own facts: prices, hours and the address come from what the owner entered
    (services/answers.py, the same facts as the 24/7 chat answers); any other question goes to the AI with the facts
    and must come back "known" — and every number in its answer must be in the facts — or it isn't said
  → book (the booking page's own rules and slots, services/booking.py), take a message, or put the caller through
    to the owner's phone (no answer there: the missed-call text-back, as always)
  → anything it doesn't know: "I'll have someone call you back" + a task for the team + a word to the owner.
Each call is kept (calls): what was said, what came of it, a short summary — on the customer's page.

The engine is split in two so a realtime voice engine can plug in later (docs/ARCHITECTURE.md, "Phone agent"):
the BRAIN (Brain.greet / Brain.turn: facts, booking, messages, callbacks — no audio, no Twilio) and the
TRANSPORT (GatherTransport: the Brain's Reply as TwiML). A streaming transport (Twilio <Connect><Stream> to a
realtime speech model) would call the same Brain with the same Reply.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

from babel.dates import format_datetime
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, crypto, db, packs, phones
from . import Invalid, NotFound, log

logger = logging.getLogger("valeria_one.voice_agent")
VA, CL = db.voice_agents, db.calls
MAX_TURNS = 14
LIVE_MINUTES = 30                                            # a call with no news for this long is over
INTENTS = ("price", "hours", "location", "book", "message", "transfer", "question", "bye", "yes", "no")
DEFAULTS = {"enabled": False, "greeting": "", "can_book": True, "can_message": True, "transfer_to": None, "record": False}


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


# ---------------------------------------------------------------- settings and readiness
def settings(conn: Connection, business_id: UUID) -> dict:
    r = conn.execute(select(VA).where(VA.c.business_id == business_id)).mappings().first()
    return {**DEFAULTS, **(dict(r) if r else {}), "business_id": business_id}


def save(conn: Connection, biz: dict, actor: str, **fields) -> dict:
    vals = {}
    for k in ("enabled", "can_book", "can_message", "record"):
        if k in fields and fields[k] is not None:
            vals[k] = bool(fields[k])
    if "greeting" in fields and fields["greeting"] is not None:
        vals["greeting"] = " ".join(str(fields["greeting"]).split())[:400]
    if "transfer_to" in fields and fields["transfer_to"] is not None:
        raw = str(fields["transfer_to"]).strip()
        if raw:
            try:
                vals["transfer_to"] = phones.e164(raw, packs.country(biz["country"]))
            except phones.BadPhone:
                raise Invalid("check the phone number", "transfer_to") from None
        else:
            vals["transfer_to"] = None
    if vals.get("enabled"):
        why = missing(conn, biz)
        if why:
            raise Invalid(_t(f"vc_need_{why[0]}", biz["language"]), "enabled")
    if vals:
        conn.execute(pg_insert(VA).values(business_id=biz["id"], **vals)
                     .on_conflict_do_update(index_elements=[VA.c.business_id], set_={**vals, "updated_at": func.now()}))
        log(conn, biz["id"], "voice_agent.updated", actor, biz["id"], {k: v for k, v in vals.items() if k != "greeting"})
    return settings(conn, biz["id"])


def number_account(conn: Connection, business_id: UUID):
    """The business's own Twilio number (the SMS channel with a phone number): calls come to it."""
    A = db.channel_accounts
    for r in conn.execute(select(A).where(A.c.business_id == business_id, A.c.channel == "sms", A.c.active)
                          .order_by(A.c.created_at)).mappings():
        if crypto.open_(r["config"]).get("from_number"):
            return r
    return None


def missing(conn: Connection, biz: dict) -> list[str]:
    """What it still needs, in order: number | ai. [] = it can work."""
    from . import ai as AI
    out = []
    if not number_account(conn, biz["id"]):
        out.append("number")
    if not AI.own_key(conn, biz):
        out.append("ai")
    return out


def active(conn: Connection, biz: dict, acc) -> bool:
    """Answering calls on this number right now? Only when switched on and both accounts are the business's own."""
    s = settings(conn, biz["id"])
    if not s["enabled"] or biz.get("suspended_at") or not blocks.on(biz, "ai"):
        return False
    from . import ai as AI
    return bool(AI.own_key(conn, biz)) and acc["channel"] == "sms" and bool(crypto.open_(acc["config"]).get("from_number"))


# ---------------------------------------------------------------- what the brain says
@dataclass
class Reply:
    say: str = ""
    listen: bool = True                                      # wait for the caller's next words
    hangup: bool = False
    transfer: str | None = None                              # put through to this number
    notes: list[str] = field(default_factory=list)


def _ai(biz: dict, system: str, user: str, http=None) -> str:
    """One question to the business's own AI (tests replace this)."""
    from . import ai as AI
    with db.tx() as conn:
        creds = AI.own_key(conn, biz)
    if not creds:
        raise AI.AIError("no AI key")
    prov, key, model = creds
    return AI._call(prov, key, model, system, [("user", user)], AI._http(http), max_tokens=300)


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


def _facts_text(conn: Connection, biz: dict) -> str:
    from . import ai as AI
    s = AI.settings(conn, biz["id"])
    notes = (s or {}).get("instructions") or ""
    return AI._facts(conn, biz) + ("\nNOTES FROM THE OWNER:\n" + notes if notes.strip() else "")


def grounded(answer: str, facts: str) -> bool:
    """Every number in the answer appears in the facts (prices, times, years): never an invented figure."""
    norm = facts.replace(",", "").replace(".", "")
    for n in re.findall(r"\d[\d.,:]*", answer or ""):
        core = n.rstrip(".,:").replace(",", "").replace(".", "")
        if core and core not in norm:
            return False
    return True


class Brain:
    """What to say next. No audio, no Twilio: a realtime engine would use this as it is."""

    def __init__(self, biz: dict, agent: dict, http=None):
        self.biz, self.agent, self.http = biz, agent, http
        self.lang = _lang(biz)

    def w(self, key: str, **kw) -> str:
        s = _t(key, self.lang)
        return s.format(**kw) if kw else s

    def greet(self, call: dict) -> Reply:
        say = self.agent.get("greeting") or self.w("vc_greet_default", business=self.biz["name"])
        if self.agent.get("record"):
            say = self.w("vc_recorded") + " " + say
        return Reply(say=say)

    def turn(self, conn: Connection, call: dict, heard: str, now: datetime) -> Reply:
        if call.get("state") is None:
            call["state"] = {}
        st = call["state"]                                   # the same dict: what the brain notes is saved with the call
        heard = (heard or "").strip()
        if len(call["turns"] or []) >= MAX_TURNS * 2:
            return self._bye("vc_l_long")
        if not heard:
            st["quiet"] = st.get("quiet", 0) + 1
            if st["quiet"] >= 2:
                return self._bye("vc_l_silence")
            return Reply(say=self.w("vc_didnt_hear"))
        st["quiet"] = 0
        mode = st.get("mode")
        if mode == "message":
            return self._take_message(conn, call, heard, now)
        if mode in ("book_service", "book_slot"):
            return self._booking(conn, call, heard, now)
        intent = self._understand(heard)
        kind = intent.get("intent")
        st.setdefault("asked", []).append(kind or "?")
        if kind in ("price", "hours", "location"):
            from . import answers as AN
            text = AN._facts(conn, self.biz, kind, self.lang)
            if not text:
                return self._callback(conn, call, heard, now)
            return Reply(say=_speakable(text) + " " + self.w("vc_anything_else"), notes=[f"vc_l_{kind}"])
        if kind == "book":
            if not self.agent.get("can_book") or not blocks.on(self.biz, "calendar"):
                return self._offer_message(conn, call)
            st["mode"] = "book_service"
            return self._booking(conn, call, intent.get("service") or heard, now)
        if kind == "message":
            return self._offer_message(conn, call)
        if kind == "transfer":
            if self.agent.get("transfer_to"):
                st["mode"] = "transfer"
                return Reply(say=self.w("vc_transfer"), listen=False, transfer=self.agent["transfer_to"], notes=["vc_l_transfer"])
            return self._callback(conn, call, heard, now)
        if kind == "bye" or (kind == "no" and st.get("asked_else")):
            return self._bye("vc_l_bye")
        if kind == "yes" and st.get("asked_else"):
            st["asked_else"] = False
            return Reply(say=self.w("vc_go_ahead"))
        return self._question(conn, call, heard, now)

    # -- understanding
    def _understand(self, heard: str) -> dict:
        system = ("You route phone calls for a small business. Read what the caller said and answer with JSON only: "
                  '{"intent": one of ' + ", ".join(INTENTS) + ', "service": the service they name or "", '
                  '"question": their question in a few words}. price = asks what something costs; hours = when open; '
                  "location = where / address; book = wants an appointment; message = wants to leave a message; transfer "
                  "= wants a person / the owner; question = anything else about the business; bye = done; yes/no = a plain "
                  "yes or no.")
        try:
            got = _json(_ai(self.biz, system, heard, self.http))
        except Exception as e:                                # noqa: BLE001 — the AI being down is a callback, never silence
            logger.info("voice agent: AI could not understand (%s)", type(e).__name__)
            return {"intent": "question", "question": heard}
        if got.get("intent") not in INTENTS:
            got["intent"] = "question"
        return got

    def _question(self, conn: Connection, call: dict, heard: str, now: datetime) -> Reply:
        facts = _facts_text(conn, self.biz)
        system = ("You answer a caller's question for a small business, by phone, in at most two short sentences, in "
                  f"{_lang_name(self.lang)}. Use ONLY the facts below. If the facts don't answer it, say so. "
                  'Answer with JSON only: {"known": true|false, "answer": "..."}.\n\nFACTS:\n' + facts)
        try:
            got = _json(_ai(self.biz, system, heard, self.http))
        except Exception:                                     # noqa: BLE001
            got = {}
        ans = str(got.get("answer") or "").strip()
        if got.get("known") is True and ans and grounded(ans, facts):
            return Reply(say=ans + " " + self.w("vc_anything_else"), notes=["vc_l_answered"])
        return self._callback(conn, call, heard, now)

    # -- not known: a person calls back
    def _callback(self, conn: Connection, call: dict, heard: str, now: datetime) -> Reply:
        from . import owner as OW
        from . import projects as PR
        st = call["state"]
        who = call["caller"] or "—"
        title = self.w("vc_task_callback", who=who, what=heard[:160])
        task = PR.add_task(conn, self.biz, "voice_agent", {"title": title[:300], "contact_id": call["contact_id"],
                                                           "due_at": now + timedelta(hours=2)})
        call["task_id"] = task["id"]
        OW.ask(conn, self.biz, "notice", title, {"text": title}, contact_id=call["contact_id"], by="voice_agent",
               dedupe=f"call:{call['id']}:{len(call['turns'] or [])}", owner_says_go=True, now=now)
        st["callback"] = True
        st["asked_else"] = True
        return Reply(say=self.w("vc_callback") + " " + self.w("vc_anything_else"), notes=["vc_l_callback"])

    def _offer_message(self, conn: Connection, call: dict) -> Reply:
        if not self.agent.get("can_message"):
            return Reply(say=self.w("vc_no_message") + " " + self.w("vc_anything_else"))
        call["state"]["mode"] = "message"
        return Reply(say=self.w("vc_message_go"))

    def _take_message(self, conn: Connection, call: dict, heard: str, now: datetime) -> Reply:
        from . import owner as OW
        from . import projects as PR
        title = self.w("vc_task_message", who=call["caller"] or "—", what=heard[:200])
        task = PR.add_task(conn, self.biz, "voice_agent", {"title": title[:300], "contact_id": call["contact_id"],
                                                           "due_at": now + timedelta(hours=4)})
        call["task_id"] = task["id"]
        OW.ask(conn, self.biz, "notice", title, {"text": title}, contact_id=call["contact_id"], by="voice_agent",
               dedupe=f"call:{call['id']}:msg", owner_says_go=True, now=now)
        call["state"]["mode"] = None
        call["state"]["message"] = heard[:500]
        call["state"]["asked_else"] = True
        return Reply(say=self.w("vc_message_taken") + " " + self.w("vc_anything_else"), notes=["vc_l_message"])

    # -- booking, one question at a time
    def _booking(self, conn: Connection, call: dict, heard: str, now: datetime) -> Reply:
        from . import booking as BK
        st = call["state"]
        svcs = BK.services(conn, self.biz["id"], active_only=True)
        if not svcs or not self.biz.get("hours"):
            st["mode"] = None
            return self._callback(conn, call, heard, now)
        if st.get("mode") == "book_slot":
            pick = _pick(heard, len(st.get("slots") or []))
            if pick is None:
                ans = self._understand(heard)
                if ans.get("intent") == "no":
                    st["mode"] = None
                    return self._callback(conn, call, heard, now)
                pick = _pick(str(ans.get("question") or ""), len(st.get("slots") or []))
            if pick is None:
                return Reply(say=self.w("vc_pick_again", a=st["said"][0], b=st["said"][-1]))
            start = datetime.fromisoformat(st["slots"][pick])
            name = (conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == call["contact_id"])).scalar()
                    if call["contact_id"] else "") or self.w("vc_caller")
            try:
                res = BK.book(conn, self.biz, service_id=UUID(st["service_id"]), starts_at=start, name=name,
                              phone=call["caller"], notes=self.w("vc_booked_note"), now=now)
            except Invalid:
                st["mode"] = None
                return self._callback(conn, call, heard, now)
            call["appointment_id"] = res["appointment"]["id"]
            call["contact_id"] = res["contact"]["id"]
            st["mode"] = None
            st["booked"] = st["said"][pick]
            st["asked_else"] = True
            return Reply(say=self.w("vc_booked", when=st["said"][pick]) + " " + self.w("vc_anything_else"), notes=["vc_l_booked"])
        svc = _match_service(svcs, heard)
        if not svc:
            if len(svcs) == 1:
                svc = svcs[0]
            else:
                names = ", ".join(s["name"] for s in svcs[:6])
                st["mode"] = "book_service"
                st["svc_tries"] = st.get("svc_tries", 0) + 1
                if st["svc_tries"] > 2:
                    st["mode"] = None
                    return self._callback(conn, call, heard, now)
                return Reply(say=self.w("vc_which_service", names=names))
        found = []
        for d in BK.open_days(conn, self.biz, svc["id"], now, limit=3):
            for s in BK.slots(conn, self.biz, svc["id"], d, now):
                found.append(s)
                if len(found) >= 2:
                    break
            if len(found) >= 2:
                break
        if not found:
            st["mode"] = None
            return self._callback(conn, call, heard, now)
        loc = _locale(self.biz, self.lang)
        tz = ZoneInfo(self.biz["timezone"])
        said = [format_datetime(s.astimezone(tz), "EEEE d MMMM, HH:mm", locale=loc) for s in found]
        st.update(mode="book_slot", service_id=str(svc["id"]), slots=[s.isoformat() for s in found], said=said)
        if len(said) == 1:
            return Reply(say=self.w("vc_one_slot", service=svc["name"], a=said[0]))
        return Reply(say=self.w("vc_two_slots", service=svc["name"], a=said[0], b=said[1]))

    def _bye(self, note: str) -> Reply:
        return Reply(say=self.w("vc_bye", business=self.biz["name"]), listen=False, hangup=True, notes=[note])


_FIRST = ("1", "one", "first", "uno", "primero", "primera", "um", "primeiro", "premier", "première", "erste", "ersten", "una",
          "una po", "satu", "pertama", "पहला", "पहले", "الأول", "الاول", "一", "最初", "第一", "前の")
_SECOND = ("2", "two", "second", "dos", "segundo", "segunda", "dois", "deuxième", "second", "zweite", "zweiten", "dalawa",
           "pangalawa", "dua", "kedua", "दूसरा", "दूसरे", "الثاني", "二", "二番目", "第二", "後の")


def _pick(heard: str, n: int) -> int | None:
    low = f" {(heard or '').lower()} "
    if n >= 2 and any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low) for w in _SECOND):
        return 1
    if n >= 1 and any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low) for w in _FIRST):
        return 0
    if n == 1 and re.search(r"\b(yes|sí|si|sim|oui|ja|oo|opo|ya|हाँ|نعم|はい|是|好)\b", low):
        return 0
    return None


def _match_service(svcs: list[dict], heard: str) -> dict | None:
    low = (heard or "").lower()
    best = [s for s in svcs if s["name"].lower() in low or any(w in low for w in s["name"].lower().split() if len(w) > 3)]
    return best[0] if best else None


def _speakable(text: str) -> str:
    """Chat answers into phone sentences: no bullets, no links."""
    out = re.sub(r"https?://\S+", "", text or "")
    out = out.replace("·", "").replace("\n", ". ")
    return re.sub(r"\s{2,}", " ", re.sub(r"(\.\s*){2,}", ". ", out)).strip(" .") + "."


def _lang(biz: dict) -> str:
    from ..web.i18n import ui_lang
    return ui_lang(biz.get("language"))


def _lang_name(lang: str) -> str:
    from . import ai as AI
    return AI._LANG.get(lang, "the caller's language")


def _locale(biz: dict, lang: str) -> str:
    pack = packs.country(biz["country"])
    return pack.locale if pack.locale.split("_")[0] == lang else lang


# ---------------------------------------------------------------- the transport: TwiML, turn by turn
class GatherTransport:
    """The Brain's Reply as TwiML for Twilio Programmable Voice (speech recognition by Twilio, one turn per webhook)."""

    def __init__(self, base_url: str, lang: str, caller: str = ""):
        from .calls import _VOICE
        self.base, self.voice, self.caller = base_url, _VOICE.get(lang, "en-US"), caller

    def render(self, r: Reply) -> str:
        say = f'<Say language="{self.voice}">{escape(r.say)}</Say>' if r.say else ""
        if r.transfer:
            done = self.base + "/voice/done"
            body = (say + f'<Dial timeout="20" action={quoteattr(done)} method="POST" callerId={quoteattr(self.caller)}>'
                    f"<Number>{escape(r.transfer)}</Number></Dial>")
        elif r.hangup or not r.listen:
            body = say + "<Hangup/>"
        else:
            action = self.base + "/voice/agent"
            body = (f'<Gather input="speech" action={quoteattr(action)} method="POST" language="{self.voice}" '
                    f'speechTimeout="auto" actionOnEmptyResult="true">{say}</Gather>'
                    f"<Redirect method=\"POST\">{escape(action)}</Redirect>")
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


# ---------------------------------------------------------------- the webhooks (called from services/calls.py and the API)
def _base(req_url: str) -> str:
    """https://…/hooks/<hook_id>/voice[/agent] -> https://…/hooks/<hook_id>"""
    u = req_url.split("?")[0]
    return u[: u.index("/voice")] if "/voice" in u else u


def _record(cfg: dict, call_sid: str, base: str, http=None) -> None:
    """Start recording the whole call (Twilio's Recordings API), only after the caller heard it is recorded."""
    import httpx
    from ..channels.twilio_sms import API
    h = http or httpx.Client(timeout=10)
    try:
        h.post(f"{API}/Accounts/{cfg['account_sid']}/Calls/{call_sid}/Recordings.json", auth=(cfg["account_sid"], cfg["auth_token"]),
               data={"RecordingStatusCallback": base + "/voice/agent/recording", "RecordingStatusCallbackMethod": "POST"})
    except Exception:                                         # noqa: BLE001 — no recording is better than a dropped call
        logger.info("voice agent: could not start the recording")


def answer(acc, cfg: dict, biz: dict, p: dict, req_url: str, http=None, now: datetime | None = None) -> str:
    """A new call: the greeting, then listen."""
    from . import contacts as C
    now = now or datetime.now(timezone.utc)
    sid = (p.get("CallSid") or "")[:64]
    caller = _e164(p.get("From"), biz)
    with db.tx() as conn:
        agent = settings(conn, biz["id"])
        contact_id = None
        if caller:
            contact, _ = C.find_or_create_by_identity(conn, biz, "sms", caller, None, "phone call")
            contact_id = contact["id"]
        conn.execute(pg_insert(CL).values(business_id=biz["id"], channel_account_id=acc["id"], contact_id=contact_id,
                                          call_sid=sid or f"nosid-{now.timestamp()}", caller=caller or "")
                     .on_conflict_do_nothing(constraint="uq_call_sid"))
        call = dict(conn.execute(select(CL).where(CL.c.business_id == biz["id"], CL.c.call_sid == (sid or f"nosid-{now.timestamp()}")))
                    .mappings().one())
        reply = Brain(biz, agent, http).greet(call)
        _say(conn, call, "agent", reply.say, now)
        log(conn, biz["id"], "call.agent", "webhook:sms", contact_id, {"call_id": str(call["id"])})
    base = _base(req_url)
    if agent["record"] and sid:
        _record(cfg, sid, base, http)
    return GatherTransport(base, _lang(biz), caller or "").render(reply)


def turn(acc, cfg: dict, biz: dict, p: dict, req_url: str, http=None, now: datetime | None = None) -> str:
    """The caller spoke (or stayed quiet): the next thing to say."""
    now = now or datetime.now(timezone.utc)
    sid = (p.get("CallSid") or "")[:64]
    heard = (p.get("SpeechResult") or "").strip()[:1000]
    base = _base(req_url)
    with db.tx() as conn:
        call = conn.execute(select(CL).where(CL.c.business_id == biz["id"], CL.c.call_sid == sid).with_for_update()).mappings().first()
        if not call:
            return GatherTransport(base, _lang(biz)).render(Reply(say="", hangup=True))
        call = dict(call)
        call["state"] = dict(call["state"] or {})
        agent = settings(conn, biz["id"])
        if heard:
            _say(conn, call, "caller", heard, now)
        reply = Brain(biz, agent, http).turn(conn, call, heard, now)
        _say(conn, call, "agent", reply.say, now)
        notes = call["state"].setdefault("notes", [])
        notes += [n for n in reply.notes if n not in notes]
        vals = {"state": call["state"], "contact_id": call["contact_id"], "appointment_id": call.get("appointment_id"),
                "task_id": call.get("task_id")}
        conn.execute(update(CL).where(CL.c.id == call["id"]).values(**vals))
        if reply.hangup or reply.transfer:
            finish(conn, biz, {**call, **vals}, now)
    return GatherTransport(base, _lang(biz), call["caller"]).render(reply)


def recording(acc, biz: dict, p: dict) -> None:
    with db.tx() as conn:
        conn.execute(update(CL).where(CL.c.business_id == biz["id"], CL.c.call_sid == (p.get("CallSid") or "")[:64])
                     .values(recording_url=(p.get("RecordingUrl") or "")[:500] or None))


def _say(conn: Connection, call: dict, who: str, text: str, now: datetime) -> None:
    if not text:
        return
    turns = list(call["turns"] or []) + [{"who": who, "text": text[:1000], "at": now.isoformat()}]
    call["turns"] = turns
    conn.execute(update(CL).where(CL.c.id == call["id"]).values(turns=turns))


def _e164(raw: str | None, biz: dict) -> str | None:
    if not raw:
        return None
    try:
        return phones.e164(raw, packs.country(biz["country"]))
    except phones.BadPhone:
        return None


def summary(biz: dict, call: dict) -> tuple[str, str]:
    """-> (outcome, a short summary in the owner's language). From what happened, never made up."""
    st = call["state"] or {}
    notes = st.get("notes") or []
    lang = _lang(biz)
    parts = []
    for key in ("vc_l_price", "vc_l_hours", "vc_l_location", "vc_l_answered"):
        if key in notes:
            parts.append(_t(key + "_s", lang))
    outcome = "answered" if parts else "hangup"
    if st.get("booked"):
        parts.append(_t("vc_s_booked", lang).format(when=st["booked"]))
        outcome = "booked"
    if st.get("message"):
        parts.append(_t("vc_s_message", lang).format(what=st["message"][:200]))
        outcome = "message"
    if st.get("callback"):
        parts.append(_t("vc_s_callback", lang))
        outcome = "callback" if outcome in ("answered", "hangup") else outcome
    if "vc_l_transfer" in notes:
        parts.append(_t("vc_s_transfer", lang))
        outcome = "transfer"
    return outcome, " ".join(parts) or _t("vc_s_nothing", lang)


def finish(conn: Connection, biz: dict, call: dict, now: datetime) -> None:
    if conn.execute(select(CL.c.status).where(CL.c.id == call["id"])).scalar() == "done":
        return
    outcome, text = summary(biz, call)
    conn.execute(update(CL).where(CL.c.id == call["id"]).values(status="done", outcome=outcome, summary=text, ended_at=now))
    log(conn, biz["id"], "call.done", "voice_agent", call["contact_id"], {"call_id": str(call["id"]), "outcome": outcome})


def sweep(now: datetime | None = None) -> int:
    """The job: calls that went quiet (the caller hung up) are closed with their summary."""
    now = now or datetime.now(timezone.utc)
    n = 0
    with db.tx() as conn:
        rows = conn.execute(select(CL).where(CL.c.status == "live", CL.c.started_at < now - timedelta(minutes=LIVE_MINUTES))
                            .limit(200).with_for_update(skip_locked=True)).mappings().all()
        for r in rows:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == r["business_id"])).mappings().one())
            finish(conn, biz, dict(r), now)
            n += 1
    return n


def calls_of(conn: Connection, business_id: UUID, *, contact_id: UUID | None = None, limit: int = 30) -> list[dict]:
    C = db.contacts
    q = select(CL, C.c.name.label("contact_name")).outerjoin(C, C.c.id == CL.c.contact_id).where(CL.c.business_id == business_id)
    if contact_id:
        q = q.where(CL.c.contact_id == contact_id)
    return [dict(r) for r in conn.execute(q.order_by(CL.c.started_at.desc()).limit(limit)).mappings()]


def get_call(conn: Connection, business_id: UUID, call_id: UUID) -> dict:
    r = conn.execute(select(CL).where(CL.c.business_id == business_id, CL.c.id == call_id)).mappings().first()
    if not r:
        raise NotFound("call")
    return dict(r)
