from tests.test_v119 import live_env, MINT  # noqa: F401
from app.live.engine import LiveEngine
def test_fluffy_exempt(live_env, monkeypatch):
    monkeypatch.setenv("LIVE_TRADE_CAP_EXEMPT_ARMS", "fluffy_pump_dump")
    monkeypatch.setenv("LIVE_SIZE_SOL_FLUFFY_PUMP_DUMP", "0.40")
    e = LiveEngine(); e.day()["trades"] = 10; e.day()["realized_pnl_sol"] = -0.005
    ok, why, size = e.check_caps("fluffy_pump_dump", MINT); assert ok, why; assert size == 0.40
    ok, why, _ = e.check_caps("scout_solo", MINT); assert not ok and any("max trades" in w for w in why)
    ok, why, _ = e.check_caps("zyrox", MINT); assert not ok
    e.day()["realized_pnl_sol"] = -0.7
    ok, why, _ = e.check_caps("fluffy_pump_dump", MINT); assert not ok and any("loss limit" in w for w in why)
