import os
import runpy
import unittest
from pathlib import Path
from unittest import mock

import sms_provider


class SmsProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        sms_provider._twilio_disabled_logged = False

    def test_module_import_has_no_network_side_effects(self) -> None:
        module_path = Path(__file__).resolve().parents[1] / "sms_provider.py"
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call on import")):
            runpy.run_path(str(module_path), run_name="__sms_provider_import_check__")

    def test_send_sms_missing_env_returns_false_without_network(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TWILIO_ACCOUNT_SID": "",
                "TWILIO_AUTH_TOKEN": "",
                "TWILIO_FROM_NUMBER": "",
            },
            clear=False,
        ):
            with mock.patch("sms_provider.urllib.request.urlopen") as mocked_urlopen:
                with self.assertLogs("sms_provider", level="WARNING") as logs:
                    ok = sms_provider.send_sms("+46701234567", "test")
        self.assertFalse(ok)
        mocked_urlopen.assert_not_called()
        self.assertTrue(any("missing Twilio env vars" in line for line in logs.output))

    def test_send_sms_missing_env_logs_only_once_across_multiple_calls(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TWILIO_ACCOUNT_SID": "",
                "TWILIO_AUTH_TOKEN": "",
                "TWILIO_FROM_NUMBER": "",
            },
            clear=False,
        ):
            with mock.patch("sms_provider.urllib.request.urlopen") as mocked_urlopen:
                with self.assertLogs("sms_provider", level="WARNING") as logs:
                    first = sms_provider.send_sms("+46701234567", "test1")
                    second = sms_provider.send_sms("+46701234567", "test2")
                    third = sms_provider.send_sms("+46701234567", "test3")
        self.assertFalse(first)
        self.assertFalse(second)
        self.assertFalse(third)
        mocked_urlopen.assert_not_called()
        self.assertEqual(sum("missing Twilio env vars" in line for line in logs.output), 1)

    def test_normalize_swedish_mobile(self) -> None:
        self.assertEqual(sms_provider.normalize_swedish_mobile("0701234567"), "+46701234567")
        self.assertEqual(sms_provider.normalize_swedish_mobile("+46701234567"), "+46701234567")
        self.assertEqual(sms_provider.normalize_swedish_mobile("0046701234567"), "+46701234567")
        self.assertIsNone(sms_provider.normalize_swedish_mobile("031123456"))


if __name__ == "__main__":
    unittest.main()
