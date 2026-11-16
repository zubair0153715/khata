"""KHATA tests: intake parsing, matching/verification, summary math, API."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

TMP = tempfile.mkdtemp()
os.environ["KHATA_DB"] = os.path.join(TMP, "test.db")

from app.main import parse_sms, detect_method  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------ parsing
def test_parse_raast_sms():
    p = parse_sms("Raast: Rs. 1,500 received from Ali. Ref: RAAST123456. 17/11/2026 14:30")
    assert p["amount"] == 1500.0
    assert p["sender_name"] == "Ali"
    assert p["reference"] == "RAAST123456"
    assert p["method"] == "Raast"


def test_parse_easypaisa_commas():
    p = parse_sms("Easypaisa: You have received PKR 12,750 from Fatima Khan. Txn ID EP98765")
    assert p["amount"] == 12750.0
    assert p["method"] == "Easypaisa"
    assert p["reference"] == "EP98765"


def test_parse_jazzcash_no_ref():
    p = parse_sms("JazzCash: Rs. 850 received from Bilal")
    assert p["amount"] == 850.0
    assert p["method"] == "JazzCash"
    assert p["reference"] == ""


def test_parse_no_amount_rejected():
    with pytest.raises(ValueError):
        parse_sms("Hello, your payment is on the way.")


def test_detect_method_unknown():
    assert detect_method("Bank transfer received Rs. 500") == "Unknown"


# ------------------------------------------------------- API: intake
def test_intake_matching_bill_is_verified(client):
    # DEMO seed has open bill: Ali, Rs. 1500
    r = client.post("/api/payments/intake", json={
        "sms_text": "Raast: Rs. 1,500 received from Ali. Ref: RAAST123456."})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "VERIFIED"
    assert body["matched_bill"]["customer_name"] == "Ali"
    assert "Ali ki taraf se receive ho gaye" in body["alert_message"]


def test_intake_no_match_is_unverified(client):
    r = client.post("/api/payments/intake", json={
        "sms_text": "JazzCash: Rs. 9,999 received from Unknown Person. Ref: JC0001."})
    assert r.status_code == 201
    assert r.json()["status"] == "UNVERIFIED"
    assert r.json()["matched_bill"] is None


def test_intake_webhook_json(client):
    # DEMO seed bill: Fatima Rs. 2750
    r = client.post("/api/payments/intake", json={
        "amount": 2750, "sender_name": "Fatima",
        "reference": "EP-111", "method": "Easypaisa"})
    assert r.status_code == 201
    assert r.json()["status"] == "VERIFIED"


def test_intake_wrong_amount_not_verified(client):
    # Same name, wrong amount -> must NOT verify
    r = client.post("/api/payments/intake", json={
        "amount": 1501, "sender_name": "Ali",
        "reference": "X", "method": "Raast"})
    assert r.status_code == 201
    assert r.json()["status"] == "UNVERIFIED"


def test_intake_empty_rejected(client):
    r = client.post("/api/payments/intake", json={})
    assert r.status_code == 422


def test_intake_sms_without_amount_rejected(client):
    r = client.post("/api/payments/intake",
                    json={"sms_text": "payment received, thanks"})
    assert r.status_code == 422


# ------------------------------------------------------- bills + ledger
def test_create_bill_and_search(client):
    r = client.post("/api/bills", json={
        "customer_name": "Test Customer", "amount": 4321,
        "item": "Test Item", "quantity": 3, "reference": "BILL-TEST-1"})
    assert r.status_code == 201
    assert r.json()["status"] == "open"
    r = client.get("/api/bills", params={"q": "Test Customer"})
    assert any(b["reference"] == "BILL-TEST-1" for b in r.json())


def test_payment_search_by_amount(client):
    r = client.get("/api/payments", params={"q": "9999"})
    assert r.status_code == 200
    assert any(p["amount"] == 9999.0 for p in r.json())


def test_payment_filter_unverified(client):
    r = client.get("/api/payments", params={"status": "UNVERIFIED"})
    assert r.status_code == 200
    assert all(p["status"] == "UNVERIFIED" for p in r.json())
    assert len(r.json()) >= 1


# ------------------------------------------------------------ summary
def test_summary_math(client):
    # Payments recorded above on today's date: 1500 (Raast, verified) +
    # 9999 (JazzCash, unverified) + 2750 (Easypaisa, verified) + 1501 (Raast)
    today = __import__("datetime").date.today().isoformat()
    r = client.get("/api/summary", params={"day": today})
    assert r.status_code == 200
    s = r.json()
    assert s["total_digital_sales"] == 1500 + 9999 + 2750 + 1501
    assert s["verified_total"] == 1500 + 2750
    assert s["unverified_total"] == 9999 + 1501
    assert s["per_method"]["Raast"] == 1500 + 1501
    assert s["per_method"]["JazzCash"] == 9999
    assert s["per_method"]["Easypaisa"] == 2750
    assert s["tax_estimate"] == round(s["total_digital_sales"] * 0.05, 2)
    # Stock outflow: matched bills paid today: Ali 1 unit + Fatima 1 unit
    assert s["stock_outflow_units"] == 2
    assert s["bills_paid_today"] == 2


def test_summary_empty_day(client):
    r = client.get("/api/summary", params={"day": "2001-01-01"})
    assert r.status_code == 200
    assert r.json()["total_digital_sales"] == 0
    assert r.json()["per_method"] == {}


def test_summary_bad_date(client):
    assert client.get("/api/summary", params={"day": "not-a-date"}).status_code == 422


# ------------------------------------------------------ whatsapp hook
def test_whatsapp_send_is_simulated_without_token(client):
    r = client.post("/api/whatsapp/send", json={
        "to": "+923001234567",
        "message": "Rs. 1,500 Ali ki taraf se receive ho gaye."})
    assert r.status_code == 200
    assert r.json()["simulated"] is True


def test_health_and_homepage(client):
    assert client.get("/api/health").json()["ok"] is True
    r = client.get("/")
    assert r.status_code == 200
    assert "screenshot alone proves nothing" in r.text.lower()
    assert "SIMULATOR" in r.text
