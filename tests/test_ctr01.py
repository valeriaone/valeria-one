"""ctr01 (v0.24): contracts — a business sends its customer a written agreement, and the customer signs it on the phone.

One base contract (who, what, when, how much, cancellation, changes, care and damage, data privacy, signatures) + the clause
blocks the trade's kit switches on; the owner switches blocks on and off without losing their own changes; an unusual
business describes itself and its OWN AI picks the blocks (no AI = base + the kit's blocks); the owner's own PDF or words.
A copy is filled in from the customer and the booking / quote / invoice, sent by the channel they use (another one when the
24 h window is closed, else the owner shares the link), frozen with a SHA-256 fingerprint (a change = a new version, the old
one voided); the customer's signed link (bad = 404) records the typed name, the drawn signature, the time, IP, device and the
fingerprint checked again; voided / older copies can't be signed; decline with a reason; the owner hears it; a booking can
wait for the signed contract (and deposit); one reminder after 24 h; a printable notary copy; one business never sees
another's; the export and the "holds data" pin include it; every word is in all 11 languages. Nothing leaves the machine."""
import base64
import importlib.util
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update

from _va import Net, actions, appt, customer, link_owner, owner_session, refuse_network, shop, with_ai
from conftest import make_business
from test_web import signup
from valeria_one import db, kits, packs
from valeria_one.api import app
from valeria_one.services import Invalid, NotFound
from valeria_one.services import contracts as CT
from valeria_one.services import deals as D
from valeria_one.services import export as EXP
from valeria_one.services import inbox as X
from valeria_one.services import quotes as QS
from valeria_one.web import contracts as WCT

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
NOW = lambda: datetime.now(timezone.utc)                     # noqa: E731
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)
    WCT._sign_hits.clear()


def png(ink=True, w=400, h=150) -> str:
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if ink:
        ImageDraw.Draw(im).line([(20, 100), (120, 40), (220, 110), (360, 30)], fill=(10, 10, 10, 255), width=5)
    b = io.BytesIO()
    im.save(b, "PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def setup(lang="en", industry="salon"):
    """A salon with its owner on Telegram and its customers on a gateway; Ana is a customer."""
    biz, tg, gw = shop(country="US", name="Sol Salon", lang=lang)
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(industry=industry))
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    net = Net()
    owner = link_owner(biz, tg, net=net)
    net.tg.clear()
    ana = customer(biz, gw, "Ana López", who="+14155552671", phone="+14155552671")
    return biz, tg, gw, net, ana


def kit_template(biz):
    with db.tx() as conn:
        return CT.from_kit(conn, biz, "t")


def draft(biz, contact=None, **refs):
    tpl = kit_template(biz)
    with db.tx() as conn:
        return CT.create(conn, biz, "t", template_id=tpl["id"], contact_id=contact["id"] if contact else None, **refs)


def sent(biz, contact, net, **refs):
    c = draft(biz, contact, **refs)
    res = CT.send(biz, "t", c["id"], http=net.http)
    return res


def path_of(url):
    return url.replace("https://one.test", "")


def row(cid):
    with db.tx() as conn:
        return dict(conn.execute(select(db.contracts).where(db.contracts.c.id == cid)).mappings().one())


def pub_client():
    return TestClient(app, raise_server_exceptions=False, base_url="https://testserver", headers={"User-Agent": "Mozilla/5.0 (iPhone) TestPhone"})


def sign_via_web(c, url, name="Ana López", **over):
    p = path_of(url)
    page = c.get(p)
    assert page.status_code == 200, page.text[:300]
    t = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    data = {"csrf": t, "name": name, "privacy": "1", "agree": "1", "signature": png(), **over}
    return c.post(p, data={k: v for k, v in data.items() if v is not None}, headers={"referer": "https://testserver" + p},
                  follow_redirects=False)


# ================================================================ the words: base + kit blocks, toggling
def test_a_template_from_the_kit_has_the_base_and_the_trades_blocks():
    biz, *_ = setup()
    tpl = kit_template(biz)
    keys = [s["key"] for s in tpl["sections"]]
    assert tpl["source"] == "kit" and keys[:4] == ["parties", "what", "when", "price"] and keys[-2:] == ["privacy", "signatures"]
    want = packs.industry("salon").kit.contracts
    assert want and CT.blocks_of(tpl["sections"]) == [b for b in CT.BLOCKS if b in want]
    assert keys.index("liability") < keys.index(f"k:{want[0]}") < keys.index("privacy")      # blocks after liability
    words = CT.text_of(tpl["sections"])
    assert "{client}" in words and "{price}" in words and "## Your data" in words
    assert not re.search(r"\b(Act|Code|Article|Section)\s+\d", words)                     # no statute cited
    assert kits.check_kits() == []
    for code, ip in packs.industries().items():                                        # every kit's blocks exist
        assert set(ip.kit.contracts) <= set(CT.BLOCKS), code
    assert len(CT.BLOCKS) >= 15


def test_blocks_switch_on_and_off_and_the_owners_own_changes_stay():
    biz, *_ = setup()
    tpl = kit_template(biz)
    secs = [dict(s) for s in tpl["sections"]]
    secs[0]["text"] = "Between Sol Salon and {client}. MY OWN WORDS."
    with db.tx() as conn:
        CT.save_template(conn, biz, "t", tpl["id"], sections=secs + [{"key": "own:1", "title": "Parking", "text": "Park in the back."}])
        before = CT.blocks_of(CT.template(conn, biz["id"], tpl["id"])["sections"])
        t2 = CT.set_blocks(conn, biz, "t", tpl["id"], ["rental", "pets", "no_show"])
    keys = [s["key"] for s in t2["sections"]]
    assert CT.blocks_of(t2["sections"]) == ["no_show", "rental", "pets"] and set(before) != {"no_show", "rental", "pets"}
    assert keys.index("k:pets") < keys.index("privacy") and "own:1" in keys
    assert t2["sections"][0]["text"] == "Between Sol Salon and {client}. MY OWN WORDS."
    with db.tx() as conn:
        t3 = CT.set_blocks(conn, biz, "t", tpl["id"], [])
    assert CT.blocks_of(t3["sections"]) == [] and "own:1" in [s["key"] for s in t3["sections"]]


# ================================================================ filled in from the customer and the booking
def test_a_copy_is_filled_in_from_the_customer_and_the_booking_and_a_quote():
    biz, tg, gw, net, ana = setup()
    a = appt(biz, ana, NOW() + timedelta(days=3), title="Bridal hair")
    c = draft(biz, ana, appointment_id=a["id"])
    assert c["status"] == "draft" and c["contact_id"] == ana["id"] and c["appointment_id"] == a["id"]
    assert "Ana López" in c["body"] and "Bridal hair" in c["body"] and "+14155552671" in c["body"] and "Sol Salon" in c["body"]
    assert CT.BLANK in c["body"] and "{" not in c["body"]                                 # unknown price = a line to write on
    with db.tx() as conn:
        q = QS.create(conn, biz, "t", title="Wedding package", lines=[{"description": "Hair", "qty": 1, "unit_minor": 50000}],
                      contact_id=ana["id"])
        c2 = CT.create(conn, biz, "t", template_id=c["template_id"], quote_id=q["id"])
        c2 = CT.save(conn, biz, "t", c2["id"], fields={"deposit_minor": 10000, "due": "on the day"}, refill=True)
    assert c2["contact_id"] == ana["id"] and "Wedding package" in c2["body"] and "$500.00" in c2["body"]
    assert "$100.00" in c2["body"] and "$400.00" in c2["body"] and "on the day" in c2["body"] and c2["deposit_minor"] == 10000
    with db.tx() as conn:
        c3 = CT.save(conn, biz, "t", c2["id"], body="Only my words for Ana.")
    assert c3["body"] == "Only my words for Ana."


# ================================================================ sent = frozen, a change = a new version
def test_sending_freezes_it_with_its_fingerprint_and_a_change_makes_a_new_version():
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    assert res["status"] == "sent" and res["problem"] is None and res["channel"] == "webhook"
    assert res["doc_hash"] == CT.fingerprint(res) and len(res["doc_hash"]) == 64
    assert any(res["link"] in m["body"] for m in net.gw) and "/sign/" in res["link"]
    with db.tx() as conn:
        with pytest.raises(Invalid):
            CT.save(conn, biz, "t", res["id"], body="sneaky change")
        with pytest.raises(Invalid):
            CT.delete_draft(conn, biz, "t", res["id"])
        v2 = CT.new_version(conn, biz, "t", res["id"])
    assert v2["status"] == "draft" and v2["version"] == 2 and v2["number"] == res["number"] and v2["body"] == res["body"]
    old = row(res["id"])
    assert old["status"] == "voided" and old["void_reason"] == "version 2" and old["doc_hash"] == res["doc_hash"]
    with pytest.raises(Invalid):
        CT.send(biz, "t", res["id"])                                                    # nothing sent twice


# ================================================================ the customer's page
def test_the_signing_link_opens_only_with_its_own_signature(client):
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    c = pub_client()
    page = c.get(path_of(res["link"]))
    assert page.status_code == 200 and "Ana López" in page.text and 'id="sg-pad"' in page.text and "sign electronically" in page.text
    assert "lawyer" not in page.text                                                  # the owner's note never reaches the customer
    assert row(res["id"])["status"] == "viewed"
    other = sent(biz, ana, net)
    for p in (f"/sign/{res['id']}/{'0' * 32}", f"/sign/{res['id']}/{CT.sig(row(other['id']))}", "/sign/not-a-uuid/xyz",
              f"/sign/00000000-0000-0000-0000-000000000000/badtoken", f"/sign/{res['id']}/{'0' * 32}/print",
              f"/sign/{res['id']}/{'0' * 32}/file"):
        assert c.get(p).status_code == 404, p
        assert c.post(p, data={"name": "x"}).status_code in (404, 405), p
    with db.tx() as conn:
        d = CT.create(conn, biz, "t", template_id=res["template_id"], contact_id=ana["id"])
    assert CT.link(d) is None                                                          # a draft has no link at all


def test_signing_records_the_name_drawing_time_ip_device_and_the_fingerprint():
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    c = pub_client()
    r = sign_via_web(c, res["link"])
    assert r.status_code == 303
    s = row(res["id"])
    assert s["status"] == "signed" and s["signed_name"] == "Ana López" and s["signed_at"] and s["signed_ip"]
    assert "TestPhone" in s["signed_ua"] and s["signature"].startswith("data:image/png;base64,")
    assert s["signed_hash"] == s["doc_hash"] == CT.fingerprint(s)
    page = c.get(path_of(res["link"])).text
    assert "Signed. Thank you!" in page and s["doc_hash"] in page and "/print" in page
    notes = [a["summary"] for a in actions(biz, kind="notice")]
    assert any("Ana signed" in n for n in notes), notes                                 # the owner hears it
    pr = c.get(path_of(res["link"]) + "/print")
    assert pr.status_code == 200 and "Signature record" in pr.text and s["doc_hash"] in pr.text and s["signature"][:40] in pr.text
    with db.tx() as conn:
        ev = conn.execute(select(db.events.c.data).where(db.events.c.type == "contract.signed")).scalar()
    assert ev["doc_hash"] == s["doc_hash"] and ev["signed_name"] == "Ana López"


def test_a_signature_must_be_drawn_named_and_consented():
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    c = pub_client()
    for over in ({"signature": png(ink=False)}, {"signature": "data:image/png;base64,AAAA"}, {"signature": "hello"},
                 {"privacy": None}, {"agree": None}, {"name": "A"},
                 {"signature": "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (CT.MAX_SIGNATURE + 10)).decode()}):
        r = sign_via_web(c, res["link"], **over)
        assert r.status_code == 303 and row(res["id"])["status"] == "viewed", over
    with pytest.raises(Invalid):
        CT.check_signature(png(ink=False))
    assert CT.check_signature(png()).startswith("data:image/png;base64,")


def test_a_voided_copy_and_an_older_version_can_never_be_signed():
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    with db.tx() as conn:
        CT.void(conn, biz, "t", res["id"], "wrong price")
    c = pub_client()
    page = c.get(path_of(res["link"]))
    assert page.status_code == 200 and "cancelled by the business" in page.text and 'id="sg-pad"' not in page.text
    sign_via_web(c, res["link"])
    assert row(res["id"])["status"] == "voided" and row(res["id"])["signed_at"] is None
    with pytest.raises(Invalid):
        CT.sign(res["id"], CT.sig(row(res["id"])), name="Ana López", privacy=True, agree=True, signature=png(), ip="1", ua="x")
    res2 = sent(biz, ana, net)
    with db.tx() as conn:
        v2 = CT.new_version(conn, biz, "t", res2["id"])
    page = c.get(path_of(res2["link"]))
    assert "replaced by a newer one" in page.text and 'id="sg-pad"' not in page.text
    sign_via_web(c, res2["link"])
    assert row(res2["id"])["status"] == "voided" and row(v2["id"])["status"] == "draft"
    v2s = CT.send(biz, "t", v2["id"], http=net.http)
    assert v2s["link"] != res2["link"] and v2s["doc_hash"] != res2["doc_hash"]          # version is in the fingerprint
    assert sign_via_web(c, v2s["link"]).status_code == 303 and row(v2["id"])["status"] == "signed"
    with db.tx() as conn:                                                              # a changed body after sending: refused
        r3 = sent(biz, ana, net)
        conn.execute(update(db.contracts).where(db.contracts.c.id == r3["id"]).values(body="tampered"))
    with pytest.raises(Invalid):
        CT.sign(r3["id"], CT.sig(row(r3["id"])), name="Ana López", privacy=True, agree=True, signature=png(), ip="1", ua="x")
    assert row(r3["id"])["status"] == "sent"
    with db.tx() as conn:
        with pytest.raises(Invalid):
            CT.void(conn, biz, "t", v2["id"])                                          # a signed one stays as it is


def test_the_customer_can_decline_with_a_reason_and_the_owner_hears_it():
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    c = pub_client()
    p = path_of(res["link"])
    t = re.search(r'name="csrf" value="([^"]+)"', c.get(p).text).group(1)
    assert c.post(p, data={"csrf": t, "act": "decline", "reason": "The price is too high"}, follow_redirects=False).status_code == 303
    s = row(res["id"])
    assert s["status"] == "declined" and s["decline_reason"] == "The price is too high" and s["declined_at"]
    assert any("declined" in a["summary"] and "too high" in a["summary"] for a in actions(biz, kind="notice"))
    assert "You declined this contract" in c.get(p).text
    sign_via_web(c, res["link"])
    assert row(res["id"])["status"] == "declined"


# ================================================================ the channel: the 24 h window, else the owner shares it
def test_a_closed_24h_window_goes_another_way_or_the_owner_is_told_to_share_the_link(client):
    biz, tg, gw, net, ana = setup()
    with db.tx() as conn:
        fb = X.connect(conn, biz, "t", channel="messenger", name="Page",
                       cfg={"page_id": "P1", "page_token": "pt", "app_secret": "s", "verify_token": "v"})
        from valeria_one.services import contacts as C
        C.link(conn, biz["id"], ana["id"], "messenger", "PSID-ANA")
        conn.execute(db.conversations.insert().values(business_id=biz["id"], contact_id=ana["id"], channel_account_id=fb["id"],
                                                     last_inbound_at=NOW() - timedelta(hours=30), last_message_at=NOW()))
    res = sent(biz, ana, net)                                                         # Messenger's window closed -> the gateway
    assert res["problem"] is None and res["channel"] == "webhook"
    lone, _ = make_business("US", "salon", name="Solo")
    with db.tx() as conn:
        from valeria_one.services import contacts as C
        nobody = C.create(conn, lone, "t", {"name": "Beto"})
    r2 = sent(lone, nobody, net)
    assert r2["status"] == "sent" and r2["problem"] == "no_channel" and r2["link"]
    owner_session(client, lone)
    client.post("/lang/en", data={"csrf": tok(client)})
    page = client.get(f"/app/contracts/{r2['id']}").text
    assert r2["link"] in page and "wa.me/?text=" in page and "mailto:" in page


# ================================================================ one business, its own
def test_another_business_never_sees_or_touches_a_contract(client):
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    tpl_id = res["template_id"]
    other, _ = make_business("US", "salon", name="Other")
    with db.tx() as conn:
        for fn, args in ((CT.get, (other["id"], res["id"])), (CT.template, (other["id"], tpl_id))):
            with pytest.raises(NotFound):
                fn(conn, *args)
        with pytest.raises(NotFound):
            CT.void(conn, other, "t", res["id"])
        with pytest.raises(NotFound):
            CT.create(conn, other, "t", template_id=tpl_id)
        assert CT.list_(conn, other["id"]) == []
    with db.tx() as conn:
        otpl = CT.from_kit(conn, other, "t")
        with pytest.raises(NotFound):
            CT.create(conn, other, "t", template_id=otpl["id"], contact_id=ana["id"])
    owner_session(client, other)
    t = tok(client, "/app/contracts")
    assert f"/app/contracts/{res['id']}" not in client.get("/app/contracts").text
    for p in (f"/app/contracts/{res['id']}", f"/app/contracts/{res['id']}/print", f"/app/contracts/templates/{tpl_id}"):
        assert client.get(p).status_code == 404, p
    for p, data in ((f"/app/contracts/{res['id']}", {"act": "void"}), (f"/app/contracts/{res['id']}", {"act": "new_version"}),
                    (f"/app/contracts/templates/{tpl_id}", {"act": "archive"})):
        assert client.post(p, data={"csrf": t, **data}).status_code == 404, p
    assert row(res["id"])["status"] == "sent"


def test_signed_out_pages_send_to_sign_in_and_posts_never_fail(client):
    for p in ("/app/contracts", "/app/contracts/new", f"/app/contracts/{'0' * 8}-0000-0000-0000-{'0' * 12}"):
        assert client.get(p, follow_redirects=False).status_code == 303, p
    for p in ("/app/contracts", "/app/contracts/templates", "/app/contracts/settings", f"/app/contracts/{'0' * 8}-0000-0000-0000-{'0' * 12}"):
        assert client.post(p, follow_redirects=False).status_code == 303, p


# ================================================================ the booking waits for it (and the deposit)
def test_a_booking_waits_for_its_signed_contract_and_deposit():
    biz, tg, gw, net, ana = setup()
    a = appt(biz, ana, NOW() + timedelta(days=2))
    with db.tx() as conn:
        CT.save_settings(conn, biz, "t", require_signed=True, require_deposit=True)
        with pytest.raises(Invalid, match="signed contract"):
            D.set_appointment_status(conn, biz["id"], "t", a["id"], "confirmed")
    c = draft(biz, ana, appointment_id=a["id"])
    with db.tx() as conn:
        CT.save(conn, biz, "t", c["id"], fields={"price_minor": 30000, "deposit_minor": 5000}, refill=True)
    res = CT.send(biz, "t", c["id"], http=net.http)
    net.gw.clear()
    CT.sign(res["id"], CT.sig(row(res["id"])), name="Ana López", privacy=True, agree=True, signature=png(), ip="1.2.3.4", ua="t",
            http=net.http)
    assert any("signed copy" in m["body"] and res["link"] in m["body"] for m in net.gw)    # the customer's copy, same channel
    with db.tx() as conn:
        ap = conn.execute(select(db.appointments.c.status).where(db.appointments.c.id == a["id"])).scalar()
        assert ap == "booked" and CT.gate(conn, biz["id"], a["id"]) == "deposit"            # signed, deposit not in yet
        with pytest.raises(Invalid, match="deposit"):
            D.set_appointment_status(conn, biz["id"], "t", a["id"], "confirmed")
        CT.mark_deposit(conn, biz, "t", res["id"])
        assert conn.execute(select(db.appointments.c.status).where(db.appointments.c.id == a["id"])).scalar() == "confirmed"
    b = appt(biz, ana, NOW() + timedelta(days=4))
    with db.tx() as conn:
        CT.save_settings(conn, biz, "t", require_signed=False, require_deposit=True)
        assert CT.settings(conn, biz["id"])["require_deposit"] is False                   # deposit only with signed
        D.set_appointment_status(conn, biz["id"], "t", b["id"], "confirmed")                # off: as before


def test_a_yes_to_the_reminder_does_not_confirm_a_booking_that_waits_for_its_contract():
    from _va import gw_in, shop as _shop
    from test_reminders import sent_reminder
    biz, tg, gw = _shop()
    with db.tx() as conn:
        CT.save_settings(conn, biz, "t", require_signed=True, require_deposit=False)
    c, a, net = sent_reminder(biz, gw)
    _, res = gw_in(gw, "ana-1", "1", net=net)
    assert res == ["reminder:confirmed"]                                               # thanked as before
    with db.tx() as conn:
        assert conn.execute(select(db.appointments.c.status).where(db.appointments.c.id == a["id"])).scalar() == "booked"
        assert conn.execute(select(db.events.c.data).where(db.events.c.type == "appointment.confirm_waiting")).scalar()["why"] == "unsigned"


def test_a_paid_pay_link_counts_as_the_deposit():
    biz, tg, gw, net, ana = setup()
    a = appt(biz, ana, NOW() + timedelta(days=2))
    with db.tx() as conn:
        CT.save_settings(conn, biz, "t", require_signed=True, require_deposit=True)
        pay = conn.execute(db.payments.insert().values(business_id=biz["id"], provider="cash", contact_id=ana["id"], amount_minor=5000,
                                                       currency="USD", description="Deposit", token="tok-dep-1")
                           .returning(db.payments.c.id)).scalar()
    c = draft(biz, ana, appointment_id=a["id"])
    with db.tx() as conn:
        CT.save(conn, biz, "t", c["id"], fields={"deposit_minor": 5000})
        conn.execute(update(db.contracts).where(db.contracts.c.id == c["id"]).values(payment_id=pay))
    res = CT.send(biz, "t", c["id"], http=net.http)
    CT.sign(res["id"], CT.sig(row(res["id"])), name="Ana López", privacy=True, agree=True, signature=png(), ip="1", ua="t")
    from valeria_one.services import payments as PM
    with db.tx() as conn:
        assert PM.mark_paid(conn, pay, "t")
    assert row(res["id"])["deposit_paid_at"] is not None
    with db.tx() as conn:
        assert conn.execute(select(db.appointments.c.status).where(db.appointments.c.id == a["id"])).scalar() == "confirmed"


# ================================================================ one reminder after 24 h (the jobs round)
def test_unsigned_after_24h_gets_one_reminder_else_the_owner_is_told():
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    assert CT.round_(http=net.http) == {"reminded": 0, "told_owner": 0}                 # not yet
    with db.tx() as conn:
        conn.execute(update(db.contracts).where(db.contracts.c.id == res["id"]).values(sent_at=NOW() - timedelta(hours=25)))
    net.gw.clear()
    assert CT.round_(http=net.http) == {"reminded": 1, "told_owner": 0}
    assert any("reminder" in m["body"] and res["link"] in m["body"] for m in net.gw) and row(res["id"])["reminded_at"]
    assert CT.round_(http=net.http) == {"reminded": 0, "told_owner": 0}                 # once
    lone, _ = make_business("US", "salon", name="Solo")
    with db.tx() as conn:
        from valeria_one.services import contacts as C
        beto = C.create(conn, lone, "t", {"name": "Beto Ruiz"})
    r2 = sent(lone, beto, net)
    with db.tx() as conn:
        conn.execute(update(db.contracts).where(db.contracts.c.id == r2["id"]).values(sent_at=NOW() - timedelta(hours=30)))
    assert CT.round_(http=net.http) == {"reminded": 0, "told_owner": 1}
    note = actions(lone, kind="notice")[-1]["summary"]
    assert "Beto hasn't signed" in note and r2["link"] in note
    from valeria_one import jobs
    assert "contracts" in jobs._round()[0]


# ================================================================ Valeria drafts it (own AI) — never live AI in tests
def test_an_unusual_business_described_in_its_own_words_without_ai_gets_base_and_kit_blocks():
    biz, *_ = setup()
    tpl, by_ai = CT.from_words(biz, "t", "We rent karaoke machines and set them up at parties")
    assert by_ai is False and tpl["source"] == "kit"
    assert CT.blocks_of(tpl["sections"]) == [b for b in CT.BLOCKS if b in packs.industry("salon").kit.contracts]
    with pytest.raises(Invalid):
        CT.from_words(biz, "t", "   ")


def test_with_its_own_ai_valeria_picks_the_blocks_and_adds_one_clause(monkeypatch):
    biz, *_ = setup()
    biz = with_ai(biz)
    out = json.dumps({"blocks": ["rental", "travel", "made_up", "weather"], "title": "Karaoke rental agreement",
                      "extra_title": "Noise", "extra": "The client keeps the volume within the venue's rules."})
    net = Net(ai_text=lambda prompt, j: out)
    tpl, by_ai = CT.from_words(biz, "t", "We rent karaoke machines and set them up at parties", http=net.http)
    assert by_ai and tpl["source"] == "ai" and tpl["name"] == "Karaoke rental agreement"
    assert CT.blocks_of(tpl["sections"]) == ["rental", "travel", "weather"]                 # unknown keys dropped
    keys = [s["key"] for s in tpl["sections"]]
    assert "own:ai" in keys and keys.index("own:ai") < keys.index("privacy")
    sys_prompt = net.ai_calls[-1]["systemInstruction"]["parts"][0]["text"]
    assert "never cite or name a law" in sys_prompt and "karaoke" in json.dumps(net.ai_calls[-1]["contents"])
    tpl2, by_ai2 = CT.from_words(biz, "t", "We groom dogs", http=Net(ai_text=None).http)  # busy AI -> the kit's
    assert not by_ai2 and tpl2["source"] == "kit"
    tpl3, by_ai3 = CT.from_words(biz, "t", "We groom dogs", http=Net(ai_text="no json here").http)
    assert not by_ai3


# ================================================================ the owner's own contract
def test_the_owner_uploads_their_own_pdf_or_words_and_valeria_still_sends_and_signs(client):
    biz, tg, gw, net, ana = setup()
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    t = tok(client, "/app/contracts")
    r = client.post("/app/contracts/templates", data={"csrf": t, "act": "upload", "name": "My lawyer's contract"},
                    files={"file": ("mine.pdf", PDF, "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303 and "/app/contracts/templates/" in r.headers["location"]
    with db.tx() as conn:
        tpl = CT.templates(conn, biz["id"])[0]
    assert tpl["source"] == "upload" and tpl["file_id"] and [s["key"] for s in tpl["sections"]] == list(CT.SUMMARY)
    page = client.get(r.headers["location"]).text
    assert "Your PDF" in page and "lawyer in your country" not in page                 # their own words: no sample note
    with db.tx() as conn:
        c = CT.create(conn, biz, "t", template_id=tpl["id"], contact_id=ana["id"])
    import hashlib
    assert c["file_sha256"] == hashlib.sha256(PDF).hexdigest()
    res = CT.send(biz, "t", c["id"], http=net.http)
    pc = pub_client()
    page = pc.get(path_of(res["link"])).text
    assert "/file" in page and "<iframe" in page
    f = pc.get(path_of(res["link"]) + "/file")
    assert f.status_code == 200 and f.content == PDF and f.headers["content-type"] == "application/pdf"
    assert sign_via_web(pc, res["link"]).status_code == 303 and row(res["id"])["status"] == "signed"
    cert = pc.get(path_of(res["link"]) + "/print").text
    assert c["file_sha256"] in cert and "attached document" in cert
    with db.tx() as conn:                                                              # a contract's document is kept
        from valeria_one.services import files as FL
        with pytest.raises(Invalid):
            FL.remove(conn, biz["id"], "t", tpl["file_id"])
    t = tok(client, "/app/contracts")
    r = client.post("/app/contracts/templates", data={"csrf": t, "act": "upload", "name": ""},
                    files={"file": ("terms.txt", "# Terms\nPay on time, {client}.\n# Extra\nNo refunds.".encode(), "text/plain")},
                    follow_redirects=False)
    with db.tx() as conn:
        own = [x for x in CT.templates(conn, biz["id"]) if x["source"] == "own"][0]
    assert [s["title"] for s in own["sections"]] == ["Terms", "Extra"] and own["name"] == "terms"
    r = client.post("/app/contracts/templates", data={"csrf": t, "act": "upload", "name": "x"},
                    files={"file": ("x.png", b"\x89PNG....", "image/png")}, follow_redirects=False)
    assert r.status_code == 303                                                        # refused plainly, never a 500
    with db.tx() as conn:                                                              # the business can still be deleted
        conn.execute(delete(db.businesses).where(db.businesses.c.id == biz["id"]))


# ================================================================ the screens, end to end
def test_the_owner_makes_sends_and_follows_a_contract_on_the_screens(client, monkeypatch):
    biz, tg, gw, net, ana = setup()
    owner_session(client, biz)
    client.post("/lang/en", data={"csrf": tok(client)})
    page = client.get("/app/contracts", headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
    assert page.status_code == 200 and 'name="viewport"' in page.text and "lawyer in your country" in page.text
    t = tok(client, "/app/contracts")
    r = client.post("/app/contracts/templates", data={"csrf": t, "act": "kit", "name": "Salon agreement"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[1]
    tp = client.get(f"/app/contracts/templates/{tid}").text
    assert "Salon agreement" in tp and "Sample wording" in tp and 'id="b-rental"' in tp and "{client}" in tp
    a = appt(biz, ana, NOW() + timedelta(days=1), title="Color")
    nw = client.get(f"/app/contracts/new?appointment={a['id']}").text
    assert "Color" in nw and f'value="{a["id"]}"' in nw
    r = client.post("/app/contracts", data={"csrf": t, "template_id": tid, "contact_id": str(ana["id"]), "appointment_id": str(a["id"])},
                    follow_redirects=False)
    cid = r.headers["location"].rsplit("/", 1)[1]
    r = client.post(f"/app/contracts/{cid}", data={"csrf": t, "act": "refill", "client": "Ana López", "service": "Color",
                                                   "date": "Friday 10:00", "price": "120", "deposit": "20", "due": "on the day"})
    body = row(cid)["body"]
    assert "$120.00" in body and "$100.00" in body and "Friday 10:00" in body
    real, calls = CT._deliver, []

    def through_net(*a, **kw):                                                        # the screen's send, on the mocked network
        calls.append(a)
        return real(*a, **{**kw, "http": net.http})
    monkeypatch.setattr(CT, "_deliver", through_net)
    r = client.post(f"/app/contracts/{cid}", data={"csrf": t, "act": "send", "account_id": ""}, follow_redirects=False)
    assert r.status_code == 303 and row(cid)["status"] == "sent" and calls and row(cid)["channel"] == "webhook"
    assert "Sent. You&#39;ll hear when they sign." in client.get(f"/app/contracts/{cid}").text
    cp = client.get(f"/app/contracts/{cid}").text
    assert "/sign/" in cp and "Change it (new version)" in cp and "Print for a notary" in cp
    pr = client.get(f"/app/contracts/{cid}/print?notary=1")
    assert pr.status_code == 200 and "Notary acknowledgment" in pr.text and "Seal / commission number" in pr.text
    assert client.get(f"/app/contracts/{cid}/print").status_code == 200 and "Notary acknowledgment" not in client.get(f"/app/contracts/{cid}/print").text
    cl = client.get(f"/app/contacts/{ana['id']}").text
    assert f'href="/app/contracts/{cid}"' in cl and 'id="contracts"' in cl
    lst = client.get("/app/contracts?status=sent").text
    assert f'href="/app/contracts/{cid}"' in lst and client.get("/app/contracts?status=signed").text.count(f'/app/contracts/{cid}"') == 0
    r = client.post("/app/contracts/settings", data={"csrf": t, "require_signed": "1"}, follow_redirects=False)
    with db.tx() as conn:
        assert CT.settings(conn, biz["id"])["require_signed"] is True
    r = client.post(f"/app/contracts/{cid}", data={"csrf": t, "act": "void", "reason": "typo"}, follow_redirects=False)
    assert r.status_code == 303 and row(cid)["status"] == "voided"


def test_staff_send_from_templates_but_void_templates_and_settings_are_the_owners():
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    c2 = TestClient(app, raise_server_exceptions=False, base_url="https://testserver")
    owner_session(c2, biz, role="staff")
    c2.post("/lang/en", data={"csrf": tok(c2)})
    assert c2.get("/app/contracts").status_code == 200
    assert c2.get(f"/app/contracts/templates/{res['template_id']}", follow_redirects=False).status_code == 303
    t = tok(c2, "/app/contracts")
    c2.post(f"/app/contracts/{res['id']}", data={"csrf": t, "act": "void"})
    assert row(res["id"])["status"] == "sent"
    assert c2.post("/app/contracts/settings", data={"csrf": t, "require_signed": "1"}, follow_redirects=False).status_code == 303
    with db.tx() as conn:
        assert CT.settings(conn, biz["id"])["require_signed"] is False
    r = c2.post("/app/contracts", data={"csrf": t, "template_id": str(res["template_id"]), "contact_id": str(ana["id"])},
                follow_redirects=False)
    assert r.status_code == 303 and "/app/contracts/" in r.headers["location"]


@pytest.mark.parametrize("industry", sorted(packs.industries()))
def test_the_menu_link_and_add_more_for_every_kit(client, industry):
    signup(client, industry=industry, email=f"ct-{industry}@example.com")
    client.post("/lang/en", data={"csrf": tok(client)})
    nav = client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
    assert ('href="/app/contracts"' in nav) == (industry != "shop")
    assert 'id="t-contracts"' in client.get("/app/add").text
    for ua in ("Mozilla/5.0 (iPhone)", "Mozilla/5.0 (X11; Linux x86_64)"):
        r = client.get("/app/contracts", headers={"User-Agent": ua})
        left = set(re.findall(r"\{(\w+)\}", r.text.split("<main", 1)[1]))
        assert r.status_code == 200 and 'name="viewport"' in r.text and left <= set(CT.FIELDS), (industry, left)   # only the facts it explains


@pytest.mark.parametrize("lang", LANGS)
def test_the_signing_page_speaks_every_language_and_leaves_no_blank_words(lang):
    biz, tg, gw, net, ana = setup(lang=lang)
    res = sent(biz, ana, net)
    c = pub_client()
    c.post(f"/lang/{lang}", data={"csrf": re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)})
    page = c.get(path_of(res["link"]))
    assert page.status_code == 200 and not re.search(r"\{\w+\}", page.text.split("<main", 1)[1]), lang
    from markupsafe import escape
    assert ('dir="rtl"' in page.text) == (lang == "ar") and str(escape(CT._t("ct_pub_esign", lang))) in page.text
    assert str(escape(CT._t("ct_s_privacy", lang))) in page.text and "Ana López" in page.text           # the words are the business's language


# ================================================================ the export, the migration, the words
def test_the_export_holds_contracts_templates_and_the_setting():
    biz, tg, gw, net, ana = setup()
    res = sent(biz, ana, net)
    CT.sign(res["id"], CT.sig(row(res["id"])), name="Ana López", privacy=True, agree=True, signature=png(), ip="9.9.9.9", ua="t")
    with db.tx() as conn:
        CT.save_settings(conn, biz, "t", require_signed=True, require_deposit=False)
        out = EXP.business_data(conn, biz["id"], "t")
    assert {"contracts", "contract_templates", "contract_settings"} <= set(EXP.TABLES)
    assert len(out["contracts"]) == 1 and out["contracts"][0]["signed_name"] == "Ana López" and out["contracts"][0]["signature"]
    assert len(out["contract_templates"]) == 1 and out["contract_settings"][0]["require_signed"] is True
    json.dumps(out, default=str)


def test_the_tool_is_registered_and_kept_by_its_own_migration():
    t = kits.TOOLS["contracts"]
    assert t.group == "sell" and t.href == "/app/contracts" and t.uses and not t.owner
    path = next((ROOT / "alembic" / "versions").glob("*_ctr01_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1                                     # one head; v0.25 sits on top of this one (intentional)
    assert script.get_revision(mig.revision).down_revision == "c4d8e2a7f9b3"
    assert set(mig.USES) == {"contracts"}
    biz, _ = make_business("MX", "shop", name="Tienda")                              # a shop kit has no Contracts
    other, _ = make_business("MX", "shop", name="Otra")
    with db.tx() as conn:
        CT.from_kit(conn, biz, "t")
        conn.execute(update(db.businesses).values(tools={}))
        for sql in mig.pin_statements():
            conn.execute(text(sql))
        assert conn.execute(select(db.businesses.c.tools).where(db.businesses.c.id == biz["id"])).scalar() == {"contracts": True}
        assert conn.execute(select(db.businesses.c.tools).where(db.businesses.c.id == other["id"])).scalar() == {}
        assert "contracts" in kits.in_use(conn, biz["id"])


def test_every_template_compiles_and_the_signature_pad_script_is_valid_js():
    import shutil
    import subprocess
    from valeria_one.web import templates
    names = [n for n in templates.env.list_templates() if n.endswith(".html")]
    for n in names:
        templates.env.get_template(n)
    assert {"contracts.html", "contract.html", "contract_template.html", "contract_new.html", "contract_sign.html",
            "contract_print.html"} <= set(names)
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", str(ROOT / "valeria_one" / "web" / "static" / "sign.js")]).returncode == 0


def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {k for k in files["en"] if k.startswith("ct_")} | {"contract", "contracts", "ft_contracts_d", "r_no_channel", "r_no_contact"}
    assert len(keys) >= 200
    used = set()
    for p in ("valeria_one/services/contracts.py", "valeria_one/web/contracts.py", "valeria_one/web/templates/contracts.html",
              "valeria_one/web/templates/contract.html", "valeria_one/web/templates/contract_template.html",
              "valeria_one/web/templates/contract_new.html", "valeria_one/web/templates/contract_sign.html",
              "valeria_one/web/templates/contract_print.html", "valeria_one/web/templates/contact.html"):
        used |= set(re.findall(r"\bct_[a-z0-9_]+[a-z0-9]", (ROOT / p).read_text(encoding="utf-8")))
    prefixes = {"ct_st", "ct_src", "ct_k", "ct_s", "ct_f", "ct_gate_why"}
    used = {k for k in used if k not in prefixes}
    used |= {f"ct_st_{s}" for s in CT.STATUSES} | {f"ct_src_{s}" for s in ("kit", "ai", "own", "upload")}
    used |= {f"ct_k_{b}{x}" for b in CT.BLOCKS for x in ("", "_t")} | {f"ct_s_{b}{x}" for b in CT.BASE for x in ("", "_t")}
    used |= {f"ct_f_{f}" for f in CT.FIELDS} | {"ct_gate_why_unsigned", "ct_gate_why_deposit"}
    assert used <= keys, used - keys
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
    for k in keys:
        if len(files["en"][k]) > 14:
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k
    assert len(set(map(frozenset, files.values()))) == 1
    for lg in LANGS:                                                                   # the base contract fills in, every language
        words = CT.fill(CT.text_of(CT.compose(lg, list(CT.BLOCKS))), {f: "X" for f in CT.FIELDS})
        assert "{" not in words and words.count("## ") == len(CT.BASE) + len(CT.BLOCKS), lg
