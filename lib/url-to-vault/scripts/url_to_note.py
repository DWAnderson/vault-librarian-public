#!/usr/bin/env python3
"""Write one vault note from a url_probe.py result (OKF v0.2 + the shared citation keys).

Usage:
  python url_to_note.py <vault> probe.json [--summary s.md] [--description "One sentence."]
         [--context note.txt] [--captured-via mid:<Message-ID>] [--title "…"]
         [--by process:url-intake] [--dry-run]

  article -> type Web Clipping, full text from the probe's text_path
  text    -> type Web Summary, needs --summary (the one paragraph Claude wrote)
  image   -> type Image, downloads the image into the vault's attachments folder
             (a web comic - probe "comic": true - keeps every panel in "images", the hover text and a
             bonus panel, and none of the page's other material; comics.py, 2026-10-18)
             (_Obsidian Internal/attachments/, or a root attachments/ in an older vault) and embeds it

Checks first that no note already has this `resource` (prints {"ok": false, "duplicate": path}).
Writes Inbox/<title>.md (never overwrites) and prints {"ok", "note", "category"}.
"""
import argparse
import datetime as dt
import json
import mimetypes
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import citation  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

TYPES = {"article": "Web Clipping", "text": "Web Summary", "image": "Image"}
BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')
BOILER = re.compile(r"(?mi)^\W*(follow topics and authors|sign up for|subscribe to (our|the)|"
                    r"most popular|recommended for you|share this (story|article)|advertisement)\b.*$\n?")
SKIP = {".obsidian", ".trash", ".git", "_snapshots", "node_modules", ".sync-backups"}
ORDER = ["type", "title", "description", "resource", "author", "publication", "publisher", "published",
         "accessed", "work_type", "doi", "journal", "volume", "issue", "pages", "section", "language",
         "word_count", "cite_as", "captured_via"]


def yaml_line(key, val):
    if isinstance(val, dict):
        inner = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in val.items())
        return f"{key}: {{ {inner} }}"
    if isinstance(val, list):
        return f"{key}: [{', '.join(json.dumps(v, ensure_ascii=False) for v in val)}]"
    return f"{key}: {json.dumps(val, ensure_ascii=False)}"


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


def norm_url(u):
    u = (u or "").strip().lower().rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", u)


def find_existing(vault, urls, skip=()):
    want = {norm_url(u) for u in urls if u}
    skip = {x.replace("\\", "/") for x in skip}
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
        for f in files:
            if not f.endswith(".md"):
                continue
            p = os.path.join(root, f)
            try:
                head = open(p, encoding="utf-8", errors="replace").read(4000)
            except OSError:
                continue
            m = re.search(r"(?m)^resource:\s*[\"']?([^\"'\n]+)", head)
            rel = os.path.relpath(p, vault).replace(os.sep, "/")
            if m and norm_url(m.group(1)) in want and rel not in skip:
                return rel
    return None


def attachments_dir(vault):
    """The vault's attachments folder: `_Obsidian Internal/attachments` if it is there, else the
    root one (case-insensitive). Same rule as vault-librarian's vaultlib.vault_rel; change both together."""
    def ci(parent, name):
        try:
            return next((n for n in os.listdir(parent) if n.lower() == name.lower()), None)
        except OSError:
            return None
    idir = ci(vault, "_Obsidian Internal")
    if idir and ci(os.path.join(vault, idir), "attachments"):
        return os.path.join(vault, idir, ci(os.path.join(vault, idir), "attachments"))
    if ci(vault, "attachments"):
        return os.path.join(vault, ci(vault, "attachments"))
    return os.path.join(vault, idir, "attachments") if idir else os.path.join(vault, "attachments")


def safe_name(title, folder, ext=".md"):
    base = " ".join(BAD.sub(" ", title or "Untitled").split())[:100].strip(" .") or "Untitled"
    name, n = base, 2
    while os.path.exists(os.path.join(folder, name + ext)):
        name, n = f"{base} {n}", n + 1
    return name


def one_sentence(s, limit=240):
    s = " ".join((s or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    s = m.group(1) if m and len(m.group(1)) > 30 else s
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


def append_log(vault, line):
    """Append under today's `## YYYY-MM-DD` / **URL intake** heading in log.md (created if needed)."""
    lp = os.path.join(vault, "log.md")
    s = open(lp, encoding="utf-8").read().rstrip() if os.path.exists(lp) else "# Vault Update Log"
    today = citation.today()
    heads = re.findall(r"(?m)^## (.+)$", s)
    if not (heads and heads[-1].strip() == today):
        s += f"\n\n## {today}\n\n**URL intake**\n"
    else:
        section = s[s.rfind("\n## "):]
        bolds = re.findall(r"(?m)^\*\*(.+?)\*\*\s*$", section)
        if not bolds or bolds[-1] != "URL intake":
            s += "\n\n**URL intake**\n"
    s += "\n" + line
    with open(lp, "w", encoding="utf-8", newline="\n") as f:
        f.write(s + "\n")


def download(url, folder, stem):
    import requests
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
    if not ctype.startswith("image/"):
        raise ValueError(f"image URL returned {ctype or 'no content type'}")
    ext = mimetypes.guess_extension(ctype) or os.path.splitext(url.split("?")[0])[1] or ".jpg"
    ext = {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext)
    name = safe_name(stem, folder, ext)
    with open(os.path.join(folder, name + ext), "wb") as f:
        f.write(r.content)
    return name + ext, len(r.content)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("probe")
    ap.add_argument("--summary")
    ap.add_argument("--description")
    ap.add_argument("--context")
    ap.add_argument("--captured-via")
    ap.add_argument("--title")
    ap.add_argument("--by", default="process:url-intake")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-dup", action="append", default=[],
                    help="vault-relative note this capture replaces (a Web Clipper stub or a partial capture); "
                         "its matching resource is not treated as a duplicate. The caller archives it afterwards.")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    if not os.path.isdir(os.path.join(vault, ".obsidian")):
        sys.exit(f"not an Obsidian vault (no .obsidian folder): {vault}")
    pr = json.load(open(a.probe, encoding="utf-8"))
    cat = pr.get("category")
    if cat not in TYPES:
        print(json.dumps({"ok": False, "category": cat, "reason": pr.get("reason"), "url": pr.get("url")}))
        return
    meta = dict(pr.get("meta") or {})
    url = meta.get("resource") or pr.get("final_url") or pr.get("url")
    dup = find_existing(vault, [url, pr.get("final_url"), pr.get("url")], a.ignore_dup)
    if dup:
        print(json.dumps({"ok": False, "duplicate": dup, "category": cat, "url": url}))
        return
    title = a.title or meta.get("title") or url
    cit = citation.clean(meta)
    cit["cite_as"] = citation.chicago(cit, title, url)
    if a.captured_via:
        cit["captured_via"] = a.captured_via
    desc = a.description or one_sentence(meta.get("description") or "")
    summary = open(a.summary, encoding="utf-8").read().strip() if a.summary else ""
    if cat == "text" and not summary:
        sys.exit("category 'text' needs --summary (the one-paragraph summary)")
    if not desc and summary:
        desc = one_sentence(summary)
    context = open(a.context, encoding="utf-8").read().strip() if a.context else ""
    inbox = os.path.join(vault, "Inbox")
    name = safe_name(title, inbox)
    now = citation.now().isoformat(timespec="seconds")

    body = [f"# {title}", ""]
    if context:
        name = os.environ.get("VAULT_OWNER_NAME", "").strip()   # set by the app from its config owner block
        label = os.environ.get("VAULT_OWNER_NOTE_LABEL", "").strip() or (f"{name}'s note" if name else "My note")
        body += [f"> [!note] {label}", *[f"> {ln}" for ln in context.splitlines()], ""]
    if cat == "article":
        text = open(pr["text_path"], encoding="utf-8").read().strip() if pr.get("text_path") else ""
        if not text:
            sys.exit("article has no extracted text (text_path missing)")
        text = re.sub(r"(?m)^#{1,2}\s+" + re.escape(title.strip()) + r"\s*$\n?", "", text, count=1)  # no doubled headline
        text = BOILER.sub("", text).strip()
        if not desc:
            desc = one_sentence(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text[:1200]))
        body += [text, ""]
    elif cat == "text":
        body += [summary, ""]
    elif pr.get("comic"):  # web comic (comics.py, 2026-10-18): the strip, hover text, bonus panel; no page chrome
        att = attachments_dir(vault)
        imgs = pr.get("images") or [{"url": pr["image_url"], "role": "comic"}]
        if not a.dry_run:
            os.makedirs(att, exist_ok=True)
        for i, im in enumerate(imgs):
            stem = name if i == 0 else f"{name} ({im.get('role') or 'panel ' + str(i + 1)})"
            fname = f"{stem} (dry run).png" if a.dry_run else download(im["url"], att, stem)[0]
            if i:
                body += [f"## {str(im.get('role') or 'Panel').capitalize()}", ""]
            body += [f"![[{fname}]]", ""]
            if i == 0 and (im.get("hover") or pr.get("caption")):
                body += [f"*Hover text: {' '.join(str(im.get('hover') or pr['caption']).split())}*", ""]
        body += ["Image files: " + " · ".join(f"<{im['url']}>" for im in imgs), ""]
        if not desc:
            desc = one_sentence(f"Webcomic: {title}.")
    else:  # image
        att = attachments_dir(vault)
        if not pr.get("image_url"):
            sys.exit("image category but no image_url in probe")
        if a.dry_run:
            fname, size = "(dry run).jpg", 0
        else:
            os.makedirs(att, exist_ok=True)
            fname, size = download(pr["image_url"], att, name)
        body += [f"![[{fname}]]", ""]
        if pr.get("caption"):
            body += [f"*{' '.join(str(pr['caption']).split())}*", ""]
        if pr.get("alt") and pr.get("alt") != pr.get("caption"):
            body += [f"Alt text: {pr['alt']}", ""]
        if pr.get("credit"):
            body += [f"Credit: {pr['credit']}", ""]
        if pr.get("context"):
            body += ["## Context on the page", "", pr["context"].strip(), ""]
        body += [f"Image file: {pr['image_url']}", ""]
        if not desc:
            desc = one_sentence(pr.get("caption") or pr.get("alt") or
                                f"Image: {title}" + (f" ({cit['publication']})" if cit.get("publication") else "") + ".")
    body += [f"Source: <{url}>", ""]

    fm = {"type": TYPES[cat], "title": title, "description": desc, "resource": url, **cit}
    lines = ["---"] + [yaml_line(k, fm[k]) for k in ORDER if not citation.empty(fm.get(k))]
    lines += [yaml_line(k, v) for k, v in fm.items() if k not in ORDER and not citation.empty(v)]
    lines += [yaml_line("generated", {"by": a.by, "at": now})] + owner_props(fm.get("type")) + ["---", ""]
    text = "\n".join(lines) + "\n".join(body)
    yaml.safe_load("\n".join(lines[1:-2]))  # raises if the frontmatter is not valid YAML
    path = os.path.join(inbox, name + ".md")
    if a.dry_run:
        print(text[:1500])
        print(json.dumps({"ok": True, "dry_run": True, "note": f"Inbox/{name}.md", "category": cat}))
        return
    os.makedirs(inbox, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
    append_log(vault, f"- Created `Inbox/{name}.md` ({TYPES[cat]}) from <{url}>")
    print(json.dumps({"ok": True, "note": f"Inbox/{name}.md", "category": cat, "type": TYPES[cat]}))


if __name__ == "__main__":
    main()
