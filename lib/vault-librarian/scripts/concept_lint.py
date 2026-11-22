#!/usr/bin/env python3
"""Weekly check of the concept layer (runs locally; costs no tokens). Reports, never changes anything.

  - likely duplicate concepts: same name once plurals, case and punctuation are set aside; one name inside
    the other; very similar spelling; or mostly the same words. Each pair gets a suggested keeper
    (the one more analyses use; ties: the older). Pairs the owner has said are different are skipped.
  - concepts only one analysis uses, older than --stale-days
  - concepts no analysis uses
  - new concepts still waiting for a second use after --stale-days (from the pending file)
  - analyses whose item is gone (analysis_of doesn't resolve)

Usage:
  python concept_lint.py <vault> [--pending concepts.json] [--stale-days 28] [--json out.json]
"""
import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import RESERVED, is_archived, split_frontmatter, walk

ANALYSIS_TYPES = {"Analysis", "Part Analysis"}
STOP = {"the", "of", "and", "a", "an", "in", "on", "to", "for", "by", "theory", "concept", "principle", "effect"}


def norm(s):
    s = re.sub(r"\(concept\)$", "", str(s).lower()).strip()
    words = [w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in re.findall(r"[a-z0-9]+", s)]
    return " ".join(words)


def words(s):
    return {w for w in norm(s).split() if w not in STOP}


def why_same(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return None
    if na == nb:
        return "same name apart from case, plural or punctuation"
    if re.search(r"\b" + re.escape(na) + r"\b", nb) or re.search(r"\b" + re.escape(nb) + r"\b", na):
        return "one name contains the other"
    r = difflib.SequenceMatcher(None, na, nb).ratio()
    if r >= 0.88:
        return f"very similar spelling ({r:.0%})"
    wa, wb = words(a), words(b)
    if wa and wb and len(wa & wb) / len(wa | wb) >= 0.6:
        return "mostly the same words"
    return None


def when(fm):
    g = fm.get("generated")
    try:
        d = dt.datetime.fromisoformat(str(g.get("at"))) if isinstance(g, dict) else None
        return d if d is None or d.tzinfo else d.astimezone()
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--pending")
    ap.add_argument("--stale-days", type=int, default=28)
    ap.add_argument("--json")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    now = dt.datetime.now().astimezone()
    stale = dt.timedelta(days=a.stale_days)
    names, concepts, analyses = set(), {}, []
    for rel, ab in walk(vault):
        if not rel.lower().endswith(".md") or rel.rsplit("/", 1)[-1] in RESERVED:
            continue
        name = rel.rsplit("/", 1)[-1][:-3]
        names.add(name)
        with open(ab, encoding="utf-8", errors="replace") as f:
            fm, _, _ = split_frontmatter(f.read())
        if not isinstance(fm, dict):
            continue
        if fm.get("type") == "Concept" and not is_archived(rel):
            al = fm.get("aliases") or []
            concepts[name] = {"rel": rel, "aliases": [str(x) for x in (al if isinstance(al, list) else [al])],
                              "at": when(fm), "uses": 0}
        elif fm.get("type") in ANALYSIS_TYPES:
            m = re.match(r"\[\[([^\]|#]+)", str(fm.get("analysis_of") or ""))
            analyses.append({"name": name, "of": m.group(1).strip() if m else None,
                             "uses": [str(c) for c in (fm.get("concepts") or [])]})
    key = {}
    for c, d in concepts.items():
        for k in [c] + d["aliases"]:
            key[k.lower()] = c
    for an in analyses:
        for u in {key.get(u.lower()) for u in an["uses"]} - {None}:
            concepts[u]["uses"] += 1
    pend = {}
    if a.pending and os.path.exists(a.pending):
        with open(a.pending, encoding="utf-8") as f:
            pend = json.load(f)
    apart = {tuple(sorted(p)) for p in pend.get("not_duplicates") or []}
    dups, cn = [], sorted(concepts)
    for i, x in enumerate(cn):
        for y in cn[i + 1:]:
            if tuple(sorted([x, y])) in apart:
                continue
            why = why_same(x, y) or next((why_same(p, q) for p in [x] + concepts[x]["aliases"]
                                          for q in [y] + concepts[y]["aliases"] if why_same(p, q)), None)
            if not why:
                continue
            ux, uy = concepts[x]["uses"], concepts[y]["uses"]
            ax, ay = concepts[x]["at"] or now, concepts[y]["at"] or now
            keep, merge = (x, y) if (ux, -ax.timestamp()) >= (uy, -ay.timestamp()) else (y, x)
            dups.append({"keep": keep, "merge": merge, "why": why,
                         "keep_uses": concepts[keep]["uses"], "merge_uses": concepts[merge]["uses"]})
    single = [c for c, d in concepts.items() if d["uses"] == 1 and d["at"] and now - d["at"] > stale]
    unused = [c for c, d in concepts.items() if d["uses"] == 0]
    waiting = []
    for c, p in (pend.get("pending") or {}).items():
        try:
            added = dt.datetime.fromisoformat(p.get("added") or "")
        except ValueError:
            continue
        if len(p.get("analyses") or []) < 2 and now - (added if added.tzinfo else added.astimezone()) > stale:
            waiting.append(c)
    broken = [an["name"] for an in analyses if an["of"] and an["of"] not in names]
    res = {"concepts": len(concepts), "analyses": len(analyses), "duplicates": dups, "single_use": sorted(single),
           "unused": sorted(unused), "waiting": sorted(waiting), "broken": sorted(broken)}
    print(f"{len(concepts)} concepts, {len(analyses)} analyses")
    print(f"likely duplicates: {len(dups)}" + "".join(f"\n  {d['merge']} -> {d['keep']} ({d['why']})" for d in dups))
    print(f"used by only one analysis for over {a.stale_days} days: {len(single)}" + ("" if not single else ": " + ", ".join(sorted(single))))
    print(f"used by no analysis: {len(unused)}" + ("" if not unused else ": " + ", ".join(sorted(unused))))
    print(f"new concepts still waiting for a second use after {a.stale_days} days: {len(waiting)}"
          + ("" if not waiting else ": " + ", ".join(sorted(waiting))))
    print(f"analyses whose item is gone: {len(broken)}" + ("" if not broken else ": " + ", ".join(sorted(broken))))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
