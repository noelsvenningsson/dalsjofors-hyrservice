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
import sqlite3
import hmac
import hashlib
import html as html_lib
import time
import base64
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import db
import notifications
import sms_provider
from qrcodegen import QrCode
from swish_client import SwishClient, SwishConfig


ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
logger = logging.getLogger(__name__)
NOTIFIER = notifications.create_notification_service_from_env()
_ADMIN_TOKEN_WARNING_EMITTED = False
ADMIN_SESSION_COOKIE_NAME = "admin_session"
ADMIN_SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
ADMIN_LOGIN_FAILURE_DELAY_SECONDS = 0.3


def _debug_swish_enabled() -> bool:
    return (os.environ.get("DEBUG_SWISH") or "").strip() == "1"


def _debug_swish_log(event: str, **fields: Any) -> None:
    if not _debug_swish_enabled():
        return
    logger.warning(
        "SWISH_DEBUG %s %s",
        event,
        json.dumps(fields, ensure_ascii=False, default=str),
    )


def is_production_environment() -> bool:
    """Best-effort check for production runtime."""
    env_name = (
        os.environ.get("APP_ENV")
        or os.environ.get("ENV")
        or os.environ.get("PYTHON_ENV")
        or ""
    ).strip().lower()
    if env_name in {"production", "prod"}:
        return True
    render_flag = (os.environ.get("RENDER") or "").strip().lower()
    return render_flag in {"1", "true", "yes"}


def get_admin_token() -> str:
    return (os.environ.get("ADMIN_TOKEN") or "").strip()


def get_admin_password() -> str:
    return (os.environ.get("ADMIN_PASSWORD") or "").strip()


def get_admin_session_secret() -> str:
    return (os.environ.get("ADMIN_SESSION_SECRET") or "").strip()


def _constant_time_secret_match(provided_value: str, expected_value: str) -> bool:
    provided_hash = hashlib.sha256(provided_value.encode("utf-8")).digest()
    expected_hash = hashlib.sha256(expected_value.encode("utf-8")).digest()
    return hmac.compare_digest(provided_hash, expected_hash)


def _warn_admin_auth_disabled_once() -> None:
    """Warn once when admin auth is disabled in local development."""
    global _ADMIN_TOKEN_WARNING_EMITTED
    if _ADMIN_TOKEN_WARNING_EMITTED:
        return
    _ADMIN_TOKEN_WARNING_EMITTED = True
    logger.warning(
        "ADMIN_TOKEN is not set; API admin/dev auth is disabled for local development. "
        "Set ADMIN_TOKEN to protect /api/dev/* and /api/admin/* routes."
    )


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

    def end_html_message(self, code: int, title: str, message: str) -> None:
        body = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>{title}</title></head><body>"
            f"<h1>{title}</h1><p>{message}</p></body></html>"
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _request_is_https(self) -> bool:
        forwarded_proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if forwarded_proto == "https":
            return True
        forwarded = (self.headers.get("Forwarded") or "").lower()
        if "proto=https" in forwarded:
            return True
        return bool(getattr(self.connection, "cipher", None))

    def _send_redirect(self, location: str, *, set_cookie: Optional[str] = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def _admin_session_cookie_value(self, expected_password: str) -> Optional[str]:
        secret = get_admin_session_secret()
        if not secret:
            return None
        expires_at = int(time.time()) + ADMIN_SESSION_MAX_AGE_SECONDS
        password_hash = hashlib.sha256(expected_password.encode("utf-8")).hexdigest()
        payload = f"v1|{expires_at}|{password_hash}"
        signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{payload_b64}.{signature}"

    def _admin_session_cookie_header(self, session_value: str) -> str:
        parts = [
            f"{ADMIN_SESSION_COOKIE_NAME}={session_value}",
            "Path=/",
            f"Max-Age={ADMIN_SESSION_MAX_AGE_SECONDS}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self._request_is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _clear_admin_session_cookie_header(self) -> str:
        parts = [
            f"{ADMIN_SESSION_COOKIE_NAME}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self._request_is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _has_valid_admin_session_cookie(self, expected_password: str) -> bool:
        secret = get_admin_session_secret()
        if not secret:
            return False
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return False
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
        except Exception:
            return False
        morsel = cookies.get(ADMIN_SESSION_COOKIE_NAME)
        if morsel is None:
            return False
        raw_value = morsel.value
        if "." not in raw_value:
            return False
        payload_b64, signature = raw_value.split(".", 1)
        if not payload_b64 or not signature:
            return False
        try:
            payload_bytes = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
            payload = payload_bytes.decode("utf-8")
        except Exception:
            return False
        expected_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return False
        parts = payload.split("|")
        if len(parts) != 3 or parts[0] != "v1":
            return False
        try:
            expires_at = int(parts[1])
        except ValueError:
            return False
        if int(time.time()) > expires_at:
            return False
        expected_password_hash = hashlib.sha256(expected_password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(parts[2], expected_password_hash)

    def _admin_login_html(self, *, error_message: Optional[str] = None) -> bytes:
        escaped_error = html_lib.escape(error_message) if error_message else ""
        error_block = f"<p style='color:#a60000;font-weight:600'>{escaped_error}</p>" if escaped_error else ""
        page = f"""
<!doctype html><html lang="sv"><head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Admin inloggning</title>
  <style>
    body {{ font-family: "Segoe UI", Arial, sans-serif; margin: 0; background: #f4f7fa; color: #1f2b37; }}
    main {{ max-width: 440px; margin: 48px auto; padding: 0 16px; }}
    .card {{ background: #fff; border: 1px solid #dbe5ee; border-radius: 12px; padding: 20px; box-shadow: 0 8px 28px rgba(18, 35, 52, 0.08); }}
    label {{ display: block; margin-bottom: 8px; font-weight: 600; }}
    input {{ width: 100%; padding: 10px; border: 1px solid #c9d6e2; border-radius: 8px; font-size: 16px; }}
    button {{ margin-top: 14px; width: 100%; border: 0; border-radius: 8px; padding: 11px 14px; background: #1f4f7d; color: #fff; font-weight: 700; font-size: 16px; cursor: pointer; }}
    p {{ line-height: 1.45; }}
  </style>
</head><body>
<main>
  <div class="card">
    <h1>Admin</h1>
    <p>Logga in med admin-lösenordet för att öppna adminpanelen i webbläsaren.</p>
    {error_block}
    <form method="post" action="/admin/login">
      <label for="password">Admin-lösenord</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required />
      <button type="submit">Logga in</button>
    </form>
  </div>
</main>
</body></html>
"""
        return page.encode("utf-8")

    def handle_admin_login_get(self) -> None:
        expected_password = get_admin_password()
        if not expected_password:
            return self.end_html_message(
                500,
                "Server Misconfigured",
                "ADMIN_PASSWORD is required for admin browser login.",
            )
        body = self._admin_login_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_admin_login_post(self) -> None:
        expected_password = get_admin_password()
        if not expected_password:
            return self.end_html_message(
                500,
                "Server Misconfigured",
                "ADMIN_PASSWORD is required for admin browser login.",
            )
        session_value = self._admin_session_cookie_value(expected_password)
        if not session_value:
            return self.end_html_message(
                500,
                "Server Misconfigured",
                "ADMIN_SESSION_SECRET is required for admin login sessions.",
            )
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        raw = self.rfile.read(content_length) if content_length > 0 else b""
        form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        provided_password = form.get("password", [""])[0] or ""
        if not _constant_time_secret_match(provided_password, expected_password):
            time.sleep(ADMIN_LOGIN_FAILURE_DELAY_SECONDS)
            body = self._admin_login_html(error_message="Fel lösenord. Försök igen.")
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        cookie_header = self._admin_session_cookie_header(session_value)
        return self._send_redirect("/admin", set_cookie=cookie_header)

    def handle_admin_logout_post(self) -> None:
        return self._send_redirect("/admin/login", set_cookie=self._clear_admin_session_cookie_header())

    def require_admin_page_auth(self) -> bool:
        expected_password = get_admin_password()
        if not expected_password:
            if is_production_environment():
                self.end_html_message(
                    500,
                    "Server Misconfigured",
                    "ADMIN_PASSWORD is required in production.",
                )
                return False
            self.end_html_message(
                500,
                "Server Misconfigured",
                "ADMIN_PASSWORD is required for admin browser login.",
            )
            return False

        if self._has_valid_admin_session_cookie(expected_password):
            return True

        self._send_redirect("/admin/login")
        return False

    def require_admin_api_auth(self) -> bool:
        expected_token = get_admin_token()
        if not expected_token:
            if is_production_environment():
                self.api_error(
                    500,
                    "server_misconfigured",
                    "Server misconfigured",
                    legacy_error="Server misconfigured",
                )
                return False
            _warn_admin_auth_disabled_once()
            return True

        provided_bearer_token = self._extract_bearer_token()
        if provided_bearer_token is not None and hmac.compare_digest(provided_bearer_token, expected_token):
            return True
        self.api_error(
            401,
            "unauthorized",
            "Unauthorized",
            legacy_error="Unauthorized",
        )
        return False

    def _extract_bearer_token(self) -> Optional[str]:
        auth_header = (self.headers.get("Authorization") or "").strip()
        if not auth_header:
            return None
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return ""
        return token.strip()

    def require_dev_auth(self, *, path: str, raw_query: str) -> bool:
        params = parse_query(raw_query)
        booking_id = params.get("bookingId")
        status = params.get("status")
        expected_token = get_admin_token()
        authorized = True

        if expected_token:
            provided_bearer_token = self._extract_bearer_token()
            if provided_bearer_token is None:
                authorized = False
                logger.warning(
                    "DEV_ENDPOINT_HIT endpoint=%s bookingId=%s status=%s authorized=no",
                    path,
                    booking_id,
                    status,
                )
                self.api_error(
                    401,
                    "unauthorized",
                    "Missing bearer token",
                    legacy_error="Unauthorized",
                )
                return False
            if not provided_bearer_token or not hmac.compare_digest(provided_bearer_token, expected_token):
                authorized = False
                logger.warning(
                    "DEV_ENDPOINT_HIT endpoint=%s bookingId=%s status=%s authorized=no",
                    path,
                    booking_id,
                    status,
                )
                self.api_error(
                    403,
                    "forbidden",
                    "Invalid bearer token",
                    legacy_error="Forbidden",
                )
                return False

        logger.warning(
            "DEV_ENDPOINT_HIT endpoint=%s bookingId=%s status=%s authorized=%s",
            path,
            booking_id,
            status,
            "yes" if authorized else "no",
        )
        return True

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

    def _validate_optional_customer_phone(self, raw_value: Optional[str]) -> Optional[str]:
        value = (raw_value or "").strip()
        if not value:
            return None
        normalized = sms_provider.normalize_swedish_mobile(value)
        if not normalized:
            self._invalid_field_error({"customerPhone": "Ange svensk mobil: +46xxxxxxxxx eller 07xxxxxxxx"})
            return None
        return normalized

    def _trailer_label(self, trailer_type: str) -> str:
        return "Galler-släp" if trailer_type == "GALLER" else "Kåpsläp"

    def _booking_period_label(self, booking: Dict[str, Any]) -> str:
        start_dt = booking.get("start_dt") or ""
        end_dt = booking.get("end_dt") or ""
        rental_type = (booking.get("rental_type") or "").upper()
        if rental_type == "FULL_DAY":
            return start_dt[:10]
        return f"{start_dt.replace('T', ' ')} - {end_dt.replace('T', ' ')}"

    def _send_paid_sms_notifications(self, booking_id: int) -> None:
        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return
        swish_status = (booking.get("swish_status") or "").upper()
        if swish_status != "PAID":
            return

        booking_ref = booking.get("booking_reference") or f"BOOKING-{booking_id}"
        trailer_label = self._trailer_label(booking.get("trailer_type") or "")
        period_label = self._booking_period_label(booking)
        price_label = f"{booking.get('price')} kr"

        if booking.get("sms_admin_sent_at") is None:
            admin_number = sms_provider.get_admin_sms_number_e164()
            if admin_number:
                admin_message = (
                    f"Ny bokning PAID: {booking_ref} | {trailer_label} | {period_label} | {price_label}"
                )
                if sms_provider.send_sms(admin_number, admin_message):
                    db.mark_sms_admin_sent(booking_id)

        booking = db.get_booking_by_id(booking_id) or booking
        customer_phone = booking.get("customer_phone_temp")
        if customer_phone and booking.get("sms_customer_sent_at") is None:
            customer_message = (
                f"Dalsjofors Hyrservice: Bokningskvitto: {booking_ref} | {trailer_label} | "
                f"{period_label} | {price_label} | Betalning: PAID"
            )
            if sms_provider.send_sms(customer_phone, customer_message):
                db.mark_sms_customer_sent(booking_id)
                db.clear_customer_phone_temp(booking_id)

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
        * Supports payment endpoints for retrieving Swish payment details
          and rendering QR content.
        * Serves dynamic payment and confirmation pages (``/pay`` and
          ``/confirm``).
        """
        # Parse path and query parameters
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_params = parse_query(parsed.query)
        if path.startswith("/api/dev/"):
            if not self.require_dev_auth(path=path, raw_query=parsed.query):
                return

        # Expire outdated bookings on each GET request
        try:
            db.expire_outdated_bookings()
        except Exception:
            pass

        # Root page
        if path in ("/", ""):
            return self.serve_file("index.html", "text/html; charset=utf-8")
        if path == "/admin/login":
            return self.handle_admin_login_get()
        if path == "/admin":
            if not self.require_admin_page_auth():
                return
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
        if path == "/api/payment-status":
            return self.handle_payment_status(query_params)
        if path == "/api/swish/qr":
            return self.handle_swish_qr(query_params)
        if path.startswith("/api/admin/"):
            if not self.require_admin_api_auth():
                return
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
                    "commit": "8fdf328",
                    "time": datetime.now().isoformat(timespec="seconds"),
                },
            )

        # Payment pages
        if path == "/pay":
            return self.serve_pay_page(query_params)
        if path == "/confirm":
            return self.serve_confirm_page(query_params)

        return self.end_json(404, {"error": "Not Found"})

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_params = parse_query(parsed.query)
        if path.startswith("/api/dev/"):
            if not self.require_dev_auth(path=path, raw_query=parsed.query):
                return
        if path == "/api/swish/qr":
            return self.handle_swish_qr(query_params, head_only=True)
        self.send_response(404)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_POST(self) -> None:
        """Handle HTTP POST requests.

        Supported POST endpoints now include booking holds and Swish
        callbacks.  Outdated bookings are expired up front to avoid
        lingering reservations.
        """
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/dev/"):
            if not self.require_dev_auth(path=path, raw_query=parsed.query):
                return

        debug_booking_id: Optional[int] = None
        if _debug_swish_enabled() and path == "/api/swish/paymentrequest":
            parsed_params = parse_query(parsed.query)
            booking_id_str = parsed_params.get("bookingId")
            if booking_id_str and booking_id_str.isdigit():
                debug_booking_id = int(booking_id_str)
                conn = sqlite3.connect(db.DB_PATH)
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute(
                        """
                        SELECT id, status, start_dt, end_dt, expires_at, swish_status
                        FROM bookings
                        WHERE id = ?
                        """,
                        (debug_booking_id,),
                    ).fetchone()
                    _debug_swish_log(
                        "do_post.paymentrequest.pre_expire",
                        db_path=str(db.DB_PATH),
                        booking_id=debug_booking_id,
                        raw_row=(dict(row) if row else None),
                    )
                finally:
                    conn.close()

        # Expire outdated bookings on each POST except paymentrequest.
        # paymentrequest should not mutate payable bookings before validation.
        if path != "/api/swish/paymentrequest":
            try:
                expired_count = db.expire_outdated_bookings()
                if _debug_swish_enabled():
                    _debug_swish_log(
                        "do_post.expire_outdated_bookings",
                        path=path,
                        expired_count=expired_count,
                    )
            except Exception as exc:
                if _debug_swish_enabled():
                    _debug_swish_log("do_post.expire_outdated_bookings.error", path=path, error=str(exc))
        elif _debug_swish_enabled():
            _debug_swish_log("do_post.expire_outdated_bookings.skipped", path=path)
            if debug_booking_id is not None:
                conn = sqlite3.connect(db.DB_PATH)
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute(
                        """
                        SELECT id, status, start_dt, end_dt, expires_at, swish_status
                        FROM bookings
                        WHERE id = ?
                        """,
                        (debug_booking_id,),
                    ).fetchone()
                    _debug_swish_log(
                        "do_post.paymentrequest.post_expire_skip",
                        db_path=str(db.DB_PATH),
                        booking_id=debug_booking_id,
                        raw_row=(dict(row) if row else None),
                    )
                finally:
                    conn.close()

        # Create a booking hold
        if path == "/api/hold":
            return self.handle_hold()
        if path == "/api/swish/paymentrequest":
            return self.handle_swish_payment_request(parsed.query)
        if path == "/api/dev/swish/mark":
            return self.handle_dev_swish_mark(parsed.query)

        # Handle Swish Commerce callback
        if path == "/api/swish/callback":
            return self.handle_swish_callback()
        if path == "/admin/login":
            return self.handle_admin_login_post()
        if path == "/admin/logout":
            if not self.require_admin_page_auth():
                return
            return self.handle_admin_logout_post()
        if path.startswith("/api/admin/"):
            if not self.require_admin_api_auth():
                return
        if path == "/api/admin/blocks":
            return self.handle_admin_blocks_create()
        if path == "/api/admin/expire-pending":
            return self.handle_admin_expire_pending()

        return self.end_json(404, {"error": "Not Found"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_params = parse_query(parsed.query)
        if path.startswith("/api/dev/"):
            if not self.require_dev_auth(path=path, raw_query=parsed.query):
                return

        try:
            db.expire_outdated_bookings()
        except Exception:
            pass

        if path.startswith("/api/admin/"):
            if not self.require_admin_api_auth():
                return
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
        day_type_label: Optional[str] = None
        if rental_type_u == "FULL_DAY":
            day_type_label = "Helg/röd dag" if db.full_day_rate_label(dt) == "HELG_OR_ROD_DAG" else "Vardag"
        return self.end_json(
            200,
            {
                "price": price,
                "dayTypeLabel": day_type_label,
            },
        )

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

    def _swish_mode(self) -> str:
        return (os.environ.get("SWISH_MODE") or "mock").strip().lower()

    def _swish_client(self) -> SwishClient:
        return SwishClient(
            SwishConfig(
                base_url=(os.environ.get("SWISH_COMMERCE_BASE_URL") or "mock"),
                merchant_alias=(os.environ.get("SWISH_COMMERCE_MERCHANT_ALIAS") or "1234945580"),
                callback_url=self._swish_callback_url(),
                mock=self._swish_mode() == "mock",
            )
        )

    def _swish_callback_url(self) -> str:
        configured = (os.environ.get("SWISH_COMMERCE_CALLBACK_URL") or "").strip()
        if configured:
            return configured
        host = self.headers.get("Host") or "localhost:8000"
        scheme = "https" if self._request_is_https() else "http"
        return f"{scheme}://{host}/api/swish/callback"

    def _swish_build_app_url(self, token: str) -> str:
        callback_url = self._swish_callback_url()
        token_enc = urllib.parse.quote(token, safe="")
        callback_enc = urllib.parse.quote(callback_url, safe="")
        return f"swish://paymentrequest?token={token_enc}&callbackurl={callback_enc}"

    def _is_payable_booking_status(self, booking_status: str) -> bool:
        return booking_status in {"PENDING_PAYMENT", "HOLD"}

    def _normalize_swish_status(self, swish_status_raw: Optional[str]) -> str:
        swish_status = (swish_status_raw or "").upper()
        if swish_status == "PAID":
            return "PAID"
        if swish_status in db.SWISH_FAILED_STATUSES:
            return "FAILED"
        return "PENDING"

    def _payment_request_payload(self, booking_id: int, booking: Dict[str, Any], *, idempotent: bool) -> Dict[str, Any]:
        token = booking.get("swish_token")
        swish_status = self._normalize_swish_status(booking.get("swish_status"))
        if (booking.get("status") or "").upper() in {"PAID", "CONFIRMED"}:
            swish_status = "PAID"
        swish_app_url = self._swish_build_app_url(token) if token else None
        return {
            "bookingId": booking_id,
            "swishRequestId": booking.get("swish_request_id"),
            "swishToken": token,
            "swishAppUrl": swish_app_url,
            "qrUrl": f"/api/swish/qr?bookingId={booking_id}",
            "status": swish_status,
            "idempotent": idempotent,
        }

    def handle_payment(self, params: Dict[str, str]) -> None:
        """Legacy payment endpoint kept for backward compatibility."""
        booking_id_str = params.get("bookingId")
        if not booking_id_str:
            return self.api_error(400, "invalid_request", "bookingId is required", legacy_error="bookingId is required")
        try:
            booking_id = int(booking_id_str)
        except ValueError:
            return self.api_error(400, "invalid_request", "bookingId must be an integer", legacy_error="bookingId must be an integer")
        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.api_error(404, "not_found", "Booking not found", legacy_error="Booking not found")
        return self.end_json(
            200,
            {
                "bookingId": booking_id,
                "bookingReference": booking.get("booking_reference"),
                "price": booking.get("price"),
                "swishId": booking.get("swish_id"),
                "swishToken": None,
                "swishAppUrl": None,
                "qrImageUrl": f"/api/swish/qr?bookingId={booking_id}",
                "integrationStatus": "NOT_CONFIGURED",
                "integrationMessage": "Use /api/swish/paymentrequest for mock payment flow.",
            },
        )

    def handle_swish_payment_request(self, raw_query: str) -> None:
        params = parse_query(raw_query)
        booking_id_str = params.get("bookingId")
        if not booking_id_str:
            return self.api_error(400, "invalid_request", "bookingId is required", legacy_error="bookingId is required")
        try:
            booking_id = int(booking_id_str)
        except ValueError:
            return self.api_error(400, "invalid_request", "bookingId must be an integer", legacy_error="bookingId must be an integer")

        if _debug_swish_enabled():
            conn = sqlite3.connect(db.DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                raw_row = conn.execute(
                    """
                    SELECT id, status, start_dt, end_dt, expires_at, swish_status, swish_token, swish_request_id
                    FROM bookings
                    WHERE id = ?
                    """,
                    (booking_id,),
                ).fetchone()
                _debug_swish_log(
                    "paymentrequest.raw_booking_row",
                    db_path=str(db.DB_PATH),
                    booking_id=booking_id,
                    raw_row=(dict(raw_row) if raw_row else None),
                )
            finally:
                conn.close()

        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.api_error(404, "not_found", "Booking not found", legacy_error="Booking not found")

        booking_status = (booking.get("status") or "").upper()
        if booking_status in {"PAID", "CONFIRMED"}:
            booking = dict(booking)
            booking["swish_status"] = "PAID"
            return self.end_json(200, self._payment_request_payload(booking_id, booking, idempotent=True))
        if not self._is_payable_booking_status(booking_status):
            if _debug_swish_enabled():
                _debug_swish_log(
                    "paymentrequest.not_payable",
                    db_path=str(db.DB_PATH),
                    booking_id=booking_id,
                    booking=booking,
                    booking_status=booking_status,
                )
            return self.api_error(
                409,
                "invalid_booking_status",
                "Booking is not payable",
                legacy_error="Booking is not payable",
                details={"status": booking_status},
            )

        swish_status = (booking.get("swish_status") or "").upper()
        if booking.get("swish_request_id") and swish_status in db.SWISH_PENDING_STATUSES:
            return self.end_json(200, self._payment_request_payload(booking_id, booking, idempotent=True))

        now_iso = datetime.now().isoformat(timespec="seconds")
        amount = int(booking.get("price") or 0)
        booking_ref = booking.get("booking_reference") or f"BOOKING-{booking_id}"
        message = f"DHS {booking_ref}"

        try:
            created = self._swish_client().create_payment_request(amount, message, self._swish_callback_url())
        except Exception as exc:
            return self.api_error(500, "internal_error", "Could not create payment request", legacy_error=str(exc))

        db.set_swish_payment_request(
            booking_id,
            instruction_uuid=created["instruction_uuid"],
            token=created["token"],
            request_id=created["request_id"],
            status="PENDING",
            created_at=now_iso,
            updated_at=now_iso,
        )
        refreshed = db.get_booking_by_id(booking_id)
        if not refreshed:
            return self.api_error(500, "internal_error", "Booking disappeared after update", legacy_error="Booking disappeared after update")
        return self.end_json(200, self._payment_request_payload(booking_id, refreshed, idempotent=False))

    def handle_payment_status(self, params: Dict[str, str]) -> None:
        booking_id_str = params.get("bookingId")
        if not booking_id_str:
            return self.api_error(400, "invalid_request", "bookingId is required", legacy_error="bookingId is required")
        try:
            booking_id = int(booking_id_str)
        except ValueError:
            return self.api_error(400, "invalid_request", "bookingId must be an integer", legacy_error="bookingId must be an integer")

        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.api_error(404, "not_found", "Booking not found", legacy_error="Booking not found")

        swish_status = self._normalize_swish_status(booking.get("swish_status"))
        if (booking.get("status") or "").upper() in {"PAID", "CONFIRMED"}:
            swish_status = "PAID"
        # Never auto-confirm in mock mode. Booking should stay pending until
        # an explicit status update arrives (e.g. callback or dev mark endpoint).

        if swish_status == "PAID" and (booking.get("status") or "").upper() != "CONFIRMED":
            db.set_swish_status(
                booking_id,
                "PAID",
                booking_status="CONFIRMED",
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
        if swish_status == "PAID":
            self._send_paid_sms_notifications(booking_id)

        return self.end_json(200, {"bookingId": booking_id, "swishStatus": swish_status})

    def handle_dev_swish_mark(self, raw_query: str) -> None:
        if self._swish_mode() != "mock":
            return self.api_error(403, "forbidden", "Endpoint available only when SWISH_MODE=mock", legacy_error="SWISH_MODE must be mock")

        params = parse_query(raw_query)
        booking_id_str = params.get("bookingId")
        target_status = (params.get("status") or "").upper()

        if not booking_id_str:
            return self.api_error(400, "invalid_request", "bookingId is required", legacy_error="bookingId is required")
        if target_status not in {"PAID", "FAILED"}:
            return self.api_error(400, "invalid_request", "status must be PAID or FAILED", legacy_error="status must be PAID or FAILED")
        try:
            booking_id = int(booking_id_str)
        except ValueError:
            return self.api_error(400, "invalid_request", "bookingId must be an integer", legacy_error="bookingId must be an integer")

        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.api_error(404, "not_found", "Booking not found", legacy_error="Booking not found")

        booking_status = "CONFIRMED" if target_status == "PAID" else "CANCELLED"
        db.set_swish_status(
            booking_id,
            target_status,
            booking_status=booking_status,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        if target_status == "PAID":
            self._send_paid_sms_notifications(booking_id)
        return self.end_json(200, {"bookingId": booking_id, "swishStatus": target_status, "bookingStatus": booking_status})

    def handle_swish_qr(self, params: Dict[str, str], *, head_only: bool = False) -> None:
        booking_id_str = params.get("bookingId")
        if not booking_id_str:
            return self.api_error(400, "invalid_request", "bookingId is required", legacy_error="bookingId is required")
        try:
            booking_id = int(booking_id_str)
        except ValueError:
            return self.api_error(400, "invalid_request", "bookingId must be an integer", legacy_error="bookingId must be an integer")

        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.api_error(404, "not_found", "Booking not found", legacy_error="Booking not found")
        token = booking.get("swish_token")
        if not token:
            return self.api_error(404, "not_found", "Swish token not found for booking", legacy_error="Swish token not found")

        qr_payload = self._swish_build_app_url(token)
        svg = self._render_qr_svg(qr_payload, size=320)
        body = svg.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _render_notice_svg(self, text: str, size: int = 320) -> str:
        escaped = html_lib.escape(text)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'width="{size}" height="{size}" role="img" aria-label="{escaped}">'
            '<rect width="100%" height="100%" fill="#ffffff"/>'
            '<rect x="8" y="8" width="304" height="304" rx="14" fill="#fff8e1" stroke="#f0d19f"/>'
            f'<text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" '
            'font-size="16" fill="#8a4c00" font-family="Arial, sans-serif">'
            f"{escaped}</text>"
            "</svg>"
        )

    def _render_qr_svg(self, payload: str, size: int = 320, border: int = 2) -> str:
        qr = QrCode.encode_text(payload, QrCode.Ecc.MEDIUM)
        qr_size = qr.get_size()
        scale = max(1, size // (qr_size + border * 2))
        canvas = (qr_size + border * 2) * scale
        rects = []
        for y in range(qr_size):
            for x in range(qr_size):
                if qr.get_module(x, y):
                    rects.append(
                        f'<rect x="{(x + border) * scale}" y="{(y + border) * scale}" '
                        f'width="{scale}" height="{scale}"/>'
                    )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas} {canvas}" '
            f'width="{size}" height="{size}" role="img" aria-label="Swish QR">'
            '<rect width="100%" height="100%" fill="#fff"/>'
            '<g fill="#000">'
            + "".join(rects)
            + "</g></svg>"
        )

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
        customer_phone = self._validate_optional_customer_phone(data.get("customerPhone"))
        if data.get("customerPhone") and customer_phone is None:
            return
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
                trailer_type_u, rental_type_u, start_dt, end_dt, customer_phone_temp=customer_phone
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
                "createdAt": booking.get("created_at") if booking else None,
                "price": price,
            },
        )

    def handle_swish_callback(self) -> None:
        """Handle Swish callback payload.

        TODO: In live mode, verify Swish callback authenticity (mTLS/signature)
        and map full Commerce payload fields before mutating booking state.
        """
        if self._swish_mode() != "mock":
            return self.api_error(
                501,
                "not_implemented",
                "Swish callback verification requires cert configuration",
                legacy_error="cert not configured",
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self.api_error(400, "invalid_json", "Request body must be valid JSON", legacy_error="Invalid JSON")

        booking_id = data.get("paymentReference") or data.get("bookingId")
        status = (data.get("status") or "").upper()
        if not booking_id:
            return self.api_error(400, "invalid_request", "paymentReference is required", legacy_error="paymentReference is required")
        try:
            booking_id = int(booking_id)
        except ValueError:
            return self.api_error(400, "invalid_request", "paymentReference must be integer", legacy_error="paymentReference must be integer")

        booking = db.get_booking_by_id(booking_id)
        if not booking:
            return self.api_error(404, "not_found", "Booking not found", legacy_error="Booking not found")

        if status == "PAID":
            db.set_swish_status(booking_id, "PAID", booking_status="CONFIRMED")
            booking_after = db.get_booking_by_id(booking_id)
            was_pending = bool(booking and booking.get("status") == "PENDING_PAYMENT")
            if was_pending and booking_after and booking_after.get("status") == "CONFIRMED":
                try:
                    NOTIFIER.notify_booking_confirmed(booking_after)
                except Exception:
                    logger.exception(
                        "notification dispatch failed event=booking.confirmed booking_id=%s",
                        booking_id,
                    )
            self._send_paid_sms_notifications(booking_id)
        elif status in {"FAILED", "CANCELLED", "EXPIRED", "ERROR"}:
            db.set_swish_status(booking_id, "FAILED", booking_status="CANCELLED")
        else:
            return self.api_error(400, "invalid_request", "status must be PAID or FAILED", legacy_error="invalid status")
        return self.end_json(200, {"ok": True, "bookingId": booking_id, "swishStatus": "PAID" if status == "PAID" else "FAILED"})

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
        qr_image_url = details["qrImageUrl"]
        booking_reference = details.get("bookingReference")
        swish_app_url = details.get("swishAppUrl")
        integration_message = details.get("integrationMessage")
        integration_is_configured = bool(swish_app_url)
  
        # Compose payment page with polished styling; no booking/payment behavior changes.
        html = f"""
<!doctype html><html lang=\"sv\"><head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>Betalning – Bokning {booking_id}</title>
  <style>
    :root {{
      --bg: #f2f6f9;
      --text: #1e2730;
      --muted: #5b6875;
      --surface: #ffffff;
      --border: #d7e0e8;
      --brand: #1f4f7d;
      --success: #107443;
      --radius: 16px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(1200px 500px at 20% -10%, #d7e8f6 0%, rgba(215, 232, 246, 0) 65%),
        radial-gradient(1000px 400px at 80% -15%, #ddeef0 0%, rgba(221, 238, 240, 0) 70%),
        var(--bg);
    }}
    header, footer {{
      background: linear-gradient(165deg, #123152 0%, #1f4f7d 62%, #2d628e 100%);
      color: #fff;
      padding: 16px;
      text-align: center;
    }}
    header h1 {{ margin: 0; font-size: clamp(1.5rem, 3.5vw, 2rem); }}
    main {{ max-width: 700px; margin: 0 auto; padding: 16px 12px; }}
    .progress {{
      display: inline-flex;
      border-radius: 999px;
      padding: 6px 12px;
      border: 1px solid #d2dfeb;
      background: #e6eff7;
      color: #2c5377;
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      margin-bottom: 12px;
      box-shadow: 0 10px 30px rgba(18, 35, 52, 0.08);
    }}
    .meta p {{
      margin: 8px 0;
      padding: 8px 10px;
      border-radius: 9px;
      background: #f8fafb;
      border: 1px solid #e4ebf2;
    }}
    .qr-wrap {{
      display: flex;
      justify-content: center;
      margin-top: 12px;
    }}
    .qr-wrap img {{
      max-width: min(320px, 100%);
      width: 100%;
      height: auto;
      border-radius: 14px;
      border: 1px solid #dbe5ee;
      background: #fff;
      padding: 10px;
    }}
    .swish-actions {{
      margin-top: 12px;
      display: grid;
      gap: 8px;
    }}
    .swish-warning {{
      margin-top: 10px;
      color: #8a4c00;
      background: #fff6e8;
      border: 1px solid #f0d19f;
      border-radius: 10px;
      padding: 9px 10px;
      font-weight: 600;
    }}
    .swish-open-btn {{
      border: 0;
      border-radius: 12px;
      padding: 13px 14px;
      background: #107443;
      color: #fff;
      font-weight: 700;
      font-size: 1rem;
      cursor: pointer;
    }}
    .swish-open-btn:disabled {{
      background: #8e9aa7;
      cursor: not-allowed;
    }}
    .swish-open-btn:hover,
    .swish-open-btn:focus-visible {{
      background: #0e653a;
    }}
    .swish-help {{
      color: var(--muted);
      margin: 2px 0 0;
    }}
    .swish-fallback {{
      display: none;
      margin-top: 2px;
      color: #8a4c00;
      background: #fff6e8;
      border: 1px solid #f0d19f;
      border-radius: 10px;
      padding: 9px 10px;
    }}
    .qr-alt {{
      margin-top: 14px;
      font-weight: 600;
      color: #2c5377;
    }}
    .waiting {{
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--success);
      font-weight: 700;
      margin-bottom: 8px;
    }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--success);
      animation: pulse 1.2s ease-in-out infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: .5; transform: scale(1); }}
      50% {{ opacity: 1; transform: scale(1.15); }}
    }}
    footer p {{ margin: 6px 0; }}
  </style>
</head><body>
<header><h1>Dalsjöfors Hyrservice</h1></header>
<main>
  <div class=\"progress\">Steg 4 av 5: Betalning</div>
  <div class=\"card\">
    <h2>Betala med Swish</h2>
    <p>Scanna QR-koden i din Swish-app eller öppna betalningen direkt.</p>
    <div class=\"meta\">
      <p><strong>Belopp:</strong> {price} kr</p>
      <p><strong>Bokningsreferens:</strong> {html_lib.escape(booking_reference or "saknas")}</p>
    </div>
    {"<p class=\"swish-warning\">Swish-integration ej konfigurerad. "
      + html_lib.escape(integration_message or "")
      + "</p>" if not integration_is_configured else ""}
    <div class=\"swish-actions\">
      <button id=\"open-swish\" type=\"button\" class=\"swish-open-btn\" {"disabled" if not integration_is_configured else ""}>Öppna i Swish</button>
      <p class=\"swish-help\" id=\"swish-help\">Öppna Swish på den här enheten för snabb betalning.</p>
      <p class=\"swish-fallback\" id=\"swish-fallback\">Använd QR-koden nedan.</p>
    </div>
    <p class=\"qr-alt\">Använd Swish på annan enhet</p>
    <div class=\"qr-wrap\">
      <img src=\"{html_lib.escape(qr_image_url)}\" alt=\"Swish QR\" width=\"320\" height=\"320\" />
    </div>
  </div>
  <div class=\"card\">
    <div class=\"waiting\"><span class=\"dot\" aria-hidden=\"true\"></span>Väntar på betalning …</div>
    <p>När du har betalat via Swish kommer din bokning automatiskt att bekräftas. Du kan stänga den här sidan.</p>
  </div>
</main>
<footer>
  <p>&copy; 2026 Dalsjöfors Hyrservice AB</p>
  <p>Dalsjöfors Hyrservice AB • Org.nr: 559062-4556 • Momsnr: SE559062455601 • Adress: Boråsvägen 58B, 516 34 Dalsjöfors • Telefon: 070‑457 97 09</p>
  <p>Frågor eller problem? Ring <strong>070‑457 97 09</strong></p>
</footer>
<script>
  const swishAppUrl = {json.dumps(swish_app_url, ensure_ascii=False)};
  const openBtn = document.getElementById("open-swish");
  const helpText = document.getElementById("swish-help");
  const fallbackText = document.getElementById("swish-fallback");
  const isTouch = window.matchMedia("(pointer: coarse)").matches || navigator.maxTouchPoints > 0;
  const isMobileUA = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "");
  const isMobileContext = isTouch || isMobileUA;

  if (isMobileContext) {{
    openBtn.textContent = "Öppna i Swish";
    helpText.textContent = "Tryck för att öppna Swish. Om appen inte öppnas kan du använda QR-koden.";
  }} else {{
    openBtn.textContent = "Försök öppna Swish här";
    helpText.textContent = "På dator fungerar oftast QR-koden bäst. Du kan ändå prova att öppna Swish.";
  }}

  function showFallbackInstruction() {{
    fallbackText.style.display = "block";
  }}

  openBtn.addEventListener("click", () => {{
    if (!swishAppUrl) {{
      showFallbackInstruction();
      return;
    }}
    fallbackText.style.display = "none";
    window.location = swishAppUrl;
    setTimeout(() => {{
      if (document.visibilityState === "visible") {{
        showFallbackInstruction();
      }}
    }}, 1200);
  }});
</script>
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
        trailer_text = self._trailer_label(booking["trailer_type"])
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
            f"Betalstatus: {(booking.get('swish_status') or 'PENDING').upper()}",
            f"Skapad: {booking.get('created_at') or '-'}",
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
    :root {{
      --bg: #f2f6f9;
      --text: #1e2730;
      --muted: #5b6875;
      --surface: #ffffff;
      --border: #d7e0e8;
      --brand: #1f4f7d;
      --brand-dark: #163a5c;
      --success: #107443;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(1200px 500px at 20% -10%, #d7e8f6 0%, rgba(215, 232, 246, 0) 65%),
        radial-gradient(1000px 400px at 80% -15%, #ddeef0 0%, rgba(221, 238, 240, 0) 70%),
        var(--bg);
    }}
    header, footer {{
      background: linear-gradient(165deg, #123152 0%, #1f4f7d 62%, #2d628e 100%);
      color: #fff;
      padding: 16px;
      text-align: center;
    }}
    header h1 {{ margin: 0; font-size: clamp(1.4rem, 3vw, 1.9rem); }}
    main {{ max-width: 700px; margin: 0 auto; padding: 16px 12px; }}
    .progress {{
      display: inline-flex;
      border-radius: 999px;
      padding: 6px 12px;
      border: 1px solid #d2dfeb;
      background: #e6eff7;
      color: #2c5377;
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 10px 30px rgba(18, 35, 52, 0.08);
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--success);
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .status::before {{
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--success);
    }}
    textarea {{
      width: 100%;
      min-height: 220px;
      border-radius: 10px;
      padding: 10px;
      border: 1px solid #c6d4e1;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 0.9rem;
      line-height: 1.45;
    }}
    button {{
      margin-top: 10px;
      padding: 12px 16px;
      border-radius: 12px;
      border: none;
      background: var(--brand);
      color: #fff;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--brand-dark); }}
    .code-note {{
      margin-top: 10px;
      color: var(--muted);
    }}
    footer p {{ margin: 6px 0; }}
  </style>
</head><body>
<header><h1>Bokningskvitto</h1></header>
<main>
  <div class=\"progress\">Steg 5 av 5: Bekräftelse</div>
  <div class=\"card\">
    <div class=\"status\">Betalstatus: {(booking.get("swish_status") or "PENDING").upper()}</div>
    <h2>Kvitto</h2>
    <textarea readonly id=\"confirm-text\">{confirm_text}</textarea>
    <button type=\"button\" id=\"copy-confirm\">Kopiera text</button>
    <p class=\"code-note\">Kodlåskod: <strong>6392</strong></p>
  </div>
</main>
<footer>
  <p>&copy; 2026 Dalsjöfors Hyrservice AB</p>
  <p>Dalsjöfors Hyrservice AB • Org.nr: 559062-4556 • Momsnr: SE559062455601 • Adress: Boråsvägen 58B, 516 34 Dalsjöfors • Telefon: 070‑457 97 09</p>
  <p>Frågor eller problem? Ring <strong>070‑457 97 09</strong></p>
</footer>
<script>
  const copyBtn = document.getElementById("copy-confirm");
  const confirmText = document.getElementById("confirm-text");
  copyBtn.addEventListener("click", () => {{
    navigator.clipboard.writeText(confirmText.value)
      .then(() => {{
        const oldLabel = copyBtn.textContent;
        copyBtn.textContent = "Kopierad";
        copyBtn.disabled = true;
        setTimeout(() => {{
          copyBtn.textContent = oldLabel;
          copyBtn.disabled = false;
        }}, 1200);
      }})
      .catch(() => {{
        alert("Kunde inte kopiera texten");
      }});
  }});
</script>
</body></html>
"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_or_create_payment_details(self, booking_id: int) -> Dict[str, Any]:
        """Legacy helper used by `/pay` page."""
        booking = db.get_booking_by_id(booking_id)
        if not booking:
            raise ValueError("Booking not found")
        swish_status = (booking.get("swish_status") or "").upper()
        if (
            self._is_payable_booking_status((booking.get("status") or "").upper())
            and not booking.get("swish_token")
            and not (booking.get("swish_request_id") and swish_status in db.SWISH_PENDING_STATUSES)
        ):
            now_iso = datetime.now().isoformat(timespec="seconds")
            amount = int(booking.get("price") or 0)
            booking_ref = booking.get("booking_reference") or f"BOOKING-{booking_id}"
            created = self._swish_client().create_payment_request(amount, f"DHS {booking_ref}", self._swish_callback_url())
            db.set_swish_payment_request(
                booking_id,
                instruction_uuid=created["instruction_uuid"],
                token=created["token"],
                request_id=created["request_id"],
                status="PENDING",
                created_at=now_iso,
                updated_at=now_iso,
            )
            booking = db.get_booking_by_id(booking_id)
            if not booking:
                raise ValueError("Booking not found")
        return {
            "bookingId": booking_id,
            "bookingReference": booking.get("booking_reference"),
            "price": booking["price"],
            "swishId": booking.get("swish_id"),
            "swishToken": None,
            "swishAppUrl": None,
            "qrImageUrl": f"/api/swish/qr?bookingId={booking_id}",
            "integrationStatus": "NOT_CONFIGURED",
            "integrationMessage": "Use /api/swish/paymentrequest for mock payment flow.",
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
    if is_production_environment() and not get_admin_token():
        raise RuntimeError("ADMIN_TOKEN is required in production environments")
    if is_production_environment() and not get_admin_password():
        raise RuntimeError("ADMIN_PASSWORD is required in production environments")
    if is_production_environment() and not get_admin_session_secret():
        raise RuntimeError("ADMIN_SESSION_SECRET is required in production environments")
    if not is_production_environment() and not get_admin_token():
        _warn_admin_auth_disabled_once()
    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Running Dalsjöfors Hyrservice on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
