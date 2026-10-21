"""v1.22 momentum mention extraction (offline). Run: .venv/bin/python -m unittest discover -s tests -v"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher as W  # noqa: E402

MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"


class MentionTests(unittest.TestCase):
    def test_ca_with_ticker(self):
        it = W.mention_items("New gem $MOON ca: " + MINT, None, "Chan", 1)
        self.assertEqual(it, [{"mint": MINT, "symbol": "MOON", "chat": "Chan", "ts_ms": 1}])

    def test_tickers_only_when_no_ca(self):
        it = W.mention_items("$WIF and $bonk pumping, $SOL up, $WIF again", None, "G", 2)
        self.assertEqual([x["symbol"] for x in it], ["WIF", "BONK"])
        self.assertTrue(all("mint" not in x and "text" not in x for x in it))

    def test_nothing(self):
        self.assertEqual(W.mention_items("gm frens", None, "G", 3), [])
        self.assertEqual(W.mention_items("price $100k mcap", None, "G", 3), [])

    def test_config_flag_default_off(self):
        saved = {k: os.environ.get(k) for k in ("MOMENTUM_MENTIONS_ENABLED", "TELEGRAM_API_ID", "TELEGRAM_API_HASH")}
        try:
            os.environ.pop("MOMENTUM_MENTIONS_ENABLED", None)
            os.environ["TELEGRAM_API_ID"], os.environ["TELEGRAM_API_HASH"] = "1", "x"
            W.load_dotenv = lambda *a, **k: None
            self.assertFalse(W.load_config()["momentum_mentions"])
            os.environ["MOMENTUM_MENTIONS_ENABLED"] = "true"
            self.assertTrue(W.load_config()["momentum_mentions"])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
