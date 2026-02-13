# Smoke Test Checklist

Use this checklist after deploys or before handover.

- [ ] Start the app
  - Run: `python3 app.py`
  - Expect: `Running Dalsjöfors Hyrservice on http://localhost:8000`

- [ ] Check health endpoint
  - Run: `curl http://localhost:8000/api/health`
  - Expect: HTTP `200` and JSON with `ok: true`

- [ ] Create a booking (hold)
  - Run:
    - `curl -X POST http://localhost:8000/api/hold -H "Content-Type: application/json" -d '{"trailerType":"GALLER","rentalType":"TWO_HOURS","date":"2026-05-10","startTime":"10:00"}'`
  - Expect: HTTP `201` with `bookingId`, `bookingReference`, `price`

- [ ] Verify booking reference
  - Expected format: `DHS-YYYYMMDD-XXXXXX`
  - Optional check: open `/confirm?bookingId=<bookingId>` and confirm reference is shown

- [ ] Validate race / double-booking behavior
  - Submit the same hold twice in parallel for the same slot
  - Expect: one request `201`, one request `409` with `{"error":"slot taken"}`

- [ ] Test admin block create/list/delete
  - Create block (`POST /api/admin/blocks`) using either:
    - `startDatetime` + `endDatetime` (canonical), or
    - `start` + `end` (legacy aliases)
  - List blocks (`GET /api/admin/blocks`) and confirm block appears
  - Delete block (`DELETE /api/admin/blocks?id=<id>`) and confirm it is removed

- [ ] Check pending expiration behavior
  - Create a hold and wait until `expires_at` has passed (10 minutes by default)
  - Trigger cleanup by calling any API endpoint or `POST /api/admin/expire-pending`
  - Expect: expired booking transitions to `CANCELLED` and no longer blocks availability
