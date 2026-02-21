from __future__ import annotations

import http.client
import json
import os
import re
import threading
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import app
import db
import sms_provider


def _post_json(port: int, path: str, payload: dict, headers: dict[str, str] | None = None) -> tuple[int, dict, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    conn.request("POST", path, body=json.dumps(payload).encode("utf-8"), headers=request_headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    status = resp.status
    out_headers = {k: v for (k, v) in resp.getheaders()}
    conn.close()
    return status, (json.loads(body) if body else {}), out_headers


def _get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    status = resp.status
    out_headers = {k: v for (k, v) in resp.getheaders()}
    conn.close()
    return status, (json.loads(body) if body else {}), out_headers


def test_response_includes_request_id_header_echo(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        original_db_path = db.DB_PATH
        try:
            db.DB_PATH = Path(tmpdir) / "test_database.db"
            db.init_db()
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, payload, headers = _get(
                    server.server_port,
                    "/api/health",
                    headers={"X-Request-Id": "req-abc-123"},
                )
                assert status == 200
                assert payload.get("ok") is True
                assert headers.get("X-Request-Id") == "req-abc-123"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            db.DB_PATH = original_db_path


def test_swish_callback_paid_is_idempotent_for_sms(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        original_db_path = db.DB_PATH
        original_swish_mode = os.environ.get("SWISH_MODE")
        try:
            db.DB_PATH = Path(tmpdir) / "test_database.db"
            db.init_db()
            monkeypatch.setenv("SWISH_MODE", "mock")

            sms_calls: list[tuple[str, str]] = []

            def _fake_send_sms(to: str, message: str) -> bool:
                sms_calls.append((to, message))
                return True

            monkeypatch.setattr(sms_provider, "get_admin_sms_number_e164", lambda: "+46709999999")
            monkeypatch.setattr(sms_provider, "send_sms", _fake_send_sms)

            start_dt = (datetime.now() + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
            end_dt = start_dt + timedelta(hours=2)
            booking_id, _ = db.create_booking(
                "GALLER",
                "TWO_HOURS",
                start_dt,
                end_dt,
                customer_phone_temp="+46701234567",
                customer_email_temp=None,
                receipt_requested_temp=False,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status1, payload1, _ = _post_json(
                    server.server_port,
                    "/api/swish/callback",
                    {"paymentReference": booking_id, "status": "PAID"},
                )
                assert status1 == 200
                assert payload1.get("swishStatus") == "PAID"

                status2, payload2, _ = _post_json(
                    server.server_port,
                    "/api/swish/callback",
                    {"paymentReference": booking_id, "status": "PAID"},
                )
                assert status2 == 200
                assert payload2.get("swishStatus") == "PAID"

                booking = db.get_booking_by_id(booking_id)
                assert booking
                assert booking.get("status") == "CONFIRMED"
                assert booking.get("swish_status") == "PAID"
                assert len(sms_calls) == 2
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


def test_no_obvious_secret_logging_patterns() -> None:
    paths = [Path("app.py"), Path("notifications.py"), Path("db.py"), Path("swish_client.py")]
    risky = re.compile(r"logger\.(?:info|warning|error|exception)\([^\n]*(ADMIN_TOKEN|WEBHOOK_SECRET|NOTIFY_WEBHOOK_SECRET)")
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert risky.search(text) is None, f"Possible secret logging pattern in {path}"
