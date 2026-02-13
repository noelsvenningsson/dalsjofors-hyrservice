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
import os
import urllib.request
from typing import Any, Protocol

logger = logging.getLogger(__name__)


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
    """Create notification service with default log provider plus optional webhook."""
    providers: list[NotificationProvider] = [LogNotificationProvider()]

    webhook_url = (os.environ.get("NOTIFY_WEBHOOK_URL") or "").strip()
    if webhook_url:
        webhook_secret = (os.environ.get("NOTIFY_WEBHOOK_SECRET") or "").strip() or None
        providers.append(WebhookNotificationProvider(webhook_url, webhook_secret))

    return NotificationService(providers)
