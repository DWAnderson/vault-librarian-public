#!/usr/bin/env python3
"""One-time move of the vault's housekeeping items into `_Obsidian Internal/` (runs locally; costs no tokens).

Moves, from the vault root into `_Obsidian Internal/` (created if missing):
  Archives, attachments, Templates, Topics, _claude-tags.md, Vault Review.base
index.md, log.md and CLAUDE.md stay at the root (OKF bundle root; Claude reads CLAUDE.md there).

Because the files are moved by a script rather than inside Obsidian, it also updates what
Obsidian would have updated, plus what Obsidian never updates:
  - path-qualified links and embeds: [[Archives/x]], ![[attachments/Sources/y.pdf]], [t](/Topics/z.md)
  - plain paths in frontmatter, e.g. `resource: attachments/Sources/y.pdf`
  - Vault Review.base filters: file.inFolder("Templates") / ("Topics")
  - Obsidian settings: template folder, daily-note template, attachment folder
Links by name ([[Some note]]) need no change. Other `.obsidian` files that mention a moved
folder are listed, not changed.

Dry run by default; --apply makes the changes, takes the shared vault lock and writes log.md.
Safe to re-run: items already inside `_Obsidian Internal/` are left alone.

Usage:
  python move_internal.py <vault> [--apply] [--owner "laptop move_internal"]
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import INTERNAL_DIR, INTERNAL_ITEMS, _find_ci  # noqa: E402

TEXT_EXT = (".md", ".base", ".canvas")
SKIP = {".obsidian", ".trash", ".git", "_snapshots", ".smart-env", ".sync-backups", "node_modules"}
LOCK = ".vault-librarian.lock"
STALE = dt.timedelta(hours=2)


def now():
    return dt.datetime.now().astimezone()


def take_lock(vault, owner):
    p = os.path.join(vault, LOCK)
    if os.path.exists(p):
        try:
            info = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            info = {}
        try:
            when = dt.datetime.fromisoformat(str(info.get("created")).replace("Z", "+00:00"))
        except ValueError:
            when = dt.datetime.fromtimestamp(os.path.getmtime(p)).astimezone()
        if info.get("owner") != owner and now() - when < STALE:
            sys.exit(f"vault is locked by {info.get('owner')} since {info.get('created')}; try again later")
    with open(p, "w", encoding="ascii") as f:
        json.dump({"owner": owner, "created": now().isoformat(timespec="seconds"), "host": os.environ.get("COMPUTERNAME", "")}, f)
    return p


def plan_moves(vault):
    """-> [(root name, target rel)] for items at the root that aren't already inside."""
    idir = _find_ci(vault, INTERNAL_DIR) or INTERNAL_DIR
    moves = []
    for name in INTERNAL_ITEMS:
        real = _find_ci(vault, name)
        if real:
            moves.append((real, f"{idir}/{real}"))
    return idir, moves


def move_tree(src, dst, conflicts):
    """Rename src to dst; if dst exists (a folder), merge child by child, never overwriting."""
    if not os.path.exists(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        return
    if not (os.path.isdir(src) and os.path.isdir(dst)):
        conflicts.append(dst)
        return
    for n in os.listdir(src):
        move_tree(os.path.join(src, n), os.path.join(dst, n), conflicts)
    if not os.listdir(src):
        os.rmdir(src)


def rewriter(moves, idir):
    """-> fn(text) that prefixes moved roots in link targets (anywhere) and in plain
    frontmatter paths (`resource: attachments/Sources/y.pdf`, frontmatter block only)."""
    names = sorted((r for r, _ in moves), key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in names)
    lower = {n.lower(): n for n in names}
    # [[Archives/x]]  ![[attachments/y.png]]  [t](/Topics/z.md)  [t](<Archives/x.md>): folder paths only
    link = re.compile(r"(?P<pre>!?\[\[|\]\(<?/?)(?P<name>" + alt + r")(?P<post>/)", re.I)
    # YAML value or list item that is a path under a moved folder
    plain = re.compile(r"(?m)(?P<pre>^[ \t]*(?:[\w-]+:[ \t]+|-[ \t]+)[\"']?)(?P<name>" + alt + r")(?P<post>/)", re.I)
    fm_re = re.compile(r"(?s)\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)")

    def sub(m):
        pre, name, post = m.group("pre"), m.group("name"), m.group("post")
        inner = idir.replace(" ", "%20") if pre.startswith("](") and not pre.startswith("](<") else idir
        return f"{pre}{inner}/{lower[name.lower()]}{post}"

    def fn(text):
        n = 0
        fm = fm_re.match(text)
        if fm:
            head, k1 = plain.subn(sub, fm.group(0))
            text, n = head + text[fm.end():], k1
        text, k2 = link.subn(sub, text)
        return text, n + k2
    return fn


def fix_base(text, idir):
    for n in ("Templates", "Topics", "Archives", "attachments"):
        text = re.sub(r'inFolder\("' + n + r'"\)', f'inFolder("{idir}/{n}")', text, flags=re.I)
    return text


def fix_settings(vault, moves, idir, apply):
    """Obsidian core settings that hold a moved folder's path -> [(file, key, old, new)]."""
    changed, mentions = [], []
    roots = {r.lower(): r for r, _ in moves}
    odir = os.path.join(vault, ".obsidian")
    targets = {"templates.json": ["folder"], "daily-notes.json": ["template"], "app.json": ["attachmentFolderPath"]}
    for fn, keys in targets.items():
        p = os.path.join(odir, fn)
        if not os.path.exists(p):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        dirty = False
        for k in keys:
            v = data.get(k)
            if not isinstance(v, str):
                continue
            head, _, rest = v.strip("/").partition("/")
            if head.lower() in roots:
                nv = f"{idir}/{roots[head.lower()]}" + (f"/{rest}" if rest else "")
                changed.append((fn, k, v, nv))
                data[k] = nv
                dirty = True
        if dirty and apply:
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, indent=2)
    for root, dirs, files in os.walk(odir):
        for f in files:
            if f.endswith(".json") and f not in targets:
                p = os.path.join(root, f)
                try:
                    s = open(p, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                for r in roots.values():
                    if re.search(r'"' + re.escape(r) + r'(/[^"]*)?"', s):
                        mentions.append(os.path.relpath(p, vault).replace(os.sep, "/") + f" mentions \"{r}\"")
    return changed, mentions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--owner", default="move_internal")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    if not os.path.isdir(os.path.join(vault, ".obsidian")):
        sys.exit(f"not an Obsidian vault (no .obsidian folder): {vault}")
    idir, moves = plan_moves(vault)
    if not moves:
        print(f"nothing to move: no housekeeping items at the root of {vault}")
        return 0
    lock = take_lock(vault, a.owner) if a.apply else None
    log, conflicts = [], []
    try:
        rewrite = rewriter(moves, idir)
        # 1. rewrite references (before moving, so paths are still where walk expects them)
        edits = 0
        for root, dirs, files in os.walk(vault):
            dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
            for f in files:
                if not f.lower().endswith(TEXT_EXT) or (root == vault and f.lower() == "log.md"):
                    continue  # log.md is history: its old paths stay as they were
                p = os.path.join(root, f)
                rel = os.path.relpath(p, vault).replace(os.sep, "/")
                s = open(p, encoding="utf-8", errors="surrogateescape").read()
                t, n = rewrite(s)
                if f.lower() == "vault review.base":
                    t = fix_base(t, idir)
                if t != s:
                    edits += 1
                    n = n or 1
                    print(f"{'update' if a.apply else 'would update'} {rel} ({n} path reference(s))")
                    if a.apply:
                        with open(p, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                            fh.write(t)
        if edits:
            log.append(f"- Updated folder paths in {edits} note(s) and files")
        # 2. Obsidian settings
        changed, mentions = fix_settings(vault, moves, idir, a.apply)
        for fn, k, v, nv in changed:
            print(f"{'set' if a.apply else 'would set'} .obsidian/{fn} {k}: {v} -> {nv}")
            log.append(f"- Obsidian setting `{fn}` `{k}`: `{v}` → `{nv}`")
        for m in mentions:
            print(f"CHECK   .obsidian: {m} (not changed)")
        # 3. move
        for real, target in moves:
            print(f"{'moved' if a.apply else 'would move'} {real} -> {target}")
            if a.apply:
                move_tree(os.path.join(vault, real), os.path.join(vault, *target.split("/")), conflicts)
                log.append(f"- Moved `{real}` → `{target}`")
        for c in conflicts:
            print(f"CONFLICT {os.path.relpath(c, vault)} already exists; the root copy was left in place")
            log.append(f"- Not moved (already exists in {idir}): `{os.path.relpath(c, vault)}`")
        if a.apply and log:
            lp = os.path.join(vault, "log.md")
            existing = open(lp, encoding="utf-8").read() if os.path.exists(lp) else "# Vault Update Log\n"
            entry = f"\n## {now().date().isoformat()}\n\n**Moved housekeeping folders into {idir}**\n\n" + "\n".join(log) + "\n"
            with open(lp, "w", encoding="utf-8", newline="\n") as f:
                f.write(existing.rstrip() + "\n" + entry)
        print(("done" if a.apply else "dry run: nothing changed") + f"; {len(moves)} item(s), {edits} file(s) with path references")
        return 1 if conflicts else 0
    finally:
        if lock and os.path.exists(lock):
            os.remove(lock)


if __name__ == "__main__":
    sys.exit(main())
