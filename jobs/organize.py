#!/usr/bin/env python3
"""Organize by email (vault-librarian Organize, unattended half) on this PC.

  python organize.py --prep [--dry-run]   06:00 task: if Inbox/Clippings hold unfiled notes, claude -p
                                          drafts a filing proposal (renames, type/description/tags,
                                          and lettered questions for where things go) and emails it.
  check_replies(cfg, log, session, base)  called by intake.py every 30 min: finds the owner's reply
                                          ("1a 2c 3b", "ok" = accept defaults, "skip"), applies the
                                          matching plan with apply_plan.py, rebuilds indexes/hubs,
                                          lints the touched notes and emails a short result.
Proposal files: reports\\organize-<date>.json (+ .md). Nothing moves without the owner's reply;
unanswered questions leave their notes in the Inbox. Only approved tags (_claude-tags.md) are used;
new tags are asked as yes/no questions. Renames follow the intake-note rule (apply_plan enforces it).
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import claude_cli  # noqa: E402
import update_email as U  # noqa: E402

PARA = ("Projects", "Areas", "Resources", "Archives", "Subjects", "Manuals", "Personal Notes/Personal")
# Subjects/ and Manuals/ (2026-10-07): only EXISTING subfolders may be chosen; a new one needs his yes.
FIXED_SUBS = ("Subjects", "Manuals")
TAG = "Vault organize"
PROMPT = """You are preparing a filing proposal for «owner's» Obsidian vault, following the vault-librarian rules.
The JSON on stdin has: notes (unfiled Inbox/Clippings notes: path, frontmatter, start of text), folders
(existing destination folders), tags (the ONLY approved tags), types (allowed note types).
Rules: PARA - Projects = defined outcome/deadline; Areas = ongoing responsibility; Resources = reference;
Archives = inactive. Manuals = product manuals, quick-start guides, warranties and spec sheets (type Manual):
file them in the matching existing Manuals/<subfolder> (Home, Home IT, Media, Computer Notes, Games, Other...).
Subjects = incoming material (articles, videos, podcasts, recipes, emails) on a subject that has an existing
Subjects/<subject> folder. Personal Notes/Personal is ONLY for notes the owner wrote personally (type Personal Note or
generated.by «owner_id») - never file incoming items there. Never choose a Subjects/ or Manuals/ subfolder that is
not in the folders list (Subjects folders can be nested: Subjects/Economics/Housing).
Learn from past_choices (the owner's earlier decisions: what you suggested and where they actually filed it): when
they overrode a suggestion, file similar items where they put them; prefer folders they have accepted for similar items.
folder_examples shows some notes already in each Subjects/Manuals folder.
NAMES: a note that summarizes a work (type Book/Video/Podcast/Web Summary, or content: summary) is named
"<work> (Summary)"; transcripts "<work> (Transcript)"; <work> is "Title (Author year)" or "Title (Channel)" when the
source is known. When you rename such a note keep its " (Summary)" or " (Summary and original)" ending. name_corrections lists the owner's own recent
renames (before -> after): imitate them.
NEW SUBJECT FOLDERS: when at least 2 of these notes share a subject that has no folder, add a "new_folders" entry
(folder = "Subjects/<Name>" or a subfolder of an existing one, e.g. "Subjects/Economics/Housing"; for manuals
"Manuals/<Name>"; short Title Case name). Still put those notes in a normal question (option a = best EXISTING
folder). Never propose a folder listed in declined_folders.
Rename only if the current name is unclear: short subject-based title, 3-8 words, sentence case, source or
year in parentheses if helpful, no characters \\ / : * ? " < > | # ^ [ ]. Never invent tags: use only the
approved list; if a clearly useful tag is missing, propose it as a yes/no question.
Group notes with the same likely destination into ONE question. Each question has 2-4 options; option "a"
is your recommendation. Every note must appear in exactly one question.
Output ONLY this JSON (no prose, no code fence):
{"items":[{"path":"Inbox/x.md","rename_to":null,"set":{"description":"one sentence","tags":["..."],"type":"..."}}],
 "questions":[{"id":1,"text":"3 notes on hangar leases - where?","paths":["Inbox/x.md"],
   "options":{"a":{"label":"Resources (reference)","move_to":"Resources"},"b":{"label":"Areas/Aviation","move_to":"Areas/Aviation"}}}],
 "new_tags":[{"id":9,"tag":"example","covers":"what it covers","paths":["Inbox/x.md"]}],
 "new_folders":[{"id":10,"folder":"Subjects/Economics","covers":"what belongs there","paths":["Inbox/x.md","Inbox/y.md"]}]}
Only include "type" in set when the note has none or it is clearly wrong; keep existing good descriptions."""


def held_stub_paths():
    """Vault paths of Web Clipper stubs waiting in queue\\held.json for the laptop browser (nightly.py)."""
    try:
        held = json.loads((C.APP / "queue" / "held.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {h["stub_path"] for h in held.values() if h.get("stub_path") and h.get("status") == "held"}


def unfiled(vault, limit):
    out, skip = [], held_stub_paths()
    for folder in ("Inbox", "Clippings"):
        for p in sorted((vault / folder).glob("*.md")):
            if p.name.lower() in ("index.md", "log.md") or p.relative_to(vault).as_posix() in skip:
                continue
            out.append(p)
    return out[:limit]


def summarize_note(vault, p):
    text = p.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"(?s)^---\s*\n(.*?)\n---\s*\n?(.*)$", text)
    fm, body = (m.group(1), m.group(2)) if m else ("", text)
    return {"path": p.relative_to(vault).as_posix(), "frontmatter": fm[:1200], "text": body.strip()[:700]}


# ------------------------------------------------------------------ Summary + Transcript pairs (2026-09-26)
# The Remember job and the Daily Summary's podcast Save to Vault write a summary note plus a separate
# Transcript note whose body starts "Summary: [[<summary name>]]" and which the summary links back to.
# Organize files a pair as ONE item: the transcript is left out of the proposal, follows its summary's
# folder and tags, is renamed "<work> (Transcript)" (vault_names, 2026-10-10) when the summary is renamed, and the
# two notes' links to each other are rewritten after the rename (plan section 5).
_PAIR_LINK = re.compile(r"^\s*Summary:\s*\[\[([^\]|#]+)")


def find_pairs(vault, paths):
    """{summary vault-relative path: transcript vault-relative path} among the given unfiled notes."""
    by_stem = {Path(p).stem: p for p in paths}
    pairs = {}
    for p in paths:
        text = (vault / p).read_text(encoding="utf-8", errors="replace")
        m = re.match(r"(?s)^---\s*\n(.*?)\n---\s*\n?(.*)$", text)
        fm, body = (m.group(1), m.group(2)) if m else ("", text)
        if not re.search(r'(?m)^type:\s*"?Transcript"?\s*$', fm):
            continue
        lm = _PAIR_LINK.match(body)
        if lm and lm.group(1).strip() in by_stem:
            pairs[by_stem[lm.group(1).strip()]] = p
    return pairs


def _clean_name(n):
    """The name apply_plan.py actually gives a renamed note (its clean_name), without '.md'."""
    try:
        sys.path.insert(0, str(C.VL))
        from apply_plan import clean_name
        n = clean_name(n)
    except Exception:  # noqa: BLE001
        pass
    return n[:-3] if n.lower().endswith(".md") else n


def fix_pair_links(vault, plan_items, log=print):
    """After apply_plan: rewrite [[old]] -> [[new]] inside renamed pairs, only where the new file exists."""
    dest = {}
    for it in plan_items:
        old = it["path"]
        folder = it.get("move_to") or str(Path(old).parent).replace("\\", "/")
        name = _clean_name(it["rename_to"]) if it.get("rename_to") else Path(old).stem
        dest[old] = (Path(old).stem, name, vault / folder / f"{name}.md")
    for it in plan_items:
        partner = it.get("pair_with")
        if not partner or partner not in dest:
            continue
        me_old, me_new, me_path = dest[it["path"]]
        pa_old, pa_new, pa_path = dest[partner]
        if pa_old == pa_new or not me_path.exists() or not pa_path.exists():
            continue
        t = me_path.read_text(encoding="utf-8")
        t2 = t.replace(f"[[{pa_old}]]", f"[[{pa_new}]]").replace(f"[[{pa_old}|", f"[[{pa_new}|")
        if t2 != t:
            me_path.write_text(t2, encoding="utf-8")
            log(f"pair link updated in {me_path.name}: [[{pa_old}]] -> [[{pa_new}]]")


# ------------------------------------------------------------------ duplicate questions (2026-09-28)
# Archived duplicates (restore / merge / delete) and weak duplicate pairs (keep one / not duplicates) are
# asked in the same 06:00 email as the filing questions, with the same letters, "ok" and web page.
# Option "a" is always the suggestion. Nothing happens to a duplicate until the owner answers.
DUP_MAX = 15            # questions per morning; the rest wait for the next day
STUB_WORDS = 150        # an archived copy this short (e.g. a Substack share-page stub) is suggested for delete


def _words(vault, rel):
    try:
        text = (vault / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    m = re.match(r"(?s)^---\s*\n.*?\n---\s*\n?(.*)$", text)
    return len(re.findall(r"\w+", m.group(1) if m else text))


def _paragraphs(vault, rel):
    """Number of paragraphs in a note's body (counted like resolve_duplicates.py)."""
    try:
        text = (vault / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    m = re.match(r"(?s)^---\s*\n.*?\n---\s*\n?(.*)$", text)
    return sum(1 for p in re.split(r"\n\s*\n", m.group(1) if m else text) if len(p.strip()) > 3)


def _fm_flag(vault, rel, key):
    try:
        head = (vault / rel).read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    fm = head.split("\n---", 1)[0]
    return bool(re.search(rf"(?m)^{re.escape(key)}:\s*(true|yes)\s*$", fm, re.I))


# ------------------------------------------------------------------ source files behind a duplicate pair (2026-10-12)
# The owner: possible duplicates are often summaries of two copies of the same work. Compare the SOURCE files (not just the
# summaries): same file / same text / one an excerpt of the other / different parts (no shared text, e.g. chapter 1
# and chapter 2 of a book split into files: not asked, per his 2026-10-08 rule) / partly shared. He can then delete
# the duplicate summary together with its source file (and its analysis notes), after opening both files.
SRC_SAME, SRC_PARTS = 0.85, 0.10   # share of 6-word phrases: >= SAME counts as the same text; both < PARTS: different parts
EMBED_RE = re.compile(r"!\[\[([^\]|#]+\.(?:pdf|docx?|epub|mobi|txt|html?|mhtml?|rtf))(?:[#|][^\]]*)?\]\]", re.I)


def _split_note(vault, rel):
    try:
        text = (vault / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    sys.path.insert(0, str(C.VL))
    from vaultlib import split_frontmatter
    fm, _err, body = split_frontmatter(text)
    return (fm if isinstance(fm, dict) else {}), body


def source_file(vault, rel):
    """The file a summary was made from: `resource`/`source_file` (note_view.original_of), else its first embed."""
    import note_view as V
    fm, body = _split_note(vault, rel)
    f = V.original_of(vault, fm, body)
    return f.relative_to(vault).as_posix() if f else None


def _src_hashes(cfg, path):
    """(set of hashed 6-word phrases, pages, sha256) of a source file; cached by size+mtime. Local, no tokens."""
    import hashlib
    import zlib
    st = path.stat()
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}".encode("utf-8")).hexdigest()
    cdir = Path(cfg["local_root"]) / "work" / "srccmp"
    cdir.mkdir(parents=True, exist_ok=True)
    cf = cdir / f"{key}.json"
    if cf.exists():
        d = json.loads(cf.read_text(encoding="utf-8"))
        return set(d["h"]), d.get("pages"), d.get("sha")
    import extract as X
    try:
        res = X.extract(path, cdir / f"{key}-x", ocr=False)
        text, pages = res.get("text") or "", res.get("pages")
    except Exception:  # noqa: BLE001  unreadable: compared by bytes only
        text, pages = "", None
    w = re.findall(r"[a-z]{3,}", text.lower())
    h = sorted({zlib.crc32(" ".join(w[i:i + 6]).encode("utf-8")) for i in range(max(0, len(w) - 5))})
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    cf.write_text(json.dumps({"h": h, "pages": pages, "sha": sha}), encoding="utf-8")
    return set(h), pages, sha


def compare_sources(cfg, vault, a, b):
    """How the source files behind notes a and b relate -> dict(kind, text, files, share)."""
    fa, fb = source_file(vault, a), source_file(vault, b)
    out = {"files": [fa, fb], "kind": "no_source", "share": [None, None]}
    if not (fa and fb):
        out["text"] = ("no source file to compare for " + ("either note" if not (fa or fb) else "one of them"))
        return out
    if fa == fb:
        out.update(kind="same_file", text="both notes were made from the same file")
        return out
    ha, pa, sa = _src_hashes(cfg, vault / fa)
    hb, pb, sb = _src_hashes(cfg, vault / fb)
    pg = lambda p: f"{p} page{'s' if p != 1 else ''}" if p else "?"   # noqa: E731
    if sa == sb:
        out.update(kind="same_file", text=f"the two source files are identical copies ({pg(pa)})")
        return out
    if not (ha and hb):
        out.update(kind="unreadable", text="a source file has no readable text, so only the summaries were compared")
        return out
    inter = len(ha & hb)
    xa, xb = inter / len(ha), inter / len(hb)
    out["share"] = [round(xa, 2), round(xb, 2)]
    na, nb = a.rsplit("/", 1)[-1][:-3], b.rsplit("/", 1)[-1][:-3]
    if min(xa, xb) >= SRC_SAME:
        out.update(kind="same_text", text=f"the two source files hold the same text ({pg(pa)} and {pg(pb)}; "
                                          f"{round(100 * min(xa, xb))}% shared), so these summarize the same work twice")
    elif max(xa, xb) >= SRC_SAME:
        small, big = (na, nb) if xa >= xb else (nb, na)
        ps, pb_ = (pa, pb) if xa >= xb else (pb, pa)
        out.update(kind="excerpt", bigger=b if xa >= xb else a,
                   text=f"the source of “{small}” ({pg(ps)}) is an excerpt of the source of “{big}” ({pg(pb_)}): "
                        f"{round(100 * max(xa, xb))}% of it is inside the larger file")
    elif max(xa, xb) < SRC_PARTS:
        out.update(kind="parts", text=f"the source files share almost no text ({round(100 * max(xa, xb))}%): different "
                                      f"parts or different pieces of the same work, not duplicates")
    else:
        out.update(kind="overlap", text=f"the source files partly overlap ({round(100 * xa)}% of the first, "
                                        f"{round(100 * xb)}% of the second): related, but each has text of its own")
    return out


def _family(vault, rel):
    """A summary note plus what belongs to it: its Analysis, Transcript and part-analysis notes (families.py)."""
    try:
        import families
        return families.family_rels(vault, rel)
    except Exception:  # noqa: BLE001  fall back to the name-based lookup
        pass
    import vault_names as VN
    name = rel.rsplit("/", 1)[-1][:-3]
    parent = (vault / rel).parent
    out = [rel]
    for nm in VN.analysis_names(name) + VN.transcript_names(name):
        if (parent / f"{nm}.md").is_file():
            out.append((parent / f"{nm}.md").relative_to(vault).as_posix())
    d = parent / VN.base(name)
    if d.is_dir():
        out += sorted(x.relative_to(vault).as_posix() for x in d.rglob("*.md"))
    return out


def _file_used_elsewhere(vault, file_rel, skip):
    fname = file_rel.rsplit("/", 1)[-1]
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if not f.endswith(".md"):
                continue
            rel = (Path(root) / f).relative_to(vault).as_posix()
            if rel in skip:
                continue
            try:
                if fname in (Path(root) / f).read_text(encoding="utf-8", errors="replace"):
                    return rel
            except OSError:
                continue
    return None


def delete_duplicate(cfg, log, keep, drop, drop_file, day):
    """Move a duplicate summary, its family and its source file into the vault's .trash (recoverable), point every
    link at the kept note's equivalents, and keep the old name as an alias of the kept note -> result line."""
    import shutil
    import vault_names as VN
    vault = Path(cfg["vault_path"])
    fam = _family(vault, drop)
    kname, dname = keep.rsplit("/", 1)[-1][:-3], drop.rsplit("/", 1)[-1][:-3]
    kparent = (vault / keep).parent
    mapping = {dname.lower(): kname}
    for olds, news in ((VN.analysis_names(dname), VN.analysis_names(kname)), (VN.transcript_names(dname), VN.transcript_names(kname))):
        target = next((n for n in news if (kparent / f"{n}.md").is_file()), kname)
        for o in olds:
            mapping[o.lower()] = target
    for r in fam[1:]:
        nm = r.rsplit("/", 1)[-1][:-3]
        mapping.setdefault(nm.lower(), next((n for n in VN.analysis_names(kname) if (kparent / f"{n}.md").is_file()), kname))
    file_note = ""
    if drop_file and (vault / drop_file).is_file():
        other = _file_used_elsewhere(vault, drop_file, set(fam) | {keep})
        if other:
            file_note = f"; its source file was kept (also used by {other.rsplit('/', 1)[-1][:-3]})"
            drop_file = None
    trash = vault / ".trash" / f"{day} deleted duplicates"
    moved = []
    for r in fam + ([drop_file] if drop_file else []):
        src = vault / r
        if not src.exists():
            continue
        dst = trash / r
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append(r)
    for d in {(vault / drop).parent / VN.base(dname), (vault / drop).parent}:   # an emptied work folder goes too
        try:
            left = list(d.iterdir()) if d.is_dir() else None
            if left is not None and d.name == VN.work_of(dname) and all(x.name == "index.md" for x in left):
                for x in left:
                    x.unlink()
                d.rmdir()
        except OSError:
            pass
    changed = 0
    for _rel, p in VN._walk(vault):
        try:
            t = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        n = VN.rewrite_links(t, mapping)
        if p == vault / keep:
            n = VN._add_alias(n, dname)
        if n != t:
            p.write_text(n, encoding="utf-8")
            changed += 1
    try:
        lp = vault / "log.md"
        lp.write_text(lp.read_text(encoding="utf-8").rstrip() + f"\n\n## {C.now(cfg).date().isoformat()}\n\n"
                      f"**Duplicate deleted (your answer, {day})**\n\n"
                      + "".join(f"- Moved `{r}` to `.trash/{day} deleted duplicates/`\n" for r in moved)
                      + f"- Links to “{dname}” now point to “{kname}” ({changed} file(s))\n", encoding="utf-8")
    except OSError:
        pass
    log(f"duplicate deleted: {drop} (+{len(moved) - 1} file(s)); kept {keep}")
    extra = len(moved) - 1 - (1 if drop_file else 0)
    return (f"{dname}: deleted" + (" with its source file" if drop_file else "") + (f" and {extra} analysis/transcript note(s)" if extra > 0 else "")
            + f" (in the vault's .trash for now); kept {kname}{file_note}")


def dup_questions(cfg, log, first_id):
    """Questions for archived duplicates and weak duplicate pairs, numbered from first_id."""
    vault = Path(cfg["vault_path"])
    work = Path(cfg["local_root"]) / "work"
    work.mkdir(parents=True, exist_ok=True)
    out, n = [], first_id
    # 1. archived duplicates: restore / merge / delete
    lj = work / "organize-archived.json"
    if lj.exists():
        lj.unlink()
    rc, txt = C.run(log, C.VL / "resolve_duplicates.py", vault, "--list", "--json", lj)
    archived = json.loads(lj.read_text(encoding="utf-8")) if not rc and lj.exists() else []
    for d in archived[:DUP_MAX]:
        path, keeper, uniq = d["path"], d.get("keeper"), int(d.get("unique_paragraphs") or 0)
        kname = keeper.rsplit("/", 1)[-1][:-3] if keeper else None
        restore = {"label": f"Restore it (not a duplicate) (back to {d.get('archived_from') or 'its folder'})", "action": "restore"}
        delete = {"label": "Delete the archived copy (nothing is lost)" if (keeper and uniq == 0) else
                  "Delete the archived copy anyway", "action": "delete"}
        merge = {"label": f"Merge its {uniq} extra paragraph(s) into the kept note, then delete it", "action": "merge"}
        stub = _fm_flag(vault, path, "superseded_stub") or _words(vault, path) < STUB_WORDS
        total = _paragraphs(vault, path)
        if keeper and not stub and uniq >= 20 and uniq >= 0.5 * total:
            # most of it isn't in the kept note: probably a different work archived by mistake (2026-10-01:
            # papers sharing their series' web page were archived as copies), so restoring is suggested
            opts = {"a": restore, "b": merge, "c": delete}
            why = (f"a copy of “{kname}”, but {uniq} of its {total} paragraphs aren't in that note, "
                   f"so it's probably a different work")
        elif not keeper:
            opts, why = {"a": restore, "b": delete}, "a duplicate of a note that can't be found any more"
        elif uniq == 0 or stub:
            opts = {"a": delete, "b": restore} if uniq == 0 else {"a": delete, "b": merge, "c": restore}
            why = (f"a short stub of “{kname}”" if stub and uniq else f"a copy of “{kname}”")
        else:
            opts, why = {"a": merge, "b": restore, "c": delete}, f"a copy of “{kname}” with {uniq} paragraph(s) of its own"
        out.append({"id": n, "kind": "archived", "path": path, "keeper": keeper, "unique": uniq,
                    "text": f"Archived as {why}", "options": opts})
        n += 1
    # 2. weak pairs (strong ones were archived overnight): keep one, or not duplicates
    dj = work / "organize-dups.json"
    if dj.exists():
        dj.unlink()
    rc, txt = C.run(log, C.VL / "find_duplicates.py", vault, "--json", dj)
    pairs = json.loads(dj.read_text(encoding="utf-8")) if not rc and dj.exists() else []
    try:   # pairs that say the same thing in other words (search index; vault search plan, phase 4, 2026-10-12)
        import vault_search
        seen = {frozenset(p["notes"]) for p in pairs}
        for p in vault_search.similar_pairs(cfg, seen, limit=5):
            if all(_fm_flag(vault, x, "not_a_duplicate") for x in p["notes"]):
                continue      # The owner said "not duplicates" before
            pairs.append(dict(p, strong=False))
    except Exception as e:  # noqa: BLE001  never lose the morning questions over this
        log(f"meaning-based duplicate check failed: {type(e).__name__}: {e}")
    for p in pairs:
        if len(out) >= DUP_MAX:
            break
        if p.get("strong"):
            continue
        a, b = p["notes"]
        if _part_of_other(vault, a, b):   # an Analysis and its own Part analysis are not copies (2026-10-12)
            continue
        try:
            cmp_ = compare_sources(cfg, vault, a, b)
        except Exception as e:  # noqa: BLE001  never lose the question over the comparison
            log(f"source comparison failed for {a} / {b}: {type(e).__name__}: {e}")
            cmp_ = {"kind": "no_source", "files": [None, None], "text": "the source files couldn't be compared"}
        if cmp_["kind"] == "parts":
            log(f"not asked (different parts, no shared source text): {a} / {b}")
            continue
        wa, wb = _words(vault, a), _words(vault, b)
        big, small = (a, b) if wa >= wb else (b, a)
        if cmp_["kind"] == "excerpt":
            big = cmp_["bigger"]
            small = b if big == a else a
        elif cmp_["kind"] in ("same_file", "same_text"):
            # keep the one already filed (not Inbox/Clippings), then the longer summary
            def rank(x):   # ...and the one with an Analysis (its analysis isn't thrown away; 2026-10-13)
                return (0 if x.split("/", 1)[0] in ("Inbox", "Clippings") else 1, len(_family(vault, x)), _words(vault, x))
            big, small = (a, b) if rank(a) >= rank(b) else (b, a)
        bn, sn = big.rsplit("/", 1)[-1][:-3], small.rsplit("/", 1)[-1][:-3]
        if bn == sn:   # same name in two folders (2026-10-15: the options couldn't be told apart)
            bn, sn = f"{bn} (in {_where(big)})", f"{sn} (in {_where(small)})"
        notdup = {"label": "Not duplicates (keep both; never ask again)", "action": "notdup"}
        why = "These two look alike (" + "; ".join(p.get("why", [])) + ")"
        files = dict(zip((a, b), cmp_.get("files") or [None, None]))
        if cmp_["kind"] == "no_source":
            keep_big = {"label": f"Keep “{bn}”, archive “{sn}”", "action": "archive", "keep": big, "drop": small}
            keep_small = {"label": f"Keep “{sn}”, archive “{bn}”", "action": "archive", "keep": small, "drop": big}
            similar = any("similar" in w for w in p.get("why", []))
            opts = {"a": keep_big, "b": keep_small, "c": notdup} if similar else {"a": notdup, "b": keep_big, "c": keep_small}
        else:
            def dele(k, d, kn, dn):
                f = files.get(d) if files.get(d) != files.get(k) else None
                fam = len(_family(vault, d)) - 1
                return {"label": f"Keep “{kn}”; delete “{dn}”" + (" and its source file" if f else "")
                                 + (f" (its {fam} analysis/transcript note(s) go too)" if fam else ""),
                        "action": "delete", "keep": k, "drop": d, "drop_file": f}
            del_small, del_big = dele(big, small, bn, sn), dele(small, big, sn, bn)
            if cmp_["kind"] in ("same_file", "same_text", "excerpt"):
                opts = {"a": del_small, "b": del_big, "c": notdup}
            else:
                opts = {"a": notdup, "b": del_small, "c": del_big}
        out.append({"id": n, "kind": "pair", "paths": [a, b], "why": p.get("why", []), "sources": cmp_.get("files"),
                    "source_kind": cmp_["kind"], "text": f"{why}. Source files: {cmp_['text']}." + _origin(vault, a, b),
                    "options": opts})
        n += 1
    return out


def _where(p):
    return p.rsplit("/", 1)[0] if "/" in p else "the vault root"


def _origin(vault, a, b):
    """Where the two notes came from (2026-10-13: tell real double intake from a copy made by the
    summary/analysis process): the file each was made from, and whether either has an analysis."""
    bits = []
    for x in (a, b):
        fm, _ = _split_note(vault, x)
        sf = str((fm or {}).get("source_file") or "").strip()
        fam = len(_family(vault, x)) - 1
        nm = x.rsplit("/", 1)[-1][:-3]
        if a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]:
            nm += f" (in {_where(x)})"
        bits.append(f"“{nm}” was made from {('the file ' + sf) if sf else 'a web page or note (no file)'}"
                    + (f" and has {fam} analysis/transcript note(s)" if fam else ""))
    sa = str((_split_note(vault, a)[0] or {}).get("source_file") or "")
    sb = str((_split_note(vault, b)[0] or {}).get("source_file") or "")
    how = (" Two different files went into the dropbox, so this is a double intake, not a copy made by the "
           "summary process." if sa and sb and sa != sb else "")
    return " " + "; ".join(bits) + "." + how


def _part_of_other(vault, a, b):
    fa, _ = _split_note(vault, a)
    fb, _ = _split_note(vault, b)
    na, nb = a.rsplit("/", 1)[-1][:-3], b.rsplit("/", 1)[-1][:-3]
    return (str(fa.get("part_of") or "").strip("[]") == nb or str(fb.get("part_of") or "").strip("[]") == na)


def apply_dups(cfg, log, prop, ans):
    """Carry out the owner's answers to the duplicate questions -> list of plain result lines."""
    vault = Path(cfg["vault_path"])
    work = Path(cfg["local_root"]) / "work"
    day = prop["day"]
    decisions, plan, lines = [], [], []
    for q in prop.get("dups", []):
        opt = q["options"].get(ans.get(q["id"]) or "")
        if not opt:
            continue
        act = opt["action"]
        if q["kind"] == "archived":
            name = q["path"].rsplit("/", 1)[-1][:-3]
            if not (vault / q["path"]).exists():
                lines.append(f"{name}: already resolved elsewhere, nothing done")
                continue
            decisions.append({"path": q["path"], "action": act})
            lines.append(f"{name}: {'restored' if act == 'restore' else 'merged into the kept note' if act == 'merge' else 'deleted'}")
        elif act == "notdup":
            for p in q["paths"]:
                if (vault / p).exists():
                    plan.append({"path": p, "set": {"not_a_duplicate": True}})
            lines.append(" / ".join(p.rsplit("/", 1)[-1][:-3] for p in q["paths"]) + ": marked not duplicates")
        elif act == "delete":   # The owner (2026-10-12): the duplicate summary and its source file
            keep, drop = opt["keep"], opt["drop"]
            if not ((vault / keep).exists() and (vault / drop).exists()):
                lines.append(f"{drop.rsplit('/', 1)[-1][:-3]}: skipped (a note was moved or removed since)")
                continue
            try:
                lines.append(delete_duplicate(cfg, log, keep, drop, opt.get("drop_file"), day))
            except Exception as e:  # noqa: BLE001
                lines.append(f"{drop.rsplit('/', 1)[-1][:-3]}: couldn't be deleted ({type(e).__name__}: {e})")
        elif act == "archive":
            keep, drop = opt["keep"], opt["drop"]
            if not ((vault / keep).exists() and (vault / drop).exists()):
                lines.append(f"{drop.rsplit('/', 1)[-1][:-3]}: skipped (a note was moved or removed since)")
                continue
            plan.append({"path": drop, "move_to": "Archives",
                         "set": {"status": "deprecated", "duplicate_of": f"[[{keep[:-3]}]]",
                                 "archived_from": drop.rsplit("/", 1)[0] if "/" in drop else ""}})
            lines.append(f"{drop.rsplit('/', 1)[-1][:-3]}: archived (kept {keep.rsplit('/', 1)[-1][:-3]})")
    if decisions:
        dj = work / f"organize-dup-decisions-{day}.json"
        dj.write_text(json.dumps(decisions, ensure_ascii=False, indent=1), encoding="utf-8")
        rc, out = C.run(log, C.VL / "resolve_duplicates.py", vault, "--apply", dj)
        if rc:
            lines.append("resolve_duplicates reported a problem: " + (out.strip().splitlines() or ["?"])[-1])
    if plan:
        pj = work / f"organize-dup-plan-{day}.json"
        pj.write_text(json.dumps({"actor": "claude/organize-by-email", "log": f"Duplicate answers by email ({day})",
                                  "items": plan}, ensure_ascii=False, indent=1), encoding="utf-8")
        rc, out = C.run(log, C.VL / "apply_plan.py", vault, pj)
        if rc:
            lines.append("apply_plan reported a problem: " + (out.strip().splitlines() or ["?"])[-1])
    return lines


# ------------------------------------------------------------------ intake-queue questions (2026-09-28)
# Items the intake queue holds for the owner's decision are asked here, answered like every other question:
#   duplicate    - a book/summary that may already be in the vault: a) don't add (remove it)  b) add anyway  c) not now
#   costly       - a book/summary Claude found on its own:           a) go ahead  b) not now  c) don't add (remove it)
#   confidential - looks like firm/client material:                  a) keep it out  b) it's fine, file it
QUEUE_MAX = 15


def queue_questions(cfg, log, first_id):
    import vqueue as Q
    store = Q.Store(cfg)
    out, i = [], first_id
    for it in sorted(store.all(), key=lambda x: x.get("added") or ""):
        kind = (it.get("work") or {}).get("hold_kind")
        if it.get("status") != "held" or kind not in ("duplicate", "costly", "confidential", "title"):
            continue
        ref = it.get("source_ref") or {}
        in_drop = it.get("source") == "folder" and not it.get("parent")
        where = (f"dropbox: {ref.get('path')}" if in_drop else
                 f"attachment in Remember email" if it.get("parent") else f"Outlook {ref.get('folder') or 'Remember'}")
        src = "the dropbox" if in_drop else "Remember"
        f = it.get("file") or {}
        size = ", ".join(x for x in (f"{f['pages']} pages" if f.get("pages") else "",
                                     f"{f['words']:,} words" if f.get("words") else "") if x)
        q = {"id": i, "kind": kind, "item": it["id"], "title": it.get("title") or "?", "where": where,
             "size": size, "why": it.get("why") or "", "my_note": it.get("my_note") or ""}
        if kind == "duplicate":
            best = ((it.get("work") or {}).get("dup_of") or [{}])[0]
            name = str(best.get("note") or "?").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            q.update(match=best.get("note"), text=f"May already be in the vault as \"{name}\" ({best.get('why', '')})",
                     options={"a": {"label": f"Don't add it: it's already there; remove it from {src}", "action": "dup"},
                              "b": {"label": "Add it anyway (spends Claude usage on the summary)", "action": "go"},
                              "c": {"label": "Not now (ask again tomorrow)", "action": None}})
        elif kind == "title":   # title check (2026-10-06)
            sug = it.get("title_suggest") or {}
            nm = sug.get("name") or sug.get("title")
            cur = (it.get("source_ref") or {}).get("name") or it.get("title") or "?"
            q.update(title=cur, text=(f"The name looks like a file name. Suggested: \u201c{nm}\u201d"
                                      + (f" ({', '.join(sug.get('author') or [])}{', ' + str(sug.get('published')) if sug.get('published') else ''})"
                                         if sug.get("author") else "")) if nm else "The name looks like a file name, and no title was found in it.",
                     suggest=sug,
                     # his own title, typed on the answer page (2026-11-03); blank = asked again tomorrow
                     options=dict(zip("abcd", ([{"label": f"Use \u201c{nm}\u201d", "action": "title_use"}] if nm else [])
                                       + [{"label": "Use my own title (type it on the answer page)", "action": "title_own"},
                                          {"label": "Keep the current name", "action": "title_keep"},
                                          {"label": "Not now (edit it on the queue page: Open & edit)", "action": None}])))
        elif kind == "costly" and Q.route_of(it) in ("analyze", "analyze_long"):
            q.update(where=f"vault: {ref.get('path')}", text=f"Analyze it? ({it.get('why') or 'costly'})",
                     options={"a": {"label": "Go ahead (concept analysis in the next run)", "action": "go"},
                              "b": {"label": "Not now (ask again tomorrow)", "action": None},
                              "c": {"label": "Don't analyze it", "action": "drop"}})
        elif kind == "costly":
            what = "book" if Q.route_of(it) == "book" else "long document"
            q.update(text=f"Claude thinks this is a {what}: summarize it?" ,
                     options={"a": {"label": "Go ahead (detailed summary in an overnight run)", "action": "go"},
                              "b": {"label": "Not now (ask again tomorrow)", "action": None},
                              "c": {"label": f"Don't add it; remove it from {src}", "action": "drop"}})
        else:
            q.update(text="Looks like firm or client material, so nothing was sent to Claude",
                     options={"a": {"label": "Keep it out of the vault (leave it where it is)", "action": "keep_out"},
                              "b": {"label": "It's fine: file it", "action": "go"}})
        out.append(q)
        i += 1
        if len(out) >= QUEUE_MAX:
            break
    try:   # new concepts proposed by analyses (concept analysis plan, phase A)
        import analyze
        out += analyze.concept_questions(cfg, i, max(0, QUEUE_MAX - len(out)))
    except Exception as e:  # noqa: BLE001
        log(f"concept questions skipped: {type(e).__name__}: {e}")
    try:   # one-item concepts Claude reviewed overnight: keep or drop (2026-11-23); their own quota, not QUEUE_MAX
        import concept_review
        out += concept_review.questions(cfg, max([q["id"] for q in out] + [first_id - 1]) + 1)
    except Exception as e:  # noqa: BLE001
        log(f"concept review questions skipped: {type(e).__name__}: {e}")
    return out


# ------------------------------------------------------------------ items left in Remember (2026-10-01)
# The owner: deal with "couldn't be saved automatically" items on the web page. Every held item gets a card with
# an Open link and a box for his note; dropbox files and links also get lettered choices:
#   not a document (empty file, leftovers like .ini2/.ptn): a) delete it from the dropbox  b) keep it, stop asking
#   dead link (HTTP 404/410): a) save the Internet Archive copy if there is one, else delete; b/c the others
#   anything else in the dropbox: a) leave it for now  b) keep it, stop asking  c) delete it
# Links waiting for the laptop's signed-in browser get the note box only. Formats handled since are retried by
# queue_run.py on their own, so they don't get here.
JUNK_EXT = {".ini", ".ini2", ".ptn", ".ptn2", ".tmp", ".temp", ".lnk", ".db", ".ds_store", ".dat", ".bak", ".log",
            ".lock", ".crdownload", ".part", ".partial", ".download", ".dll", ".exe", ".sys", ".cache"}
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
HELD_MAX = 25


def wayback(url):
    """(snapshot URL, 'YYYY-MM-DD') of the Internet Archive's closest copy of a dead page, or (None, None)."""
    try:
        import requests
        r = requests.get("https://archive.org/wayback/available", params={"url": url}, timeout=20)
        c = ((r.json() or {}).get("archived_snapshots") or {}).get("closest") or {}
        if c.get("available") and str(c.get("status", "200")) == "200" and c.get("url"):
            ts = str(c.get("timestamp") or "")
            return c["url"].replace("http://", "https://", 1), (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else None)
        # the availability API often answers "nothing" for pages it has: ask the capture index too
        r = requests.get("https://web.archive.org/cdx/search/cdx", timeout=30,
                         params={"url": url, "output": "json", "filter": "statuscode:200", "limit": "-1"})
        rows = r.json() if r.ok and r.text.strip() else []
        if len(rows) > 1:
            ts, orig = rows[-1][1], rows[-1][2]
            return f"https://web.archive.org/web/{ts}/{orig}", f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    except Exception:  # noqa: BLE001  no network, odd answer: just no archive option
        pass
    return None, None


def held_questions(cfg, log, first_id, skip_items=()):
    """Cards for held items (queue\\held.json) not already asked about as intake-queue questions."""
    import held_bridge as HB
    import vqueue as Q
    store = Q.Store(cfg)
    delete = {"label": "Delete it from the dropbox", "action": "delete"}
    keep = {"label": "Keep it in the dropbox; stop asking", "action": "keep"}
    later = {"label": "Leave it for now (ask again tomorrow)", "action": None}
    retry = {"label": "Try again tonight", "action": "retry"}
    stop = {"label": "Stop trying (drop it from the queue)", "action": "cancel"}
    stop_mail = {"label": "Stop trying (drop it; its email goes to Deleted Items)", "action": "cancel"}
    out, n = [], first_id
    try:
        healed = heal_held(cfg, log, store)
    except Exception as e:  # noqa: BLE001  best effort; the cards below still show
        healed = set()
        log(f"heal_held failed: {type(e).__name__}: {e}")
    for key, h in sorted(HB.load().items(), key=lambda kv: kv[1].get("at") or ""):
        if h.get("queue_id") and h["queue_id"] in healed:
            continue
        if h.get("status") != "held" or h.get("needs") == "confirm" or (h.get("queue_id") and h["queue_id"] in skip_items):
            continue
        if len(out) >= HELD_MAX:
            break
        it = store.get(h["queue_id"]) if h.get("queue_id") else None
        title = re.sub(r"\s*\(file in the dropbox\)$", "", h.get("subject") or "")
        if not title or title == "(Web Clipper stub)":
            title = h.get("title") or (h.get("url") or "?")
        q = {"id": n, "kind": "held", "key": key, "item": h.get("queue_id"), "title": title, "why": h.get("why") or "",
             "url": h.get("url"), "note": (it or {}).get("doug_note") or h.get("doug_note") or "", "options": {},
             "my_note": (it or {}).get("my_note") or ""}
        in_drop = bool(it) and it.get("source") == "folder" and not it.get("parent")
        if in_drop:
            q["where"] = f"dropbox: {(it.get('source_ref') or {}).get('path')}"
        if it and (it.get("work") or {}).get("hold_kind") == "notext" and in_drop:
            # The owner (2026-10-01): a scan with no readable text: a link to it, and keep or delete
            q["sub"], q["text"] = "notext", f"No readable text, even after OCR ({it.get('why') or ''})."
            q["options"] = {"a": keep, "b": delete}
        elif h.get("needs") == "browser":
            # The owner (2026-10-12): clear these while answering on the laptop - open the link in his own signed-in
            # browser and clip it; reclip.check (every intake run) matches the clip by address and clears the card.
            q["sub"], q["text"] = "browser", BROWSER_TODO
            if it:
                # The owner (2026-10-15): it wasn't clear what to pick after clipping - a) covers both
                q["options"] = {"a": {"label": "I've clipped it, or will: leave the card; it clears by itself once "
                                               "your clip is found (or run “process the vault queue”)", "action": None},
                                "b": stop_mail if it.get("source") == "mail" and not it.get("parent") else stop}
        elif in_drop:
            f = it.get("file") or {}
            name, ext = (f.get("name") or "").lower(), (f.get("ext") or "").lower()
            if q["url"] and re.match(r"HTTP (404|410)\b", q["why"]):
                snap, when = wayback(q["url"])
                q["sub"] = "dead"
                if snap:
                    q.update(archive=snap, text=f"The page is gone ({q['why']}). The Internet Archive has a copy"
                             + (f" from {when}" if when else "") + ".")
                    q["options"] = {"a": {"label": "Save the Internet Archive copy", "action": "archive"}, "b": delete, "c": keep}
                else:
                    q["text"] = f"The page is gone ({q['why']}) and the Internet Archive has no copy."
                    q["options"] = {"a": delete, "b": keep}
            elif f.get("bytes") == 0 or ext in JUNK_EXT or name in JUNK_NAMES or name.startswith("~$"):
                q["sub"] = "junk"
                q["text"] = ("An empty file (0 bytes)." if f.get("bytes") == 0 else
                             f"Not a document: a {ext or 'system'} file, probably left behind by a program.")
                q["options"] = {"a": delete, "b": keep}
            else:
                q["sub"], q["text"] = "other", f"Couldn't be saved: {q['why']}."
                q["options"] = {"a": later, "b": keep, "c": delete}
        else:
            q["sub"], q["text"] = "other", "Couldn't be saved: " + plain_why(q["why"], it)
            if it:   # The owner (2026-11-19): say what was being worked on and where it came from
                q["text"] += item_origin(cfg, it, q)
            if it and re.search(r"edited by hand|analysis is verified", q["why"]):
                # 2026-10-19: "try again" retried without --force, so it failed the same way every night
                q["sub"] = "kept_analysis"
                q["text"] = ("A new analysis was asked for (e.g. your notes changed), but the current one "
                             + ("is marked verified" if "verified" in q["why"] else "looks edited by hand")
                             + ", so it wasn't replaced. If you didn't edit it, replace it; the current one is "
                               "backed up first (.analysis-backups).")
                q["options"] = {"a": {"label": "Keep the current analysis (drop this redo)", "action": "keep_analysis"},
                                "b": {"label": "Replace it with a new analysis tonight", "action": "force"}}
            elif it:   # e.g. an analysis job (2026-10-12: these had no choices, so they came back every day)
                q["options"] = {"a": retry, "b": stop_mail if it.get("source") == "mail" and not it.get("parent") else stop}
        out.append(q)
        n += 1
    return out


BROWSER_TODO = ("The site needs you signed in. On the laptop: Open the link (your own browser is signed in) and save it "
                "with the Obsidian Web Clipper. The next intake run (within 30 minutes) finds your clip by its address, "
                "clears this card and the Remember email, and copies your note here into the clip. Or say "
                "“process the vault queue” in a Claude session on the laptop.")
LONG_PATH = 240   # Windows paths: <vault>\\... longer than this fail (MAX_PATH 260)


def plain_why(why, it=None):
    """What 'couldn't be saved' means, in plain words (2026-10-12: the raw errors told him nothing)."""
    why = str(why or "")
    m = re.search(r"No such file or directory: (['\"])(.+)\1", why)
    if m and len(m.group(2).replace("\\\\", "\\")) >= LONG_PATH:
        return ("a software problem, not the item: the name chosen for one of its analysis notes made the file path "
                "longer than Windows allows. Fixed 2026-10-12; it is retried automatically.")
    m = re.match(r"note not found: (.+)$", why)
    if m:
        return (f"the note it was queued to analyze ({m.group(1)}) is no longer in the vault, so there is nothing to "
                "analyze. Such jobs are now dropped automatically.")
    if "no text to summarize" in why:
        return "the file has no text layer that could be read (a scan whose OCR gave nothing). Open it and decide."
    if "answer was not the JSON" in why:
        return "Claude's answer came back malformed several times (a temporary glitch). Try again or stop."
    m = re.search(r"no JSON from claude \(exit (\d+)\)", why)
    if m and (int(m.group(1)) > 255 or "Bun has crashed" in why or "bun.report" in why or int(m.group(1)) == 3):
        # 2026-11-19: a crash of the Claude program itself (Bun crash report, or a Windows crash code such as
        # 3221226505 = 0xC0000409); the raw text was cut mid-message and its bun.report link looked like the item's
        code = int(m.group(1))
        how = f"Windows crash code 0x{code:08X}" if code > 255 else "a Bun runtime crash"
        return (f"the Claude program crashed while working on it ({how}). That is a problem with the tool, not "
                "the item, and it usually clears on a retry.")
    return why.rstrip(".") + "."


def item_origin(cfg, it, q):
    """' What it was: ...' for a held card, and set q['where'] / q['url'] so the card links the note and the
    source (2026-11-19: failed analysis cards gave no way to tell what the item was). Never raises."""
    try:
        ref = it.get("source_ref") or {}
        route = it.get("route") or ""
        if it.get("source") == "vault" and ref.get("path"):
            import vault_names as VN
            from my_note import read_fm
            vault = Path(cfg["vault_path"])
            rel = VN.follow(ref["path"], cfg) or ref["path"]
            if not (vault / rel).is_file() and rel.endswith(".md"):   # named "<work> (Summary)" after filing
                alt = rel[:-3] + " (Summary).md"
                rel = alt if (vault / alt).is_file() else rel
            fm = read_fm(vault / rel) if (vault / rel).is_file() else {}
            q["where"] = f"vault: {rel}"
            src = str(fm.get("resource") or fm.get("url") or "")
            if src.startswith("http") and not q.get("url"):
                q["url"] = src
            who = fm.get("author")
            who = ", ".join(map(str, who)) if isinstance(who, list) else str(who or "")
            pub = str(fm.get("publication") or "")
            by = (fm.get("generated") or {}).get("by") if isinstance(fm.get("generated"), dict) else ""
            what = "the analysis" if route.startswith("analyze") else f"the {route or 'work'} step"
            bits = [x for x in (str(fm.get("type") or ""), " by ".join(x for x in (pub, who) if x)) if x]
            s = f" What failed: {what} of the vault note below" + (f" ({'; '.join(bits)})" if bits else "") + "."
            if not fm:
                s += " That note is no longer at this path."
            if by or fm.get("accessed"):
                s += " Saved" + (f" {str(fm.get('accessed'))[:10]}" if fm.get("accessed") else "") + (f" by {by}" if by else "") + "."
            return s
        if it.get("source") == "mail":
            q.setdefault("where", f"Outlook {ref.get('folder') or 'Remember'} email" + (" (attachment)" if it.get("parent") else ""))
        elif it.get("source") == "folder":
            q.setdefault("where", f"dropbox: {ref.get('path')}")
        if it.get("url") and not q.get("url"):
            q["url"] = it["url"]
        return f" What failed: the {route or 'work'} step." if route else ""
    except Exception:  # noqa: BLE001  the card still shows without it
        return ""


def heal_held(cfg, log, store):
    """Fix held queue items that don't need the owner (2026-10-12) -> set of item ids taken off today's cards.
    - analysis of a note that has gone: follow its rename (vault_names) and retry, else cancel it
    - failed only because an analysis part note's path was too long (fixed in analyze.part_path): retry once"""
    import vqueue as Q
    import vault_names as VN
    vault = Path(cfg["vault_path"])
    done = set()
    for it in store.all():
        if it.get("status") != "held" or it.get("route") not in ("analyze", "analyze_long"):
            continue
        why = str(it.get("why") or "")
        w = it.setdefault("work", {})
        ref = it.get("source_ref") or {}
        if why.startswith("note not found"):
            now_rel = VN.follow(ref.get("path")) or ref.get("path")
            if now_rel and (vault / now_rel).is_file():
                ref["path"] = now_rel
                it["attempts"] = 0
                Q.set_status(it, "ready", f"retrying: the note is now {now_rel}", cfg=cfg)
            else:
                Q.set_status(it, "cancelled", "the note to analyze is no longer in the vault (removed or deleted)", cfg=cfg)
        elif "No such file or directory" in why and not w.get("long_path_retry"):
            m = re.search(r"No such file or directory: (['\"])(.+)\1", why)   # the path may contain ' (wasn't)
            if not (m and len(m.group(2).replace("\\\\", "\\")) >= LONG_PATH):
                continue
            w["long_path_retry"] = C.now(cfg).isoformat(timespec="seconds")
            it["attempts"] = 0
            Q.set_status(it, "ready", "retrying: part-note names are now kept short enough for Windows", cfg=cfg)
        else:
            continue
        store.save(it)
        done.add(it["id"])
        log(f"heal_held: {it.get('title')}: {it['status']}")
    return done


def apply_held(cfg, log, prop, ans, notes=None):
    """Carry out the owner's answers and notes for the held-item cards -> list of plain result lines."""
    import held_bridge as HB
    import vqueue as Q
    store = Q.Store(cfg)
    root = Path((Q.qcfg(cfg).get("dropbox_path") or "").strip() or "\0")
    lines, notes = [], notes or {}
    for q in prop.get("held", []):
        it = store.get(q["item"]) if q.get("item") else None
        note = (notes.get(q["id"]) or notes.get(str(q["id"])) or "").strip()
        if note and note != (q.get("note") or "").strip():
            if it:
                it["doug_note"] = note
                store.save(it)
            else:   # a link that isn't a queue item (phone share, clipper stub): its held.json entry
                fresh = HB.load()
                if q["key"] in fresh:
                    fresh[q["key"]]["doug_note"] = note
                    Q.atomic_write(HB.held_path(), json.dumps(fresh, ensure_ascii=False, indent=1))
            lines.append(f"{q['title']}: your note saved")
        opt = q["options"].get(ans.get(q["id"]) or "")
        if not opt or not opt.get("action"):
            continue
        if not it or it.get("status") != "held":
            lines.append(f"{q['title']}: already handled")
            continue
        act = opt["action"]
        if act == "delete":
            src = root / ((it.get("source_ref") or {}).get("path") or "\0")
            try:
                if root.is_absolute() and src.is_file() and root.resolve() in src.resolve().parents:
                    side = src.with_suffix(".txt")
                    src.unlink()
                    if side.exists() and side != src:
                        side.unlink()
                Q.set_status(it, "cancelled", "deleted from the dropbox (daily email)", cfg=cfg)
                lines.append(f"{q['title']}: deleted from the dropbox")
            except OSError as e:
                lines.append(f"{q['title']}: couldn't be deleted ({e})")
        elif act == "retry":
            it["attempts"] = 0
            it.setdefault("manual", {})["released"] = True
            Q.set_status(it, "ready", "try again (daily email)", cfg=cfg)
            lines.append(f"{q['title']}: will be tried again")
        elif act == "force":   # 2026-10-19: replace a kept (hand-edited / verified) analysis
            it["attempts"] = 0
            it.setdefault("manual", {})["released"] = True
            it.setdefault("source_ref", {})["force"] = True
            Q.set_status(it, "ready", "replace the analysis anyway (daily email)", cfg=cfg)
            lines.append(f"{q['title']}: the analysis will be replaced tonight (the current one is backed up)")
        elif act == "keep_analysis":
            Q.set_status(it, "cancelled", "kept the current analysis (daily email)", cfg=cfg)
            lines.append(f"{q['title']}: current analysis kept; redo dropped")
        elif act == "cancel":
            Q.set_status(it, "cancelled", "dropped (daily email)", cfg=cfg)
            if it.get("source") == "mail" and not it.get("parent"):
                # 2026-11-12: otherwise the email stayed in Remember for good, never asked about again
                it.setdefault("result", {})["remove_source"] = True
                lines.append(f"{q['title']}: dropped from the queue; its email goes to Deleted Items")
            else:
                lines.append(f"{q['title']}: dropped from the queue")
        elif act == "keep":
            Q.set_status(it, "skipped", "kept in the dropbox; not asked about again (daily email)", cfg=cfg)
            lines.append(f"{q['title']}: kept in the dropbox; won't be asked about again")
        elif act == "archive" and q.get("archive"):
            it["url"], it["route"], it["kind"] = q["archive"], "link", "link"
            it.setdefault("manual", {})["released"] = True
            Q.set_status(it, "ready", "saving the Internet Archive copy (daily email)", cfg=cfg)
            lines.append(f"{q['title']}: the Internet Archive copy will be saved in the next intake run")
        store.save(it)
    return lines


def apply_queue(cfg, log, prop, ans):
    """Carry out the owner's answers to the queue questions -> list of plain result lines."""
    import vqueue as Q
    store = Q.Store(cfg)
    lines = []
    concepts_done = False
    for q in prop.get("queue", []):
        opt = q["options"].get(ans.get(q["id"]) or "")
        if not opt or not opt.get("action"):
            continue
        if q.get("kind") in ("concept", "concept_dup", "concept_review"):
            import analyze
            line = analyze.apply_concept(cfg, log, q, opt["action"])
            if line:
                lines.append(line)
                concepts_done = True
            continue
        it = store.get(q["item"])
        if not it or it.get("status") != "held":
            lines.append(f"{q['title']}: already handled")
            continue
        m, act = it.setdefault("manual", {}), opt["action"]
        if act == "title_own":
            import titlecheck
            own = re.sub(r"\s+", " ", str((prop.get("own_titles") or {}).get(str(q["id"])) or "")).strip()
            if not own:
                lines.append(f"{q['title']}: no title was typed; it will be asked again")
                continue
            if own == re.sub(r"\s+", " ", q.get("title") or "").strip():   # the box's current name, unchanged
                lines.append(titlecheck.apply(cfg, log, store, it, {}, keep=True))
                continue
            name = re.sub(r'[\\/:*?"<>|]+', " -", own)   # a note name can't hold these; the title keeps them
            lines.append(titlecheck.apply(cfg, log, store, it, {"title": own, "name": re.sub(r"\s+", " ", name).strip(" -.")}))
            continue
        if act in ("title_use", "title_keep"):
            import titlecheck
            sug = q.get("suggest") or it.get("title_suggest") or {}
            lines.append(titlecheck.apply(cfg, log, store, it, {"title": sug.get("title"), "name": sug.get("name"),
                                                               "author": sug.get("author"), "published": sug.get("published"),
                                                               "publisher": sug.get("publisher")}, keep=act == "title_keep"))
            continue
        if act == "go":
            m["released"] = m["dup_ok"] = True
            m["hold"] = False
            nxt = "ready" if it.get("kind") in ("email", "link", "note") or (it.get("work") or {}).get("extracted") else "new"
            Q.set_status(it, nxt, "approved in the daily email", cfg=cfg)
            lines.append(f"{q['title']}: approved")
        elif act == "dup":
            it["result"]["notes"] = [q["match"]] if q.get("match") else []
            Q.set_status(it, "duplicate", f"already in the vault ({q.get('match')}); not added (daily email)", cfg=cfg)
            lines.append(f"{q['title']}: not added (already in the vault); it will be removed from its folder")
        elif act == "drop":
            if Q.route_of(it) == "analyze":
                Q.set_status(it, "skipped", "not to be analyzed (daily email)", cfg=cfg)
                lines.append(f"{q['title']}: won't be analyzed")
            else:
                it["result"]["remove_source"] = True
                Q.set_status(it, "skipped", "not wanted (daily email)", cfg=cfg)
                lines.append(f"{q['title']}: not added; it will be removed from its folder")
        elif act == "keep_out":
            Q.set_status(it, "skipped", "kept out of the vault (daily email)", cfg=cfg)
            lines.append(f"{q['title']}: kept out of the vault (left where it is)")
        store.save(it)
    if concepts_done:
        import analyze
        analyze.refresh_after_concepts(cfg, log)
    return lines


# Filing choices (2026-10-07): every answered filing question is logged so later proposals learn from
# where he actually puts things; declined new-folder suggestions are never asked again.
CHOICES = C.APP / "queue" / "filing-choices.jsonl"
FSTATE = C.APP / "queue" / "folder-suggestions.json"   # {"declined": [...], "asked": {folder: day}}
BAD_FOLDER_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')


def clean_folder_name(name):
    n = re.sub(r"\s+", " ", BAD_FOLDER_CHARS.sub(" ", name or "")).strip(" .")
    return n[:60]


def fstate():
    try:
        return json.loads(FSTATE.read_text(encoding="utf-8"))
    except Exception:
        return {"declined": [], "asked": {}}


def save_fstate(st):
    FSTATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def past_choices(n_over=40, n_ok=20):
    """Recent decisions for the prompt: overrides matter most, a few accepted ones show what is right."""
    try:
        rows = [json.loads(x) for x in CHOICES.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        return []
    over = [r for r in rows if r.get("chosen") != r.get("suggested")][-n_over:]
    ok = [r for r in rows if r.get("chosen") == r.get("suggested")][-n_ok:]
    keep = ("title", "type", "tags", "description", "suggested", "chosen")
    return [{k: r.get(k) for k in keep if r.get(k)} for r in over + ok]


def _name_examples():
    try:
        import corrections
        return corrections.name_examples()
    except Exception:  # noqa: BLE001
        return []


def folder_examples(vault, fset, per=6):
    out = {}
    for f in fset:
        if not f.startswith(FIXED_SUBS):
            continue
        names = sorted(p.stem for p in (vault / f).glob("*.md") if p.name != "index.md")
        if names:
            out[f] = names[:per]
    return out


def record_choices(vault, prop, plan_items, ans, picks):
    """Append one line per filed note: what was suggested (option a) and where it went."""
    items = {it["path"]: it for it in prop.get("items", [])}
    dest = {it["path"]: it.get("move_to") for it in plan_items}
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    lines = []
    for q in prop.get("questions", []):
        k = ans.get(q["id"])
        if not k:
            continue
        sug = (q["options"].get("a") or {}).get("move_to")
        for p in q["paths"]:
            if p not in dest:
                continue
            it = items.get(p, {})
            st = it.get("set") or {}
            optd = (q["options"].get(k) or {}).get("move_to")
            per = p in ((prop.get("q_picks") or {}).get(str(q["id"])) or {})
            how = ("accepted" if dest[p] == sug else "picked" if k in ("p", "n") or per
                   else "new folder" if k == "nf" or dest[p] != optd else f"option {k}")
            lines.append(json.dumps({"at": now, "day": prop.get("day"), "title": it.get("rename_to") or Path(p).stem,
                                     "type": st.get("type"), "tags": st.get("tags"), "description": st.get("description"),
                                     "suggested": sug, "chosen": dest[p], "how": how}, ensure_ascii=False))
    if lines:
        with CHOICES.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def _fm_tags(path):
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000].split("\n---", 1)[0]
        m = re.search(r"(?m)^tags:\s*\[(.*)\]\s*$", head)
        if m:
            return [t.strip().strip("\"'") for t in m.group(1).split(",") if t.strip()]
        m = re.search(r"(?ms)^tags:\s*\n((?:\s*-.*\n?)+)", head)
        return [t.strip().lstrip("-").strip().strip("\"'") for t in m.group(1).splitlines()] if m else []
    except OSError:
        return []


def later_folder_questions(vault):
    """Folder questions the owner put off (whole question, or single notes after creating the folder) -> asked again
    every morning until answered (2026-10-12). Notes that have moved or gone are dropped."""
    st = fstate()
    out = []
    for folder, d in (st.get("later") or {}).items():
        if folder in st.get("declined", []):
            continue
        paths = [p for p in d.get("paths", []) if (vault / p).is_file() and not p.startswith(folder + "/")]
        if paths:
            out.append({"folder": folder, "covers": d.get("covers") or f"{len(paths)} note(s) you left for later",
                        "paths": paths, **({"from_tag": d["from_tag"]} if d.get("from_tag") else {})})
    return out


def tag_folder_suggestions(cfg, vault, fset, limit=2, min_notes=5):
    """Weekly (Mondays): suggest Subjects/<Tag> for an approved tag with >= min_notes notes loose in Resources."""
    if C.now(cfg).weekday() != 0:
        return []
    st = fstate()
    have = {f.rsplit("/", 1)[-1].lower().replace(" ", "-") for f in fset if f.startswith("Subjects/")}
    today = C.now(cfg).date()
    by_tag = {}
    import vault_names as VN
    for p in sorted((vault / "Resources").glob("*.md")):
        if p.name == "index.md" or VN.is_derived(p.stem):   # an Analysis/Transcript follows its summary (2026-10-13)
            continue
        for t in _fm_tags(p):
            by_tag.setdefault(t, []).append(p.relative_to(vault).as_posix())
    out, used = [], set()
    for t, paths in sorted(by_tag.items(), key=lambda kv: -len(kv[1])):
        paths = [x for x in paths if x not in used]   # a note is asked about in one folder card only (2026-10-13)
        if t in ("manuals",) or t in have or len(paths) < min_notes:
            continue
        folder = "Subjects/" + " ".join(w.capitalize() for w in t.split("-"))
        asked = st.get("asked", {}).get(folder)
        if (folder in st.get("declined", []) or folder in (st.get("later") or {})
                or (asked and (today - dt.date.fromisoformat(asked)).days < 28)):
            continue
        out.append({"folder": folder, "covers": f"the {len(paths)} notes in Resources tagged `{t}`", "paths": paths[:60],
                    "from_tag": t})
        used.update(paths[:60])
        if len(out) >= limit:
            break
    return out


def folders(vault, works=False):
    """Filing destinations. A folder that only holds one work's notes ("Subjects/Political Philosophy/Can
    Democracies be Despotic") is never one (2026-10-13) unless works=True."""
    res = _all_folders(vault)
    if works:
        return res
    import families
    wd = set(families.work_dirs(vault))
    return [x for x in res if not any(x == w or x.startswith(w + "/") for w in wd)]


def _all_folders(vault):
    res = []
    for top in PARA:
        base = C.vpath(vault, "Archives") if top == "Archives" else vault / top  # plans say "Archives"; apply_plan maps it
        if base.is_dir():
            res.append(top)
            if top in FIXED_SUBS:   # Subjects/Manuals: every level (sub-sub folders allowed)
                res += sorted(d.relative_to(vault).as_posix() for d in base.rglob("*")
                              if d.is_dir() and not any(x.startswith(".") for x in d.relative_to(base).parts))
            else:
                res += [f"{top}/{d.name}" for d in sorted(base.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    return res


def tags(vault):
    t = C.vpath(vault, "_claude-tags.md").read_text(encoding="utf-8")
    return re.findall(r"(?m)^- `([^`]+)`", t)


def validate(prop, notes, fset, tset):
    paths = {n["path"] for n in notes}
    seen = set()
    for q in prop.get("questions", []):
        q["paths"] = [p for p in q.get("paths", []) if p in paths]
        for o in q.get("options", {}).values():
            mt = (o.get("move_to") or "").strip("/")
            if mt and mt.startswith(FIXED_SUBS) and mt not in fset:   # Subjects/Manuals: existing folders only
                o["move_to"] = None
            elif mt and not any(mt == f or mt.startswith(f + "/") for f in fset):
                if not mt.startswith(PARA):
                    o["move_to"] = None
        seen |= set(q["paths"])
    for it in prop.get("items", []):
        st = it.get("set") or {}
        if "tags" in st:
            st["tags"] = [t for t in st["tags"] if t in tset]
    st = fstate()
    nfs = []
    for nf in prop.get("new_folders", []) or []:
        parts = [clean_folder_name(x) for x in str(nf.get("folder") or "").split("/") if x.strip()]
        folder = "/".join(x for x in parts if x)
        nf["paths"] = [p for p in nf.get("paths", []) if p in paths]
        if (len(parts) >= 2 and folder.startswith(FIXED_SUBS) and folder not in fset
                and folder not in st.get("declined", []) and len(nf["paths"]) >= 2):
            nf["folder"] = folder
            nfs.append(nf)
    prop["new_folders"] = nfs
    prop["items"] = [it for it in prop.get("items", []) if it.get("path") in paths]
    prop["questions"] = [q for q in prop.get("questions", []) if q["paths"] and q.get("options")]
    return paths - seen


def render(prop, day):
    lines = [f"Vault organize proposal for {day}: {len(prop['items'])} unfiled note(s).", "",
             "Reply with the letter for each question, e.g.  1a 2b 3a   (\"ok\" = all recommendations marked *, "
             "\"skip\" = leave everything in the Inbox). Unanswered questions leave their notes where they are.", ""]
    names = {it["path"]: it for it in prop["items"]}
    for q in prop["questions"]:
        lines.append(f"{q['id']}. {q['text']}")
        for p in q["paths"]:
            it = names.get(p, {})
            rn = f"  (rename to: {it['rename_to']})" if it.get("rename_to") else ""
            lines.append(f"     - {p.rsplit('/', 1)[-1][:-3]}{rn}")
        for k, o in sorted(q["options"].items()):
            lines.append(f"   {k}) {o.get('label')}{'  *' if k == 'a' else ''}")
        lines.append("")
    for nt in prop.get("new_tags", []):
        lines.append(f"{nt['id']}. Add new tag `{nt['tag']}` ({nt.get('covers', '')}) to your approved tags and apply it to "
                     f"{len(nt.get('paths', []))} note(s)?   a) yes   b) no *")
    for nf in prop.get("new_folders", []):
        lines.append(f"{nf['id']}. Create the folder `{nf['folder']}` ({nf.get('covers', '')}) and file "
                     f"{len(nf.get('paths', []))} note(s) there?   a) yes   b) no *")
    for q in prop.get("dups", []):
        lines.append(f"{q['id']}. {q['text']}")
        for p in ([q["path"]] if q["kind"] == "archived" else q["paths"]):
            lines.append(f"     - {p}")
        for k, o in sorted(q["options"].items()):
            lines.append(f"   {k}) {o.get('label')}{'  *' if k == 'a' else ''}")
        lines.append("")
    for q in prop.get("queue", []):
        lines.append(f"{q['id']}. {q['title']} ({q['where']}): {q['text']}")
        for k, o in sorted(q["options"].items()):
            lines.append(f"   {k}) {o.get('label')}{'  *' if k == 'a' else ''}")
        lines.append("")
    return "\n".join(lines)


def number_new_folders(cfg, vault, fset, prop):
    """Add the weekly tag-based suggestions and give every new-folder question an id after all the others."""
    nfs = list(prop.get("new_folders") or [])
    have = {nf["folder"] for nf in nfs}
    for x in later_folder_questions(vault) + tag_folder_suggestions(cfg, vault, fset):
        if x["folder"] not in have:
            nfs.append(x)
            have.add(x["folder"])
    import vault_names as VN
    seen = set()
    for nf in nfs:   # each note in one card only, and never a companion note (it follows its summary; 2026-10-13)
        nf["paths"] = [p for p in nf.get("paths", []) if p not in seen and not VN.is_derived(p.rsplit("/", 1)[-1][:-3])]
        seen.update(nf["paths"])
    nfs = [nf for nf in nfs if nf["paths"]]
    used = ([q["id"] for q in prop.get("questions", [])] + [nt["id"] for nt in prop.get("new_tags", [])]
            + [q["id"] for k in ("dups", "queue", "held") for q in prop.get(k, [])])
    nxt = max(used + [0]) + 1
    for nf in nfs:
        nf["exists"] = nf["folder"] in fset
        nf["proposal_id"] = nf.get("id")      # the number the proposal used (its option labels may cite it)
        nf["id"] = nxt
        nxt += 1
    prop["new_folders"] = nfs
    renumber(prop)


ORDER = ("questions", "new_tags", "new_folders", "dups", "queue", "held")   # the order the email shows them


def renumber(prop):
    """Number every question 1, 2, 3... in the order the email shows it (2026-10-19: new folders were
    numbered last but shown after the filing questions, so the list read 1 2 3 20 21 4 5 ...). Runs before the
    proposal is saved or sent, so replies and the web page use the new numbers. References to the proposal's own
    numbers in question text ("see new_folders 10") are rewritten to the new question number."""
    n, cited = 1, {}
    for k in ORDER:
        for q in prop.get(k, []) or []:
            if k in ("new_tags", "new_folders"):
                pid = q.get("proposal_id", q.get("id")) if k == "new_folders" else q.get("id")
                if pid is not None:
                    cited[(k, str(pid))] = n
            q["id"] = n
            n += 1

    def fix(s):
        if not isinstance(s, str):
            return s
        s = re.sub(r"\b(new_folders|new_tags)\s*#?\s*(\d+)",
                   lambda m: f"question {cited[(m.group(1), m.group(2))]}" if (m.group(1), m.group(2)) in cited
                   else m.group(0), s)
        return s
    for q in prop.get("questions", []) or []:
        q["text"] = fix(q.get("text"))
        for o in (q.get("options") or {}).values():
            o["label"] = fix(o.get("label"))


def prep(cfg, log, dry):
    vault = Path(cfg["vault_path"])
    try:   # the answer page must run today's code (2026-10-08: a missed nightly restart hid the folder picker)
        import nightly
        log("organize prep: " + nightly.restart_if_stale(log, dry)[0])
    except Exception as e:  # noqa: BLE001
        log(f"organize prep: service restart check failed: {e}")
    if not dry and has_unapplied():   # answers saved but not applied: apply them before asking anything again
        lk = C.VaultLock(cfg, f"{C.pc_name()} organize-retry")  # not organize-apply: that would take over a running apply's lock
        ok, lnote = lk.acquire()
        if lnote:
            log(f"organize prep: {lnote}")
        if ok:
            try:
                retry_failed(cfg, log)
            except Exception as e:  # noqa: BLE001
                log(f"organize prep: retry failed: {type(e).__name__}: {e}")
            finally:
                lk.release()
    paths = unfiled(vault, int(cfg.get("organize", {}).get("batch", 40)))
    pairs = find_pairs(vault, [p.relative_to(vault).as_posix() for p in paths])
    tpaths = set(pairs.values())
    notes = [summarize_note(vault, p) for p in paths if p.relative_to(vault).as_posix() not in tpaths]
    for n in notes:
        if n["path"] in pairs:
            n["transcript_note"] = Path(pairs[n["path"]]).stem + " (filed and renamed together with this note)"
    day = C.now(cfg).strftime("%Y-%m-%d")
    rep = C.APP / "reports"
    if not notes:
        log("organize prep: nothing unfiled")
        dups = dup_questions(cfg, log, 1)
        qq = queue_questions(cfg, log, max([q["id"] for q in dups] + [0]) + 1)
        hq = held_questions(cfg, log, max([q["id"] for q in dups + qq] + [0]) + 1, {q.get("item") for q in qq})
        if not dups and not qq and not hq and not tag_folder_suggestions(cfg, vault, folders(vault)):
            send_and_record(cfg, log, None, day, None, dry)   # still report held items / overnight needs, if any
            supersede_older(cfg, log, day, dry)           # nothing left to ask: earlier open days are done with
            return 0
        prop = {"items": [], "questions": [], "new_tags": [], "dups": dups, "queue": qq, "held": hq, "pairs": {}, "day": day,
                "status": "sent" if not dry else "dry-run", "created": C.now(cfg).isoformat(timespec="seconds"),
                "web_token": secrets.token_urlsafe(16)}
        number_new_folders(cfg, vault, folders(vault), prop)
        keep_open_token(cfg, log, rep / f"organize-{day}.json", prop, dry)
        (rep / f"organize-{day}.json").write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
        (rep / f"organize-{day}.md").write_text(render(prop, day), encoding="utf-8")
        send_and_record(cfg, log, rep / f"organize-{day}.json", day, prop, dry)
        supersede_older(cfg, log, day, dry)
        log(f"organize proposal {'(dry run) ' if dry else ''}ready: 0 notes, {len(dups)} duplicate question(s)")
        return 0
    fset, tset = folders(vault), tags(vault)
    types = ["Daily Note", "Capture", "Web Clipping", "Web Summary", "Image", "Video Summary", "Podcast Summary",
             "Transcript", "Podcast Transcript",
             "Project Note", "Area Note", "Reference", "Atomic Note", "Meeting Note", "Email", "Personal Note",
             "Article", "Recipe", "Manual", "Book Summary"]   # added 2026-09-28 (intake queue, phase 2)
    material = json.dumps({"notes": notes, "folders": fset, "tags": tset, "types": types,
                           "past_choices": past_choices(), "folder_examples": folder_examples(vault, fset),
                           "name_corrections": _name_examples(),
                           "declined_folders": fstate().get("declined", [])}, ensure_ascii=False)
    work = Path(cfg["local_root"]) / "work"
    prop = None
    for attempt in (1, 2):
        out, info = claude_cli.ask(PROMPT, material, work, timeout=600, record=False, route="organize")   # recorded below, with the catch-up
        if out:
            try:
                import claude_auth
                claude_auth.record_ok(cfg)
            except Exception:  # noqa: BLE001
                pass
        if not out:
            log(f"claude failed: {info.get('error')}")
            if info.get("auth"):   # 2026-10-24: sign-in expired - one clear email, catch-up once it works again
                import claude_auth
                claude_auth.record_failure(info.get("error"), "the 06:00 filing proposal", cfg, missed="organize")
                log("organize: Claude sign-in failed; recorded for catch-up (claude_auth recover)")
                return 1
            continue
        m = re.search(r"\{.*\}", out, re.S)
        try:
            prop = json.loads(m.group(0))
            break
        except Exception:
            log(f"attempt {attempt}: proposal was not valid JSON")
    if not prop:
        err = (info or {}).get("error") if out is None else "the answer wasn't valid JSON (twice)"
        C.send_mail(cfg, f"[{TAG} {day}] could not prepare a proposal",
                    "<p>This morning's filing proposal couldn't be built: Claude was asked twice and neither answer "
                    "was usable. <b>Nothing in the vault was changed</b>; the unfiled notes stay where they are and "
                    "tomorrow's 06:00 run picks them up again.</p>"
                    f"<p>Last error: {err}</p><p style='color:#666'>Details: "
                    f"{cfg['local_root']}\\logs\\organize-{day}.log. To try again now, start the "
                    f"Organize task (Task Scheduler, {C.task_path(cfg).strip(chr(92))} folder) or ask Claude to run it.</p>")
        return 1
    missing = validate(prop, notes, fset, tset)
    if missing:
        prop["questions"].append({"id": max([q["id"] for q in prop["questions"]] + [0]) + 1,
                                  "text": "Notes the proposal didn't place", "paths": sorted(missing),
                                  "options": {"a": {"label": "Resources", "move_to": "Resources"},
                                              "b": {"label": "leave in Inbox", "move_to": None}}})
    prop.update(day=day, status="sent" if not dry else "dry-run", created=C.now(cfg).isoformat(timespec="seconds"),
                web_token=secrets.token_urlsafe(16), pairs=pairs)
    ids = [q["id"] for q in prop["questions"]] + [nt["id"] for nt in prop.get("new_tags", [])]
    prop["dups"] = dup_questions(cfg, log, max(ids + [0]) + 1)
    prop["queue"] = queue_questions(cfg, log, max(ids + [q["id"] for q in prop["dups"]] + [0]) + 1)
    prop["held"] = held_questions(cfg, log, max(ids + [q["id"] for q in prop["dups"] + prop["queue"]] + [0]) + 1,
                                  {q.get("item") for q in prop["queue"]})
    number_new_folders(cfg, vault, fset, prop)
    keep_open_token(cfg, log, rep / f"organize-{day}.json", prop, dry)
    (rep / f"organize-{day}.json").write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
    (rep / f"organize-{day}.md").write_text(render(prop, day), encoding="utf-8")
    send_and_record(cfg, log, rep / f"organize-{day}.json", day, prop, dry)
    supersede_older(cfg, log, day, dry)
    log(f"organize proposal {'(dry run) ' if dry else ''}ready: {len(notes)} notes, {len(prop['questions'])} questions, "
        f"{len(prop['dups'])} duplicate question(s)")
    return 0


def keep_open_token(cfg, log, f, prop, dry):
    """A second prep on the same day (e.g. a manual re-run) must not break the link already emailed: an open
    proposal's web_token and email record are kept, and the replaced proposal is saved under work/organize-replaced
    (outside reports, so it never counts as an open day). Home page plan, 2026-11-12."""
    try:
        old = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except (OSError, ValueError):
        old = None
    if not old or old.get("status") != "sent" or dry:
        return
    prop["web_token"] = old.get("web_token") or prop.get("web_token")
    if old.get("email"):
        prop["email"] = old["email"]
    keep = Path(cfg["local_root"]) / "work" / "organize-replaced"
    keep.mkdir(parents=True, exist_ok=True)
    (keep / f"organize-{old.get('day')}-{C.now(cfg):%H%M%S}.json").write_text(
        json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"organize prep: replaced the open proposal for {old.get('day')} (kept its link; old copy in {keep.name})")


RETRY_MAX = 3


def retry_failed(cfg, log):
    """Re-apply answers that were saved but never finished (the apply crashed or was killed) before anything asks
    those questions again (2026-11-23, the owner). Caller holds the vault lock. Called by intake every 30 minutes
    and by the 06:00 prep. Each try counts; after RETRY_MAX the day is marked apply_failed, the owner gets one
    email, and the next morning's questions ask again. Returns the summaries of the answers applied."""
    done = []
    for d, (f, prop) in sorted(pending().items()):
        sub = prop.get("submitted")
        if not sub:
            continue
        n = int(sub.get("attempts") or 0)
        if n >= RETRY_MAX:
            prop.update(status="apply_failed", failed_at=C.now(cfg).isoformat(timespec="seconds"))
            f.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
            log(f"organize: the {d} answers failed {n} times; giving up (asked again in the next update)")
            try:
                C.send_mail(cfg, f"[{U.NOTICE} {d}] Your answers couldn't be applied",
                            U.simple(U.fmt_day(d), "Obsidian Vault Update",
                                     f"<p style=\"margin:0 0 10px 0;\">Your answers to the {U.e(d)} update ({U.e(sub.get('via') or '')}) "
                                     f"were tried {n} times and failed each time, so <b>what didn't get done is asked again</b> "
                                     "in the next morning's update. Anything already filed stays filed.</p>"
                                     f"<p style=\"margin:0;font-size:10pt;color:#616161;\">Details: the organize and server logs "
                                     f"for {U.e(d)} on {U.e(C.pc_name(cfg))}.</p>", "Not applied", "warn"))
            except Exception as e:  # noqa: BLE001
                log(f"apply-failed email not sent: {type(e).__name__}: {e}")
            continue
        ans = {(int(k) if str(k).isdigit() else k): v for k, v in (sub.get("answers") or {}).items()}
        log(f"organize: the {d} answers were saved but not applied (try {n}); applying them again")
        try:
            done.append(apply_answers(cfg, log, f, prop, ans, sub.get("mode") or "letters", sub.get("via") or "web page",
                                      sub.get("notes")))
        except Exception as e:  # noqa: BLE001
            log(f"organize: retry of the {d} answers failed: {type(e).__name__}: {e}")
    return done


def has_unapplied():
    return any((p.get("submitted") for _, p in pending().values()))


def supersede_older(cfg, log, day, dry=False):
    """Each morning re-asks everything still unanswered, so an earlier day that is still open is closed off as
    superseded (2026-11-12, the owner: Oct 24, Oct 30 and Nov 6 stayed open for good). Its page and email replies then
    point to the newer questions instead of applying answers to stale ones."""
    if dry:
        return
    for d, (f, prop) in pending().items():
        if d >= day:
            continue
        prop.update(status="superseded", superseded_by=day, superseded_at=C.now(cfg).isoformat(timespec="seconds"))
        f.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"organize: the {d} questions are superseded by {day}'s (asked again there)")


def send_and_record(cfg, log, f, day, prop, dry):
    """Send the daily update and record on the proposal whether it was delivered (2026-11-12: Graph was down and
    a 502 was logged as 'emailed'). Never raises: the questions are already saved and answerable on the web."""
    try:
        code = U.send_daily(cfg, log, day, prop, dry)
        rec = {"sent": code == 202, "code": code, "at": C.now(cfg).isoformat(timespec="seconds")}
        if code is None:
            off = (cfg.get("organize") or {}).get("email_enabled") is False
            rec = {"sent": None, "why": "dry run" if dry else "email is off" if off else "nothing needed an email",
                   "at": rec["at"]}
        elif code != 202:
            rec["error"] = f"HTTP {code}"
            log(f"daily update NOT delivered (HTTP {code}); the questions are on the home page")
    except Exception as e:  # noqa: BLE001
        rec = {"sent": False, "error": f"{type(e).__name__}: {e}"[:300], "at": C.now(cfg).isoformat(timespec="seconds")}
        log(f"daily update NOT delivered ({rec['error']}); the questions are on the home page")
    if f is None or prop is None:
        return rec
    tries = int((prop.get("email") or {}).get("tries") or 0) + 1
    prop["email"] = dict(rec, tries=tries)
    try:
        cur = json.loads(f.read_text(encoding="utf-8"))
        if cur.get("web_token") == prop.get("web_token"):
            cur["email"] = prop["email"]
            f.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, ValueError) as e:
        log(f"couldn't record the email result: {e}")
    return rec


def catch_up_send(cfg, log, dry=False):
    """Called by Intake once Graph reads work again: an open proposal of today or yesterday whose update email
    wasn't delivered is sent once more (at most 3 tries a day). Home page plan, 2026-11-12."""
    today = C.now(cfg).date()
    for day, (f, prop) in sorted(pending().items()):
        em = prop.get("email") or {}
        try:
            age = (today - dt.date.fromisoformat(day)).days
        except ValueError:
            continue
        if em.get("sent") is not False or age > 1 or int(em.get("tries") or 0) >= 3 or dry:
            continue
        log(f"catch-up: the {day} update wasn't delivered ({em.get('error')}); sending it again")
        send_and_record(cfg, log, f, day, prop, dry)


def resend(cfg, log, day, reopen):
    """Rebuild and send the daily update for an existing proposal (reopen = make an applied one answerable again)."""
    f = C.APP / "reports" / f"organize-{day}.json"
    prop = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    if prop:
        if reopen and prop.get("status") == "applied" and not prop.get("filed"):
            prop["status"] = "sent"
            for k in ("answers", "applied", "filed"):
                prop.pop(k, None)
        prop.setdefault("web_token", secrets.token_urlsafe(16))
        f.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
        if prop.get("status") != "sent":
            prop = None
    send_and_record(cfg, log, f if prop else None, day, prop, False)
    return 0


# ------------------------------------------------------------------ replies
# Where the quoted original starts in a reply. html2text renders Outlook's bold "From:" as "**From:**",
# which the old pattern (^From:) missed - so the quoted instructions ("skip" = ...) were read as the answer.
QUOTE_START = re.compile(r"(?mi)^[\s>*_]*(?:from|von|de)\s*:\s*[*_]*\s|^[\s>*_]*-{2,}\s*original message|"
                         r"^[\s>*_]*on .{4,200}wrote:|^[\s>*_]*_{8,}|^[\s>*_]*sent from my |^[\s>*_]*get outlook for ")


def reply_text(text):
    m = QUOTE_START.search(text or "")
    first = text[:m.start()] if m else (text or "")
    return "\n".join(ln for ln in first.splitlines() if not ln.lstrip().startswith(">"))


def parse_answers(text, prop):
    """Return (answers, mode). mode: 'letters', 'ok' (defaults, letters override), 'skip', or 'unclear'."""
    low = reply_text(text).lower().replace("*", " ").replace("_", " ")
    ids = ([q["id"] for q in prop["questions"]] + [nt["id"] for nt in prop.get("new_tags", [])]
           + [nf["id"] for nf in prop.get("new_folders", [])]
           + [q["id"] for q in prop.get("dups", [])] + [q["id"] for q in prop.get("queue", [])]
           + [q["id"] for q in prop.get("held", []) if q.get("options")])
    ans = {int(n): l for n, l in re.findall(r"\b(\d{1,2})\s*[\).:-]?\s*([a-d])\b", low) if int(n) in ids}
    if re.search(r"\b(ok|okay|yes|approve[ds]?|approve all|accept(ed)?|defaults?|all good|sounds good|go ahead)\b", low):
        for q in prop["questions"] + prop.get("dups", []) + prop.get("queue", []):
            ans.setdefault(q["id"], "a")
        for q in prop.get("held", []):
            if q.get("options"):
                ans.setdefault(q["id"], "a")
        for nt in prop.get("new_tags", []) + prop.get("new_folders", []):
            ans.setdefault(nt["id"], "b")
        return ans, "ok"
    if ans:
        return ans, "letters"
    if re.search(r"\b(skip|none|leave (it|them|everything))\b", low):
        return {}, "skip"
    return {}, "unclear"


def build_plan(prop, ans, day):
    items = {it["path"]: dict(path=it["path"], set=dict(it.get("set") or {}), add_generated=True,
                              **({"rename_to": it["rename_to"]} if it.get("rename_to") else {}))
             for it in prop["items"]}
    chosen = []
    picks = prop.get("picks") or {}   # web page "Choose a different folder" (2026-10-07)
    q_picks = prop.get("q_picks") or {}   # web page, per note of a multi-note question (2026-10-15)
    for q in prop["questions"]:
        k = ans.get(q["id"])
        opt = ({"move_to": picks[str(q["id"])]} if k == "p" and picks.get(str(q["id"]))
               else q["options"].get(k) if k else None)
        qp = q_picks.get(str(q["id"])) or {}
        for p in q["paths"]:
            if p in qp:                      # this note's own choice wins over the question's answer
                if qp[p]:                    # "" = leave it where it is (asked again tomorrow)
                    items.setdefault(p, {"path": p, "set": {}, "add_generated": True})["move_to"] = qp[p]
                    chosen.append(p)
                    ans.setdefault(q["id"], "n")
                continue
            if opt and opt.get("move_to"):
                items.setdefault(p, {"path": p, "set": {}, "add_generated": True})["move_to"] = opt["move_to"]
                chosen.append(p)
    new_tags = []
    for nt in prop.get("new_tags", []):
        if ans.get(nt["id"]) == "a":
            new_tags.append(nt)
            for p in nt.get("paths", []):
                if p in items:
                    items[p]["set"]["tags"] = sorted(set(items[p]["set"].get("tags", [])) | {nt["tag"]})
    qof = {p: q["id"] for q in prop["questions"] for p in q["paths"]}
    nf_picks = prop.get("nf_picks") or {}   # web page, per note: folder, another folder, or "" = later (2026-10-12)
    for nf in prop.get("new_folders", []):
        if ans.get(nf["id"]) != "a":
            continue
        picks_nf = nf_picks.get(str(nf["id"])) or {}
        for p in nf.get("paths", []):
            dest = picks_nf.get(p, nf["folder"])
            if not dest:                        # decide later: left where it is (asked again tomorrow)
                continue
            if p in items:
                if ans.get(qof.get(p)) == "p":     # an explicit pick on the note's own question wins
                    continue
                items[p]["move_to"] = dest
                if qof.get(p) is not None:
                    ans.setdefault(qof[p], "nf")
            else:                               # weekly tag suggestion: notes already filed in Resources
                if dest == p.rsplit("/", 1)[0]:
                    continue
                items[p] = {"path": p, "set": {}, "add_generated": True, "move_to": dest}
            if p not in chosen:
                chosen.append(p)
    plan = [items[p] for p in chosen]   # only notes whose destination the owner answered
    for sp, tp in (prop.get("pairs") or {}).items():   # a Transcript note follows its summary
        s_it = items.get(sp)
        if not s_it or sp not in chosen:
            continue
        t_it = {"path": tp, "set": {k: v for k, v in s_it["set"].items() if k == "tags"}, "add_generated": True,
                "move_to": s_it["move_to"], "pair_with": sp}
        if s_it.get("rename_to"):
            import vault_names as VN   # "<work> (Transcript)" (2026-10-10; was "<summary name> - Transcript")
            t_it["rename_to"] = VN.transcript_name(s_it["rename_to"])
            # the two notes link only to each other: renaming both together keeps every link working
            s_it["rename_approved"] = t_it["rename_approved"] = True
        s_it["pair_with"] = tp
        plan.append(t_it)
    return {"actor": "claude/organize-by-email", "log": f"Organize by email ({day}, {len(plan)} notes)", "items": plan}, new_tags


def fix_junk_titles(vault, plan_items):
    """A note being renamed whose `title` is meaningless PDF metadata ("No Job Name", "x.indd") gets the new
    name as its title too, so duplicate checks and indexes see the real title (2026-10-01)."""
    sys.path.insert(0, str(C.VL))
    from vaultlib import junk_title
    for it in plan_items:
        if not it.get("rename_to") or "title" in (it.get("set") or {}):
            continue
        try:
            head = (vault / it["path"]).read_text(encoding="utf-8", errors="replace")[:4000].split("\n---", 1)[0]
        except OSError:
            continue
        m = re.search(r"(?m)^title:\s*[\"']?(.*?)[\"']?\s*$", head)
        if not m or junk_title(m.group(1)):
            it.setdefault("set", {})["title"] = _clean_name(it["rename_to"])


def add_tags_to_vocab(vault, new_tags):
    p = C.vpath(vault, "_claude-tags.md")
    s = p.read_text(encoding="utf-8").rstrip("\n")
    for nt in new_tags:
        if f"`{nt['tag']}`" not in s:
            s += f"\n- `{nt['tag']}` — {nt.get('covers', '')}"
    p.write_text(s + "\n", encoding="utf-8")


def pending():
    """{day: (file, proposal)} for proposals still waiting for an answer."""
    out = {}
    for f in (C.APP / "reports").glob("organize-*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("status") == "sent":
                out[d["day"]] = (f, d)
        except Exception:
            continue
    return out


def has_pending():
    return bool(pending())


def apply_answers(cfg, log, f, prop, ans, mode, via, notes=None, progress=None):
    """Apply answers to a pending proposal (caller holds the vault lock). Returns a short summary.
    Used by both the email-reply check (intake) and the web page (vault_server). progress(step, text, items=None)
    is called as each stage starts (the web page shows it live, 2026-10-13)."""
    if progress is None:
        def progress(step, text, items=None):
            return None
    vault = Path(cfg["vault_path"])
    day = prop["day"]
    # Save the answers before acting on them (2026-11-23): on 2026-11-22 an apply crashed after filing, the answers
    # were never written down, and the next morning asked everything again. retry_failed() re-runs saved answers.
    sub = prop.get("submitted") or {}
    keyed = {str(k): v for k, v in ans.items()}
    retry = bool(sub) and sub.get("answers") == keyed and sub.get("mode") == mode
    try:
        json.dumps(notes)
        keep_notes = notes
    except (TypeError, ValueError):
        keep_notes = None
    prop["submitted"] = {"answers": keyed, "mode": mode, "via": sub.get("via", via) if retry else via,
                         "notes": {str(k): v for k, v in keep_notes.items()} if isinstance(keep_notes, dict) else keep_notes,
                         "at": sub.get("at") if retry else C.now(cfg).isoformat(timespec="seconds"),
                         "attempts": int(sub.get("attempts") or 0) + 1 if retry else 1}
    f.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
    plan, new_tags = build_plan(prop, ans, day)
    if mode != "skip" and not retry:   # a retry must not log the same filing choices twice
        try:
            record_choices(vault, prop, plan["items"], ans, prop.get("picks") or {})
            st = fstate()
            later = st.setdefault("later", {})   # {folder: {"covers", "from_tag", "paths"}} asked again next morning
            for nf in prop.get("new_folders", []):
                a = ans.get(nf["id"])
                if a == "b" and nf["folder"] not in st["declined"]:
                    st["declined"].append(nf["folder"])
                if a in ("a", "b"):
                    st.setdefault("asked", {})[nf["folder"]] = day
                    later.pop(nf["folder"], None)
                if a == "a":   # notes he set to "decide later" come back tomorrow, in the folder's card
                    picks_nf = (prop.get("nf_picks") or {}).get(str(nf["id"])) or {}
                    rest = [p for p in nf.get("paths", []) if picks_nf.get(p, nf["folder"]) == ""]
                    if rest:
                        later[nf["folder"]] = {"covers": nf.get("covers", ""), "from_tag": nf.get("from_tag"), "paths": rest}
                elif not a and nf.get("from_tag") or (not a and nf["folder"] in later):
                    # The owner (2026-10-12): "decide later" on a suggested folder means tomorrow, not in 28 days
                    later[nf["folder"]] = {"covers": nf.get("covers", ""), "from_tag": nf.get("from_tag"),
                                           "paths": nf.get("paths", [])}
            save_fstate(st)
        except Exception as ex:  # noqa: BLE001  learning is best-effort; never block filing
            log(f"filing-choice log failed: {ex}")
    # Duplicate answers first (2026-10-13): filing renamed/moved "Economics in One Lesson 2" before its
    # "delete" answer ran, so the delete was skipped and both copies were filed. A note being deleted isn't filed.
    progress("Duplicates", "carrying out your duplicate answers")
    dup_lines = apply_dups(cfg, log, prop, ans) if prop.get("dups") and mode != "skip" else []
    gone = [it for it in plan["items"] if not (vault / it["path"]).exists()]
    if gone:
        plan["items"] = [it for it in plan["items"] if (vault / it["path"]).exists()]
        log(f"not filed ({len(gone)} note(s) no longer at the path asked about: deleted as a duplicate, or moved "
            f"since): {', '.join(it['path'] for it in gone)[:600]}")
    fix_junk_titles(vault, plan["items"])
    import my_note
    my_note.ensure_items(vault, plan["items"])   # every filed source item ends with see_also / my_note (2026-10-01)
    work = Path(cfg["local_root"]) / "work"
    pj = work / f"organize-plan-{day}.json"
    pj.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    result, lint = [], ""
    if plan["items"]:
        if new_tags:
            add_tags_to_vocab(vault, new_tags)
        progress("Filing", f"moving and renaming {len(plan['items'])} note(s)",
                 [it.get("rename_to") or it["path"].rsplit("/", 1)[-1][:-3] for it in plan["items"]])
        rc, out = C.run(log, C.VL / "apply_plan.py", vault, pj)
        result.append(out.strip()[-1500:])
        fix_pair_links(vault, plan["items"], log)
        try:   # a filed note's Analysis, Transcript and parts follow it (families.py, 2026-10-13)
            progress("Filing", "moving each work's Analysis, Transcript and part notes with it")
            import families
            moved = [it.get("rename_to") or it["path"] for it in plan["items"] if it.get("move_to")]
            for r in families.regroup(cfg, log, only=moved):
                log("families: " + r)
        except Exception as e:  # noqa: BLE001
            log(f"families regroup failed: {type(e).__name__}: {e}")
        progress("Indexes", "rebuilding folder indexes and topic hubs")
        for sname in ("build_indexes.py", "build_hubs.py"):
            C.run(log, C.VL / sname, vault)
        progress("Check", "running the compliance check")
        rc, out = C.run(log, C.VL / "okf_lint.py", vault, "--no-orphans")
        lint = (out.strip().splitlines() or ["?"])[-1]
    progress("Queue", "applying your intake-queue and concept answers")
    q_lines = apply_queue(cfg, log, prop, ans) if prop.get("queue") and mode != "skip" else []
    if prop.get("held") and mode != "skip":   # items left in Remember: choices and the owner's notes (2026-10-01)
        q_lines += apply_held(cfg, log, prop, ans, notes)
    if dup_lines and not plan["items"]:
        for sname in ("build_indexes.py", "build_hubs.py"):
            C.run(log, C.VL / sname, vault)
    tset_ = set((prop.get("pairs") or {}).values())
    main_items = [it for it in plan["items"] if it["path"] not in tset_]   # a pair's transcript isn't counted
    filed_paths = {it["path"] for it in main_items}
    left = len([it for it in prop["items"] if it["path"] not in filed_paths and it["path"] not in tset_])
    prop.update(status="applied", answers={str(k): v for k, v in ans.items()}, mode=mode, via=via,
                applied=C.now(cfg).isoformat(timespec="seconds"), filed=len(main_items))
    if retry:
        prop["retried"] = prop["submitted"]["attempts"] - 1
    prop.pop("submitted", None)
    f.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
    names = {it["path"]: (it.get("rename_to") or it["path"].rsplit("/", 1)[-1][:-3]) for it in prop["items"]}
    rows = "".join(f"<tr><td style=\"padding:3px 8px 3px 0;\">&#10003; {U.e(names.get(it['path'], it['path']))}</td>"
                   f"<td style=\"padding:3px 0;color:#616161;\">&rarr; {U.e(it['move_to'])}</td></tr>" for it in main_items)
    how = {"ok": "approved all suggestions", "skip": "asked to leave everything in the Inbox",
           "letters": ("answered " + ", ".join(f"{k}{v}" for k, v in sorted(ans.items()))) if ans
           else "left everything in the Inbox"}.get(mode, mode)
    n_dups = len(prop.get("dups", []))
    undecided = n_dups - len([q for q in prop.get("dups", []) if q["options"].get(ans.get(q["id"]) or "")])
    who = f"You {how} ({via})"
    if via == "Claude":   # Vault connector (plan: 2026-10-08 Vault search - plan.md, phase 3)
        who = (f"Answered via Claude at your request: {', '.join(f'{k}{v}' for k, v in sorted(ans.items())) or 'none'}"
               " (questions that remove something were left for you)")
    summary = (f"{who}: {len(main_items)} note(s) filed; {left} left in the Inbox"
               + (f"; new tag(s) added: {', '.join(t['tag'] for t in new_tags)}" if new_tags else "")
               + (f"; duplicates: {'; '.join(dup_lines)}" if dup_lines else "")
               + (f"; intake queue: {'; '.join(q_lines)}" if q_lines else "") + ".")
    drows = "".join(f"<tr><td style=\"padding:3px 0;\">&#10003; {U.e(x)}</td></tr>" for x in dup_lines)
    body = (f"<p style=\"margin:0 0 10px 0;\">{U.e(who)}.</p>"
            + (f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" style=\"font-size:11pt;\">{rows}</table>" if rows else "")
            + (f"<p style=\"margin:10px 0 0 0;\">{left} note{'s' if left != 1 else ''} stay in the Inbox and will be asked about "
               f"again tomorrow morning.</p>" if left else "")
            + (f"<p style=\"margin:12px 0 4px 0;\"><b>Duplicates</b></p><table role=\"presentation\" cellpadding=\"0\" "
               f"cellspacing=\"0\" style=\"font-size:11pt;\">{drows}</table>" if drows else "")
            + (f"<p style=\"margin:12px 0 4px 0;\"><b>Intake queue</b></p><table role=\"presentation\" cellpadding=\"0\" "
               f"cellspacing=\"0\" style=\"font-size:11pt;\">"
               + "".join(f"<tr><td style=\"padding:3px 0;\">&#10003; {U.e(x)}</td></tr>" for x in q_lines) + "</table>" if q_lines else "")
            + (f"<p style=\"margin:10px 0 0 0;\">{undecided} duplicate question{'s' if undecided != 1 else ''} left "
               f"unanswered; asked again tomorrow morning.</p>" if n_dups and undecided else "")
            + (f"<p style=\"margin:10px 0 0 0;font-size:10pt;color:#616161;\">Compliance check after filing: {U.e(lint)}</p>" if lint else ""))
    progress("Email", "sending the confirmation email")
    status = (f"{len(main_items)} filed" + (f", {left} left in the Inbox" if left else "")
              + (f", {len(dup_lines)} duplicate(s) resolved" if dup_lines else "")
              + (f", {len(q_lines)} queue item(s) decided" if q_lines else ""))
    C.send_mail(cfg, f"[{U.NOTICE} {day}] Done: {status}",
                U.simple(U.fmt_day(day), "Obsidian Vault Update: filed", body, status,
                         "ok" if plan["items"] or dup_lines or q_lines or mode == "skip" else "warn"))
    log(summary)
    if result:
        log("apply_plan: " + result[0][-400:].replace("\n", " | "))
    return summary


USAGE_WORDS = ((re.compile(r"\b(use more|more tonight)\b", re.I), "more"),
               (re.compile(r"\b(go light|light tonight)\b", re.I), "light"),
               (re.compile(r"\bnormal tonight\b", re.I), "normal"))


def usage_reply(cfg, log, text, via="email reply"):
    """'more tonight' / 'light tonight' / 'normal tonight' in a reply sets tonight's usage mode (2026-10-06).
    Returns the mode set, or None."""
    low = reply_text(text)
    for rx, mode in USAGE_WORDS:
        if rx.search(low):
            import usage_plan
            rec = usage_plan.set_mode(cfg, mode, via)
            log(f"usage mode for the night of {rec['night']}: {mode} ({via})")
            return mode
    return None


def _usage_ack(cfg, day, mode):
    import update_email as U
    import usage_plan
    C.send_mail(cfg, f"[{U.NOTICE} {day}] Tonight: {usage_plan.LABEL[mode]}",
                U.simple(U.fmt_day(day), "Obsidian Vault Update",
                         f"<p style=\"margin:0;\">Got it: tonight's vault run is set to <b>{U.e(usage_plan.LABEL[mode])}</b>. "
                         "It applies to the next night window only.</p>", usage_plan.LABEL[mode], "ok"))


def check_replies(cfg, log, s, base):
    """Apply the owner's emailed answers to pending proposals. s = Graph session, base = user URL.
    Also reads 'more tonight' / 'light tonight' in any reply to an update email (2026-10-06)."""
    pend = pending()
    r = s.get(f"{base}/mailFolders/inbox/messages", timeout=30,
              params={"$search": '"subject:Vault"', "$select": "id,subject,from,body,receivedDateTime", "$top": 15})
    if not r.ok:
        log(f"reply search failed: HTTP {r.status_code}")
        return []
    done = []
    for msg in sorted(r.json().get("value", []), key=lambda m: m.get("receivedDateTime") or ""):
        subj = msg.get("subject") or ""
        m = re.search(r"\[Vault (?:update|organize) (\d{4}-\d{2}-\d{2})\]", subj)
        frm = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        if not m or not re.match(r"(?i)^\s*(re|aw|sv)\s*:", subj) or frm != cfg["mailbox"].lower():
            continue
        day = m.group(1)
        if day not in pend:
            # already answered (e.g. on the web page) - acknowledge and clear the reply
            f = C.APP / "reports" / f"organize-{day}.json"
            if f.exists():
                prev = json.loads(f.read_text(encoding="utf-8"))
                if prev.get("status") == "superseded":   # 2026-11-12: asked again in a later update
                    s.post(f"{base}/messages/{msg['id']}/move", json={"destinationId": "deleteditems"}, timeout=30)
                    later = prev.get("superseded_by") or "a later"
                    C.send_mail(cfg, f"[{U.NOTICE} {day}] Asked again later",
                                U.simple(U.fmt_day(day), "Obsidian Vault Update",
                                         f"<p style=\"margin:0;\">These questions were asked again in the {U.e(later)} update, so "
                                         f"this reply was not applied. Answer that update, or use the home page: "
                                         f"<a href=\"{U.e(U.home_url(cfg))}\">Vault home</a>. Nothing changed.</p>",
                                         "Nothing changed", "ok"))
                    continue
                if prev.get("status") == "applied":
                    s.post(f"{base}/messages/{msg['id']}/move", json={"destinationId": "deleteditems"}, timeout=30)
                    um = usage_reply(cfg, log, (msg.get("body") or {}).get("content", ""))
                    if um:
                        _usage_ack(cfg, day, um)
                        continue
                    C.send_mail(cfg, f"[{U.NOTICE} {day}] Already answered",
                                U.simple(U.fmt_day(day), "Obsidian Vault Update",
                                         f"<p style=\"margin:0;\">Your email reply arrived after this day's questions were already "
                                         f"answered ({U.e(prev.get('via', ''))}, {U.e(str(prev.get('applied', ''))[:16])}), so it was "
                                         f"not applied again. Nothing changed.</p>", "Nothing changed", "ok"))
            continue
        f, prop = pend[day]
        body = (msg.get("body") or {}).get("content", "")
        if (msg.get("body") or {}).get("contentType") == "html":
            import html2text
            h = html2text.HTML2Text()
            h.body_width = 0
            body = h.handle(body)
        ans, mode = parse_answers(body, prop)
        s.post(f"{base}/messages/{msg['id']}/move", json={"destinationId": "deleteditems"}, timeout=30)
        um = usage_reply(cfg, log, body)
        if um and mode == "unclear":     # only the usage choice: confirm it, leave the questions open
            _usage_ack(cfg, day, um)
            continue
        if mode == "unclear":
            said = reply_text(body).strip()[:200] or "(empty)"
            C.send_mail(cfg, f"[{U.NOTICE} {day}] Couldn't read your reply - nothing changed",
                        U.simple(U.fmt_day(day), "Obsidian Vault Update",
                                 f"<p style=\"margin:0 0 10px 0;\">Your reply said: <i>{U.e(said)}</i></p>"
                                 "<p style=\"margin:0;\">I couldn't match that to an answer, so <b>nothing was filed</b> and the "
                                 "questions are still open. Reply to the original update email with <b>ok</b> (approve all "
                                 "Suggested choices), <b>skip</b> (leave everything in the Inbox), or letters such as "
                                 "<b>1a 2b</b>; or use the web page link in that email.</p>", "Nothing changed", "warn"))
            log(f"reply for {day} not understood: {said[:80]!r}")
            continue
        done.append(apply_answers(cfg, log, f, prop, ans, mode, "email reply"))
        pend.pop(day, None)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prep", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resend", metavar="YYYY-MM-DD", help="rebuild and send the daily update for that day's proposal")
    ap.add_argument("--reopen", action="store_true", help="with --resend: reopen a proposal that was applied with 0 filed")
    a = ap.parse_args()
    cfg = C.load_config()
    log = C.Log(cfg, "organize")
    if a.resend:
        return resend(cfg, log, a.resend, a.reopen)
    if a.prep:
        return prep(cfg, log, a.dry_run)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
