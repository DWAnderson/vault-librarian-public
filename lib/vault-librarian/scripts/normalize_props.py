#!/usr/bin/env python3
"""Property clean-up: map older/variant frontmatter names onto the OKF v0.2 convention and
write an apply_plan.py plan for the owner to approve (runs locally; changes nothing itself).

Usage:
  python normalize_props.py <vault> --plan plan.json [--actor human:<owner>] [--keep-originals]
Then show the owner the summary, and on approval:  python apply_plan.py <vault> plan.json

Mappings (only where the OKF field is missing):
  updated / modified / last_modified / updated_at  -> generated.at   (preferred)
  created / date / created_at / creation_date      -> generated.at   (fallback)
      generated.by = --actor (default: VAULT_OWNER_ID, else human:owner; tool/obsidian-web-clipper for Web Clippings); the date moves, the old key is removed
      unless --keep-originals (`created` is always kept: OKF has no creation field)
  source / url / link / source_url / source-url    -> resource   (URL values only)
  summary / excerpt / abstract                     -> description
  status seedling|sprout|budding|growing|wip|in-progress -> draft
         evergreen|done|complete|final|published         -> stable
         archived|obsolete|superseded                    -> deprecated
Never mapped: `published` and the other citation keys (see the OKF set below).
Also reports (no mapping): every non-standard property name and how many notes use it,
and `timestamp` (v0.1) which it converts like `updated`.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import owner, RESERVED, split_frontmatter, walk

OKF = {"type", "title", "description", "resource", "tags", "generated", "verified", "status",
       "stale_after", "sources", "aliases", "okf_version", "project", "chat_date", "exported",
       "session_id", "source", "truncated", "chats", "cssclasses",
       # shared citation property set: kept as-is (`published` is the work's publication date,
       # never provenance); url-to-vault's citation_fill.py cleans and fills these
       "author", "publication", "publisher", "published", "accessed", "work_type", "doi", "journal",
            "volume", "issue", "pages", "section", "language", "word_count", "duration", "season",
            "episode", "platform", "archived_url", "cite_as", "captured_via", "summary_by", "audio_url"}
UPDATED = ["timestamp", "updated", "modified", "last_modified", "updated_at", "last-modified"]
CREATED = ["created", "date", "created_at", "creation_date", "created-at"]
RESOURCE = ["url", "link", "source_url", "source-url", "source"]
DESC = ["summary", "excerpt", "abstract"]
STATUS = {**{k: "draft" for k in "seedling sprout budding growing wip in-progress in_progress".split()},
          **{k: "stable" for k in "evergreen done complete completed final published".split()},
          **{k: "deprecated" for k in "archived obsolete superseded".split()}}


def to_iso(v):
    if isinstance(v, dt.datetime):
        d = v
    elif isinstance(v, dt.date):
        d = dt.datetime(v.year, v.month, v.day)
    elif isinstance(v, str):
        s = v.strip().replace("Z", "+00:00")
        d = None
        for fmt in (None, "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                d = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if d is None:
            return None
    else:
        return None
    if d.tzinfo is None:
        d = d.astimezone()  # assume local time
    return d.isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--actor", default=owner()["id"])
    ap.add_argument("--keep-originals", action="store_true")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    keycount = collections.Counter()
    items, unparsed, counts = [], [], collections.Counter()
    for rel, ab in walk(vault):
        if not rel.lower().endswith(".md") or rel.rsplit("/", 1)[-1] in RESERVED:
            continue
        with open(ab, encoding="utf-8", errors="replace") as f:
            fm, err, _ = split_frontmatter(f.read())
        if not isinstance(fm, dict) or not fm:
            continue
        keycount.update(k for k in fm if k not in OKF)
        st, un = {}, []
        if not fm.get("generated"):
            src = next((k for k in UPDATED + CREATED if fm.get(k)), None)
            if src:
                iso = to_iso(fm[src])
                if iso:
                    who = "tool/obsidian-web-clipper" if fm.get("type") == "Web Clipping" else a.actor
                    st["generated"] = {"by": who, "at": iso}
                    counts["generated from " + src] += 1
                    if src != "created" and not a.keep_originals:
                        un.append(src)
                else:
                    unparsed.append(f"{rel}: {src}={fm[src]!r}")
        elif "timestamp" in fm and not a.keep_originals:
            un.append("timestamp")
            counts["removed legacy timestamp (generated already set)"] += 1
        if not fm.get("resource"):
            src = next((k for k in RESOURCE if isinstance(fm.get(k), str) and re.match(r"https?://", fm[k].strip())), None)
            if src:
                st["resource"] = fm[src].strip()
                counts["resource from " + src] += 1
                if not a.keep_originals and not (src == "source" and fm.get("type") in ("Claude Chat", "Claude Chat History")):
                    un.append(src)
        if not fm.get("description"):
            src = next((k for k in DESC if isinstance(fm.get(k), str) and fm[k].strip()), None)
            if src:
                st["description"] = " ".join(fm[src].split())[:300]
                counts["description from " + src] += 1
                if not a.keep_originals:
                    un.append(src)
        s = fm.get("status")
        if isinstance(s, str) and s.lower() in STATUS:
            st["status"] = STATUS[s.lower()]
            counts[f"status {s} -> {STATUS[s.lower()]}"] += 1
        if st or un:
            items.append({"path": rel, "set": st, "unset": sorted(set(un))})
    with open(a.plan, "w", encoding="utf-8") as f:
        json.dump({"actor": a.actor, "log": f"Property clean-up ({len(items)} notes)", "items": items}, f, indent=1)
    print(f"{len(items)} notes would change. Plan written to {a.plan}\n")
    for k, n in counts.most_common():
        print(f"  {n:4}  {k}")
    if unparsed:
        print("\nDates not understood (left alone):")
        for u in unparsed[:30]:
            print("  " + u)
    other = [(k, n) for k, n in keycount.most_common() if k not in UPDATED + CREATED + RESOURCE + DESC]
    if other:
        print("\nOther non-OKF properties in use (kept; OKF allows custom keys):")
        print("  " + ", ".join(f"{k} ({n})" for k, n in other[:40]))


if __name__ == "__main__":
    main()
