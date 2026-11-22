"""The owner's one-line take (`my_note`) and `see_also` on ingested notes (2026-10-01).

- Every source item being ingested ends its frontmatter with `see_also` and `my_note` (empty if new);
  `ensure_items()` adds them to an organize plan's items, keeping any values already there.
- The answer page has an "Add a note" button on every item asked about whose `my_note` is empty, and lists
  the items ingested since the night before that weren't asked about, with the same button.
  A note on a vault note is written at once (`set_my_note`); a note on an item not yet in the vault
  (an intake-queue or held file) is kept on the queue item and written into the note when it is filed
  (`queue_run.py` -> `apply_pending`).
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

sys.path.insert(0, str(C.VL))
from vaultlib import split_frontmatter  # noqa: E402

# Types that never get the owner's two properties (same list as file_to_note.py OWNER_PROPS_SKIP)
SKIP_TYPES = {"Transcript", "Podcast Transcript", "Analysis", "Part Analysis", "Concept", "Topic Map",
              "Tag Vocabulary", "Agent Instructions", "Daily Note"}
SKIP_DIRS = {".obsidian", ".trash", ".git", ".smart-env", ".sync-backups", ".analysis-backups", ".stversions",
             "_snapshots", "node_modules"}
SKIP_TOP = ("_Obsidian Internal/", "Claude/", "Concepts/", "Daily Notes/")
MAX_LEN = 300


def clean(text):
    """One line, at most MAX_LEN characters."""
    return " ".join(str(text or "").split())[:MAX_LEN]


def read_fm(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(6000)
        fm = split_frontmatter(head if head.count("---") >= 2 else "")[0]
    except Exception:  # noqa: BLE001
        fm = None
    return fm if isinstance(fm, dict) else {}


def wants_props(fm):
    """A source item (not the owner's own note, not a companion or navigation note)."""
    g = fm.get("generated") if isinstance(fm.get("generated"), dict) else {}
    return str(fm.get("type") or "") not in SKIP_TYPES and not str(g.get("by") or "").startswith("human:")


def my_note_of(path):
    return clean(read_fm(path).get("my_note") or "")


def ensure_items(vault, plan_items):
    """Add see_also / my_note (existing values kept, so they stay last) to every plan item that should have them."""
    for it in plan_items:
        fm = read_fm(Path(vault) / it["path"])
        if not fm or not wants_props(fm):
            continue
        s = it.setdefault("set", {})
        sa = fm.get("see_also")
        s.pop("see_also", None)
        s.pop("my_note", None)
        s["see_also"] = sa if isinstance(sa, list) else ([sa] if sa else [])
        s["my_note"] = clean(fm.get("my_note") or "")


def set_my_note(cfg, log, rel, text):
    """Write `my_note` (and an empty `see_also` if missing) on one vault note via apply_plan.py (logged in log.md).
    The caller holds the vault lock. Returns (ok, message)."""
    vault = Path(cfg["vault_path"])
    p = vault / rel
    try:
        if not (p.is_file() and vault.resolve() in p.resolve().parents and p.suffix.lower() == ".md"):
            return False, "note not found"
    except OSError:
        return False, "note not found"
    fm = read_fm(p)
    sa = fm.get("see_also")
    upd = {"see_also": sa if isinstance(sa, list) else ([sa] if sa else []), "my_note": clean(text)}
    work = Path(cfg["local_root"]) / "work"
    work.mkdir(parents=True, exist_ok=True)
    pj = work / f"my-note-{os.getpid()}-{abs(hash(rel)) % 10**8}.json"
    pj.write_text(json.dumps({"actor": C.owner(cfg)["id"], "log": "Your note (my_note) added from the answer page",
                              "items": [{"path": rel, "set": upd}]}, ensure_ascii=False), encoding="utf-8")
    rc, out = C.run(log, C.VL / "apply_plan.py", vault, pj)
    try:
        pj.unlink()
    except OSError:
        pass
    return (rc == 0 and my_note_of(p) == clean(text)), (out.strip().splitlines() or [""])[-1]


def apply_pending(cfg, log, item, notes):
    """After an item is filed: write the my_note the owner added on the answer page into its new note(s)."""
    text = clean(item.get("my_note") or "")
    if not text:
        return
    for rel in notes or []:
        if str(rel).lower().endswith(".md"):
            ok, msg = set_my_note(cfg, log, rel, text)
            log(f"my_note on {rel}: {'written' if ok else 'NOT written: ' + msg}")


def fresh_notes(cfg, since, exclude=()):
    """Source notes ingested since `since` (generated.at, else file time) whose my_note is empty, oldest first."""
    vault = Path(cfg["vault_path"])
    out = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if not f.endswith(".md") or f in ("index.md", "log.md", "CLAUDE.md"):
                continue
            ab = Path(root) / f
            rel = ab.relative_to(vault).as_posix()
            if rel in exclude or rel.startswith(SKIP_TOP):
                continue
            try:
                if dt.datetime.fromtimestamp(ab.stat().st_mtime).astimezone() < since:
                    continue   # not touched since: can't be new
            except OSError:
                continue
            fm = read_fm(ab)
            if not fm or not wants_props(fm) or clean(fm.get("my_note") or ""):
                continue
            g = fm.get("generated") if isinstance(fm.get("generated"), dict) else {}
            try:
                at = dt.datetime.fromisoformat(str(g.get("at")))
                at = at if at.tzinfo else at.astimezone()
            except (TypeError, ValueError):
                continue
            if at >= since:
                out.append({"path": rel, "name": ab.stem, "type": str(fm.get("type") or ""), "at": at.isoformat()})
    return sorted(out, key=lambda x: x["at"])
