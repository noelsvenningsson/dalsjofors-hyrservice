# Dalsjofors Hyrservice

Lightweight trailer booking service built with Python standard library + SQLite.

## Release Readiness

This repository is prepared for handover with:
- Runtime env template: `.env.example`
- API quick reference (below)
- Migration notes for recent schema/behavior changes
- Smoke test checklist: `SMOKE_TEST_CHECKLIST.md`

## Local Setup (WSL/Ubuntu)

Prerequisites:
- Ubuntu (native or WSL)
- Python 3.11+

From project root:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
```

No third-party Python packages are required in this project.

## Install / Run / Test Commands

Initialize database (safe to run multiple times):

```bash
python3 -c "import db; db.init_db(); print('database initialized')"
```

Start app:

```bash
python3 app.py
```

Run tests:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

Default local URL: `http://localhost:8000`

## Environment Variables

Use `.env.example` as the source of truth.

- `PORT`: HTTP listen port (default `8000`)
- `SWISH_MODE`: `mock` (default) eller `live` (callback-stubben returnerar 501 i live tills cert-verifiering är implementerad)
- `SWISH_COMMERCE_MERCHANT_ALIAS`: Swish Commerce merchant alias
- `SWISH_COMMERCE_CERT_PATH`: path to Swish Commerce client certificate
- `SWISH_COMMERCE_KEY_PATH`: path to Swish Commerce private key
- `SWISH_COMMERCE_CALLBACK_URL`: optional explicit callback URL for Swish (`/api/swish/callback` is used by default)
- `NOTIFY_WEBHOOK_URL`: webhook endpoint för e-postkvitto (Google Apps Script)
- `REPORT_WEBHOOK_URL`: webhook endpoint för fel-/skaderapport (prioriteras för `POST /report-issue`)
  - Fallback för rapporter: `NOTIFY_WEBHOOK_URL`
- `NOTIFY_WEBHOOK_SECRET`: delas i webhook-payload som `secret`
  - Rotera omedelbart om den läckt
- `TWILIO_ACCOUNT_SID`: Twilio Account SID (optional, used for SMS on PAID)
- `TWILIO_AUTH_TOKEN`: Twilio Auth Token
- `TWILIO_FROM_NUMBER`: Twilio sender number in E.164 format
- `ADMIN_SMS_NUMBER`: admin mobile (default `0709663485`, normalized internally)
- `ADMIN_TOKEN`: admin auth token for `/api/dev/*` and `/api/admin/*` (`X-Admin-Token`)
  - Required for admin/dev API endpoints in all environments
- `ADMIN_PASSWORD`: admin password for browser login at `/admin/login`
  - Required in production
- `ADMIN_SESSION_SECRET`: HMAC secret for signed admin session cookies (`/admin/login`)
  - Required if browser login/session auth should be enabled
  - Keep this secret unique per environment
- `REPORT_TO`: mottagaradress för rapporter (default: `svenningsson@outlook.com`)

## Deployment Notes (Render)

Recommended service type:
- Render Web Service (Python)

Suggested settings:
- Build command: `echo "No build step required"`
- Start command: `python3 app.py`
- Python version: 3.11+
- Health check path: `/api/health`

Environment variables in Render:
- `PORT` is provided by Render
- Set `SWISH_COMMERCE_MERCHANT_ALIAS`, `SWISH_COMMERCE_CERT_PATH`, `SWISH_COMMERCE_KEY_PATH` in Render dashboard when enabling Swish Commerce
- Set `ADMIN_TOKEN` (required for production)
- Set `ADMIN_PASSWORD` (required for production browser admin login)
- Set `ADMIN_SESSION_SECRET` (required for `/admin/login` session cookies)

SQLite note:
- App stores data in local `database.db`.
- On Render, attach a persistent disk if you need data durability across deploys/restarts.

## Migration Notes

Recent migrations and behavior updates are auto-applied by `db.init_db()`:

1. `booking_reference`
- New `bookings.booking_reference` column
- Unique index on non-null references
- New bookings now return `bookingReference` in API responses (for example `POST /api/hold`)

2. Admin blocks
- New `admin_blocks` table
- `POST /api/admin/blocks` accepts both:
  - canonical: `startDatetime` / `endDatetime`
  - legacy aliases: `start` / `end`
- If both styles are provided, canonical fields win

3. Pending expiration
- `bookings.expires_at` tracks pending-payment timeout
- Expired `PENDING_PAYMENT` bookings are cancelled automatically during request handling
- Manual cleanup endpoint: `POST /api/admin/expire-pending`

4. Heldag weekday/weekend+holiday pricing and SMS idempotency
- `config/holidays.py` contains configurable holiday dates (`YYYY-MM-DD`)
- Heldag: weekday `250`, weekend/holiday `300`
- `bookings.customer_phone_temp` stores optional customer mobile temporarily
- `bookings.sms_admin_sent_at` and `bookings.sms_customer_sent_at` enforce one-time SMS dispatch
- Customer number is cleared after successful customer receipt SMS
- Customer number is also cleared when a booking becomes `CANCELLED` (including expiry cleanup)

5. E-postkvitto via webhook (Apps Script)
- `bookings.customer_email_temp` och `bookings.receipt_requested_temp` lagrar kvittoönskemål till betalning är klar
- Webhook triggas endast vid `PAID`/`CONFIRMED` och bara om `receipt_requested_temp=1` och `customer_email_temp` finns
- Vid webhook-svar `HTTP 200` med body som innehåller `ok` rensas tempfälten för att undvika spam vid retries

6. Fel/skaderapport
- Ny publik sida: `GET /report-issue`
- Formulär skickas till `POST /report-issue` med `multipart/form-data`
- Stöd för 0-6 bilder (`jpg/png/webp`, max 5 MB/st), server-side validering och webhook för utskick
- Rapport skickas till `REPORT_WEBHOOK_URL` (fallback `NOTIFY_WEBHOOK_URL`) med JSON-payload inklusive:
  - `friendlyFields` (svenska etiketter för mailrendering)
  - `reportId` (UUID) och `submittedAt` (ISO-tid)
  - `attachmentNames` + `attachmentCount`
  - `secret` (`NOTIFY_WEBHOOK_SECRET`) och upp till 3 base64-bilder
- Om webhook-payload blir för stor skickas rapporten utan bilder och med varningstext i meddelandet
- Enkel anti-spam via honeypot + rate limit per IP
- Vid saknad/felande webhook loggas `REPORT_WEBHOOK_SEND_FAILED` och användaren får ett generiskt felmeddelande

Google Apps Script-exempel för webhook finns i `scripts/google_apps_script_webhook.gs`.

## API Quick Reference

All responses are JSON.

Admin auth:
- `/admin` requires a valid signed `admin_session` cookie.
- `/api/dev/*` and `/api/admin/*` require `X-Admin-Token: <ADMIN_TOKEN>`.
- `Authorization: Bearer <ADMIN_TOKEN>` is still accepted for backward compatibility.
- Browser login endpoints:
  - `GET /admin/login` (HTML form)
  - `POST /admin/login` (creates signed session cookie, ~8h max age)
  - `POST /admin/logout` (clears session cookie)
- Missing or invalid API auth returns `401` for `/api/admin/*`.

Error responses use a stable structure with a legacy-compatible string:

```json
{
  "error": "legacy error message",
  "errorInfo": {
    "code": "invalid_request",
    "message": "Invalid request",
    "details": {
      "fields": {
        "date": "Expected format YYYY-MM-DD"
      }
    }
  }
}
```

### `GET /api/price`

Required inputs:
- Query params: `rentalType`, `date`
- Optional query param: `trailerType` (defaults to `GALLER` if omitted)

Example success (`200`):

```json
{
  "price": 250,
  "dayTypeLabel": "Vardag"
}
```

Common errors:
- `400 {"error":"rentalType and date are required"}`
- `400 {"error":"Invalid rentalType"}`

### `GET /api/availability`

Required inputs:
- Query params: `trailerType`, `rentalType`, `date`
- For `TWO_HOURS`, `startTime` is required

Example success (`200`):

```json
{
  "available": true,
  "remaining": 1
}
```

Common errors:
- `400 {"error":"trailerType, rentalType and date are required"}`
- `400 {"error":"startTime required for TWO_HOURS"}`
- `400 {"error":"Invalid rentalType"}`

### `POST /api/hold` (booking create)

Required inputs (JSON body):
- `trailerType`
- `rentalType`
- `date`
- `startTime` (required for `TWO_HOURS`)
- `customerPhone` (optional, Swedish mobile for receipt SMS: `+46...` or `07...`)
- `receiptRequested` (optional boolean, default `false`)
- `customerEmail` (optional string; required only when `receiptRequested=true`)

Example success (`201`):

```json
{
  "bookingId": 123,
  "bookingReference": "DHS-20260510-000123",
  "createdAt": "2026-05-10T09:15:33",
  "price": 200
}
```

## SMS Provider (Twilio)

SMS is sent when a booking reaches `swish_status=PAID`:
- Admin SMS: one-time (`sms_admin_sent_at`)
- Customer receipt SMS (if optional number was provided): one-time (`sms_customer_sent_at`)

If Twilio env vars are missing, app logs clearly and continues without crashing.

## E-postkvitto via Webhook (Google Apps Script)

När en bokning blir `PAID` skickar backend webhook endast om kunden aktivt begärt kvitto:
- `receipt_requested_temp = 1`
- `customer_email_temp` finns

Payload till `NOTIFY_WEBHOOK_URL`:

```json
{
  "secret": "...",
  "receiptRequested": true,
  "customerEmail": "kund@example.com",
  "event": "booking.confirmed",
  "bookingId": 123,
  "bookingReference": "DHS-20260510-000123",
  "trailerType": "GALLER",
  "startDt": "2026-05-10T10:00",
  "endDt": "2026-05-10T12:00",
  "price": 200,
  "swishStatus": "PAID"
}
```

Loggar:
- `WEBHOOK_DISABLED` (saknar `NOTIFY_WEBHOOK_URL`)
- `WEBHOOK_SEND` (event + bookingReference + maskad e-post)
- `WEBHOOK_OK` / `WEBHOOK_FAIL` (status + kort fel)

Säkerhet:
- Rotera `NOTIFY_WEBHOOK_SECRET` direkt om den misstänks ha läckt.

Quick local test:

```bash
python3 -c "import sms_provider; print(sms_provider.send_sms('+46701234567','Test från DHS'))"
```

## Manual Test Checklist

- Weekday date + heldag -> `250`
- Saturday/Sunday + heldag -> `300`
- Date in `config/holidays.py` + heldag -> `300`
- `PAID` transition -> admin SMS sent exactly once
- Customer mobile provided -> customer SMS sent; `customer_phone_temp` cleared after success
- Simulated SMS failure -> retry can send later; no duplicate send after success

Common errors:
- `400 {"error":"Invalid JSON"}`
- `400 {"error":"trailerType, rentalType and date are required"}`
- `409 {"error":"slot taken"}`
- `409 {"error":"slot blocked","message":"Requested slot overlaps an admin block","block":{...}}`

## Swish Mock Smoke Tests

All commands assume local server at `http://localhost:8000` and `SWISH_MODE=mock`.

Create a hold:

```bash
BOOKING_ID=$(curl -sS -X POST http://localhost:8000/api/hold \
  -H 'Content-Type: application/json' \
  -d '{"trailerType":"GALLER","rentalType":"TWO_HOURS","date":"2026-02-20","startTime":"10:00"}' | jq -r '.bookingId')
echo "$BOOKING_ID"
```

Create or reuse payment request (idempotent):

```bash
curl -sS -X POST "http://localhost:8000/api/swish/paymentrequest?bookingId=${BOOKING_ID}" | jq
curl -sS -X POST "http://localhost:8000/api/swish/paymentrequest?bookingId=${BOOKING_ID}" | jq
```

Debug existing booking row (example with `bookingId=4`) without enabling noisy logs globally:

```bash
DEBUG_SWISH=1 curl -sS -X POST "http://localhost:8000/api/swish/paymentrequest?bookingId=4" | jq
```

Fetch payment status:

```bash
curl -sS "http://localhost:8000/api/payment-status?bookingId=${BOOKING_ID}" | jq
```

Mark as paid in mock and verify poll endpoint response:

```bash
curl -sS -X POST "http://localhost:8000/api/dev/swish/mark?bookingId=${BOOKING_ID}&status=PAID" | jq
curl -sS "http://localhost:8000/api/payment-status?bookingId=${BOOKING_ID}" | jq
```

Webhook-kvitto test (med `receiptRequested=true`):

```bash
BOOKING_ID=$(curl -sS -X POST http://localhost:8000/api/hold \
  -H 'Content-Type: application/json' \
  -d '{"trailerType":"GALLER","rentalType":"TWO_HOURS","date":"2026-02-20","startTime":"10:00","receiptRequested":true,"customerEmail":"din.mail@example.com"}' | jq -r '.bookingId')

curl -sS -X POST "http://localhost:8000/api/dev/swish/mark?bookingId=${BOOKING_ID}&status=PAID" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" | jq
```

Render testflöde:

```bash
APP_URL="https://<din-render-service>.onrender.com"
ADMIN_TOKEN="<din-admin-token>"

BOOKING_ID=$(curl -sS -X POST "${APP_URL}/api/hold" \
  -H 'Content-Type: application/json' \
  -d '{"trailerType":"GALLER","rentalType":"TWO_HOURS","date":"2026-02-20","startTime":"10:00","receiptRequested":true,"customerEmail":"din.mail@example.com"}' | jq -r '.bookingId')

curl -sS -X POST "${APP_URL}/api/dev/swish/mark?bookingId=${BOOKING_ID}&status=PAID" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" | jq
```

Render testflöde utan `jq` (minimal bash/WSL):

```bash
APP_URL="https://<din-render-service>.onrender.com"
ADMIN_TOKEN="<din-admin-token>"

HOLD_RESP=$(curl -sS -X POST "${APP_URL}/api/hold" \
  -H 'Content-Type: application/json' \
  -d '{"trailerType":"GALLER","rentalType":"TWO_HOURS","date":"2026-02-20","startTime":"10:00","receiptRequested":true,"customerEmail":"din.mail@example.com"}')
echo "${HOLD_RESP}"

BOOKING_ID=$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("bookingId",""))' <<< "${HOLD_RESP}")
echo "bookingId=${BOOKING_ID}"

curl -sS -X POST "${APP_URL}/api/dev/swish/mark?bookingId=${BOOKING_ID}&status=PAID" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}"
```

Förväntade loggar i Render:
- `WEBHOOK_SEND event=booking.confirmed ... customerEmail=n***@...`
- `WEBHOOK_OK status=200 ...`

QR endpoint should return SVG image when token exists:

```bash
curl -sS -I "http://localhost:8000/api/swish/qr?bookingId=${BOOKING_ID}"
```

### `POST /api/admin/blocks`

Required inputs (JSON body):
- `trailerType`
- Datetime range using either:
  - `startDatetime` + `endDatetime`, or
  - `start` + `end`

Required header:
- `X-Admin-Token: <ADMIN_TOKEN>`

Example success (`201`):

```json
{
  "id": 7,
  "trailerType": "KAP",
  "startDatetime": "2026-05-04T08:00",
  "endDatetime": "2026-05-04T09:00",
  "reason": "Maintenance",
  "createdAt": "2026-02-13T22:00:00"
}
```

Common errors:
- `400 {"error":"trailerType and datetime range are required; use startDatetime/endDatetime or start/end"}`
- `400 {"error":"Invalid datetime format; expected ISO 8601 in startDatetime/endDatetime or start/end"}`
- `400 {"error":"endDatetime must be after startDatetime"}`

### `GET /api/admin/blocks`

Required inputs:
- None
- Optional query params: `startDatetime`, `endDatetime` (for range filtering)
Required header:
- `X-Admin-Token: <ADMIN_TOKEN>`

Example success (`200`):

```json
{
  "blocks": [
    {
      "id": 7,
      "trailerType": "KAP",
      "startDatetime": "2026-05-04T08:00",
      "endDatetime": "2026-05-04T09:00",
      "reason": "Maintenance",
      "createdAt": "2026-02-13T22:00:00"
    }
  ]
}
```

Common errors:
- `400 {"error":"Invalid datetime format"}`
- `400 {"error":"endDatetime must be after startDatetime"}`

### `DELETE /api/admin/blocks`

Required inputs:
- Query param: `id` (integer)
Required header:
- `X-Admin-Token: <ADMIN_TOKEN>`

Example success (`200`):

```json
{
  "deleted": true,
  "id": 7
}
```

Common errors:
- `400 {"error":"id is required"}`
- `400 {"error":"id must be an integer"}`
- `404 {"error":"Block not found"}`

### `GET /report-issue`

Visar formuläret för fel-/skaderapport.

### `POST /report-issue`

Tar emot rapporten via `multipart/form-data` och skickar JSON till rapport-webhook (`REPORT_WEBHOOK_URL`, fallback `NOTIFY_WEBHOOK_URL`).

Formfält:
- Obligatoriska: `name`, `phone`, `email`, `trailer_type`, `detected_at`, `report_type`, `message`
- Valfritt: `booking_reference`
- Bilder: `images` (0-6 filer, `jpg/png/webp`, max 5 MB per fil)

### `GET /api/health`

Required inputs:
- None

Example success (`200`):

```json
{
  "ok": true,
  "service": "dalsjofors-hyrservice",
  "time": "2026-02-13T22:15:00"
}
```

Common errors:
- Not expected in normal operation

## Smoke Test

See `SMOKE_TEST_CHECKLIST.md`.

## Database Backup

Use the helper script to create a timestamped SQLite backup:

```bash
./scripts/backup_db.sh
```

Backups are stored in `backups/` with names like `database_20260213_221500.db`.
