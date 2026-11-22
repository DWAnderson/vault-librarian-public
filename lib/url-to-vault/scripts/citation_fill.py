#!/usr/bin/env python3
"""Tidy and complete the citation properties of existing notes (Web Clippings by default).

Usage:
  python citation_fill.py <vault> --plan plan.json [--paths a.md …] [--all] [--fetch] [--max 40]

Scope: Web Clippings in Clippings/ and Inbox/ (default), every Web Clipping in the vault
(--all, the "backfill citations" mode), or exactly --paths.
Per note: removes empty citation properties the Web Clipper left, normalizes author lists
(strips [[wikilinks]]), dates (ISO) and work_type (`article` -> article-newspaper etc. when the
page says so), and with --fetch reads the page's metadata (never its text) to fill only the
keys that are missing. Recomputes `cite_as` (Chicago). Never touches the body or `generated`.
Writes a vault-librarian apply_plan.py plan; apply it with:
  python <vault-librarian>/scripts/apply_plan.py <vault> plan.json
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import citation  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

SKIP = {".obsidian", ".trash", ".git", "_snapshots", "node_modules", ".sync-backups", "Templates"}
FILL_FROM_PAGE = ["author", "publication", "publisher", "published", "doi", "journal", "volume",
                  "issue", "pages", "section", "language", "work_type"]


def read_fm(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    m = re.match(r"(?s)^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)", text)
    if not m:
        return None
    try:
        d = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    return d if isinstance(d, dict) else None


def same(a, b):
    return json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--paths", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--max", type=int, default=40)
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    if a.paths:
        targets = [p.replace("\\", "/").lstrip("/") for p in a.paths]
    else:
        targets = []
        for root, dirs, files in os.walk(vault):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
            for f in sorted(files):
                if f.endswith(".md") and f not in ("index.md", "log.md"):
                    rel = os.path.relpath(os.path.join(root, f), vault).replace(os.sep, "/")
                    if a.all or rel.startswith(("Clippings/", "Inbox/")):
                        targets.append(rel)
    if a.fetch:
        import url_probe
    items, stats, failed, left = [], {"notes": 0, "fetched": 0, "keys_set": 0, "keys_removed": 0}, [], 0
    for rel in targets:
        fm = read_fm(os.path.join(vault, rel))
        if fm is None:
            continue
        if not a.paths and fm.get("type") != "Web Clipping" and not (rel.startswith("Clippings/") and not fm.get("type")):
            continue
        if len(items) >= a.max:
            left += 1
            continue
        stats["notes"] += 1
        cleaned = citation.clean(fm)
        url = fm.get("resource") if isinstance(fm.get("resource"), str) else None
        if not url and isinstance(fm.get("source"), str) and fm["source"].startswith("http"):
            url = fm["source"]
        missing = [k for k in ("author", "publication", "published") if k not in cleaned]
        vague_wt = cleaned.get("work_type") in (None, "article")
        made_here = isinstance(fm.get("generated"), dict) and fm["generated"].get("by") == "process:url-intake"
        if a.fetch and url and url.startswith("http") and (missing or vague_wt) and not made_here:
            try:
                res, _ = url_probe.probe(url)
                page = citation.clean(res.get("meta") or {})
                stats["fetched"] += 1
                for k in FILL_FROM_PAGE:
                    if k in page and (k not in cleaned or (k == "work_type" and vague_wt and page[k] != "webpage")):
                        cleaned[k] = page[k]
                if res.get("category") == "unsupported":
                    failed.append(f"{rel}: {res.get('reason')}")
                time.sleep(1)
            except Exception as e:  # keep going; report
                failed.append(f"{rel}: {type(e).__name__}: {e}")
        if cleaned.get("work_type") is None and fm.get("type") == "Web Clipping":
            cleaned["work_type"] = "article"
        cleaned.pop("cite_as", None)
        cleaned["cite_as"] = citation.chicago(cleaned, fm.get("title") or rel.rsplit("/", 1)[-1][:-3], url)
        st = {k: v for k, v in cleaned.items() if not same(fm.get(k), v)}
        un = [k for k in citation.KEYS if k in fm and k not in cleaned]
        if st or un:
            items.append({"path": rel, "set": st, "unset": un})
            stats["keys_set"] += len(st)
            stats["keys_removed"] += len(un)
    with open(a.plan, "w", encoding="utf-8") as f:
        json.dump({"actor": "process:url-intake", "log": f"Citation clean-up ({len(items)} notes)",
                   "items": items}, f, indent=1, ensure_ascii=False, default=str)
    print(f"{stats['notes']} notes checked, {len(items)} would change "
          f"({stats['keys_set']} properties set, {stats['keys_removed']} empty ones removed); "
          f"{stats['fetched']} pages read for metadata. Plan: {a.plan}")
    if left:
        print(f"{left} more notes not included (batch limit {a.max}); run again after applying.")
    for x in failed[:20]:
        print("  metadata not available: " + x)


if __name__ == "__main__":
    main()
