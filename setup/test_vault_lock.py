#!/usr/bin/env python3
"""Tests the shared vault lock (jobs/common.py VaultLock) against a scratch folder.
Run: <runtime python> setup\\test_vault_lock.py      (does not touch the real vault)"""
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
import common as C  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="vaultlock-test-"))
cfg = {"vault_path": str(tmp), "timezone": "America/Chicago", "lock_stale_minutes": 120, "pc_name": "TESTPC"}
lockf = tmp / ".vault-librarian.lock"
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


def fake(owner, hours_ago, raw=None, mtime_hours_ago=None):
    created = (C.now(cfg) - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    lockf.write_text(raw if raw is not None else json.dumps({"owner": owner, "created": created, "host": "laptop-test"}), encoding="ascii")
    if mtime_hours_ago is not None:
        t = time.time() - mtime_hours_ago * 3600
        os.utime(lockf, (t, t))
    return created


# 1. no lock -> acquire, exact format, release deletes
lk = C.VaultLock(cfg, "TESTPC nightly")
ok, note = lk.acquire()
body = lockf.read_text(encoding="ascii")
d = json.loads(body)
check("1 no lock: acquired", ok and note is None)
check("1 format is ASCII JSON {owner, created, host}", set(d) == {"owner", "created", "host"} and d["owner"] == "TESTPC nightly"
      and d["host"] == "TESTPC" and dt.datetime.fromisoformat(d["created"]).utcoffset() is not None and body.isascii(), body)
lk.release()
check("1 release deletes the lock", not lockf.exists())

# 2. FRESH lock from the laptop job (10 minutes old) -> skipped, lock untouched
created = fake("laptop cowork-sync", 10 / 60)
before = lockf.read_text()
ok, note = C.VaultLock(cfg, "TESTPC nightly").acquire()
check("2 fresh lock by another job: skipped", not ok and note == f"skipped: lock held by laptop cowork-sync since {created}", note)
check("2 fresh lock left exactly as it was", lockf.read_text() == before)

# 3. STALE lock from the laptop job (3 hours old) -> replaced, noted, then released
created = fake("laptop cowork-sync", 3)
lk = C.VaultLock(cfg, "TESTPC nightly")
ok, note = lk.acquire()
check("3 stale lock (3 h): acquired and noted", ok and note.startswith("replaced a stale lock held by laptop cowork-sync since " + created), note)
check("3 lock now ours", json.loads(lockf.read_text())["owner"] == "TESTPC nightly")
lk.release()
check("3 released", not lockf.exists())

# 4. unreadable lock, file modified 10 minutes ago -> treated as fresh (skipped)
fake(None, 0, raw="{not json", mtime_hours_ago=10 / 60)
ok, note = C.VaultLock(cfg, "TESTPC weekly").acquire()
check("4 unreadable + recent file time: skipped", not ok and "file time" in note, note)

# 5. unreadable lock, file modified 3 hours ago -> stale by file time, replaced
fake(None, 0, raw="{not json", mtime_hours_ago=3)
lk = C.VaultLock(cfg, "TESTPC weekly")
ok, note = lk.acquire()
check("5 unreadable + old file time: replaced", ok and note.startswith("replaced a stale lock"), note)
lk.release()

# 6. leftover lock from the same job (fresh) -> replaced
fake("TESTPC intake", 0.1)
lk = C.VaultLock(cfg, "TESTPC intake")
ok, note = lk.acquire()
check("6 leftover from same job: replaced", ok and note.startswith("replaced a leftover lock from this job"), note)
lk.release()

# 7. our lock was replaced as stale by someone else -> release must not delete theirs
lk = C.VaultLock(cfg, "TESTPC nightly")
lk.acquire()
fake("laptop cowork-sync", 0)
lk.release()
check("7 release leaves another job's lock alone", lockf.exists() and json.loads(lockf.read_text())["owner"] == "laptop cowork-sync")
lockf.unlink()

# 8. job fails inside try/finally -> lock still removed
lk = C.VaultLock(cfg, "TESTPC nightly")
try:
    ok, _ = lk.acquire()
    try:
        raise RuntimeError("simulated job failure")
    finally:
        lk.release()
except RuntimeError:
    pass
check("8 lock removed after a failing job", not lockf.exists())

print(f"\n{sum(results)}/{len(results)} checks passed  (scratch folder {tmp})")
sys.exit(0 if all(results) else 1)
