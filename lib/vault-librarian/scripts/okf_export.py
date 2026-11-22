#!/usr/bin/env python3
"""Export an Obsidian vault as a self-describing, shareable OKF v0.2 bundle (runs locally; costs no tokens).

The vault itself keeps Obsidian [[wikilinks]]; this writes a converted COPY of the folders chosen at export time:
  [[Note]]                -> [Note](/Folder/Note.md)
  [[Note|Alias]]          -> [Alias](/Folder/Note.md)
  [[Note#Heading]]        -> [Note > Heading](/Folder/Note.md#Heading)
  ![[image.png]]          -> ![image.png](/attachments/image.png)
  ![[Other Note]]         -> [Other Note](/Folder/Other%20Note.md)   (embeds become links)
Links inside code are left alone. A link to a note that is not exported becomes plain text, and a list entry
that only pointed to one is dropped (so left-out notes' titles don't leak through hubs and indexes).
Unresolved wikilinks become plain text and are reported. The vault is never modified.

What makes the bundle usable by someone else's AI (2026-10-12):
  * AGENTS.md at the root: what the bundle is, the trust layers, how to search, read and cite
    (template: assets/okf-export-guide.md); CLAUDE.md just points to it. The vault's own CLAUDE.md and log.md
    (maintenance rules and change history) are not exported.
  * a `layer` property on every note (source / summary / compiled / mine / claude chat), the same mapping as
    this PC's Vault connector.
  * _tools/okf_search.py + _tools/search.db: the passage index and a read-only search tool (CLI and stdio MCP
    server; standard-library Python; meaning search optional), plus README.txt and export-manifest.json.
  * every index.md is rebuilt from the exported notes only.
  * only the attachments the exported notes use are copied; originals (attachments/Sources) only with --originals.

Choosing what goes in is required and happens at export time:
  python okf_export.py <vault> --list-folders [--json]            folders, note counts, sharing defaults
  python okf_export.py <vault> <out_dir> --defaults                default sharing set (see DEFAULT_EXCLUDE)
  python okf_export.py <vault> <out_dir> --include Areas Concepts ...   only these (subfolders allowed)
  python okf_export.py <vault> <out_dir> --exclude "Personal Notes" ...  everything except these
  python okf_export.py <vault> <out_dir> --all                     everything
--include and --exclude can be combined (the deeper folder wins). With none of these and a terminal attached,
the script asks; without a terminal it stops and lists the folders, so an agent must ask the person first.
A note with `share: false` in its frontmatter is never exported.
Other options: --originals (include original PDFs/e-books and their page text), --strip-my-notes (drop my_note
and "My notes"/"Your notes" sections), --owner NAME, --zip, --no-search.
Then check the result:  python okf_lint.py <out_dir> --strict
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import string
import sys
from urllib.parse import quote, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from vaultlib import (FENCE_RE, INLINE_CODE_RE, INTERNAL_DIR, MDLINK_RE, SKIP_DIRS, WIKILINK_RE,  # noqa: E402
                      Resolver, parse_wikilink, split_frontmatter, walk)

SAFE = "/-_.~!$&'*+,;=:@"   # parentheses and spaces are percent-encoded so links stay valid
DEFAULT_EXCLUDE = ["Personal Notes", "Daily Notes", "Today", "Claude", "Obsidian", "Inbox", "Clippings",
                   "Obsidian Templates", f"{INTERNAL_DIR}/Archives"]
HINTS = {"Personal Notes": "notes you wrote yourself", "Daily Notes": "your daily notes", "Today": "your to-dos",
         "Claude": "exported Claude chats and memories", "Obsidian": "Obsidian help and Vault Librarian docs "
         "(machine setup, addresses)", "Inbox": "unprocessed captures", "Clippings": "unprocessed captures",
         "Obsidian Templates": "templates", f"{INTERNAL_DIR}/Archives": "archived (inactive) notes",
         f"{INTERNAL_DIR}/Topics": "topic hubs (one per tag)", "Concepts": "concept pages",
         "Manuals": "device manuals", "Resources": "reference items", "Subjects": "incoming items by subject",
         "Areas": "ongoing areas", "Projects": "projects"}
NEVER = {"index.md", "log.md", "CLAUDE.md", "AGENTS.md"}          # root files rebuilt or replaced
LAYERS = {   # same mapping as this PC's vault_search (config search.layers)
    "mine_types": ["Personal Note", "Daily Note"], "mine_by": [os.environ.get("VAULT_OWNER_ID") or "human:owner"], "chat_types": ["Claude Chat"],
    "summary_types": ["Summary", "Book Summary", "Video Summary", "Podcast Summary", "Web Summary"],
    "compiled_types": ["Analysis", "Part Analysis", "Concept", "Atomic Note", "Project Note", "Area Note"],
}
MINE_HEADINGS = ["my notes", "your notes", "my thoughts", "my comments"] + (
    [os.environ["VAULT_OWNER_NAME"].strip().lower() + "'s notes"] if os.environ.get("VAULT_OWNER_NAME", "").strip() else [])
X = "\x00"                                                          # marks a link to a left-out note


# ---------------------------------------------------------------- choosing folders
def units(vault):
    """Selectable folders: top-level folders, with the housekeeping folder split into its subfolders."""
    out = []
    for name in sorted(os.listdir(vault), key=str.lower):
        p = os.path.join(vault, name)
        if not os.path.isdir(p) or name.startswith(".") or name in SKIP_DIRS:
            continue
        if name.lower() == INTERNAL_DIR.lower():
            for sub in sorted(os.listdir(p), key=str.lower):
                if os.path.isdir(os.path.join(p, sub)) and sub.lower() not in ("attachments", "templates") \
                        and not sub.startswith("."):
                    out.append(f"{name}/{sub}")
        else:
            out.append(name)
    return out


def unit_of(rel):
    """The selectable folder a path belongs to (top folder, or the housekeeping folder's subfolder)."""
    parts = rel.split("/")
    if len(parts) == 1:
        return "(root)"
    if parts[0].lower() == INTERNAL_DIR.lower() and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def folder_of(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def decide(rel, rules, base):
    """Longest matching folder rule wins; base when none matches."""
    f, best, keep = folder_of(rel).lower(), -1, base
    for prefix, inc in rules:
        p = prefix.lower().strip("/")
        if (f == p or f.startswith(p + "/")) and len(p) > best:
            best, keep = len(p), inc
    return keep


def is_internal_root_file(rel):
    parts = rel.split("/")
    return len(parts) == 2 and parts[0].lower() == INTERNAL_DIR.lower()


def share_false(fm):
    v = (fm or {}).get("share")
    return v is False or str(v).strip().lower() in ("false", "no", "0", "never")


def read_fm(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    try:
        fm, _err, _body = split_frontmatter(text)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        fm = None
    return text, fm or {}


def survey(vault):
    rows = []
    md = [(r, a) for r, a in walk(vault) if r.lower().endswith(".md")]
    for u in units(vault):
        ul = u.lower()
        n = sum(1 for r, _ in md if folder_of(r).lower() == ul or folder_of(r).lower().startswith(ul + "/"))
        rows.append({"folder": u, "notes": n, "default": "exclude" if u in DEFAULT_EXCLUDE else "include",
                     "hint": HINTS.get(u, "")})
    nshare = sum(1 for r, a in md if share_false(read_fm(a)[1]))
    src = os.path.join(vault, INTERNAL_DIR, "attachments", "Sources")
    nsrc = size = 0
    if os.path.isdir(src):
        for f in os.listdir(src):
            nsrc += 1
            size += os.path.getsize(os.path.join(src, f))
    return {"folders": rows, "share_false_notes": nshare, "originals": {"files": nsrc, "mb": round(size / 1e6)}}


def print_survey(s):
    print(f"{'#':>2}  {'folder':32} {'notes':>6}  default")
    for i, r in enumerate(s["folders"], 1):
        print(f"{i:>2}  {r['folder']:32} {r['notes']:>6}  {r['default']:8} {r['hint']}")
    o = s["originals"]
    print(f"\nOriginals (attachments/Sources): {o['files']} files, {o['mb']:,} MB - left out unless --originals")
    print(f"Notes marked share: false (never exported): {s['share_false_notes']}")


def ask_terminal(s):
    print_survey(s)
    inc = {r["folder"]: r["default"] == "include" for r in s["folders"]}
    names = [r["folder"] for r in s["folders"]]
    while True:
        ans = input("\nEnter = defaults; numbers to switch (e.g. 3 7); 'all'; 'none'; 'list': ").strip().lower()
        if not ans:
            break
        if ans == "list":
            print("\n".join(f"{i:>2} {'IN ' if inc[n] else 'out'} {n}" for i, n in enumerate(names, 1)))
            continue
        if ans in ("all", "none"):
            inc = {n: ans == "all" for n in names}
        else:
            for t in ans.replace(",", " ").split():
                if t.isdigit() and 1 <= int(t) <= len(names):
                    n = names[int(t) - 1]
                    inc[n] = not inc[n]
        print("Included: " + ", ".join(n for n in names if inc[n]))
        if input("Done? [Y/n] ").strip().lower() in ("", "y", "yes"):
            break
    orig = input("Include originals (PDFs/e-books)? [y/N] ").strip().lower() in ("y", "yes")
    strip = input("Strip your own notes (my_note, 'My notes' sections) from items? [y/N] ").strip().lower() in ("y", "yes")
    return [(n, v) for n, v in inc.items()], orig, strip


# ---------------------------------------------------------------- converting
def href(path, heading=None):
    h = "/" + quote(path, safe=SAFE)
    if heading:
        h += "#" + quote(heading, safe="-_.~")
    return h


def layer_of(fm):
    ntype = str(fm.get("type") or "")
    gen = fm.get("generated")
    by = str(gen.get("by", "")) if isinstance(gen, dict) else ""
    if ntype in LAYERS["mine_types"] or by in LAYERS["mine_by"]:
        return "mine"
    if ntype in LAYERS["chat_types"]:
        return "claude chat"
    if ntype in LAYERS["summary_types"]:
        return "summary"
    if ntype in LAYERS["compiled_types"]:
        return "compiled"
    return "source"


def is_original(path):
    return "/attachments/sources/" in "/" + path.lower()


class Ctx:
    def __init__(self, resolver, included, originals, rules, base):
        self.resolver, self.included, self.originals = resolver, included, originals
        self.rules, self.base = rules, base
        self.copy, self.problems, self.dropped_links, self.links, self.site_links = {}, [], 0, 0, 0

    def place(self, path):
        """Where an attachment goes in the bundle. One stored in a left-out folder (e.g. images under Personal
        Notes used by an exported Subjects note) moves to the attachments folder, so the left-out folder's
        name doesn't appear in the export."""
        if path not in self.copy:
            dest = path
            if not decide(path, self.rules, self.base):
                dest = f"{INTERNAL_DIR}/attachments/{folder_of(path).rsplit('/', 1)[-1]}/{path.rsplit('/', 1)[-1]}"
            self.copy[path] = dest
        return self.copy[path]


def convert(text, ctx, rel, origin=None):
    spans, last, out = [], 0, []
    for m in sorted(list(FENCE_RE.finditer(text)) + list(INLINE_CODE_RE.finditer(text)), key=lambda m: m.start()):
        if m.start() < last:
            continue
        spans.append((last, m.start(), True))
        spans.append((m.start(), m.end(), False))
        last = m.end()
    spans.append((last, len(text), True))

    def target(path, label, heading, embed):
        is_note = path.lower().endswith(".md")
        if is_note and path not in ctx.included:
            ctx.dropped_links += 1
            return f"{X}{label}{X}"
        if not is_note:
            if is_original(path) and not ctx.originals:
                ctx.dropped_links += 1
                return f"{X}{label}{X}"
            path = ctx.place(path)
            if embed:
                return f"![{label}]({href(path)})"
        return f"[{label}]({href(path, heading)})"

    def wiki(m):
        embed, inner = m.group(1), m.group(2)
        if "''" in inner:   # inside a single-quoted YAML value: unescape to resolve, escape the result again
            return wiki_plain(embed, inner.replace("''", "'")).replace("'", "''")
        return wiki_plain(embed, inner)

    def wiki_plain(embed, inner):
        tgt, heading, alias = parse_wikilink(inner)
        ctx.links += 1
        path, status = ctx.resolver.resolve(tgt)
        if status == "ambiguous":
            ctx.problems.append(f"{rel}: ambiguous [[{inner}]] -> chose {path}")
        label = alias or (f"{tgt} > {heading}" if heading else tgt)
        if not path:
            ctx.problems.append(f"{rel}: unresolved [[{inner}]] -> left as plain text")
            return label
        return target(path, alias or (tgt if embed and not path.lower().endswith(".md") else label), heading,
                      bool(embed))

    def md(m):
        embed, label, url = m.group(1), m.group(2), m.group(3)
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I) or url.startswith("#"):
            return m.group(0)                                           # web, mailto, obsidian:, in-page
        u, _, frag = unquote(url).partition("#")
        base = folder_of(rel)
        cands = [u.lstrip("/"), os.path.normpath(os.path.join(base, u)).replace(os.sep, "/")]
        for c in cands:
            if c in ctx.resolver.pathset:
                return target(c, label, frag or None, bool(embed))
        if origin and url.startswith("/"):    # site-relative link in a clipped page: point it at the site
            ctx.site_links += 1
            return f"{embed}[{label}]({origin}{url})"
        return m.group(0)

    for s, e, convertible in spans:
        chunk = text[s:e]
        if convertible:
            chunk = MDLINK_RE.sub(md, chunk)       # existing Markdown links first, so converted ones aren't redone
            chunk = WIKILINK_RE.sub(wiki, chunk)
        out.append(chunk)
    text = "".join(out)
    if X in text:   # drop list entries that only pointed to left-out notes; elsewhere keep the words
        keep = []
        for line in text.split("\n"):
            if re.match(r"^\s*[-*+]\s+(\[[ xX]\]\s+)?[\"']?" + X + r"[^" + X + r"]*" + X + r"[\"']?\s*(—.*|-\s.*|:.*)?$",
                        line):
                continue
            keep.append(line)
        text = "\n".join(keep).replace(X, "")
    return text


def site_origin(fm):
    """https://host of the page a clipped note came from (resource / source / url property), or None."""
    for k in ("resource", "source", "url"):
        for v in (fm.get(k) if isinstance(fm.get(k), list) else [fm.get(k)]):
            m = re.match(r"^\s*(https?://[^/\s\])]+)", str(v or ""))
            if m:
                return m.group(1)
    return None


def strip_mine(text):
    """Remove my_note and the owner's own sections from an item."""
    text = re.sub(r"(?m)^my_note:.*(\n[ \t]+.*)*\n", "", text, count=1)
    lines, out, skip_lvl = text.split("\n"), [], None
    for line in lines:
        h = re.match(r"^(#{1,6})\s+(.*?)\s*:?\s*#*\s*$", line)
        if h:
            lvl = len(h.group(1))
            if skip_lvl is not None and lvl <= skip_lvl:
                skip_lvl = None
            if skip_lvl is None and h.group(2).strip().lower() in MINE_HEADINGS:
                skip_lvl = lvl
                continue
        if skip_lvl is None:
            out.append(line)
    return "\n".join(out)


def set_layer(text, layer):
    m = re.match(r"(?s)^---[ \t]*\r?\n(.*?)(\r?\n)---[ \t]*(\r?\n|$)", text)
    if not m:
        return text
    body = re.sub(r"(?m)^layer:.*$\n?", "", m.group(1)).rstrip("\n")
    return f"---\n{body}\nlayer: {layer}\n---{m.group(3)}" + text[m.end():]


# ---------------------------------------------------------------- export
def main():
    ap = argparse.ArgumentParser(description="Export the vault as a shareable OKF v0.2 bundle.")
    ap.add_argument("vault")
    ap.add_argument("out_dir", nargs="?")
    ap.add_argument("--list-folders", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--defaults", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--include", nargs="+", default=[])
    ap.add_argument("--exclude", nargs="+", default=[])
    ap.add_argument("--originals", action="store_true")
    ap.add_argument("--strip-my-notes", action="store_true")
    ap.add_argument("--owner", default=os.environ.get("VAULT_OWNER_NAME") or "the vault owner")
    ap.add_argument("--tz", default="America/Chicago", help="time zone for the export's timestamps")
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--okf-version", default="0.2")
    a = ap.parse_args()
    vault = os.path.abspath(a.vault)
    if a.list_folders:
        s = survey(vault)
        print(json.dumps(s, indent=1) if a.json else "", end="")
        if not a.json:
            print_survey(s)
        return
    if not a.out_dir:
        sys.exit("out_dir is required (or use --list-folders)")
    out = os.path.abspath(a.out_dir)
    if out == vault or out.startswith(vault + os.sep):
        sys.exit("out_dir must be outside the vault")
    if os.path.exists(out) and os.listdir(out):
        sys.exit(f"out_dir {out} is not empty; choose a new folder")

    if a.all:
        rules, base = [], True
    elif a.defaults:
        rules, base = [(f, False) for f in DEFAULT_EXCLUDE] + [(f, True) for f in a.include], True
        rules += [(f, False) for f in a.exclude]
    elif a.include or a.exclude:
        rules, base = [(f, True) for f in a.include] + [(f, False) for f in a.exclude], not a.include
    elif sys.stdin.isatty():
        rules, a.originals, a.strip_my_notes = ask_terminal(survey(vault))
        base = False
    else:
        print("Choose what to export first. Ask the person which folders to include, then pass --defaults, "
              "--include, --exclude or --all (and --originals / --strip-my-notes if wanted).\n", file=sys.stderr)
        print_survey(survey(vault))
        sys.exit(2)

    known = {f.lower() for f in units(vault)}
    for dp, dirs, _f in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        known.add(os.path.relpath(dp, vault).replace(os.sep, "/").lower())
    for f, _inc in rules:
        if f.strip("/").lower() not in known:
            print(f"WARN no folder named {f!r} in the vault (rule ignored)", file=sys.stderr)
    files = list(walk(vault))
    resolver = Resolver([r for r, _ in files]).add_aliases(vault)
    notes, left_out, shared_off = {}, {}, 0
    for rel, ab in files:
        if not rel.lower().endswith(".md"):
            continue
        if rel in NEVER or is_internal_root_file(rel) or rel.rsplit("/", 1)[-1] in ("index.md", "log.md"):
            continue
        top = unit_of(rel)
        if not decide(rel, rules, base):
            left_out[top] = left_out.get(top, 0) + 1
            continue
        text, fm = read_fm(ab)
        if share_false(fm):
            shared_off += 1
            continue
        notes[rel] = (text, fm)
    ctx = Ctx(resolver, set(notes), a.originals, rules, base)
    by_layer, by_folder, by_type = {}, {}, {}
    os.makedirs(out, exist_ok=True)
    for rel, (text, fm) in notes.items():
        layer = layer_of(fm)
        if a.strip_my_notes:
            text = strip_mine(text)
        new = set_layer(convert(text, ctx, rel, site_origin(fm)), layer)
        dest = os.path.join(out, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        by_layer[layer] = by_layer.get(layer, 0) + 1
        top = unit_of(rel)
        by_folder[top] = by_folder.get(top, 0) + 1
        t = str(fm.get("type") or "Untyped")
        by_type[t] = by_type.get(t, 0) + 1
    n_att = n_orig = size = 0
    for rel, to in sorted(ctx.copy.items()):
        src = os.path.join(vault, *rel.split("/"))
        if not os.path.isfile(src):
            continue
        dest = os.path.join(out, *to.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        size += os.path.getsize(src)
        n_orig += is_original(rel)
        n_att += 1

    # AGENTS.md (guide) and CLAUDE.md (pointer)
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo(a.tz)).replace(microsecond=0)
    except Exception:  # noqa: BLE001
        now = dt.datetime.now().astimezone().replace(microsecond=0)
    contents = ["| folder | notes |", "|---|---|"] + [f"| `{k}/` | {v:,} |" for k, v in sorted(by_folder.items())]
    contents += ["", f"{len(notes):,} notes and {n_att:,} attachments ({size / 1e6:,.0f} MB)."]
    if left_out:
        contents.append("Left out by the owner's choice: " + ", ".join(
            f"`{k}/` ({plural(v, 'note')})" for k, v in sorted(left_out.items())) + ".")
    if shared_off:
        contents.append(f"Also left out: {plural(shared_off, 'note')} marked `share: false`.")
    contents.append("Original PDFs and e-books: " + (f"included ({n_orig:,} files; their page text is searchable)."
                                                     if a.originals else "not included."))
    contents.append(f"The owner's own notes inside items (`my_note`, \"My notes\" sections): "
                    + ("removed." if a.strip_my_notes else "kept, with layer `mine`."))
    layer_counts = "Notes by layer: " + ", ".join(f"{k} {v:,}" for k, v in sorted(by_layer.items(), key=lambda x: -x[1])) + "."
    gpath = os.path.join(HERE, "..", "assets", "okf-export-guide.md")
    if not os.path.exists(gpath):
        sys.exit(f"reading-guide template missing: {gpath} (run exports from the laptop skill source)")
    with open(gpath, encoding="utf-8") as f:
        guide = string.Template(f.read()).substitute(
            owner=a.owner, export_date=now.strftime("%Y-%m-%d"), generated_at=now.isoformat(timespec="seconds"),
            contents="\n".join(contents), layer_counts=layer_counts)
    with open(os.path.join(out, "AGENTS.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(guide)
    with open(os.path.join(out, "CLAUDE.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("---\ntype: Agent Instructions\ntitle: Claude instructions for this bundle\n"
                "description: Points Claude to AGENTS.md, the reading guide for this knowledge bundle.\n"
                f"generated: {{ by: process:okf_export, at: {now.isoformat(timespec='seconds')} }}\n---\n\n"
                "# Claude instructions\n\nRead [AGENTS.md](/AGENTS.md) before answering from this bundle.\n\n@AGENTS.md\n")

    # indexes rebuilt from the exported notes only
    import build_indexes as BI
    queue = BI.build(out, "", True, True, a.okf_version)
    while queue:
        queue = queue[1:] + BI.build(out, queue[0], True, True, a.okf_version)

    # search tool and index
    tools = os.path.join(out, "_tools")
    os.makedirs(tools, exist_ok=True)
    shutil.copy2(os.path.join(HERE, "okf_search.py"), os.path.join(tools, "okf_search.py"))
    with open(os.path.join(tools, "README.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(README.format(owner=a.owner, date=now.strftime("%Y-%m-%d")))
    if not a.no_search:
        import okf_search as OS
        from pathlib import Path
        OS.build(Path(out), originals=a.originals)
        import sqlite3
        con = sqlite3.connect(os.path.join(tools, "search.db"))
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('built', ?)", (now.isoformat(),))
        con.commit()
        con.close()
    manifest = {"okf_version": a.okf_version, "exported_at": now.isoformat(timespec="seconds"), "owner": a.owner,
                "exporter": "vault-librarian okf_export.py", "rules": [{"folder": f, "include": i} for f, i in rules],
                "base_include": base, "originals": a.originals, "strip_my_notes": a.strip_my_notes,
                "notes": len(notes), "attachments": n_att, "original_files": n_orig, "by_folder": by_folder,
                "by_layer": by_layer, "by_type": by_type, "left_out_by_folder": left_out,
                "left_out_share_false": shared_off, "links_converted": ctx.links,
                "links_to_left_out_items": ctx.dropped_links, "site_relative_links_made_absolute": ctx.site_links, "problems": ctx.problems}
    with open(os.path.join(tools, "export-manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)

    for p in ctx.problems:
        print("WARN", p)
    print(f"\nExported {len(notes):,} notes and {n_att:,} attachments ({size / 1e6:,.0f} MB) to {out}; "
          f"left out {sum(left_out.values()):,} notes by folder and {shared_off} marked share: false; "
          f"{ctx.dropped_links:,} links to left-out items made plain text; {len(ctx.problems)} problems.")
    if a.zip:
        z = shutil.make_archive(out, "zip", out)
        print(f"Zipped: {z} ({os.path.getsize(z) / 1e6:,.0f} MB)")


README = """Knowledge bundle exported from {owner}'s Obsidian vault on {date} (OKF v0.2).

For an AI assistant: point it at AGENTS.md in the folder above; it explains the notes and how to search them.

Search from a terminal (needs Python 3.8 or later, nothing else):
    python _tools/okf_search.py search "your question" --variant "another wording"
    python _tools/okf_search.py get "Note name" --heading "Section"

Give Claude (or another MCP client) the search tools:
    Claude Code, from the bundle folder:
        claude mcp add okf-bundle -- python "<full path>/_tools/okf_search.py" mcp
    Claude Desktop: Settings > Developer > Edit Config, then add under "mcpServers":
        "okf-bundle": {{"command": "python", "args": ["<full path>/_tools/okf_search.py", "mcp"]}}
    (on Windows write the path with forward slashes or doubled backslashes), then restart Claude Desktop.

Optional search by meaning: pip install fastembed numpy, then python _tools/okf_search.py embed (once).
After editing notes: python _tools/okf_search.py build
export-manifest.json records exactly what was exported and what was left out.
"""


if __name__ == "__main__":
    main()
