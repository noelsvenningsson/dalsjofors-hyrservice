import json
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


class AdminBlocksAndPendingExpirationTest(unittest.TestCase):
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
        try:
            with urlopen(url) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body)

    def _delete_json(self, path: str, params: dict | None = None) -> tuple[int, dict]:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, method="DELETE")
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body)

    def test_booking_overlaps_block_is_rejected(self) -> None:
        status, block_payload = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "GALLER",
                "startDatetime": "2026-05-01T10:00",
                "endDatetime": "2026-05-01T12:00",
                "reason": "Service",
            },
        )
        self.assertEqual(status, 201)
        self.assertIn("id", block_payload)

        hold_status, hold_payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "date": "2026-05-01",
                "startTime": "10:00",
            },
        )
        self.assertEqual(hold_status, 409)
        self.assertEqual(hold_payload.get("error"), "slot blocked")
        self.assertEqual(hold_payload.get("message"), "Requested slot overlaps an admin block")
        self.assertEqual(hold_payload.get("block", {}).get("id"), block_payload["id"])

    def test_non_overlapping_booking_is_allowed(self) -> None:
        status, _ = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "GALLER",
                "startDatetime": "2026-05-02T10:00",
                "endDatetime": "2026-05-02T12:00",
                "reason": "Inspection",
            },
        )
        self.assertEqual(status, 201)

        hold_status, hold_payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "date": "2026-05-02",
                "startTime": "12:00",
            },
        )
        self.assertEqual(hold_status, 201)
        self.assertIn("bookingId", hold_payload)
        self.assertIn("price", hold_payload)

    def test_pending_payment_expires_and_no_longer_blocks_slot(self) -> None:
        start = datetime(2026, 5, 3, 10, 0)
        end = start + timedelta(hours=2)
        booking_id, _ = db.create_booking("KAP", "TWO_HOURS", start, end)

        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute(
                """
                UPDATE bookings
                SET status = 'PENDING_PAYMENT',
                    expires_at = ?
                WHERE id = ?
                """,
                ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), booking_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Expired pending rows should not block availability even before cleanup.
        self.assertTrue(db.check_availability("KAP", start, end))

        status, payload = self._post_json("/api/admin/expire-pending", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload.get("expiredCount", 0), 0)

        booking = db.get_booking_by_id(booking_id)
        self.assertIsNotNone(booking)
        self.assertEqual(booking["status"], "CANCELLED")

    def test_admin_block_crud_basic(self) -> None:
        create_status, create_payload = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "KAP",
                "startDatetime": "2026-05-04T08:00",
                "endDatetime": "2026-05-04T09:00",
                "reason": "Maintenance",
            },
        )
        self.assertEqual(create_status, 201)
        block_id = create_payload["id"]

        list_status, list_payload = self._get_json(
            "/api/admin/blocks",
            {"startDatetime": "2026-05-04T00:00", "endDatetime": "2026-05-04T23:59"},
        )
        self.assertEqual(list_status, 200)
        matching = [row for row in list_payload.get("blocks", []) if row.get("id") == block_id]
        self.assertEqual(len(matching), 1)

        delete_status, delete_payload = self._delete_json("/api/admin/blocks", {"id": block_id})
        self.assertEqual(delete_status, 200)
        self.assertTrue(delete_payload.get("deleted"))

        list_after_status, list_after_payload = self._get_json(
            "/api/admin/blocks",
            {"startDatetime": "2026-05-04T00:00", "endDatetime": "2026-05-04T23:59"},
        )
        self.assertEqual(list_after_status, 200)
        matching_after = [
            row for row in list_after_payload.get("blocks", []) if row.get("id") == block_id
        ]
        self.assertEqual(len(matching_after), 0)


if __name__ == "__main__":
    unittest.main()
