"""v1.17.4 Day Recap wallet panel: read-only, public addresses only, public RPC (never Helius), no network in tests."""
import asyncio
import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app import recap, recap_wallet as rw

DAY = date(2026, 10, 8)
S0 = int(datetime(2026, 10, 8, 0, 0, 0, tzinfo=recap.TZ).timestamp())
ADDR = "CoDjxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxseE6"
WATCH = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
MINT = "CBoJBdjSLZuBQLXLLB6V7qXiwXfV4eMryQnUUoBpKkMn"
DUST = "DUSTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"
FAKE_KEYPAIR = "/Users/someone/secret-folder/trading-keypair.json"
FAKE_SECRET = "REDACTED"


def _tx(slot, bt, pre_w, post_w, fee=5000, others=((0, 0),), tok=None, err=None, payer=True):
    keys = [ADDR, "Other1111111111111111111111111111111111111"] if payer else ["Payer111111111111111111111111111111111111111", ADDR]
    pre = [pre_w, 0] if payer else [10 ** 9, pre_w]
    post = [post_w, 0] if payer else [10 ** 9 - fee, post_w]
    for a, b in others:
        keys.append("Acct" + str(len(keys)) + "x" * 36)
        pre.append(a)
        post.append(b)
    pre_t, post_t = [], []
    if tok:
        m, a, b = tok
        pre_t.append({"owner": ADDR, "mint": m, "uiTokenAmount": {"uiAmount": a}})
        post_t.append({"owner": ADDR, "mint": m, "uiTokenAmount": {"uiAmount": b}})
    return {"slot": slot, "blockTime": bt, "transaction": {"message": {"accountKeys": keys}},
            "meta": {"err": err, "fee": fee, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": pre_t, "postTokenBalances": post_t}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_LEDGER_PATH", str(tmp_path / "live_trades.jsonl"))
    monkeypatch.setenv("RECAP_NET_ENABLED", "false")
    monkeypatch.setenv("SOLANA_WALLET", ADDR)
    monkeypatch.setenv("SOLANA_KEYPAIR_PATH", FAKE_KEYPAIR)
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", FAKE_SECRET)
    monkeypatch.setenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=" + FAKE_SECRET)
    for k in ("RECAP_WATCH_WALLETS", "RECAP_WALLET_RPC_URLS", "RECAP_WALLET_CACHE_PATH", "RECAP_WALLET_SNAPSHOTS_PATH",
              "RECAP_CACHE_PATH", "JUPITER_USER_PUBLIC_KEY", "RECAP_WALLET_SNAPSHOTS_FORCE"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "live_trades.jsonl").write_text(json.dumps(
        {"ts_ms": (S0 + 40_000) * 1000, "kind": "buy", "live": True, "id": "b1", "mint": MINT, "buy_sig": "SIGBUY"}) + "\n")
    fresh = rw.WalletBook()
    fresh._loaded = True
    monkeypatch.setattr(rw, "book", fresh)
    return tmp_path


def _seed(b):
    lsigs = {"SIGBUY": ("buy", MINT)}
    rows = [
        ("SIGPREV", _tx(100, S0 - 3600, 2_000_000_000, 1_500_000_000, others=())),                       # yesterday: transfer out
        ("SIGBUY", _tx(200, S0 + 40_000, 1_500_000_000, 1_397_950_000, others=((0, 2_039_280),),
                       tok=(MINT, 0, 1000.0))),                                                           # buy + new token acct
        ("SIGSELL", _tx(300, S0 + 41_000, 1_397_950_000, 1_502_000_000, tok=(MINT, 1000.0, 0))),         # sell (from deltas)
        ("SIGCLOSE", _tx(400, S0 + 42_000, 1_502_000_000, 1_504_034_280, others=((2_039_280, 0),))),     # close empty acct
        ("SIGIN", _tx(500, S0 + 43_000, 1_504_034_280, 1_504_035_280, fee=5000, others=(), payer=False)),  # tiny transfer in
        ("SIGFAIL", _tx(600, S0 + 44_000, 1_504_035_280, 1_504_030_280, others=(), err={"x": 1})),
    ]
    for sig, tx in rows:
        s = b._summarize(ADDR, sig, tx, lsigs)
        b.tx[f"{ADDR}:{sig}"] = s
        b.sigs.setdefault(ADDR, {})[sig] = {"slot": tx["slot"], "blockTime": tx["blockTime"], "err": tx["meta"]["err"] is not None}
    b.state[ADDR] = {"lamports": 1_504_030_280, "refreshed_ms": 1, "errors": [], "accounts": [
        {"account": "A1", "program": "spl", "mint": MINT, "amount": 500.0, "decimals": 6, "rent_lamports": 2_039_280},
        {"account": "A2", "program": "t22", "mint": DUST, "amount": 3.0, "decimals": 6, "rent_lamports": 2_074_080},
        {"account": "A3", "program": "spl", "mint": "EMPTYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "amount": 0.0, "decimals": 6,
         "rent_lamports": 2_039_280},
    ]}
    b.prices[MINT] = (9e12, {"symbol": "CBOJ", "price_usd": 0.01, "liquidity_usd": 5000.0})
    b.prices[DUST] = (9e12, {"symbol": "DST", "price_usd": 0.0001, "liquidity_usd": 10.0})
    b.sol_usd = (9e12, 100.0)


def test_wallets_parsing_and_validation(env, monkeypatch):
    monkeypatch.setenv("RECAP_WATCH_WALLETS", f" Fomo={WATCH}, not-an-address, {ADDR}, Bad=0OIl ")
    ws = rw.wallets()
    assert [w["role"] for w in ws] == ["trading", "watch"]
    assert ws[1] == {"address": WATCH, "label": "Fomo", "role": "watch"}
    monkeypatch.setenv("SOLANA_WALLET", "")
    monkeypatch.setenv("JUPITER_USER_PUBLIC_KEY", ADDR)
    assert rw.wallets()[0]["address"] == ADDR


def test_rpc_urls_never_helius(env, monkeypatch):
    assert all("helius" not in u for u in rw.rpc_urls())
    assert any("publicnode" in u for u in rw.rpc_urls())
    monkeypatch.setenv("RECAP_WALLET_RPC_URLS", "https://mainnet.helius-rpc.com/?api-key=x, https://rpc.example/x?api-key=y, https://ok.example")
    assert rw.rpc_urls() == ["https://ok.example"]


def test_summarize_types_fees_rent(env):
    _seed(rw.book)
    t = {k.split(":")[1]: v for k, v in rw.book.tx.items()}
    assert t["SIGBUY"]["type"] == "buy" and t["SIGBUY"]["mint"] == MINT and t["SIGBUY"]["rent_paid"] == 2_039_280
    assert t["SIGSELL"]["type"] == "sell"
    assert t["SIGCLOSE"]["type"] == "close accounts" and t["SIGCLOSE"]["rent_recovered"] == 2_039_280
    assert t["SIGIN"]["type"] == "transfer in" and t["SIGIN"]["fee"] == 0
    assert t["SIGFAIL"]["type"] == "failed"
    assert t["SIGPREV"]["type"] == "transfer out"


def test_build_view_day_change_tokens_dust(env):
    _seed(rw.book)
    s, e = recap.day_bounds(DAY)
    v = rw.build_wallet_view(rw.wallets()[0], s, e, False, {})
    d = v["day"]
    assert d["start_sol"] == 1.5 and d["end_sol"] == 1.50403028 and d["change_sol"] == pytest.approx(0.00403028)
    assert d["tx_count"] == 5 and d["fees_sol"] == pytest.approx(0.00002)
    assert d["rent_paid_sol"] == pytest.approx(0.00203928) and d["rent_recovered_sol"] == pytest.approx(0.00203928)
    assert d["transfers_in_sol"] == pytest.approx(0.000001)
    assert len(d["series"]) >= 5
    assert [t["symbol"] for t in v["tokens"]] == ["CBOJ", "DST"]
    assert v["tokens"][0]["usd"] == 5.0 and not v["tokens"][0]["dust"] and v["tokens"][1]["dust"]
    assert v["dust_count"] == 1 and v["rent"]["empty_accounts"] == 1 and v["rent"]["closable_empty_sol"] == pytest.approx(0.00203928)
    assert v["sol_usd_value"] == pytest.approx(150.4, abs=0.01) and v["total_usd"] == pytest.approx(155.40, abs=0.01)
    assert len(v["recent"]) == 6 and v["recent"][0]["sig"] == "SIGFAIL"
    assert v["short"] == "CoDj…seE6" and v["solscan"].endswith(ADDR)


def test_endpoint_soft_fail_offline_and_no_secrets(env):
    from app.main import app
    _seed(rw.book)
    with TestClient(app) as c:
        r = c.get("/api/recap/wallet?date=2026-10-08")
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] and j["read_only"] and j["wallets"][0]["day"]["start_sol"] == 1.5
        page = c.get("/recap").text
        rec = c.get("/api/recap?date=2026-10-08").text
        empty = c.get("/api/recap/wallet?date=2026-09-01").json()
        assert empty["wallets"][0]["sol"] is not None
    blob = r.text + page + rec
    for bad in (FAKE_KEYPAIR, "trading-keypair", FAKE_SECRET, "helius-rpc", "SOLANA_KEYPAIR_PATH", "api-key="):
        assert bad not in blob
    assert 'id="walletCard"' in page and "/api/recap/wallet" in page


def test_no_wallet_configured(env, monkeypatch):
    monkeypatch.setenv("SOLANA_WALLET", "")
    j = asyncio.run(rw.get_wallet_recap(DAY))
    assert j["ok"] and j["wallets"] == [] and "SOLANA_WALLET" in j["note"]


def test_snapshots_off_under_pytest_and_written_when_forced(env, monkeypatch):
    assert rw.book.snapshots_enabled() is False
    monkeypatch.setenv("RECAP_WALLET_SNAPSHOTS_FORCE", "true")
    assert rw.book.snapshots_enabled() is False          # still off while RECAP_NET_ENABLED=false
    monkeypatch.setenv("RECAP_NET_ENABLED", "true")
    assert rw.book.snapshots_enabled() is True
    monkeypatch.setenv("RECAP_NET_ENABLED", "false")
    p = env / "wallet_snapshots.jsonl"
    p.write_text(json.dumps({"ts_ms": (S0 + 60) * 1000, "wallet": ADDR, "role": "trading", "sol": 1.6}) + "\n")
    s, e = recap.day_bounds(DAY)
    v = rw.build_wallet_view(rw.wallets()[0], s, e, False, {})        # no tx history → first snapshot
    assert v["day"]["start_sol"] == 1.6 and v["day"]["source"] == "first snapshot of the day" and v["day"]["snapshots"] == 1


def test_ensure_noop_when_offline(env):
    assert rw.book.ensure(ADDR, S0, {}, 45) is None
