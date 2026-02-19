import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import app
import db


class ApiValidationHardeningTest(unittest.TestCase):
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

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if path.startswith("/api/admin/"):
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

    def _assert_stable_error(
        self,
        payload: dict,
        *,
        expected_code: str,
        expected_field: str | None = None,
        expected_legacy_error: str | None = None,
    ) -> None:
        self.assertIsInstance(payload.get("error"), str)
        if expected_legacy_error is not None:
            self.assertEqual(payload.get("error"), expected_legacy_error)
        self.assertIn("errorInfo", payload)
        self.assertEqual(payload.get("errorInfo", {}).get("code"), expected_code)
        self.assertIsInstance(payload.get("errorInfo", {}).get("message"), str)
        if expected_field is not None:
            self.assertIn(expected_field, payload.get("errorInfo", {}).get("details", {}).get("fields", {}))

    def test_invalid_trailer_or_rental_type_returns_400_with_stable_payload(self) -> None:
        status, payload = self._get_json(
            "/api/availability",
            {
                "trailerType": "INVALID",
                "rentalType": "TWO_HOURS",
                "date": "2026-05-01",
                "startTime": "10:00",
            },
        )
        self.assertEqual(status, 400)
        self._assert_stable_error(payload, expected_code="invalid_request", expected_field="trailerType")

        status, payload = self._get_json(
            "/api/price",
            {"trailerType": "KAP", "rentalType": "UNKNOWN", "date": "2026-05-01"},
        )
        self.assertEqual(status, 400)
        self._assert_stable_error(payload, expected_code="invalid_request", expected_field="rentalType")

    def test_invalid_date_or_starttime_returns_400_with_stable_payload(self) -> None:
        status, payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "date": "2026/05/01",
                "startTime": "10:00",
            },
        )
        self.assertEqual(status, 400)
        self._assert_stable_error(payload, expected_code="invalid_request", expected_field="date")

        status, payload = self._post_json(
            "/api/hold",
            {
                "trailerType": "GALLER",
                "rentalType": "TWO_HOURS",
                "date": "2026-05-01",
                "startTime": "9:7",
            },
        )
        self.assertEqual(status, 400)
        self._assert_stable_error(payload, expected_code="invalid_request", expected_field="startTime")

    def test_admin_block_invalid_datetime_returns_400_with_stable_payload(self) -> None:
        status, payload = self._post_json(
            "/api/admin/blocks",
            {
                "trailerType": "KAP",
                "startDatetime": "2026-05-01 10:00",
                "endDatetime": "2026-05-01T12:00",
            },
        )
        self.assertEqual(status, 400)
        self._assert_stable_error(payload, expected_code="invalid_request", expected_field="startDatetime")


if __name__ == "__main__":
    unittest.main()
