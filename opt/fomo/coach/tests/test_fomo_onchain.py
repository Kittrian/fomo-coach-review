"""v1.12 fomo-onchain source, keyless annotations and the fomo_top_trader paper profile (offline)."""
import asyncio
import time

import pytest

from app.fomo_onchain import FOMO_COSIGNER, PROFILE, SOURCE, USDC_MINT, parser
from app.fomo_onchain import market, strategy as strat_mod, wallets as wallets_mod
from app.pump import exit_signals
from app.pump import paper as paper_mod

W1 = "2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF"
W2 = "5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V"
MINT = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def _tx(wallet, *, fee_payer=FOMO_COSIGNER, signed=True, usdc=(500.0, 400.0), tok=(0.0, 1000.0), mint=MINT):
    keys = [{"pubkey": fee_payer, "signer": True}, {"pubkey": wallet, "signer": signed}]
    def tb(side_usdc, side_tok):
        return [{"owner": wallet, "mint": USDC_MINT, "uiTokenAmount": {"uiAmountString": str(side_usdc), "decimals": 6}},
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"uiAmountString": str(side_tok), "decimals": 6}}]
    return {"blockTime": int(time.time()) - 5, "slot": 1,
            "transaction": {"signatures": ["sig1"], "message": {"accountKeys": keys, "instructions": []}},
            "meta": {"err": None, "preBalances": [0, 10**9], "postBalances": [0, 10**9],
                     "preTokenBalances": tb(usdc[0], tok[0]), "postTokenBalances": tb(usdc[1], tok[1]),
                     "innerInstructions": []}}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_mod, "LOG_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(wallets_mod, "STATE_PATH", tmp_path / "w.json")
    paper_mod.paper.profile_positions.clear()
    strat_mod.strategy.__init__()
    wallets_mod.watchlist.__init__()
    yield
    paper_mod.paper.profile_positions.clear()


# ---------------- parser ----------------
def test_parser_buy_requires_cosigner_and_wallet_signature():
    r = parser.classify(_tx(W1), W1)
    assert r["kind"] == "buy" and r["fomo"] and r["mint"] == MINT
    assert r["usd"] == pytest.approx(100) and r["fill_price_usd"] == pytest.approx(0.1)
    assert parser.classify(_tx(W1, fee_payer=W2), W1)["kind"] == "not_fomo"          # not FOMO-relayed
    assert parser.classify(_tx(W1, signed=False), W1)["kind"] == "not_fomo"          # dust: wallet didn't sign


def test_parser_sell_fraction():
    r = parser.classify(_tx(W1, usdc=(0, 150.0), tok=(1000.0, 250.0)), W1)
    assert r["kind"] == "sell" and r["sold_fraction"] == pytest.approx(0.75) and r["usd"] == pytest.approx(150)


# ---------------- wallet quality ----------------
def test_quality_drop_and_keep():
    wl = wallets_mod.Watchlist()
    a = wallets_mod.Wallet(wallet=W1, handle="a", rank=1, usdc_ata="x")
    b = wallets_mod.Wallet(wallet=W2, handle="b", rank=2, usdc_ata="y")
    wl.apply_sweep(a, [{"kind": "not_fomo", "fomo": False, "wallet_signed": True}])
    assert a.status == "dropped" and a.quality == 0 and "no FOMO co-signed" in a.drop_reason
    fomo = [{**parser.classify(_tx(W2), W2)} for _ in range(3)]
    wl.apply_sweep(b, fomo)
    assert b.status == "kept" and b.cosigned_trades == 3 and 60 < b.quality <= 100


# ---------------- exits ----------------
def test_stop_ratchets_breakeven_then_trail_never_loosens():
    cfg = exit_signals.fomo_config()
    assert exit_signals.fomo_stop(1.0, 1.0, None, cfg)["stop_price"] == pytest.approx(0.80)
    be = exit_signals.fomo_stop(1.0, 1.16, None, cfg)
    assert be["stop_kind"] == "breakeven_stop" and be["stop_price"] == pytest.approx(1.03)
    tr = exit_signals.fomo_stop(1.0, 1.40, be["stop_price"], cfg)
    assert tr["stop_kind"] == "trailing_stop" and tr["stop_price"] == pytest.approx(1.19)
    assert exit_signals.fomo_stop(1.0, 1.0, 1.19, cfg)["stop_price"] == pytest.approx(1.19)   # ratchet holds


def test_follow_trader_cards():
    base = dict(entry=1.0, price=1.1, high=1.1, stop_price=0.8, stop_kind="hard_stop", entry_ms=int(time.time() * 1000))
    full = exit_signals.evaluate_fomo(**base, trader_sells=[{"handle": "a", "sold_fraction": 0.9}])
    assert any(c["kind"] == "follow_trader" and c["action"] == "exit_all" for c in exit_signals.fired(full))
    part = exit_signals.evaluate_fomo(**base, trader_sells=[{"handle": "a", "sold_fraction": 0.3}])
    assert any(c["action"] == "exit_fraction" and c["fraction"] == 0.3 for c in exit_signals.fired(part))
    hit = exit_signals.evaluate_fomo(**{**base, "price": 0.79}, trader_sells=[])
    assert any(c["kind"] == "hard_stop" for c in exit_signals.fired(hit))


# ---------------- strategy ----------------
def _setup_wallets(q1=60.0, q2=60.0):
    for w, h, q in ((W1, "a", q1), (W2, "b", q2)):
        wo = wallets_mod.Wallet(wallet=w, handle=h, rank=1, usdc_ata=w[:5], status="kept")
        wo.quality = q
        wallets_mod.watchlist.wallets[w] = wo


def _mark(monkeypatch, **kw):
    mk = {"price_usd": 0.1, "m5": 1, "h1": 2, "h6": 3, "h24": 5, "liquidity_usd": 50_000, "mcap_usd": 400_000,
          "dex": "pumpswap", "pair_created_ms": int(time.time() * 1000) - 3600_000, "symbol": "TST",
          "at_ms": int(time.time() * 1000), **kw}

    async def marks(mints, client=None, force=False):
        return {m: mk for m in mints}
    async def auth(mint, url, client=None):
        return {"ok": True, "mint_authority": None, "freeze_authority": None}
    monkeypatch.setattr(market, "marks", marks)
    monkeypatch.setattr(market, "sol_usd", lambda: 200.0)
    monkeypatch.setattr(market, "mint_authorities", auth)
    return mk


class _Ev:
    def __init__(self, w, h, usd=200.0, px=0.1):
        self.source, self.token_address, self.trader_id, self.trader_handle = SOURCE, MINT, w, h
        self.usd_amount, self.occurred_at_ms = usd, int(time.time() * 1000)
        self.meta = {"wallet": w, "fill_price_usd": px}


def _feed(ev):
    s = strat_mod.strategy
    s.on_ingested_buy(ev)            # no running loop → decide() not spawned; call directly
    return asyncio.run(s.decide(MINT))


def test_convergence_enters_in_separate_book(monkeypatch):
    _setup_wallets()
    _mark(monkeypatch)
    fluffy_before = dict(paper_mod.paper.positions)
    d1 = _feed(_Ev(W1, "a"))
    assert d1["decision"] == "watching"                               # solo, not early enough → wait
    d2 = _feed(_Ev(W2, "b"))
    assert d2["decision"] == "enter"
    pos = paper_mod.paper.profile_open_for(PROFILE, MINT)
    assert pos and pos.entry_sol == pytest.approx(0.10) and pos.profile == PROFILE
    assert paper_mod.paper.open_for(MINT) is None and paper_mod.paper.positions == fluffy_before   # Fluffy untouched
    assert pos.stop_price_sol == pytest.approx(pos.entry_price_sol * 0.8)


def test_anti_chase_skips_big_runup(monkeypatch):
    _setup_wallets()
    _mark(monkeypatch, h1=45)
    _feed(_Ev(W1, "a"))
    d = _feed(_Ev(W2, "b"))
    assert d["decision"] == "skip" and "anti-chase" in d["reasons"][0]
    assert paper_mod.paper.profile_open_for(PROFILE, MINT) is None


def test_runup_flag_blocks_solo_and_late_price_skips(monkeypatch):
    _setup_wallets(q1=90)
    _mark(monkeypatch, h1=9, dex="pumpfun")
    d = _feed(_Ev(W1, "a"))
    assert d["decision"] == "watching" and any("run-up" in r for r in d["reasons"])
    strat_mod.strategy.__init__()
    _mark(monkeypatch, price_usd=0.13, dex="pumpfun")
    d = _feed(_Ev(W1, "a", px=0.1))
    assert d["decision"] == "skip" and any("too late" in r for r in d["reasons"])


def test_early_solo_high_quality_enters(monkeypatch):
    _setup_wallets(q1=90)
    _mark(monkeypatch, dex="pumpfun")
    assert _feed(_Ev(W1, "a"))["decision"] == "enter"


def test_follow_trader_and_stop_exits(monkeypatch):
    _setup_wallets(q1=90)
    mk = _mark(monkeypatch, dex="pumpfun")
    _feed(_Ev(W1, "a"))
    pos = paper_mod.paper.profile_open_for(PROFILE, MINT)
    pos.meta["pending_trader_sells"] = [{"handle": "a", "wallet": W1, "sold_fraction": 1.0}]
    asyncio.run(strat_mod.strategy.manage_position(pos))
    assert pos.status == "closed" and pos.exit_reason == "follow_trader"
    # stop path: new position, mark gaps below hard stop → fills at the (worse) mark, not at the stop
    paper_mod.paper.profile_positions.clear(); strat_mod.strategy.closed_at.clear()
    strat_mod.strategy.candidates.clear()
    _feed(_Ev(W1, "a"))
    pos = paper_mod.paper.profile_open_for(PROFILE, MINT)
    mk["price_usd"] = 0.06
    asyncio.run(strat_mod.strategy.manage_position(pos))
    assert pos.status == "closed" and pos.exit_reason == "hard_stop"
    assert pos.pnl()["pnl_pct"] < -20          # gap: worse than the −20% stop → no-loss NOT guaranteed


def test_trader_sell_on_fluffy_mint_is_advisory_only(monkeypatch):
    fl = paper_mod.PaperPosition(mint=MINT, source="fluffy/telegram", entry_sol=0.4, entry_lamports=4 * 10**8,
                                 tokens_initial=10, entry_price_sol=1.0, entry_venue="curve", entry_ms=0, tokens_left=10)
    monkeypatch.setitem(paper_mod.paper.positions, MINT, fl)
    out = strat_mod.strategy.on_trader_sell(wallet=W1, handle="a", mint=MINT, sold_fraction=1.0, usd=1.0,
                                            signature="s", block_ms=0)
    assert out["applied_to"] == ["fluffy_advisory"] and fl.status == "open" and fl.external_signals


# ---------------- keyless ----------------
def test_keyless_dedup_annotation_only_and_join():
    from app.fomo_onchain.keyless import Keyless
    from app.state import state
    k = Keyless()
    n_before = len(state.buys)
    wallets_mod.watchlist.wallets[W1] = wallets_mod.Wallet(wallet=W1, handle="alice", rank=1, usdc_ata="x", status="kept")
    now = int(time.time() * 1000)
    trades = [{"mint": MINT, "kind": "buy", "block_ms": now - 30_000, "usd": 100.0, "wallet": W1, "signature": "s"}]
    msg = {"alertType": "buy", "eventId": "e1", "trader": "@alice", "tokenAddress": MINT, "usdValue": 105,
           "ts": now - 5_000, "chain": "solana", "text": "alice bought TST"}
    a = k.on_message(dict(msg))
    assert a is not None and k.on_message(dict(msg)) is None and k.stats["dups"] == 1
    assert len(state.buys) == n_before                     # never an entry trigger / ingest
    assert strat_mod.strategy.theses[MINT][0]["handle"] == "@alice"
    assert k.join(a, trades)["status"] == "confirmed" and wallets_mod.watchlist.get(W1).handle_confirmed
    assert k.on_message({"type": "welcome", "delaySeconds": 60}) is None and k.delay_s == 60
    # copy-cluster: another handle buying the same mint/size is only a weak candidate, never flips the seed
    other = {**a, "event_id": "e2", "handle": "@bob"}
    assert k.join(other, trades)["status"] == "candidate" and wallets_mod.watchlist.get(W1).handle_confirmed


# ---------------- low-priority ingest ----------------
def test_low_priority_ingest_skips_fast_lane_and_signals(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.pump import fast_lane
    from app.scoring import compute_signals

    def boom(*a, **k):
        raise AssertionError("fast lane must not run for fomo-onchain")
    monkeypatch.setattr(fast_lane, "schedule", boom)
    monkeypatch.setattr("app.ingest.buy.schedule_enrichment", boom)
    monkeypatch.setattr(strat_mod.strategy, "_spawn", lambda coro: coro.close())   # no network in tests
    r = TestClient(app).post("/api/ingest/buy", json={"trader_id": W1, "token_address": MINT, "source": SOURCE,
                                                       "usd_amount": 100.0})
    assert r.status_code == 200 and r.json()["low_priority"] and r.json()["fast_lane_scheduled"] is False
    assert all(s.token_address != MINT for s in compute_signals(min_score=0))


def test_dust_filtered_from_logs_without_rpc():
    from app.fomo_onchain.source import route_in_logs
    assert route_in_logs(["Program DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH invoke [1]"])
    assert route_in_logs(["Program ComputeBudget111111111111111111111111111111 invoke [1]",
                          "Program 99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2 invoke [1]"])
    assert not route_in_logs(["Program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL invoke [1]",
                              "Program 11111111111111111111111111111111 invoke [1]"])
