"""Malicious-fixture tests for fomo-signer. Every attack must be refused; legit buy/sell/tip/sweep must pass."""
import asyncio
import base64
import json
import os
import socket
import struct

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature

from fomo_signer import policy, server
from fomo_signer.policy import ATA_PROGRAM, COMPUTE_BUDGET, SYSTEM, TOKEN, WSOL

KP = Keypair()
W = str(KP.pubkey())
ATTACKER = str(Keypair().pubkey())
COLD = str(Keypair().pubkey())
MINT = str(Keypair().pubkey())
OTHER_MINT = str(Keypair().pubkey())
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
EVIL_PROGRAM = str(Keypair().pubkey())
WSOL_ATA = policy.ata(W, WSOL)
OUT_ATA = policy.ata(W, MINT)
OTHER_ATA = policy.ata(W, OTHER_MINT)
POOL = str(Keypair().pubkey())
TIP = sorted(policy.JITO_TIP_ACCOUNTS)[0]
SOL = 1_000_000_000
BUY = 200_000_000


def pk(s):
    return Pubkey.from_string(s)


def ix(program, metas, data=b""):
    return Instruction(pk(program), bytes(data), [AccountMeta(pk(a), s, w) for a, s, w in metas])


def cu(limit=200_000, price_micro=5_000):
    return [ix(COMPUTE_BUDGET, [], b"\x02" + struct.pack("<I", limit)),
            ix(COMPUTE_BUDGET, [], b"\x03" + struct.pack("<Q", price_micro))]


def sys_transfer(src, dst, lam):
    return ix(SYSTEM, [(src, True, True), (dst, False, True)], struct.pack("<IQ", 2, lam))


def msg(ixs, payer=W):
    return MessageV0.try_compile(pk(payer), ixs, [], Hash.default())


def tok(mint, owner, amount, delegate=None, close=None):
    d = bytes(pk(mint)) + bytes(pk(owner)) + struct.pack("<Q", amount)
    d += (struct.pack("<I", 1) + bytes(pk(delegate))) if delegate else bytes(36)
    d += b"\x01" + bytes(12) + struct.pack("<Q", 0)
    d += (struct.pack("<I", 1) + bytes(pk(close))) if close else bytes(36)
    assert len(d) == 165
    return {"lamports": 2_039_280, "owner": TOKEN, "data": d}


def wallet(lam):
    return {"lamports": lam, "owner": SYSTEM, "data": b""}


def jup_buy_ixs(amount=BUY, extra=()):
    return cu() + [
        ix(ATA_PROGRAM, [(W, True, True), (WSOL_ATA, False, True), (W, False, False), (WSOL, False, False),
                         (SYSTEM, False, False), (TOKEN, False, False)], b"\x01"),
        sys_transfer(W, WSOL_ATA, amount),
        ix(TOKEN, [(WSOL_ATA, False, True)], b"\x11"),
        ix(JUP, [(W, True, True), (WSOL_ATA, False, True), (OUT_ATA, False, True), (POOL, False, True)], b"\xe5route"),
        *extra]


class FakeChain:
    """pre: account map now; post: what simulateTransaction returns (built per test)."""

    def __init__(self, pre, post, err=None, alts=None):
        self.pre, self.post, self.err, self._alts = pre, post, err, alts or {}
        self.sims = 0

    async def alts(self, keys):
        return {k: self._alts[k] for k in keys if k in self._alts}

    async def accounts(self, addrs):
        return {a: self.pre.get(a) for a in addrs}

    async def simulate(self, tx_b64, addrs):
        self.sims += 1
        return {"err": self.err, "post": {a: self.post.get(a, self.pre.get(a)) for a in addrs}, "units": 1,
                "logs_tail": []}


class LocalKP:
    name = "test"
    pubkey = W

    async def sign(self, m, unsigned):
        return bytes(KP.sign_message(m))


def mk(tmp_path, chain, mode="live", **cfg):
    c = policy.Config(wallet=W, cold_address=COLD, **cfg)
    return server.Signer(c, LocalKP(), chain, mode=mode, state_dir=str(tmp_path), halt_file=str(tmp_path / "HALT"))


def legit_buy_chain(loss=BUY + 1_015_000, out_tokens=1_000_000, extra_post=None):
    pre = {W: wallet(3 * SOL), POOL: {"lamports": 10 * SOL, "owner": JUP, "data": b"x"}}
    post = {W: wallet(3 * SOL - loss), OUT_ATA: tok(MINT, W, out_tokens), POOL: {"lamports": 10 * SOL + BUY, "owner": JUP, "data": b"x"}}
    post[W]["lamports"] -= 2_039_280          # rent moved into our new token account (not a loss)
    post.update(extra_post or {})
    return FakeChain(pre, post)


def run(signer, m, intent, dry_run=False):
    req = {"op": "sign", "msg_b64": base64.b64encode(to_bytes_versioned(m)).decode(), "intent": intent, "dry_run": dry_run}
    return asyncio.run(signer.handle(req))


BUY_INTENT = {"kind": "buy", "input_mint": WSOL, "output_mint": MINT, "amount": BUY, "max_priority_lamports": 2_000_000}


# ------------------------------------------------------------------ legit paths

def test_legit_buy_signs_and_signature_verifies(tmp_path):
    s = mk(tmp_path, legit_buy_chain())
    m = msg(jup_buy_ixs())
    r = run(s, m, BUY_INTENT)
    assert r["ok"] and r["signed"], r
    sig = Signature.from_bytes(base64.b64decode(r["signature_b64"]))
    assert sig.verify(KP.pubkey(), to_bytes_versioned(m))
    rows = [json.loads(l) for l in open(tmp_path / "decisions.jsonl")]
    assert rows[-1]["ok"] and "msg_b64" not in json.dumps(rows[-1])
    assert rows[-1]["signature"] == str(sig)          # tripwire's known-signature source


def test_shadow_mode_never_signs(tmp_path):
    s = mk(tmp_path, legit_buy_chain(), mode="shadow")
    r = run(s, msg(jup_buy_ixs()), BUY_INTENT)
    assert r["ok"] and not r["signed"] and "signature_b64" not in r


def test_dry_run_never_signs(tmp_path):
    r = run(mk(tmp_path, legit_buy_chain()), msg(jup_buy_ixs()), BUY_INTENT, dry_run=True)
    assert r["ok"] and not r["signed"]


def test_legit_sell(tmp_path):
    pre = {W: wallet(SOL), OUT_ATA: tok(MINT, W, 1_000_000)}
    post = {W: wallet(SOL + 150_000_000), OUT_ATA: tok(MINT, W, 0)}
    m = msg(cu() + [ix(JUP, [(W, True, True), (OUT_ATA, False, True), (WSOL_ATA, False, True)], b"\xe5route")])
    r = run(mk(tmp_path, FakeChain(pre, post)), m, {"kind": "sell", "input_mint": MINT, "output_mint": WSOL, "amount": 1_000_000})
    assert r["ok"] and r["signed"], r


def test_legit_tip_and_sweep(tmp_path):
    t = msg([sys_transfer(W, TIP, 100_000)])
    ch = FakeChain({W: wallet(SOL)}, {W: wallet(SOL - 105_000)})
    assert run(mk(tmp_path, ch), t, {"kind": "tip", "amount": 100_000})["signed"]
    sw = msg([sys_transfer(W, COLD, SOL)])
    ch = FakeChain({W: wallet(2 * SOL + 10_000)}, {W: wallet(SOL + 5_000)})
    assert run(mk(tmp_path / "x" if False else tmp_path, ch), sw, {"kind": "sweep", "amount": SOL})["signed"]


# ------------------------------------------------------------------ malicious fixtures

@pytest.mark.parametrize("name,ixs,intent,expect", [
    ("drain to attacker", jup_buy_ixs(extra=[sys_transfer(W, ATTACKER, 5 * SOL)]), BUY_INTENT, "non-allowed destination"),
    ("unknown program", jup_buy_ixs(extra=[ix(EVIL_PROGRAM, [(W, True, True)], b"steal")]), BUY_INTENT, "program not allowed"),
    ("buy over cap", jup_buy_ixs(amount=2 * SOL), {**BUY_INTENT, "amount": 2 * SOL}, "outside"),
    ("wrap more than intent", jup_buy_ixs(amount=BUY * 3), BUY_INTENT, "non-allowed destination"),
    ("approve delegate", jup_buy_ixs(extra=[ix(TOKEN, [(OUT_ATA, False, True), (ATTACKER, False, False), (W, True, False)],
                                               b"\x04" + struct.pack("<Q", 2**63))]), BUY_INTENT, "Approve"),
    ("set authority", jup_buy_ixs(extra=[ix(TOKEN, [(OUT_ATA, False, True), (W, True, False)],
                                            b"\x06\x02\x01" + bytes(pk(ATTACKER)))]), BUY_INTENT, "SetAuthority"),
    ("close to attacker", jup_buy_ixs(extra=[ix(TOKEN, [(WSOL_ATA, False, True), (ATTACKER, False, True), (W, True, False)],
                                                b"\x09")]), BUY_INTENT, "rent to someone else"),
    ("burn our tokens", jup_buy_ixs(extra=[ix(TOKEN, [(OUT_ATA, False, True), (MINT, False, True), (W, True, False)],
                                              b"\x08" + struct.pack("<Q", 5))]), BUY_INTENT, "Burn"),
    ("token transfer out", jup_buy_ixs(extra=[ix(TOKEN, [(OUT_ATA, False, True), (POOL, False, True), (W, True, False)],
                                                 b"\x03" + struct.pack("<Q", 5))]), BUY_INTENT, "non-own account"),
    ("durable nonce", [ix(SYSTEM, [(POOL, False, True), (Pubkey.from_string("SysvarRecentB1ockHashes11111111111111111111").__str__(), False, False),
                                   (W, True, False)], struct.pack("<I", 4))] + jup_buy_ixs(), BUY_INTENT, "durable-nonce"),
    ("assign wallet", jup_buy_ixs(extra=[ix(SYSTEM, [(W, True, True)], struct.pack("<I", 1) + bytes(pk(EVIL_PROGRAM)))]),
     BUY_INTENT, "assign/allocate"),
    ("priority fee bomb", cu(1_400_000, 50_000_000) + jup_buy_ixs()[2:], BUY_INTENT, "priority fee"),
    ("tip to non-jito", [sys_transfer(W, ATTACKER, 100_000)], {"kind": "tip", "amount": 100_000}, "non-allowed destination"),
    ("tip over cap", [sys_transfer(W, TIP, 50_000_000)], {"kind": "tip", "amount": 50_000_000}, "non-allowed destination"),
    ("tip smuggles swap", [sys_transfer(W, TIP, 1000), ix(JUP, [(W, True, True)], b"x")], {"kind": "tip"}, "program not allowed"),
    ("sweep to attacker", [sys_transfer(W, ATTACKER, SOL)], {"kind": "sweep", "amount": SOL}, "non-allowed destination"),
    ("sweep amount mismatch", [sys_transfer(W, COLD, 2 * SOL)], {"kind": "sweep", "amount": SOL}, "non-allowed destination"),
    ("bad kind", jup_buy_ixs(), {**BUY_INTENT, "kind": "transfer"}, "unknown intent kind"),
    ("sell as buy shape", jup_buy_ixs(), {**BUY_INTENT, "input_mint": MINT, "output_mint": WSOL}, "buy must be"),
])
def test_static_refusals(tmp_path, name, ixs, intent, expect):
    ch = legit_buy_chain()
    r = run(mk(tmp_path, ch), msg(ixs), intent)
    assert not r["ok"] and not r["signed"], name
    assert any(expect in x for x in r["reasons"]), (name, r["reasons"])
    assert ch.sims == 0                     # static refusals never even reach simulation


def test_hidden_drain_via_allowed_program_caught_by_simulation(tmp_path):
    ch = legit_buy_chain(loss=BUY + 2 * SOL)          # JUP-looking ix, but CPI drains 2 SOL
    r = run(mk(tmp_path, ch), msg(jup_buy_ixs()), BUY_INTENT)
    assert not r["ok"] and any("SOL value would fall" in x for x in r["reasons"])


def test_cpi_delegate_and_owner_change_caught(tmp_path):
    ch = legit_buy_chain(extra_post={OUT_ATA: tok(MINT, W, 1_000_000, delegate=ATTACKER)})
    r = run(mk(tmp_path, ch), msg(jup_buy_ixs()), BUY_INTENT)
    assert any("delegate" in x for x in r["reasons"])
    pre = {W: wallet(SOL), OTHER_ATA: tok(OTHER_MINT, W, 500)}
    post = {W: wallet(SOL - BUY - 20_000), OUT_ATA: tok(MINT, W, 10), OTHER_ATA: tok(OTHER_MINT, ATTACKER, 500)}
    m = msg(jup_buy_ixs()[:-1] + [ix(JUP, [(W, True, True), (WSOL_ATA, False, True), (OUT_ATA, False, True), (OTHER_ATA, False, True)], b"x")])
    r = run(mk(tmp_path, FakeChain(pre, post)), m, BUY_INTENT)
    assert any("change owner" in x for x in r["reasons"]), r


def test_foreign_close_authority_caught(tmp_path):
    ch = legit_buy_chain(extra_post={OUT_ATA: tok(MINT, W, 1_000_000, close=ATTACKER)})
    assert any("close authority" in x for x in run(mk(tmp_path, ch), msg(jup_buy_ixs()), BUY_INTENT)["reasons"])


def test_other_token_drained_caught(tmp_path):
    pre = {W: wallet(SOL), OUT_ATA: tok(MINT, W, 1_000), OTHER_ATA: tok(OTHER_MINT, W, 500)}
    post = {W: wallet(SOL + 100_000_000), OUT_ATA: tok(MINT, W, 0), OTHER_ATA: tok(OTHER_MINT, W, 0)}
    m = msg(cu() + [ix(JUP, [(W, True, True), (OUT_ATA, False, True), (OTHER_ATA, False, True)], b"x")])
    r = run(mk(tmp_path, FakeChain(pre, post)), m, {"kind": "sell", "input_mint": MINT, "output_mint": WSOL, "amount": 1_000})
    assert any("unexpected token change" in x for x in r["reasons"])


def test_sell_spends_more_than_intent(tmp_path):
    pre = {W: wallet(SOL), OUT_ATA: tok(MINT, W, 1_000_000)}
    post = {W: wallet(SOL + 1), OUT_ATA: tok(MINT, W, 0)}
    m = msg(cu() + [ix(JUP, [(W, True, True), (OUT_ATA, False, True)], b"x")])
    r = run(mk(tmp_path, FakeChain(pre, post)), m, {"kind": "sell", "input_mint": MINT, "output_mint": WSOL, "amount": 10})
    assert any("sell spends" in x for x in r["reasons"])


def test_wallet_assigned_via_cpi_caught(tmp_path):
    ch = legit_buy_chain(extra_post={W: {"lamports": 2 * SOL, "owner": EVIL_PROGRAM, "data": b""}})
    assert any("plain System account" in x for x in run(mk(tmp_path, ch), msg(jup_buy_ixs()), BUY_INTENT)["reasons"])


def test_buy_without_output_refused(tmp_path):
    ch = legit_buy_chain(out_tokens=0)
    assert any("no output" in x for x in run(mk(tmp_path, ch), msg(jup_buy_ixs()), BUY_INTENT)["reasons"])


def test_simulation_failure_refused(tmp_path):
    ch = legit_buy_chain()
    ch.err = {"InstructionError": [4, {"Custom": 6001}]}
    r = run(mk(tmp_path, ch), msg(jup_buy_ixs()), BUY_INTENT)
    assert not r["ok"] and "simulation failed" in r["reasons"][0]


def test_sweep_below_float_refused(tmp_path):
    sw = msg([sys_transfer(W, COLD, SOL)])
    ch = FakeChain({W: wallet(int(1.5 * SOL))}, {W: wallet(int(0.5 * SOL) - 5000)})
    assert any("float" in x for x in run(mk(tmp_path, ch), sw, {"kind": "sweep", "amount": SOL})["reasons"])


def test_external_fee_payer(tmp_path):
    mm = str(Keypair().pubkey())
    m = msg(jup_buy_ixs()[2:], payer=mm)       # JupiterZ-style: market maker pays, we co-sign
    assert any("fee payer" in x for x in run(mk(tmp_path, legit_buy_chain()), m, BUY_INTENT)["reasons"])
    r = run(mk(tmp_path, legit_buy_chain(loss=BUY)), m, BUY_INTENT, )
    assert not r["ok"]
    r = run(mk(tmp_path, legit_buy_chain(loss=BUY), allow_external_fee_payer=True), m, BUY_INTENT)
    assert r["ok"] and r["external_fee_payer"], r
    r = run(mk(tmp_path, legit_buy_chain(loss=BUY + SOL), allow_external_fee_payer=True), m, BUY_INTENT)
    assert not r["ok"] and any("SOL value" in x for x in r["reasons"])


def test_three_signers_refused(tmp_path):
    a, b = str(Keypair().pubkey()), str(Keypair().pubkey())
    m = msg(jup_buy_ixs(extra=[ix(JUP, [(a, True, False), (b, True, False)], b"x")]))
    assert any("required signers" in x for x in run(mk(tmp_path, legit_buy_chain()), m, BUY_INTENT)["reasons"])


def test_halt_file_refuses_everything(tmp_path):
    s = mk(tmp_path, legit_buy_chain())
    (tmp_path / "HALT").write_text("tripwire")
    r = run(s, msg(jup_buy_ixs()), BUY_INTENT)
    assert not r["ok"] and "halted" in r["reasons"][0]


def test_non_canonical_bytes_refused(tmp_path):
    s = mk(tmp_path, legit_buy_chain())
    b = to_bytes_versioned(msg(jup_buy_ixs())) + b"\x00junk"
    r = asyncio.run(s.handle({"op": "sign", "msg_b64": base64.b64encode(b).decode(), "intent": BUY_INTENT}))
    assert not r["ok"]


def test_24h_and_rate_limits(tmp_path):
    s = mk(tmp_path, legit_buy_chain(), max_buy_lamports_24h=3 * BUY, max_signs_per_min=100)
    for _ in range(3):
        assert run(s, msg(jup_buy_ixs()), BUY_INTENT)["signed"]
    r = run(s, msg(jup_buy_ixs()), BUY_INTENT)
    assert not r["signed"] and "24h" in r["reasons"][0]
    s2 = mk(tmp_path, legit_buy_chain(), max_buy_lamports_24h=3 * BUY)   # limits persist across a restart
    assert "24h" in run(s2, msg(jup_buy_ixs()), BUY_INTENT)["reasons"][0]
    s3 = mk(tmp_path / "r", legit_buy_chain(), max_signs_per_min=2) if (tmp_path / "r").mkdir() is None else None
    assert run(s3, msg(jup_buy_ixs()), BUY_INTENT)["signed"] and run(s3, msg(jup_buy_ixs()), BUY_INTENT)["signed"]
    assert "per minute" in run(s3, msg(jup_buy_ixs()), BUY_INTENT)["reasons"][0]


def test_one_sweep_per_hour(tmp_path):
    s = mk(tmp_path, FakeChain({W: wallet(3 * SOL)}, {W: wallet(2 * SOL - 5000)}))
    assert run(s, msg([sys_transfer(W, COLD, SOL)]), {"kind": "sweep", "amount": SOL})["signed"]
    assert "sweep already" in run(s, msg([sys_transfer(W, COLD, SOL)]), {"kind": "sweep", "amount": SOL})["reasons"][0]


def test_alt_accounts_are_checked(tmp_path):
    from solders.address_lookup_table_account import AddressLookupTableAccount
    table = str(Keypair().pubkey())
    lut = AddressLookupTableAccount(pk(table), [pk(POOL), pk(OTHER_ATA)])
    ixs = jup_buy_ixs()[:-1] + [ix(JUP, [(W, True, True), (WSOL_ATA, False, True), (OUT_ATA, False, True),
                                         (POOL, False, True), (OTHER_ATA, False, True)], b"x")]
    m = MessageV0.try_compile(pk(W), ixs, [lut], Hash.default())
    assert len(m.address_table_lookups) == 1
    pre = {W: wallet(SOL), OTHER_ATA: tok(OTHER_MINT, W, 500)}
    post = {W: wallet(SOL - BUY - 2_100_000), OUT_ATA: tok(MINT, W, 9), OTHER_ATA: tok(OTHER_MINT, W, 0)}
    r = run(mk(tmp_path, FakeChain(pre, post, alts={table: [POOL, OTHER_ATA]})), m, BUY_INTENT)
    assert any("unexpected token change" in x for x in r["reasons"]), r      # drained via an ALT-loaded account
    r = run(mk(tmp_path, FakeChain(pre, post)), m, BUY_INTENT)                 # unresolvable table → refused
    assert not r["ok"] and "evaluation error" in r["reasons"][0]


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="SO_PEERCRED is Linux-only")
def test_socket_peer_uid_enforced(tmp_path):
    import tempfile
    s = mk(tmp_path, legit_buy_chain())
    path = os.path.join(tempfile.mkdtemp(dir="/tmp"), "s.sock")

    async def go(allowed):
        task = asyncio.create_task(server.serve(s, path, allowed))
        for _ in range(50):
            if os.path.exists(path):
                break
            await asyncio.sleep(0.02)
        r, w = await asyncio.open_unix_connection(path)
        try:
            w.write(b'{"op":"pubkey"}\n')
            await w.drain()
            line = await r.readline()
        except ConnectionError:
            line = b""                       # refused peer: connection dropped
        w.close()
        task.cancel()
        return line

    assert json.loads(asyncio.run(go([os.getuid()])))["pubkey"] == W
    assert asyncio.run(go([os.getuid() + 12345])) == b""


# FOMOANT 2026-10-19: SIGNER_ALLOW_STRAY_BUY_TOKENS
def _stray_buy_msg():
    return msg(jup_buy_ixs()[:-1] + [ix(JUP, [(W, True, True), (WSOL_ATA, False, True), (OUT_ATA, False, True), (OTHER_ATA, False, True)], b"x")])


def test_stray_token_on_buy_refused_by_default(tmp_path):
    ch = legit_buy_chain(extra_post={OTHER_ATA: tok(OTHER_MINT, W, 5)})
    r = run(mk(tmp_path, ch), _stray_buy_msg(), BUY_INTENT)
    assert any("unexpected token change" in x for x in r.get("reasons", [])), r


def test_stray_token_on_buy_allowed_with_flag(tmp_path):
    ch = legit_buy_chain(extra_post={OTHER_ATA: tok(OTHER_MINT, W, 5)})
    r = run(mk(tmp_path, ch, allow_stray_buy_tokens=True), _stray_buy_msg(), BUY_INTENT)
    assert not any("unexpected token change" in x for x in r.get("reasons", [])), r


def test_other_token_lost_on_buy_still_refused_with_flag(tmp_path):
    pre_extra = {OTHER_ATA: tok(OTHER_MINT, W, 500)}
    ch = legit_buy_chain(extra_post={OTHER_ATA: tok(OTHER_MINT, W, 0)})
    ch.pre.update(pre_extra) if hasattr(ch, "pre") else None
    r = run(mk(tmp_path, ch, allow_stray_buy_tokens=True), _stray_buy_msg(), BUY_INTENT)
    if hasattr(ch, "pre"):
        assert any("unexpected token change" in x for x in r.get("reasons", [])), r
