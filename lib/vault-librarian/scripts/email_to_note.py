#!/usr/bin/env python3
"""Write emails (from Outlook Inbox\\Remember) into the vault's Inbox as OKF notes.

Usage:
  python email_to_note.py <vault> emails.json
emails.json: a list of
  {"id": "<Graph message id>", "subject": "...", "from": "a@b.com", "to": ["..."], "cc": ["..."],
   "received": "2026-09-24T17:59:55Z", "internet_message_id": "<...@...>",
   "web_link": "https://outlook.office365.com/...", "word_count": 412,
   "content": "full" | "summary",          # full = verbatim body; summary = long email condensed
   "description": "One sentence.",          # optional
   "attachments": ["file.pdf", ...],        # names only
   "body": "Markdown text (verbatim body, or summary + key facts + excerpts)"}
Writes Inbox/"YYYY-MM-DD <Subject> (<Sender>).md" (never overwrites; adds a number if the
name is taken) and prints one JSON line per email: {"id", "note", "ok"}. Only emails with
ok=true may be removed from the Remember folder.
"""
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import split_frontmatter

BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')


def yq(v):
    return json.dumps(v, ensure_ascii=False)


# The owner's properties (2026-09-30): every new source item ends its frontmatter with an empty `see_also` (list of
# [[links]]) and `my_note` (one-line take), unless it already has them; companions (Transcript, Analysis,
# Concept) don't get them. The analysis (the app's jobs/analyze.py) reads both.
OWNER_PROPS_SKIP = {"Transcript", "Podcast Transcript", "Analysis", "Part Analysis", "Concept", "Topic Map",
                   "Tag Vocabulary", "Agent Instructions", "Daily Note"}


def owner_props(ntype, present=()):
    """The frontmatter lines for the owner's two properties (last in the block), for keys not already present."""
    if ntype in OWNER_PROPS_SKIP:
        return []
    return [x for k, x in (("see_also", "see_also: []"), ("my_note", 'my_note: ""')) if k not in present]


def main():
    vault, src = os.path.abspath(sys.argv[1]), sys.argv[2]
    emails = json.load(open(src, encoding="utf-8"))
    inbox = os.path.join(vault, "Inbox")
    os.makedirs(inbox, exist_ok=True)
    for e in emails:
        try:
            rec = dt.datetime.fromisoformat(e["received"].replace("Z", "+00:00")).astimezone()
            sender = (e.get("from") or "unknown").split("@")[0]
            subj = re.sub(r"^(re|fw|fwd):\s*", "", (e.get("subject") or "(no subject)").strip(), flags=re.I)
            base = " ".join(BAD.sub(" ", f"{rec:%Y-%m-%d} {subj} ({sender})").split())[:110].strip(". ")
            name, n = base, 2
            while os.path.exists(os.path.join(inbox, name + ".md")):
                name, n = f"{base} {n}", n + 1
            mid = (e.get("internet_message_id") or "").strip("<>")
            fm = ["---", f"type: {yq('Email')}", f"title: {yq(e.get('subject') or '(no subject)')}"]
            if e.get("description"):
                fm.append(f"description: {yq(e['description'])}")
            fm += [f"from: {yq(e.get('from', ''))}", f"to: {yq(e.get('to') or [])}"]
            if e.get("cc"):
                fm.append(f"cc: {yq(e['cc'])}")
            fm.append(f"date: {yq(rec.isoformat(timespec='seconds'))}")
            if mid:
                fm.append(f"resource: {yq('mid:' + mid)}")
            if e.get("web_link"):
                fm.append(f"outlook_link: {yq(e['web_link'])}")
            fm.append(f"content: {yq(e.get('content', 'full'))}")
            if e.get("word_count"):
                fm.append(f"word_count: {int(e['word_count'])}")
            if e.get("attachments"):
                fm.append(f"attachments: {yq(e['attachments'])}")
            fm.append(f"generated: {{ by: {yq('process:email-intake')}, at: {yq(rec.isoformat(timespec='seconds'))} }}")
            fm += owner_props("Email")
            fm.append("---")
            head = f"# {e.get('subject') or '(no subject)'}\n\n*From {e.get('from', '')} — {rec:%B %d, %Y %I:%M %p}*"
            if e.get("content") == "summary":
                head += f"\n\n> Summary of a {e.get('word_count', 'long')}-word email; the original was removed from Outlook's Remember folder (Deleted Items keeps it for a while)."
            if e.get("attachments"):
                head += "\n\nAttachments (not saved): " + ", ".join(e["attachments"])
            text = "\n".join(fm) + "\n\n" + head + "\n\n" + (e.get("body") or "").strip() + "\n"
            path = os.path.join(inbox, name + ".md")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            fmd, err, _ = split_frontmatter(open(path, encoding="utf-8").read())
            ok = bool(fmd and not err and fmd.get("type") == "Email")
            print(json.dumps({"id": e.get("id"), "note": f"Inbox/{name}.md", "ok": ok}))
        except Exception as ex:  # report, never crash the batch
            print(json.dumps({"id": e.get("id"), "note": None, "ok": False, "error": str(ex)}))


if __name__ == "__main__":
    main()
