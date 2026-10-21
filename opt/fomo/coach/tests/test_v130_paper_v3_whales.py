"""v1.30: whales (peacefuldestroy, Mitch) in paper_v3_new, PAPER ONLY, with a 25% chase cap vs the whale's fill."""
from __future__ import annotations

import asyncio
import json
import time

from app.live.engine import LiveEngine
from tests.test_v129_paper_v3 import _drain, _rows, v3  # noqa: F401  (fixture)

MITCH = "4Be9CvxqHW6BYiRAxW9Q3xu1ycTMWaL5z8NX4HR3ha7t"
PEACE = "8AtQ4ka3dgtrH1z4Uq3Tm4YdMN3cK5RRj1eKuGNnvenm"
NUMER0 = "A3W8psibkTUvjxs4LRscbnjux6TFDXdvD4m4GsGpQ2KJ"
M1 = "MintWHAL1111111111111111111111111111111pump"


def test_whales_are_paper_targets_only():
    import app.paper_v3 as pv
    c = pv.cfg()
    assert set(c["whales"]) == {MITCH, PEACE} and MITCH in c["targets"] and PEACE in c["targets"]
    assert c["whale_max_chase_pct"] == 25


def _buy(p, wallet, trader_px):
    async def go():
        st = p.on_scout_trade(wallet, "v3:x", True, M1, price_sol=trader_px, sol=10.0, sig="W1",
                              recv_ms=int(time.time() * 1000), venue="pump_curve")
        await _drain(p)
        return st
    return asyncio.run(go())


def test_whale_skipped_above_cap_and_logged(v3):  # noqa: F811
    p, prices, tmp = v3
    prices["px"] = 0.000130                       # +30% above whale fill 0.0001
    _buy(p, MITCH, 0.0001)
    assert not p.book and M1 not in p.seen_mints
    sk = [r for r in _rows(tmp / "paper_v3.jsonl") if r["kind"] == "skip" and r.get("whale_cap")]
    assert sk and sk[0]["chase_pct"] == 30.0 and sk[0]["cap_pct"] == 25
    assert LiveEngine().day()["trades"] == 0


def test_whale_entered_within_cap(v3):  # noqa: F811
    p, prices, tmp = v3
    prices["px"] = 0.000120                       # +20%: inside the cap
    _buy(p, PEACE, 0.0001)
    assert len(p.book) == 1


def test_whale_unknown_fill_price_skipped(v3):  # noqa: F811
    p, prices, tmp = v3
    _buy(p, MITCH, None)
    assert not p.book


def test_non_whale_not_capped(v3):  # noqa: F811
    p, prices, tmp = v3
    prices["px"] = 0.000200                       # +100% but Numer0 is not a whale: no cap
    _buy(p, NUMER0, 0.0001)
    assert len(p.book) == 1


def test_whales_get_paper_role_never_scout_or_confirmer(monkeypatch):
    from app.scout.roster import Roster
    monkeypatch.setenv("PAPER_V3_ENABLED", "true")
    r = Roster()
    r.load()
    r.add_paper_v3_wallets()
    for a in (MITCH, PEACE):
        assert r.by_wallet[a].role == "paper_v3"
