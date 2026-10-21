import os
import shutil
import sys
import tempfile
from pathlib import Path

# Never open network sources from the test suite (v1.12 fomo-onchain / fomoapi keyless).
os.environ.setdefault("FOMO_ONCHAIN_ENABLED", "false")
os.environ.setdefault("FOMOAPI_KEYLESS_ENABLED", "false")
os.environ.setdefault("SCOUT_ENABLED", "false")
os.environ.setdefault("PAPER_REALISM_ONCHAIN_ENABLED", "false")   # v1.37: no on-chain entry reads / latency waits
os.environ.setdefault("PAPER_REALISM_SNAPSHOT_ENABLED", "false")   # v1.38: no learner snapshot writes from tests

# v1.14 live trading: tests never touch the real token file, state, kill file or keypair, and flags are forced off
# (individual tests opt in with monkeypatch + a mocked jupiter module).
_LIVE_TMP = tempfile.mkdtemp(prefix="coach-live-test-")
for _k, _v in {
    "JUPITER_EXEC_ENABLED": "false",
    "LIVE_AUTOTRADE_ENABLED": "false",
    "LIVE_ARMS": "",
    "LIVE_API_TOKEN_PATH": os.path.join(_LIVE_TMP, "cfg", "live-api-token"),
    "LIVE_STATE_PATH": os.path.join(_LIVE_TMP, "live_state.json"),
    "LIVE_LEDGER_PATH": os.path.join(_LIVE_TMP, "live_trades.jsonl"),
    "LIVE_KILL_FILE": os.path.join(_LIVE_TMP, "KILL"),
    "SOLANA_KEYPAIR_PATH": os.path.join(_LIVE_TMP, "no-keypair.json"),
    "INGEST_SEEN_PATH": os.path.join(_LIVE_TMP, "ingest_seen.json"),
}.items():
    os.environ[_k] = _v

# v1.16 data hygiene: the suite NEVER writes into the real data/ directory.
#  1. COACH_DATA_DIR points every module's default data path at a temp dir (read-only seed files copied in);
#  2. every per-file override is pinned to the temp dir too (belt and braces);
#  3. an audit hook makes any write/rename into the real data/ dir raise immediately, so a new writer that
#     forgets to use app.paths fails the test that triggered it instead of polluting live data.
_REAL_DATA = (Path(__file__).resolve().parents[1] / "data").resolve()
_DATA_TMP = Path(tempfile.mkdtemp(prefix="coach-data-test-"))
for _seed in ("scout_wallets.json", "fomo_top100_wallets.json"):
    if (_REAL_DATA / _seed).exists():
        shutil.copyfile(_REAL_DATA / _seed, _DATA_TMP / _seed)
os.environ["COACH_DATA_DIR"] = str(_DATA_TMP)
for _k, _name in {
    "PAPER_LOG_PATH": "paper_trades.jsonl", "SCOUT_LOG_PATH": "scout_trades.jsonl",
    "SCOUT_FILTERS_PATH": "scout_filters.json", "SCOUT_WALLETS_PATH": "scout_wallets.json",
    "FOMO_ONCHAIN_STATE_PATH": "fomo_onchain_wallets.json", "FOMO_ONCHAIN_SEED_PATH": "fomo_top100_wallets.json",
    "SCOUT_DETECTIONS_PATH": "scout_detections.jsonl", "SCOUT_AUDIT_PATH": "scout_audit.json",
    "SCOUT_AUDIT_HISTORY_PATH": "scout_audit_history.jsonl", "CANDIDATES_PATH": "candidates.jsonl",
    "CANDIDATE_OUTCOMES_PATH": "candidate_outcomes.jsonl", "LATENCY_LOG_PATH": "latency_timelines.jsonl",
    "WALLET_SCORES_PATH": "wallet_scores.json", "WALLET_SCORES_CACHE_PATH": "wallet_scores_cache.json",
    "RUGGER_CREATORS_PATH": "rugger_creators.json",
}.items():
    os.environ[_k] = str(_DATA_TMP / _name)
os.environ.setdefault("SCOUT_AUDIT_ENABLED", "false")
os.environ.setdefault("LEARNING_OUTCOMES_ENABLED", "false")
os.environ.setdefault("LEARNING_HOLDERS_ENABLED", "false")
# v1.17: no wallet-score job, no account-watch socket, no open-position rug polling unless a test opts in
os.environ.setdefault("WALLET_SCORES_ENABLED", "false")
os.environ.setdefault("LIVE_WATCH_ENABLED", "false")
os.environ.setdefault("LIVE_RUG_WATCH_ENABLED", "false")

TEST_DATA_DIR = _DATA_TMP
REAL_DATA_DIR = _REAL_DATA


def _under_real_data(p) -> bool:
    try:
        s = os.fsdecode(p) if not isinstance(p, int) else ""
        return bool(s) and os.path.realpath(os.path.abspath(s)).startswith(str(_REAL_DATA) + os.sep)
    except Exception:  # noqa: BLE001
        return False


def _guard(event, args):
    if event == "open" and args:
        path, mode, flags = (list(args) + [None, None, None])[:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
            (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
        if writing and _under_real_data(path):
            raise PermissionError(f"test suite tried to write real data file: {path}")
    elif event in ("os.rename", "os.replace", "os.remove", "os.unlink", "shutil.rmtree", "os.truncate") and args:
        for a in args[:2]:
            if _under_real_data(a):
                raise PermissionError(f"test suite tried to modify real data path: {a}")


sys.addaudithook(_guard)


# v1.16.1: the non-SOL quote-price sources never hit the network in tests (fail closed unless a test patches them).
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_nonsol_network(monkeypatch):
    from app.live import nonsol

    async def _down(*a, **k):
        raise RuntimeError("network disabled in tests")
    monkeypatch.setattr(nonsol, "_fetch_prices", _down)
    monkeypatch.setattr(nonsol, "_route_rate", _down)
    nonsol._cache.clear()
    yield
    nonsol._cache.clear()


# v1.17: the live pre-buy rug check never hits the network in tests. The shared checker passes (no refusal, no
# unknowns) so older buy-path tests keep their meaning; tests/test_v117.py builds its own RugChecker with fake data.
@pytest.fixture(autouse=True)
def _no_rugcheck_network(monkeypatch):
    from app.live import rugcheck

    async def _pass(mint, arm, *, creator=None):
        return {"ok": True, "refuse": [], "unknown": [], "checks": {}, "elapsed_ms": 0, "linked": [],
                "creator": creator, "launch_holdings": {}}

    async def _down(self, *a, **k):
        raise RuntimeError("network disabled in tests")
    monkeypatch.setattr(rugcheck.checker, "prebuy", _pass)
    monkeypatch.setattr(rugcheck.RugChecker, "rpc", _down)
    rugcheck._rug_cache.update(t=0.0, set=set())
    yield
