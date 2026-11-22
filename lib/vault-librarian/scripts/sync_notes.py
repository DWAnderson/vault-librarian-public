#!/usr/bin/env python3
"""Keep marked Cowork .md files in sync with the vault — one way, Cowork is the master.

Usage:
  python sync_notes.py mark <file.md>... [--unmark]
  python sync_notes.py scan  <cowork_root> <vault> [--json out.json]
  python sync_notes.py apply <cowork_root> <vault> [--only SRC ...] [--force SRC ...]
                             [--adopt SRC=VAULT_REL ...] [--as-new SRC ...] [--dry-run] [--json out.json]

Marker: a source file is synced when its frontmatter has `obsidian_sync: true`
(`mark` adds the key, creating a frontmatter block if needed; `--unmark` removes it).
SRC is the file's path relative to <cowork_root>, with forward slashes.

Each vault copy records where it came from, so it is found again after it is renamed
or filed anywhere in the vault:
  synced_from: "<path relative to cowork_root>"
  synced_hash: <hash of the source body at last sync>
  synced_body_hash: <hash of the vault body as written at last sync>
  synced_at: <ISO 8601 time of last sync>

Statuses reported by `scan`:
  new        marked source with no vault copy yet (lands in Inbox/ on apply). If a vault
             note looks like an untracked earlier copy (same name, alias or title, or
             >= 90% identical text) it is listed under `candidates`; apply then needs
             --adopt SRC=VAULT_REL (replace that note in place) or --as-new SRC.
  changed    source edited since last sync; apply replaces the vault body in place and
             keeps the vault copy's name, folder, tags, type and other frontmatter.
  conflict   the vault body was edited (in Obsidian) since last sync; skipped unless --force.
  unchanged  nothing to do.
  orphaned   vault copy whose source was deleted or unmarked — left alone, reported only.

Body handling: kept word for word except Markdown links to other local files, which become
[[wikilinks]] to that file's vault copy when it has one, or plain text otherwise. Embedded local
IMAGES (![alt](pic.png) or ![alt](<pic with spaces.png>)) are copied into the vault under
<attachments>/Synced/<path under cowork_root> and embedded there as ![[...]]; a changed image
counts as a changed source (2026-10-12).

Where NEW copies land (2026-10-12: "Claude generated items I asked to be sync'd should
generally go in a Claude subfolder"): a Claude-generated source (frontmatter generated.by starting
with "claude/", or a managed copy carrying copy_of) is created directly in Claude/Other
(Claude/Chats for type Claude Chat, Claude/Memories for memory exports); everything else in Inbox/.
A source may name another vault folder with `obsidian_folder: <vault folder>`.
Before a vault body is replaced, the prior version is saved under
<vault>/.sync-backups/<date>/ (hidden from Obsidian). A changed body drops `verified`
(the owner confirmed the old text, not the new) and refreshes `generated.at`.
Every change is appended to log.md.
"""
import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vaultlib import MDLINK_RE, RESERVED, mask_code, split_frontmatter, walk  # noqa: E402
from apply_plan import clean_name, set_keys  # noqa: E402
from vaultlib import vault_rel  # noqa: E402

MARK = "obsidian_sync"
TRACK_KEYS = ("synced_from", "synced_hash", "synced_body_hash", "synced_at")
SKIP_SRC_DIRS = {"runtime", "_session", "_session_maint", "_lastrun", "site-packages", "node_modules",
                 ".git", ".obsidian", "_snapshots", "_to_delete", "__pycache__", ".sync-backups"}
FM_RE = re.compile(r"(?s)^---[ \t]*\r?\n.*?\r?\n---[ \t]*(\r?\n|$)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
# Like vaultlib.MDLINK_RE, but also takes <targets with spaces> (CommonMark angle-bracket form).
LINK_RE = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*(?:<([^>\n]+)>|([^)\s>]+))(?:\s+\"[^\"]*\")?\s*\)")
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}


def is_claude_generated(fm):
    fm = fm or {}
    g = fm.get("generated")
    by = str(g.get("by") or "") if isinstance(g, dict) else ""
    return by.startswith("claude/") or bool(fm.get("copy_of"))


def dest_folder(fm):
    """Vault folder for a NEW synced copy (see the module docstring)."""
    fm = fm or {}
    own = str(fm.get("obsidian_folder") or "").strip().strip("/").replace("\\", "/")
    if own:
        return own
    if not is_claude_generated(fm):
        return "Inbox"
    t = str(fm.get("type") or "").lower()
    if t == "claude chat":
        return "Claude/Chats"
    if "memor" in t:
        return "Claude/Memories"
    return "Claude/Other"


def local_images(body, src_abs):
    """[(link target as written, absolute path)] for embedded local images that exist."""
    out = []
    for m in LINK_RE.finditer(mask_code(body)):
        href = m.group(3) or m.group(4)
        if m.group(1) != "!" or SCHEME_RE.match(href):
            continue
        path = unquote(href).partition("#")[0]
        if os.path.splitext(path)[1].lower() not in IMG_EXT:
            continue
        ab = os.path.normpath(os.path.join(os.path.dirname(src_abs), path.replace("/", os.sep)))
        if os.path.isfile(ab):
            out.append((href, ab))
    return out


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def src_digest(body, src_abs):
    """Hash of a source: its body, plus its embedded images when it has any."""
    imgs = local_images(body, src_abs)
    if not imgs:
        return digest(body)
    return digest(body + "\n" + "\n".join(file_hash(ab) for _, ab in imgs))


def jsonable(v):
    """YAML dates/datetimes (e.g. an unquoted copied_at) -> ISO strings, so frontmatter can be serialized."""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    return v


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def body_of(text):
    m = FM_RE.match(text)
    return text[m.end():] if m else text


def digest(body):
    norm = "\n".join(ln.rstrip() for ln in body.strip().split("\n"))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def is_marked(fm):
    v = (fm or {}).get(MARK)
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes"))


# ---------------------------------------------------------------- mark

def cmd_mark(a):
    rc = 0
    for p in a.files:
        if not os.path.isfile(p) or not p.lower().endswith(".md"):
            print(f"SKIPPED {p}: not an .md file")
            rc = 1
            continue
        text = read(p)
        fm, err, _ = split_frontmatter(text)
        if err:
            print(f"SKIPPED {p}: frontmatter unreadable ({err})")
            rc = 1
            continue
        if a.unmark:
            if MARK not in (fm or {}):
                print(f"unchanged {p} (not marked)")
                continue
            new = set_keys(text, {}, [MARK])
            new = re.sub(r"^---\n\s*---\n", "", new).lstrip("\n")  # block held only the marker
            write(p, new)
            print(f"unmarked {p}")
        else:
            if is_marked(fm):
                print(f"unchanged {p} (already marked)")
                continue
            write(p, set_keys(text, {MARK: True}))
            print(f"marked {p}")
    return rc


# ---------------------------------------------------------------- scan

def find_sources(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_SRC_DIRS and not d.startswith("."))
        for f in sorted(files):
            if not f.lower().endswith(".md"):
                continue
            ab = os.path.join(dirpath, f)
            try:
                with open(ab, encoding="utf-8-sig", errors="replace") as fh:
                    head = fh.read(4096)
            except OSError:
                continue
            if MARK not in head:
                continue
            text = read(ab)
            fm, err, _ = split_frontmatter(text)
            if not err and is_marked(fm):
                out[os.path.relpath(ab, root).replace(os.sep, "/")] = ab
    return out


def vault_notes(vault):
    notes = {}
    for rel, ab in walk(vault):
        if not rel.lower().endswith(".md") or rel.rsplit("/", 1)[-1] in RESERVED:
            continue
        text = read(ab)
        fm, err, _ = split_frontmatter(text)
        notes[rel] = {"abs": ab, "text": text, "fm": fm or {}, "err": err}
    return notes


def prior_copy_candidates(src_rel, src_text, notes):
    base = src_rel.rsplit("/", 1)[-1][:-3].lower()
    sfm, _, sbody = split_frontmatter(src_text)
    stitle = str((sfm or {}).get("title") or "").strip().lower()
    sbody = sbody.strip()
    hits = []
    for rel, n in notes.items():
        if n["fm"].get("synced_from"):
            continue
        name = rel.rsplit("/", 1)[-1][:-3].lower()
        al = n["fm"].get("aliases") or []
        al = [str(x).lower() for x in (al if isinstance(al, list) else [al])]
        title = str(n["fm"].get("title") or "").strip().lower()
        why = None
        if name == base or base in al:
            why = "same name"
        elif stitle and title == stitle:
            why = "same title"
        elif sbody:
            vb = body_of(n["text"]).strip()
            sm = difflib.SequenceMatcher(None, sbody, vb, autojunk=False)
            if vb and sm.real_quick_ratio() >= 0.9 and sm.quick_ratio() >= 0.9 and sm.ratio() >= 0.9:
                why = f"{round(sm.ratio() * 100)}% same text"
        if why:
            hits.append({"vault": rel, "why": why})
    return hits


def scan(root, vault):
    sources = find_sources(root)
    notes = vault_notes(vault)
    tracked = {}
    res = {"new": [], "changed": [], "conflict": [], "unchanged": [], "orphaned": [], "errors": []}
    for rel, n in notes.items():
        sf = n["fm"].get("synced_from")
        if sf:
            tracked.setdefault(str(sf), []).append(rel)
    for sf, rels in tracked.items():
        if len(rels) > 1:
            res["errors"].append({"src": sf, "error": f"more than one vault note claims this source: {rels}"})
        if sf not in sources:
            gone = not os.path.isfile(os.path.join(root, *sf.split("/")))
            for r in rels:
                res["orphaned"].append({"src": sf, "vault": r,
                                        "why": "source deleted" if gone else "source no longer marked"})
    for src_rel, ab in sources.items():
        text = read(ab)
        sh = src_digest(body_of(text), ab)
        rels = tracked.get(src_rel, [])
        if len(rels) > 1:
            continue
        if not rels:
            res["new"].append({"src": src_rel, "candidates": prior_copy_candidates(src_rel, text, notes)})
            continue
        n = notes[rels[0]]
        vault_edited = digest(body_of(n["text"])) != str(n["fm"].get("synced_body_hash"))
        src_changed = sh != str(n["fm"].get("synced_hash"))
        item = {"src": src_rel, "vault": rels[0]}
        if vault_edited:
            item["why"] = "vault copy edited in Obsidian since last sync" + \
                ("" if src_changed else " (source unchanged)")
            res["conflict"].append(item)
        elif src_changed:
            res["changed"].append(item)
        else:
            res["unchanged"].append(item)
    return res, sources, notes


# ---------------------------------------------------------------- apply

def convert_links(body, src_abs, root, vault_name_for, images=None, att=None):
    """Markdown links to local files -> [[wikilink]] to the vault copy, or plain text.
    Embedded local images -> ![[<att>/Synced/<path under root>]]; the copies to make are appended to `images`."""
    scan_text = mask_code(body)
    out, last, n_wiki, n_plain = [], 0, 0, 0
    for m in LINK_RE.finditer(scan_text):
        bang, text, href = m.group(1), m.group(2), (m.group(3) or m.group(4))
        if SCHEME_RE.match(href) or href.startswith("#"):
            continue
        path, _, frag = unquote(href).partition("#")
        if not path:
            continue
        target = os.path.normpath(os.path.join(os.path.dirname(src_abs), path.replace("/", os.sep)))
        rel_in_root = os.path.relpath(target, root).replace(os.sep, "/")
        name = vault_name_for.get(rel_in_root)
        label = text.strip() or os.path.basename(path)
        if (bang and images is not None and att and os.path.isfile(target)
                and os.path.splitext(path)[1].lower() in IMG_EXT and not rel_in_root.startswith("..")):
            dest = f"{att}/Synced/{rel_in_root}"
            images.append((target, dest))
            out.append(body[last:m.start()] + f"![[{dest}]]")
            last = m.end()
            n_wiki += 1
            continue
        if name and not bang:
            repl = f"[[{name}{'#' + frag if frag else ''}|{label}]]" if label != name else f"[[{name}]]"
            n_wiki += 1
        else:
            repl = label
            n_plain += 1
        out.append(body[last:m.start()] + repl)
        last = m.end()
    out.append(body[last:])
    return "".join(out), n_wiki, n_plain


def unique_name(base, top, taken):
    cand = clean_name(base) or "Untitled"
    if cand.lower() + ".md" not in taken:
        return cand
    cand2 = f"{cand} ({top})" if top else cand
    i = 2
    c = cand2
    while c.lower() + ".md" in taken:
        c = f"{cand2} {i}"
        i += 1
    return c


def copy_images(vault, imgs, log, src, dry=False):
    """Copy embedded images into the vault when missing or different. Returns how many were copied."""
    n = 0
    for ab, dest in imgs:
        out = os.path.join(vault, *dest.split("/"))
        if os.path.isfile(out) and file_hash(out) == file_hash(ab):
            continue
        n += 1
        if dry:
            print(f"would copy image {dest}")
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(ab, out)
        log.append(f"- Copied image `{dest}` for synced note from Cowork `{src}`")
    return n


def backup(vault, rel, text, stamp):
    dest = os.path.join(vault, ".sync-backups", stamp, *rel.split("/"))
    if os.path.exists(dest):
        return
    write(dest, text)


def cmd_apply(a):
    root, vault = os.path.abspath(a.cowork_root), os.path.abspath(a.vault)
    res, sources, notes = scan(root, vault)
    only = set(a.only or [])
    force = set(a.force or [])
    as_new = set(a.as_new or [])
    adopt = {}
    for spec in a.adopt or []:
        s, _, v = spec.partition("=")
        adopt[s.strip()] = v.strip().replace("\\", "/")
    pick = lambda s: not only or s in only
    taken = {rel.rsplit("/", 1)[-1].lower() for rel in notes}
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    now = now_iso()
    att = vault_rel(vault, "attachments")
    log, skipped, done = [], [], {"new": [], "updated": [], "adopted": []}

    # Work list: (src, vault_rel or None for new, mode)
    work = []
    for it in res["new"]:
        s = it["src"]
        if not pick(s):
            continue
        if s in adopt:
            v = adopt[s]
            if v not in notes or notes[v]["fm"].get("synced_from"):
                skipped.append(f"{s}: cannot adopt '{v}' (not found or already synced)")
                continue
            work.append((s, v, "adopt"))
        elif it["candidates"] and s not in as_new:
            c = ", ".join(f"{x['vault']} ({x['why']})" for x in it["candidates"])
            skipped.append(f"{s}: possible earlier copy in vault: {c} — pass --adopt or --as-new")
        else:
            work.append((s, None, "new"))
    for it in res["changed"]:
        if pick(it["src"]):
            work.append((it["src"], it["vault"], "update"))
    for it in res["conflict"]:
        if not pick(it["src"]):
            continue
        if it["src"] in force:
            work.append((it["src"], it["vault"], "update"))
        else:
            skipped.append(f"{it['src']}: {it['why']} ({it['vault']}) — pass --force to overwrite")

    # Pass 1: decide every vault name, so links between synced files resolve.
    vault_name_for = {}
    for rel, n in notes.items():
        sf = n["fm"].get("synced_from")
        if sf:
            vault_name_for[str(sf)] = rel[:-3]
    dest_for = {}
    for s, v, mode in work:
        if mode == "new":
            top = s.split("/", 1)[0] if "/" in s else ""
            name = unique_name(s.rsplit("/", 1)[-1][:-3], top, taken)
            taken.add(name.lower() + ".md")
            sfm0, _e0, _b0 = split_frontmatter(read(sources[s]))
            dest_for[s] = f"{dest_folder(sfm0)}/{name}.md"
        else:
            dest_for[s] = v
        vault_name_for[s] = dest_for[s][:-3]
    # Prefer bare note names in wikilinks when unique vault-wide.
    all_names = {}
    for rel in list(notes) + [d for s, d in dest_for.items() if d not in notes]:
        all_names.setdefault(rel.rsplit("/", 1)[-1].lower(), []).append(rel)
    for s, rel in list(vault_name_for.items()):
        bn = rel.rsplit("/", 1)[-1]
        if len(all_names.get(bn.lower() + ".md", [])) <= 1:
            vault_name_for[s] = bn

    # Pass 2: write.
    for s, v, mode in work:
        src_abs = sources[s]
        src_text = read(src_abs)
        sfm, err, _ = split_frontmatter(src_text)
        if err:
            skipped.append(f"{s}: source frontmatter unreadable ({err})")
            continue
        src_body = body_of(src_text)
        imgs = []
        body, n_wiki, n_plain = convert_links(src_body, src_abs, root, vault_name_for, imgs, att)
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(src_abs)).astimezone().isoformat(timespec="seconds")
        track = {"synced_from": s, "synced_hash": src_digest(src_body, src_abs),
                 "synced_body_hash": digest(body), "synced_at": now}
        dest_rel = dest_for[s]
        links = f" ({n_wiki} links → wikilinks, {n_plain} → plain text)" if n_wiki or n_plain else ""
        if mode == "new":
            carry = {k: jsonable(val) for k, val in (sfm or {}).items()
                     if k != MARK and k not in TRACK_KEYS and k != "obsidian_folder"}
            text = set_keys(body.lstrip("\n"), {**carry, **track}) if carry or track else body
            if a.dry_run:
                print(f"would create {dest_rel} from {s}{links}")
                copy_images(vault, imgs, log, s, dry=True)
                continue
            write(os.path.join(vault, *dest_rel.split("/")), text)
            copy_images(vault, imgs, log, s)
            done["new"].append({"src": s, "vault": dest_rel})
            log.append(f"- Synced new note `{dest_rel}` from Cowork `{s}`{links}")
        else:
            old = notes[v]["text"]
            m = FM_RE.match(old)
            head = old[:m.end()] if m else ""
            new_text = head + ("\n" if head and not body.startswith("\n") else "") + body
            upd = dict(track)
            gen = notes[v]["fm"].get("generated")
            if isinstance(gen, dict) and gen.get("by"):
                upd["generated"] = {"by": gen["by"], "at": mtime}
            if mode == "adopt":  # bring over source keys the earlier copy lacks
                for k, val in (sfm or {}).items():
                    if k != MARK and k not in TRACK_KEYS and k not in notes[v]["fm"]:
                        upd[k] = jsonable(val)
            unset = [k for k in ("verified",) if k in notes[v]["fm"]]
            new_text = set_keys(new_text, upd, unset)
            if a.dry_run:
                print(f"would {'adopt' if mode == 'adopt' else 'update'} {v} from {s}{links}")
                copy_images(vault, imgs, log, s, dry=True)
                continue
            backup(vault, v, old, stamp)
            write(notes[v]["abs"], new_text)
            copy_images(vault, imgs, log, s)
            key = "adopted" if mode == "adopt" else "updated"
            done[key].append({"src": s, "vault": v, "dropped_verified": bool(unset)})
            verb = "Adopted earlier copy" if mode == "adopt" else "Updated synced note"
            log.append(f"- {verb} `{v}` from Cowork `{s}`{links}"
                       + ("; removed `verified` (content changed)" if unset else "")
                       + f"; prior version in `.sync-backups/{stamp}/`")

    summary = {"applied": done, "skipped": skipped,
               "unchanged": [x["src"] for x in res["unchanged"]],
               "orphaned": res["orphaned"], "errors": res["errors"], "dry_run": a.dry_run}
    if a.json:
        write(a.json, json.dumps(summary, indent=2, ensure_ascii=False))
    for k in ("new", "updated", "adopted"):
        for x in done[k]:
            print(f"{k.upper():8} {x['src']} -> {x['vault']}")
    for s in skipped:
        print("SKIPPED ", s)
    for o in res["orphaned"]:
        print(f"ORPHANED {o['vault']} ({o['why']}: {o['src']})")
    for e in res["errors"]:
        print(f"ERROR    {e['src']}: {e['error']}")
    print(f"{len(res['unchanged'])} unchanged")
    if log and not a.dry_run:
        lp = os.path.join(vault, "log.md")
        today = dt.date.today().isoformat()
        existing = read(lp) if os.path.exists(lp) else "# Vault Update Log\n"
        entry = f"\n## {today}\n\n**Cowork sync**\n\n" + "\n".join(log) + "\n"
        if skipped:
            entry += "\nSkipped:\n" + "\n".join(f"- {s}" for s in skipped) + "\n"
        write(lp, existing.rstrip() + "\n" + entry)
        print(f"{len(log)} changes applied; logged to log.md")
    return 0


def cmd_scan(a):
    res, _, _ = scan(os.path.abspath(a.cowork_root), os.path.abspath(a.vault))
    if a.json:
        write(a.json, json.dumps(res, indent=2, ensure_ascii=False))
    for it in res["new"]:
        c = "; possible earlier copy: " + ", ".join(f"{x['vault']} ({x['why']})" for x in it["candidates"]) \
            if it["candidates"] else ""
        print(f"NEW       {it['src']}{c}")
    for it in res["changed"]:
        print(f"CHANGED   {it['src']} -> {it['vault']}")
    for it in res["conflict"]:
        print(f"CONFLICT  {it['src']} -> {it['vault']}: {it['why']}")
    for it in res["orphaned"]:
        print(f"ORPHANED  {it['vault']} ({it['why']}: {it['src']})")
    for e in res["errors"]:
        print(f"ERROR     {e['src']}: {e['error']}")
    print(f"{len(res['unchanged'])} unchanged")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mark")
    m.add_argument("files", nargs="+")
    m.add_argument("--unmark", action="store_true")
    for name in ("scan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("cowork_root")
        p.add_argument("vault")
        p.add_argument("--json")
        if name == "apply":
            p.add_argument("--only", nargs="*")
            p.add_argument("--force", nargs="*")
            p.add_argument("--adopt", nargs="*")
            p.add_argument("--as-new", nargs="*", dest="as_new")
            p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd != "mark" and not os.path.isdir(os.path.join(a.vault, ".obsidian")):
        sys.exit(f"{a.vault} has no .obsidian folder — not a vault?")
    sys.exit({"mark": cmd_mark, "scan": cmd_scan, "apply": cmd_apply}[a.cmd](a))


if __name__ == "__main__":
    main()
