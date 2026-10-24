import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# point everything at the TEST database before the app reads its settings
for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
    if line.startswith("VO_TEST_DATABASE_URL="):
        os.environ["VO_DATABASE_URL"] = line.split("=", 1)[1].strip()
if os.environ.get("VO_TEST_DATABASE_URL"):
    os.environ["VO_DATABASE_URL"] = os.environ["VO_TEST_DATABASE_URL"]
os.environ["VO_PUBLIC_URL"] = "https://one.test"
os.environ.setdefault("VO_SANDBOX", "0")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from valeria_one import db  # noqa: E402
from valeria_one.api import app  # noqa: E402
from valeria_one.services import businesses as B  # noqa: E402

assert "test" in os.environ["VO_DATABASE_URL"], "tests must never run against a non-test database"


@pytest.fixture(autouse=True)
def fresh_limits():
    """Rate limits are per process; each test starts with a clean slate."""
    import valeria_one.api as api_mod
    import valeria_one.web as web_mod
    api_mod._signups.clear()
    api_mod._bookings.clear()
    web_mod._book_hits.clear()
    yield


@pytest.fixture(autouse=True)
def clean_db():
    with db.tx() as conn:
        conn.execute(text("TRUNCATE businesses, api_keys, contacts, channel_accounts, contact_identities, conversations, "
                          "messages, deals, appointments, events, users, memberships, meta_leads, comment_rules, social_comments, booking_services, calendar_links, ai_settings, webhook_endpoints, webhook_deliveries, invitations, payment_accounts, payments, automations, campaigns, scheduled_messages, quotes, page_views, instance_kv RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False, base_url="https://testserver")


def make_business(country="MX", industry="generic", sandbox=False, name=None):
    with db.tx() as conn:
        biz = B.create(conn, name=name or f"Test {country} {industry}", country=country, industry=industry, sandbox=sandbox)
        key, _ = B.new_key(conn, biz["id"])
    return biz, key


@pytest.fixture
def mx(client):
    biz, key = make_business("MX", "salon")
    return biz, {"Authorization": f"Bearer {key}"}


@pytest.fixture
def us(client):
    biz, key = make_business("US", "contractor")
    return biz, {"Authorization": f"Bearer {key}"}
