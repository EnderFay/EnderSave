import os
import unittest
from unittest.mock import patch

from config import Settings


class ConfigTests(unittest.TestCase):
    def load(self, **overrides):
        # Настоящий .env с токеном никогда не используется тестами.
        with patch.dict(os.environ, {"BOT_TOKEN": "123456:dummy", **overrides}, clear=True), patch("config.load_dotenv"):
            return Settings.load()

    def test_default_limit_is_one_decimal_gigabyte(self):
        self.assertEqual(self.load().max_file_bytes, 1_000_000_000)

    def test_cloud_api_rejected_for_large_files(self):
        with self.assertRaises(ValueError):
            self.load(BOT_API_BASE_URL="https://api.telegram.org")

    def test_invalid_limits_rejected(self):
        for limit in ("0", "-1", "bad", "2000000001"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.load(MAX_FILE_BYTES=limit)

    def test_missing_bot_token_rejected(self):
        with self.assertRaises(ValueError):
            self.load(BOT_TOKEN="")

    def test_invalid_bitrate_rejected(self):
        with self.assertRaises(ValueError):
            self.load(MP3_BITRATE="999")


if __name__ == "__main__":
    unittest.main()
