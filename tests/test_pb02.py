"""pb02 (v0.46.0): a partner's sample site learns which business its claim link became, so its leads reach the owner."""
import pytest
from conftest import make_business
from valeria_one import db
from valeria_one.services import claims as CL
from valeria_one.services import partner as PT


@pytest.fixture
def pk(client):
    with db.tx() as conn:
        key, _ = PT.new_key(conn, "valai sample sites", "test")
    return {"Authorization": f"Bearer {key}"}


def test_claim_status_before_and_after_the_owner_claims(client, pk):
    with db.tx() as conn:
        kit = CL.create(conn, {"business_name": "Armor Gym", "country": "MX", "industry": "fitness"}, "test")
    r = client.get(f"/v1/partner/claims/{kit['code']}", headers=pk)
    assert r.status_code == 200 and r.json() == {"claimed": False, "business_id": None}
    biz, _ = make_business("MX", "fitness", name="Armor Gym")
    with db.tx() as conn:
        CL.take(conn, kit["code"], biz["id"])
    r = client.get(f"/v1/partner/claims/{kit['code']}", headers=pk).json()
    assert r["claimed"] is True and r["business_id"] == str(biz["id"]) and r["suspended"] is False
    with db.tx() as conn:
        PT.suspend(conn, "t", biz["id"], "unpaid")
    assert client.get(f"/v1/partner/claims/{kit['code']}", headers=pk).json()["suspended"] is True
    assert client.get("/v1/partner/claims/nope", headers=pk).json() == {"claimed": False, "business_id": None}
    assert client.get(f"/v1/partner/claims/{'x' * 40}", headers=pk).json()["claimed"] is False
    assert client.get(f"/v1/partner/claims/{kit['code']}").status_code == 401
