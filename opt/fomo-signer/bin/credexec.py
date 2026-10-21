#!/usr/bin/env python3
"""Load systemd encrypted credentials (decrypted by systemd into this unit's private, non-swappable
/run/credentials/<unit>/) into the process environment, then exec the real program. Never prints values.
  FOMO_ENV_CREDS=a,b        credentials holding KEY=VALUE lines → os.environ
  FOMO_TG_SESSION_CRED=x    Telethon session credential → copied to $RUNTIME_DIRECTORY (tmpfs), SESSION_NAME set
  FOMO_KEYPAIR_CRED=x       keypair credential → SOLANA_KEYPAIR_PATH points at it (signer only)"""
import os
import shutil
import sys


def parse(path):
    out = {}
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def main():
    cd = os.environ.get("CREDENTIALS_DIRECTORY")
    if not cd:
        sys.exit("credexec: no CREDENTIALS_DIRECTORY (must run under systemd with LoadCredentialEncrypted=)")
    for name in filter(None, os.environ.get("FOMO_ENV_CREDS", "").split(",")):
        os.environ.update(parse(os.path.join(cd, name.strip())))
    sess = os.environ.get("FOMO_TG_SESSION_CRED")
    if sess:
        rd = os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0]
        if not rd:
            sys.exit("credexec: RUNTIME_DIRECTORY needed for the Telegram session")
        dst = os.path.join(rd, "fomo_pump.session")
        old = os.umask(0o077)
        shutil.copyfile(os.path.join(cd, sess), dst)
        os.umask(old)
        os.chmod(dst, 0o600)
        os.environ["SESSION_NAME"] = os.path.join(rd, "fomo_pump")
    kp = os.environ.get("FOMO_KEYPAIR_CRED")
    if kp:
        os.environ["SOLANA_KEYPAIR_PATH"] = os.path.join(cd, kp)
    for k in ("FOMO_ENV_CREDS", "FOMO_TG_SESSION_CRED", "FOMO_KEYPAIR_CRED"):
        os.environ.pop(k, None)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
