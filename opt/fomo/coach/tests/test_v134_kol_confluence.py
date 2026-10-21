"""v1.34 cryptohub wallets (paper/log only) + kol_confluence paper arm + pumped_since_first_alert feature + learner gate."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.live import jupiter
from app.live.engine import LiveEngine

ROOT = Path(__file__).resolve().parents[1]
MINT = "MintKOLC1111111111111111111111111111111pump"
W = ["4xY9T1Q7foJzJsJ6YZDSsfp9zkzeZsXnxd45SixduMmr", "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o",
     "3BLjRcxWGtR7WRshJ3hL25U3RjWr5Ud98wMcczQqk4Ei", "9EhcDGGJ6NpetxNumZKwD1o1kuHBwvfNGKrmS6zekP7C"]
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@pytest.fixture
def kc(monkeypatch, tmp_path):
    import app.paper_fomo_top as ftp
    import app.paper_kol_confluence as m
    monkeypatch.setenv("KOL_CONFLUENCE_ENABLED", "true")
    monkeypatch.setenv("KOL_CONFLUENCE_LOG_PATH", str(tmp_path / "kc.jsonl"))
    monkeypatch.setenv("KOL_CONFLUENCE_STATE_PATH", str(tmp_path / "kc_state.json"))
    for k in ("KOL_CONFLUENCE_N", "KOL_CONFLUENCE_WINDOW_MIN", "KOL_CONFLUENCE_ROLES", "KOL_CONFLUENCE_LIVE_ENABLED"):
        monkeypatch.delenv(k, raising=False)

    def boom(*a, **k):
        raise AssertionError("paper arm must never reach the live executor / signer / Jupiter")
    for name in ("order", "execute", "swap", "sign_partial", "keypair", "simulate_order", "simulate_signed"):
        monkeypatch.setattr(jupiter, name, boom)
    monkeypatch.setattr(LiveEngine, "on_signal", boom)
    monkeypatch.setattr(LiveEngine, "_buy", boom)
    monkeypatch.setattr(LiveEngine, "_sell", boom)
    p = m.KolConfluence()
    prices = {"px": 0.00015}

    async def price_sol(mint, force=True):
        return prices["px"], {"symbol": "TST", "price_usd": 0.03}

    class Rug:
        async def prebuy(self, mint, arm, creator=None):
            return {"ok": True, "refuse": [], "unknown": [], "na": [], "elapsed_ms": 5}

    async def mesh_ok(mint, timeout_s):
        return {"ok": True, "why": "mesh ok"}
    monkeypatch.setattr(p, "_price_sol", price_sol)
    monkeypatch.setattr(p, "_rugchecker", lambda: Rug())
    monkeypatch.setattr(ftp, "mesh_verdict", mesh_ok)
    monkeypatch.setattr(p, "ensure_loop", lambda: None)
    from app.fomo_onchain import market
    monkeypatch.setattr(market, "sol_usd", lambda: 200.0)
    return p, prices, tmp_path


def _rows(path: Path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


async def _drain(p):
    for _ in range(5):
        await asyncio.sleep(0)
        if p._tasks:
            await asyncio.gather(*list(p._tasks))


def _buy(p, i, ms, px, role="cryptohub", mint=MINT):
    return p.tracker(W[i], f"ch:w{i}", role, True, mint, price_sol=px, sol=1.0, sig=f"s{i}{ms}", recv_ms=ms)


def test_fires_at_three_distinct_wallets_and_logs_pumped_feature(kc):
    p, prices, tmp = kc

    async def go():
        t = int(time.time() * 1000)
        assert _buy(p, 0, t, 0.0001) is None
        assert _buy(p, 0, t + 1000, 0.00011) is None            # same wallet twice: still 1 distinct
        assert _buy(p, 1, t + 2000, 0.00012, role="scout") is None
        assert _buy(p, 2, t + 3000, 0.00013) == "entering"
        await _drain(p)
    asyncio.run(go())
    rows = _rows(tmp / "kc.jsonl")
    sig = [r for r in rows if r["kind"] == "signal"]
    assert len(sig) == 1 and sig[0]["confluence_n"] == 3 and sig[0]["paper"] is True
    assert sig[0]["pumped_since_first_alert_pct"] == 30.0          # 0.00013 vs first 0.0001
    assert sig[0]["first_alert_label"] == "ch:w0"
    op = [r for r in rows if r["kind"] == "open"]
    assert len(op) == 1
    b = next(iter(p.book.values()))
    assert {t for t in b["tracks"] if not t.startswith("real_")} == {"live_exit", "follow_wallet", "half2x_c30"}   # existing paper exit ladder
    assert b["meta"]["pumped_since_first_alert_pct_at_entry"] == 50.0       # mark 0.00015
    assert set(W[:3]) <= set(b["meta"]["wallets"])


def test_window_expiry_and_knobs(kc, monkeypatch):
    p, prices, tmp = kc
    t = int(time.time() * 1000)
    _buy(p, 0, t, 0.0001)
    _buy(p, 1, t + 1000, 0.0001)
    assert _buy(p, 2, t + 31 * 60_000, 0.0001) is None            # first two fell out of the 30-min window
    monkeypatch.setenv("KOL_CONFLUENCE_N", "2")
    monkeypatch.setenv("KOL_CONFLUENCE_WINDOW_MIN", "60")

    async def go():
        assert _buy(p, 3, t + 32 * 60_000, 0.0001) == "entering"
        await _drain(p)
    asyncio.run(go())


def test_pumped_feature_is_not_a_filter(kc):
    p, prices, tmp = kc

    async def go():
        t = int(time.time() * 1000)
        _buy(p, 0, t, 0.00001)
        _buy(p, 1, t + 1000, 0.00005)
        assert _buy(p, 2, t + 2000, 0.0001) == "entering"           # +900% still enters (feature only)
        await _drain(p)
    asyncio.run(go())
    assert any(r["kind"] == "open" for r in _rows(tmp / "kc.jsonl"))


def test_roles_outside_config_do_not_count_but_set_first_alert(kc, monkeypatch):
    p, prices, tmp = kc
    monkeypatch.setenv("KOL_CONFLUENCE_ROLES", "cryptohub")
    t = int(time.time() * 1000)
    _buy(p, 0, t, 0.0001, role="scout")
    _buy(p, 1, t + 1, 0.0002)
    assert _buy(p, 2, t + 2, 0.0002) is None
    assert p.first_alert[MINT]["label"] == "ch:w0"


def test_imported_file_is_paper_only_valid_and_deduped():
    p = ROOT / "data/cryptohub_wallets.json"
    if not p.exists():
        pytest.skip("no cryptohub import on this host")
    d = json.loads(p.read_text())
    from app.pump.solana_pda import is_pubkey
    ws = d["wallets"]
    addrs = [w["address"] for w in ws]
    assert len(addrs) == len(set(addrs)) and ws
    for w in ws:
        assert B58.match(w["address"]) and is_pubkey(w["address"]) and not w["address"].lower().startswith("0x")
        assert w["paper_only"] is True and w["live_weight"] == 0.0 and w["role"] == "cryptohub"
        assert "alerts_seen" in w["prior"] and "x_handle" in w["prior"]
    seed = {w.get("address") for w in json.loads((ROOT / "data/scout_wallets.json").read_text())["wallets"]}
    from app.paper_v3 import cfg
    assert not (set(addrs) & (seed | set(cfg()["targets"]) | set(cfg()["watch"])))


def test_roster_role_cryptohub_never_scout_or_confirmer(tmp_path, monkeypatch):
    import app.scout.roster as ro
    f = tmp_path / "ch.json"
    f.write_text(json.dumps({"wallets": [
        {"address": W[0], "label": "a", "paper_only": True, "live_weight": 0.0},
        {"address": W[1], "label": "b", "paper_only": True, "live_weight": 1.0},     # hand-edited weight: refused
        {"address": "0x8230cf5bc3", "label": "evm", "paper_only": True, "live_weight": 0.0}]}))
    monkeypatch.setattr(ro, "CRYPTOHUB_PATH", f)
    r = ro.Roster()
    assert r.add_cryptohub_wallets() == 1
    w = r.by_wallet[W[0]]
    assert w.role == "cryptohub" and w.role not in ro.trigger_roles()
    assert [k for _, _, k in r.sub_keys(w)] == ["pump_uva", "amm_uva"]


def test_engine_routes_cryptohub_away_from_scout_logic(monkeypatch):
    from app.scout import engine as en
    from app.scout.roster import Watched
    e = en.ScoutEngine() if hasattr(en, "ScoutEngine") else type(en.engine)()
    seen = []
    import app.paper_kol_confluence as m
    monkeypatch.setattr(m.paper, "tracker", lambda *a, **k: seen.append(a[2]))
    w = Watched(wallet=W[0], label="ch:a", role="cryptohub", key="k")
    ev = {"mint": MINT, "is_buy": True, "sol_amount": 1e9, "token_amount": 1e12, "price_sol": 1e-6}
    monkeypatch.setattr(e, "_spawn", lambda c: (_ for _ in ()).throw(AssertionError("no arm logic for cryptohub")))
    e.on_trade(w, ev, "sig", int(time.time() * 1000))
    assert seen == ["cryptohub"] and not e.buys.get(MINT)


def test_live_code_never_reads_cryptohub_or_kol_confluence():
    for f in ("app/live/engine.py", "app/live/config.py", "app/jupiter_exec.py", "app/exec_api.py", "app/risk_gate.py"):
        p = ROOT / f
        if p.exists():
            s = p.read_text()
            # v1.35: kol_confluence is now an approved live arm (Oct 16, 2026); live code still never imports the
            # paper module or reads cryptohub wallet data
            assert "paper_kol_confluence" not in s and "cryptohub_wallets.json" not in s, f
    src = (ROOT / "app/paper_kol_confluence.py").read_text().lower()
    assert "import" not in [l for l in src.splitlines() if "signer" in l and l.strip().startswith(("import", "from"))]
    # v1.35: live routing goes ONLY through the live engine (same gates/caps/kill); never the signer or Jupiter directly
    assert "from app.live import jupiter" not in src and "jupiter_exec" not in src


def test_learner_scores_cryptohub_and_only_proposes(tmp_path):
    d = tmp_path / "data"
    (d / "learn").mkdir(parents=True)
    (d / "cryptohub_wallets.json").write_text(json.dumps({"wallets": [{"address": W[0], "label": "zeropnl"}]}))
    now = int(time.time() * 1000)
    lines = []
    for i in range(60):
        pid = f"id{i}"
        lines.append({"ts_ms": now - 1000, "kind": "open", "id": pid, "pumped_since_first_alert_pct_at_entry": 5 + i})
        lines.append({"ts_ms": now - 500, "kind": "close", "id": pid, "track": "half2x_c30", "handles": ["zeropnl"],
                      "pnl_sol": 0.05 if i % 3 else -0.01, "max_multiple": 1.5})
    (d / "kol_confluence.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    env = {**os.environ, "FOMO_DATA_DIR": str(d), "FOMO_COACH_DIR": str(d.parent)}
    r = subprocess.run([sys.executable, str(ROOT / "scripts/learn_daily.py")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    sc = json.loads(next((d / "learn").glob("scores_*.json")).read_text())
    # v1.38: the legacy (optimistic) basis is still scored for comparison ...
    assert sc["legacy"]["kol_confluence"]["by_wallet"]["zeropnl"]["n"] == 60
    assert sc["legacy"]["kol_confluence"]["by_pumped_bucket"]
    lp = [p for p in sc["legacy"]["proposals"] if p["kind"] == "promote_wallet_cryptohub"]
    assert lp and all(p["status"] == "awaiting_andrew" and p["requires_andrew_approval"] for p in lp)
    # ... but decisions are realistic: these rows carry no entry / trader price, so they cannot be priced like a live
    # fill and nothing is proposed for real money on them
    assert sc["basis"] == "realistic" and sc["kol_confluence"]["n"] == 0
    props = _rows(d / "learn/live_proposals.jsonl")
    assert not [p for p in props if p["kind"] == "promote_wallet_cryptohub"]
    assert any("zeropnl" in t for t in sc["proposal_comparison"]["legacy_only"])
    ov = json.loads((d / "learn/paper_overrides.json").read_text())
    assert "zeropnl" not in json.dumps(ov)                         # never written into any applied override
