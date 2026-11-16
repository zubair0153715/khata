# KHATA (کھاتا) — WhatsApp AI Payment & Reconciliation Agent

A working payment-intake, verification and nightly-summary app for small
merchants in Islamabad. FastAPI + SQLite + plain HTML/JS (no build step).
Zero budget, no paid service, nothing sent outbound by default.

## What it does

1. **Payment intake** — `POST /api/payments/intake` accepts:
   - (a) pasted/forwarded bank or wallet **SMS text** (Raast / Easypaisa / JazzCash),
   - (b) generic **payment-webhook JSON** (`amount`, `sender_name`, `reference`, `method`).
   It parses amount, sender name, reference, method and timestamp.
2. **Verification** — every payment is matched against the open bills/dues the
   merchant recorded in the app. Same amount **and** same customer name (or
   same reference) → `VERIFIED`. Anything else → `UNVERIFIED`.
   A payment is only "verified" when it matches a real record the system
   received. **A screenshot alone proves nothing** — screenshots can be edited
   in seconds, so KHATA never marks a payment verified from a screenshot. The
   dashboard says this visibly.
3. **Instant merchant alert** — on every payment the app renders a
   WhatsApp-style message in Roman Urdu, e.g.
   *"Rs. 1,500 Ali ki taraf se receive ho gaye."*, with a voice-note button
   (browser text-to-speech). Real WhatsApp sending is a **simulator** by
   default; the real Meta Cloud API send hook exists behind
   `WHATSAPP_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` (see Honest limits).
4. **Nightly auto-summary** — `GET /api/summary?day=YYYY-MM-DD` (and the
   dashboard panel) computes from real recorded data: total digital sales,
   per-method split (Raast / Easypaisa / JazzCash), verified vs unverified
   totals, tax estimate, and stock outflow (units + items from bills paid
   that day).
5. **Merchant dashboard + khata (ledger)** — record bills/dues, record
   payments, see matched/unverified status, search by customer, amount or
   reference.

## Run locally

```bash
git clone https://github.com/zubair0153715/khata.git
cd khata
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

Demo data is seeded on first run only and is labelled **DEMO** everywhere
(demo merchant: Ahmed General Store, Islamabad; demo bills for Ali, Fatima,
Bilal). Delete `khata.db` to start empty.

### Try it

```bash
# SMS intake — matches the DEMO bill (Ali, Rs. 1,500) -> VERIFIED
curl -X POST http://127.0.0.1:8000/api/payments/intake \
  -H 'Content-Type: application/json' \
  -d '{"sms_text": "Raast: Rs. 1,500 received from Ali. Ref: RAAST123456."}'

# Webhook JSON intake — no matching bill -> UNVERIFIED
curl -X POST http://127.0.0.1:8000/api/payments/intake \
  -H 'Content-Type: application/json' \
  -d '{"amount": 9999, "sender_name": "Unknown Person", "method": "JazzCash"}'

# Today's summary
curl http://127.0.0.1:8000/api/summary

# Tests
python -m pytest -q
```

## Context (verified facts only)

- Dawn reported on **2026-11-13** (dawn.com/news/2044285) that Islamabad will
  become Pakistan's first fully cashless city by **31 December 2026**.
- After **30 November 2026**, no cash payment will be accepted in any ICT
  administration / CDA department.
- The target is all Islamabad merchants cashless by 31 December 2026.
- The Interior Ministry statement cited in the article says **26,000
  merchants are already connected** to the digital payments system. That
  number comes from the minister's statement — it is **not** independently
  verified.

## Honest limits — what needs the owner's setup

- **Real WhatsApp sending is NOT live.** Alerts are simulated in the UI and
  logged. Real sending needs the owner's own Meta Cloud API setup:
  a Meta developer account, a WhatsApp Business number, and the env vars
  `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`. Until then nothing leaves
  this app.
- **No bank / Raast integration.** Payments arrive as pasted SMS text or as
  webhook JSON the merchant's bank/wallet forwards. KHATA does not connect to
  any bank, does not hold a Raast merchant ID, and has no government approval.
  Never claim otherwise.
- **Verification = match against bills you entered.** If a bill was never
  recorded, the payment stays UNVERIFIED — that is the system working, not
  failing.
- **Tax estimate is a flat assumed 5%** (`KHATA_TAX_RATE` env var) — an
  estimate only, not a tax ruling. Adjust it to the merchant's actual slab.
- **SMS parsing is heuristic.** Unusual message formats may fail to parse;
  the API returns a 422 with the reason instead of guessing.
- **Demo data is fake.** Every seeded merchant, bill and value is labelled
  DEMO. Delete `khata.db` for a clean start.
- SQLite, single merchant per install in v1; no auth yet — run on localhost
  or behind the owner's own login.

## Sales pitch (one page)

**Who pays:** small merchants in Islamabad — general stores, pharmacies,
restaurants, clinics — who are being pushed cashless by 31 December 2026 and
today reconcile payments by scrolling SMS screenshots and asking staff
"paisa aaya?".

**The pain:** a customer shows a screenshot, the merchant can't tell if the
money really arrived, and at night nobody knows the day's real sales, which
method brought how much, or what stock went out.

**What KHATA does:** paste the SMS (or connect the wallet webhook) and in one
second the merchant knows — verified against their own khata or flagged
unverified — plus a Roman Urdu WhatsApp-style alert with voice, and a nightly
summary: total digital sales, Raast/Easypaisa/JazzCash split, tax estimate,
stock outflow.

**How much (the owner's asking prices):** **PKR 3,000–5,000/month per merchant
subscription.** At 200 merchants that is **~PKR 6–10 lakh/month recurring
revenue**. Onboarding is a phone number and a bill book — no hardware, no
bank integration needed on day one.

**Why now:** Islamabad goes cashless by 31 Dec 2026. Every merchant in the
city needs exactly this, this month.

## Project layout

```
app/main.py            FastAPI app: intake, matching, summary, WhatsApp hook
app/static/index.html  Dashboard + khata ledger (plain HTML/JS, no build step)
tests/test_khata.py    pytest: parsing, verification, summary math, API
requirements.txt       fastapi[standard], uvicorn, httpx, pytest
```
