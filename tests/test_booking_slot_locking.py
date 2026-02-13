import json
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import db


class BookingSlotLockingTest(unittest.TestCase):
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

    def _post_hold(self, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self._base_url}/api/hold",
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

    def _run_request_race(self, payload: dict, participants: int) -> list[tuple[int, dict]]:
        barrier = threading.Barrier(participants)
        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            result = self._post_hold(payload)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(participants)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "race worker did not finish")

        return results

    def test_atomic_slot_locking_under_race(self) -> None:
        trailer_type = "GALLER"
        rental_type = "TWO_HOURS"
        start_time = "10:00"
        base_date = date(2026, 3, 1)

        for round_index in range(20):
            with self.subTest(round=round_index + 1):
                race_date = (base_date + timedelta(days=round_index)).isoformat()
                payload = {
                    "trailerType": trailer_type,
                    "rentalType": rental_type,
                    "date": race_date,
                    "startTime": start_time,
                }
                results = self._run_request_race(payload, participants=3)
                self.assertEqual(len(results), 3)

                statuses = sorted(status for status, _ in results)
                self.assertEqual(statuses, [201, 201, 409])

                success_payload = next(body for status, body in results if status == 201)
                self.assertIn("bookingId", success_payload)
                self.assertIn("price", success_payload)

                conflict_payload = next(body for status, body in results if status == 409)
                self.assertEqual(conflict_payload.get("error"), "slot taken")


if __name__ == "__main__":
    unittest.main()
