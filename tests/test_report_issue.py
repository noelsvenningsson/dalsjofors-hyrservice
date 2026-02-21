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


def _build_multipart(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----dhsreportboundary"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for field_name, filename, content_type, payload in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class ReportIssueTest(unittest.TestCase):
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

    def _request(self, method: str, path: str, *, data: bytes = b"", headers: dict[str, str] | None = None) -> tuple[int, str]:
        req = Request(
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers=headers or {},
        )
        try:
            with urlopen(req) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as err:
            return err.code, err.read().decode("utf-8")

    def test_parse_form_data_multipart_extracts_fields_and_file(self) -> None:
        body, content_type = _build_multipart(
            {"name": "Alice", "message": "Hej"},
            [("images", "damage.png", "image/png", b"\x89PNG\r\n\x1a\n")],
        )
        fields, files = app.parse_form_data(content_type, body)

        self.assertEqual(fields["name"], "Alice")
        self.assertEqual(fields["message"], "Hej")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["field_name"], "images")
        self.assertEqual(files[0]["filename"], "damage.png")
        self.assertEqual(files[0]["content_type"], "image/png")
        self.assertEqual(files[0]["data_bytes"], b"\x89PNG\r\n\x1a\n")

    def test_parse_form_data_urlencoded_extracts_fields(self) -> None:
        body = b"name=Alice+Andersson&message=Hej+igen&website="
        fields, files = app.parse_form_data("application/x-www-form-urlencoded", body)

        self.assertEqual(fields["name"], "Alice Andersson")
        self.assertEqual(fields["message"], "Hej igen")
        self.assertEqual(fields["website"], "")
        self.assertEqual(files, [])

    def test_report_issue_post_sends_mail_with_attachment(self) -> None:
        fields = {
            "name": "Test Person",
            "phone": "0701234567",
            "email": "test@example.com",
            "trailer_type": "GALLER",
            "booking_reference": "DHS-TEST-1",
            "detected_at": "2026-02-21T13:10",
            "report_type": "DURING_RENTAL",
            "message": "Skrapskada på vänster sida.",
            "website": "",
        }
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        body, content_type = _build_multipart(
            fields,
            [("images", "damage.png", "image/png", png_bytes)],
        )
        env_backup = {k: os.environ.get(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "REPORT_TO")}
        os.environ["SMTP_HOST"] = "smtp.example.com"
        os.environ["SMTP_PORT"] = "587"
        os.environ["SMTP_USER"] = "user@example.com"
        os.environ["SMTP_PASSWORD"] = "secret"
        os.environ["SMTP_FROM"] = "noreply@example.com"
        os.environ["REPORT_TO"] = "svenningsson@outlook.com"
        try:
            with mock.patch("smtplib.SMTP") as smtp_class:
                smtp_client = smtp_class.return_value.__enter__.return_value
                smtp_client.has_extn.return_value = False
                status, response_text = self._request(
                    "POST",
                    "/report-issue",
                    data=body,
                    headers={"Content-Type": content_type},
                )

                self.assertEqual(status, 200)
                self.assertIn("Rapport mottagen. Vi återkommer.", response_text)
                smtp_client.send_message.assert_called_once()
                message = smtp_client.send_message.call_args.args[0]
                self.assertEqual(message["To"], "svenningsson@outlook.com")
                attachments = list(message.iter_attachments())
                self.assertEqual(len(attachments), 1)
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
