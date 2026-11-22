"""Keeps the notes about one work together and cross-linked (2026-10-13).

A work's notes ("family"):
    main        the Summary / Summary and original / the item itself (what analyses point at)
    analysis    "<work> (Analysis)"                    analysis_of: [[main]]
    parts       "<work> (Part NN Analysis) - ..."       analysis_of: [[main]]
    transcript  "<work> (Transcript)"
    same        other notes of the same work in the same place (e.g. "On Liberty" beside "On Liberty (Summary)")

Layout (the owner's choice, 2026-10-13): a work with 2 or more part analyses lives in its own folder,
<home>/<work>/, holding the main note, its Analysis, Transcript and every part. A work with 0 or 1 part stays
flat: everything sits in <home> side by side (the old one-part folders are emptied and removed).
<home> is wherever the main note was filed; when the main note moves, the family follows.

Work folders are never offered as filing destinations (organize.folders) and give no folder tag: a note moved
into one is tagged after <home> (apply_plan "tag_folder").

see_also: the main note, the Analysis and the Transcript list every other note of the work; each part lists the
main note, the Analysis and the Transcript. Links are only ever added, never removed. Concept analysis ignores
these family links when it reads the owner's see_also (analyze.my_notes), so adding them never triggers a redo.

    regroup(cfg, log, dry=False, only=None) -> report rows       (nightly 3c, after organize filing, after analyze)
    is_work_dir(path), work_dirs(vault), parts_dir(vault, main_rel, n_parts), family_rels(vault, rel)
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import vault_names as VN  # noqa: E402

PATH_LIMIT = 250          # Windows: full paths past ~260 characters fail (2026-10-12: 3 items held)
SKIP_TOP = VN.SKIP_TOP    # Inbox, Clippings, _Obsidian Internal, Archives, Templates, Claude
_LINK = re.compile(r"\s*\[\[([^\]|#]+)")


def _target(v):
    m = _LINK.match(str(v or ""))
    return m.group(1).strip() if m else ""


def _names(v):
    """Names in a see_also value (list or single; [[Name|x]] or bare)."""
    out = []
    for x in (v if isinstance(v, list) else [v] if v else []):
        m = _LINK.match(str(x or ""))
        nm = (m.group(1) if m else str(x or "")).strip()
        if nm:
            out.append(nm)
    return out


def _top(rel):
    return rel.split("/", 1)[0]


def _part_no(n):
    try:
        return int(n["fm"].get("part") or 0)
    except (TypeError, ValueError):
        m = re.search(r"\(Part (\d+) Analysis\)| - Part (\d+)", n["name"])
        return int(next(g for g in m.groups() if g)) if m else 0


def scan(vault):
    import analyze as AZ
    return AZ.scan(vault)


def families(notes):
    """{main name: {"work", "main", "analysis", "parts": [names in order], "transcript", "same": [names]}}"""
    low = {k.lower(): k for k in notes}
    fam = {}

    def get(main):
        return fam.setdefault(main, {"work": VN.work_of(main), "main": main, "analysis": None, "parts": [],
                                     "transcript": None, "same": []})
    for n in notes.values():
        if n["type"] not in ("Analysis", "Part Analysis") or _top(n["rel"]) in SKIP_TOP:
            continue
        main = low.get(_target(n["fm"].get("analysis_of")).lower())
        if not main or _top(notes[main]["rel"]) in SKIP_TOP or notes[main]["type"] in ("Analysis", "Part Analysis"):
            continue
        f = get(main)
        if n["type"] == "Part Analysis":
            f["parts"].append(n["name"])
        elif not f["analysis"] or n["name"] in VN.analysis_names(main):
            f["analysis"] = n["name"]
    for n in notes.values():                     # video / podcast summaries: summary + transcript
        if _top(n["rel"]) in SKIP_TOP or n["name"] in fam:
            continue
        if VN.kind(n["fm"]) == "summary" and VN.find(notes, VN.transcript_names(n["name"])):
            get(n["name"])
    for main, f in fam.items():
        tr = VN.find(notes, VN.transcript_names(main))
        if tr and _top(tr["rel"]) not in SKIP_TOP:
            f["transcript"] = tr["name"]
        f["parts"].sort(key=lambda x: (_part_no(notes[x]), x))
        members = {main, f["analysis"], f["transcript"], *f["parts"]}
        here = {os.path.dirname(notes[main]["rel"]), home_of(notes, f)}
        for n in notes.values():                 # "On Liberty" beside "On Liberty (Summary)"
            if (n["name"] not in members and VN.work_of(n["name"]) == f["work"]
                    and os.path.dirname(n["rel"]) in here and n["type"] not in ("Analysis", "Part Analysis")):
                f["same"].append(n["name"])
    return fam


def home_of(notes, f):
    """Where the work is filed: the main note's folder, or the folder above it when it already sits in its
    own work folder."""
    d = os.path.dirname(notes[f["main"]]["rel"]).replace("\\", "/")
    if "/" in d and _names_work(d.rsplit("/", 1)[-1], f["work"]):
        return d.rsplit("/", 1)[0]
    return d


def _names_work(folder, work):
    """folder is the work's own folder: its full name, or a shortened one (a word-boundary prefix of at least
    20 characters, used when the full name would make Windows paths too long; 2026-10-14)."""
    return folder == work or (len(folder) >= 20 and work.startswith(folder))


def short_name(name, room):
    """name cut to at most room characters at a word boundary, without a half-open '(' or trailing punctuation."""
    if len(name) <= room:
        return name
    b = name[:max(room, 20)]
    b = (b.rsplit(" ", 1)[0] if " " in b[10:] else b).rstrip(" ,;:-(")
    if b.count("(") > b.count(")"):
        b = b[:b.rfind("(")].rstrip(" ,;:-(")
    return b


_ROOT_LEN = []


def root_len_default():
    """Length of the vault root (config vault_path) plus the separator, for the Windows path-length checks."""
    if not _ROOT_LEN:
        try:
            _ROOT_LEN.append(len(C.load_config()["vault_path"]) + 1)
        except Exception:  # noqa: BLE001  config unreadable (tests): assume a short root
            _ROOT_LEN.append(16)
    return _ROOT_LEN[0]


def work_folder(home, work, longest, root_len=None):
    """The work's folder name: its name, shortened when the longest note in it would pass PATH_LIMIT (2026-10-14)."""
    root_len = root_len or root_len_default()
    room = PATH_LIMIT - root_len - len(home) - 1 - 1 - longest - 3
    return work if len(work) <= room else short_name(work, room)


def target_of(notes, f, root_len=None):
    h = home_of(notes, f)
    if len(f["parts"]) < 2:
        return h
    return f"{h}/{work_folder(h, f['work'], max(len(m) for m in members(f)), root_len)}"


def members(f):
    return [x for x in [f["main"], f["analysis"], f["transcript"], *f["parts"]] if x]


def _dir_of_work(folder, work):
    """A folder named for this work: same name, or either one shortened for Windows path length (2026-10-14)."""
    return folder == work or (min(len(folder), len(work)) >= 20 and (work.startswith(folder) or folder.startswith(work)))


def is_work_dir(path):
    """A folder that holds one work's notes (its name is the work's name, e.g. 'Can Democracies be Despotic')."""
    p = Path(path)
    try:
        for c in p.iterdir():
            if c.is_file() and c.suffix.lower() == ".md" and c.name != "index.md" and _dir_of_work(p.name, VN.work_of(c.stem)):
                return True
    except OSError:
        return False
    return False


def work_dirs(vault):
    """Vault-relative work folders (never filing destinations)."""
    vault = Path(vault)
    out = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel = Path(root).relative_to(vault).as_posix()
        if rel == "." or _top(rel) in SKIP_TOP:
            continue
        name = Path(root).name
        if any(f.lower().endswith(".md") and f != "index.md" and _dir_of_work(name, VN.work_of(f[:-3])) for f in files):
            out.append(rel)
        elif not dirs and not [f for f in files if f != "index.md"] and any(
                f.lower().endswith(".md") and VN.work_of(f[:-3]) == name for f in os.listdir(Path(root).parent)):
            # A work's emptied folder (only a generated index.md left) whose notes now sit beside it in the
            # parent folder, e.g. after a 1-part work moved back out of its own folder (2026-10-14).
            out.append(rel)
    return out


def parts_dir(vault, main_rel, n_parts):
    """Folder for a work's part analyses (analyze phase C): its own folder for 2+ parts, else beside the main note."""
    vault = Path(vault)
    d = (vault / main_rel).parent
    work = VN.work_of(Path(main_rel).stem)
    home = d.parent if _names_work(d.name, work) else d
    if n_parts < 2:
        return home
    try:                                   # an existing (possibly shortened) folder for this work
        for c in sorted(home.iterdir()):
            if c.is_dir() and _names_work(c.name, work):
                return c
    except OSError:
        pass
    rel = home.relative_to(vault).as_posix() if home != vault else ""
    return home / work_folder(rel, work, len(work) + len(" (Summary and original)"))


_CACHE = {}


def family_rels(vault, rel):
    """rel (a main note) plus every note of its family, vault-relative (vault scanned at most once a minute)."""
    import time
    hit = _CACHE.get(str(vault))
    if not hit or time.time() - hit[0] > 60:
        hit = _CACHE[str(vault)] = (time.time(), scan(vault))
    notes = hit[1]
    name = Path(rel).stem
    f = families(notes).get(name)
    if not f:
        return [rel]
    return [rel] + [notes[m]["rel"] for m in members(f) if m != name and m in notes]


def _see_also_updates(notes, f):
    """{name: new see_also list} for members whose see_also lacks a family link."""
    allm = members(f) + f["same"]
    core = [x for x in [f["main"], f["analysis"], f["transcript"]] if x]
    out = {}
    for m in allm:
        if m not in notes:
            continue
        want = [x for x in (allm if m in core or m in f["same"] else core + f["same"]) if x != m]
        cur = notes[m]["fm"].get("see_also")
        cur_list = list(cur) if isinstance(cur, list) else ([cur] if cur else [])
        have = {x.lower() for x in _names(cur_list)}
        add = [f"[[{x}]]" for x in want if x.lower() not in have]
        if add:
            out[m] = cur_list + add
    return out


def tag_home(vault, d):
    """The folder that names the folder tag for notes in d: d itself, or the first folder above it that isn't
    a work folder."""
    d = str(d or "").replace("\\", "/")
    while "/" in d and is_work_dir(Path(vault) / d):
        d = d.rsplit("/", 1)[0]
    return d


def _shorten_parts(cfg, vault, notes, f, target, root_len, log):
    """Part notes whose path in target would be too long for Windows lose their chapter title
    ('<work> (Part 03 Analysis) - <chapter>' -> '<work> (Part 03 Analysis)'); links follow (vault_names)."""
    moves = []
    for m in f["parts"]:
        if root_len + len(target) + 1 + len(m) + 3 <= PATH_LIMIT:
            continue
        bare = VN.part_name(f["main"], _part_no(notes[m]), "")
        if bare != m and bare not in notes and root_len + len(target) + 1 + len(bare) + 3 <= PATH_LIMIT:
            rel = notes[m]["rel"]
            moves.append((rel, (rel.rsplit("/", 1)[0] + "/" if "/" in rel else "") + bare + ".md", m, bare))
    if moves:
        VN.rename_notes(cfg, vault, moves)
        log(f"families: {len(moves)} part note name(s) shortened for {f['work']}")
    return len(moves)


def _remove_shells(vault, old_dirs, rows):
    """Remove emptied work folders (only a generated index.md left), adding a report row for each."""
    for d in work_dirs(vault):        # also sweep shells left by an earlier run whose clean-up failed (2026-10-14)
        try:
            if all(c.name == "index.md" for c in (vault / d).iterdir()):
                old_dirs.add(d)
        except OSError:
            pass
    for d in sorted(old_dirs, key=len, reverse=True):     # emptied work folders: only a generated index.md left
        p = vault / d
        try:
            left = [c for c in p.iterdir()] if p.is_dir() else None
        except OSError:
            continue
        if left is None or _top(d) in SKIP_TOP or "/" not in d:
            continue
        if all(c.name == "index.md" for c in left):
            idx = p / "index.md"
            if idx.exists() and "generated by vault-librarian" not in idx.read_text(encoding="utf-8", errors="replace")[:300]:
                continue
            try:
                if idx.exists():
                    idx.unlink()
                try:
                    p.rmdir()
                except OSError:
                    import time
                    time.sleep(3)
                    p.rmdir()
                rows.append(f"removed the empty folder {d}")
            except OSError as e:
                rows.append(f"couldn't remove {d}: {e}")


def regroup(cfg, log=print, dry=False, only=None, vault=None):
    """Move each family to its place and add the family see_also links. only: main-note names (or vault paths)
    to limit to (e.g. the notes just filed). Returns report rows (plain text)."""
    vault = Path(vault or cfg["vault_path"])
    notes = scan(vault)
    fams = families(notes)
    if only is not None:
        want = {Path(str(x)).stem.lower() for x in only}
        fams = {k: f for k, f in fams.items() if any(m.lower() in want for m in members(f))}
    root_len = len(str(cfg.get("vault_path") or vault)) + 1
    if not dry:                                   # shorten over-long part names first, then look again
        n_short = 0
        for main, f in sorted(fams.items()):
            n_short += _shorten_parts(cfg, vault, notes, f, target_of(notes, f, root_len), root_len, log)
        if n_short:
            notes = scan(vault)
            fams2 = families(notes)
            fams = {k: fams2[k] for k in fams if k in fams2}
    items, rows, old_dirs, too_long = [], [], set(), []
    for main, f in sorted(fams.items()):
        home, target = home_of(notes, f), target_of(notes, f, root_len)
        mv = {}
        for m in members(f):
            cur = os.path.dirname(notes[m]["rel"]).replace("\\", "/")
            if cur != target:
                mv[m] = cur
        if any(root_len + len(target) + 1 + len(m) + 3 > PATH_LIMIT for m in mv):
            too_long.append(main)
            mv = {}
        sa = _see_also_updates(notes, f)
        for m in sorted(set(mv) | set(sa)):
            it = {"path": notes[m]["rel"]}
            if m in mv:
                it["move_to"] = target
                th = tag_home(vault, home)
                if th != target:
                    it["tag_folder"] = th
                old_dirs.add(mv[m])
            if m in sa:
                it["set"] = {"see_also": sa[m]}
                fm = notes[m]["fm"]
                if "my_note" in fm:            # keep the owner's two properties last, see_also then my_note
                    it["set"]["my_note"] = fm["my_note"] if fm["my_note"] is not None else ""
            items.append(it)
        if mv:
            rows.append(f"{f['work']}: {len(mv)} note(s) moved to {target}")
        if sa:
            rows.append(f"{f['work']}: see_also links added to {len(sa)} note(s)")
    for main in too_long:
        rows.append(f"{fams[main]['work']}: not moved (the path would be too long for Windows); left where it is")
    if dry or not items:
        if dry:
            log(f"families: would change {len(items)} note(s)")
        else:
            _remove_shells(vault, old_dirs, rows)
        return rows
    work = Path(cfg["local_root"]) / "work"
    work.mkdir(parents=True, exist_ok=True)
    pj = work / "families-plan.json"
    pj.write_text(json.dumps({"actor": "claude/families", "log": "Notes about one work kept together and cross-linked",
                              "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    rc, out = C.run(log, C.VL / "apply_plan.py", vault, pj)
    skipped = [ln[8:] for ln in (out or "").splitlines() if ln.startswith("SKIPPED ")]
    rows += [f"skipped: {s}" for s in skipped[:20]]
    _remove_shells(vault, old_dirs, rows)
    log(f"families: {len(items)} note(s) changed in {len(fams)} work(s); {len(skipped)} skipped")
    return rows


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Keep each work's notes together and cross-linked")
    ap.add_argument("--vault")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the families found")
    x = ap.parse_args()
    cfg = C.load_config() if hasattr(C, "load_config") else {}
    if x.vault:
        cfg["vault_path"] = x.vault
    if x.list:
        ns = scan(Path(cfg["vault_path"]))
        for k, f in sorted(families(ns).items()):
            print(f"{k}: target {target_of(ns, f)}; analysis={f['analysis']}; parts={len(f['parts'])}; "
                  f"transcript={f['transcript']}; same={f['same']}")
    else:
        for r in regroup(cfg, print, dry=x.dry_run):
            print(r)
