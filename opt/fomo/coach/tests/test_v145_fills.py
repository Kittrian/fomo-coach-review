"""v1.45 better fills: price bound, buy tip floor, whale CU match."""
import base64
import struct

from app.live import fees, pumpdirect, whalefee

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def test_b58_roundtrip():
    d = bytes([3]) + struct.pack("<Q", 1_234_567)
    assert whalefee._b58decode(b58(d)) == d


def test_cu_price_json_parsed_and_raw():
    data = b58(bytes([3]) + struct.pack("<Q", 777_000))
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "W"}], "instructions": [
        {"programId": whalefee.COMPUTE_BUDGET, "accounts": [], "data": b58(bytes([2]) + struct.pack("<I", 90000))},
        {"programId": whalefee.COMPUTE_BUDGET, "accounts": [], "data": data}]}}}
    assert whalefee.cu_price_from_tx(tx) == 777_000
    tx2 = {"transaction": {"message": {"accountKeys": ["W", whalefee.COMPUTE_BUDGET],
                                       "instructions": [{"programIdIndex": 1, "data": data}]}}}
    assert whalefee.cu_price_from_tx(tx2) == 777_000
    assert whalefee.cu_price_from_tx({}) is None
    assert whalefee.cu_price_from_tx({"transaction": {"message": {"instructions": [{"programId": "x", "data": "zz"}]}}}) is None


def test_cache_age():
    whalefee.note("M", 5)
    assert whalefee.get("M") == 5
    assert whalefee.get("M", max_age_s=-1) is None
    whalefee.note("M", 0)
    assert whalefee.get("M") == 5


def test_price_bound_math():
    # 0.1 SOL at whale px 1e-7 SOL/token, chase 5 + slack 0.5 → max px 1.055e-7 → 947,867 tokens (6 dp)
    t = pumpdirect.price_bound_min_tokens(100_000_000, 1e-7, 5, 0.5)
    assert abs(t / 1e6 - 0.1 / 1.055e-7) / (0.1 / 1.055e-7) < 1e-6
    assert pumpdirect.price_bound_min_tokens(100_000_000, None, 5, 0.5) == 0
    assert pumpdirect.price_bound_min_tokens(100_000_000, 1e-7, 0, 0.5) == 0


def _curve():
    return {"virtual_token_reserves": 1_000_000_000_000_000, "virtual_quote_reserves": 30_000_000_000,
            "real_token_reserves": 793_000_000_000_000, "complete": False, "quote_is_sol": True}


def test_build_uses_tighter_floor(monkeypatch):
    st = {"curve": _curve(), "token_program": pumpdirect.TOKEN_PROGRAM, "fee_config": None}
    user = "CoDjAcZF4sJnFKf1hBvVLGQ1gRr1RxR6U1wy7K3xseE6"
    mint = "So11111111111111111111111111111111111111112"
    b0 = pumpdirect.build("buy", mint, user, 100_000_000, st, slippage_pct=0.15, cu_limit=90_000, priority_lamports=50_000)
    hi = int(b0["expected_out"] * 0.97)
    b1 = pumpdirect.build("buy", mint, user, 100_000_000, st, slippage_pct=0.15, cu_limit=90_000,
                          priority_lamports=50_000, price_floor=hi)
    assert b1["min_out"] == hi > b0["min_out"]
    b2 = pumpdirect.build("buy", mint, user, 100_000_000, st, slippage_pct=0.15, cu_limit=90_000,
                          priority_lamports=50_000, price_floor=1)
    assert b2["min_out"] == b0["min_out"]
    data = bytes(b1["ixs"][-1].data)
    assert struct.unpack("<QQ", data[8:24]) == (100_000_000, hi)


def test_buy_tip_floor(monkeypatch):
    monkeypatch.setenv("LIVE_SENDER_BUY_TIP_MIN_LAMPORTS", "1000000")
    monkeypatch.setenv("LIVE_SENDER_BUY_TIP_MAX_LAMPORTS", "1000000")
    monkeypatch.setenv("LIVE_SENDER_TIP_MAX_LAMPORTS", "250000")
    monkeypatch.setenv("LIVE_FEE_BUDGET_BPS", "50")
    monkeypatch.setattr(fees, "kick", lambda: None)
    monkeypatch.setattr(fees, "_log", lambda r: None)
    b = fees.plan(100_000_000, side="buy", route="pump_direct", signer_tip_cap=1_000_000, want_tip=True)
    assert b["tip_lamports"] == 1_000_000 and b["priority_lamports"] >= 50_000 and b["dual_route"]
    s = fees.plan(100_000_000, side="sell", route="pump_direct", signer_tip_cap=1_000_000, want_tip=True)
    assert s["tip_lamports"] <= 250_000
    old = fees.plan(100_000_000, side="buy", route="pump_direct", signer_tip_cap=250_000, want_tip=True)
    assert old["tip_lamports"] == 250_000          # signer not updated → never above its cap
    monkeypatch.setenv("LIVE_SENDER_BUY_TIP_MIN_LAMPORTS", "0")
    monkeypatch.setenv("LIVE_SENDER_BUY_TIP_MAX_LAMPORTS", "0")
    off = fees.plan(100_000_000, side="buy", route="pump_direct", signer_tip_cap=1_000_000, want_tip=True)
    assert off["tip_lamports"] <= 250_000


def test_cfg_flags(monkeypatch):
    for k in ("LIVE_DIRECT_PRICE_BOUND", "LIVE_MATCH_WHALE_CU"):
        monkeypatch.delenv(k, raising=False)
    c = pumpdirect.cfg()
    assert c["price_bound"] is False and c["whale_cu"] is False and c["whale_cu_max_micro"] == 2_000_000
