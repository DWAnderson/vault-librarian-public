"""Note names that tell a summary or analysis apart from the work itself (2026-10-10).

    <work> (Summary)                         Book / Video / Podcast / Web Summary, or content: summary
    <work> (Summary and original)            the same, when the note itself embeds the original file under
                                             "## Original" (2026-10-13)
    <work> (Analysis)                        Analysis (was "<work> - Analysis")
    <work> (Transcript)                      Transcript (was "<work> - Transcript")
    <work> (Part 03 Analysis) - <chapter>    Part Analysis (was "<work> - Part 03 - <chapter>"); the chapter
                                             title is kept only when it says something ("Part 3", "2",
                                             file-name-like titles are dropped)

<work> keeps the existing "Title (Author year)" / "Title (Channel)" convention, so every note about one work
sorts together. Lookups accept the old forms too, so nothing breaks while old names remain.

enforce(cfg, log, dry) renames notes outside Inbox/Clippings to these names, rewrites every [[link]] to them,
keeps the old name in `aliases`, refreshes Analysis notes' notes_hash when only link text changed (so no
analysis is redone), and records each rename in queue/renames.jsonl (follow() reads it).
Run by nightly.py and at the end of intake runs. Duplicated in spirit by vault-librarian's skill docs.
"""
import json
import os
import re
from pathlib import Path

SUMMARY_TYPES = {"Book Summary", "Video Summary", "Podcast Summary", "Web Summary"}
TRANSCRIPT_TYPES = {"Transcript", "Podcast Transcript"}
SKIP_TOP = {"Inbox", "Clippings", "_Obsidian Internal", "Archives", "Templates", "Claude"}
SKIP_DIRS = {".obsidian", ".trash", ".sync-backups", ".analysis-backups", ".smart-env", ".git"}

_TAIL = re.compile(r"(?: \((?:Summary and original|Summary|Analysis|Transcript)\)| - (?:Analysis|Transcript))$")
_ORIGINAL = re.compile(r"(?ms)^## Original[ \t]*\n\s*!\[\[")
_PART_OLD = re.compile(r"^(?P<b>.*?) - Part (?P<n>\d+)(?: - (?P<t>.*))?$")
_PART_NEW = re.compile(r"^(?P<b>.*?) \(Part (?P<n>\d+) Analysis\)(?: - (?P<t>.*))?$")


def base(name):
    """The work's own name: suffixes for summary/analysis/transcript removed."""
    name = str(name or "").strip()
    for _ in range(2):
        name = _TAIL.sub("", name).strip()
    return name


def summary_name(name, original=False):
    return f"{base(name)} (Summary and original)" if original else f"{base(name)} (Summary)"


def summary_names(name):
    """Both summary forms, the embedded-original one first."""
    return [summary_name(name, True), summary_name(name)]


def has_original(text):
    """The note embeds its original file (![[...pdf]]) under a '## Original' heading."""
    return bool(_ORIGINAL.search(text or ""))


def work_of(name):
    """The work a note belongs to: suffixes and ' (Part NN Analysis) - ...' removed."""
    n = str(name or "").strip()
    m = _PART_NEW.match(n) or _PART_OLD.match(n)
    return base(m.group("b") if m else n)


def analysis_name(name):
    return f"{base(name)} (Analysis)"


def transcript_name(name):
    return f"{base(name)} (Transcript)"


def analysis_names(name):
    """New form first, then the old one."""
    return [analysis_name(name), f"{base(name)} - Analysis"]


def transcript_names(name):
    return [transcript_name(name), f"{base(name)} - Transcript"]


def is_derived(name):
    """An analysis, part analysis or transcript (either naming form) - never itself an item to analyze."""
    n = str(name or "")
    return bool(re.search(r"(?: \((?:Analysis|Transcript)\)| - (?:Analysis|Transcript))$", n)
                or _PART_NEW.match(n))


def new_forms(name):
    """Names an old-style note name may have now (for finding a note by an old link or path)."""
    n = str(name or "")
    if n.endswith(" - Analysis"):
        return [analysis_name(n)]
    if n.endswith(" - Transcript"):
        return [transcript_name(n)]
    m = _PART_OLD.match(n)
    if m:
        return [part_name(m.group("b"), m.group("n"), m.group("t") or "")]
    if n.endswith(" (Summary)"):
        return [summary_name(n, True)]
    if is_derived(n) or n.endswith(" (Summary and original)"):
        return []
    return summary_names(n)


def find(notes, names):
    for n in names:
        if n in notes:
            return notes[n]
    return None


def _clean(t):
    return re.sub(r"\s+", " ", re.sub(r'[\[\]#^|\\/:*?"<>]', "", str(t or ""))).strip(" .;-")


def meaningful_part_title(t, work=""):
    t = _clean(t)
    if not t or "_" in t:
        return ""
    core = re.sub(r"(?i)\b(part|chapter|chap|ch|section|sec|pt)\b|\(part \d+\)|[\d.;:,()\s-]", "", t)
    if len(core) < 3:
        return ""
    if work and _clean(t).lower().rstrip(" 0123456789;") == _clean(work).lower():
        return ""
    return t


def part_name(item_name, i, title):
    b = base(item_name)
    t = meaningful_part_title(title, b)[:60].strip()
    return f"{b} (Part {int(i):02d} Analysis)" + (f" - {t}" if t else "")


def part_names(item_name, i, title):
    """New form, then the old one (old: '<item> - Part 03 - <title>')."""
    t = _clean(title)[:60].strip() or f"Part {i}"
    return [part_name(item_name, i, title), f"{item_name} - Part {int(i):02d} - {t}"]


def part_label(name):
    """Short label for a Parts list: the chapter title, or 'Part 3'."""
    m = _PART_NEW.match(name) or _PART_OLD.match(name)
    if not m:
        return name
    return m.group("t") or f"Part {int(m.group('n'))}"


def kind(fm):
    t = str((fm or {}).get("type") or "")
    if t == "Analysis":
        return "analysis"
    if t == "Part Analysis":
        return "part"
    if t in TRANSCRIPT_TYPES:
        return "transcript"
    if t in SUMMARY_TYPES or str((fm or {}).get("content") or "").lower() == "summary":
        return "summary"
    return None


def expected(fm, name, text=None):
    """The name this note should have, or None when it is fine / not covered. text: the whole note, so a
    summary that embeds its original gets "(Summary and original)" (2026-10-13)."""
    k = kind(fm)
    if k == "summary":
        want = summary_name(name, has_original(text))
    elif k == "analysis":
        want = analysis_name(name)
    elif k == "transcript":
        want = transcript_name(name)
    elif k == "part":
        m = _PART_NEW.match(name) or _PART_OLD.match(name)
        if not m:
            return None
        want = part_name(m.group("b"), m.group("n"), m.group("t") or "")
    else:
        return None
    return want if want != name else None


# ------------------------------------------------------------------ rename ledger
def ledger_path(cfg=None):
    return Path(__file__).resolve().parent.parent / "queue" / "renames.jsonl"


def follow(rel, cfg=None):
    """Where a note that was renamed by enforce() is now (rel path), following chains; rel if never renamed."""
    try:
        moves = {}
        for ln in ledger_path(cfg).read_text(encoding="utf-8").splitlines():
            if ln.strip():
                x = json.loads(ln)
                moves[x["old"]] = x["new"]
    except (OSError, ValueError):
        return rel
    seen = set()
    while rel in moves and rel not in seen:
        seen.add(rel)
        rel = moves[rel]
    return rel


# ------------------------------------------------------------------ enforce
WIKI = re.compile(r"(!?\[\[)([^\]\|#\n]+)((?:#[^\]\|\n]*)?)((?:\|[^\]\n]*)?)(\]\])")


def _walk(vault):
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".md"):
                p = Path(root) / f
                yield p.relative_to(vault).as_posix(), p


def _fm(text):
    import yaml
    if not text.startswith("---"):
        return {}, None
    end = text.find("\n---", 3)
    if end < 0:
        return {}, None
    try:
        fm = yaml.safe_load(text[3:end]) or {}
    except Exception:  # noqa: BLE001
        return None, None
    return (fm if isinstance(fm, dict) else {}), end


def plan(vault):
    """[(old_rel, new_rel, old_name, new_name)] for notes outside Inbox/Clippings whose name should change."""
    vault = Path(vault)
    names = {}
    found = []
    for rel, p in _walk(vault):
        names.setdefault(p.stem.lower(), []).append(rel)
        if rel.split("/")[0] in SKIP_TOP:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            fm, _ = _fm(text[:6000])
        except OSError:
            continue
        if not fm:
            continue
        want = expected(fm, p.stem, text)
        if want:
            found.append((rel, p.stem, want))
    out, taken = [], set()
    for rel, old, want in found:
        if rel.split("/")[0] in SKIP_TOP:
            continue
        limit = max(len(rel), 200)              # Windows paths: never make a path longer than this
        folder = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
        over = len(folder + want + ".md") - limit
        if over > 0:
            m = _PART_NEW.match(want)
            if m and m.group("t"):
                t = m.group("t")[:max(0, len(m.group("t")) - over)].rstrip(" .;-")
                want = f"{m.group('b')} (Part {m.group('n')} Analysis)" + (f" - {t}" if len(t) >= 8 else "")
        if names.get(want.lower()) or want.lower() in taken:
            continue            # a note by that name already exists: left for the owner (reported)
        taken.add(want.lower())
        new_rel = folder + want + ".md"
        out.append((rel, new_rel, old, want))
    return out


# 2026-10-19: link rewrites (renames, corrections, duplicate merges) changed analysis bodies without updating
# analysis_hash, so later redos thought the owner had edited them (678 of 904 analyses failed the check).
_FM_BLOCK = re.compile(r"(?s)^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)")   # as vaultlib.split_frontmatter
_AN_HASH = re.compile(r"(?m)^(analysis_hash:[ \t]*['\"]?)([0-9a-f]{16})(['\"]?[ \t]*)$")


def _body_hash(body):
    import hashlib
    return hashlib.sha1(body.strip().encode("utf-8")).hexdigest()[:16]   # = analyze._hash(body.strip())


def keep_analysis_hash(old, new):
    """If `old` is an analysis whose analysis_hash still matched its body (not edited by hand), give `new` the
    hash of its own body, so a machine change doesn't look like a hand edit. Otherwise `new` is unchanged."""
    if old == new:
        return new
    mo, mn = _FM_BLOCK.match(old), _FM_BLOCK.match(new)
    if not mo or not mn:
        return new
    h = _AN_HASH.search(mo.group(1))
    if not h or h.group(2) != _body_hash(old[mo.end():]):
        return new
    nh = _body_hash(new[mn.end():])
    fm2 = _AN_HASH.sub(lambda m: m.group(1) + nh + m.group(3), mn.group(1), count=1)
    return new[:mn.start(1)] + fm2 + new[mn.end(1):]


def rewrite_links(text, mapping):
    """[[Old]] / [[folder/Old]] / ![[Old#h|x]] -> new name; mapping keys are lower-case old names.
    An unedited analysis keeps a matching analysis_hash (keep_analysis_hash, 2026-10-19)."""
    def sub(m):
        tgt = m.group(2).strip()
        stem = tgt.rsplit("/", 1)[-1]
        stem_nomd = stem[:-3] if stem.lower().endswith(".md") else stem
        new = mapping.get(stem_nomd.lower())
        if not new and "''" in stem_nomd:       # inside a single-quoted YAML value ' is written ''
            new = mapping.get(stem_nomd.replace("''", "'").lower())
            new = new.replace("'", "''") if new else None
        if not new:
            return m.group(0)
        prefix = tgt[: len(tgt) - len(stem)]
        return f"{m.group(1)}{prefix}{new}{'.md' if stem != stem_nomd else ''}{m.group(3)}{m.group(4)}{m.group(5)}"
    return keep_analysis_hash(text, WIKI.sub(sub, text))


def _add_alias(text, old):
    """Add `old` to the aliases list in the frontmatter (block or flow style); text unchanged if no frontmatter."""
    import yaml
    fm, end = _fm(text)
    if fm is None or end is None:
        return text
    al = fm.get("aliases") or []
    al = list(al) if isinstance(al, list) else [al]
    if old in al:
        return text
    head, rest = text[3:end], text[end:]
    line = "aliases: " + json.dumps(al + [old], ensure_ascii=False)
    if re.search(r"(?m)^aliases:", head):
        head = re.sub(r"(?ms)^aliases:.*?(?=^\S|\Z)", line + "\n", head, count=1)
    else:
        # after title when present (keeps see_also/my_note last)
        if re.search(r"(?m)^title:.*$", head):
            head = re.sub(r"(?m)^(title:.*)$", lambda m: m.group(1) + "\n" + line, head, count=1)
        else:
            head = head.rstrip("\n") + "\n" + line + "\n"
    return "---" + head + rest


def rename_notes(cfg, vault, moves):
    """Rename notes [(old_rel, new_rel, old_name, new_name)]: rewrite [[links]] everywhere, keep the old name in
    `aliases`, record each in queue/renames.jsonl (so corrections.py knows Claude did it). -> (rows, files changed)"""
    import datetime as dt
    vault = Path(vault)
    # 1. links everywhere
    mapping = {old.lower(): new for _, _, old, new in moves}
    changed = 0
    for rel, p in _walk(vault):
        try:
            t = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "[[" not in t:
            continue
        nt = rewrite_links(t, mapping)
        if nt != t:
            p.write_text(nt, encoding="utf-8", newline="\n")
            changed += 1
    # 2. rename + alias
    led = ledger_path(cfg)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    with open(led, "a", encoding="utf-8") as lf:
        for old_rel, new_rel, old, new in moves:
            src, dst = vault / old_rel, vault / new_rel
            if not src.exists() or dst.exists():
                continue
            t = src.read_text(encoding="utf-8")
            t2 = _add_alias(t, old)
            if t2 != t:
                src.write_text(t2, encoding="utf-8", newline="\n")
            os.rename(src, dst)
            lf.write(json.dumps({"at": now, "old": old_rel, "new": new_rel}, ensure_ascii=False) + "\n")
            rows.append((old_rel, new_rel))
    # 3. concept register: names of the item a pending concept came from
    try:
        cj = led.parent / "concepts.json"
        s = cj.read_text(encoding="utf-8")
        d = json.loads(s)
        hit = False
        for v in (d.get("pending") or {}).values():
            if isinstance(v, dict) and str(v.get("first_from") or "").lower() in mapping:
                v["first_from"] = mapping[str(v["first_from"]).lower()]
                hit = True
        if hit:
            cj.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return rows, changed


def enforce(cfg, log, dry=False, vault=None, hashes=True):
    """Rename to the suffix convention. Returns report rows [(old, new)]."""
    import datetime as dt
    vault = Path(vault or cfg["vault_path"])
    moves = plan(vault)
    if not moves:
        return []
    if dry:
        return [(o, n) for o, n, _, _ in moves]
    mapping = {old.lower(): new for _, _, old, new in moves}
    # analyses whose notes_hash is current before the rename (refreshed after, so link text alone redoes nothing)
    current = []
    if hashes:
        try:
            import analyze as AZ
            a = AZ.acfg(cfg)
            notes = AZ.scan(vault)
            for n in notes.values():
                if is_derived(n["name"]) or n["type"] in {"Analysis", "Part Analysis", "Concept"} | TRANSCRIPT_TYPES:
                    continue
                an = find(notes, analysis_names(n["name"]))
                if an and an["type"] == "Analysis" and an["fm"].get("notes_hash"):
                    if AZ.my_notes(vault, notes, n, a)[2] == str(an["fm"]["notes_hash"]):
                        current.append((n["name"], an["name"]))
        except Exception as e:  # noqa: BLE001
            log(f"vault_names: couldn't check analysis hashes ({type(e).__name__}: {e}); continuing")
            current = []
    rows, changed = rename_notes(cfg, vault, moves)
    # 4. refresh notes_hash on analyses that were current
    if current:
        try:
            import analyze as AZ
            a = AZ.acfg(cfg)
            notes = AZ.scan(vault)
            fixed = 0
            for item_name, an_name in current:
                item_name = mapping.get(item_name.lower(), item_name)
                an_name = mapping.get(an_name.lower(), an_name)
                n, an = notes.get(item_name), notes.get(an_name)
                if not n or not an:
                    continue
                h = AZ.my_notes(vault, notes, n, a)[2]
                if h and h != str(an["fm"].get("notes_hash")):
                    p = vault / an["rel"]
                    t = p.read_text(encoding="utf-8")
                    t2 = re.sub(r"(?m)^notes_hash:.*$", f"notes_hash: {h}", t, count=1)
                    if t2 != t:
                        p.write_text(t2, encoding="utf-8", newline="\n")
                        fixed += 1
            log(f"vault_names: refreshed notes_hash on {fixed} analyses")
        except Exception as e:  # noqa: BLE001
            log(f"vault_names: notes_hash refresh failed ({type(e).__name__}: {e})")
    log(f"vault_names: renamed {len(rows)} notes; links updated in {changed} files")
    return rows


if __name__ == "__main__":
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault")
    ap.add_argument("--dry-run", action="store_true")
    x = ap.parse_args()
    import common as C
    cfg = C.load_config() if hasattr(C, "load_config") else json.loads(
        (Path(__file__).resolve().parent.parent / "config" / "vault.json").read_text(encoding="utf-8"))
    if x.vault:
        cfg["vault_path"] = x.vault
    for o, n in enforce(cfg, print, dry=x.dry_run):
        print(f"{o}  ->  {n}")
