import http.client
import json
import os
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import app
import db


class DevSwishMarkAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = TemporaryDirectory()
        cls._original_db_path = db.DB_PATH
        cls._original_admin_token = os.environ.get("ADMIN_TOKEN")
        cls._original_swish_mode = os.environ.get("SWISH_MODE")
        cls._original_app_env = os.environ.get("APP_ENV")

        cls._admin_token = "test-admin-token"
        os.environ["ADMIN_TOKEN"] = cls._admin_token
        os.environ["SWISH_MODE"] = "mock"
        os.environ["APP_ENV"] = "production"

        db.DB_PATH = Path(cls._tmpdir.name) / "test_database.db"
        db.init_db()

        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls._host = "127.0.0.1"
        cls._port = cls._server.server_port

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()
        cls._thread.join(timeout=2)
        if cls._original_admin_token is None:
            os.environ.pop("ADMIN_TOKEN", None)
        else:
            os.environ["ADMIN_TOKEN"] = cls._original_admin_token
        if cls._original_swish_mode is None:
            os.environ.pop("SWISH_MODE", None)
        else:
            os.environ["SWISH_MODE"] = cls._original_swish_mode
        if cls._original_app_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = cls._original_app_env
        db.DB_PATH = cls._original_db_path
        cls._tmpdir.cleanup()

    def _request(self, method: str, path: str, headers: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=5)
        conn.request(method, path, body=b"", headers=dict(headers or {}))
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        status = response.status
        conn.close()
        payload = json.loads(body) if body else {}
        return status, payload

    def _create_pending_booking(self) -> int:
        start_dt = (datetime.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(hours=2)
        booking_id, _ = db.create_booking("GALLER", "TWO_HOURS", start_dt, end_dt)
        return booking_id

    def test_dev_mark_without_token_fails_in_production_like_config(self) -> None:
        booking_id = self._create_pending_booking()
        status, payload = self._request(
            "POST", f"/api/dev/swish/mark?bookingId={booking_id}&status=PAID"
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload.get("errorInfo", {}).get("code"), "unauthorized")

        booking = db.get_booking_by_id(booking_id)
        self.assertIsNotNone(booking)
        self.assertEqual(booking.get("status"), "PENDING_PAYMENT")
        self.assertIsNone(booking.get("swish_status"))

    def test_dev_mark_with_token_succeeds(self) -> None:
        booking_id = self._create_pending_booking()
        status, payload = self._request(
            "POST",
            f"/api/dev/swish/mark?bookingId={booking_id}&status=PAID",
            headers={"Authorization": f"Bearer {self._admin_token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("bookingId"), booking_id)
        self.assertEqual(payload.get("swishStatus"), "PAID")
        self.assertEqual(payload.get("bookingStatus"), "CONFIRMED")

        booking = db.get_booking_by_id(booking_id)
        self.assertIsNotNone(booking)
        self.assertEqual(booking.get("status"), "CONFIRMED")
        self.assertEqual(booking.get("swish_status"), "PAID")


if __name__ == "__main__":
    unittest.main()
