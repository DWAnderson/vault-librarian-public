#!/usr/bin/env python3
"""Apply an approved organize/distill plan to the vault (runs locally; keeps bulk edits
out of the conversation).

Usage:
  python apply_plan.py <vault> <plan.json> [--dry-run]

plan.json:
{
  "actor": "human:<owner>",              # default `generated.by` for notes lacking it (default: vaultlib.owner() id)
  "log": "Organize pass (Inbox, 12 notes)",  # heading text for this session's log.md entry
  "items": [
    {"path": "Inbox/quick.md",           # vault-relative source
     "move_to": "Resources",             # optional destination folder (created if needed)
     "tag_folder": "Subjects/Policy",    # optional: folder that names the folder tag (default move_to; "" = none)
     "set": {"type": "Capture",           # frontmatter keys to set (overwrites that key only)
             "description": "One line.",
             "tags": ["aviation"]},
     "add_generated": true,              # add generated {by: actor, at: file mtime} if missing
     "generated_by": "process:export-chat-history",  # optional per-item actor for add_generated
     "rename_to": "Hangar lease rates 2026 (Aviation Week)",  # optional new note name (no .md)
     "unset": ["created", "updated"],     # optional: frontmatter keys to remove (after mapping them)
     "link_mentions": [{"target": "Projects/Smith Renewal", "text": "Smith renewal"}]}
                                          # optional, the owner-approved only: link first unlinked occurrence
  ]
}

Folder tags: an item moved into a subfolder (e.g. Manuals/Home IT) also gets the tag `home-it`; new
folder tags are appended to _claude-tags.md. See folder_tag().

Rules enforced: never overwrites or deletes a file; refuses a move or rename that would
create a duplicate note name in the vault. `rename_to` is honored only for intake notes
(in Clippings/ or Inbox/ at plan time) that NOTHING in the vault links to yet; the old
name is kept in `aliases` so it still resolves. Any other rename is refused unless the
item carries "rename_approved": true (the owner approved that specific rename); existing frontmatter keys not named in `set` are left exactly
as written. Every change is appended to log.md under today's ISO date.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import owner, RESERVED, MDLINK_RE, WIKILINK_RE, Resolver, map_internal, mask_code, parse_wikilink, split_frontmatter, walk

INTAKE = ("clippings/", "inbox/")
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")


def yaml_line(key, val):
    if isinstance(val, dict):
        inner = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in val.items())
        return f"{key}: {{ {inner} }}"
    if isinstance(val, list):
        return f"{key}: [{', '.join(json.dumps(v, ensure_ascii=False) for v in val)}]"
    if isinstance(val, bool):
        return f"{key}: {'true' if val else 'false'}"
    return f"{key}: {json.dumps(val, ensure_ascii=False)}"


def set_keys(text, updates, unset=()):
    """Set (or remove) top-level frontmatter keys, preserving every other line as written."""
    drop = set(updates) | set(unset)
    m = re.match(r"(?s)^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)", text)
    if not m:
        if not updates:
            return text
        block = "\n".join(yaml_line(k, v) for k, v in updates.items())
        return f"---\n{block}\n---\n\n{text.lstrip()}"
    lines = m.group(1).split("\n")
    out, skip = [], False
    for ln in lines:
        km = re.match(r"^([A-Za-z0-9_\-]+)\s*:", ln)
        if km:
            skip = km.group(1) in drop
        elif ln and not ln[0].isspace() and not ln.startswith("-"):
            skip = False
        if not skip:
            out.append(ln)
    out += [yaml_line(k, v) for k, v in updates.items()]
    new_fm = "\n".join(out)
    yaml.safe_load(new_fm)  # raises if we produced invalid YAML
    return f"---\n{new_fm}\n---\n" + text[m.end():]


def inbound_links(vault, target_rel, resolver):
    """Vault files (other than the target) that link to target_rel by wikilink or path."""
    hits = []
    for rel, ab in walk(vault):
        if rel == target_rel or not rel.lower().endswith(".md") or rel.rsplit("/", 1)[-1] in RESERVED:
            continue  # generated index/log pages don't count as real links
        with open(ab, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        if "build_hubs.py" in raw[:600]:
            continue  # generated topic hub
        scan = mask_code(raw)
        for m in WIKILINK_RE.finditer(scan):
            if resolver.resolve(parse_wikilink(m.group(2))[0])[0] == target_rel:
                hits.append(rel)
                break
        else:
            for m in MDLINK_RE.finditer(scan):
                path = unquote(m.group(3).split("#", 1)[0])
                cand = path.lstrip("/") if path.startswith("/") else \
                    os.path.normpath(os.path.join(os.path.dirname(rel), path)).replace(os.sep, "/")
                if cand == target_rel:
                    hits.append(rel)
                    break
    return hits


def link_first(text, phrase, target):
    """Wrap the first unlinked, non-code occurrence of phrase (outside frontmatter) in a wikilink."""
    if not phrase or not target:
        return text, False
    m = re.match(r"(?s)^---[ \t]*\r?\n.*?\r?\n---[ \t]*(\r?\n|$)", text)
    start = m.end() if m else 0
    body = text[start:]
    blank = lambda x: " " * len(x.group(0))
    scan = MDLINK_RE.sub(blank, WIKILINK_RE.sub(blank, mask_code(body)))
    hit = re.search(r"(?<![\w\[])" + re.escape(phrase) + r"(?![\w\]])", scan, re.I)
    if not hit:
        return text, False
    orig = body[hit.start():hit.end()]
    name = target.rsplit("/", 1)[-1]
    link = f"[[{target}|{orig}]]" if "/" in target or orig != name else f"[[{name}]]"
    return text[:start] + body[:hit.start()] + link + body[hit.end():], True


def clean_name(name):
    name = BAD_CHARS.sub(" ", name).strip().strip(".")
    return re.sub(r"\s+", " ", name)[:120].strip()


# Folder tags (2026-10-07): a note filed into or moved to a SUBFOLDER gets a tag named after that
# subfolder (kept alongside its existing tags), e.g. Manuals/Home IT -> `home-it`, Subjects/Media -> `media`.
# Top-level folders give no tag; housekeeping/intake/export trees are exempt. New folder tags are added
# to _claude-tags.md automatically (the rule itself is the owner's approval).
FOLDER_TAG_EXEMPT = ("_obsidian internal", "inbox", "clippings", "claude", "daily notes", "concepts",
                     "archives", "templates", "topics", "attachments", ".sync-backups")


def folder_tag(folder):
    parts = [x for x in (folder or "").replace("\\", "/").split("/") if x]
    if len(parts) < 2 or parts[0].lower() in FOLDER_TAG_EXEMPT or parts[-1].lower() == "other":
        return None   # an "Other" catch-all folder gives no tag (2026-10-07)
    t = re.sub(r"[^a-z0-9]+", "-", parts[-1].lower()).strip("-")
    return t or None


def add_folder_tags_to_vocab(vault, pairs):
    """pairs: {tag: folder}. Appends missing tags to _claude-tags.md (wherever it lives)."""
    if not pairs:
        return []
    p = os.path.join(vault, map_internal(vault, "_claude-tags.md"))
    if not os.path.isfile(p):
        return []
    s = open(p, encoding="utf-8").read()
    have = set(re.findall(r"(?m)^- `([^`]+)`", s))
    added = [t for t in sorted(pairs) if t not in have]
    if added:
        s = s.rstrip("\n") + "".join(f"\n- `{t}` — folder tag for `{pairs[t]}` (added automatically when items were filed "
                                      f"there, {dt.date.today().isoformat()})" for t in added) + "\n"
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(s)
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("plan")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    actor = plan.get("actor") or owner()["id"]
    resolver = Resolver([r for r, _ in walk(vault)]).add_aliases(vault)
    names = {}
    for p in resolver.paths:
        if p.lower().endswith(".md"):
            names.setdefault(p.rsplit("/", 1)[-1].lower(), set()).add(p)
    log, skipped = [], []
    folder_tags = {}
    for it in plan.get("items", []):
        rel = it["path"].replace("\\", "/").lstrip("/")
        src = os.path.join(vault, *rel.split("/"))
        if not os.path.isfile(src):
            skipped.append(f"{rel}: not found")
            continue
        with open(src, encoding="utf-8", errors="replace") as f:
            text = f.read()
        fm, err, _ = split_frontmatter(text)
        if err:
            skipped.append(f"{rel}: frontmatter unreadable ({err}) — fix by hand")
            continue
        updates = dict(it.get("set") or {})
        if it.get("add_generated") and not (fm or {}).get("generated"):
            at = dt.datetime.fromtimestamp(os.path.getmtime(src)).astimezone().isoformat(timespec="seconds")
            updates["generated"] = {"by": it.get("generated_by") or actor, "at": at}
        base = rel.rsplit("/", 1)[-1]
        new_base = base
        if it.get("rename_to"):
            cand = clean_name(it["rename_to"])
            cand = cand if cand.lower().endswith(".md") else cand + ".md"
            if cand == base:
                pass
            elif not (rel.lower().startswith(INTAKE) or it.get("rename_approved")):
                skipped.append(f"{rel}: not an intake note — rename to '{cand}' needs the owner's approval")
            elif names.get(cand.lower(), set()) - {rel}:
                skipped.append(f"{rel}: a note named '{cand}' already exists — not renamed")
            else:
                links = inbound_links(vault, rel, resolver)
                if links and not it.get("rename_approved"):
                    skipped.append(f"{rel}: linked from {', '.join(links[:3])} — not renamed")
                else:
                    new_base = cand
                    old_aliases = (fm or {}).get("aliases") or []
                    old_aliases = old_aliases if isinstance(old_aliases, list) else [old_aliases]
                    old_name = clean_name(base[:-3])
                    if old_name and old_name not in old_aliases:
                        updates["aliases"] = old_aliases + [old_name]
        if it.get("move_to") is not None:
            # "tag_folder": the folder whose name gives the tag, when the note goes into a work's own folder
            # (Subjects/Policy/A Fiscal Cliff -> tag `policy`, not `a-fiscal-cliff`; 2026-10-13). "" = no tag.
            ftag = folder_tag(map_internal(vault, it["tag_folder"] if "tag_folder" in it else it["move_to"]))
            if ftag:
                cur = updates.get("tags", (fm or {}).get("tags")) or []
                cur = list(cur) if isinstance(cur, list) else [cur]
                if ftag not in cur:
                    updates["tags"] = cur + [ftag]
                folder_tags.setdefault(ftag, map_internal(vault, it.get("tag_folder") or it["move_to"]))
        if updates and any(k not in ("see_also", "my_note") for k in updates):
            for k in ("see_also", "my_note"):   # The owner's two properties always stay last in the frontmatter
                if k in updates:
                    updates[k] = updates.pop(k)
                elif k in (fm or {}):
                    updates[k] = fm[k] if fm[k] is not None else ([] if k == "see_also" else "")
        unset = [k for k in (it.get("unset") or []) if k not in updates]
        new_text = set_keys(text, updates, unset) if (updates or unset) else text
        linked = []
        for lm in it.get("link_mentions") or []:
            new_text, ok = link_first(new_text, lm.get("text", ""), lm.get("target", ""))
            (linked if ok else skipped).append(lm.get("text") if ok else f"{rel}: mention '{lm.get('text')}' not found unlinked")
        dest_rel = rel if new_base == base else (rel.rsplit("/", 1)[0] + "/" + new_base if "/" in rel else new_base)
        if it.get("move_to") is not None:
            folder = map_internal(vault, it["move_to"])  # "Archives" -> wherever Archives lives
            dest_rel = f"{folder}/{new_base}" if folder else new_base
        if dest_rel != rel:
            dest = os.path.join(vault, *dest_rel.split("/"))
            others = names.get(new_base.lower(), set()) - {rel}
            if os.path.exists(dest):
                skipped.append(f"{rel}: {dest_rel} already exists — not moved/renamed")
                dest_rel, new_base = rel, base
                updates.pop("aliases", None)
                new_text = set_keys(text, updates, unset) if (updates or unset) else text
            # a move keeps the name, so it can't create a new name clash
        if a.dry_run:
            print(f"would update {rel}" + (f" -> {dest_rel}" if dest_rel != rel else "") +
                  (f" set {sorted(updates)}" if updates else ""))
            continue
        if new_text != text:
            with open(src, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_text)
            what = []
            if updates:
                what.append("set " + ", ".join(sorted(updates)))
            if unset:
                what.append("removed " + ", ".join(sorted(unset)))
            if linked:
                what.append("linked mentions " + ", ".join(f"'{x}'" for x in linked))
            msg = "; ".join(what)
            log.append(f"- {msg[:1].upper() + msg[1:]} on `{dest_rel}`")
        if dest_rel != rel:
            os.makedirs(os.path.dirname(os.path.join(vault, *dest_rel.split("/"))), exist_ok=True)
            os.rename(src, os.path.join(vault, *dest_rel.split("/")))
            log.append(f"- {'Renamed and moved' if new_base != base else 'Moved'} `{rel}` → `{dest_rel}`")
            names.setdefault(base.lower(), set()).discard(rel)
            names.setdefault(new_base.lower(), set()).add(dest_rel)
    for s in skipped:
        print("SKIPPED", s)
    if a.dry_run or not log:
        print(f"{len(log)} changes" + (" (dry run)" if a.dry_run else ""))
        return
    added = add_folder_tags_to_vocab(vault, folder_tags)
    if added:
        log.append("- Added folder tag(s) to the approved tags: " + ", ".join(f"`{t}`" for t in added))
    lp = os.path.join(vault, "log.md")
    today = dt.date.today().isoformat()
    existing = open(lp, encoding="utf-8").read() if os.path.exists(lp) else "# Vault Update Log\n"
    entry = f"\n## {today}\n\n**{plan.get('log', 'vault-librarian changes')}**\n\n" + "\n".join(log) + "\n"
    if skipped:
        entry += "\nSkipped:\n" + "\n".join(f"- {s}" for s in skipped) + "\n"
    with open(lp, "w", encoding="utf-8", newline="\n") as f:
        f.write(existing.rstrip() + "\n" + entry)
    print(f"{len(log)} changes applied; logged to log.md")


if __name__ == "__main__":
    main()
