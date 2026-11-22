#!/usr/bin/env python3
"""Review and resolve archived duplicates (notes with status: deprecated + duplicate_of).

Usage:
  python resolve_duplicates.py <vault> --list [--json out.json]
      Lists each archived duplicate, its keeper, and how many paragraphs exist only in
      the archived copy (0 = nothing would be lost by deleting it).
  python resolve_duplicates.py <vault> --apply decisions.json [--dry-run]
      decisions.json: [{"path": "Archives/x.md", "action": "restore" | "merge" | "delete"}]
        restore: back to its archived_from folder (or the keeper's folder); status,
                 duplicate_of and archived_from removed; not_a_duplicate: true added so it
                 is never auto-archived again.
        merge:   paragraphs found only in the archived copy are appended to the keeper
                 under "## Merged from <name> (<date>)"; then the archived copy is deleted.
        delete:  archived copy deleted.
      Deleting needs Cowork's delete permission for the vault; if it is refused the
      file is left in place and reported. Every action is appended to log.md.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import Resolver, parse_wikilink, split_frontmatter, walk
from apply_plan import set_keys


def paras(body):
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def norm(p):
    return " ".join(re.sub(r"[^\w\s]|_", " ", p.lower()).split())


def load(vault):
    files = list(walk(vault))
    res = Resolver([r for r, _ in files]).add_aliases(vault)
    out = []
    for rel, ab in files:
        if not rel.lower().endswith(".md"):
            continue
        with open(ab, encoding="utf-8", errors="replace") as f:
            text = f.read()
        fm, _, body = split_frontmatter(text)
        if isinstance(fm, dict) and fm.get("status") == "deprecated" and fm.get("duplicate_of"):
            m = re.search(r"\[\[([^\]]+)\]\]", str(fm["duplicate_of"]))
            target = parse_wikilink(m.group(1))[0] if m else str(fm["duplicate_of"])
            keeper, _ = res.resolve(target)
            if not keeper and "/" in target:  # keeper was filed elsewhere since archiving: find it by name
                keeper, _ = res.resolve(target.rsplit("/", 1)[-1])
            unique = []
            if keeper:
                with open(os.path.join(vault, *keeper.split("/")), encoding="utf-8", errors="replace") as f:
                    kb = split_frontmatter(f.read())[2]
                have = {norm(p) for p in paras(kb)}
                unique = [p for p in paras(body) if norm(p) not in have and len(norm(p)) > 3]
            out.append({"path": rel, "keeper": keeper, "archived_from": fm.get("archived_from"),
                        "unique_paragraphs": len(unique), "unique": unique, "text": text})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--apply")
    ap.add_argument("--json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    dups = load(vault)
    if a.list:
        for d in dups:
            print(f"{d['path']}  (keeper: {d['keeper'] or 'NOT FOUND'})  — {d['unique_paragraphs']} paragraph(s) only in the archived copy")
        print(f"\n{len(dups)} archived duplicates")
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump([{k: v for k, v in d.items() if k != "text"} for d in dups], f, indent=1)
        return
    by = {d["path"]: d for d in dups}
    decisions = json.load(open(a.apply, encoding="utf-8"))
    log, problems = [], []
    today = dt.date.today().isoformat()
    for dec in decisions:
        d = by.get(dec["path"].replace("\\", "/").lstrip("/"))
        act = dec.get("action")
        if not d:
            problems.append(f"{dec['path']}: not an archived duplicate")
            continue
        src = os.path.join(vault, *d["path"].split("/"))
        if act == "restore":
            folder = d["archived_from"] or (d["keeper"].rsplit("/", 1)[0] if d["keeper"] and "/" in d["keeper"] else "")
            dest_rel = (folder + "/" if folder else "") + d["path"].rsplit("/", 1)[-1]
            dest = os.path.join(vault, *dest_rel.split("/"))
            if os.path.exists(dest):
                problems.append(f"{d['path']}: {dest_rel} already exists — not restored")
                continue
            if not a.dry_run:
                text = set_keys(d["text"], {"not_a_duplicate": True}, ["status", "duplicate_of", "archived_from"])
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(src, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                os.rename(src, dest)
            log.append(f"- Restored `{d['path']}` → `{dest_rel}`")
        elif act in ("merge", "delete"):
            if act == "merge" and d["unique"]:
                if not d["keeper"]:
                    problems.append(f"{d['path']}: keeper not found — not merged")
                    continue
                kp = os.path.join(vault, *d["keeper"].split("/"))
                if not a.dry_run:
                    with open(kp, encoding="utf-8") as f:
                        kt = f.read()
                    kt = kt.rstrip() + f"\n\n## Merged from {d['path'].rsplit('/', 1)[-1][:-3]} ({today})\n\n" + "\n\n".join(d["unique"]) + "\n"
                    with open(kp, "w", encoding="utf-8", newline="\n") as f:
                        f.write(kt)
                log.append(f"- Merged {len(d['unique'])} paragraph(s) from `{d['path']}` into `{d['keeper']}`")
            if not a.dry_run:
                try:
                    os.remove(src)
                except OSError as e:
                    problems.append(f"{d['path']}: could not delete ({e.strerror}) — request delete permission, then rerun")
                    continue
            log.append(f"- Deleted archived duplicate `{d['path']}`")
        else:
            problems.append(f"{d['path']}: unknown action {act!r}")
    for p in problems:
        print("PROBLEM", p)
    for l in log:
        print(l)
    if log and not a.dry_run:
        lp = os.path.join(vault, "log.md")
        existing = open(lp, encoding="utf-8").read() if os.path.exists(lp) else "# Vault Update Log\n"
        with open(lp, "w", encoding="utf-8", newline="\n") as f:
            f.write(existing.rstrip() + f"\n\n## {today}\n\n**Resolved archived duplicates**\n\n" + "\n".join(log) + "\n")


if __name__ == "__main__":
    main()
