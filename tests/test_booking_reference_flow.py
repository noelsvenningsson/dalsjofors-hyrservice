import json
import os
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import app
import db


class BookingReferenceFlowTest(unittest.TestCase):
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

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body)

    def _get_json(self, path: str, params: dict | None = None) -> tuple[int, dict]:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {}
        if path.startswith("/api/admin/"):
            headers["X-Admin-Token"] = self._admin_token
        request = Request(url, headers=headers)
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body)

    def _create_hold(self, date_str: str, start_time: str = "10:00") -> dict:
        status, payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "date": date_str,
                "startTime": start_time,
            },
        )
        self.assertEqual(status, 201)
        self.assertIn("bookingId", payload)
        self.assertIn("bookingReference", payload)
        return payload

    def test_reference_created_on_new_booking(self) -> None:
        payload = self._create_hold("2026-04-10")
        booking_id = payload["bookingId"]
        booking_reference = payload["bookingReference"]
        self.assertIsNotNone(booking_reference)
        self.assertRegex(booking_reference, r"^DHS-\d{8}-\d{6}$")

        booking = db.get_booking_by_id(booking_id)
        self.assertIsNotNone(booking)
        self.assertEqual(booking.get("booking_reference"), booking_reference)

    def test_reference_uniqueness(self) -> None:
        first = self._create_hold("2026-04-11", "10:00")
        second = self._create_hold("2026-04-11", "13:00")
        self.assertNotEqual(first["bookingReference"], second["bookingReference"])

        conn = sqlite3.connect(db.DB_PATH)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE bookings SET booking_reference = ? WHERE id = ?",
                    (first["bookingReference"], second["bookingId"]),
                )
                conn.commit()
        finally:
            conn.close()

    def test_reference_returned_in_api_responses(self) -> None:
        hold = self._create_hold("2026-04-12")
        booking_id = hold["bookingId"]
        booking_reference = hold["bookingReference"]

        payment_status, payment = self._get_json("/api/payment", {"bookingId": booking_id})
        self.assertEqual(payment_status, 200)
        self.assertEqual(payment.get("bookingReference"), booking_reference)
        self.assertEqual(payment.get("swishMessage"), booking_reference)

        admin_status, admin = self._get_json("/api/admin/bookings")
        self.assertEqual(admin_status, 200)
        matching = [row for row in admin.get("bookings", []) if row.get("bookingId") == booking_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].get("bookingReference"), booking_reference)

    def test_old_booking_without_reference_does_not_crash_endpoints(self) -> None:
        start = datetime(2026, 4, 13, 10, 0)
        end = start + timedelta(hours=2)
        conn = sqlite3.connect(db.DB_PATH)
        try:
            cur = conn.execute(
                """
                INSERT INTO bookings (
                    booking_reference,
                    trailer_type,
                    rental_type,
                    start_dt,
                    end_dt,
                    price,
                    status,
                    created_at,
                    swish_id,
                    expires_at
                )
                VALUES (NULL, 'GALLER', 'TWO_HOURS', ?, ?, 200, 'PENDING_PAYMENT', ?, NULL, ?)
                """,
                (
                    start.isoformat(timespec="minutes"),
                    end.isoformat(timespec="minutes"),
                    datetime.now().isoformat(timespec="seconds"),
                    (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds"),
                ),
            )
            old_booking_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        payment_status, payment = self._get_json("/api/payment", {"bookingId": old_booking_id})
        self.assertEqual(payment_status, 200)
        self.assertIsNone(payment.get("bookingReference"))
        self.assertIn("payload", payment)
        self.assertIn("DHS-", payment.get("swishMessage", ""))

        admin_status, admin = self._get_json("/api/admin/bookings")
        self.assertEqual(admin_status, 200)
        matching = [row for row in admin.get("bookings", []) if row.get("bookingId") == old_booking_id]
        self.assertEqual(len(matching), 1)
        self.assertIsNone(matching[0].get("bookingReference"))

        with urlopen(f"{self._base_url}/confirm?bookingId={old_booking_id}") as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
        self.assertIn("Bokningsreferens: saknas", html)


if __name__ == "__main__":
    unittest.main()
