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
import uuid
import socket
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

import db
import notifications
import requests
import sms_provider
from config import runtime
from qrcodegen import QrCode
from swish_client import SwishClient, SwishConfig


ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
EMAIL_MAX_LENGTH = 254
logger = logging.getLogger(__name__)
NOTIFIER = notifications.create_notification_service_from_env()
ADMIN_SESSION_COOKIE_NAME = "admin_session"
ADMIN_SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
ADMIN_LOGIN_FAILURE_DELAY_SECONDS = 0.3
REPORT_TYPES = {"BEFORE_RENTAL", "DURING_RENTAL", "OTHER"}
REPORT_TYPE_LABELS = {
    "BEFORE_RENTAL": "Upptäckt innan hyra",
    "DURING_RENTAL": "Skada under hyra",
    "OTHER": "Annat",
}
REPORT_ALLOWED_MIME_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}
REPORT_MAX_IMAGE_COUNT = 6
REPORT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
REPORT_MAX_TOTAL_ATTACHMENT_BYTES = 15 * 1024 * 1024
REPORT_MAX_ATTACHED_IMAGES = 3
REPORT_MAX_WEBHOOK_PAYLOAD_BYTES = 10 * 1024 * 1024
REPORT_RATE_LIMIT_WINDOW_SECONDS = 15 * 60
REPORT_RATE_LIMIT_MAX_SUBMITS = 5
REPORT_RATE_LIMIT_BY_IP: Dict[str, list[float]] = {}
MIN_WEBHOOK_SECRET_LENGTH = 32
CONFIRM_LINK_MAX_AGE_SECONDS = 60 * 60 * 24 * 45


def process_due_test_bookings(*, now: Optional[datetime] = None) -> Dict[str, int]:
    """Process ephemeral test bookings (SMS dispatch and auto-delete)."""
    effective_now = now or datetime.now()
    processed_paid = 0

    due_paid = db.get_due_test_bookings_for_auto_paid(effective_now)
    for row in due_paid:
        test_booking_id = int(row.get("id") or 0)
        if test_booking_id and db.mark_test_booking_paid(test_booking_id, now=effective_now):
            processed_paid += 1

    paid_rows = db.get_paid_test_bookings_pending_sms(effective_now)
    for row in paid_rows:
        test_booking_id = int(row.get("id") or 0)
        booking_reference = row.get("booking_reference") or f"TEST-{test_booking_id}"
        trailer_type = row.get("trailer_type") or ""
        rental_type = row.get("rental_type") or ""
        price = int(row.get("price") or 0)

        if row.get("sms_admin_sent_at") is None:
            admin_number = sms_provider.get_admin_sms_number_e164()
            if admin_number:
                admin_msg = (
                    f"TEST bokning PAID: {booking_reference} | {trailer_type} | {rental_type} | {price} kr"
                )
                if sms_provider.send_sms(admin_number, admin_msg):
                    db.mark_test_sms_admin_sent(test_booking_id, sent_at=effective_now.isoformat(timespec="seconds"))

        if row.get("sms_target_temp") and row.get("sms_target_sent_at") is None:
            target_msg = (
                f"Dalsjofors Hyrservice AB: TEST bokningskvitto {booking_reference} | {trailer_type} | {rental_type} | {price} kr"
            )
            if sms_provider.send_sms(str(row.get("sms_target_temp")), target_msg):
                db.mark_test_sms_target_sent(test_booking_id, sent_at=effective_now.isoformat(timespec="seconds"))

    deleted = db.delete_due_test_bookings(effective_now)
    return {"processedPaid": processed_paid, "deleted": deleted}


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
    return runtime.admin_token()


def get_admin_password() -> str:
    return runtime.admin_password()


def get_admin_session_secret() -> str:
    return runtime.admin_session_secret()


def _constant_time_secret_match(provided_value: str, expected_value: str) -> bool:
    provided_hash = hashlib.sha256(provided_value.encode("utf-8")).digest()
    expected_hash = hashlib.sha256(expected_value.encode("utf-8")).digest()
    return hmac.compare_digest(provided_hash, expected_hash)


def parse_query(query: str) -> Dict[str, str]:
    """Parse a URL query string into a dict of first values."""
    parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def parse_form_data(content_type: str, body: bytes) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Parse multipart or urlencoded form data without third-party dependencies."""
    normalized_content_type = (content_type or "").strip()
    lowered = normalized_content_type.lower()

    if lowered.startswith("multipart/form-data"):
        pseudo = (
            b"Content-Type: "
            + normalized_content_type.encode("utf-8", errors="ignore")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        try:
            message = BytesParser(policy=default).parsebytes(pseudo)
        except Exception as exc:
            raise ValueError("could_not_parse_multipart") from exc

        if not message.is_multipart():
            raise ValueError("invalid_multipart")

        fields: dict[str, str] = {}
        files: list[dict[str, Any]] = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue

            field_name = part.get_param("name", header="content-disposition")
            if not field_name:
                continue

            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files.append(
                    {
                        "field_name": str(field_name),
                        "filename": str(filename),
                        "content_type": (part.get_content_type() or "application/octet-stream").lower(),
                        "data_bytes": payload,
                        "size": len(payload),
                    }
                )
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            fields[str(field_name)] = text.strip()
        return fields, files

    if lowered.startswith("application/x-www-form-urlencoded"):
        query = body.decode("utf-8", errors="replace")
        parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
        fields = {key: (values[0] if values else "").strip() for key, values in parsed.items()}
        return fields, []

    raise ValueError("unsupported_content_type")


class Handler(BaseHTTPRequestHandler):
    """Custom HTTP request handler supporting API and static files."""

    server_version = "DalsjoforsHyrservice/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Silence default logging
        return

    def _request_id(self) -> str:
        value = getattr(self, "_request_id_value", "")
        if value:
            return value
        incoming = (self.headers.get("X-Request-Id") or "").strip()
        if incoming and re.match(r"^[A-Za-z0-9._:-]{1,64}$", incoming):
            rid = incoming
        else:
            rid = uuid.uuid4().hex
        self._request_id_value = rid
        return rid

    def end_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-Id", self._request_id())
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
        self.send_header("X-Request-Id", self._request_id())
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
        self.send_header("X-Request-Id", self._request_id())
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
        self.send_header("X-Request-Id", self._request_id())
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
            self.send_header("X-Request-Id", self._request_id())
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
        expected_password = get_admin_password()
        if expected_password and self._has_valid_admin_session_cookie(expected_password):
            return True

        expected_token = get_admin_token()
        if not expected_token:
            self.api_error(
                500,
                "server_misconfigured",
                "Server misconfigured",
                legacy_error="Server misconfigured",
            )
            return False

        provided_token = self._extract_admin_api_token()
        if provided_token is not None and hmac.compare_digest(provided_token, expected_token):
            return True
        self.api_error(
            401,
            "unauthorized",
            "Unauthorized",
            legacy_error="Unauthorized",
        )
        return False

    def _extract_admin_api_token(self) -> Optional[str]:
        header_token = (self.headers.get("X-Admin-Token") or "").strip()
        if header_token:
            return header_token
        return self._extract_bearer_token()

    def _extract_bearer_token(self) -> Optional[str]:
        auth_header = (self.headers.get("Authorization") or "").strip()
        if not auth_header:
            return None
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return ""
        return token.strip()

    def _confirm_link_secret(self) -> str:
        return (
            runtime.confirm_link_secret()
            or get_admin_session_secret()
            or runtime.webhook_secret()
            or get_admin_token()
        )

    def _generate_confirm_token(self, booking_id: int, *, now_ts: Optional[int] = None) -> Optional[str]:
        secret = self._confirm_link_secret()
        if not secret:
            return None
        issued_at = now_ts if now_ts is not None else int(time.time())
        expires_at = issued_at + CONFIRM_LINK_MAX_AGE_SECONDS
        payload = f"v1|{booking_id}|{expires_at}"
        signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{payload_b64}.{signature}"

    def _is_valid_confirm_token(self, booking_id: int, token: str, *, now_ts: Optional[int] = None) -> bool:
        secret = self._confirm_link_secret()
        if not secret or "." not in token:
            return False
        payload_b64, provided_signature = token.split(".", 1)
        if not payload_b64 or not provided_signature:
            return False
        try:
            payload_bytes = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
            payload = payload_bytes.decode("utf-8")
        except Exception:
            return False
        expected_signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided_signature, expected_signature):
            return False
        parts = payload.split("|")
        if len(parts) != 3 or parts[0] != "v1":
            return False
        try:
            payload_booking_id = int(parts[1])
            expires_at = int(parts[2])
        except ValueError:
            return False
        if payload_booking_id != booking_id:
            return False
        current_ts = now_ts if now_ts is not None else int(time.time())
        return current_ts <= expires_at

    def _booking_confirm_url(self, booking_id: int) -> str:
        token = self._generate_confirm_token(booking_id)
        if not token:
            return f"/confirm?bookingId={booking_id}"
        encoded_token = urllib.parse.quote(token, safe="")
        return f"/confirm?bookingId={booking_id}&token={encoded_token}"

    def require_dev_auth(self, *, path: str, raw_query: str) -> bool:
        params = parse_query(raw_query)
        booking_id = params.get("bookingId")
        status = params.get("status")
        expected_token = get_admin_token()
        authorized = True

        if expected_token:
            provided_token = self._extract_admin_api_token()
            if provided_token is None:
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
                    "Missing admin token",
                    legacy_error="Unauthorized",
                )
                return False
            if not provided_token or not hmac.compare_digest(provided_token, expected_token):
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
                    "Invalid admin token",
                    legacy_error="Forbidden",
                )
                return False
        else:
            self.api_error(
                500,
                "server_misconfigured",
                "Server misconfigured",
                legacy_error="Server misconfigured",
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

    def _validate_optional_customer_email(self, raw_value: Optional[str]) -> Optional[str]:
        value = (raw_value or "").strip().lower()
        if not value:
            return None
        if len(value) > EMAIL_MAX_LENGTH or "@" not in value:
            return None
        return value

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
                f"Dalsjofors Hyrservice AB (Org.nr 559062-4556): Bokningskvitto: {booking_ref} | {trailer_label} | "
                f"{period_label} | {price_label} | Betalning: PAID"
            )
            if sms_provider.send_sms(customer_phone, customer_message):
                db.mark_sms_customer_sent(booking_id)
                db.clear_customer_phone_temp(booking_id)

        booking = db.get_booking_by_id(booking_id) or booking
        receipt_booking = booking
        required_receipt_fields = (
            "id",
            "booking_reference",
            "trailer_type",
            "start_dt",
            "end_dt",
            "price",
            "customer_email_temp",
            "receipt_requested_temp",
        )
        if any(receipt_booking.get(field) in (None, "") for field in required_receipt_fields):
            receipt_booking = db.get_booking_by_id(booking_id) or receipt_booking
        booking_reference = receipt_booking.get("booking_reference")
        if str(booking_reference or "").startswith("TEST-"):
            logger.info(
                "RECEIPT_WEBHOOK_SKIP reason=test_booking bookingId=%s bookingReference=%s",
                booking_id,
                booking_reference,
            )
            return
        if not receipt_booking.get("receipt_requested_temp"):
            return
        if not receipt_booking.get("customer_email_temp"):
            logger.info(
                "RECEIPT_WEBHOOK_SKIP reason=missing_email bookingId=%s bookingReference=%s",
                booking_id,
                booking_reference,
            )
            return
        if not runtime.notify_webhook_url():
            logger.info(
                "RECEIPT_WEBHOOK_SKIP reason=missing_env bookingId=%s bookingReference=%s",
                booking_id,
                booking_reference,
            )
            return
        if not db.claim_receipt_webhook_send(booking_id):
            logger.info(
                "RECEIPT_WEBHOOK_SKIP reason=already_inflight_or_sent bookingId=%s bookingReference=%s",
                booking_id,
                booking_reference,
            )
            return
        if notifications.send_receipt_webhook(receipt_booking):
            db.mark_receipt_webhook_sent(booking_id)
            db.clear_receipt_temp_fields(booking_id)
            return
        db.release_receipt_webhook_lock(booking_id)

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
        logger.info("REQUEST method=GET path=%s requestId=%s", path, self._request_id())
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
        if path == "/terms":
            return self.serve_file("terms.html", "text/html; charset=utf-8")
        if path == "/report-issue":
            return self.serve_report_issue_page()
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
        if path == "/api/availability-slots":
            return self.handle_availability_slots(query_params)
        if path == "/api/payment":
            return self.handle_payment(query_params)
        if path == "/api/payment-status":
            return self.handle_payment_status(query_params)
        if path == "/api/swish/qr":
            return self.handle_swish_qr(query_params)
        if path == "/api/version":
            return self.handle_version()
        if path.startswith("/api/dev/"):
            if path == "/api/dev/netcheck":
                logger.info("DEV_NETCHECK path=%s", path)
                return self.handle_dev_netcheck(query_params)
            if path == "/api/dev/report-webhook-test":
                return self.handle_dev_report_webhook_test()
            return self.end_json(404, {"error": "Not Found"})
        if path.startswith("/api/admin/"):
            if not self.require_admin_api_auth():
                return
        if path == "/api/admin/bookings":
            return self.handle_admin_bookings(query_params)
        if path == "/api/admin/test-bookings":
            return self.handle_admin_test_bookings_get(query_params)
        if path == "/api/admin/blocks":
            return self.handle_admin_blocks_get(query_params)

        # Dev/test endpoint
        if path == "/api/health":
            return self.end_json(
                200,
                {
                    "ok": True,
                    "service": "dalsjofors-hyrservice",
                    "commit": self._resolve_commit_value(),
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
        logger.info("REQUEST method=HEAD path=%s requestId=%s", path, self._request_id())
        if path.startswith("/api/dev/"):
            if not self.require_dev_auth(path=path, raw_query=parsed.query):
                return
        if path == "/api/swish/qr":
            return self.handle_swish_qr(query_params, head_only=True)
        self.send_response(404)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-Id", self._request_id())
        self.end_headers()

    def do_POST(self) -> None:
        """Handle HTTP POST requests.

        Supported POST endpoints now include booking holds and Swish
        callbacks.  Outdated bookings are expired up front to avoid
        lingering reservations.
        """
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        logger.info("REQUEST method=POST path=%s requestId=%s", path, self._request_id())
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
        if path == "/report-issue":
            return self.handle_report_issue_submit()
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
        if path == "/api/admin/test-bookings":
            return self.handle_admin_test_bookings_create()
        if path == "/api/admin/test-bookings/run":
            return self.handle_admin_test_bookings_run()
        if path == "/api/admin/expire-pending":
            return self.handle_admin_expire_pending()

        return self.end_json(404, {"error": "Not Found"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_params = parse_query(parsed.query)
        logger.info("REQUEST method=DELETE path=%s requestId=%s", path, self._request_id())
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
        payload: Dict[str, Any] = {"available": available, "remaining": remaining}
        if block:
            payload["blocked"] = True
            payload["blockReason"] = (block.get("reason") or "").strip() or "Administrativ blockering"
        else:
            payload["blocked"] = False
            payload["blockReason"] = ""
        return self.end_json(200, payload)

    def handle_availability_slots(self, params: Dict[str, str]) -> None:
        trailer_type_u = self._validate_trailer_type(params.get("trailerType"))
        rental_type_u = self._validate_rental_type(params.get("rentalType"))
        date_str = self._validate_date(params.get("date"))
        if trailer_type_u is None or rental_type_u is None or date_str is None:
            return
        if rental_type_u != "TWO_HOURS":
            return self.api_error(
                400,
                "invalid_request",
                "Endpoint supports TWO_HOURS only",
                legacy_error="rentalType must be TWO_HOURS",
            )

        slots: list[Dict[str, Any]] = []
        for hour in range(8, 19):
            for minute in (0, 30):
                start_time = f"{hour:02d}:{minute:02d}"
                start_dt = datetime.strptime(f"{date_str}T{start_time}", "%Y-%m-%dT%H:%M")
                end_dt = start_dt + timedelta(hours=2)
                block = db.find_block_overlap(trailer_type_u, start_dt, end_dt)
                if block:
                    remaining = 0
                    available = False
                    block_reason = (block.get("reason") or "").strip() or "Administrativ blockering"
                else:
                    overlapping = db.count_overlapping_active_bookings(trailer_type_u, start_dt, end_dt)
                    remaining = max(0, db.TRAILERS_PER_TYPE - overlapping)
                    available = remaining > 0
                    block_reason = ""
                slots.append(
                    {
                        "time": start_time,
                        "available": available,
                        "remaining": remaining,
                        "blocked": bool(block),
                        "blockReason": block_reason,
                    }
                )
        return self.end_json(200, {"slots": slots})

    def _swish_mode(self) -> str:
        return runtime.swish_mode()

    def _swish_client(self) -> SwishClient:
        return SwishClient(
            SwishConfig(
                base_url=runtime.swish_api_url(),
                merchant_alias=runtime.swish_merchant_alias(),
                callback_url=self._swish_callback_url(),
                cert_path=runtime.swish_cert_path() or None,
                key_path=runtime.swish_key_path() or None,
                ca_path=runtime.swish_ca_path() or None,
                mock=self._swish_mode() == "mock",
            )
        )

    def _swish_callback_url(self) -> str:
        configured = runtime.swish_callback_url()
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

    def handle_dev_netcheck(self, params: Dict[str, str]) -> None:
        host = (params.get("host") or "").strip()
        port_raw = (params.get("port") or "").strip()
        if not host:
            return self.api_error(400, "invalid_request", "host is required", legacy_error="host is required")
        if not port_raw:
            return self.api_error(400, "invalid_request", "port is required", legacy_error="port is required")
        try:
            port = int(port_raw)
        except ValueError:
            return self.api_error(400, "invalid_request", "port must be an integer", legacy_error="port must be an integer")
        if port < 1 or port > 65535:
            return self.api_error(400, "invalid_request", "port must be between 1 and 65535", legacy_error="port must be between 1 and 65535")

        try:
            with socket.create_connection((host, port), timeout=5):
                return self.end_json(200, {"ok": True})
        except Exception as exc:
            return self.end_json(200, {"ok": False, "error": str(exc)})

    def handle_dev_report_webhook_test(self) -> None:
        webhook_url = (
            runtime.report_webhook_url()
            or runtime.notify_webhook_url()
        )
        report_to = runtime.report_to()
        webhook_secret = runtime.webhook_secret()
        report_id = str(uuid.uuid4())
        submitted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not webhook_url:
            return self.end_json(
                200,
                {
                    "ok": False,
                    "error": "missing webhook url (REPORT_WEBHOOK_URL/NOTIFY_WEBHOOK_URL)",
                    "url": "",
                },
            )

        payload: Dict[str, Any] = {
            "type": "issue_report",
            "secret": webhook_secret,
            "to": report_to,
            "subject": "TEST issue_report",
            "reportId": report_id,
            "submittedAt": submitted_at,
            "attachmentNames": [],
            "attachmentCount": 0,
            "friendlyFields": {
                "Släp": "TEST",
                "Bokningsreferens": "",
                "Typ av rapport": "Skada under hyra",
                "Upptäckt datum/tid": "2026-02-21T13:10",
                "Namn": "Dev Test",
                "Telefon": "0700000000",
                "E-post": "test@test.se",
                "Beskrivning": "Test från /api/dev/report-webhook-test",
            },
            "fields": {
                "name": "Dev Test",
                "phone": "0700000000",
                "email": "test@test.se",
                "trailer": "TEST",
            },
            "message": "Test från /api/dev/report-webhook-test",
        }
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            return self.end_json(
                200,
                {
                    "ok": True,
                    "status": int(resp.status_code or 0),
                    "body": (resp.text or "")[:500],
                    "url": webhook_url,
                },
            )
        except Exception as exc:
            return self.end_json(
                200,
                {
                    "ok": False,
                    "error": str(exc),
                    "url": webhook_url,
                },
            )

    def _resolve_commit_value(self) -> str:
        for var_name in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "COMMIT_SHA", "SOURCE_VERSION"):
            value = (os.environ.get(var_name) or "").strip()
            if value:
                return value
        return "unknown"

    def handle_version(self) -> None:
        return self.end_json(
            200,
            {
                "commit": self._resolve_commit_value(),
                "time": datetime.now().isoformat(timespec="seconds"),
                "service": "dalsjofors-hyrservice",
            },
        )

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
        self.send_header("X-Request-Id", self._request_id())
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
                    "confirmUrl": self._booking_confirm_url(int(booking["id"])),
                }
            )
        return self.end_json(200, {"bookings": payload_rows})

    def handle_admin_test_bookings_get(self, params: Dict[str, str]) -> None:
        limit_raw = (params.get("limit") or "").strip()
        limit = 10
        if limit_raw:
            try:
                limit = max(1, min(100, int(limit_raw)))
            except ValueError:
                return self._invalid_field_error({"limit": "Must be an integer"})
        rows = db.list_test_bookings(limit=limit)
        payload_rows = []
        for row in rows:
            payload_rows.append(
                {
                    "id": row.get("id"),
                    "bookingReference": row.get("booking_reference"),
                    "trailerType": row.get("trailer_type"),
                    "rentalType": row.get("rental_type"),
                    "price": row.get("price"),
                    "status": row.get("status"),
                    "createdAt": row.get("created_at"),
                }
            )
        return self.end_json(200, {"testBookings": payload_rows})

    def handle_admin_test_bookings_create(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self.api_error(400, "invalid_json", "Request body must be valid JSON", legacy_error="Invalid JSON")

        sms_to = (data.get("smsTo") or "").strip()
        trailer_type = (data.get("trailerType") or "").strip().upper()
        rental_type = (data.get("rentalType") or "").strip().upper()
        date_raw = (data.get("date") or "").strip()
        if not sms_to:
            return self._invalid_field_error({"smsTo": "This field is required"})
        sms_to_e164 = sms_provider.normalize_swedish_mobile(sms_to)
        if not sms_to_e164:
            return self._invalid_field_error({"smsTo": "Ange svensk mobil: +46xxxxxxxxx eller 07xxxxxxxx"})
        if trailer_type not in db.VALID_TEST_TRAILER_TYPES:
            return self._invalid_field_error({"trailerType": f"Must be one of: {', '.join(sorted(db.VALID_TEST_TRAILER_TYPES))}"})
        if rental_type not in db.VALID_TEST_RENTAL_TYPES:
            return self._invalid_field_error({"rentalType": f"Must be one of: {', '.join(sorted(db.VALID_TEST_RENTAL_TYPES))}"})
        if not DATE_RE.match(date_raw):
            return self._invalid_field_error({"date": "Expected format YYYY-MM-DD"})

        price = 250
        created_row = db.create_test_booking(
            trailer_type=trailer_type,
            rental_type=rental_type,
            price=price,
            sms_target_temp=sms_to_e164,
        )
        return self.end_json(
            201,
            {
                "id": created_row.get("id"),
                "bookingReference": created_row.get("booking_reference"),
                "status": created_row.get("status"),
            },
        )

    def handle_admin_test_bookings_run(self) -> None:
        result = process_due_test_bookings()
        return self.end_json(200, result)

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
        receipt_requested_raw = data.get("receiptRequested", False)
        customer_email = self._validate_optional_customer_email(data.get("customerEmail"))
        if data.get("customerPhone") and customer_phone is None:
            return
        if not isinstance(receipt_requested_raw, bool):
            return self._invalid_field_error({"receiptRequested": "Must be boolean true/false"})
        receipt_requested = receipt_requested_raw
        if receipt_requested and not customer_email:
            error_text = "Om du vill ha kvitto via e-post måste du ange en giltig e-postadress."
            return self.api_error(
                400,
                "invalid_request",
                error_text,
                legacy_error=error_text,
                details={"fields": {"customerEmail": "Ogiltig eller saknas"}},
            )
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
        now_dt = datetime.now()
        if end_dt <= now_dt:
            return self._invalid_field_error({"date": "Bokningstid måste ligga i framtiden"})
        # Create booking hold using existing logic
        try:
            booking_id, price = db.create_booking(
                trailer_type_u,
                rental_type_u,
                start_dt,
                end_dt,
                customer_phone_temp=customer_phone,
                customer_email_temp=customer_email if receipt_requested else None,
                receipt_requested_temp=receipt_requested,
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
                "confirmUrl": self._booking_confirm_url(booking_id),
            },
        )

    def handle_swish_callback(self) -> None:
        """Handle Swish callback payload.

        In mock mode, callback is open for local/test flows.
        In non-mock mode, callback requires webhook secret validation and is
        intended to be used behind TLS/mTLS-capable infrastructure.
        """
        if self._swish_mode() != "mock":
            expected_secret = runtime.webhook_secret()
            if not expected_secret:
                return self.api_error(
                    500,
                    "server_misconfigured",
                    "Server misconfigured",
                    legacy_error="Server misconfigured",
                )
            provided_secret = (
                (self.headers.get("X-Webhook-Secret") or "").strip()
                or (self.headers.get("X-Swish-Webhook-Secret") or "").strip()
            )
            if not provided_secret or not hmac.compare_digest(provided_secret, expected_secret):
                return self.api_error(
                    401,
                    "unauthorized",
                    "Unauthorized",
                    legacy_error="Unauthorized",
                )
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self.api_error(400, "invalid_json", "Request body must be valid JSON", legacy_error="Invalid JSON")

        booking_id = data.get("paymentReference") or data.get("bookingId") or data.get("id")
        status_raw = str(data.get("status") or data.get("paymentStatus") or "").upper()
        status = "PAID" if status_raw in {"PAID", "COMPLETED"} else status_raw
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

    def _report_client_ip(self) -> str:
        forwarded_for = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded_for:
            return forwarded_for
        return (self.client_address[0] if self.client_address else "") or "unknown"

    def _report_rate_limited(self, ip_address: str) -> bool:
        now_ts = time.time()
        recent = [
            ts for ts in REPORT_RATE_LIMIT_BY_IP.get(ip_address, []) if now_ts - ts < REPORT_RATE_LIMIT_WINDOW_SECONDS
        ]
        if len(recent) >= REPORT_RATE_LIMIT_MAX_SUBMITS:
            REPORT_RATE_LIMIT_BY_IP[ip_address] = recent
            return True
        recent.append(now_ts)
        REPORT_RATE_LIMIT_BY_IP[ip_address] = recent
        return False

    def _report_issue_default_values(self) -> Dict[str, str]:
        return {
            "name": "",
            "phone": "",
            "email": "",
            "trailer_type": "",
            "booking_reference": "",
            "detected_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "report_type": "",
            "message": "",
            "website": "",
        }

    def _valid_report_email(self, value: str) -> bool:
        return bool(value and len(value) <= EMAIL_MAX_LENGTH and "@" in value and "." in value.split("@")[-1])

    def _render_report_issue_page(
        self,
        *,
        values: Optional[Dict[str, str]] = None,
        field_errors: Optional[Dict[str, str]] = None,
        global_error: Optional[str] = None,
        success_message: Optional[str] = None,
    ) -> str:
        form_values = self._report_issue_default_values()
        if values:
            form_values.update({k: (v or "") for k, v in values.items()})
        errors = field_errors or {}
        report_options_html = "".join(
            [
                f"<option value=\"{key}\" {'selected' if form_values.get('report_type') == key else ''}>{html_lib.escape(label)}</option>"
                for key, label in REPORT_TYPE_LABELS.items()
            ]
        )
        trailer_options_html = "<option value=\"\">Välj släp</option>" + "".join(
            [
                (
                    f"<option value=\"{trailer}\" {'selected' if form_values.get('trailer_type') == trailer else ''}>"
                    f"{html_lib.escape(self._trailer_label(trailer))}</option>"
                )
                for trailer in sorted(db.VALID_TRAILER_TYPES)
            ]
        )

        def error_text(field_name: str) -> str:
            value = errors.get(field_name)
            if not value:
                return ""
            return f"<p class=\"field-error\">{html_lib.escape(value)}</p>"

        success_html = (
            f"<p class=\"alert success\">{html_lib.escape(success_message)}</p>" if success_message else ""
        )
        global_error_html = f"<p class=\"alert\">{html_lib.escape(global_error)}</p>" if global_error else ""
        return f"""<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Rapportera fel/skada – Dalsjöfors Hyrservice</title>
  <link rel="stylesheet" href="/static/report.css">
</head>
<body>
  <header class="site-header">
    <h1>Dalsjöfors Hyrservice</h1>
    <p>Rapportera fel eller skada på släp</p>
  </header>
  <main>
    <section class="panel">
      <h2>Rapportera fel/skada</h2>
      <p class="intro">Skicka in uppgifterna nedan så återkommer vi så snart som möjligt.</p>
      <p class="back-link-wrap">
        <a class="button-secondary" href="/">Boka släp istället</a>
      </p>
      {success_html}
      {global_error_html}
      <form method="post" action="/report-issue" enctype="multipart/form-data" novalidate>
        <div class="grid">
          <label>
            Namn
            <input type="text" name="name" maxlength="120" required value="{html_lib.escape(form_values['name'])}" />
            {error_text("name")}
          </label>
          <label>
            Telefon
            <input type="tel" name="phone" maxlength="40" required value="{html_lib.escape(form_values['phone'])}" />
            {error_text("phone")}
          </label>
          <label>
            E-post
            <input type="email" name="email" maxlength="254" required value="{html_lib.escape(form_values['email'])}" />
            {error_text("email")}
          </label>
          <label>
            Välj släp
            <select name="trailer_type" required>
              {trailer_options_html}
            </select>
            {error_text("trailer_type")}
          </label>
          <label>
            Bokningsreferens eller Booking ID
            <input type="text" name="booking_reference" maxlength="80" value="{html_lib.escape(form_values['booking_reference'])}" />
          </label>
          <label>
            Datum/tid när felet upptäcktes
            <input type="datetime-local" name="detected_at" required value="{html_lib.escape(form_values['detected_at'])}" />
            {error_text("detected_at")}
          </label>
          <label>
            Typ av rapport
            <select name="report_type" required>
              <option value="">Välj typ</option>
              {report_options_html}
            </select>
            {error_text("report_type")}
          </label>
          <label>
            Bilder (1-6 valfria, jpg/png/webp, max 5 MB/st)
            <input type="file" name="images" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" multiple />
            <p class="hint">Vid stora filer bifogas max 3 bilder automatiskt.</p>
            {error_text("images")}
          </label>
          <label class="full">
            Meddelande / beskrivning
            <textarea name="message" required maxlength="5000">{html_lib.escape(form_values['message'])}</textarea>
            {error_text("message")}
          </label>
          <label class="hp-field" aria-hidden="true">
            Lämna detta fält tomt
            <input type="text" name="website" tabindex="-1" autocomplete="off" value="{html_lib.escape(form_values['website'])}" />
          </label>
        </div>
        <div class="actions">
          <button type="submit">Skicka rapport</button>
        </div>
      </form>
    </section>
  </main>
  <footer class="site-footer">
    <p>&copy; 2026 Dalsjöfors Hyrservice AB</p>
    <p>Dalsjöfors Hyrservice AB • Org.nr: 559062-4556 • Boråsvägen 58B, 516 34 Dalsjöfors</p>
  </footer>
</body>
</html>"""

    def serve_report_issue_page(self) -> None:
        html = self._render_report_issue_page()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-Id", self._request_id())
        self.end_headers()
        self.wfile.write(body)

    def _send_issue_report_webhook(
        self,
        fields: Dict[str, str],
        images: list[Dict[str, Any]],
    ) -> bool:
        webhook_url = (
            runtime.report_webhook_url()
            or runtime.notify_webhook_url()
        )
        report_to = runtime.report_to()
        webhook_secret = runtime.webhook_secret()
        report_id = str(uuid.uuid4())
        submitted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not webhook_url:
            logger.error("REPORT_WEBHOOK_MISSING url_env=REPORT_WEBHOOK_URL/NOTIFY_WEBHOOK_URL")
            return False

        report_type = REPORT_TYPE_LABELS.get(fields["report_type"], fields["report_type"])
        subject = (
            f"Skaderapport – {self._trailer_label(fields['trailer_type'])} – "
            f"{fields['name']} – {fields['detected_at']}"
        )
        message_text = fields["message"]

        selected_images: list[Dict[str, Any]] = []
        total_selected_bytes = 0
        omitted_images = 0
        for item in images:
            payload = item["data"]
            if len(selected_images) >= REPORT_MAX_ATTACHED_IMAGES:
                omitted_images += 1
                continue
            if total_selected_bytes + len(payload) > REPORT_MAX_TOTAL_ATTACHMENT_BYTES:
                omitted_images += 1
                continue
            selected_images.append(item)
            total_selected_bytes += len(payload)
        if omitted_images > 0:
            message_text = (
                f"{message_text}\n\n"
                f"OBS: {omitted_images} bild(er) bifogades inte på grund av storleksgräns. "
                "Be kunden skicka fler bilder vid behov."
            )

        attachments_payload: list[Dict[str, str]] = []
        for item in selected_images:
            attachments_payload.append(
                {
                    "filename": str(item["filename"]),
                    "contentType": str(item["content_type"]),
                    "dataBase64": base64.b64encode(item["data"]).decode("ascii"),
                }
            )
        attachment_names = [item["filename"] for item in attachments_payload]

        payload: Dict[str, Any] = {
            "type": "issue_report",
            "secret": webhook_secret,
            "to": report_to,
            "subject": subject,
            "reportId": report_id,
            "submittedAt": submitted_at,
            "attachmentNames": attachment_names,
            "attachmentCount": len(attachments_payload),
            "friendlyFields": {
                "Släp": self._trailer_label(fields["trailer_type"]),
                "Bokningsreferens": fields.get("booking_reference") or "",
                "Typ av rapport": report_type,
                "Upptäckt datum/tid": fields["detected_at"],
                "Namn": fields["name"],
                "Telefon": fields["phone"],
                "E-post": fields["email"],
                "Beskrivning": fields["message"],
            },
            "fields": {
                "name": fields["name"],
                "phone": fields["phone"],
                "email": fields["email"],
                "trailer_type": fields["trailer_type"],
                "trailer_label": self._trailer_label(fields["trailer_type"]),
                "booking_reference": fields.get("booking_reference") or "",
                "detected_at": fields["detected_at"],
                "report_type": fields["report_type"],
                "report_type_label": report_type,
                "message": fields["message"],
                "website": fields.get("website") or "",
            },
            "message": message_text,
            "attachments": attachments_payload,
        }
        booking_ref = (fields.get("booking_reference") or "").strip()
        if booking_ref:
            payload["bookingRef"] = booking_ref

        payload_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if payload_size > REPORT_MAX_WEBHOOK_PAYLOAD_BYTES and attachments_payload:
            payload["attachments"] = []
            payload["attachmentNames"] = []
            payload["attachmentCount"] = 0
            too_large_message = "Bilder kunde inte bifogas pga storlek, be kunden skicka separat"
            payload["message"] = f"{message_text}\n\n{too_large_message}" if message_text else too_large_message
        approx_payload_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        try:
            logger.warning(
                "REPORT_WEBHOOK_ATTEMPT url=%s to=%s subject=%s att_count=%d payload_bytes=%d",
                webhook_url,
                report_to,
                subject,
                len(payload.get("attachments", [])),
                approx_payload_size,
            )
            resp = requests.post(webhook_url, json=payload, timeout=15)
            logger.warning(
                "REPORT_WEBHOOK_RESPONSE status=%s body_snippet=%s",
                resp.status_code,
                (resp.text or "")[:500],
            )
            if 200 <= int(resp.status_code or 0) < 300:
                return True
            logger.error(
                "REPORT_WEBHOOK_SEND_FAILED status=%s to=%s",
                resp.status_code,
                report_to,
            )
            return False
        except Exception:
            logger.exception("REPORT_WEBHOOK_SEND_FAILED to=%s", report_to)
            return False

    def handle_report_issue_submit(self) -> None:
        content_type_header = self.headers.get("Content-Type") or ""
        content_type = content_type_header.lower()
        if not (
            content_type.startswith("multipart/form-data")
            or content_type.startswith("application/x-www-form-urlencoded")
        ):
            return self.end_json(
                400,
                {
                    "error": (
                        "Felaktigt format. Formuläret måste skickas som multipart/form-data "
                        "eller application/x-www-form-urlencoded."
                    )
                },
            )

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > 40 * 1024 * 1024:
            html = self._render_report_issue_page(
                global_error="För stor eller ogiltig förfrågan. Kontrollera bildernas storlek och försök igen."
            )
            body = html.encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", self._request_id())
            self.end_headers()
            self.wfile.write(body)
            return

        body_bytes = self.rfile.read(content_length)
        try:
            parsed_fields, parsed_files = parse_form_data(content_type_header, body_bytes)
        except ValueError:
            html = self._render_report_issue_page(
                global_error="Kunde inte läsa formuläret. Kontrollera bildernas format och försök igen."
            )
            body = html.encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", self._request_id())
            self.end_headers()
            self.wfile.write(body)
            return

        def field(name: str) -> str:
            value = parsed_fields.get(name, "")
            return str(value).strip()

        values = {
            "name": field("name"),
            "phone": field("phone"),
            "email": field("email").lower(),
            "trailer_type": field("trailer_type").upper(),
            "booking_reference": field("booking_reference"),
            "detected_at": field("detected_at"),
            "report_type": field("report_type").upper(),
            "message": field("message"),
            "website": field("website"),
        }

        if values["website"]:
            html = self._render_report_issue_page(success_message="Rapport mottagen. Vi återkommer.")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", self._request_id())
            self.end_headers()
            self.wfile.write(body)
            return

        if self._report_rate_limited(self._report_client_ip()):
            html = self._render_report_issue_page(
                values=values,
                global_error="För många försök just nu. Vänta en stund och försök igen.",
            )
            body = html.encode("utf-8")
            self.send_response(429)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", self._request_id())
            self.end_headers()
            self.wfile.write(body)
            return

        errors: Dict[str, str] = {}
        if not values["name"]:
            errors["name"] = "Namn är obligatoriskt."
        if not values["phone"]:
            errors["phone"] = "Telefon är obligatoriskt."
        if not values["email"]:
            errors["email"] = "E-post är obligatorisk."
        elif not self._valid_report_email(values["email"]):
            errors["email"] = "Ange en giltig e-postadress."
        if values["trailer_type"] not in db.VALID_TRAILER_TYPES:
            errors["trailer_type"] = "Välj ett giltigt släp."
        if not values["detected_at"]:
            errors["detected_at"] = "Datum och tid är obligatoriskt."
        else:
            try:
                datetime.strptime(values["detected_at"], "%Y-%m-%dT%H:%M")
            except ValueError:
                errors["detected_at"] = "Ange datum/tid i formatet ÅÅÅÅ-MM-DD TT:MM."
        if values["report_type"] not in REPORT_TYPES:
            errors["report_type"] = "Välj en rapporttyp."
        if not values["message"]:
            errors["message"] = "Beskrivning är obligatorisk."

        image_items = [
            item for item in parsed_files if str(item.get("field_name") or "") in {"images", "photos"}
        ]
        image_payloads: list[Dict[str, Any]] = []
        uploaded_count = 0
        for item in image_items:
            if not item.get("filename"):
                continue
            uploaded_count += 1
            if uploaded_count > REPORT_MAX_IMAGE_COUNT:
                errors["images"] = f"Du kan ladda upp max {REPORT_MAX_IMAGE_COUNT} bilder."
                break
            original_name = Path(str(item["filename"])).name
            extension = Path(original_name).suffix.lower()
            content_type_value = str(item.get("content_type") or "").lower()
            allowed_extensions = REPORT_ALLOWED_MIME_TYPES.get(content_type_value)
            if not allowed_extensions or extension not in allowed_extensions:
                errors["images"] = "Endast jpg, png eller webp är tillåtna."
                break
            payload = item.get("data_bytes") or b""
            if len(payload) > REPORT_MAX_IMAGE_BYTES:
                errors["images"] = "Varje bild får vara max 5 MB."
                break
            if not payload:
                continue

            target_extension = ".jpg" if content_type_value == "image/jpeg" else extension
            target_payload = payload
            target_content_type = content_type_value
            try:
                from PIL import Image  # type: ignore

                with Image.open(BytesIO(payload)) as image:
                    max_width = 1600
                    if image.width > max_width:
                        ratio = max_width / float(image.width)
                        resized = image.resize((max_width, max(1, int(image.height * ratio))))
                    else:
                        resized = image
                    buffer = BytesIO()
                    if content_type_value == "image/png":
                        resized.save(buffer, format="PNG", optimize=True)
                        target_extension = ".png"
                        target_content_type = "image/png"
                    elif content_type_value == "image/webp":
                        resized.save(buffer, format="WEBP", quality=85)
                        target_extension = ".webp"
                        target_content_type = "image/webp"
                    else:
                        if resized.mode not in {"RGB", "L"}:
                            resized = resized.convert("RGB")
                        resized.save(buffer, format="JPEG", quality=85, optimize=True)
                        target_extension = ".jpg"
                        target_content_type = "image/jpeg"
                    target_payload = buffer.getvalue()
            except Exception:
                pass

            image_payloads.append(
                {
                    "filename": f"report_{uuid.uuid4().hex}{target_extension}",
                    "content_type": target_content_type,
                    "data": target_payload,
                }
            )

        if errors:
            html = self._render_report_issue_page(values=values, field_errors=errors)
            body = html.encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", self._request_id())
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._send_issue_report_webhook(values, image_payloads):
            html = self._render_report_issue_page(
                values=values,
                global_error="Rapporten kunde inte skickas just nu. Försök igen senare.",
            )
            body = html.encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", self._request_id())
            self.end_headers()
            self.wfile.write(body)
            return

        html = self._render_report_issue_page(success_message="Rapport mottagen. Vi återkommer.")
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", self._request_id())
        self.end_headers()
        self.wfile.write(body)

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
<header>
  <h1>Dalsjöfors Hyrservice</h1>
</header>
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
        self.send_header("X-Request-Id", self._request_id())
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
        token = (params.get("token") or "").strip()
        if not self._is_valid_confirm_token(booking_id, token):
            expected_password = get_admin_password()
            has_admin_session = bool(
                expected_password and self._has_valid_admin_session_cookie(expected_password)
            )
            if not has_admin_session:
                return self.end_html_message(
                    403,
                    "Otillåten åtkomst",
                    "Länken är ogiltig eller har löpt ut.",
                )
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
<header>
  <h1>Bokningskvitto</h1>
</header>
<main>
  <div class=\"progress\">Steg 5 av 5: Bekräftelse</div>
  <div class=\"card\">
    <div class=\"status\">Betalstatus: {(booking.get("swish_status") or "PENDING").upper()}</div>
    <h2>Kvitto</h2>
    <textarea readonly id=\"confirm-text\">{confirm_text}</textarea>
    <button type=\"button\" id=\"copy-confirm\">Kopiera text</button>
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
        self.send_header("X-Request-Id", self._request_id())
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
        self.send_header("X-Request-Id", self._request_id())
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
    if is_production_environment():
        webhook_secret = runtime.webhook_secret()
        if len(webhook_secret) < MIN_WEBHOOK_SECRET_LENGTH:
            raise RuntimeError(
                f"WEBHOOK_SECRET must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters in production environments"
            )
    port = runtime.port()
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Running Dalsjöfors Hyrservice on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
