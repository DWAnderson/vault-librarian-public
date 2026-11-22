#!/usr/bin/env python3
"""Refresh a read-only copy of a Markdown file in a Cowork folder (2026-10-12).

  python copy_out.py <root> <rel> <dest_file> [--master "<how to name the master>"]

Used by the daily laptop "Vault Cowork sync" task to keep the Cowork copy of the Vault Librarian User Manual current,
reading the master in the app folder directly (root = the Custom Apps/Obsidian folder, rel = the file name).
(This PC no longer writes these documents into the vault itself: its jobs/doc_sync.py is off since 2026-10-12.)
The copy gets the note's body and a small frontmatter (type, title, description, tags, copy_of, copied_at) and none of
the sync tracking keys. With --sync (2026-10-12 12:17: all Claude documents reach the vault through the Cowork
sync) it also gets `obsidian_sync: true`, so the same task's Cowork sync carries it into the vault right after; the
task runs this step BEFORE its scan. Without --sync the copy is never a sync source. A line under the
title says it is a copy and where to edit. Writes only when the content changed. Images the document embeds (local, relative) are copied beside the copy too
(2026-10-12). copied_at is written quoted (an unquoted one made YAML return a datetime and broke sync_notes). Prints "updated" or "unchanged".
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import split_frontmatter  # noqa: E402

IMG_RE = re.compile(r"!\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^)\s>]+))")
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}


def j(v):
    """JSON (valid YAML flow) with dates/datetimes as ISO strings."""
    return json.dumps(v, ensure_ascii=False, default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o))


def copy_images(body, src_dir, dest_dir):
    """Copy local images the document embeds from beside the master to beside the copy (same relative path)."""
    n = 0
    for m in IMG_RE.finditer(body):
        rel = (m.group(1) or m.group(2)).split("#")[0]
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", rel) or os.path.splitext(rel)[1].lower() not in IMG_EXT:
            continue
        a = os.path.normpath(os.path.join(src_dir, rel))
        b = os.path.normpath(os.path.join(dest_dir, rel))
        if not os.path.isfile(a) or not b.startswith(os.path.normpath(dest_dir)):
            continue
        if os.path.isfile(b) and hashlib.sha256(open(a, "rb").read()).digest() == hashlib.sha256(open(b, "rb").read()).digest():
            continue
        os.makedirs(os.path.dirname(b), exist_ok=True)
        shutil.copy2(a, b)
        n += 1
    return n


KEEP = ("type", "title", "description", "tags", "generated")   # never the sync tracking keys; obsidian_sync only with --sync
# generated is carried so the Cowork sync can tell a Claude-written document (files it under Claude/Other).


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("rel")
    ap.add_argument("dest")
    ap.add_argument("--master", default="the app folder")
    ap.add_argument("--sync", action="store_true", help="mark the copy obsidian_sync: true (it is then synced to the vault)")
    a = ap.parse_args(argv)
    src = os.path.join(a.root, *a.rel.split("/"))
    with open(src, encoding="utf-8-sig") as f:
        text = f.read().replace("\r\n", "\n")
    fm, err, body = split_frontmatter(text)
    fm = fm if isinstance(fm, dict) else {}
    if a.sync:
        note = (f"> Copy of the master `{a.rel}` in {a.master}, refreshed daily by the laptop task \"Vault Cowork sync\", "
                f"which also syncs it into the vault. Edit only the master: edits to this copy or to the vault note are "
                f"overwritten or held as a conflict.\n")
    else:
        note = (f"> Copy for reference, refreshed daily from the master `{a.rel}` in {a.master}. "
                f"Don't edit it here: changes made here are overwritten.\n")
    head = ["---"] + [f"{k}: {j(fm[k])}" for k in KEEP if k in fm]
    head.append(f"copy_of: {j(a.master + ': ' + a.rel)}")
    if a.sync:
        head.append("obsidian_sync: true")
    lines = body.lstrip("\n").split("\n")
    i = 1 if lines and lines[0].startswith("# ") else 0     # the note goes right under the title
    new_body = "\n".join(lines[:i] + ([""] if i else []) + [note] + lines[i:])
    old = ""
    if os.path.exists(a.dest):
        with open(a.dest, encoding="utf-8-sig") as f:
            old = f.read().replace("\r\n", "\n")
        _ofm, _e, old_body = split_frontmatter(old)
        _ofm = _ofm if isinstance(_ofm, dict) else {}
        n_img = copy_images(body, os.path.dirname(src), os.path.dirname(a.dest))
        if (old_body.strip() == new_body.strip() and bool(_ofm.get("obsidian_sync")) == a.sync
                and ("generated" in _ofm or "generated" not in fm)):
            print("unchanged" + (f" ({n_img} image(s) updated)" if n_img else ""))
            return 0
    copy_images(body, os.path.dirname(src), os.path.dirname(a.dest))
    out = "\n".join(head + [f"copied_at: {j(dt.datetime.now().astimezone().isoformat(timespec='seconds'))}", "---", ""])
    os.makedirs(os.path.dirname(a.dest), exist_ok=True)
    with open(a.dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(out + new_body.rstrip("\n") + "\n")
    print("updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
