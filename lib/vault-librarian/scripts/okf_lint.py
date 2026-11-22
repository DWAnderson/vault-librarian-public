#!/usr/bin/env python3
"""OKF v0.2 compliance check for an Obsidian vault (runs locally; costs no tokens).

Usage:
  python okf_lint.py <vault> [--strict] [--paths a.md b.md ...] [--json out.json]

Default ("vault") mode follows the vault-librarian convention: OKF frontmatter is
required, but Obsidian [[wikilinks]] are allowed inside the vault because
okf_export.py converts them. --strict applies the full OKF v0.2 spec, so any wikilink
is an error; use it on an exported bundle.

Also reports orphans (INFO): notes with no links in or out, ignoring links from
index.md, log.md and Topic Map hub notes. Skip with --no-orphans.

Levels: ERROR = not OKF-conformant; WARN = recommended field missing or a link
problem; INFO = worth a look (e.g. stale content). Exit code 1 if any ERROR.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import (RESERVED, WIKILINK_RE, MDLINK_RE, Resolver, mask_code,
                      parse_wikilink, split_frontmatter, walk)

ACTOR_RE = re.compile(r"^(human:\S+|process:\S+|[^\s:/]+/\S+)$")
STATUS = {"draft", "stable", "deprecated"}
# Shared citation property set (url-to-vault skill; Web Clipper "Article with citation" template)
CITATION = ["author", "publication", "publisher", "published", "accessed", "work_type", "doi", "journal",
            "volume", "issue", "pages", "section", "language", "word_count", "duration", "season",
            "episode", "platform", "archived_url", "cite_as", "captured_via", "summary_by", "audio_url"]


def as_dt(v):
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day, tzinfo=dt.timezone.utc)
    if isinstance(v, str):
        try:
            d = dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def check_actor(val, field, add):
    if not isinstance(val, str) or not ACTOR_RE.match(val.strip()):
        add("WARN", f"{field} {val!r} should be human:<id>, process:<id>, or <tool>/<version>")


def lint_frontmatter(fm, add):
    t = fm.get("type")
    if not isinstance(t, str) or not t.strip():
        add("ERROR", "missing or empty `type` (the one required OKF field)")
    if not fm.get("description"):
        add("WARN", "no `description` (recommended; used by index.md)")
    gen = fm.get("generated")
    if gen is None:
        add("WARN", "no `generated: { by, at }` (OKF v0.2 provenance)")
    elif not isinstance(gen, dict) or "by" not in gen:
        add("WARN", "`generated` must be a mapping with `by` (and `at`)")
    else:
        check_actor(gen.get("by"), "generated.by", add)
        if gen.get("at") is None:
            add("WARN", "`generated` has no `at` datetime")
        elif as_dt(gen.get("at")) is None:
            add("WARN", "generated.at is not an ISO 8601 datetime")
    if "timestamp" in fm:
        add("WARN", "`timestamp` is superseded in OKF v0.2 by `generated.at`")
    ver = fm.get("verified")
    if ver is not None:
        for ev in (ver if isinstance(ver, list) else [ver]):
            if not isinstance(ev, dict) or "by" not in ev:
                add("WARN", "`verified` entries need `by` and `at`")
            else:
                check_actor(ev.get("by"), "verified.by", add)
    st = fm.get("status")
    if st is not None and st not in STATUS:
        add("WARN", f"`status: {st}` is not one of draft | stable | deprecated")
    sa = fm.get("stale_after")
    if sa is not None:
        d = as_dt(sa)
        if d is None:
            add("WARN", "`stale_after` is not an ISO 8601 date/datetime")
        elif d <= dt.datetime.now(dt.timezone.utc):
            add("INFO", f"stale since {sa} — review, or update `stale_after`")
    src = fm.get("sources")
    if src is not None and not (isinstance(src, list) and all(isinstance(s, dict) and s.get("id") for s in src)):
        add("WARN", "`sources` should be a list of entries, each with an `id`")
    for k in CITATION:
        if k in fm and (fm[k] is None or fm[k] == "" or fm[k] == []):
            add("WARN", f"empty citation property `{k}` (Organize removes it)")
    for k in ("published", "accessed"):
        if fm.get(k) not in (None, "") and as_dt(fm[k]) is None:
            add("WARN", f"`{k}` is not an ISO 8601 date")
    if fm.get("author") not in (None, "", []) and not isinstance(fm.get("author"), list):
        add("WARN", "`author` should be a YAML list")
    tags = fm.get("tags")
    if tags is not None and not isinstance(tags, list):
        add("WARN", "`tags` should be a YAML list")


NAV_TYPES = {"Topic Map", "Agent Instructions", "Tag Vocabulary"}
ORPHAN_EXEMPT_TYPES = NAV_TYPES | {"Daily Note"}


def lint_links(rel, body, fm, resolver, strict, add, graph):
    scan = mask_code(body)
    out = graph.setdefault(rel, set())
    for m in WIKILINK_RE.finditer(scan):
        target, _, _ = parse_wikilink(m.group(2))
        if strict:
            add("ERROR", f"wikilink [[{m.group(2)}]] — OKF requires standard Markdown links")
        tgt, status = resolver.resolve(target)
        if tgt:
            out.add(tgt)
        if status == "missing":
            add("WARN", f"unresolved wikilink [[{m.group(2)}]]")
        elif status == "ambiguous":
            add("WARN", f"ambiguous wikilink [[{m.group(2)}]] — more than one file has that name")
    for m in MDLINK_RE.finditer(scan):
        href = m.group(3)
        if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I) or href.startswith("#"):
            continue
        path = unquote(href.split("#", 1)[0])
        if not path:
            continue
        if path.startswith("/"):
            cand = path.lstrip("/")
        else:
            cand = os.path.normpath(os.path.join(os.path.dirname(rel), path)).replace(os.sep, "/")
        if cand not in resolver.pathset:
            add("WARN", f"broken link target {href}")
        else:
            out.add(cand)
    if fm and isinstance(fm.get("sources"), list):
        ids = {s.get("id") for s in fm["sources"] if isinstance(s, dict)}
        for fn in sorted(set(re.findall(r"\[\^([^\]\s]+)\]", scan))):
            if fn not in ids and not re.search(r"^\[\^" + re.escape(fn) + r"\]:", body, re.M):
                add("WARN", f"footnote [^{fn}] matches no `sources` id or footnote definition")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--paths", nargs="*", help="only report on these vault-relative notes")
    ap.add_argument("--json")
    ap.add_argument("--no-orphans", action="store_true")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    files = list(walk(vault))
    resolver = Resolver([r for r, _ in files]).add_aliases(vault)
    only = {p.replace("\\", "/").lstrip("/") for p in (a.paths or [])}
    findings = []
    notes = 0
    graph, types = {}, {}
    for rel, ab in files:
        if not rel.lower().endswith(".md") or (only and rel not in only):
            continue
        notes += 1
        add = lambda lvl, msg, rel=rel: findings.append({"level": lvl, "file": rel, "msg": msg})
        with open(ab, encoding="utf-8", errors="replace") as f:
            text = f.read()
        fm, err, body = split_frontmatter(text)
        reserved = rel.rsplit("/", 1)[-1] in RESERVED
        if err:
            add("ERROR", f"frontmatter unreadable: {err}")
        elif fm is None and not reserved:
            add("ERROR", "no YAML frontmatter (OKF requires a `type` field)")
        elif fm is not None and not reserved:
            lint_frontmatter(fm, add)
        types[rel] = (fm or {}).get("type") if isinstance(fm, dict) else None
        lint_links(rel, body, fm, resolver, a.strict, add, graph)
    if not a.no_orphans:
        inbound = {}
        for src, tgts in graph.items():
            if src.rsplit("/", 1)[-1] in RESERVED or types.get(src) in NAV_TYPES:
                continue
            for t in tgts:
                if t != src:
                    inbound.setdefault(t, set()).add(src)
        for rel, tgts in graph.items():
            if rel.rsplit("/", 1)[-1] in RESERVED or types.get(rel) in ORPHAN_EXEMPT_TYPES:
                continue
            if rel.startswith(("Clippings/", "Inbox/")):
                continue  # unprocessed intake; Organize handles these
            if not inbound.get(rel) and not (tgts - {rel}):
                findings.append({"level": "INFO", "file": rel,
                                 "msg": "orphan — no links to or from other notes"})
    for name, paths in sorted(resolver.duplicate_notes().items()):
        if not only or only & set(paths):
            findings.append({"level": "WARN", "file": ", ".join(paths),
                             "msg": f"duplicate note name '{name}' — wikilinks to it are ambiguous"})
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    findings.sort(key=lambda x: (order[x["level"]], x["file"]))
    counts = {k: sum(1 for x in findings if x["level"] == k) for k in order}
    for x in findings:
        print(f"{x['level']:5} {x['file']}: {x['msg']}")
    mode = "strict OKF v0.2" if a.strict else "vault mode: wikilinks allowed"
    print(f"\n{notes} notes checked ({mode}) — {counts['ERROR']} errors, "
          f"{counts['WARN']} warnings, {counts['INFO']} info")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"counts": counts, "notes": notes, "findings": findings}, f, indent=1)
    sys.exit(1 if counts["ERROR"] else 0)


if __name__ == "__main__":
    main()
