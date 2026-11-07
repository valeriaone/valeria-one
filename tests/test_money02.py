"""money02 (v0.41.0): Today opens with «Your week with Valeria» — real rows from the last 7 days, counted once, only shown when
there is something to show; only the messages Valeria wrote are called hers."""
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from valeria_one import db
from valeria_one.services import inbox as X

from _va import owner_session
from conftest import make_business


def rows(biz, ago_days=0):
    """One AI-written reply, one booking, a 1,000.00 quote, a 500.00 paid invoice settled by its payment link, a 200.00 paid
    payment link on its own — all `ago_days` old."""
    when = datetime.now(timezone.utc) - timedelta(days=ago_days)
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="sandbox", name="S")
        cid = conn.execute(db.contacts.insert().values(business_id=biz["id"], name="Sofía", tags=[], notes="", marketing_consent=False,
                                                       custom={}).returning(db.contacts.c.id)).scalar()
        cv = conn.execute(db.conversations.insert().values(business_id=biz["id"], contact_id=cid, channel_account_id=acc["id"], status="open",
                                                           needs_human=False).returning(db.conversations.c.id)).scalar()
        conn.execute(db.messages.insert().values(business_id=biz["id"], conversation_id=cv, direction="out", kind="service", body="¡Hola!",
                                                 media=[], status="sent", by_ai=True, created_at=when))
        conn.execute(db.messages.insert().values(business_id=biz["id"], conversation_id=cv, direction="out", kind="service", body="yo",
                                                 media=[], status="sent", by_ai=False, created_at=when))   # the owner's own: not Valeria's
        conn.execute(db.appointments.insert().values(business_id=biz["id"], contact_id=cid, title="Visita", starts_at=when + timedelta(days=2),
                                                     ends_at=when + timedelta(days=2, hours=1), status="booked", created_at=when))
        conn.execute(db.deals.insert().values(business_id=biz["id"], contact_id=cid, title="Cabecera", stage="new", amount_minor=100000,
                                              currency=biz["currency"], created_at=when))
        pay = lambda amt: conn.execute(db.payments.insert().values(   # noqa: E731
            business_id=biz["id"], provider="link", amount_minor=amt, currency=biz["currency"], description="x",
            token=secrets.token_urlsafe(8), attempts=0, status="paid", paid_at=when).returning(db.payments.c.id)).scalar()
        p1 = pay(50000)
        conn.execute(db.invoices.insert().values(business_id=biz["id"], number=secrets.randbelow(10**6) + 1, title="Cabecera", lines=[],
                                                 currency=biz["currency"], subtotal_minor=50000, total_minor=50000, tax_name="", notes="",
                                                 issued_on=date.today(), due_on=date.today(), status="paid", token=secrets.token_urlsafe(8),
                                                 created_by="t", payment_id=p1, paid_at=when))
        pay(20000)


def card(client):
    page = client.get("/app").text
    return page.split('id="week"')[1].split("</div>\n</div>")[0] if 'id="week"' in page else None


def test_nothing_to_show_no_card(client):
    biz, _ = make_business("MX", "contractor", name="Taller Nuevo")
    owner_session(client, biz)
    assert card(client) is None


def test_the_week_counts_real_rows_once_and_only_calls_hers_what_valeria_wrote(client):
    biz, _ = make_business("MX", "contractor", name="Taller Hugo")
    rows(biz)
    owner_session(client, biz)
    c = card(client)
    assert c is not None
    nums = re.findall(r'class="n"[^>]*>([^<]+)<', c)
    assert nums[0].strip() == "1"                                   # one message by Valeria (the owner's own is not counted)
    assert "1,000" in c and "700" in c and "1,200" not in c         # quoted 1,000; collected 500 + 200 once (the invoice's payment not again)


def test_older_than_a_week_is_left_out(client):
    biz, _ = make_business("MX", "contractor", name="Taller Viejo")
    rows(biz, ago_days=9)
    owner_session(client, biz)
    assert card(client) is None


def test_the_card_speaks_every_language():
    from valeria_one.web.i18n import UI_LANGS, t
    for lang in UI_LANGS:
        for k in ("wk_title", "wk_answered", "wk_booked", "wk_quoted", "wk_collected", "wk_note"):
            assert t(k, lang) != k, (lang, k)


def test_the_demo_shows_valeria_at_work(client, monkeypatch):
    """The public demos carry sample work by Valeria: a reply of hers to each sample customer and one paid deposit."""
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    seed.seed()
    client.get("/try/decora-con-hugo")
    c = card(client)
    assert c is not None
    nums = re.findall(r'class="n"[^>]*>([^<]+)<', c)
    assert nums[0].strip() == "3" and "$1,400" in c          # 3 sample customers answered; deposit = half of the first deal ($2,800)
