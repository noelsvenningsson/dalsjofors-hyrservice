import json
import os
import threading
import unittest
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import app
import db
import notifications


class _RecordingNotifier:
    def __init__(self, *, raise_on_created: bool = False) -> None:
        self.raise_on_created = raise_on_created
        self.created_calls: list[dict] = []
        self.confirmed_calls: list[dict] = []

    def notify_booking_created(self, booking: dict) -> None:
        if self.raise_on_created:
            raise RuntimeError("boom")
        self.created_calls.append(booking)

    def notify_booking_confirmed(self, booking: dict) -> None:
        self.confirmed_calls.append(booking)


class NotificationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = TemporaryDirectory()
        cls._original_db_path = db.DB_PATH
        db.DB_PATH = Path(cls._tmpdir.name) / "test_database.db"
        db.init_db()

        cls._original_notifier = app.NOTIFIER
        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls._base_url = f"http://127.0.0.1:{cls._server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        app.NOTIFIER = cls._original_notifier
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

    def test_build_booking_payload(self) -> None:
        booking = {
            "booking_reference": "DHS-20260213-000001",
            "trailer_type": "GALLER",
            "rental_type": "TWO_HOURS",
            "start_dt": "2026-06-01T10:00",
            "end_dt": "2026-06-01T12:00",
            "status": "PENDING_PAYMENT",
            "price": 200,
        }

        payload = notifications.build_booking_payload(booking)

        self.assertEqual(
            payload,
            {
                "bookingReference": "DHS-20260213-000001",
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "startDatetime": "2026-06-01T10:00",
                "endDatetime": "2026-06-01T12:00",
                "status": "PENDING_PAYMENT",
                "price": 200,
            },
        )

    def test_create_notification_service_does_not_enable_generic_webhook_provider(self) -> None:
        old_url = os.environ.get("NOTIFY_WEBHOOK_URL")
        old_secret = os.environ.get("NOTIFY_WEBHOOK_SECRET")
        os.environ["NOTIFY_WEBHOOK_URL"] = "https://example.com/webhook"
        os.environ["NOTIFY_WEBHOOK_SECRET"] = "secret"
        try:
            service = notifications.create_notification_service_from_env()
            provider_types = {provider.__class__.__name__ for provider in service.providers}
            self.assertIn("LogNotificationProvider", provider_types)
            self.assertNotIn("WebhookNotificationProvider", provider_types)
        finally:
            if old_url is None:
                os.environ.pop("NOTIFY_WEBHOOK_URL", None)
            else:
                os.environ["NOTIFY_WEBHOOK_URL"] = old_url
            if old_secret is None:
                os.environ.pop("NOTIFY_WEBHOOK_SECRET", None)
            else:
                os.environ["NOTIFY_WEBHOOK_SECRET"] = old_secret

    def test_booking_creation_succeeds_when_notifier_raises(self) -> None:
        app.NOTIFIER = _RecordingNotifier(raise_on_created=True)

        status, payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "date": "2026-06-02",
                "startTime": "10:00",
            },
        )

        self.assertEqual(status, 201)
        self.assertIn("bookingId", payload)
        booking = db.get_booking_by_id(payload["bookingId"])
        self.assertIsNotNone(booking)
        self.assertEqual(booking.get("status"), "PENDING_PAYMENT")

    def test_confirmed_booking_triggers_notifier(self) -> None:
        recorder = _RecordingNotifier()
        app.NOTIFIER = recorder

        create_status, create_payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "KAP",
                "rentalType": "TWO_HOURS",
                "date": "2026-06-03",
                "startTime": "10:00",
            },
        )
        self.assertEqual(create_status, 201)
        booking_id = create_payload["bookingId"]

        callback_status, callback_payload = self._post_json(
            "/api/swish/callback",
            {"paymentReference": booking_id, "status": "PAID"},
        )
        self.assertEqual(callback_status, 200)
        self.assertTrue(callback_payload.get("ok"))

        self.assertEqual(len(recorder.confirmed_calls), 1)
        self.assertEqual(recorder.confirmed_calls[0].get("id"), booking_id)
        self.assertEqual(recorder.confirmed_calls[0].get("status"), "CONFIRMED")

    def test_paid_callback_clears_receipt_temp_fields_when_webhook_returns_302(self) -> None:
        requests_seen: list[str] = []

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # type: ignore[override]
                return

            def do_POST(self) -> None:  # noqa: N802
                content_length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(content_length)
                requests_seen.append(self.path)

                if self.path == "/exec":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.end_headers()
                    self.wfile.write(b"redirect")
                    return

                self.send_response(405)
                self.end_headers()
                self.wfile.write(b"method not allowed")

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_url = os.environ.get("NOTIFY_WEBHOOK_URL")
        old_secret = os.environ.get("NOTIFY_WEBHOOK_SECRET")
        try:
            os.environ["NOTIFY_WEBHOOK_URL"] = f"http://127.0.0.1:{server.server_port}/exec"
            os.environ["NOTIFY_WEBHOOK_SECRET"] = "test-secret"

            create_status, create_payload = self._post_json(
                "/api/hold",
                {
                    "trailerType": "KAP",
                    "rentalType": "TWO_HOURS",
                    "date": "2026-06-04",
                    "startTime": "10:00",
                    "receiptRequested": True,
                    "customerEmail": "receipt@example.com",
                },
            )
            self.assertEqual(create_status, 201)
            booking_id = create_payload["bookingId"]

            callback_status, callback_payload = self._post_json(
                "/api/swish/callback",
                {"paymentReference": booking_id, "status": "PAID"},
            )
            self.assertEqual(callback_status, 200)
            self.assertTrue(callback_payload.get("ok"))
            self.assertEqual(requests_seen, ["/exec"])

            booking = db.get_booking_by_id(booking_id)
            self.assertIsNotNone(booking)
            self.assertIsNone(booking.get("customer_email_temp"))
            self.assertEqual(booking.get("receipt_requested_temp"), 0)
        finally:
            if old_url is None:
                os.environ.pop("NOTIFY_WEBHOOK_URL", None)
            else:
                os.environ["NOTIFY_WEBHOOK_URL"] = old_url
            if old_secret is None:
                os.environ.pop("NOTIFY_WEBHOOK_SECRET", None)
            else:
                os.environ["NOTIFY_WEBHOOK_SECRET"] = old_secret
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ReceiptWebhookRedirectHandlingTest(unittest.TestCase):
    @patch("notifications.urllib.request.urlopen")
    def test_send_receipt_webhook_treats_initial_http_302_as_success(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = HTTPError(
            "https://example.com/exec",
            302,
            "Found",
            {"Location": "https://script.googleusercontent.com/macros/echo"},
            BytesIO(b"redirect"),
        )

        old_url = os.environ.get("NOTIFY_WEBHOOK_URL")
        old_secret = os.environ.get("NOTIFY_WEBHOOK_SECRET")
        os.environ["NOTIFY_WEBHOOK_URL"] = "https://example.com/exec"
        os.environ["NOTIFY_WEBHOOK_SECRET"] = "test-secret"
        try:
            ok = notifications.send_receipt_webhook(
                {
                    "id": 123,
                    "booking_reference": "DHS-20260219-000001",
                    "trailer_type": "KAP",
                    "start_dt": "2026-02-20T10:00",
                    "end_dt": "2026-02-20T12:00",
                    "price": 200,
                    "customer_email_temp": "test@example.com",
                    "receipt_requested_temp": 1,
                }
            )
            self.assertTrue(ok)
        finally:
            if old_url is None:
                os.environ.pop("NOTIFY_WEBHOOK_URL", None)
            else:
                os.environ["NOTIFY_WEBHOOK_URL"] = old_url
            if old_secret is None:
                os.environ.pop("NOTIFY_WEBHOOK_SECRET", None)
            else:
                os.environ["NOTIFY_WEBHOOK_SECRET"] = old_secret

    def _run_send_receipt_webhook_server_test(self, first_status: int, first_body: str) -> tuple[bool, list[str]]:
        requests_seen: list[str] = []

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # type: ignore[override]
                return

            def do_POST(self) -> None:  # noqa: N802
                content_length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(content_length)
                requests_seen.append(self.path)

                if self.path == "/exec":
                    self.send_response(first_status)
                    self.send_header("Location", "/final")
                    self.end_headers()
                    self.wfile.write(first_body.encode("utf-8"))
                    return

                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/exec"
            old_url = os.environ.get("NOTIFY_WEBHOOK_URL")
            old_secret = os.environ.get("NOTIFY_WEBHOOK_SECRET")
            os.environ["NOTIFY_WEBHOOK_URL"] = url
            os.environ["NOTIFY_WEBHOOK_SECRET"] = "test-secret"
            try:
                result = notifications.send_receipt_webhook(
                    {
                        "id": 123,
                        "booking_reference": "DHS-20260219-000001",
                        "trailer_type": "KAP",
                        "start_dt": "2026-02-20T10:00",
                        "end_dt": "2026-02-20T12:00",
                        "price": 200,
                        "customer_email_temp": "test@example.com",
                        "receipt_requested_temp": 1,
                    }
                )
                return result, requests_seen
            finally:
                if old_url is None:
                    os.environ.pop("NOTIFY_WEBHOOK_URL", None)
                else:
                    os.environ["NOTIFY_WEBHOOK_URL"] = old_url
                if old_secret is None:
                    os.environ.pop("NOTIFY_WEBHOOK_SECRET", None)
                else:
                    os.environ["NOTIFY_WEBHOOK_SECRET"] = old_secret
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_send_receipt_webhook_accepts_302_and_does_not_follow_redirect(self) -> None:
        ok, requests_seen = self._run_send_receipt_webhook_server_test(302, "redirect")
        self.assertTrue(ok)
        self.assertEqual(requests_seen, ["/exec"])

    def test_send_receipt_webhook_fail_on_405(self) -> None:
        ok, requests_seen = self._run_send_receipt_webhook_server_test(405, "method not allowed")
        self.assertFalse(ok)
        self.assertEqual(requests_seen, ["/exec"])


if __name__ == "__main__":
    unittest.main()
