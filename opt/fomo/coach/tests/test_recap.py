"""v1.17.3 Day Recap: read-only endpoint on sample data; live engine untouched; no network in tests."""
import copy
import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app import recap

DAY = date(2026, 10, 8)
T0 = int(datetime(2026, 10, 8, 11, 45, 0, tzinfo=recap.TZ).timestamp() * 1000)
M1, M2, M3 = "CBoJBdjSLZuBQLXLLB6V7qXiwXfV4eMryQnUUoBpKkMn", "DRRyYvxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump", "AACtroxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"
SCOUT = "Cv5GgkpXvtXcsrMcKCJvx46BpMjiM61mpPU1j8RbbbVL"


def _w(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture()
def sample(tmp_path, monkeypatch):
    led = tmp_path / "live_trades.jsonl"
    _w(led, [
        # previous day: must not count
        {"ts_ms": T0 - 86_400_000, "kind": "buy", "live": True, "id": "old", "mint": "OLD", "arm": "scout_solo",
         "size_sol": 0.1, "spent_lamports": 100000000, "entry_ms": T0 - 86_400_000, "meta": {}},
        {"ts_ms": T0 - 86_390_000, "kind": "sell", "live": True, "id": "old", "mint": "OLD", "reason": "trader_sold", "sol": 0.05, "pnl_sol": -0.05, "tokens": 1},
        {"ts_ms": T0 - 86_390_000, "kind": "close", "live": True, "id": "old", "mint": "OLD", "reason": "trader_sold", "pnl": {"pnl_sol": -0.05}},
        # winner: half at 2x then trail
        {"ts_ms": T0, "kind": "buy", "live": True, "id": "w1", "mint": M1, "arm": "scout_solo", "size_sol": 0.1,
         "spent_lamports": 100000000, "tokens_initial": 1000, "entry_ms": T0, "entry_price_sol": 1e-7, "buy_sig": "BUY1",
         "trader_wallets": [SCOUT], "meta": {"detect_ms": T0 - 3600, "buy_ms": 950, "edge_gap_pct": -8.6,
                                              "timing": {"gates_ms": 400, "swap_ms": 900, "post_fill_ms": 300, "detect_to_open_ms": 1900}}},
        {"ts_ms": T0 + 28_000, "kind": "sell", "live": True, "id": "w1", "mint": M1, "reason": "tp_2x", "sol": 0.1063, "pnl_sol": 0.0563, "tokens": 500},
        {"ts_ms": T0 + 53_000, "kind": "sell", "live": True, "id": "w1", "mint": M1, "reason": "trail", "sol": 0.1084, "pnl_sol": 0.0584, "tokens": 500},
        {"ts_ms": T0 + 53_000, "kind": "close", "live": True, "id": "w1", "mint": M1, "reason": "trail",
         "pnl": {"pnl_sol": 0.1147, "pnl_pct": 114.7}},
        # loser: followed the scout out
        {"ts_ms": T0 - 3_600_000, "kind": "buy", "live": True, "id": "l1", "mint": M2, "arm": "scout_solo", "size_sol": 0.1,
         "spent_lamports": 100000000, "tokens_initial": 1000, "entry_ms": T0 - 3_600_000, "trader_wallets": [SCOUT],
         "meta": {"detect_ms": T0 - 3_604_000, "buy_ms": 1000}},
        {"ts_ms": T0 - 3_590_000, "kind": "sell", "live": True, "id": "l1", "mint": M2, "reason": "trader_sold", "sol": 0.085, "pnl_sol": -0.015, "tokens": 1000},
        {"ts_ms": T0 - 3_590_000, "kind": "close", "live": True, "id": "l1", "mint": M2, "reason": "trader_sold", "pnl": {"pnl_sol": -0.015, "pnl_pct": -15.0}},
        # blocked buy (rug check) that later ran
        {"ts_ms": T0 + 3_000_000, "kind": "buy_aborted", "live": True, "mint": M3, "reason": "rugcheck top10: top holders 45.5% (Jupiter audit)"},
        {"ts_ms": T0 + 3_100_000, "kind": "tg_buy_refused", "live": True, "arm": "fluffy_pump_dump", "mint": "FLUF", "reasons": ["lane migrated"]},
    ])
    monkeypatch.setenv("LIVE_LEDGER_PATH", str(led))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path))
    for k in ("SCOUT_LOG_PATH", "CANDIDATES_PATH", "CANDIDATE_OUTCOMES_PATH", "PAPER_LOG_PATH", "SCOUT_DETECTIONS_PATH",
              "SCOUT_WALLETS_PATH", "SCOUT_TRIAL_LOG", "EXIT_VARIANTS_PATH", "EXIT_VARIANTS_LOG_PATH", "RECAP_CACHE_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RECAP_NET_ENABLED", "false")
    _w(tmp_path / "candidates.jsonl", [
        {"ts_ms": T0 + 2_999_000, "cid": "c1", "arm": "scout_solo", "mint": M3, "decision": "enter"},
        {"ts_ms": T0 + 3_000_000, "cid": "c2", "arm": "scout_solo", "mint": M3, "decision": "skip", "skip_reason": "rugcheck top#"},
    ])
    _w(tmp_path / "candidate_outcomes.jsonl", [
        {"ts_ms": T0 + 3_000_100, "cid": "c1", "mint": M3, "kind": "outcome", "horizon_s": 0, "price_sol": 1e-7, "ret": 0.0},
        {"ts_ms": T0 + 3_060_000, "cid": "c1", "mint": M3, "kind": "outcome", "horizon_s": 60, "ret": 2.1},
        {"ts_ms": T0 + 3_300_000, "cid": "c1", "mint": M3, "kind": "outcome", "horizon_s": 300, "ret": 19.2},
        {"ts_ms": T0 + 6_600_000, "cid": "c1", "mint": M3, "kind": "outcome", "horizon_s": 3600, "ret": 0.19},
    ])
    _w(tmp_path / "scout_trades.jsonl", [
        {"ts_ms": T0 - 100, "kind": "signal", "paper": True, "arm": "scout_solo", "mint": M1, "decision": "enter",
         "scout": "R4", "scout_sig": "SCOUTBUY1"},
    ])
    _w(tmp_path / "exit_variants.jsonl", [
        {"ts_ms": T0 + 60_000, "kind": "variant_closed", "signal_id": "s1", "variant": "A", "plan": "half 2x + trail 30", "pnl_sol": 0.11},
        {"ts_ms": T0 + 60_000, "kind": "variant_closed", "signal_id": "s1", "variant": "B", "plan": "quarters 20/40", "pnl_sol": 0.13},
        {"ts_ms": T0 + 90_000, "kind": "variant_closed", "signal_id": "s2", "variant": "B", "pnl_sol": 9.0, "restart_gap": True},
        {"ts_ms": T0 - 86_400_000, "kind": "variant_closed", "signal_id": "s0", "variant": "A", "pnl_sol": -0.01},
    ])
    recap._file_cache.clear()
    recap._resp_cache.clear()
    recap.enrich.slots = {"BUY1": 1000, "SCOUTBUY1": 987}
    recap.enrich.dex = {}
    recap.enrich._loaded = True
    yield tmp_path
    recap._file_cache.clear()
    recap._resp_cache.clear()


def test_build_recap_sample(sample):
    r = recap.build_recap(DAY, live={"buys_enabled": True, "kill": False, "arms": ["scout_solo"], "open_positions": 0,
                                     "warnings": []}, open_positions=[], version="t")
    t = r["tiles"]
    assert t["trades"] == 2 and t["closed"] == 2 and t["wins"] == 1 and t["losses"] == 1
    assert t["realized_pnl_sol"] == pytest.approx(0.1147 - 0.015, abs=1e-6)
    assert t["best"]["mint"] == M1 and t["worst"]["mint"] == M2
    w = next(x for x in r["trades"] if x["id"] == "w1")
    assert [e["category"] for e in w["exits"]] == ["tp", "trail"] and w["exit_category"] == "trail"
    assert w["speed"]["detect_to_open_ms"] == 1900 and w["speed"]["slots_behind_scout"] == 13
    assert w["scout_label"] == "R4" and w["hold_s"] == 53.0
    assert [p["cum_pnl_sol"] for p in r["timeline"]] == pytest.approx([-0.015, 0.0413, 0.0997], abs=1e-6)
    cats = {b["category"]: b for b in r["blocks"]}
    assert cats["rug_check"]["outcome"]["verdict"] == "missed_winner" and cats["rug_check"]["arm"] == "scout_solo"
    assert "tg_refused" in cats
    xv = r["exit_variants"]
    assert xv["base"] == "A" and xv["variants"][0]["variant"] == "B" and xv["variants"][0]["vs_base_sol"] == pytest.approx(0.02)
    assert xv["variants"][0]["beats_base"] == 1 and xv["variants"][0]["label"] == "quarters 20/40"
    assert xv["all_time"]["variants"][-1]["pnl_sol"] == pytest.approx(0.10)       # A all-time: 0.11 - 0.01; gap row excluded
    take = " ".join(r["take"])
    assert "Up 0.0997 SOL" in take and "carried the day" in take and "Following the scout" in take and "missed winner" in take
    assert r["date"] == "2026-10-08"


def test_endpoints_and_page(sample):
    from app.main import app
    from app.live.engine import engine
    before = (copy.deepcopy(dict(engine.stats)), dict(engine.positions), dict(engine.bought_mints), engine.api_kill)
    c = TestClient(app)
    r = c.get("/api/recap", params={"date": "2026-10-08"})
    assert r.status_code == 200 and r.json()["tiles"]["trades"] == 2 and r.json()["read_only"] is True
    assert "_want" not in r.json()
    r2 = c.get("/api/recap/today")
    assert r2.status_code == 200 and r2.json()["is_today"] is True and r2.json()["status"] is not None
    assert c.get("/api/recap", params={"date": "nope"}).status_code == 400
    p = c.get("/recap")
    assert p.status_code == 200 and "Day Recap" in p.text and "cdn" not in p.text.lower()
    after = (dict(engine.stats), dict(engine.positions), dict(engine.bought_mints), engine.api_kill)
    assert before == after                                         # the recap never mutates the live engine


def test_day_bounds_are_chicago_calendar_days():
    s, e = recap.day_bounds(date(2026, 11, 1))                     # DST ends → 25-hour day
    assert (e - s) == 25 * 3_600_000
    s, e = recap.day_bounds(date(2026, 10, 8))
    assert datetime.fromtimestamp(s / 1000, recap.TZ).hour == 0 and (e - s) == 24 * 3_600_000


def test_no_secrets_and_no_helius_rpc(sample, monkeypatch):
    monkeypatch.setenv("SCOUT_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=SECRET-123")
    monkeypatch.setenv("HELIUS_API_KEY", "SECRET-123")
    assert "helius" not in recap._rpc_url()
    r = recap.build_recap(DAY)
    assert "SECRET-123" not in json.dumps(r)


def test_soft_fail_on_missing_files(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_LEDGER_PATH", str(tmp_path / "nope.jsonl"))
    for k in ("SCOUT_LOG_PATH", "CANDIDATES_PATH", "CANDIDATE_OUTCOMES_PATH", "PAPER_LOG_PATH", "SCOUT_DETECTIONS_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RECAP_NET_ENABLED", "false")
    recap._file_cache.clear()
    r = recap.build_recap(DAY)
    assert r["tiles"]["trades"] == 0 and r["exit_variants"] is None and r["take"][0].startswith("No live trades")
