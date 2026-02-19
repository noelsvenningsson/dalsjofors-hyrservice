"""SMS provider integration (Twilio by default)."""

from __future__ import annotations

import base64
import logging
import os
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


def normalize_swedish_mobile(raw_value: Optional[str]) -> Optional[str]:
    """Normalize Swedish mobile to E.164 format (+467XXXXXXXX)."""
    value = (raw_value or "").strip()
    if not value:
        return None
    compact = "".join(ch for ch in value if ch.isdigit() or ch == "+")
    if compact.startswith("0046"):
        compact = f"+46{compact[4:]}"
    if compact.startswith("+46"):
        national = compact[3:]
        if national.startswith("0"):
            national = national[1:]
        if len(national) != 9 or not national.startswith("7") or not national.isdigit():
            return None
        return f"+46{national}"
    if compact.startswith("07") and len(compact) == 10 and compact.isdigit():
        return f"+46{compact[1:]}"
    return None


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def send_sms(to_e164: str, message: str) -> bool:
    """Send SMS using Twilio env config. Returns False on any failure."""
    account_sid = _env("TWILIO_ACCOUNT_SID")
    auth_token = _env("TWILIO_AUTH_TOKEN")
    from_number = _env("TWILIO_FROM_NUMBER")
    if not account_sid or not auth_token or not from_number:
        logger.warning(
            "SMS disabled: missing Twilio env vars (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER)."
        )
        return False

    target = normalize_swedish_mobile(to_e164) if not to_e164.startswith("+") else to_e164
    if not target:
        logger.warning("SMS not sent: invalid target phone number.")
        return False

    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = urllib.parse.urlencode(
        {
            "To": target,
            "From": from_number,
            "Body": message[:1600],
        }
    ).encode("utf-8")
    basic_auth = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if 200 <= response.status < 300:
                return True
            body = response.read().decode("utf-8", errors="replace")
            logger.error("Twilio SMS failed status=%s body=%s", response.status, body)
            return False
    except Exception as exc:
        logger.exception("Twilio SMS exception: %s", exc)
        return False


def get_admin_sms_number_e164() -> Optional[str]:
    """Resolve admin SMS number from env/default and normalize to E.164."""
    raw_number = _env("ADMIN_SMS_NUMBER") or "0709663485"
    normalized = normalize_swedish_mobile(raw_number)
    if not normalized:
        logger.error("ADMIN_SMS_NUMBER is invalid: %s", raw_number)
        return None
    return normalized
