#!/usr/bin/env python3
"""Restart the vault web service on request (2026-10-14): the Vault connector's vault_restart_service tool,
so a Claude chat can restart it without anyone signing in to this PC.

Checks, in order, before restarting the Server task:
  1. expect = {path relative to the app folder: sha256 (or its first 12+ characters)}: wait (up to `wait` seconds)
     until OneDrive has brought those files to this PC. Code edited on the laptop takes a minute or more to arrive,
     and a restart before then would load the old code.
  2. Every .py the service loads (jobs, service, vault-librarian scripts) compiles; if one doesn't, the running
     service is left alone (a half-finished edit can't take the page down).
  3. "Apply these answers" isn't running (the vault lock is held by "this PC organize-apply"): restarting would
     cut it off. force=True overrides this check only.
  4. The code changed since the last restart (nightly.STAMP); force=True restarts anyway.
Then nightly.restart_server() (the same restart Nightly and the 06:00 run use), which records the restart time.

Command line (on this PC): python service_restart.py [--force] [--expect path=sha256 ...] [--dry-run]
"""
import argparse
import datetime as dt
import hashlib
import json
import py_compile
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import nightly  # noqa: E402

APPLY_OWNER = f"{C.pc_name()} organize-apply"


def _sha(p):
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return ""


def _stamp_time():
    try:
        return float(nightly.STAMP.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def _local(ts):
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if ts else "never"


def wait_for_files(expect, wait):
    """[] when every expected file on this PC has the expected hash; else the paths still different."""
    deadline = time.time() + max(0, int(wait))
    while True:
        behind = []
        for rel, want in (expect or {}).items():
            want = str(want or "").strip().lower()
            got = _sha(C.APP / str(rel).replace("\\", "/"))
            if len(want) < 12 or not got.startswith(want):
                behind.append(str(rel))
        if not behind or time.time() >= deadline:
            return behind
        time.sleep(5)


def compile_errors():
    out = []
    with tempfile.TemporaryDirectory() as td:
        for d in (C.APP / "jobs", C.APP / "service", C.VL):
            for f in sorted(d.glob("*.py")):
                try:
                    py_compile.compile(str(f), cfile=str(Path(td) / (f.stem + ".pyc")), doraise=True)
                except py_compile.PyCompileError as e:
                    out.append(f"{f.relative_to(C.APP) if C.APP in f.parents else f.name}: {str(e.msg).strip()[-300:]}")
    return out


def apply_running(cfg):
    lock = C.VaultLock(cfg, "service-restart check")
    if not lock.path.exists():
        return ""
    owner, created, age = lock._read()
    if owner == APPLY_OWNER and (age is None or age < lock.stale):
        return f"'Apply these answers' is running (vault lock since {created})"
    return ""


def restart(cfg, expect=None, force=False, wait=40, dry=False, log=None):
    """-> dict(restarted, status, detail, ...) for the connector tool."""
    log = log or (lambda *_a: None)
    res = {"restarted": False}
    behind = wait_for_files(expect, wait) if expect else []
    if behind:
        res.update(status="waiting_for_sync",
                   detail=f"{len(behind)} changed file(s) haven't reached {C.pc_name()} yet; try again in a minute",
                   files_not_synced=behind)
        return res
    errs = compile_errors()
    if errs:
        res.update(status="not_restarted_compile_error",
                   detail=f"the code on {C.pc_name()} doesn't compile, so the running service was left alone",
                   errors=errs[:10])
        return res
    busy = apply_running(cfg)
    if busy and not force:
        res.update(status="busy", detail=busy + "; not restarted (ask again when it finishes)")
        return res
    newest, last = nightly.code_mtime(), _stamp_time()
    res.update(code_changed=_local(newest), last_restart=_local(last))
    if newest <= last and not force and not expect:
        res.update(status="already_current", detail="the service already runs the current code")
        return res
    txt, ok = nightly.restart_server(log, dry)
    res.update(restarted=bool(ok and not dry), status="restarted" if ok else "restart_failed", detail=txt)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait", type=int, default=40)
    ap.add_argument("--expect", nargs="*", default=[], help="path=sha256 pairs")
    x = ap.parse_args()
    cfg = C.load_config()
    expect = dict(e.split("=", 1) for e in x.expect if "=" in e)
    print(json.dumps(restart(cfg, expect, x.force, x.wait, x.dry_run, print), indent=1))


if __name__ == "__main__":
    main()
