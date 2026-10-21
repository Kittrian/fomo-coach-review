"""v1.17.2 item 4: SCOUT_WS_COMMITMENT=processed trial — every 'enter' signal's scout tx is re-checked
(landed / failed_onchain / dropped) and logged; a live fill on a bad trigger is counted. No network."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.scout import engine as se
from app.scout import source as src


@pytest.fixture
def trial(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOUT_WS_COMMITMENT", "processed")
    monkeypatch.setenv("SCOUT_TRIAL_LOG", str(tmp_path / "trial.jsonl"))
    for k in ("SCOUT_TRIAL_CHECK1_S", "SCOUT_TRIAL_CHECK2_S", "SCOUT_TRIAL_CHECK3_S"):
        monkeypatch.setenv(k, "0")
    return tmp_path


def _rows(p):
    return [json.loads(x) for x in p.read_text().splitlines()]


def _fake_source(monkeypatch, statuses, tx=None):
    calls = []

    async def call(method, params, **kw):
        calls.append(method)
        if method == "getSignatureStatuses":
            return {"value": [statuses.pop(0) if statuses else None]}
        return tx
    monkeypatch.setattr(src.source, "call", call)
    return calls


def test_landed(trial, monkeypatch):
    _fake_source(monkeypatch, [{"err": None, "confirmationStatus": "confirmed"}])
    e = se.ScoutEngine()
    v = asyncio.run(e._verify_trigger("scout_solo", "M1", "SIG1", "buy"))
    assert v == "landed" and e.stats["trial_landed"] == 1 and _rows(trial / "trial.jsonl")[0]["verdict"] == "landed"


def test_dropped_and_live_fill_counted(trial, monkeypatch):
    calls = _fake_source(monkeypatch, [None, None], tx=None)
    from app.live.engine import engine as live_engine

    class P:
        mint = "M2"
    monkeypatch.setitem(live_engine.positions, "pid-x", P())
    e = se.ScoutEngine()
    v = asyncio.run(e._verify_trigger("scout_solo", "M2", "SIG2", "buy"))
    assert v == "dropped" and calls == ["getSignatureStatuses", "getSignatureStatuses", "getTransaction"]
    assert e.stats["trial_bad_trigger_live_buys"] == 1
    r = _rows(trial / "trial.jsonl")[0]
    assert r["verdict"] == "dropped" and r["live_filled"] is True


def test_failed_onchain_found_late(trial, monkeypatch):
    _fake_source(monkeypatch, [None, None], tx={"meta": {"err": {"InstructionError": [0, "x"]}}})
    e = se.ScoutEngine()
    assert asyncio.run(e._verify_trigger("scout_solo", "M3", "SIG3", "refused")) == "failed_onchain"
    assert e.stats["trial_bad_trigger_live_buys"] == 0                 # no live buy → not counted as one


def test_only_tracked_when_processed(monkeypatch):
    monkeypatch.setenv("SCOUT_WS_COMMITMENT", "confirmed")
    e = se.ScoutEngine()
    spawned = []
    monkeypatch.setattr(e, "_spawn", lambda c: (spawned.append(c), c.close()))
    e._maybe_verify_trigger("scout_solo", "M", "SIG", "buy")
    assert not spawned
    monkeypatch.setenv("SCOUT_WS_COMMITMENT", "processed")
    e._maybe_verify_trigger("scout_solo", "M", "SIG", "buy")
    e._maybe_verify_trigger("scout_solo", "M", None, "buy")            # no sig → nothing
    assert len(spawned) == 1 and e.stats["trial_tracked"] == 1


def test_processed_subscription_message(monkeypatch):
    monkeypatch.setenv("SCOUT_WS_COMMITMENT", "processed")
    s = src.ScoutSource()
    s.sub_method["K"] = "logs"
    assert s._sub_msg(1, "K")["params"][1]["commitment"] == "processed"
