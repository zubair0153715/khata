"""KHATA (کھاتا) — WhatsApp AI Payment & Reconciliation Agent for small merchants.

FastAPI + SQLite + plain HTML/JS. No paid services, nothing outbound by
default: WhatsApp sending is a SIMULATOR unless the owner sets
WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID (their own Meta Cloud API setup).

Honesty rules baked into the code:
- A payment is VERIFIED only when it matches a real bill/dua record the
  merchant entered in this system. Nothing else verifies a payment.
- A screenshot alone NEVER verifies a payment. The UI says so.
- Every seed/demo value is labelled DEMO.
"""
import os
import re
import sqlite3
import urllib.request
import json as _json
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("KHATA_DB", BASE_DIR / "khata.db"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Tax estimate is an ASSUMPTION, not a tax ruling. Owner adjusts it.
TAX_ESTIMATE_RATE = float(os.environ.get("KHATA_TAX_RATE", "0.05"))
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")

METHODS = ("Raast", "Easypaisa", "JazzCash")


# ---------------------------------------------------------------- DB
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS merchants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city TEXT DEFAULT 'Islamabad',
                is_demo INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_id INTEGER NOT NULL,
                customer_name TEXT NOT NULL,
                amount REAL NOT NULL,
                item TEXT DEFAULT '',
                quantity INTEGER DEFAULT 1,
                reference TEXT DEFAULT '',
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                sender_name TEXT DEFAULT '',
                reference TEXT DEFAULT '',
                method TEXT DEFAULT 'Unknown',
                raw_text TEXT DEFAULT '',
                source TEXT DEFAULT 'sms',
                status TEXT DEFAULT 'UNVERIFIED',
                matched_bill_id INTEGER,
                received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS whatsapp_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id INTEGER,
                message TEXT NOT NULL,
                simulated INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            );
            """
        )


def seed_demo():
    """Clearly-labelled DEMO data only. Runs only on an empty DB."""
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0]:
            return
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO merchants (name, city, is_demo, created_at)"
            " VALUES (?, ?, 1, ?)",
            ("DEMO — Ahmed General Store", "Islamabad", now),
        )
        mid = cur.lastrowid
        demo_bills = [
            ("Ali", 1500.0, "Atta 10kg", 1, "DEMO-BILL-001"),
            ("Fatima", 2750.0, "Ghee 5kg", 1, "DEMO-BILL-002"),
            ("Bilal", 850.0, "Chai patti 1kg", 2, "DEMO-BILL-003"),
        ]
        for name, amt, item, qty, ref in demo_bills:
            conn.execute(
                "INSERT INTO bills (merchant_id, customer_name, amount, item,"
                " quantity, reference, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mid, name, amt, item, qty, ref, now),
            )


# ------------------------------------------------------------- parsing
AMOUNT_RE = re.compile(
    r"(?:Rs\.?|PKR|Rs)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE
)
REF_RE = re.compile(
    r"(?:Ref(?:erence)?(?:\s*No\.?)?|Txn(?:\s*ID)?|Transaction\s*ID|TRX"
    r"|Tracking\s*ID)\s*[:#]?\s*([A-Za-z0-9\-]{4,})",
    re.IGNORECASE,
)
FROM_RE = re.compile(
    r"(?:from|by|sender\s*:)\s+([A-Za-z][A-Za-z .]{1,40}?)(?:\s+to|\s+on|\s*,"
    r"|\s*\.|\s*Rs|\s*PKR|\s*$)",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)"
    r"|(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?)"
)


def detect_method(text: str) -> str:
    low = text.lower()
    if "raast" in low:
        return "Raast"
    if "easypaisa" in low or "easy paisa" in low:
        return "Easypaisa"
    if "jazzcash" in low or "jazz cash" in low:
        return "JazzCash"
    return "Unknown"


def parse_sms(text: str) -> dict:
    """Parse a pasted/forwarded bank or wallet SMS into payment fields."""
    m = AMOUNT_RE.search(text or "")
    if not m:
        raise ValueError("No amount (Rs.) found in the message text.")
    amount = float(m.group(1).replace(",", ""))
    ref = REF_RE.search(text)
    sender = FROM_RE.search(text)
    ts = DATE_RE.search(text)
    received_at = None
    if ts:
        raw = ts.group(1) or ts.group(2)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                received_at = datetime.strptime(raw, fmt).isoformat()
                break
            except ValueError:
                continue
    return {
        "amount": amount,
        "sender_name": sender.group(1).strip() if sender else "",
        "reference": ref.group(1) if ref else "",
        "method": detect_method(text),
        "received_at": received_at or datetime.now(timezone.utc).isoformat(),
    }


def names_match(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def match_payment(conn, merchant_id: int, amount: float,
                  sender_name: str, reference: str):
    """A payment is VERIFIED only if it matches a real open bill.

    Match rule: same amount AND (same customer name OR same reference).
    Anything else stays UNVERIFIED.
    """
    rows = conn.execute(
        "SELECT * FROM bills WHERE merchant_id = ? AND status = 'open'",
        (merchant_id,),
    ).fetchall()
    for bill in rows:
        if abs(bill["amount"] - amount) > 0.005:
            continue
        if names_match(sender_name, bill["customer_name"]):
            return bill
        if reference and bill["reference"] and \
                reference.lower() == bill["reference"].lower():
            return bill
    return None


def roman_urdu_alert(amount: float, sender: str, method: str,
                     status: str, reference: str) -> str:
    who = sender or "customer"
    msg = (f"Rs. {amount:,.0f} {who} ki taraf se receive ho gaye. "
           f"Method: {method}.")
    if reference:
        msg += f" Ref: {reference}."
    msg += (" Status: VERIFIED — bill se match ho gaya."
            if status == "VERIFIED" else
            " Status: UNVERIFIED — koi matching bill nahi mila, check karein.")
    return msg


# -------------------------------------------------------------- models
class BillIn(BaseModel):
    merchant_id: int = 1
    customer_name: str
    amount: float = Field(gt=0)
    item: str = ""
    quantity: int = Field(default=1, ge=1)
    reference: str = ""


class PaymentIn(BaseModel):
    merchant_id: int = 1
    amount: float = Field(gt=0)
    sender_name: str = ""
    reference: str = ""
    method: str = "Unknown"
    source: str = "manual"
    raw_text: str = ""
    received_at: str = ""


class IntakeIn(BaseModel):
    merchant_id: int = 1
    # (a) SMS text
    sms_text: str = ""
    # (b) generic payment-webhook JSON
    amount: float | None = None
    sender_name: str = ""
    reference: str = ""
    method: str = ""
    received_at: str = ""


class WhatsappSendIn(BaseModel):
    to: str = ""
    message: str
    payment_id: int | None = None


# ----------------------------------------------------------------- app
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    init_db()
    seed_demo()
    yield


app = FastAPI(title="KHATA — کھاتا", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "whatsapp_simulator": not bool(WHATSAPP_TOKEN),
            "whatsapp_real_configured": bool(WHATSAPP_TOKEN)}


def row_to_dict(row):
    return dict(row) if row is not None else None


@app.get("/api/merchants")
def list_merchants():
    with get_conn() as conn:
        return [row_to_dict(r) for r in
                conn.execute("SELECT * FROM merchants ORDER BY id")]


@app.post("/api/bills", status_code=201)
def create_bill(bill: BillIn):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bills (merchant_id, customer_name, amount, item,"
            " quantity, reference, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bill.merchant_id, bill.customer_name, bill.amount, bill.item,
             bill.quantity, bill.reference, now),
        )
        row = conn.execute("SELECT * FROM bills WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.get("/api/bills")
def list_bills(status: str = "", q: str = ""):
    with get_conn() as conn:
        sql = "SELECT * FROM bills WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if q:
            sql += (" AND (customer_name LIKE ? OR reference LIKE ?"
                    " OR item LIKE ?)")
            args += [f"%{q}%"] * 3
        sql += " ORDER BY id DESC"
        return [row_to_dict(r) for r in conn.execute(sql, args)]


def _record_payment(data: dict) -> dict:
    """Shared core: match, store, alert. Returns payment + alert."""
    with get_conn() as conn:
        bill = match_payment(conn, data["merchant_id"], data["amount"],
                             data.get("sender_name", ""),
                             data.get("reference", ""))
        status = "VERIFIED" if bill else "UNVERIFIED"
        now = data.get("received_at") or datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO payments (merchant_id, amount, sender_name,"
            " reference, method, raw_text, source, status, matched_bill_id,"
            " received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data["merchant_id"], data["amount"],
             data.get("sender_name", ""), data.get("reference", ""),
             data.get("method", "Unknown"), data.get("raw_text", ""),
             data.get("source", "manual"), status,
             bill["id"] if bill else None, now),
        )
        pid = cur.lastrowid
        if bill:
            conn.execute(
                "UPDATE bills SET status='paid', paid_at=? WHERE id=?",
                (now, bill["id"]),
            )
            bill = conn.execute("SELECT * FROM bills WHERE id = ?",
                                (bill["id"],)).fetchone()
        alert = roman_urdu_alert(data["amount"], data.get("sender_name", ""),
                                 data.get("method", "Unknown"), status,
                                 data.get("reference", ""))
        conn.execute(
            "INSERT INTO whatsapp_log (payment_id, message, simulated,"
            " created_at) VALUES (?, ?, 1, ?)",
            (pid, alert, now),
        )
        row = conn.execute("SELECT * FROM payments WHERE id = ?",
                           (pid,)).fetchone()
    result = row_to_dict(row)
    result["alert_message"] = alert
    result["matched_bill"] = row_to_dict(bill)
    return result


@app.post("/api/payments/intake", status_code=201)
def intake(payload: IntakeIn):
    """Accept (a) SMS text or (b) generic webhook JSON. Parse + verify."""
    if payload.sms_text.strip():
        try:
            parsed = parse_sms(payload.sms_text)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        data = {**parsed, "merchant_id": payload.merchant_id,
                "raw_text": payload.sms_text, "source": "sms"}
    elif payload.amount is not None:
        data = {
            "merchant_id": payload.merchant_id,
            "amount": payload.amount,
            "sender_name": payload.sender_name,
            "reference": payload.reference,
            "method": payload.method or "Unknown",
            "received_at": payload.received_at or "",
            "raw_text": "", "source": "webhook",
        }
    else:
        raise HTTPException(
            status_code=422,
            detail="Send either sms_text or a JSON body with amount.",
        )
    return _record_payment(data)


@app.post("/api/payments", status_code=201)
def create_payment(p: PaymentIn):
    return _record_payment(p.model_dump())


@app.get("/api/payments")
def list_payments(status: str = "", q: str = ""):
    with get_conn() as conn:
        sql = "SELECT * FROM payments WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if q:
            sql += (" AND (sender_name LIKE ? OR reference LIKE ?"
                    " OR CAST(amount AS TEXT) LIKE ?)")
            args += [f"%{q}%"] * 3
        sql += " ORDER BY id DESC"
        return [row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/api/summary")
def summary(day: str = ""):
    """Nightly summary computed from real recorded data only."""
    day = day or date.today().isoformat()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD")
    with get_conn() as conn:
        pays = conn.execute(
            "SELECT * FROM payments WHERE substr(received_at, 1, 10) = ?",
            (day,),
        ).fetchall()
        total = sum(p["amount"] for p in pays)
        per_method: dict[str, float] = {}
        verified_total = 0.0
        for p in pays:
            per_method[p["method"]] = per_method.get(p["method"], 0.0) \
                + p["amount"]
            if p["status"] == "VERIFIED":
                verified_total += p["amount"]
        paid_bills = conn.execute(
            "SELECT * FROM bills WHERE status='paid'"
            " AND substr(paid_at, 1, 10) = ?",
            (day,),
        ).fetchall()
        stock_outflow = sum(b["quantity"] for b in paid_bills)
        stock_items = {}
        for b in paid_bills:
            key = b["item"] or "(no item)"
            stock_items[key] = stock_items.get(key, 0) + b["quantity"]
    return {
        "date": day,
        "total_digital_sales": round(total, 2),
        "verified_total": round(verified_total, 2),
        "unverified_total": round(total - verified_total, 2),
        "payment_count": len(pays),
        "per_method": {k: round(v, 2) for k, v in per_method.items()},
        "tax_estimate": round(total * TAX_ESTIMATE_RATE, 2),
        "tax_estimate_rate": TAX_ESTIMATE_RATE,
        "tax_note": ("Estimate only, at a flat assumed rate — not a tax"
                     " ruling. Adjust KHATA_TAX_RATE to your slab."),
        "stock_outflow_units": stock_outflow,
        "stock_outflow_items": stock_items,
        "bills_paid_today": len(paid_bills),
    }


@app.get("/api/whatsapp/log")
def whatsapp_log():
    with get_conn() as conn:
        return [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM whatsapp_log ORDER BY id DESC LIMIT 50")]


@app.post("/api/whatsapp/send")
def whatsapp_send(body: WhatsappSendIn):
    """Simulator by default. Real Meta Cloud API send ONLY if the owner has
    set WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID. Never fakes a send."""
    now = datetime.now(timezone.utc).isoformat()
    if WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID and body.to:
        url = (f"https://graph.facebook.com/v21.0/"
               f"{WHATSAPP_PHONE_NUMBER_ID}/messages")
        req = urllib.request.Request(
            url,
            data=_json.dumps({
                "messaging_product": "whatsapp", "to": body.to,
                "type": "text", "text": {"body": body.message},
            }).encode(),
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = _json.loads(resp.read())
            simulated = 0
        except Exception as e:
            raise HTTPException(status_code=502,
                                detail=f"WhatsApp send failed: {e}")
    else:
        result = {"simulated": True,
                  "note": ("No WHATSAPP_TOKEN configured — message logged"
                           " as SIMULATED. Real sending needs the owner's"
                           " Meta Cloud API setup.")}
        simulated = 1
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO whatsapp_log (payment_id, message, simulated,"
            " created_at) VALUES (?, ?, ?, ?)",
            (body.payment_id, body.message, simulated, now),
        )
    return {**result, "simulated": bool(simulated), "message": body.message}
