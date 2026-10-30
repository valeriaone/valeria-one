"""Ready setups ("snapshots", v0.18): one tap sets a business up for its trade — and an owner can copy their own setup.

A setup is a bundle: the owner's own fields, ready-made recipes and workflows, a drip sequence, a campaign draft, a
form, saved replies, services (no prices: the owner fills them), opening hours and the website's headline and
about. The shipped ones live in packs/snapshots/<key>.yaml; every text in them is a locale key (sn_…), said in the
business's own language when it is applied — keys + locale strings, not eleven copies.

Applying MERGES and never overwrites: a field, form, reply, service, sequence, campaign or workflow with the same name
is left as it is (skipped); hours, the website's words and the stage names are only filled where the business has
none. The preview is the very same apply, rolled back (so it can't disagree with what happens). Everything it added is
written down (snapshot_applies) so it can be undone within UNDO_HOURS — and undo only removes what is still exactly as
the setup left it (a form that has received answers, a campaign that was sent, a field the owner filled in… stay).

Export: the owner's own setup as the same kind of bundle, in plain words (no locale keys) and WITHOUT customer data —
no customers, messages, deals, payments, notes, phone numbers or addresses; references between parts travel by name
(a field by its label, a form or a sequence by its name) and team members are left out. It can be applied to
another business the same person owns (an agency setting up its customers: the groundwork for helpers).
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db, packs
from . import Invalid, NotFound, log

SA = db.snapshot_applies
HERE = Path(__file__).resolve().parent.parent / "packs" / "snapshots"
UNDO_HOURS = 24
FORMAT = "valeria-one-setup"
PARTS = ("fields", "recipes", "workflows", "sequences", "campaigns", "forms", "replies", "services", "hours", "site", "words")


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


@lru_cache(maxsize=1)
def shipped() -> dict[str, dict]:
    out = {}
    for f in sorted(HERE.glob("*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8"))
        if d.get("key") != f.stem:
            raise ValueError(f"{f.name}: key must match the file name")
        for ind in d.get("industries") or []:
            if ind not in packs.industries():
                raise ValueError(f"{f.name}: unknown industry {ind}")
        out[d["key"]] = d
    return out


def for_business(biz: dict) -> list[dict]:
    """The setups, the business's own trade first."""
    rows = list(shipped().values())
    return sorted(rows, key=lambda d: (biz["industry"] not in (d.get("industries") or []), d["key"] == "generic", d["key"]))


def keys_in(bundle: dict) -> set[str]:
    """Every locale key a shipped setup uses (tests: all 11 languages have them)."""
    out: set[str] = set()

    def walk(v):
        if isinstance(v, str) and v.startswith("sn_"):
            out.add(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(bundle)
    return out


def resolve(bundle: dict, lang: str) -> dict:
    """A shipped setup in words: every sn_… key said in this language."""
    def say(v):
        if isinstance(v, str) and v.startswith("sn_"):
            return _t(v, lang)
        if isinstance(v, dict):
            return {k: say(x) for k, x in v.items()}
        if isinstance(v, list):
            return [say(x) for x in v]
        return v
    return say(copy.deepcopy(bundle))


# ---------------------------------------------------------------- applying (merge, never overwrite)
def _fields_by_label(conn: Connection, business_id: UUID) -> dict[str, dict]:
    from . import fields as CF
    return {f["label"].lower(): f for f in CF.list_(conn, business_id, "contact")}


def _map_refs(v, fields: dict[str, dict], forms: dict[str, UUID], seqs: dict[str, UUID]):
    """'@field:Label' / '@form:Name' / '@seq:Name' (an export's references) -> this business's own ids."""
    if isinstance(v, str) and v.startswith("@"):
        kind, _, name = v[1:].partition(":")
        if kind == "field":
            f = fields.get(name.lower())
            return f["key"] if f else None
        if kind == "form":
            return str(forms[name.lower()]) if name.lower() in forms else None
        if kind == "seq":
            return str(seqs[name.lower()]) if name.lower() in seqs else None
    if isinstance(v, dict):
        return {k: _map_refs(x, fields, forms, seqs) for k, x in v.items()}
    if isinstance(v, list):
        return [_map_refs(x, fields, forms, seqs) for x in v]
    return v


def apply(conn: Connection, biz: dict, actor: str, bundle: dict, name: str) -> dict:
    """Merge a setup (in words) into the business. -> {"added": {part: [names]}, "skipped": {part: [names]}, "id"}."""
    from . import booking as BK
    from . import campaigns as CG
    from . import fields as CF
    from . import forms as FM
    from . import recipes as RC
    from . import replies as SR
    from . import sequences as SQ
    from . import workflows as WF
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    added: dict[str, list] = {p: [] for p in PARTS}
    skipped: dict[str, list] = {p: [] for p in PARTS}
    ids: dict[str, list] = {}

    def keep(part: str, label: str, rid=None):
        added[part].append(label)
        if rid is not None:
            ids.setdefault(part, []).append(str(rid))

    # fields first: the rest may point at them
    have = _fields_by_label(conn, biz["id"])
    for f in bundle.get("fields") or []:
        label = " ".join(str(f.get("label") or "").split())[:60]
        if not label or label.lower() in have:
            skipped["fields"].append(label)
            continue
        try:
            with conn.begin_nested():
                row = CF.create(conn, biz, actor, obj=f.get("object") or "contact", label=label, kind=f.get("kind") or "text",
                                options=f.get("options"), show_in_list=bool(f.get("show_in_list")))
            keep("fields", label, row["id"])
        except Invalid:
            skipped["fields"].append(label)
    fields = _fields_by_label(conn, biz["id"])

    for fm in bundle.get("forms") or []:
        nm = str(fm.get("name") or "").strip()
        if not nm or any(x["name"].lower() == nm.lower() for x in FM.list_(conn, biz["id"])):
            skipped["forms"].append(nm)
            continue
        keys = []
        for k in fm.get("fields") or []:
            k = _map_refs(k, fields, {}, {}) if isinstance(k, str) and k.startswith("@") else k
            if k and (k in FM.BASICS or str(k) in {f["key"] for f in fields.values()}):
                keys.append({"key": k if k in FM.BASICS else f"cf:{k}", "required": k in ("name", "phone")})
        try:
            with conn.begin_nested():
                row = FM.save(conn, biz, actor, {"name": nm, "intro": fm.get("intro") or "", "thanks": fm.get("thanks") or "",
                                                 "fields": keys, "tag": fm.get("tag"), "active": True})
            keep("forms", nm, row["id"])
        except Invalid:
            skipped["forms"].append(nm)
    forms = {x["name"].lower(): x["id"] for x in FM.list_(conn, biz["id"])}

    for sq in bundle.get("sequences") or []:
        nm = str(sq.get("name") or "").strip()
        if not nm or any(x["name"].lower() == nm.lower() for x in SQ.list_(conn, biz["id"])):
            skipped["sequences"].append(nm)
            continue
        data = _map_refs(copy.deepcopy(sq), fields, forms, {})
        steps = [{**s, "wait": int(s.get("wait") or 0)} for s in data.get("steps") or []]
        try:
            with conn.begin_nested():
                row = SQ.save(conn, biz, actor, {**data, "steps": steps, "active": False})
            keep("sequences", nm, row["id"])
        except Invalid:
            skipped["sequences"].append(nm)
    seqs = {x["name"].lower(): x["id"] for x in SQ.list_(conn, biz["id"])}

    mine = {r["preset"]: r for r in RC.list_(conn, biz["id"]) if r["preset"]}
    for rc in bundle.get("recipes") or []:
        if isinstance(rc, str):                              # a ready-made recipe of the gallery, by its key
            p = next((x for x in RC.presets(conn, biz, biz["language"]) if x["key"] == rc), None)
            if not p or p["needs"] or rc in mine:
                skipped["recipes"].append(p["sentence"] if p else rc)
                continue
            try:
                with conn.begin_nested():
                    row = RC.save(conn, biz, actor, {"trigger": p["trigger"], "trigger_arg": p["trigger_arg"], "condition": {},
                                                     "actions": p["actions"], "ask_first": True, "name": ""}, preset=rc)
                keep("recipes", p["sentence"], row["id"])
            except Invalid:
                skipped["recipes"].append(p["sentence"])
            continue
        data = _map_refs(copy.deepcopy(rc), fields, forms, seqs)
        data["actions"] = [a for a in data.get("actions") or [] if a.get("do") != "assign" and None not in a.values()]
        nm = data.get("name") or ""
        same = [r for r in RC.list_(conn, biz["id"]) if nm and r["name"].lower() == nm.lower()]
        if same:
            skipped["recipes"].append(nm)
            continue
        try:
            with conn.begin_nested():
                row = RC.save(conn, biz, actor, {**data, "ask_first": True})
                conn.execute(update(db.recipes).where(db.recipes.c.id == row["id"]).values(active=False))
            keep("recipes", nm or RC.describe(conn, biz, row, biz["language"]), row["id"])
        except (Invalid, NotFound, ValueError):
            skipped["recipes"].append(nm or "—")

    have_wf = WF.list_(conn, biz["id"])
    for wf in bundle.get("workflows") or []:
        if isinstance(wf, str):
            p = next((x for x in WF.templates(conn, biz) if x["key"] == wf), None)
            if not p or p["needs"] or any(x["template"] == wf for x in have_wf):
                skipped["workflows"].append(p["name"] if p else wf)
                continue
            try:
                with conn.begin_nested():
                    row = WF.from_template(conn, biz, actor, wf)
                keep("workflows", p["name"], row["id"])
            except (Invalid, NotFound):
                skipped["workflows"].append(p["name"])
            continue
        data = _map_refs(copy.deepcopy(wf), fields, forms, seqs)
        nm = data.get("name") or ""
        if nm and any(x["name"].lower() == nm.lower() for x in have_wf):
            skipped["workflows"].append(nm)
            continue
        try:
            with conn.begin_nested():
                row = WF.save(conn, biz, actor, {**data, "active": False})
            keep("workflows", nm or "—", row["id"])
        except (Invalid, NotFound, ValueError, KeyError, TypeError):
            skipped["workflows"].append(nm or "—")

    have_cp = {c["name"].lower() for c in CG.list_(conn, biz["id"])}
    for cp in bundle.get("campaigns") or []:
        nm = str(cp.get("name") or "").strip()
        if not nm or nm.lower() in have_cp or cp.get("channel") not in CG.CHANNELS:
            skipped["campaigns"].append(nm)
            continue
        row = CG.start(conn, biz, actor, cp["channel"])
        CG.save(conn, biz, actor, row["id"], {"name": nm, "message": cp.get("text") or "", "subject": cp.get("subject"),
                                              "blocks": cp.get("blocks") or []})
        keep("campaigns", nm, row["id"])

    have_sr = {r["title"].lower() for r in SR.list_(conn, biz["id"])}
    for r in bundle.get("replies") or []:
        title = str(r.get("title") or "").strip()[:80]
        if not title or title.lower() in have_sr or not str(r.get("body") or "").strip():
            skipped["replies"].append(title)
            continue
        row = SR.add(conn, biz, actor, title=title, body=r["body"])
        have_sr.add(title.lower())
        keep("replies", title, row["id"])

    have_sv = {s["name"].lower() for s in BK.services(conn, biz["id"], active_only=True)}
    for s in bundle.get("services") or []:
        nm = str(s.get("name") or "").strip()[:120]
        if not nm or nm.lower() in have_sv:
            skipped["services"].append(nm)
            continue
        try:
            with conn.begin_nested():
                row = BK.add_service(conn, biz, actor, name=nm, minutes=int(s.get("minutes") or 30),
                                     price_minor=s.get("price_minor") if isinstance(s.get("price_minor"), int) else None)
            keep("services", nm, row["id"])
        except Invalid:
            skipped["services"].append(nm)

    hours_set = False
    if bundle.get("hours"):
        if biz.get("hours"):
            skipped["hours"].append("hours")
        else:
            BK.save_settings(conn, biz, actor, hours=bundle["hours"])
            hours_set = True
            added["hours"].append("hours")

    site_set = {}
    cur_site = dict(biz.get("site") or {})
    for k in ("headline", "about"):
        v = str((bundle.get("site") or {}).get(k) or "").strip()
        if not v:
            continue
        if cur_site.get(k):
            skipped["site"].append(k)
        else:
            site_set[k] = v
            added["site"].append(k)
    if site_set:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(site={**cur_site, **site_set},
                                                                                         updated_at=func.now()))
    words_set = {}
    own_words = dict(biz.get("words") or {})
    for k, v in (bundle.get("words") or {}).items():
        if k.startswith("stage:") and not packs.industry(biz["industry"]).stage(k[6:]):
            continue
        if own_words.get(k) or not str(v).strip():
            skipped["words"].append(k)
        else:
            words_set[k] = str(v).strip()[:40]
            added["words"].append(k)
    if words_set:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(words={**own_words, **words_set},
                                                                                          updated_at=func.now()))
    row = conn.execute(insert(SA).values(business_id=biz["id"], snapshot=name[:40], added=ids, words=words_set, site=site_set,
                                         hours=hours_set, by=actor).returning(SA.c.id)).scalar()
    log(conn, biz["id"], "snapshot.applied", actor, row, {"snapshot": name, "added": sum(len(v) for v in added.values())})
    return {"id": row, "added": added, "skipped": skipped}


def preview(conn: Connection, biz: dict, actor: str, bundle: dict, name: str) -> dict:
    """Exactly what apply() would do, rolled back."""
    sp = conn.begin_nested()
    try:
        res = apply(conn, biz, actor, bundle, name)
    finally:
        sp.rollback()
    return res


def apply_shipped(conn: Connection, biz: dict, actor: str, key: str, dry: bool = False) -> dict:
    b = shipped().get(key)
    if not b:
        raise NotFound("setup")
    bundle = resolve(b, biz["language"])
    return (preview if dry else apply)(conn, biz, actor, bundle, key)


# ---------------------------------------------------------------- undo (within a day)
def applied(conn: Connection, business_id: UUID, limit: int = 10) -> list[dict]:
    rows = [dict(r) for r in conn.execute(select(SA).where(SA.c.business_id == business_id).order_by(SA.c.applied_at.desc())
                                          .limit(limit)).mappings()]
    now = datetime.now(timezone.utc)
    for r in rows:
        r["can_undo"] = not r["undone_at"] and r["applied_at"] > now - timedelta(hours=UNDO_HOURS)
    return rows


def undo(conn: Connection, biz: dict, actor: str, apply_id: UUID, now: datetime | None = None) -> dict:
    """Remove what that setup added — only what is still as it left it. -> {"removed": n, "kept": [why…]}."""
    from . import replies as SR
    now = now or datetime.now(timezone.utc)
    r = conn.execute(select(SA).where(SA.c.business_id == biz["id"], SA.c.id == apply_id).with_for_update()).mappings().first()
    if not r:
        raise NotFound("setup")
    if r["undone_at"] or r["applied_at"] < now - timedelta(hours=UNDO_HOURS):
        raise Invalid(_t("sn_undo_too_late", biz["language"]), "undo")
    bid = biz["id"]
    removed, kept = 0, []

    def uuids(part):
        return [UUID(x) for x in (r["added"] or {}).get(part) or []]
    for fid in uuids("fields"):
        T = db.contacts
        f = conn.execute(select(db.custom_fields).where(db.custom_fields.c.id == fid)).mappings().first()
        if not f:
            continue
        used = conn.execute(select(T.c.id).where(T.c.business_id == bid, T.c.custom.has_key(f["key"])).limit(1)).first() or \
            conn.execute(select(db.deals.c.id).where(db.deals.c.business_id == bid, db.deals.c.custom.has_key(f["key"])).limit(1)).first()
        if used:
            kept.append(f["label"])
            continue
        conn.execute(db.custom_fields.delete().where(db.custom_fields.c.id == fid))
        removed += 1
    for fid in uuids("forms"):
        f = conn.execute(select(db.forms).where(db.forms.c.id == fid)).mappings().first()
        if f and f["submissions"]:
            kept.append(f["name"])
            continue
        removed += conn.execute(db.forms.delete().where(db.forms.c.business_id == bid, db.forms.c.id == fid)).rowcount
    for wid in uuids("workflows"):
        runs = conn.execute(select(db.workflow_runs.c.id).where(db.workflow_runs.c.workflow_id == wid).limit(1)).first()
        if runs:
            kept.append(conn.execute(select(db.workflows.c.name).where(db.workflows.c.id == wid)).scalar() or "—")
            continue
        removed += conn.execute(db.workflows.delete().where(db.workflows.c.business_id == bid, db.workflows.c.id == wid)).rowcount
    for rid in uuids("recipes"):
        if conn.execute(select(db.recipe_runs.c.id).where(db.recipe_runs.c.recipe_id == rid).limit(1)).first():
            kept.append("recipe")
            continue
        removed += conn.execute(db.recipes.delete().where(db.recipes.c.business_id == bid, db.recipes.c.id == rid)).rowcount
    for sid in uuids("sequences"):
        if conn.execute(select(db.sequence_enrollments.c.id).where(db.sequence_enrollments.c.sequence_id == sid).limit(1)).first():
            kept.append(conn.execute(select(db.sequences.c.name).where(db.sequences.c.id == sid)).scalar() or "—")
            continue
        removed += conn.execute(db.sequences.delete().where(db.sequences.c.business_id == bid, db.sequences.c.id == sid)).rowcount
    for cid in uuids("campaigns"):
        n = conn.execute(db.campaigns.delete().where(db.campaigns.c.business_id == bid, db.campaigns.c.id == cid,
                                                     db.campaigns.c.status == "draft")).rowcount
        removed += n
        if not n:
            kept.append("campaign")
    for rid in uuids("replies"):
        removed += SR.remove(conn, bid, rid)
    for sid in uuids("services"):
        used = conn.execute(select(db.appointments.c.id).where(db.appointments.c.business_id == bid,
                                                               db.appointments.c.title == select(db.booking_services.c.name)
                                                               .where(db.booking_services.c.id == sid).scalar_subquery()).limit(1)).first()
        if used:
            kept.append("service")
            continue
        removed += conn.execute(db.booking_services.delete().where(db.booking_services.c.business_id == bid,
                                                                   db.booking_services.c.id == sid)).rowcount
    cur = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
    vals = {}
    if r["hours"]:
        vals["hours"] = {}
    if r["site"]:
        site = dict(cur["site"] or {})
        for k, v in r["site"].items():
            if site.get(k) == v:
                site.pop(k, None)
        vals["site"] = site
    if r["words"]:
        words = dict(cur["words"] or {})
        for k, v in r["words"].items():
            if words.get(k) == v:
                words.pop(k, None)
        vals["words"] = words
    if vals:
        conn.execute(update(db.businesses).where(db.businesses.c.id == bid).values(**vals, updated_at=func.now()))
    conn.execute(update(SA).where(SA.c.id == apply_id).values(undone_at=now))
    log(conn, bid, "snapshot.undone", actor, apply_id, {"removed": removed, "kept": len(kept)})
    return {"removed": removed, "kept": kept}


# ---------------------------------------------------------------- export (the owner's own setup, no customer data)
def export(conn: Connection, biz: dict) -> dict:
    from . import booking as BK
    from . import campaigns as CG
    from . import fields as CF
    from . import forms as FM
    from . import recipes as RC
    from . import replies as SR
    from . import sequences as SQ
    from . import workflows as WF
    fl = CF.list_(conn, biz["id"], "contact")
    by_key = {f["key"]: f["label"] for f in fl}
    forms_by_id = {str(f["id"]): f["name"] for f in FM.list_(conn, biz["id"])}
    seqs_by_id = {str(s["id"]): s["name"] for s in SQ.list_(conn, biz["id"])}

    def refs(v, key=None):
        """ids and field keys -> names (they mean nothing in another business)."""
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if k == "user_id":
                    continue
                out[k] = refs(x, k)
            return out
        if isinstance(v, list):
            return [refs(x) for x in v]
        if isinstance(v, str):
            if key == "key" and v in by_key:
                return f"@field:{by_key[v]}"
            if key == "form_id" and v in forms_by_id:
                return f"@form:{forms_by_id[v]}"
            if key == "sequence_id" and v in seqs_by_id:
                return f"@seq:{seqs_by_id[v]}"
            if v.startswith("cf:") and v[3:] in by_key:
                return f"@field:{by_key[v[3:]]}"
        return v
    site = biz.get("site") or {}
    return {
        "format": FORMAT, "version": 1, "industry": biz["industry"], "name": biz["name"],
        "fields": [{"label": f["label"], "kind": f["kind"], "options": f["options"], "show_in_list": f["show_in_list"]} for f in fl],
        "recipes": [refs({"name": r["name"] or RC.describe(conn, biz, r, biz["language"]), "trigger": r["trigger"],
                          "trigger_arg": r["trigger_arg"], "condition": r["condition"],
                          "actions": [a for a in r["actions"] if a.get("do") != "assign"]}) for r in RC.list_(conn, biz["id"])],
        "workflows": [refs({"name": w["name"], "trigger": w["trigger"], "trigger_arg": w["trigger_arg"], "steps": w["steps"],
                            "goal": w["goal"]}) for w in WF.list_(conn, biz["id"])],
        "sequences": [refs({"name": s["name"], "kind": s["kind"], "trigger": s["trigger"], "trigger_arg": s["trigger_arg"],
                            "steps": [{k: v for k, v in st.items() if k != "links"} for st in s["steps"]], "exit_on": s["exit_on"]})
                      for s in SQ.list_(conn, biz["id"])],
        "campaigns": [{"name": c["name"], "channel": c["channel"], "text": c["message"], "subject": c["subject"], "blocks": c["blocks"]}
                      for c in CG.list_(conn, biz["id"])],
        "forms": [{"name": f["name"], "intro": f["intro"], "thanks": f["thanks"], "tag": f["tag"],
                   "fields": [refs(x["key"]) if x["key"].startswith("cf:") else x["key"] for x in f["fields"]]}
                  for f in FM.list_(conn, biz["id"])],
        "replies": [{"title": r["title"], "body": r["body"]} for r in SR.list_(conn, biz["id"])],
        "services": [{"name": s["name"], "minutes": s["minutes"], "price_minor": s["price_minor"]}
                     for s in BK.services(conn, biz["id"], active_only=True)],
        "hours": biz.get("hours") or {},
        "site": {k: site[k] for k in ("headline", "about") if site.get(k)},
        "words": dict(biz.get("words") or {}),
    }


def check_bundle(data) -> dict:
    """An uploaded setup: the right format, only the known parts."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise Invalid("this is not a Valeria One setup file", "file")
    return {k: data[k] for k in PARTS if k in data}


def copy_to(conn: Connection, user_id: UUID, source: dict, target_id: UUID, actor: str) -> dict:
    """Apply this business's setup to another business the same person OWNS."""
    m = conn.execute(select(db.memberships.c.role).where(db.memberships.c.business_id == target_id,
                                                         db.memberships.c.user_id == user_id)).scalar()
    if m != "owner" or target_id == source["id"]:
        raise NotFound("business")
    target = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == target_id)).mappings().one())
    return apply(conn, target, actor, check_bundle(export(conn, source)), "import")
