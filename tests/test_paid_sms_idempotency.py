import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import db


class PaidSmsIdempotencyTest(unittest.TestCase):
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

    def _post_json(self, path: str, payload: dict, *, auth: bool = False) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._admin_token}"
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

    def _post(self, path: str, *, auth: bool = False) -> tuple[int, dict]:
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self._admin_token}"
        request = Request(f"{self._base_url}{path}", data=b"", headers=headers, method="POST")
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body)

    def test_paid_sms_sent_once_and_customer_phone_cleared(self) -> None:
        status, payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "date": "2026-07-12",
                "startTime": "10:00",
                "customerPhone": "0701234567",
            },
        )
        self.assertEqual(status, 201)
        booking_id = payload["bookingId"]

        sent_calls: list[tuple[str, str]] = []

        def fake_send_sms(to_e164: str, message: str) -> bool:
            sent_calls.append((to_e164, message))
            return True

        with mock.patch("sms_provider.get_admin_sms_number_e164", return_value="+46709663485"), mock.patch(
            "sms_provider.send_sms", side_effect=fake_send_sms
        ):
            first_status, _ = self._post(
                f"/api/dev/swish/mark?bookingId={booking_id}&status=PAID",
                auth=True,
            )
            second_status, _ = self._post(
                f"/api/dev/swish/mark?bookingId={booking_id}&status=PAID",
                auth=True,
            )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(len(sent_calls), 2)
        self.assertEqual(sent_calls[0][0], "+46709663485")
        self.assertEqual(sent_calls[1][0], "+46701234567")

        booking = db.get_booking_by_id(booking_id)
        self.assertIsNotNone(booking)
        self.assertIsNotNone(booking.get("sms_admin_sent_at"))
        self.assertIsNotNone(booking.get("sms_customer_sent_at"))
        self.assertIsNone(booking.get("customer_phone_temp"))

    def test_customer_sms_retry_on_failure_without_admin_duplicate(self) -> None:
        status, payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "KAP",
                "rentalType": "TWO_HOURS",
                "date": "2026-07-13",
                "startTime": "11:00",
                "customerPhone": "+46705556677",
            },
        )
        self.assertEqual(status, 201)
        booking_id = payload["bookingId"]

        call_counter = {"admin": 0, "customer": 0}

        def flaky_send_sms(to_e164: str, message: str) -> bool:
            if to_e164 == "+46709663485":
                call_counter["admin"] += 1
                return True
            call_counter["customer"] += 1
            return call_counter["customer"] > 1

        with mock.patch("sms_provider.get_admin_sms_number_e164", return_value="+46709663485"), mock.patch(
            "sms_provider.send_sms", side_effect=flaky_send_sms
        ):
            first_status, _ = self._post(
                f"/api/dev/swish/mark?bookingId={booking_id}&status=PAID",
                auth=True,
            )
            second_status, _ = self._post(
                f"/api/dev/swish/mark?bookingId={booking_id}&status=PAID",
                auth=True,
            )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(call_counter["admin"], 1)
        self.assertEqual(call_counter["customer"], 2)

        booking = db.get_booking_by_id(booking_id)
        self.assertIsNotNone(booking)
        self.assertIsNotNone(booking.get("sms_admin_sent_at"))
        self.assertIsNotNone(booking.get("sms_customer_sent_at"))
        self.assertIsNone(booking.get("customer_phone_temp"))

    def test_customer_phone_cleared_when_booking_is_cancelled(self) -> None:
        status, payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "date": "2026-07-14",
                "startTime": "12:00",
                "customerPhone": "0709998877",
            },
        )
        self.assertEqual(status, 201)
        booking_id = payload["bookingId"]

        fail_status, fail_payload = self._post(
            f"/api/dev/swish/mark?bookingId={booking_id}&status=FAILED",
            auth=True,
        )
        self.assertEqual(fail_status, 200)
        self.assertEqual(fail_payload.get("bookingStatus"), "CANCELLED")

        booking = db.get_booking_by_id(booking_id)
        self.assertIsNotNone(booking)
        self.assertEqual(booking.get("status"), "CANCELLED")
        self.assertIsNone(booking.get("customer_phone_temp"))


if __name__ == "__main__":
    unittest.main()
