#!/usr/bin/env python3
"""Find unlinked mentions: places where a note's text names another note (by file name or
alias) without linking to it. Proposals only — nothing is changed. Runs locally.

Usage:
  python find_mentions.py <vault> [--paths a.md ...] [--min-len 6] [--json out.json]

Output (per proposal): note, target note, the matched text, and a snippet. To apply the
ones the owner approves, add them to an apply_plan.py item as
  "link_mentions": [{"target": "Projects/Smith Renewal", "text": "Smith renewal"}]
which links the FIRST unlinked occurrence only.

Skipped: names shorter than --min-len characters (default 6) unless two or more words,
date-like names (daily notes), index/log files, frontmatter, code, and text already in links.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import MDLINK_RE, RESERVED, WIKILINK_RE, mask_code, split_frontmatter, walk

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def mask_links(text):
    blank = lambda m: " " * len(m.group(0))
    text = WIKILINK_RE.sub(blank, text)
    text = MDLINK_RE.sub(blank, text)
    return re.sub(r"https?://\S+", blank, text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--paths", nargs="*")
    ap.add_argument("--min-len", type=int, default=6)
    ap.add_argument("--min-words", type=int, default=2, help="ignore note names with fewer words (default 2)")
    ap.add_argument("--json")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    notes = {}
    for rel, ab in walk(vault):
        if rel.lower().endswith(".md") and rel.rsplit("/", 1)[-1] not in RESERVED:
            with open(ab, encoding="utf-8", errors="replace") as f:
                notes[rel] = f.read()
    names = {}  # lowercase name -> target rel (only unique names)
    clash = set()
    for rel, text in notes.items():
        fm, _, _ = split_frontmatter(text)
        if isinstance(fm, dict) and fm.get("type") in ("Agent Instructions", "Tag Vocabulary", "Topic Map"):
            continue
        cands = [rel.rsplit("/", 1)[-1][:-3]]
        al = (fm or {}).get("aliases") if isinstance(fm, dict) else None
        cands += [x for x in (al if isinstance(al, list) else [al] if al else []) if isinstance(x, str)]
        for n in cands:
            n = n.strip()
            if not n or DATE_RE.match(n) or len(n) < a.min_len or len(n.split()) < a.min_words:
                continue
            k = n.lower()
            if k in names and names[k] != rel:
                clash.add(k)
            names[k] = rel
    for k in clash:
        names.pop(k, None)
    if not names:
        print("No candidate note names.")
        return
    pattern = re.compile(r"(?<![\w\[])(" + "|".join(re.escape(k) for k in sorted(names, key=len, reverse=True)) + r")(?![\w\]])", re.I)
    only = {p.replace("\\", "/").lstrip("/") for p in (a.paths or [])}
    props = []
    for rel, text in notes.items():
        if only and rel not in only:
            continue
        _, _, body = split_frontmatter(text)
        scan = mask_links(mask_code(body))
        seen = set()
        for m in pattern.finditer(scan):
            tgt = names[m.group(1).lower()]
            if tgt == rel or tgt in seen:
                continue
            seen.add(tgt)
            s0, s1 = max(0, m.start() - 40), min(len(body), m.end() + 40)
            props.append({"note": rel, "target": tgt[:-3], "text": body[m.start():m.end()],
                          "snippet": " ".join(body[s0:s1].split())})
    for p in props:
        print(f"{p['note']}: \"{p['text']}\" -> [[{p['target']}]]   …{p['snippet']}…")
    print(f"\n{len(props)} unlinked mentions found")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(props, f, indent=1)


if __name__ == "__main__":
    main()
