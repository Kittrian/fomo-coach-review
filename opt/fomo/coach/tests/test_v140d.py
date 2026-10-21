"""v1.40d: legacy-age tag in the realistic book + txsub decode shadow/auto/live with fetch fallback."""
import asyncio
import json


def _tx(wallet, mint, sol_delta=-100_000_000, tok=5_000_000):
    return {"slot": 1, "transaction": {"message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": "Other"}]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [1_000_000_000, 0],
                     "postBalances": [1_000_000_000 + sol_delta - 5000, 0],
                     "preTokenBalances": [], "postTokenBalances": [
                         {"accountIndex": 1, "mint": mint, "owner": wallet,
                          "uiTokenAmount": {"amount": str(tok), "decimals": 6}}],
                     "logMessages": []}}


def test_real_tag_before_and_after_open(tmp_path, monkeypatch):
    from app.scout import real as rmod
    r = rmod.ScoutReal() if hasattr(rmod, "ScoutReal") else type(rmod.real)()
    logged = []
    monkeypatch.setattr(r, "_log", lambda kind, **kw: logged.append((kind, kw)))

    class P:
        mint = "M1"; status = "open"
    r.pos["p1"] = P()
    r.meta["p1"] = {"arm": "scout_solo", "signal_id": "s1"}
    r.tag("M1", "age_unknown_legacy_gt3000tx", "s1")
    r.tag("M1", "age_unknown_legacy_gt3000tx", "s1")
    assert [k for k, _ in logged] == ["real_tag"] and r.meta["p1"]["tags"] == ["age_unknown_legacy_gt3000tx"]


def test_txsub_shadow_then_auto_promote(monkeypatch):
    from app.scout import source as srcmod
    s = srcmod.source
    monkeypatch.setenv("SCOUT_TXSUB_DECODE", "auto")
    monkeypatch.setenv("SCOUT_TXSUB_PROMOTE_N", "2")
    monkeypatch.setenv("SCOUT_TXSUB_LOG", "/dev/null")
    monkeypatch.setenv("SCOUT_TXSUB_WAIT_MS", "20")
    s.txsub.update(promoted=False, demoted=False, compares=0, match=0, mismatch=0, used=0)
    calls = []

    async def fake_hot(method, params, timeout=4.0):
        calls.append(params[0])
        return _tx("W", "MINTX")
    monkeypatch.setattr(s, "call_hot", fake_hot)
    emitted = []
    monkeypatch.setattr(s, "_emit", lambda w, ev, sig, recv_ms, path: emitted.append(sig))

    class W:
        wallet = "W"
    for i in range(3):
        sig = f"sig{i}"
        s.txsub_set = {"W"}
        s.txcache[sig] = (0, _tx("W", "MINTX"))
        s._resolving.discard(sig)
        asyncio.run(s._resolve_sig(W(), sig, 0, path="logs"))
    # 2 shadow compares (fetched), then promoted -> the 3rd uses the stream without getTransaction
    assert s.txsub["match"] == 2 and s.txsub["promoted"] and s.txsub["used"] == 1
    assert calls == ["sig0", "sig1"]


def test_txsub_miss_falls_back(monkeypatch):
    from app.scout import source as srcmod
    s = srcmod.source
    monkeypatch.setenv("SCOUT_TXSUB_DECODE", "live")
    monkeypatch.setenv("SCOUT_TXSUB_WAIT_MS", "10")
    calls = []

    async def fake_hot(method, params, timeout=4.0):
        calls.append(params[0])
        return _tx("W", "MINTY")
    monkeypatch.setattr(s, "call_hot", fake_hot)
    monkeypatch.setattr(s, "_emit", lambda *a, **k: None)

    class W:
        wallet = "W"
    s.txsub_set = {"W"}
    asyncio.run(s._resolve_sig(W(), "nosuchsig", 0, path="logs"))
    assert calls == ["nosuchsig"]


def test_mismatch_blocks_promotion(monkeypatch):
    from app.scout import source as srcmod
    s = srcmod.source
    monkeypatch.setenv("SCOUT_TXSUB_DECODE", "auto")
    monkeypatch.setenv("SCOUT_TXSUB_PROMOTE_N", "1")
    monkeypatch.setenv("SCOUT_TXSUB_LOG", "/dev/null")
    s.txsub.update(promoted=False, demoted=False, compares=0, match=0, mismatch=0, used=0)
    s._txsub_compare("W", "a", _tx("W", "MINTZ", tok=1), _tx("W", "MINTZ", tok=2), 100)
    s._txsub_compare("W", "b", _tx("W", "MINTZ"), _tx("W", "MINTZ"), 100)
    assert s.txsub["mismatch"] == 1 and not s.txsub["promoted"]
