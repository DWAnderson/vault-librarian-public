#!/usr/bin/env python3
"""Find web captures that may be partial (paywall, sign-in, teaser, wrong page). Report only.

Usage:
  python find_partials.py <vault> [--json out.json] [--vault-name DWA] [--all]

Looks at Web Clipping / Web Summary / Article notes with an http(s) `resource`, outside Archives,
that the owner hasn't already judged (`capture_checked` set; `--all` ignores that). Each hit is
  likely   - paywall / sign-in wording near the end of the body, or a Web Clipper stub
             (title = the site's name, little text)
  possible - a short Web Clipping (< 500 words) from a sign-in or paywalled site, or any
             Web Clipping under 250 words
  other    - not a sign-in problem but probably not the article: search-results pages,
             app/home pages, audio/video pages captured as text
with a clickable https link that opens the note in Obsidian (`link`, via this PC's vault server /open page;
`open` is the raw obsidian:// URL) and the source URL, so the owner can compare them.

Record the owner's verdict with apply_plan.py, `set: {capture_checked: <value>}`:
  complete                 - the capture is fine; never asked again
  recaptured               - the old note after a signed-in re-capture (also archived)
  issue: <short reason>    - not a sign-in problem; see modes/vault-queue.md, step P4
(added 2026-10-06, the owner)
"""
import argparse
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import is_archived, split_frontmatter, walk

TYPES = {"Web Clipping", "Web Summary", "Article"}
PAYWALL = re.compile(
    r"subscribe (now )?to (continue|read|keep reading|unlock)|sign in to (continue|read|keep)|log ?in to (continue|read)|"
    r"already a (paid )?subscriber|this (post|article|story) is for (paid )?subscribers|to continue reading|"
    r"create a free account|keep reading with a|upgrade to (paid|premium)|subscribers only|members only|"
    r"register to (continue|read)|you have (reached|read) your (free )?(article )?limit", re.I)
SIGNIN_HOSTS = ("nytimes.com", "wsj.com", "ft.com", "economist.com", "thefp.com", "thedispatch.com",
                "bloomberg.com", "washingtonpost.com", "theatlantic.com", "newyorker.com", "wired.com",
                "foreignaffairs.com", "nationalreview.com", "reason.com", "hbr.org", "medium.com",
                "theinformation.com", "stratechery.com", "app.therundown.ai", "law360.com", "barrons.com",
                "latimes.com", "chicagotribune.com", "seekingalpha.com", "substack.com")
# Sites Claude's browsers refuse to open ("not allowed due to safety restrictions"): The owner re-clips these himself
# with the Obsidian Web Clipper while signed in (decided 2026-10-06). Add a host when the browser refuses one.
BLOCKED_HOSTS = ("nytimes.com",)

OTHER = [(re.compile(r"//(www\.)?google\.[a-z.]+/search|//(www\.)?bing\.com/search|duckduckgo\.com/\?q="),
          "a search-results page, not an article"),
         (re.compile(r"youtube\.com/watch|youtu\.be/|vimeo\.com/\d|podcasts\.apple\.com|open\.spotify\.com"),
          "an audio/video page captured as text (belongs in the Remember video/podcast route)")]


def host(url):
    h = urllib.parse.urlsplit(url).hostname or ""
    return h[4:] if h.startswith("www.") else h


def signin_host(h):
    return any(h == s or h.endswith("." + s) for s in SIGNIN_HOSTS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("--json")
    ap.add_argument("--vault-name")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--web-base", default="http://127.0.0.1:8766/vault",
                    help="the app's vault server; its /open?f= page opens the note in Obsidian from a clickable https link")
    a = ap.parse_args()
    vname = a.vault_name or os.path.basename(os.path.abspath(a.vault))
    hits = []
    for rel, ab in walk(a.vault):
        if not rel.endswith(".md") or is_archived(rel) or rel.split("/")[0] in ("Claude", "Concepts"):
            continue
        try:
            fm, err, body = split_frontmatter(open(ab, encoding="utf-8").read())
        except (OSError, UnicodeDecodeError):
            continue
        if not fm or str(fm.get("type")) not in TYPES:
            continue
        url = str(fm.get("resource") or "")
        if not url.startswith("http") or (fm.get("capture_checked") and not a.all):
            continue
        t, h = str(fm["type"]), host(url)
        words = len(re.sub(r"(?m)^>.*$", "", body).split())       # callouts (the owner's note) don't count
        title = str(fm.get("title") or rel.rsplit("/", 1)[-1][:-3])
        level = why = None
        for rx, msg in OTHER:
            if rx.search(url):
                level, why = "other", msg
        if not level and PAYWALL.search(body[-3000:]):
            level, why = "likely", "paywall or sign-in wording at the end of the text"
        elif not level and t == "Web Clipping" and words < 300 and title.strip().lower() in (
                str(fm.get("publication") or "").strip().lower(), h.split(".")[0]):
            level, why = "likely", "Web Clipper stub (titled with the site's name, little text)"
        elif not level and t == "Web Clipping" and words < 500 and signin_host(h):
            level, why = "possible", f"only {words} words from a sign-in / paywalled site ({h})"
        elif not level and t == "Web Clipping" and words < 250:
            level, why = "possible", f"only {words} words"
        if level in ("likely", "possible") and any(h == b or h.endswith("." + b) for b in BLOCKED_HOSTS):
            level, why = "reclip", f"{h} can't be opened by Claude's browser: re-clip it yourself ({why})"
        if level:
            plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
            plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
            plain = re.sub(r"(?s)```.*?```", " ", plain).split()
            hits.append({"start": " ".join(plain[:45]) + ("…" if len(plain) > 45 else ""),
                         "end": ("…" + " ".join(plain[-45:])) if len(plain) > 90 else "","level": level, "why": why, "note": rel, "title": title, "words": words, "url": url,
                         "open": "obsidian://open?vault=" + urllib.parse.quote(vname) + "&file=" +
                                 urllib.parse.quote(rel[:-3]),
                         "link": a.web_base.rstrip("/") + "/open?f=" + urllib.parse.quote(rel[:-3])})
    order = {"reclip": 0, "likely": 1, "possible": 2, "other": 3}
    hits.sort(key=lambda x: (order[x["level"]], x["words"]))
    for x in hits:
        print(f"{x['level']:8} {x['words']:5}w  {x['note']}  |  {x['why']}")
    print(f"{len(hits)} to review ({sum(x['level'] == 'likely' for x in hits)} likely, "
          f"{sum(x['level'] == 'possible' for x in hits)} possible, {sum(x['level'] == 'other' for x in hits)} other, "
          f"{sum(x['level'] == 'reclip' for x in hits)} to re-clip yourself)")
    if a.json:
        json.dump(hits, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
