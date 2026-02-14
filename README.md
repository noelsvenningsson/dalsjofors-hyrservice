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
- `SWISH_COMMERCE_MERCHANT_ALIAS`: Swish Commerce merchant alias
- `SWISH_COMMERCE_CERT_PATH`: path to Swish Commerce client certificate
- `SWISH_COMMERCE_KEY_PATH`: path to Swish Commerce private key
- `SWISH_COMMERCE_CALLBACK_URL`: optional explicit callback URL for Swish (`/api/swish/callback` is used by default)
- `NOTIFY_WEBHOOK_URL`: optional webhook endpoint for booking notifications
- `NOTIFY_WEBHOOK_SECRET`: optional HMAC secret for webhook signature header
- `ADMIN_TOKEN`: admin auth token for `/admin` and `/api/admin/*`
  - Required in production
  - Optional in local development (server logs a warning and admin auth is disabled)
- `ADMIN_SESSION_SECRET`: HMAC secret for signed admin session cookies (`/admin/login`)
  - Required if browser login/session auth should be enabled
  - Keep this secret unique per environment

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

## API Quick Reference

All responses are JSON.

Admin auth:
- `/admin` and all `/api/admin/*` endpoints are protected when `ADMIN_TOKEN` is configured.
- Access is allowed if either:
  - `X-Admin-Token: <ADMIN_TOKEN>` header matches, or
  - a valid signed `admin_session` cookie exists (created by `POST /admin/login`)
- Browser login endpoints:
  - `GET /admin/login` (HTML form)
  - `POST /admin/login` (creates signed session cookie, ~8h max age)
  - `POST /admin/logout` (clears session cookie)
- Missing or invalid auth returns `401` for `/api/admin/*`.

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
  "price": 250
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

Example success (`201`):

```json
{
  "bookingId": 123,
  "bookingReference": "DHS-20260510-000123",
  "price": 200
}
```

Common errors:
- `400 {"error":"Invalid JSON"}`
- `400 {"error":"trailerType, rentalType and date are required"}`
- `409 {"error":"slot taken"}`
- `409 {"error":"slot blocked","message":"Requested slot overlaps an admin block","block":{...}}`

### `POST /api/admin/blocks`

Required inputs (JSON body):
- `trailerType`
- Datetime range using either:
  - `startDatetime` + `endDatetime`, or
  - `start` + `end`

Required header:
- `X-Admin-Token`

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
- `X-Admin-Token`

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
- `X-Admin-Token`

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
