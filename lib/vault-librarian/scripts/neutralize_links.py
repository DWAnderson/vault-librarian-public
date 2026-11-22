#!/usr/bin/env python3
"""Backtick unresolved [[wikilinks]] in Claude-generated notes so Obsidian doesn't render dead links.

Only notes whose frontmatter `generated.by` starts with `claude/` or `process:` are changed.
In any other note (the owner's own), unresolved links are reported and never changed.
Resolving and ambiguous links are left as they are. Links already inside code are ignored.
Dry run by default; pass --apply to write the changes.

Usage:
  python3 neutralize_links.py <vault> [--paths REL ...] [--folders TOP ...] [--apply]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import WIKILINK_RE, Resolver, mask_code, parse_wikilink, split_frontmatter, walk

GENERATED_PREFIXES = ("claude/", "process:")


def is_generated(fm):
    g = (fm or {}).get("generated")
    by = g.get("by") if isinstance(g, dict) else None
    return isinstance(by, str) and by.startswith(GENERATED_PREFIXES)


def fix_text(text, resolver):
    """Return (new_text, [(line, link)]) with unresolved wikilinks in the body wrapped in backticks."""
    fm, err, body = split_frontmatter(text)
    head = text[: len(text) - len(body)]
    scan = mask_code(body)
    changes, out, last = [], [], 0
    for m in WIKILINK_RE.finditer(scan):
        target, _, _ = parse_wikilink(m.group(2))
        _, status = resolver.resolve(target)
        if status != "missing":
            continue
        link = body[m.start():m.end()]
        line = head.count("\n") + body.count("\n", 0, m.start()) + 1
        changes.append((line, link))
        out.append(body[last:m.start()])
        out.append("`" + link + "`")
        last = m.end()
    out.append(body[last:])
    return fm, err, head + "".join(out), changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--paths", nargs="*", help="vault-relative notes to check")
    ap.add_argument("--folders", nargs="*", help="vault-relative folders to check (recursive)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    files = list(walk(vault))
    resolver = Resolver([r for r, _ in files]).add_aliases(vault)
    only = {p.replace("\\", "/").strip("/") for p in (a.paths or [])}
    folders = [f.replace("\\", "/").strip("/") + "/" for f in (a.folders or [])]
    fixed = reported = notes = 0
    for rel, ab in files:
        if not rel.lower().endswith(".md"):
            continue
        if (only or folders) and not (rel in only or any(rel.startswith(f) for f in folders)):
            continue
        notes += 1
        with open(ab, encoding="utf-8", newline="") as f:
            text = f.read()
        fm, err, new, changes = fix_text(text, resolver)
        if not changes:
            continue
        if err or not is_generated(fm):
            for line, link in changes:
                print(f"REPORT {rel}:{line}  unresolved {link} (not Claude-generated; left unchanged)")
                reported += 1
            continue
        for line, link in changes:
            print(f"{'FIXED ' if a.apply else 'WOULD FIX'} {rel}:{line}  {link} -> `{link}`")
            fixed += 1
        if a.apply:
            with open(ab, "w", encoding="utf-8", newline="") as f:
                f.write(new)
    verb = "backticked" if a.apply else "would backtick"
    print(f"\n{notes} notes checked — {verb} {fixed} link(s); {reported} reported in non-generated notes")


if __name__ == "__main__":
    main()
