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
  ``rentalType`` (``TWO_HOURS`` or ``FULL_DAY``) and ``date`` (ISO
  YYYY-MM-DD).  Returns ``{price: int}``.
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
import os
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import db
import sqlite3


ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"


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

        # Dev/test endpoint
        if path == "/api/health":
            return self.end_json(200, {"ok": True})

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

        return self.end_json(404, {"error": "Not Found"})

    # ---- API implementations ----

    def handle_price(self, params: Dict[str, str]) -> None:
        rental_type = params.get("rentalType")
        date_str = params.get("date")
        if not rental_type or not date_str:
            return self.end_json(400, {"error": "rentalType and date are required"})
        rental_type_u = rental_type.upper()
        try:
            # If only date is given, assume start of day for weekday determination
            dt = datetime.fromisoformat(date_str + "T00:00")
            price = db.calculate_price(dt, rental_type_u)
        except Exception as e:
            return self.end_json(400, {"error": str(e)})
        return self.end_json(200, {"price": price})

    def handle_availability(self, params: Dict[str, str]) -> None:
        trailer_type = params.get("trailerType")
        rental_type = params.get("rentalType")
        date_str = params.get("date")
        start_time = params.get("startTime")
        # Basic validation
        if not trailer_type or not rental_type or not date_str:
            return self.end_json(400, {"error": "trailerType, rentalType and date are required"})
        trailer_type_u = trailer_type.upper()
        rental_type_u = rental_type.upper()
        try:
            if rental_type_u == "FULL_DAY":
                start_dt = datetime.fromisoformat(date_str + "T00:00")
                end_dt = datetime.fromisoformat(date_str + "T23:59")
            elif rental_type_u == "TWO_HOURS":
                if not start_time:
                    return self.end_json(400, {"error": "startTime required for TWO_HOURS"})
                start_dt = datetime.fromisoformat(date_str + "T" + start_time)
                end_dt = start_dt + timedelta(hours=2)
            else:
                return self.end_json(400, {"error": "Invalid rentalType"})
        except Exception as e:
            return self.end_json(400, {"error": str(e)})
        # Compute remaining units (2 minus overlapping bookings)
        try:
            # Use direct SQL count to compute overlapping bookings for given type and period
            conn = sqlite3.connect(db.DB_PATH)
            cur = conn.execute(
                """
                SELECT COUNT(*)
                FROM bookings
                WHERE trailer_type = ? AND status != 'CANCELLED'
                  AND (start_dt < ? AND ? < end_dt)
                """,
                (
                    trailer_type_u,
                    end_dt.isoformat(timespec="minutes"),
                    start_dt.isoformat(timespec="minutes"),
                ),
            )
            row = cur.fetchone()
            overlapping = row[0] if row else 0
        except Exception as e:
            # Return internal error with message
            return self.end_json(500, {"error": str(e)})
        finally:
            conn.close()
        remaining = max(0, 2 - overlapping)
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

    def handle_hold(self) -> None:
        # Parse JSON body
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self.end_json(400, {"error": "Invalid JSON"})
        trailer_type = data.get("trailerType")
        rental_type = data.get("rentalType")
        date_str = data.get("date")
        start_time = data.get("startTime")
        if not trailer_type or not rental_type or not date_str:
            return self.end_json(400, {"error": "trailerType, rentalType and date are required"})
        try:
            rental_type_u = rental_type.upper()
            trailer_type_u = trailer_type.upper()
            if rental_type_u == "FULL_DAY":
                start_dt = datetime.fromisoformat(date_str + "T00:00")
                end_dt = datetime.fromisoformat(date_str + "T23:59")
            elif rental_type_u == "TWO_HOURS":
                if not start_time:
                    return self.end_json(400, {"error": "startTime required for TWO_HOURS"})
                start_dt = datetime.fromisoformat(date_str + "T" + start_time)
                end_dt = start_dt + timedelta(hours=2)
            else:
                return self.end_json(400, {"error": "Invalid rentalType"})
        except Exception as e:
            return self.end_json(400, {"error": str(e)})
        # Create booking hold using existing logic
        try:
            booking_id, price = db.create_booking(
                trailer_type_u, rental_type_u, start_dt, end_dt
            )
        except ValueError as ve:
            return self.end_json(400, {"error": str(ve)})
        except Exception as e:
            return self.end_json(500, {"error": str(e)})
        return self.end_json(201, {"bookingId": booking_id, "price": price})

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
            db.mark_confirmed(booking_id)
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
        message = f"DHS-{booking_id}"
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
        message = f"DHS-{booking_id}"
        payload = f"C{payee};{amount_str};{urllib.parse.quote(message, safe='')};0"
        qr_url = "https://quickchart.io/qr?size=320&text=" + urllib.parse.quote(payload, safe="")
        return {
            "bookingId": booking_id,
            "price": amount,
            "swishId": swish_id,
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
        self.wfile.write(data)


def run() -> None:
    # Initialise database on startup
    db.init_db()
    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Running Dalsjöfors Hyrservice on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()