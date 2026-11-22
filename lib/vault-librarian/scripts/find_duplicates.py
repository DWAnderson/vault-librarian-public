#!/usr/bin/env python3
"""Find likely duplicate notes (runs locally; proposals only — nothing is changed).

Signals: same `resource` URL (ignoring scheme, www, trailing slash, tracking params);
near-identical titles/names; near-identical content (word-shingle similarity, default
>= 0.8). Reserved files, Topic Maps and templates are ignored.

Usage:
  python find_duplicates.py <vault> [--threshold 0.8] [--json out.json] [--plan archive.json]

STRONG matches (same resource URL, or >= 95% identical content) can be archived
automatically: --plan writes an apply_plan.py plan that keeps the fuller copy of each
group and moves the others to Archives with status: deprecated, duplicate_of (the
keeper) and archived_from (the original folder), so resolve_duplicates.py can later
restore, merge or delete them. Weaker matches (similar title, 80-95% content) are listed
for the owner to decide.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from urllib.parse import urlsplit, parse_qsl, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import RESERVED, is_archived, junk_title, split_frontmatter, vault_rel, walk

K, BANDS, ROWS = 5, 16, 2


def norm_url(u):
    p = urlsplit(u.strip())
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))])
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


def norm_title(t):
    t = re.sub(r"\s*\|\s*[^|]{2,40}$", "", t)  # drop trailing " | Site Name"
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def generic_url(n):
    """A site's home or index page (e.g. www.law.uchicago.edu/Lawecon/index.html), not one work's own address.
    PDFs often carry their series' web page; two different papers then share it (2026-10-01)."""
    path = n.split("?", 1)[0].partition("/")[2].strip("/")
    last = path.rsplit("/", 1)[-1].lower()
    return not path or re.fullmatch(r"(index|default|home|main)(\.\w{2,5})?", last) is not None


PART_WORD = re.compile(r"\b(part|pt|chapter|ch|chap|section|sec|volume|vol|book|episode|ep|lecture|lesson|session|module)\b")


def title_numbers(t):
    """(title without its numbers or part words, the numbers in order) - for "Book - Part 03 - 3" style names."""
    nums = tuple(int(x) for x in re.findall(r"\d+", t))
    words = re.sub(r"\s+", " ", PART_WORD.sub(" ", re.sub(r"\d+", " ", t))).strip()
    return words, nums


def distinct_parts(a, b):
    """Two different parts / chapters / volumes of one work, not two copies of the same thing (2026-10-08:
    the morning email asked about 15 pairs of "Austrian Economics - Part NN" notes). True when both notes say
    which part they are (`part`) of the same parent and the numbers differ, or when the titles match apart from
    their numbers and both carry numbers that differ. "Work" vs "Work 2" is NOT treated as parts (one has no
    number) - that is how second copies are often named."""
    if a["part"] is not None and b["part"] is not None and a["part"] != b["part"] and a["parent"] == b["parent"]:
        return True
    wa, na = title_numbers(a["title"])
    wb, nb = title_numbers(b["title"])
    return bool(na) and bool(nb) and na != nb and wa == wb


def jaccard(s1, s2):
    return len(s1 & s2) / len(s1 | s2) if (s1 | s2) else 0.0


PART_OVERLAP = 0.3   # parts are asked about only when this much of their wording (5-word phrases) is shared


def shingles(text):
    w = re.findall(r"[a-z0-9]+", text.lower())
    return {hashlib.md5(" ".join(w[i:i + K]).encode()).digest()[:8] for i in range(max(1, len(w) - K + 1))} if w else set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--json")
    ap.add_argument("--plan", help="write an auto-archive plan for STRONG duplicates")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    notes = []
    for rel, ab in walk(vault):
        if not rel.lower().endswith(".md") or rel.rsplit("/", 1)[-1] in RESERVED:
            continue
        with open(ab, encoding="utf-8", errors="replace") as f:
            fm, _, body = split_frontmatter(f.read())
        fm = fm if isinstance(fm, dict) else {}
        if fm.get("type") in ("Topic Map", "Agent Instructions", "Tag Vocabulary") or fm.get("status") == "deprecated":
            continue
        # A meaningless metadata title ("No Job Name", "x.indd") falls back to the note's name; a meaningless
        # name too means no title match at all (2026-10-01: unrelated papers sharing such titles were asked about).
        title = str(fm.get("title") or "")
        if junk_title(title):
            title = rel.rsplit("/", 1)[-1][:-3]
        if junk_title(title):
            title = ""
        # A `Transcript` note shares its summary note's `resource` and title by design (2026-09-28 fix:
        # Nightly archived 12 Remember/podcast summaries as "duplicates" of their own transcripts).
        # Likewise an "<item> - Analysis" / Part Analysis note written by the concept-analysis job sits
        # beside its item with a near-identical title and often the same `resource` (2026-09-29 fix:
        # the morning email asked about 3 note/Analysis pairs as "possible duplicates").
        base = rel.rsplit("/", 1)[-1][:-3]
        if fm.get("type") in ("Transcript", "Podcast Transcript"):
            role = "transcript"
        elif fm.get("type") in ("Analysis", "Part Analysis") or base.endswith((" - Analysis", " (Analysis)")):
            role = "analysis"
        else:
            role = "main"
        notes.append({"role": role, "keep": bool(fm.get("not_a_duplicate")), "nkeys": len(fm), "blen": len(body.strip()), "rel": rel, "title": norm_title(title), "res": norm_url(fm["resource"]) if isinstance(fm.get("resource"), str) and "://" in fm["resource"] else None,
                      "from_file": bool(fm.get("source_file")),
                      "part": str(fm.get("part")).strip() if fm.get("part") not in (None, "") else None,
                      "parent": str(fm.get("part_of") or fm.get("analysis_of") or "").strip(),
                      "sh": shingles(body)})
    pairs, strong = {}, set()

    def add(i, j, why, is_strong=False):
        if notes[i]["role"] != notes[j]["role"]:
            return                      # a note and its Transcript / Analysis companion are a pair, not duplicates
        if not is_strong and notes[i]["keep"] and notes[j]["keep"]:
            return                      # The owner said "not duplicates" for both: never ask again (2026-10-01 fix)
        if distinct_parts(notes[i], notes[j]) and jaccard(notes[i]["sh"], notes[j]["sh"]) < PART_OVERLAP:
            return                      # different parts of one work whose texts aren't alike (2026-10-08 fix)
        key = tuple(sorted((notes[i]["rel"], notes[j]["rel"])))
        pairs.setdefault(key, set()).add(why)
        if is_strong:
            strong.add(key)

    by_res = {}
    for i, n in enumerate(notes):
        if n["res"]:
            by_res.setdefault(n["res"], []).append(i)
    for res, idx in by_res.items():
        if generic_url(res):
            continue                    # a site's home/index page says nothing about which work a note is
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                i, j = idx[x], idx[y]
                if notes[i]["from_file"] or notes[j]["from_file"]:
                    # a URL found inside a PDF is weak evidence: also need the texts to overlap
                    s1, s2 = notes[i]["sh"], notes[j]["sh"]
                    jac = len(s1 & s2) / len(s1 | s2) if (s1 | s2) else 0
                    if jac < 0.5:
                        continue
                add(i, j, "same resource URL", True)
    order = sorted(range(len(notes)), key=lambda i: notes[i]["title"])
    for x in range(len(order)):
        for y in range(x + 1, min(x + 6, len(order))):
            i, j = order[x], order[y]
            if notes[i]["title"] and difflib.SequenceMatcher(None, notes[i]["title"], notes[j]["title"]).ratio() >= 0.92:
                add(i, j, "near-identical title")
    buckets = {}
    for i, n in enumerate(notes):
        if len(n["sh"]) < 8:
            continue
        sig = [min(int.from_bytes(hashlib.md5(s + bytes([h])).digest()[:4], "big") for s in n["sh"]) for h in range(BANDS * ROWS)]
        for b in range(BANDS):
            buckets.setdefault((b, tuple(sig[b * ROWS:(b + 1) * ROWS])), []).append(i)
    checked = set()
    for idx in buckets.values():
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                i, j = idx[x], idx[y]
                if (i, j) in checked:
                    continue
                checked.add((i, j))
                s1, s2 = notes[i]["sh"], notes[j]["sh"]
                jac = len(s1 & s2) / len(s1 | s2)
                if jac >= a.threshold:
                    add(i, j, f"content {jac:.0%} similar", jac >= 0.95)
    out = [{"notes": list(k), "why": sorted(v), "strong": k in strong} for k, v in sorted(pairs.items())]
    for o in out:
        print(f"{'STRONG' if o['strong'] else 'weak  '} {o['notes'][0]}  <->  {o['notes'][1]}   ({'; '.join(o['why'])})")
    if a.plan:
        info = {n["rel"]: n for n in notes}
        parent = {}

        def find(x):
            while parent.get(x, x) != x:
                x = parent[x]
            return x
        for k in list(strong):
            if info[k[0]].get("keep") or info[k[1]].get("keep"):
                strong.discard(k)  # The owner restored one of these: never auto-archive the pair again
        for k in strong:
            ra, rb = find(k[0]), find(k[1])
            if ra != rb:
                parent[ra] = rb
        groups = {}
        for k in strong:
            for r in k:
                groups.setdefault(find(r), set()).add(r)

        def score(r):
            n, base = info[r], r.rsplit("/", 1)[-1]
            return (not is_archived(r), n["nkeys"], n["blen"],
                    not re.search(r"\(\d+\)\.md$", base), not re.match(r"\d{4}-\d{2}-\d{2}_", base), -len(base))
        items = []
        for members in groups.values():
            keeper = max(members, key=score)
            for r in sorted(members - {keeper}):
                if is_archived(r):
                    continue
                items.append({"path": r, "move_to": vault_rel(vault, "Archives"),
                              "set": {"status": "deprecated", "duplicate_of": f"[[{keeper[:-3]}]]",
                                      "archived_from": r.rsplit("/", 1)[0] if "/" in r else ""}})
                print(f"  auto-archive: {r}  (keep {keeper})")
        with open(a.plan, "w", encoding="utf-8") as f:
            json.dump({"log": f"Auto-archived {len(items)} strong duplicates", "items": items}, f, indent=1)
    print(f"\n{len(out)} likely duplicate pairs among {len(notes)} notes")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
