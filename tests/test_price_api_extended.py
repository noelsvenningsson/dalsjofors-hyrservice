import json
import threading
import unittest
from datetime import datetime
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import urlopen

import app
import db


class PriceApiExtendedTest(unittest.TestCase):
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

    def _get_price_payload(self, trailer_type: str, rental_type: str, date_str: str) -> dict:
        query = urlencode(
            {
                "trailerType": trailer_type,
                "rentalType": rental_type,
                "date": date_str,
            }
        )
        with urlopen(f"{self._base_url}/api/price?{query}") as resp:
            self.assertEqual(resp.status, 200)
            return json.loads(resp.read().decode("utf-8"))

    def test_calculate_price_weekday_weekend_holiday(self) -> None:
        self.assertEqual(
            db.calculate_price(datetime(2026, 2, 10, 10, 0), "FULL_DAY", "GALLER"),
            250,
        )
        self.assertEqual(
            db.calculate_price(datetime(2026, 2, 14, 10, 0), "FULL_DAY", "GALLER"),
            300,
        )
        self.assertEqual(
            db.calculate_price(datetime(2026, 5, 14, 10, 0), "FULL_DAY", "KAP"),
            300,
        )

    def test_api_price_returns_day_type_label(self) -> None:
        weekday = self._get_price_payload("GALLER", "FULL_DAY", "2026-02-10")
        self.assertEqual(weekday.get("price"), 250)
        self.assertEqual(weekday.get("dayTypeLabel"), "Vardag")

        weekend = self._get_price_payload("GALLER", "FULL_DAY", "2026-02-14")
        self.assertEqual(weekend.get("price"), 300)
        self.assertEqual(weekend.get("dayTypeLabel"), "Helg/röd dag")

        holiday = self._get_price_payload("KAP", "FULL_DAY", "2026-05-14")
        self.assertEqual(holiday.get("price"), 300)
        self.assertEqual(holiday.get("dayTypeLabel"), "Helg/röd dag")


if __name__ == "__main__":
    unittest.main()
