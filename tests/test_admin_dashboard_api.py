import json
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import app
import db


class AdminDashboardApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = TemporaryDirectory()
        cls._original_db_path = db.DB_PATH
        db.DB_PATH = Path(cls._tmpdir.name) / "test_database.db"
        db.init_db()

        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls._base_url = f"http://127.0.0.1:{cls._server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()
        cls._thread.join(timeout=2)
        db.DB_PATH = cls._original_db_path
        cls._tmpdir.cleanup()

    def _get_json(self, path: str, params: dict | None = None) -> tuple[int, dict]:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            with urlopen(url) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body)

    def test_admin_page_serves_html(self) -> None:
        with urlopen(f"{self._base_url}/admin") as response:
            self.assertEqual(response.status, 200)
            html = response.read().decode("utf-8")
        self.assertIn("Admin Dashboard", html)
        self.assertIn("/static/admin.js", html)

    def test_admin_bookings_returns_rows_and_status_filter(self) -> None:
        pending_start = datetime(2026, 6, 1, 10, 0)
        confirmed_start = datetime(2026, 6, 1, 13, 0)

        pending_id, _ = db.create_booking(
            "GALLER",
            "TWO_HOURS",
            pending_start,
            pending_start.replace(hour=12),
        )
        confirmed_id, _ = db.create_booking(
            "KAP",
            "TWO_HOURS",
            confirmed_start,
            confirmed_start.replace(hour=15),
        )
        db.mark_confirmed(confirmed_id)

        status, payload = self._get_json("/api/admin/bookings")
        self.assertEqual(status, 200)
        self.assertIn("bookings", payload)

        rows = payload["bookings"]
        self.assertTrue(any(row.get("bookingId") == pending_id for row in rows))
        self.assertTrue(any(row.get("bookingId") == confirmed_id for row in rows))

        sample = next(row for row in rows if row.get("bookingId") == pending_id)
        self.assertIn("bookingReference", sample)
        self.assertIn("trailerType", sample)
        self.assertIn("startDt", sample)
        self.assertIn("status", sample)
        self.assertIn("price", sample)

        confirmed_status, confirmed_payload = self._get_json(
            "/api/admin/bookings", {"status": "CONFIRMED"}
        )
        self.assertEqual(confirmed_status, 200)
        confirmed_rows = confirmed_payload.get("bookings", [])
        self.assertGreaterEqual(len(confirmed_rows), 1)
        self.assertTrue(all(row.get("status") == "CONFIRMED" for row in confirmed_rows))


if __name__ == "__main__":
    unittest.main()
