#!/usr/bin/env python3
"""Lists personal details in the files git would publish, before you push or make the repository public.

  python setup/privacy_check.py              files git tracks or would add (tracked + untracked, not ignored)
  python setup/privacy_check.py --history    also every past commit (what a public repo would expose)
  python setup/privacy_check.py --brief      counts per file only
  python setup/privacy_check.py --root <dir> another repository, e.g. the public copy (setup/make_public.ps1)

The terms come from your own settings, so nothing personal is written into this script:
  * config/vault.json: mailbox, notify_to, vault_path, queue.dropbox_path, mcp.hosts, intake.confidential_domains,
    intake.signature_markers, and queue.confidential_markers beyond the generic legal phrases
  * your Windows user name and this project's absolute folder
  * config/privacy-terms.local.txt (git-ignored): any other words, one per line (family names, client names ...)
Exit code 1 when anything is found. Standard library only.
"""
import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
GENERIC = {"privileged & confidential", "privileged and confidential", "attorney-client privilege",
           "attorney client privilege", "attorney work product"}


def terms():
    t = set()
    try:
        c = json.loads((APP / "config" / "vault.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        c = {}
    for k in ("mailbox", "notify_to", "vault_path"):
        if c.get(k):
            t.add(str(c[k]))
    q, i, m = c.get("queue") or {}, c.get("intake") or {}, c.get("mcp") or {}
    if q.get("dropbox_path"):
        t.add(str(q["dropbox_path"]))
    t.update(str(x) for x in m.get("hosts") or [])
    t.update(str(x) for x in i.get("confidential_domains") or [])
    t.update(str(x) for x in i.get("signature_markers") or [])
    t.update(str(x) for x in q.get("confidential_markers") or [] if str(x).lower() not in GENERIC)
    for mb in [c.get("mailbox"), c.get("notify_to")]:          # the mail domain on its own
        if mb and "@" in mb:
            t.add(mb.split("@", 1)[1])
    t.add(str(APP))
    try:
        t.add(getpass.getuser())
    except Exception:  # noqa: BLE001
        pass
    extra = APP / "config" / "privacy-terms.local.txt"
    if extra.exists():
        t.update(x.strip() for x in extra.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#"))
    t = {x for x in t if len(x) >= 4}                           # very short terms match everywhere
    # backslash paths also appear with doubled or forward slashes
    for x in list(t):
        if "\\" in x:
            t.update({x.replace("\\", "\\\\"), x.replace("\\", "/")})
    return sorted(t, key=str.lower)


ROOT = [APP]   # the repository scanned (--root); the terms always come from this app's settings


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT[0]), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def scan_worktree(ts):
    files = git("ls-files", "--cached", "--others", "--exclude-standard").stdout.splitlines()
    hits = {}
    for f in files:
        p = ROOT[0] / f
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = text.lower()
        for t in ts:
            n = low.count(t.lower())
            if n:
                hits.setdefault(f, {})[t] = n
    return hits


def scan_history(ts):
    revs = git("rev-list", "--all").stdout.split()
    found = {}
    for t in ts:
        r = git("grep", "-I", "-i", "-l", "-F", t, *revs) if revs else None
        for line in (r.stdout.splitlines() if r else []):
            rev, _, path = line.partition(":")
            found.setdefault(t, set()).add(path)
    return found


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--root", help="scan this git repository instead (e.g. the public copy from make_public.ps1)")
    a = ap.parse_args(argv)
    if a.root:
        ROOT[0] = Path(a.root).resolve()
    ts = terms()
    print(f"checking {len(ts)} personal terms from your settings (not shown)\n")
    labels = {t: f"term{i + 1}" for i, t in enumerate(ts)}       # never print the terms themselves
    hits = scan_worktree(ts)
    for f, d in sorted(hits.items()):
        total = sum(d.values())
        print(f"{total:5}  {f}" + ("" if a.brief else "   (" + ", ".join(f"{labels[t]} x{n}" for t, n in d.items()) + ")"))
    print(f"\n{len(hits)} file(s) in the working tree contain personal terms.")
    if a.history:
        h = scan_history(ts)
        paths = sorted({p for s in h.values() for p in s})
        print(f"history: {len(paths)} path(s) in past commits contain personal terms"
              + ("" if a.brief else ":\n  " + "\n  ".join(paths[:200])))
        print("A public repository needs a fresh history (see docs/MAKING-PUBLIC.md).")
    if not a.brief:
        print("\nterm numbers:")
        for t, lab in labels.items():
            shown = t if len(t) <= 3 else t[:2] + "..." + t[-2:]
            print(f"  {lab}: {shown}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
