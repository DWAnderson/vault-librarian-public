#!/usr/bin/env python3
"""Write notes made from files (intake queue, File intake mode) into the vault's Inbox as OKF notes,
and copy each original into attachments/Sources/ (at the vault root or in `_Obsidian Internal/`).

Usage:
  python file_to_note.py <vault> specs.json [--dry-run]
specs.json: a list of
  {"id": "<queue item id>",
   "type": "Article" | "Reference" | "Recipe" | "Manual" | "Book Summary",
   "title": "...", "description": "One sentence.",
   "fields": {"author": [...], "publication": "...", ...},   # extra frontmatter, in order; empties dropped
   "resource": "https://..." | null,                          # default: the vault path of the original
   "body": "Markdown, without the # title and without the Original section",
   "doug_note": "text" | "",
   "original": "C:\\...\\file.pdf" | null,                    # copied (never moved) into attachments/Sources/
   "original_name": "file.pdf",                               # name the original had (recorded as source_file)
   "generated_by": "process:file-intake" | "claude/...",
   "status": "draft" | null}
Writes Inbox/<title>.md (never overwrites; adds a number if the name is taken). Prints one JSON line per
spec: {"id", "ok", "note", "original"}. A source may be removed from the dropbox only when ok is true.
"""
import datetime as dt
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import owner, split_frontmatter, vault_rel  # noqa: E402

BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')
EMBED = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
ORDER = ["type", "title", "description", "resource"]


def yaml_line(key, val):
    if isinstance(val, dict):
        inner = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in val.items())
        return f"{key}: {{ {inner} }}"
    if isinstance(val, list):
        return f"{key}: [{', '.join(json.dumps(v, ensure_ascii=False) for v in val)}]"
    return f"{key}: {json.dumps(val, ensure_ascii=False)}"


def empty(v):
    return v is None or (isinstance(v, (str, list, dict)) and not v)


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


def safe_name(title, folder, ext=".md"):
    base = " ".join(BAD.sub(" ", title or "Untitled").split())[:100].strip(" .") or "Untitled"
    name, n = base, 2
    while os.path.exists(os.path.join(folder, name + ext)):
        name, n = f"{base} {n}", n + 1
    return name


def append_log(vault, line):
    lp = os.path.join(vault, "log.md")
    s = open(lp, encoding="utf-8").read().rstrip() if os.path.exists(lp) else "# Vault Update Log"
    today = dt.date.today().isoformat()
    heads = re.findall(r"(?m)^## (.+)$", s)
    if not (heads and heads[-1].strip() == today):
        s += f"\n\n## {today}\n\n**File intake**\n"
    else:
        section = s[s.rfind("\n## "):]
        bolds = re.findall(r"(?m)^\*\*(.+?)\*\*\s*$", section)
        if not bolds or bolds[-1] != "File intake":
            s += "\n\n**File intake**\n"
    with open(lp, "w", encoding="utf-8", newline="\n") as f:
        f.write(s + "\n" + line + "\n")


def write(vault, spec, dry):
    inbox = os.path.join(vault, "Inbox")
    src_rel = vault_rel(vault, "attachments") + "/Sources"
    srcdir = os.path.join(vault, *src_rel.split("/"))
    title = (spec.get("title") or "Untitled").strip()
    name = safe_name(title, inbox)
    orig_rel = None
    if spec.get("original"):
        ext = os.path.splitext(spec.get("original_name") or spec["original"])[1].lower() or \
            os.path.splitext(spec["original"])[1].lower()
        if ext in (".md", ".markdown"):   # kept as .txt, so the original isn't a second, frontmatter-less note
            ext = ".txt"
        fname = safe_name(name, srcdir, ext) + ext
        orig_rel = f"{src_rel}/{fname}"
        if not dry:
            os.makedirs(srcdir, exist_ok=True)
            shutil.copy2(spec["original"], os.path.join(srcdir, fname))
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    fm = {"type": spec["type"], "title": title, "description": spec.get("description") or "",
          "resource": spec.get("resource") or orig_rel}
    for k, v in (spec.get("fields") or {}).items():
        if k not in fm:
            fm[k] = v
    if spec.get("original_name"):
        fm["source_file"] = spec["original_name"]
    lines = ["---"] + [yaml_line(k, v) for k, v in fm.items() if not empty(v)]
    lines.append(yaml_line("generated", {"by": spec.get("generated_by") or "process:file-intake", "at": now}))
    if spec.get("status"):
        lines.append(yaml_line("status", spec["status"]))
    lines += owner_props(fm.get("type") or spec.get("type"), {k for k, v in fm.items() if not empty(v)})
    lines += ["---", ""]
    body = [f"# {title}", ""]
    if (spec.get("doug_note") or "").strip():
        body += [f"> [!note] {owner()['note_label']}", *[f"> {ln}" for ln in spec["doug_note"].strip().splitlines()], ""]
    body += [(spec.get("body") or "").strip(), ""]
    if orig_rel:
        link = f"![[{orig_rel}]]" if os.path.splitext(orig_rel)[1] in EMBED else f"[[{orig_rel}|{os.path.basename(orig_rel)}]]"
        body += ["## Original", "", link, ""]
    text = "\n".join(lines) + "\n".join(body)
    path = os.path.join(inbox, name + ".md")
    if dry:
        return {"ok": True, "dry": True, "note": f"Inbox/{name}.md", "original": orig_rel}
    os.makedirs(inbox, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
    fmd, err, _ = split_frontmatter(open(path, encoding="utf-8").read())
    ok = bool(fmd and not err and fmd.get("type") == spec["type"]) and \
        (not orig_rel or os.path.exists(os.path.join(vault, *orig_rel.split("/"))))
    append_log(vault, f"- Created `Inbox/{name}.md` ({spec['type']}) from file `{spec.get('original_name') or '?'}`")
    return {"ok": ok, "note": f"Inbox/{name}.md", "original": orig_rel}


def main():
    vault, src = os.path.abspath(sys.argv[1]), sys.argv[2]
    dry = "--dry-run" in sys.argv
    if not os.path.isdir(os.path.join(vault, ".obsidian")):
        sys.exit(f"not an Obsidian vault (no .obsidian folder): {vault}")
    for spec in json.load(open(src, encoding="utf-8")):
        try:
            r = write(vault, spec, dry)
        except Exception as e:  # report, never crash the batch
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(dict(r, id=spec.get("id")), ensure_ascii=False))


if __name__ == "__main__":
    main()
