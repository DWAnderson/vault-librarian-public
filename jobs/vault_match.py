"""Is this already in the vault? Checked before any token-intensive route (book, summary) spends anything (2026-09-28): a likely match is put to the owner in the daily email first.

  find(cfg, title, authors=(), sha=None, size=None, file_name=None) -> [{"note", "why", "score"}], best first
Matches:
  - the identical file in attachments/Sources/ (SHA-256; only files of the same size are hashed)
  - a note whose title or name is the same or nearly so (difflib >= 0.88, or one contains the other,
    subtitles ignored), boosted when an author's surname also appears in the note's author field
  - a note whose source_file is the same file name
"""
import difflib
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

sys.path.insert(0, str(C.VL))
from vaultlib import junk_title, split_frontmatter  # noqa: E402

SKIP = {".obsidian", ".trash", ".git", ".smart-env", ".sync-backups", "_snapshots", "node_modules", "Templates"}
_cache = {}


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"\.(pdf|docx?|epub|mobi|azw3?|txt|html?)$", "", s)
    s = re.sub(r"^\d{1,3}[_ .-]+(?=[a-z])", "", s)      # dropbox numbering: "61_Effendi_wars"
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"^(the|a|an)\s+", "", " ".join(s.split()))
    return s


def main_title(s):
    return norm(re.split(r"\s*[:—]\s*|\s+-\s+", s or "", maxsplit=1)[0])


def _index(vault):
    key = str(vault)
    if key in _cache:
        return _cache[key]
    out = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
        for f in files:
            if not f.endswith(".md") or f in ("index.md", "log.md"):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4000)
                fm, _, _ = split_frontmatter(head if head.count("---") >= 2 else "")
            except Exception:
                fm = None
            fm = fm or {}
            au = fm.get("author") or []
            out.append({"rel": os.path.relpath(p, vault).replace(os.sep, "/"), "name": f[:-3],
                        "title": str(fm.get("title") or ""), "type": str(fm.get("type") or ""),
                        "authors": " ".join(au if isinstance(au, list) else [str(au)]).lower(),
                        "source_file": str(fm.get("source_file") or "")})
    _cache[key] = out
    return out


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find(cfg, title, authors=(), sha=None, size=None, file_name=None):
    vault = Path(cfg["vault_path"])
    hits = {}

    def add(note, why, score):
        if note not in hits or hits[note]["score"] < score:
            hits[note] = {"note": note, "why": why, "score": score}

    src_rel = C.vrel(vault, "attachments") + "/Sources"
    src = vault / src_rel
    if sha and size and src.is_dir():
        for f in src.iterdir():
            try:
                if f.is_file() and f.stat().st_size == size and _sha(f) == sha:
                    add(f"{src_rel}/{f.name}", "the identical file is already in the vault", 1.0)
            except OSError:
                pass
    # A meaningless metadata title ("INTRODUCTION", "No Job Name") is not matched on; the file's own name is
    # (2026-10-01: a 131-page book whose PDF title was "INTRODUCTION" was matched to "Introduction to Obsidian
    # Web Clipper"). Containment also needs at least two words now, so one generic word can't match a title.
    if junk_title(title) and file_name:
        title = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", file_name)
    if junk_title(title):
        title = ""
    t_full, t_main = norm(title), main_title(title)
    surnames = [a.split()[-1].lower() for a in (authors or []) if a and a.split()]
    fn = (file_name or "").lower()
    for e in _index(vault):
        if fn and e["source_file"].lower() == fn:
            add(e["rel"], f"a note made from a file with the same name ({file_name})", 0.95)
        if len(t_main) < 8 or e["type"] in ("Analysis", "Part Analysis", "Transcript", "Concept"):
            continue      # point at the work's own note, not its analysis (2026-10-13)
        for cand in {e["title"], e["name"]}:
            if not cand or junk_title(cand):
                continue
            c_full, c_main = norm(cand), main_title(cand)
            if len(c_main) < 8:
                continue
            r = max(difflib.SequenceMatcher(None, t_full, c_full).ratio(),
                    difflib.SequenceMatcher(None, t_main, c_main).ratio())
            contains = (min(len(t_main), len(c_main)) >= 12 and min(len(t_main.split()), len(c_main.split())) >= 2
                        and (t_main in c_full or c_main in t_full))
            if r >= 0.88 or contains:
                score = max(r, 0.9 if contains else 0) + (0.05 if any(s in e["authors"] for s in surnames) else 0)
                add(e["rel"], f"a note with {'the same' if r > 0.97 else 'a very similar'} title"
                    + (" and author" if any(s in e["authors"] for s in surnames) else "")
                    + (f" ({e['type']})" if e["type"] else ""), round(min(score, 0.99), 2))
    return sorted(hits.values(), key=lambda h: -h["score"])[:3]
