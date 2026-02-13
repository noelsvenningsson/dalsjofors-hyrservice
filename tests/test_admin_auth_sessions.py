import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

import app
import db


class AdminAuthSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = TemporaryDirectory()
        cls._original_db_path = db.DB_PATH
        cls._original_admin_token = os.environ.get("ADMIN_TOKEN")
        cls._original_session_secret = os.environ.get("ADMIN_SESSION_SECRET")
        cls._admin_token = "test-admin-token"
        cls._session_secret = "test-admin-session-secret"
        os.environ["ADMIN_TOKEN"] = cls._admin_token
        os.environ["ADMIN_SESSION_SECRET"] = cls._session_secret
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
        if cls._original_session_secret is None:
            os.environ.pop("ADMIN_SESSION_SECRET", None)
        else:
            os.environ["ADMIN_SESSION_SECRET"] = cls._original_session_secret
        db.DB_PATH = cls._original_db_path
        cls._tmpdir.cleanup()

    def _request(
        self, method: str, path: str, *, body: str | None = None, headers: dict | None = None
    ) -> tuple[int, http.client.HTTPMessage, str]:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=5)
        request_headers = dict(headers or {})
        payload = None if body is None else body.encode("utf-8")
        conn.request(method, path, body=payload, headers=request_headers)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        out = (response.status, response.headers, data)
        conn.close()
        return out

    def _login_and_get_cookie(self) -> str:
        form = urlencode({"token": self._admin_token})
        status, headers, _ = self._request(
            "POST",
            "/admin/login",
            body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        set_cookie = headers.get("Set-Cookie")
        self.assertIsNotNone(set_cookie)
        return set_cookie.split(";", 1)[0]

    def test_login_success(self) -> None:
        form = urlencode({"token": self._admin_token})
        status, headers, _ = self._request(
            "POST",
            "/admin/login",
            body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/admin")
        set_cookie = headers.get("Set-Cookie")
        self.assertIsNotNone(set_cookie)
        self.assertIn("admin_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertIn("Max-Age=28800", set_cookie)

    def test_login_failure(self) -> None:
        form = urlencode({"token": "wrong-token"})
        status, headers, body = self._request(
            "POST",
            "/admin/login",
            body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertIn("Fel token", body)

    def test_admin_authorized_after_login_cookie(self) -> None:
        cookie = self._login_and_get_cookie()
        status, _, body = self._request("GET", "/admin", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("Admin Dashboard", body)

    def test_api_admin_unauthorized_without_auth(self) -> None:
        status, _, body = self._request("GET", "/api/admin/bookings")
        self.assertEqual(status, 401)
        payload = json.loads(body)
        self.assertEqual(payload.get("errorInfo", {}).get("code"), "unauthorized")

    def test_api_admin_authorized_with_header(self) -> None:
        status, _, body = self._request(
            "GET", "/api/admin/bookings", headers={"X-Admin-Token": self._admin_token}
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("bookings", payload)

    def test_tampered_cookie_rejected(self) -> None:
        cookie = self._login_and_get_cookie()
        cookie_name, cookie_value = cookie.split("=", 1)
        tampered_value = f"{cookie_value}x"
        tampered_cookie = f"{cookie_name}={tampered_value}"
        status, _, body = self._request(
            "GET", "/api/admin/bookings", headers={"Cookie": tampered_cookie}
        )
        self.assertEqual(status, 401)
        payload = json.loads(body)
        self.assertEqual(payload.get("errorInfo", {}).get("code"), "unauthorized")


if __name__ == "__main__":
    unittest.main()
