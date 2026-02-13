import json
import threading
import unittest
from datetime import datetime
from http.server import HTTPServer
from urllib.request import urlopen

import app


class HealthApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server = HTTPServer(("127.0.0.1", 0), app.Handler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls._base_url = f"http://127.0.0.1:{cls._server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()
        cls._thread.join(timeout=2)

    def test_health_returns_200_and_expected_payload_keys(self) -> None:
        with urlopen(f"{self._base_url}/api/health") as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))

        self.assertIn("ok", payload)
        self.assertIn("service", payload)
        self.assertIn("time", payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "dalsjofors-hyrservice")
        datetime.fromisoformat(payload["time"])


if __name__ == "__main__":
    unittest.main()
