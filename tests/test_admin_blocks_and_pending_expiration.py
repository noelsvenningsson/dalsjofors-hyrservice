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


class AdminBlocksAndPendingExpirationTest(unittest.TestCase):
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

    def _post_json(self, path: str, payload: dict, admin_token: str | None = "use-default") -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if path.startswith("/api/admin/"):
            if admin_token == "use-default":
                headers["X-Admin-Token"] = self._admin_token
            elif admin_token:
                headers["X-Admin-Token"] = admin_token
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body)

    def _get_json(self, path: str, params: dict | None = None, admin_token: str | None = "use-default") -> tuple[int, dict]:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {}
        if path.startswith("/api/admin/"):
            if admin_token == "use-default":
                headers["X-Admin-Token"] = self._admin_token
            elif admin_token:
                headers["X-Admin-Token"] = admin_token
        request = Request(url, headers=headers)
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body)

    def _delete_json(self, path: str, params: dict | None = None, admin_token: str | None = "use-default") -> tuple[int, dict]:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {}
        if path.startswith("/api/admin/"):
            if admin_token == "use-default":
                headers["X-Admin-Token"] = self._admin_token
            elif admin_token:
                headers["X-Admin-Token"] = admin_token
        request = Request(url, headers=headers, method="DELETE")
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

    def test_slot_allows_two_bookings_but_rejects_third(self) -> None:
        payload = {
            "trailerType": "GALLER",
            "rentalType": "TWO_HOURS",
            "date": "2026-05-09",
            "startTime": "10:00",
        }

        first_status, first_payload = self._post_json("/api/hold", payload)
        self.assertEqual(first_status, 201)
        self.assertIn("bookingId", first_payload)

        second_status, second_payload = self._post_json("/api/hold", payload)
        self.assertEqual(second_status, 201)
        self.assertIn("bookingId", second_payload)

        third_status, third_payload = self._post_json("/api/hold", payload)
        self.assertEqual(third_status, 409)
        self.assertEqual(third_payload.get("error"), "slot taken")

    def test_availability_remaining_reflects_capacity_two(self) -> None:
        params = {
            "trailerType": "KAP",
            "rentalType": "TWO_HOURS",
            "date": "2026-05-10",
            "startTime": "10:00",
        }

        status_before, payload_before = self._get_json("/api/availability", params)
        self.assertEqual(status_before, 200)
        self.assertEqual(payload_before.get("remaining"), 2)
        self.assertTrue(payload_before.get("available"))

        hold_payload = {
            "trailerType": "KAP",
            "rentalType": "TWO_HOURS",
            "date": "2026-05-10",
            "startTime": "10:00",
        }
        first_status, _ = self._post_json("/api/hold", hold_payload)
        self.assertEqual(first_status, 201)

        status_after_first, payload_after_first = self._get_json("/api/availability", params)
        self.assertEqual(status_after_first, 200)
        self.assertEqual(payload_after_first.get("remaining"), 1)
        self.assertTrue(payload_after_first.get("available"))

        second_status, _ = self._post_json("/api/hold", hold_payload)
        self.assertEqual(second_status, 201)

        status_after_second, payload_after_second = self._get_json("/api/availability", params)
        self.assertEqual(status_after_second, 200)
        self.assertEqual(payload_after_second.get("remaining"), 0)
        self.assertFalse(payload_after_second.get("available"))

    def test_pending_payment_expires_and_no_longer_blocks_slot(self) -> None:
        start = (datetime.now() - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=2)
        booking_id, _ = db.create_booking("KAP", "TWO_HOURS", start, end)
        second_booking_id, _ = db.create_booking("KAP", "TWO_HOURS", start, end)

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
            conn.execute(
                """
                UPDATE bookings
                SET status = 'CONFIRMED'
                WHERE id = ?
                """,
                (second_booking_id,),
            )
            conn.commit()
        finally:
            conn.close()

        # Expired pending rows should not consume one of the two slots.
        self.assertTrue(db.check_availability("KAP", start, end))

        status, payload = self._post_json("/api/admin/expire-pending", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload.get("expiredCount", 0), 0)

        booking = db.get_booking_by_id(booking_id)
        self.assertIsNotNone(booking)
        self.assertEqual(booking["status"], "CANCELLED")

    def test_admin_block_endpoints_require_token(self) -> None:
        status, payload = self._get_json("/api/admin/blocks", admin_token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload.get("errorInfo", {}).get("code"), "unauthorized")

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

    def test_admin_block_create_accepts_startdatetime_enddatetime(self) -> None:
        status, payload = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "KAP",
                "startDatetime": "2026-05-05T08:00",
                "endDatetime": "2026-05-05T09:00",
                "reason": "Docs canonical fields",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload.get("startDatetime"), "2026-05-05T08:00")
        self.assertEqual(payload.get("endDatetime"), "2026-05-05T09:00")

    def test_admin_block_create_accepts_start_end_aliases(self) -> None:
        status, payload = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "KAP",
                "start": "2026-05-06T08:00",
                "end": "2026-05-06T09:00",
                "reason": "Legacy field aliases",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload.get("startDatetime"), "2026-05-06T08:00")
        self.assertEqual(payload.get("endDatetime"), "2026-05-06T09:00")

    def test_admin_block_create_prefers_canonical_datetime_fields(self) -> None:
        status, payload = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "KAP",
                "startDatetime": "2026-05-07T08:00",
                "endDatetime": "2026-05-07T09:00",
                "start": "2026-05-07T11:00",
                "end": "2026-05-07T12:00",
                "reason": "Canonical should win",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload.get("startDatetime"), "2026-05-07T08:00")
        self.assertEqual(payload.get("endDatetime"), "2026-05-07T09:00")

    def test_admin_block_create_missing_or_invalid_datetime_returns_400(self) -> None:
        missing_status, missing_payload = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "KAP",
                "reason": "Missing datetime range",
            },
        )
        self.assertEqual(missing_status, 400)
        self.assertIn("errorInfo", missing_payload)
        self.assertEqual(missing_payload.get("errorInfo", {}).get("code"), "invalid_request")
        self.assertIn("startDatetime", missing_payload.get("errorInfo", {}).get("details", {}).get("fields", {}))
        self.assertIn("endDatetime", missing_payload.get("errorInfo", {}).get("details", {}).get("fields", {}))

        invalid_status, invalid_payload = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "KAP",
                "start": "not-a-datetime",
                "end": "2026-05-08T09:00",
            },
        )
        self.assertEqual(invalid_status, 400)
        self.assertIn("errorInfo", invalid_payload)
        self.assertEqual(invalid_payload.get("errorInfo", {}).get("code"), "invalid_request")
        self.assertEqual(invalid_payload.get("errorInfo", {}).get("details", {}).get("fields", {}).get("startDatetime"), "Expected ISO 8601 datetime (e.g. YYYY-MM-DDTHH:MM)")


if __name__ == "__main__":
    unittest.main()
