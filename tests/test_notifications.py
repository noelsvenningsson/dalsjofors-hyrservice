import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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


class RedirectPreservingPostTest(unittest.TestCase):
    def _run_redirect_server_test(self, final_status: int, final_body: str) -> tuple[int, str, list[tuple[str, str]]]:
        requests_seen: list[tuple[str, str]] = []

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # type: ignore[override]
                return

            def do_POST(self) -> None:  # noqa: N802
                content_length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(content_length).decode("utf-8")
                requests_seen.append((self.path, body))

                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.end_headers()
                    self.wfile.write(b"redirect")
                    return
                if self.path == "/final":
                    self.send_response(final_status)
                    self.end_headers()
                    self.wfile.write(final_body.encode("utf-8"))
                    return

                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/start"
            status, body = notifications._post_json_with_redirect_preserving_post(
                url,
                {"hello": "world"},
                timeout_seconds=2,
                max_redirects=2,
            )
            return status, body, requests_seen
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_post_redirect_preserving_post_success(self) -> None:
        status, body, requests_seen = self._run_redirect_server_test(200, "ok")
        self.assertEqual(status, 200)
        self.assertEqual(body, "ok")
        self.assertEqual(len(requests_seen), 2)
        self.assertEqual(requests_seen[0][0], "/start")
        self.assertEqual(requests_seen[1][0], "/final")
        self.assertEqual(requests_seen[0][1], requests_seen[1][1])

    def test_post_redirect_preserving_post_final_405(self) -> None:
        status, body, requests_seen = self._run_redirect_server_test(405, "method not allowed")
        self.assertEqual(status, 405)
        self.assertIn("method not allowed", body)
        self.assertEqual(len(requests_seen), 2)
        self.assertEqual(requests_seen[0][0], "/start")
        self.assertEqual(requests_seen[1][0], "/final")


if __name__ == "__main__":
    unittest.main()
