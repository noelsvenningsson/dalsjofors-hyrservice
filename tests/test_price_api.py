import json
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import urlopen

import app
import db


class PriceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = TemporaryDirectory()
        cls._original_db_path = db.DB_PATH
        db.DB_PATH = Path(cls._tmpdir.name) / "test_database.db"
        db.init_db()

        cls._server = HTTPServer(("127.0.0.1", 0), app.Handler)
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

    def _get_price(self, trailer_type: str, rental_type: str, date_str: str) -> int:
        query = urlencode(
            {
                "trailerType": trailer_type,
                "rentalType": rental_type,
                "date": date_str,
            }
        )
        with urlopen(f"{self._base_url}/api/price?{query}") as resp:
            self.assertEqual(resp.status, 200)
            payload = json.loads(resp.read().decode("utf-8"))
            return payload["price"]

    def test_price_for_all_required_combinations(self) -> None:
        # Monday, February 9, 2026 -> full day weekday price.
        cases = [
            ("GALLER", "TWO_HOURS", 200),
            ("GALLER", "FULL_DAY", 250),
            ("KAP", "TWO_HOURS", 200),
            ("KAP", "FULL_DAY", 250),
        ]
        for trailer_type, rental_type, expected_price in cases:
            with self.subTest(trailer_type=trailer_type, rental_type=rental_type):
                actual_price = self._get_price(trailer_type, rental_type, "2026-02-09")
                self.assertEqual(actual_price, expected_price)


if __name__ == "__main__":
    unittest.main()
