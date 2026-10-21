import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tripwire as tw  # noqa: E402

W = "Wa11et1111111111111111111111111111111111111"
SIG_KNOWN = "5" + "K" * 87
SIG_FOREIGN = "4" + "F" * 87
SIG_BIG = "3" + "B" * 87
SIG_IN = "2" + "I" * 87


def tx(signer_is_us: bool, delta: int):
    keys = [W, "Other111111111111111111111111111111111111111"] if signer_is_us else \
        ["Other111111111111111111111111111111111111111", W]
    i = keys.index(W)
    pre = [10 ** 10, 10 ** 10]
    post = list(pre)
    post[i] += delta
    return {"transaction": {"message": {"accountKeys": keys, "header": {"numRequiredSignatures": 1}}},
            "meta": {"preBalances": pre, "postBalances": post, "err": None}}


def mk(tmp_path, mode="kill", txs=None, sigs=None):
    ledger = tmp_path / "live_trades.jsonl"
    ledger.write_text(json.dumps({"kind": "buy", "jup": {"signature": SIG_KNOWN}}) + "\n")
    c = {"wallet": W, "rpc_urls": [], "mode": mode, "known_files": [str(ledger)], "signer_log": "",
         "kill_file": str(tmp_path / "KILL"), "halt_file": str(tmp_path / "HALT"), "spool": str(tmp_path / "spool"),
         "state": str(tmp_path / "state.json"), "log": str(tmp_path / "log.jsonl"), "grace_s": 0, "poll_s": 1,
         "max_outflow_lamports": 600_000_000, "refusal_burst": 3, "integrity_paths": [], "integrity_baseline": "",
         "stop_units": [], "label": "test"}
    calls = {"sigs": sigs or []}

    def rpc(method, params):
        if method == "getSignaturesForAddress":
            out, calls["sigs"] = calls["sigs"], []
            return out
        return (txs or {}).get(params[0])
    return tw.Tripwire(c, rpc=rpc), calls


def test_baseline_then_known_is_quiet(tmp_path):
    t, calls = mk(tmp_path, sigs=[{"signature": "old" + "x" * 85}])
    t.run_once()
    assert not (tmp_path / "KILL").exists()
    calls["sigs"] = [{"signature": SIG_KNOWN}]
    t.rpc.__defaults__ if False else None
    t2, c2 = mk(tmp_path, txs={SIG_KNOWN: tx(True, -200_000_000)}, sigs=[{"signature": SIG_KNOWN}])
    t2.state["last_sig"] = "old"
    t2.run_once()
    assert not (tmp_path / "KILL").exists()


def test_foreign_signed_tx_trips_kill_and_alert(tmp_path):
    t, _ = mk(tmp_path, txs={SIG_FOREIGN: tx(True, -50_000)}, sigs=[{"signature": SIG_FOREIGN}])
    t.state["last_sig"] = "old"
    t.run_once()
    assert (tmp_path / "KILL").exists()
    spool = list((tmp_path / "spool").iterdir())
    assert len(spool) == 1 and "unknown_tx" in json.loads(spool[0].read_text())["text"]
    t.trip("unknown_tx", {"signature": SIG_FOREIGN})            # de-duplicated
    assert len(list((tmp_path / "spool").iterdir())) == 1


def test_report_mode_never_kills(tmp_path):
    t, _ = mk(tmp_path, mode="report", txs={SIG_FOREIGN: tx(True, -50_000)}, sigs=[{"signature": SIG_FOREIGN}])
    t.state["last_sig"] = "old"
    t.run_once()
    assert not (tmp_path / "KILL").exists() and list((tmp_path / "spool").iterdir())


def test_incoming_unsigned_is_ignored_but_big_known_outflow_trips(tmp_path):
    t, _ = mk(tmp_path, txs={SIG_IN: tx(False, +5), SIG_KNOWN: tx(True, -900_000_000)},
              sigs=[{"signature": SIG_IN}, {"signature": SIG_KNOWN}])
    t.state["last_sig"] = "old"
    t.run_once()
    rows = [json.loads(l) for l in open(tmp_path / "log.jsonl")]
    kinds = [r.get("kind") for r in rows if r.get("event") == "TRIP"]
    assert kinds == ["big_outflow"]


def test_full_mode_halts_signer(tmp_path):
    t, _ = mk(tmp_path, mode="full", txs={SIG_FOREIGN: tx(True, -1)}, sigs=[{"signature": SIG_FOREIGN}])
    t.state["last_sig"] = "old"
    t.run_once()
    assert (tmp_path / "HALT").exists() and (tmp_path / "KILL").exists()


def test_signer_refusal_burst_and_integrity(tmp_path):
    t, _ = mk(tmp_path, mode="report")
    log = tmp_path / "decisions.jsonl"
    import time
    now = int(time.time() * 1000)
    log.write_text("".join(json.dumps({"ts_ms": now, "ok": False, "reasons": ["program not allowed: X"]}) + "\n" for _ in range(3)))
    t.c["signer_log"] = str(log)
    code = tmp_path / "code.py"
    code.write_text("a")
    t.c["integrity_paths"], t.c["integrity_baseline"] = [str(code)], str(tmp_path / "base.json")
    t.check_signer()                    # burst trips; integrity baseline written
    code.write_text("evil")
    t.check_signer()
    kinds = [json.loads(l).get("kind") for l in open(tmp_path / "log.jsonl") if "TRIP" in l]
    assert "signer_attack" in kinds and "integrity" in kinds


def test_failed_unknown_tx_alerts_without_kill(tmp_path):
    t_ = tx(True, -5000)
    t_["meta"]["err"] = {"InstructionError": [2, {"Custom": 1}]}
    t, _ = mk(tmp_path, txs={SIG_FOREIGN: t_}, sigs=[{"signature": SIG_FOREIGN}])
    t.state["last_sig"] = "old"
    t.run_once()
    assert not (tmp_path / "KILL").exists() and list((tmp_path / "spool").iterdir())


def test_jupiterz_second_signature_counts_as_known(tmp_path):
    t_ = tx(True, -200_000_000)
    t_["transaction"]["signatures"] = [SIG_FOREIGN, SIG_KNOWN]
    t, _ = mk(tmp_path, txs={SIG_FOREIGN: t_}, sigs=[{"signature": SIG_FOREIGN}])
    t.state["last_sig"] = "old"
    t.run_once()
    assert not (tmp_path / "KILL").exists()
