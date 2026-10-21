"""Coach v1.15: the watcher tells the coach whether a CA is its first sighting (offline, no network)."""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_ingest  # noqa: E402
import watcher as W  # noqa: E402

MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"


class FirstSeenTests(unittest.TestCase):
    def test_meta_carries_first_seen_and_repeat_count(self):
        b = coach_ingest.build_ingest_body(mint=MINT, symbol="X", source_group="FluffyPumpDump", text="t",
                                           first_seen=False, repeat_count=3)
        self.assertIs(b["meta"]["first_seen"], False)
        self.assertEqual(b["meta"]["repeat_count"], 3)
        b2 = coach_ingest.build_ingest_body(mint=MINT, symbol="X", source_group="FluffyPumpDump", text="t")
        self.assertIsNone(b2["meta"]["first_seen"])            # older callers: unknown, not "repeat"

    def test_post_ca_announce_accepts_new_params(self):
        params = inspect.signature(coach_ingest.post_ca_announce).parameters
        self.assertIn("first_seen", params)
        self.assertIn("repeat_count", params)

    def test_watcher_passes_seen_result(self):
        src = inspect.getsource(W)
        self.assertIn("first_seen=bool(first)", src)
        self.assertIn("repeat_count=int(count or 1)", src)


if __name__ == "__main__":
    unittest.main()
