"""
Webserver for Milstolpe B of Dalsjöfors Hyrservice.

This module exposes a simple HTTP API and serves a wizard UI for booking
trailers.  It builds upon the data model and business logic provided in
``db.py`` and does not introduce any additional third‑party
dependencies.  The API is intentionally minimal and returns JSON
responses for live price and availability, plus a booking hold (which
creates a booking in status ``PENDING_PAYMENT``).

Endpoints
~~~~~~~~~

* ``GET /`` – serves the HTML wizard (index.html).
* ``GET /static/<path>`` – serves static assets (CSS/JS/SVG).
* ``GET /api/price`` – query parameters:
  ``trailerType`` (``GALLER`` or ``KAP``), ``rentalType``
  (``TWO_HOURS`` or ``FULL_DAY``) and ``date`` (ISO YYYY-MM-DD).
  Returns ``{price: int}``.
* ``GET /api/availability`` – query parameters:
  ``trailerType`` (``GALLER`` or ``KAP``), ``rentalType``,
  ``date``, and (optionally) ``startTime`` (HH:MM).  Calculates start
  and end datetimes according to the rules and uses the logic in
  ``check_availability`` to determine whether at least one unit is free.
  Returns ``{available: bool, remaining: int}`` where ``remaining`` is
  between 0 and 2 inclusive.
* ``POST /api/hold`` – body JSON with ``trailerType``, ``rentalType``,
  ``date`` and optional ``startTime``.  Creates a booking in status
  ``PENDING_PAYMENT`` and returns ``{bookingId: int, price: int}``.
* ``POST /api/admin/blocks`` – body JSON with ``trailerType`` and a
  datetime range using either ``startDatetime``/``endDatetime`` or
  backward-compatible ``start``/``end`` aliases. If both are provided,
  ``startDatetime``/``endDatetime`` take precedence. Returns canonical
  block fields including ``startDatetime`` and ``endDatetime``.

The server intentionally does not implement Swish QR or payment
confirmation; those are added in later milestones.

Run the server from the project root with:

```
python3 app.py
```

Then open ``http://localhost:8000`` in your browser.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import db
import notifications


ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
logger = logging.getLogger(__name__)
NOTIFIER = notifications.create_notification_service_from_env()


def parse_query(query: str) -> Dict[str, str]:
    """Parse a URL query string into a dict of first values."""
    parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


class Handler(BaseHTTPRequestHandler):
    """Custom HTTP request handler supporting API and static files."""

    server_version = "DalsjoforsHyrservice/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Silence default logging
        return

    def end_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def api_error(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
        legacy_error: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Return stable API errors with legacy compatibility."""
        payload: Dict[str, Any] = {
            "error": legacy_error or message,
            "errorInfo": {"code": code, "message": message},
        }
        if details is not None:
            payload["errorInfo"]["details"] = details
        if extra:
            payload.update(extra)
        self.end_json(status, payload)

    def _invalid_field_error(self, field_errors: Dict[str, str], message: str = "Invalid request") -> None:
        self.api_error(
            400,
            "invalid_request",
            message,
            details={"fields": field_errors},
            legacy_error="; ".join([f"{field}: {msg}" for field, msg in field_errors.items()]),
        )

    def _validate_trailer_type(self, trailer_type: Optional[str], required: bool = True) -> Optional[str]:
        trailer_value = (trailer_type or "").strip().upper()
        if not trailer_value:
            if required:
                self._invalid_field_error({"trailerType": "This field is required"})
            return None
        if trailer_value not in db.VALID_TRAILER_TYPES:
            self._invalid_field_error(
                {"trailerType": f"Must be one of: {', '.join(sorted(db.VALID_TRAILER_TYPES))}"}
            )
            return None
        return trailer_value

    def _validate_rental_type(self, rental_type: Optional[str]) -> Optional[str]:
        rental_value = (rental_type or "").strip().upper()
        if not rental_value:
            self._invalid_field_error({"rentalType": "This field is required"})
            return None
        if rental_value not in db.VALID_RENTAL_TYPES:
            self._invalid_field_error(
                {"rentalType": f"Must be one of: {', '.join(sorted(db.VALID_RENTAL_TYPES))}"}
            )
            return None
        return rental_value

    def _validate_date(self, date_str: Optional[str]) -> Optional[str]:
        value = (date_str or "").strip()
        if not value:
            self._invalid_field_error({"date": "This field is required"})
            return None
        if not DATE_RE.match(value):
            self._invalid_field_error({"date": "Expected format YYYY-MM-DD"})
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            self._invalid_field_error({"date": "Invalid calendar date"})
            return None
        return value

    def _validate_start_time(self, start_time: Optional[str], required: bool) -> Optional[str]:
        value = (start_time or "").strip()
        if not value:
            if required:
                self._invalid_field_error({"startTime": "This field is required for TWO_HOURS"})
            return None
        if not TIME_RE.match(value):
            self._invalid_field_error({"startTime": "Expected format HH:MM"})
            return None
        return value

    def _parse_iso_datetime_field(self, field_name: str, raw_value: Optional[str]) -> Optional[datetime]:
        value = (raw_value or "").strip()
        if not value:
            self._invalid_field_error({field_name: "This field is required"})
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            self._invalid_field_error({field_name: "Expected ISO 8601 datetime (e.g. YYYY-MM-DDTHH:MM)"})
            return None
        if "T" not in value:
            self._invalid_field_error({field_name: "Expected ISO 8601 datetime (e.g. YYYY-MM-DDTHH:MM)"})
            return None
        return parsed

    # ---- Request handlers ----

    def do_GET(self) -> None:
        """Handle HTTP GET requests.

        In addition to serving the index, static files and API endpoints
        from Milestone B, this method now performs the following tasks:

        * Expires outdated bookings before processing to keep
          availability accurate.
        * Supports a new API endpoint ``/api/payment`` to generate a
          Swish payment request for a booking.
        * Serves dynamic payment and confirmation pages (``/pay`` and
          ``/confirm``).
        """
        # Parse path and query parameters
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_params = parse_query(parsed.query)

        # Expire outdated bookings on each GET request
        try:
            db.expire_outdated_bookings()
        except Exception:
            pass

        # Root page
        if path in ("/", ""):
            return self.serve_file("index.html", "text/html; charset=utf-8")
        if path == "/admin":
            return self.serve_file("admin.html", "text/html; charset=utf-8")

        # Serve static assets under /static
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            return self.serve_static(rel)

        # API endpoints
        if path == "/api/price":
            return self.handle_price(query_params)
        if path == "/api/availability":
            return self.handle_availability(query_params)
        if path == "/api/payment":
            return self.handle_payment(query_params)
        if path == "/api/admin/bookings":
            return self.handle_admin_bookings(query_params)
        if path == "/api/admin/blocks":
            return self.handle_admin_blocks_get(query_params)

        # Dev/test endpoint
        if path == "/api/health":
            return self.end_json(
                200,
                {
                    "ok": True,
                    "service": "dalsjofors-hyrservice",
                    "time": datetime.now().isoformat(timespec="seconds"),
                },
            )

        # Payment pages
        if path == "/pay":
            return self.serve_pay_page(query_params)
        if path == "/confirm":
            return self.serve_confirm_page(query_params)

        return self.end_json(404, {"error": "Not Found"})

    def do_POST(self) -> None:
        """Handle HTTP POST requests.

        Supported POST endpoints now include booking holds and Swish
        callbacks.  Outdated bookings are expired up front to avoid
        lingering reservations.
        """
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Expire outdated bookings on each POST
        try:
            db.expire_outdated_bookings()
        except Exception:
            pass

        # Create a booking hold
        if path == "/api/hold":
            return self.handle_hold()

        # Handle Swish Commerce callback
        if path == "/api/swish/callback":
            return self.handle_swish_callback()
        if path == "/api/admin/blocks":
            return self.handle_admin_blocks_create()
        if path == "/api/admin/expire-pending":
            return self.handle_admin_expire_pending()

        return self.end_json(404, {"error": "Not Found"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_params = parse_query(parsed.query)

        try:
            db.expire_outdated_bookings()
        except Exception:
            pass

        if path == "/api/admin/blocks":
            return self.handle_admin_blocks_delete(query_params)
        return self.end_json(404, {"error": "Not Found"})

    # ---- API implementations ----

    def _resolve_block_datetime_fields(self, data: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        """Resolve accepted datetime aliases for admin block payloads.

        Canonical keys are ``startDatetime``/``endDatetime``.
        Backward-compatible aliases ``start``/``end`` are also accepted.
        If both styles are provided, canonical keys take precedence.
        """
        start_dt_str = data.get("startDatetime")
        end_dt_str = data.get("endDatetime")
        if not start_dt_str:
            start_dt_str = data.get("start")
        if not end_dt_str:
            end_dt_str = data.get("end")
        return start_dt_str, end_dt_str

    def handle_price(self, params: Dict[str, str]) -> None:
        trailer_type = params.get("trailerType")
        rental_type_u = self._validate_rental_type(params.get("rentalType"))
        date_str = self._validate_date(params.get("date"))
        if rental_type_u is None or date_str is None:
            return
        # Backward compatible: old clients did not send trailerType.
        trailer_type_u = self._validate_trailer_type(trailer_type or "GALLER", required=True)
        if trailer_type_u is None:
            return
        dt = datetime.strptime(date_str + "T00:00", "%Y-%m-%dT%H:%M")
        try:
            price = db.calculate_price(dt, rental_type_u, trailer_type_u)
        except ValueError as e:
            return self.api_error(400, "invalid_request", str(e), legacy_error=str(e))
        return self.end_json(200, {"price": price})

    def handle_availability(self, params: Dict[str, str]) -> None:
        trailer_type_u = self._validate_trailer_type(params.get("trailerType"))
        rental_type_u = self._validate_rental_type(params.get("rentalType"))
        date_str = self._validate_date(params.get("date"))
        if trailer_type_u is None or rental_type_u is None or date_str is None:
            return

        if rental_type_u == "FULL_DAY":
            start_dt = datetime.strptime(date_str + "T00:00", "%Y-%m-%dT%H:%M")
            end_dt = datetime.strptime(date_str + "T23:59", "%Y-%m-%dT%H:%M")
        else:
            start_time = self._validate_start_time(params.get("startTime"), required=True)
            if start_time is None:
                return
            start_dt = datetime.strptime(f"{date_str}T{start_time}", "%Y-%m-%dT%H:%M")
            end_dt = start_dt + timedelta(hours=2)
        try:
            block = db.find_block_overlap(trailer_type_u, start_dt, end_dt)
            if block:
                remaining = 0
            else:
                overlapping = db.count_overlapping_active_bookings(
                    trailer_type_u, start_dt, end_dt
                )
                remaining = max(0, db.TRAILERS_PER_TYPE - overlapping)
        except Exception as e:
            return self.api_error(500, "internal_error", "Internal server error", legacy_error=str(e))
        available = remaining > 0
        return self.end_json(200, {"available": available, "remaining": remaining})

    def handle_payment(self, params: Dict[str, str]) -> None:
        """Initiate or retrieve a Swish payment request for a booking.

        Expects a query parameter ``bookingId``.  If the booking is in
        ``PENDING_PAYMENT`` state the function either returns existing
        payment details or generates a new payment request (fake
        implementation).  The response includes the Swish ID, price,
        QR-code URL and payload string.  If the booking has already been
        confirmed or cancelled an error is returned.
        """
        booking_id_str = params.get("bookingId")
        if not booking_id_str:
            return self.end_json(400, {"error": "bookingId is required"})
        try:
            booking_id = int(booking_id_str)
        except ValueError:
            return self.end_json(400, {"error": "bookingId must be an integer"})
        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.end_json(404, {"error": "Booking not found"})
        status = booking.get("status")
        if status != "PENDING_PAYMENT":
            return self.end_json(400, {"error": "Booking is not awaiting payment"})
        # Create payment details if needed
        details = self._get_or_create_payment_details(booking_id)
        return self.end_json(200, details)

    def handle_admin_bookings(self, params: Dict[str, str]) -> None:
        """Return booking rows for admin tooling."""
        status = params.get("status")
        status_u = status.upper() if status else None
        if status_u and status_u not in {"PENDING_PAYMENT", "CONFIRMED", "CANCELLED"}:
            return self.end_json(400, {"error": "Invalid status"})
        bookings = db.get_bookings(status_u)
        payload_rows = []
        for booking in bookings:
            payload_rows.append(
                {
                    "bookingId": booking["id"],
                    "bookingReference": booking.get("booking_reference"),
                    "trailerType": booking["trailer_type"],
                    "rentalType": booking["rental_type"],
                    "startDt": booking["start_dt"],
                    "endDt": booking["end_dt"],
                    "price": booking["price"],
                    "status": booking["status"],
                    "createdAt": booking["created_at"],
                    "swishId": booking.get("swish_id"),
                    "expiresAt": booking.get("expires_at"),
                }
            )
        return self.end_json(200, {"bookings": payload_rows})

    def handle_admin_blocks_get(self, params: Dict[str, str]) -> None:
        start_dt_str = params.get("startDatetime")
        end_dt_str = params.get("endDatetime")

        start_dt = self._parse_iso_datetime_field("startDatetime", start_dt_str) if start_dt_str else None
        if start_dt_str and start_dt is None:
            return
        end_dt = self._parse_iso_datetime_field("endDatetime", end_dt_str) if end_dt_str else None
        if end_dt_str and end_dt is None:
            return

        if start_dt and end_dt and end_dt <= start_dt:
            return self._invalid_field_error({"endDatetime": "Must be after startDatetime"})

        rows = db.list_blocks(start_dt, end_dt)
        payload_rows = []
        for row in rows:
            payload_rows.append(
                {
                    "id": row["id"],
                    "trailerType": row["trailer_type"],
                    "startDatetime": row["start_dt"],
                    "endDatetime": row["end_dt"],
                    "reason": row["reason"],
                    "createdAt": row["created_at"],
                }
            )
        return self.end_json(200, {"blocks": payload_rows})

    def handle_admin_blocks_create(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self.api_error(400, "invalid_json", "Request body must be valid JSON", legacy_error="Invalid JSON")

        trailer_type = self._validate_trailer_type(data.get("trailerType"))
        if trailer_type is None:
            return
        start_dt_str, end_dt_str = self._resolve_block_datetime_fields(data)
        reason = data.get("reason") or ""
        missing_fields: Dict[str, str] = {}
        if not start_dt_str:
            missing_fields["startDatetime"] = "Provide startDatetime or start"
        if not end_dt_str:
            missing_fields["endDatetime"] = "Provide endDatetime or end"
        if missing_fields:
            return self._invalid_field_error(missing_fields, "Missing required fields")
        start_dt = self._parse_iso_datetime_field("startDatetime", start_dt_str)
        if start_dt is None:
            return
        end_dt = self._parse_iso_datetime_field("endDatetime", end_dt_str)
        if end_dt is None:
            return
        if end_dt <= start_dt:
            return self._invalid_field_error({"endDatetime": "Must be after startDatetime"})
        try:
            row = db.create_block(trailer_type, start_dt, end_dt, reason)
        except ValueError as e:
            return self.api_error(400, "invalid_request", str(e), legacy_error=str(e))
        except Exception as e:
            return self.api_error(500, "internal_error", "Internal server error", legacy_error=str(e))

        return self.end_json(
            201,
            {
                "id": row["id"],
                "trailerType": row["trailer_type"],
                "startDatetime": row["start_dt"],
                "endDatetime": row["end_dt"],
                "reason": row["reason"],
                "createdAt": row["created_at"],
            },
        )

    def handle_admin_blocks_delete(self, params: Dict[str, str]) -> None:
        block_id_str = params.get("id")
        if not block_id_str:
            return self._invalid_field_error({"id": "This field is required"})
        try:
            block_id = int(block_id_str)
        except ValueError:
            return self._invalid_field_error({"id": "Must be an integer"})

        if not db.delete_block(block_id):
            return self.api_error(404, "block_not_found", "Block not found", legacy_error="Block not found")
        return self.end_json(200, {"deleted": True, "id": block_id})

    def handle_admin_expire_pending(self) -> None:
        expired_count = db.expire_outdated_bookings()
        return self.end_json(200, {"expiredCount": expired_count})

    def handle_hold(self) -> None:
        # Parse JSON body
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self.api_error(400, "invalid_json", "Request body must be valid JSON", legacy_error="Invalid JSON")
        trailer_type_u = self._validate_trailer_type(data.get("trailerType"))
        rental_type_u = self._validate_rental_type(data.get("rentalType"))
        date_str = self._validate_date(data.get("date"))
        if trailer_type_u is None or rental_type_u is None or date_str is None:
            return

        if rental_type_u == "FULL_DAY":
            start_dt = datetime.strptime(date_str + "T00:00", "%Y-%m-%dT%H:%M")
            end_dt = datetime.strptime(date_str + "T23:59", "%Y-%m-%dT%H:%M")
        else:
            start_time = self._validate_start_time(data.get("startTime"), required=True)
            if start_time is None:
                return
            start_dt = datetime.strptime(f"{date_str}T{start_time}", "%Y-%m-%dT%H:%M")
            end_dt = start_dt + timedelta(hours=2)
        # Create booking hold using existing logic
        try:
            booking_id, price = db.create_booking(
                trailer_type_u, rental_type_u, start_dt, end_dt
            )
        except db.SlotBlockedError as block_err:
            block = block_err.block
            return self.api_error(
                409,
                "slot_blocked",
                "Requested slot overlaps an admin block",
                legacy_error="slot blocked",
                details={
                    "block": {
                        "id": block["id"],
                        "trailerType": block["trailer_type"],
                        "startDatetime": block["start_dt"],
                        "endDatetime": block["end_dt"],
                        "reason": block["reason"],
                    }
                },
                extra={
                    "message": "Requested slot overlaps an admin block",
                    "block": {
                        "id": block["id"],
                        "trailerType": block["trailer_type"],
                        "startDatetime": block["start_dt"],
                        "endDatetime": block["end_dt"],
                        "reason": block["reason"],
                    },
                },
            )
        except db.SlotTakenError:
            return self.api_error(409, "slot_taken", "Requested slot is already taken", legacy_error="slot taken")
        except ValueError as ve:
            return self.api_error(400, "invalid_request", str(ve), legacy_error=str(ve))
        except Exception as e:
            return self.api_error(500, "internal_error", "Internal server error", legacy_error=str(e))
        booking = db.get_booking_by_id(booking_id)
        if booking:
            try:
                NOTIFIER.notify_booking_created(booking)
            except Exception:
                logger.exception("notification dispatch failed event=booking.created booking_id=%s", booking_id)
        return self.end_json(
            201,
            {
                "bookingId": booking_id,
                "bookingReference": booking.get("booking_reference") if booking else None,
                "price": price,
            },
        )

    def handle_swish_callback(self) -> None:
        """Handle callbacks from Swish Commerce API.

        Expected JSON payload:
        {
            "paymentReference": <bookingId>,
            "status": "PAID" | "CANCELLED" | "EXPIRED" | "ERROR"
        }

        Updates booking status accordingly.  Always returns 200 OK.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self.end_json(400, {"error": "Invalid JSON"})
        booking_id = data.get("paymentReference")
        status = (data.get("status") or "").upper()
        if not booking_id:
            return self.end_json(400, {"error": "paymentReference is required"})
        try:
            booking_id = int(booking_id)
        except ValueError:
            return self.end_json(400, {"error": "paymentReference must be integer"})
        if status == "PAID":
            booking_before = db.get_booking_by_id(booking_id)
            was_pending = bool(booking_before and booking_before.get("status") == "PENDING_PAYMENT")
            db.mark_confirmed(booking_id)
            booking_after = db.get_booking_by_id(booking_id)
            if was_pending and booking_after and booking_after.get("status") == "CONFIRMED":
                try:
                    NOTIFIER.notify_booking_confirmed(booking_after)
                except Exception:
                    logger.exception(
                        "notification dispatch failed event=booking.confirmed booking_id=%s",
                        booking_id,
                    )
        elif status in ("CANCELLED", "EXPIRED", "ERROR"):
            db.cancel_booking(booking_id)
        return self.end_json(200, {"ok": True})

    def serve_pay_page(self, params: Dict[str, str]) -> None:
        """Serve the payment page for a given booking ID.

        If the booking exists and is pending payment the page will show a
        QR-code and instructions.  The payment request is created on
        demand via ``_get_or_create_payment_details``.
        """
        booking_id_str = params.get("bookingId")
        if not booking_id_str:
            return self.end_json(400, {"error": "bookingId is required"})
        try:
            booking_id = int(booking_id_str)
        except ValueError:
            return self.end_json(400, {"error": "bookingId must be an integer"})
        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.end_json(404, {"error": "Booking not found"})
        # Only allow payment page for pending bookings
        if booking["status"] != "PENDING_PAYMENT":
            return self.end_json(400, {"error": "Booking is not awaiting payment"})
        details = self._get_or_create_payment_details(booking_id)
        price = details["price"]
        qr_url = details["qrUrl"]
        message = details["swishMessage"]
        booking_reference = details.get("bookingReference")
        # Compose simple HTML for payment page
        html = f"""
<!doctype html><html lang=\"sv\"><head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>Betalning – Bokning {booking_id}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: #f8f9fb; color: #333; }}
    header, footer {{ background: #0b3b75; color: #fff; padding: 16px; text-align: center; }}
    main {{ max-width: 640px; margin: 0 auto; padding: 16px; }}
    .card {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 16px; box-shadow: 0 1px 6px rgba(0,0,0,.1); }}
  </style>
</head><body>
<header><h1>Dalsjöfors Hyrservice</h1></header>
<main>
  <div class=\"card\">
    <h2>Betala med Swish</h2>
    <p>Scanna QR-koden i din Swish-app. Belopp och meddelande är förifyllda.</p>
    <p><strong>Belopp:</strong> {price} kr</p>
    <p><strong>Mottagare:</strong> 1234 945580</p>
    <p><strong>Meddelande:</strong> {message}</p>
    <p><strong>Bokningsreferens:</strong> {booking_reference or "saknas"}</p>
    <img src=\"{qr_url}\" alt=\"Swish QR\" width=\"280\" height=\"280\" />
  </div>
  <div class=\"card\">
    <h3>Väntar på betalning …</h3>
    <p>När du har betalat via Swish kommer din bokning automatiskt att bekräftas. Du kan stänga den här sidan.</p>
  </div>
</main>
<footer>
  <p>&copy; 2026 Dalsjöfors Hyrservice AB</p>
  <p>Dalsjöfors Hyrservice AB • Org.nr: 559062-4556 • Momsnr: SE559062455601 • Adress: Boråsvägen 58B, 516 34 Dalsjöfors • Telefon: 070‑457 97 09</p>
  <p>Frågor eller problem? Ring <strong>070‑457 97 09</strong></p>
</footer>
</body></html>
"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_confirm_page(self, params: Dict[str, str]) -> None:
        """Serve the confirmation page for a booking.

        Shows a summary of booking details and provides a copyable text for
        the customer.  This page can be accessed after payment (or via
        a link).  It is idempotent: even if the booking is not yet
        confirmed the details are shown.
        """
        booking_id_str = params.get("bookingId")
        if not booking_id_str:
            return self.end_json(400, {"error": "bookingId is required"})
        try:
            booking_id = int(booking_id_str)
        except ValueError:
            return self.end_json(400, {"error": "bookingId must be an integer"})
        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.end_json(404, {"error": "Booking not found"})
        # Build summary lines
        trailer_text = "Gallersläp" if booking["trailer_type"] == "GALLER" else "Kåpsläp"
        rental_text = "2 timmar" if booking["rental_type"] == "TWO_HOURS" else "Heldag"
        start_date = booking["start_dt"][:10]
        start_time = booking["start_dt"][11:]
        end_time = booking["end_dt"][11:]
        summary_lines = [
            f"Boknings-ID: {booking['id']}",
            f"Bokningsreferens: {booking.get('booking_reference') or 'saknas'}",
            f"Släp: {trailer_text}",
            f"Datum: {start_date}",
            f"Start: {start_time}",
            f"Slut (end exclusive): {end_time}",
            f"Typ: {rental_text}",
            f"Pris: {booking['price']} kr",
            f"Swish-nummer: 1234 945580",
            f"Kodlåskod: 6392",
        ]
        confirm_text = "\n".join(summary_lines)
        html = f"""
<!doctype html><html lang=\"sv\"><head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>Bekräftelse – Bokning {booking_id}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: #f8f9fb; color: #333; }}
    header, footer {{ background: #0b3b75; color: #fff; padding: 16px; text-align: center; }}
    main {{ max-width: 640px; margin: 0 auto; padding: 16px; }}
    .card {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 16px; box-shadow: 0 1px 6px rgba(0,0,0,.1); }}
    textarea {{ width: 100%; height: 200px; border-radius: 8px; padding: 8px; border: 1px solid #ccc; }}
    button {{ padding: 12px 16px; border-radius: 12px; border: none; background: #0b3b75; color: #fff; font-weight: 600; cursor: pointer; }}
  </style>
</head><body>
<header><h1>Bokning bekräftad</h1></header>
<main>
  <div class=\"card\">
    <h2>Bekräftelseuppgifter</h2>
    <textarea readonly>{confirm_text}</textarea>
    <button onclick=\"navigator.clipboard.writeText('{confirm_text}'.replace(/\n/g, '\n'))\">Kopiera text</button>
    <p>Kodlåskod: <strong>6392</strong></p>
  </div>
</main>
<footer>
  <p>&copy; 2026 Dalsjöfors Hyrservice AB</p>
  <p>Dalsjöfors Hyrservice AB • Org.nr: 559062-4556 • Momsnr: SE559062455601 • Adress: Boråsvägen 58B, 516 34 Dalsjöfors • Telefon: 070‑457 97 09</p>
  <p>Frågor eller problem? Ring <strong>070‑457 97 09</strong></p>
</footer>
</body></html>
"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_or_create_payment_details(self, booking_id: int) -> Dict[str, Any]:
        """Return payment details for a booking, creating a request if needed.

        This helper replicates the logic from ``handle_payment`` but
        without writing to the HTTP response.  It reads the booking
        record, generates a fake Swish ID if necessary, persists it via
        ``db.set_swish_id`` and constructs a QR payload.
        """
        booking = db.get_booking_by_id(booking_id)
        amount = booking["price"]
        swish_id = booking.get("swish_id")
        if not swish_id:
            import uuid

            swish_id = str(uuid.uuid4()).replace("-", "")
            db.set_swish_id(booking_id, swish_id)
        payee = os.environ.get("SWISH_PAYEE", "1234945580")
        amount_str = f"{amount:.2f}".replace(".", ",")
        booking_reference = booking.get("booking_reference")
        message = booking_reference or f"DHS-{booking_id}"
        payload = f"C{payee};{amount_str};{urllib.parse.quote(message, safe='')};0"
        qr_url = "https://quickchart.io/qr?size=320&text=" + urllib.parse.quote(payload, safe="")
        return {
            "bookingId": booking_id,
            "bookingReference": booking_reference,
            "price": amount,
            "swishId": swish_id,
            "swishMessage": message,
            "qrUrl": qr_url,
            "payload": payload,
        }

    # ---- Static file serving ----

    def serve_static(self, relative_path: str) -> None:
        # Sanitize path
        # Avoid directory traversal
        if ".." in relative_path or relative_path.startswith("/"):
            return self.end_json(400, {"error": "Bad path"})
        file_path = STATIC_DIR / relative_path
        if not file_path.is_file():
            return self.end_json(404, {"error": "Not Found"})
        # Determine content type
        content_type = "application/octet-stream"
        if relative_path.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif relative_path.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        elif relative_path.endswith(".svg"):
            content_type = "image/svg+xml"
        elif relative_path.endswith(".html"):
            content_type = "text/html; charset=utf-8"
        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except Exception:
            return self.end_json(500, {"error": "Could not read file"})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def serve_file(self, relative_path: str, content_type: str) -> None:
        file_path = ROOT_DIR / relative_path
        if not file_path.is_file():
            return self.end_json(404, {"error": "Not Found"})
        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except Exception:
            return self.end_json(500, {"error": "Could not read file"})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            # Client disconnected before the response body was fully sent.
            return


def run() -> None:
    # Initialise database on startup
    db.init_db()
    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Running Dalsjöfors Hyrservice on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
