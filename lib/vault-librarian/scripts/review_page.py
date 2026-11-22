#!/usr/bin/env python3
"""Build the partial-capture review page (an Artifact) from find_partials.py's JSON. Runs locally, so
note text never passes through the conversation (2026-10-06).

Usage:
  python review_page.py partials.json out.html [--reads reads.json]

The page is assets/capture-review.template.html with the items filled in: per note an "Open in Obsidian"
link (This PC /vault/open), the source link, the opening and closing words, a one-line read, and verdict
buttons that save to the artifact's database (collection `verdicts`, doc id n<k>: verdict, note, note_path).
`--reads` optionally maps note path -> Claude's one-line read; otherwise a read is made from simple signs.
"""
import argparse
import datetime as dt
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "assets", "capture-review.template.html")


def auto_read(x):
    end = x.get("end") or x.get("start") or ""
    if x["level"] == "reclip":
        return ("Claude's browser can't open this site, so re-clip it yourself: open the source page in your own browser, "
                "sign in if asked, and use the Obsidian Web Clipper. Then press \"I've re-clipped it\": Claude archives "
                "this copy and checks the new one."), True
    if x["level"] == "other":
        return x["why"][:1].upper() + x["why"][1:] + ". Keep it, archive it, or say what you'd rather have.", True
    if x["level"] == "likely":
        return "Likely cut off: " + x["why"] + ".", True
    if re.search(r"Source:\s*<?https?://", end) or re.search(r"[.!?)\"']\s*$", end.rstrip("…").rstrip()):
        return "Looks complete: the text ends cleanly. Open it if the subject usually runs longer.", False
    return "Unclear: the text doesn't end on a full sentence. Worth opening.", True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("partials")
    ap.add_argument("out")
    ap.add_argument("--reads")
    a = ap.parse_args()
    hits = json.load(open(a.partials, encoding="utf-8"))
    reads = json.load(open(a.reads, encoding="utf-8")) if a.reads else {}
    items = []
    for i, x in enumerate(hits, 1):
        r, warn = (reads[x["note"]], True) if x["note"] in reads else auto_read(x)
        it = {"k": f"n{i}", "n": i, "title": x["title"], "note": x["note"], "words": x["words"], "level": x["level"],
              "why": x["why"], "link": x["link"], "url": x["url"], "start": x.get("start", ""), "end": x.get("end", ""),
              "read": r, "warn": warn}
        if x["level"] == "other":
            it["other"] = True
        if x["level"] == "reclip":
            it["reclip"] = True
        items.append(it)
    page = open(TEMPLATE, encoding="utf-8").read()
    page = page.replace("/*ITEMS*/[]", json.dumps(items, ensure_ascii=False).replace("</", "<\\/"))
    page = page.replace("/*DATE*/", dt.date.today().strftime("%B %-d, %Y") if os.name != "nt" else dt.date.today().strftime("%B %d, %Y"))
    open(a.out, "w", encoding="utf-8").write(page)
    print(f"{len(items)} item(s) -> {a.out}")


if __name__ == "__main__":
    main()
