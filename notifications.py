"""Notification services for booking lifecycle events.

This module defines a small abstraction with pluggable providers.
A safe log provider is always enabled, and a webhook provider can be
enabled through environment variables.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.request
from typing import Any, Protocol

import db
import requests
from config import runtime

logger = logging.getLogger(__name__)

COMPANY_NAME = "Dalsjöfors Hyrservice AB"
ORGANIZATION_NUMBER = "559062-4556"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class NotificationProvider(Protocol):
    def send(self, event: str, payload: dict[str, Any]) -> None:
        """Send a notification for a booking event."""


class LogNotificationProvider:
    """Default provider that logs notification payloads."""

    def send(self, event: str, payload: dict[str, Any]) -> None:
        logger.info("booking_notification event=%s payload=%s", event, json.dumps(payload, ensure_ascii=False))


class WebhookNotificationProvider:
    """Provider that POSTs notifications to a webhook endpoint."""

    def __init__(self, url: str, secret: str | None = None, timeout_seconds: int = 3) -> None:
        self.url = url
        self.secret = secret
        self.timeout_seconds = timeout_seconds

    def send(self, event: str, payload: dict[str, Any]) -> None:
        body_obj = {"event": event, "payload": payload}
        body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self.secret:
            digest = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Notify-Signature"] = f"sha256={digest}"

        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_seconds):
            return


class NotificationService:
    """Coordinates one or more providers and fails safely."""

    def __init__(self, providers: list[NotificationProvider]) -> None:
        self.providers = providers

    def notify_booking_created(self, booking: dict[str, Any]) -> None:
        payload = build_booking_payload(booking)
        self._send("booking.created", payload)

    def notify_booking_confirmed(self, booking: dict[str, Any]) -> None:
        payload = build_booking_payload(booking)
        self._send("booking.confirmed", payload)

    def _send(self, event: str, payload: dict[str, Any]) -> None:
        for provider in self.providers:
            try:
                provider.send(event, payload)
            except Exception:
                logger.exception("notification provider failed event=%s provider=%s", event, provider.__class__.__name__)


def build_booking_payload(booking: dict[str, Any]) -> dict[str, Any]:
    """Build normalized notification payload from a booking row."""
    return {
        "bookingReference": booking.get("booking_reference"),
        "trailerType": booking.get("trailer_type"),
        "rentalType": booking.get("rental_type"),
        "startDatetime": booking.get("start_dt"),
        "endDatetime": booking.get("end_dt"),
        "status": booking.get("status"),
        "price": booking.get("price"),
    }


def create_notification_service_from_env() -> NotificationService:
    """Create notification service with only safe providers.

    Receipt webhook delivery is handled explicitly in ``send_receipt_webhook``
    after payment is PAID/CONFIRMED. The generic event notifier must never send
    booking.created/booking.confirmed to the receipt webhook endpoint.
    """
    providers: list[NotificationProvider] = [LogNotificationProvider()]

    return NotificationService(providers)


def mask_email(email: str) -> str:
    value = (email or "").strip()
    if "@" not in value:
        return "***"
    local, domain = value.split("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def _short_error(text: str, *, limit: int = 120) -> str:
    value = (text or "").replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _post_json_no_redirect(url: str, payload: dict[str, Any], *, timeout_seconds: int) -> tuple[int, str, str | None]:
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise
    return int(response.status_code or 0), response.text, response.headers.get("Location")


def _response_declares_ok(response_body: str) -> bool:
    body_text = (response_body or "").strip()
    if not body_text:
        return False
    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError:
        return "ok" in body_text.lower()
    if isinstance(parsed, dict):
        if parsed.get("ok") is True:
            return True
        if parsed.get("success") is True:
            return True
    return "ok" in body_text.lower()


def send_receipt_webhook(booking: dict[str, Any]) -> bool:
    booking_id_raw = booking.get("id")
    booking_id = int(booking_id_raw) if isinstance(booking_id_raw, int) or (isinstance(booking_id_raw, str) and booking_id_raw.isdigit()) else None
    required_fields = ("booking_reference", "trailer_type", "start_dt", "end_dt", "price", "customer_email_temp", "receipt_requested_temp")
    if booking_id is not None and any(booking.get(field) in (None, "") for field in required_fields):
        booking = db.get_booking_by_id(booking_id) or booking

    customer_email = (booking.get("customer_email_temp") or "").strip()
    receipt_requested = bool(booking.get("receipt_requested_temp"))
    if not receipt_requested or not customer_email:
        return False

    webhook_url = runtime.notify_webhook_url()
    if not webhook_url:
        logger.info("WEBHOOK_DISABLED event=booking.confirmed bookingReference=%s", booking.get("booking_reference"))
        return False

    payload = {
        "secret": runtime.webhook_secret(),
        "receiptRequested": True,
        "customerEmail": customer_email,
        "event": "booking.confirmed",
        "companyName": COMPANY_NAME,
        "organizationNumber": ORGANIZATION_NUMBER,
        "bookingId": booking.get("id"),
        "bookingReference": booking.get("booking_reference"),
        "trailerType": booking.get("trailer_type"),
        "startDt": booking.get("start_dt"),
        "endDt": booking.get("end_dt"),
        "price": booking.get("price"),
        "swishStatus": "PAID",
    }
    logger.info(
        "WEBHOOK_SEND event=booking.confirmed bookingReference=%s customerEmail=%s",
        booking.get("booking_reference"),
        mask_email(customer_email),
    )
    max_attempts = 3
    retry_backoff_seconds = (0.5, 1.0)
    for attempt in range(1, max_attempts + 1):
        try:
            status_code, response_body, redirect_location = _post_json_no_redirect(webhook_url, payload, timeout_seconds=10)
        except requests.Timeout as exc:
            if attempt < max_attempts:
                logger.warning(
                    "WEBHOOK_RETRY reason=timeout attempt=%s bookingReference=%s error=%s",
                    attempt,
                    booking.get("booking_reference"),
                    _short_error(str(exc)),
                )
                time.sleep(retry_backoff_seconds[attempt - 1])
                continue
            logger.warning(
                "WEBHOOK_FAIL status=0 bookingReference=%s error=%s",
                booking.get("booking_reference"),
                _short_error(str(exc)),
            )
            return False
        except Exception as exc:
            logger.warning(
                "WEBHOOK_FAIL status=0 bookingReference=%s error=%s",
                booking.get("booking_reference"),
                _short_error(str(exc)),
            )
            return False

        if status_code in {302, 303}:
            logger.info(
                "WEBHOOK_OK_REDIRECT status=%s bookingReference=%s",
                status_code,
                booking.get("booking_reference"),
            )
            return True

        if 200 <= status_code < 300:
            if _response_declares_ok(response_body):
                logger.info("WEBHOOK_OK status=%s bookingReference=%s", status_code, booking.get("booking_reference"))
            else:
                logger.info("WEBHOOK_OK status=%s bookingReference=%s", status_code, booking.get("booking_reference"))
            return True

        if 500 <= status_code < 600 and attempt < max_attempts:
            logger.warning(
                "WEBHOOK_RETRY reason=server_error status=%s attempt=%s bookingReference=%s",
                status_code,
                attempt,
                booking.get("booking_reference"),
            )
            time.sleep(retry_backoff_seconds[attempt - 1])
            continue

        logger.warning(
            "WEBHOOK_FAIL status=%s body=%s bookingReference=%s",
            status_code,
            _short_error(response_body if response_body else (redirect_location or "")),
            booking.get("booking_reference"),
        )
        return False

    return False
