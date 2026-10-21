"""v1.12.1 watcher parsing/dedupe tests — offline. Run: .venv/bin/python -m unittest discover -s tests -v"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alerts  # noqa: E402
import watcher as W  # noqa: E402

MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"
FLUFFY_5386 = ("I called $ANSEM, it did 2300x\nI called $CATE, it did 1230x\nI called $MEME, it did 800x\n"
               "I called $PONS, it did 2000x\nI called $CASHCAT, it did 400x\n\nNow I am calling $BULLCRAFT at "
               "$100k mcap \n\nThis is the next 1000x coin\n\nCa- " + MINT + "\n\n\nPlease do not miss out on "
               "this, just buy it and hold.")


class TickerTests(unittest.TestCase):
    def test_calling_wins_over_past_calls(self):
        p = W.parse_message(FLUFFY_5386)
        self.assertEqual(p["mint"], MINT)
        self.assertEqual(p["symbol"], "BULLCRAFT")

    def test_no_fragments(self):
        for txt in ("I called it, 1 Hour to go!", "coin to the moon", "Ca- soon, the coin is live", "it called LLED"):
            self.assertIsNone(W.parse_symbol(txt, None), txt)
        self.assertIsNone(W.parse_symbol("CA: $CA and $TO", None))

    def test_ticker_adjacent_to_ca(self):
        txt = f"gm. old: $WIF did 50x.\n\n$BULL\nCA: {MINT}"
        self.assertEqual(W.parse_symbol(txt, MINT), "BULL")

    def test_label(self):
        self.assertEqual(W.parse_symbol("Ticker: frog", None), "FROG")


class MintTests(unittest.TestCase):
    def test_pump_fun_link_only_in_entities(self):
        self.assertIsNone(W.parse_mint("LIVE NOW, click here"))
        self.assertEqual(W.parse_mint("LIVE NOW, click here", [f"https://pump.fun/coin/{MINT}"]), MINT)

    def test_dexscreener_and_gmgn_links(self):
        self.assertEqual(W.parse_mint("chart", [f"https://dexscreener.com/solana/{MINT}"]), MINT)
        self.assertEqual(W.parse_mint(f"https://gmgn.ai/sol/token/abc_{MINT}"), MINT)

    def test_message_links_from_entities_buttons_preview(self):
        msg = SimpleNamespace(
            entities=[SimpleNamespace(url="https://pump.fun/coin/" + MINT), SimpleNamespace()],
            reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[SimpleNamespace(url="https://dexscreener.com/solana/" + MINT),
                                                                       SimpleNamespace(text="no url")])]),
            media=SimpleNamespace(webpage=SimpleNamespace(url="https://t.me/x", display_url=None)))
        urls = W.message_links(msg)
        self.assertEqual(len(urls), 3)
        self.assertEqual(W.parse_message("Buy now", urls)["mint"], MINT)


class DedupeAlertTests(unittest.TestCase):
    def test_seen_store_persists(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "seen.json"
            s = alerts.SeenStore(p)
            self.assertEqual(s.note(1, MINT, 10)[:2], (True, 1))
            self.assertEqual(s.note(1, MINT, 11)[:2], (False, 2))
            self.assertTrue(s.note(1, MINT, 11)[2])  # same msg id (edit) flagged
            self.assertTrue(s.note(2, MINT, 5)[0])   # other source → first again
            self.assertFalse(alerts.SeenStore(p).note(1, MINT, 12)[0])  # survives restart

    def test_saved_messages_only(self):
        self.assertEqual(alerts.SAVED_MESSAGES, "me")

    def test_format_alert(self):
        t = alerts.format_alert(mint=MINT, symbol="BULLCRAFT", source="FluffyAlpha", message_id=5386,
                                curve={"status": "ok", "migrated": True, "mcap_sol": 860}, test=True,
                                verdict={"action": "skip", "decided_by": "jev", "lane": "migrated", "paper": "none"})
        self.assertIn("TEST", t)
        self.assertIn(f"https://pump.fun/coin/{MINT}", t)
        self.assertIn(f"https://dexscreener.com/solana/{MINT}", t)
        self.assertIn("MIGRATED", t)


if __name__ == "__main__":
    unittest.main()
