import json
import os
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import db


class EphemeralTestBookingsProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "test_database.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def test_process_due_test_bookings_immediate_paid_sms_and_delete_idempotent(self) -> None:
        now = datetime(2026, 2, 19, 12, 0, 0)
        row = db.create_test_booking(
            trailer_type="GALLER",
            rental_type="HELDAG",
            price=250,
            sms_target_temp="+46701234567",
            now=now,
        )
        test_booking_id = int(row["id"])
        self.assertEqual(row.get("status"), "PAID")

        with mock.patch("sms_provider.get_admin_sms_number_e164", return_value="+46709663485"):
            with mock.patch("sms_provider.send_sms", return_value=True) as send_sms_mock:
                first = app.process_due_test_bookings(now=now)
                self.assertEqual(first, {"processedPaid": 0, "deleted": 0})
                after_first = db.get_test_booking_by_id(test_booking_id)
                self.assertIsNotNone(after_first)
                self.assertEqual(after_first.get("status"), "PAID")
                self.assertEqual(send_sms_mock.call_count, 2)

                second = app.process_due_test_bookings(now=now)
                self.assertEqual(second, {"processedPaid": 0, "deleted": 0})
                self.assertEqual(send_sms_mock.call_count, 2)

                delete_now = now + timedelta(minutes=6)
                third = app.process_due_test_bookings(now=delete_now)
                self.assertEqual(third, {"processedPaid": 0, "deleted": 1})
                self.assertIsNone(db.get_test_booking_by_id(test_booking_id))

                fourth = app.process_due_test_bookings(now=delete_now)
                self.assertEqual(fourth, {"processedPaid": 0, "deleted": 0})
                self.assertEqual(send_sms_mock.call_count, 2)


class EphemeralTestBookingsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = TemporaryDirectory()
        cls._original_db_path = db.DB_PATH
        cls._original_admin_token = os.environ.get("ADMIN_TOKEN")
        cls._admin_token = "test-admin-token"

        os.environ["ADMIN_TOKEN"] = cls._admin_token
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
        if cls._original_admin_token is None:
            os.environ.pop("ADMIN_TOKEN", None)
        else:
            os.environ["ADMIN_TOKEN"] = cls._original_admin_token
        db.DB_PATH = cls._original_db_path
        cls._tmpdir.cleanup()

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        admin_token: str | None = "use-default",
    ) -> tuple[int, dict]:
        headers = {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if admin_token == "use-default":
            headers["X-Admin-Token"] = self._admin_token
        elif admin_token:
            headers["X-Admin-Token"] = admin_token

        req = Request(
            f"{self._base_url}{path}",
            data=(json.dumps(payload).encode("utf-8") if payload is not None else None),
            headers=headers,
            method=method,
        )
        try:
            with urlopen(req) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_admin_test_bookings_requires_token(self) -> None:
        status, payload = self._request_json("GET", "/api/admin/test-bookings", admin_token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload.get("errorInfo", {}).get("code"), "unauthorized")

    def test_create_list_and_run_test_bookings_with_x_admin_token(self) -> None:
        status, created = self._request_json(
            "POST",
            "/api/admin/test-bookings",
            {
                "smsTo": "0701234567",
                "trailerType": "KAPS",
                "date": "2026-02-19",
                "rentalType": "HELDAG",
            },
        )
        self.assertEqual(status, 201)
        self.assertIn("id", created)
        self.assertIn("bookingReference", created)
        self.assertEqual(created.get("status"), "PAID")

        list_status, listed = self._request_json("GET", "/api/admin/test-bookings")
        self.assertEqual(list_status, 200)
        rows = listed.get("testBookings", [])
        self.assertTrue(any(int(row.get("id", 0)) == int(created["id"]) for row in rows))

        run_status, run_payload = self._request_json("POST", "/api/admin/test-bookings/run", {})
        self.assertEqual(run_status, 200)
        self.assertIn("processedPaid", run_payload)
        self.assertIn("deleted", run_payload)

        bookings_status, bookings_payload = self._request_json("GET", "/api/admin/bookings")
        self.assertEqual(bookings_status, 200)
        references = [row.get("bookingReference") or "" for row in bookings_payload.get("bookings", [])]
        self.assertTrue(all(not ref.startswith("TEST-") for ref in references))


if __name__ == "__main__":
    unittest.main()
