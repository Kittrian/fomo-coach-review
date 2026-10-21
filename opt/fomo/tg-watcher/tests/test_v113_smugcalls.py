"""v1.13 SmugCalls routing / alert mute / auto-join allow-list / scout alert format — offline."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alerts  # noqa: E402
import coach_ingest  # noqa: E402
import watcher as W  # noqa: E402

MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"


class SmugTests(unittest.TestCase):
    def test_source_tags(self):
        self.assertEqual(coach_ingest.source_for("SmugCalls"), "smugcalls/telegram")
        self.assertEqual(coach_ingest.source_for("Fluffy Alpha"), "fluffy/telegram")
        self.assertEqual(coach_ingest.source_for("Zyrox Insider Calls"), "telegram")

    def test_alerts_muted_only_for_smugcalls(self):
        no = ["smugcalls"]
        self.assertTrue(W.alerts_muted("SmugCalls", "SmugCalls", no))
        self.assertTrue(W.alerts_muted("Smug Calls 🐸", "smugcalls", no))
        self.assertFalse(W.alerts_muted("Fluffy Alpha", "Fluffyalpha", no))
        self.assertFalse(W.alerts_muted("Zyrox Insider Calls", None, no))

    def test_auto_join_allowlist_cannot_be_extended_by_env(self):
        with mock.patch.dict(os.environ, {"AUTO_JOIN_PUBLIC": "SmugCalls,SomeOtherChannel,@evil",
                                          "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x"}):
            cfg = W.load_config() if hasattr(W, "load_config") else None
        if cfg is not None:
            self.assertEqual(cfg["auto_join"], ["SmugCalls"])
            self.assertIn("smugcalls", cfg["no_alert"])
        self.assertEqual(W.AUTO_JOIN_ALLOWLIST, frozenset({"smugcalls"}))
        self.assertIn("SmugCalls", W.DEFAULT_GROUPS)

    def test_scout_alert_format(self):
        txt = alerts.format_scout_alert({"mint": MINT, "scout": "Jijo", "confirmer": "Cented",
                                         "mcap_usd": 8123.4, "edge_gap_pct": 6.25})
        self.assertIn("SCOUT B (paper)", txt)
        self.assertIn("Jijo + Cented", txt)
        self.assertIn(MINT, txt)
        self.assertIn("+6.2%", txt)

    def test_poll_scout_alerts_advances_cursor(self):
        with mock.patch.object(alerts, "_get_json", return_value={"alerts": [{"ts_ms": 5, "id": "b"},
                                                                             {"ts_ms": 3, "id": "a"}]}):
            got, since = alerts.poll_scout_alerts(1)
        self.assertEqual([a["id"] for a in got], ["a", "b"])
        self.assertEqual(since, 5)
        with mock.patch.object(alerts, "_get_json", return_value=None):
            self.assertEqual(alerts.poll_scout_alerts(9), ([], 9))


if __name__ == "__main__":
    unittest.main()
