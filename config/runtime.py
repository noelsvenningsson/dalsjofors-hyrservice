from __future__ import annotations

import os
from pathlib import Path


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return default


def swish_mode() -> str:
    return env_first("SWISH_MODE", default="mock").lower()


def swish_api_url() -> str:
    return env_first("SWISH_API_URL", "SWISH_COMMERCE_BASE_URL", default="mock")


def swish_merchant_alias() -> str:
    return env_first("SWISH_MERCHANT_ALIAS", "SWISH_COMMERCE_MERCHANT_ALIAS", default="1234945580")


def swish_callback_url() -> str:
    return env_first("SWISH_CALLBACK_URL", "SWISH_COMMERCE_CALLBACK_URL")


def swish_cert_path() -> str:
    return env_first("SWISH_CERT_PATH", "SWISH_COMMERCE_CERT_PATH")


def swish_key_path() -> str:
    return env_first("SWISH_KEY_PATH", "SWISH_COMMERCE_KEY_PATH")


def swish_ca_path() -> str:
    return env_first("SWISH_CA_PATH")


def notify_webhook_url() -> str:
    return env_first("NOTIFY_WEBHOOK_URL")


def report_webhook_url() -> str:
    return env_first("REPORT_WEBHOOK_URL")


def report_to() -> str:
    return env_first("REPORT_TO", default="svenningsson@outlook.com")


def webhook_secret() -> str:
    return env_first("WEBHOOK_SECRET", "NOTIFY_WEBHOOK_SECRET")


def admin_token() -> str:
    return env_first("ADMIN_TOKEN")


def admin_password() -> str:
    return env_first("ADMIN_PASSWORD")


def admin_session_secret() -> str:
    return env_first("ADMIN_SESSION_SECRET")


def port() -> int:
    raw = env_first("PORT", default="8000")
    try:
        return int(raw)
    except ValueError:
        return 8000


def db_path(default_path: Path) -> Path:
    configured = env_first("DATABASE_PATH")
    if not configured:
        return default_path
    return Path(configured).expanduser()
