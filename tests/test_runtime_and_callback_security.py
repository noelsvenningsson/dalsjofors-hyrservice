from __future__ import annotations

import http.client
import json
import os
import threading
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import app
import db
from config import runtime


def test_runtime_aliases(monkeypatch):
    monkeypatch.setenv("SWISH_API_URL", "https://api.swish.nu")
    monkeypatch.setenv("SWISH_CERT_PATH", "/tmp/cert.pem")
    monkeypatch.setenv("SWISH_KEY_PATH", "/tmp/key.pem")
    monkeypatch.setenv("WEBHOOK_SECRET", "secret-a")

    assert runtime.swish_api_url() == "https://api.swish.nu"
    assert runtime.swish_cert_path() == "/tmp/cert.pem"
    assert runtime.swish_key_path() == "/tmp/key.pem"
    assert runtime.webhook_secret() == "secret-a"


def test_runtime_legacy_aliases(monkeypatch):
    monkeypatch.delenv("SWISH_API_URL", raising=False)
    monkeypatch.delenv("SWISH_CERT_PATH", raising=False)
    monkeypatch.delenv("SWISH_KEY_PATH", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("SWISH_COMMERCE_BASE_URL", "https://legacy.example")
    monkeypatch.setenv("SWISH_COMMERCE_CERT_PATH", "/legacy/cert.pem")
    monkeypatch.setenv("SWISH_COMMERCE_KEY_PATH", "/legacy/key.pem")
    monkeypatch.setenv("NOTIFY_WEBHOOK_SECRET", "legacy-secret")

    assert runtime.swish_api_url() == "https://legacy.example"
    assert runtime.swish_cert_path() == "/legacy/cert.pem"
    assert runtime.swish_key_path() == "/legacy/key.pem"
    assert runtime.webhook_secret() == "legacy-secret"


def test_callback_requires_secret_in_non_mock_mode(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        original_db_path = db.DB_PATH
        original_swish_mode = os.environ.get("SWISH_MODE")
        original_webhook_secret = os.environ.get("WEBHOOK_SECRET")

        try:
            db.DB_PATH = Path(tmpdir) / "test_database.db"
            db.init_db()

            monkeypatch.setenv("SWISH_MODE", "production")
            monkeypatch.setenv("WEBHOOK_SECRET", "callback-secret")

            start_dt = (datetime.now() + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
            end_dt = start_dt + timedelta(hours=2)
            booking_id, _ = db.create_booking("GALLER", "TWO_HOURS", start_dt, end_dt)

            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request(
                    "POST",
                    "/api/swish/callback",
                    body=json.dumps({"paymentReference": booking_id, "status": "PAID"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                payload = json.loads(resp.read().decode("utf-8"))
                conn.close()

                assert resp.status == 401
                assert payload.get("errorInfo", {}).get("code") == "unauthorized"

                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request(
                    "POST",
                    "/api/swish/callback",
                    body=json.dumps({"paymentReference": booking_id, "status": "PAID"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Secret": "callback-secret",
                    },
                )
                resp2 = conn.getresponse()
                payload2 = json.loads(resp2.read().decode("utf-8"))
                conn.close()

                assert resp2.status == 200
                assert payload2.get("swishStatus") == "PAID"

                booking = db.get_booking_by_id(booking_id)
                assert booking
                assert booking.get("status") == "CONFIRMED"
                assert booking.get("swish_status") == "PAID"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            db.DB_PATH = original_db_path
            if original_swish_mode is None:
                os.environ.pop("SWISH_MODE", None)
            else:
                os.environ["SWISH_MODE"] = original_swish_mode
            if original_webhook_secret is None:
                os.environ.pop("WEBHOOK_SECRET", None)
            else:
                os.environ["WEBHOOK_SECRET"] = original_webhook_secret
