#!/usr/bin/env python3
"""Vault intake queue: one record per item, shared by every way content reaches the vault.

Plan: 2026-09-28 Intake queue for files and email - plan.md (section 3).

Records: queue\\items\\<id>.json in this synced app folder, one small file per item, so this PC
and a laptop session can edit different items without OneDrive conflict copies.
Payloads (captured email, attachments, extracted text, cached model answers):
%LOCALAPPDATA%\\VaultLibrarian\\queue\\payload\\<id>\\ (this PC only).

This module is the only code that writes item records. Command line (manual control):

  python vqueue.py list [--status held] [--route book] [--all]
  python vqueue.py show <id>              full record, and why it has its priority
  python vqueue.py top <id>               do it next (manual priority 1000)
  python vqueue.py priority <id> <n>      manual priority; "priority <id> clear" returns it to the rules
  python vqueue.py hold <id> [why]        not now (kept out of processing until released)
  python vqueue.py release <id>           undo hold; also releases an item held for you/confirm
  python vqueue.py route <id> <route>     handle differently (email link article book recipe manual document summary auto)
  python vqueue.py skip <id> | cancel <id> | retry <id>
  python vqueue.py resolved <id> <note>   a laptop session saved it by hand (e.g. a browser-only link)
  python vqueue.py rules                  the ordering rules in force
  python vqueue.py analyze <target>... [--redo] [--force]
                                          queue a concept analysis (plan: 2026-09-28 Concept analysis and links);
                                          target: a vault path or note name, folder:<path>, tag:<tag>, concept:<name>;
                                          --redo replaces an existing analysis (kept if verified or hand-edited
                                          unless --force; the old one is backed up in .analysis-backups)
<id> may be any unique part of an item id.
"""
import argparse
import datetime as dt
import json
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

STATUSES = ("new", "extracted", "ready", "working", "waiting", "held", "failed",
            "duplicate", "done", "skipped", "cancelled")
OPEN = ("new", "extracted", "ready", "working", "waiting", "held", "failed")
FINAL = ("done", "duplicate", "skipped", "cancelled")
ROUTES = ("email", "link", "container", "article", "book", "recipe", "manual", "document", "summary", "auto", "analyze", "analyze_long")
COSTLY = ("book", "summary", "analyze_long")   # analyze_long: phase C, night only (2026-10-06)
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

DEFAULTS = {
    "enabled": False,
    "files_enabled": False,
    "dropbox_path": "",
    "route_folders": {"Articles": "article", "Books": "book", "Recipes": "recipe", "Manuals": "manual",
                      "Full text": "document", "Summarize": "summary", "Other": "auto"},
    "rules": [
        {"match": {"source": "mail"}, "priority": 100, "why": "Remember email first (2026-09-28)"},
        {"match": {"route": "book"}, "priority": 10, "why": "books last"},
        {"match": {}, "priority": 50, "why": "default"},
    ],
    "per_run": 20,
    "claude_calls_per_day": 60,
    "books_per_night": 1,
    "confirm_over_week_pct": 2,
    "night_window": ["00:30", "01:55"],
    "backends": {"default": "claude-cli"},
    "attachment_email_max_words": 30,
    "skip_images_under_kb": 20,
    "max_attempts": 3,
    "settle_minutes": 3,
    "extract_per_run": 10,
    "confidential_markers": ["privileged & confidential", "privileged and confidential", "attorney-client privilege",
                             "attorney client privilege", "attorney work product"],   # add firm names in config
}


def qcfg(cfg):
    """The queue settings, with defaults for anything config\\vault.json leaves out."""
    q = dict(DEFAULTS)
    q.update(cfg.get("queue") or {})
    return q


def atomic_write(path, text):
    """Write via a temp file and rename. Windows can briefly refuse the rename (antivirus, indexing,
    OneDrive syncing the folder): retry for a few seconds, then write in place."""
    import time
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(25):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2)
    path.write_text(text, encoding="utf-8")
    try:
        tmp.unlink()
    except OSError:
        pass


def stamp(cfg=None):
    return C.now(cfg).isoformat(timespec="seconds")


class Store:
    def __init__(self, cfg, items_dir=None, payload_dir=None):
        self.cfg = cfg
        self.dir = Path(items_dir) if items_dir else C.APP / "queue" / "items"
        self.payload_root = Path(payload_dir) if payload_dir else Path(cfg["local_root"]) / "queue" / "payload"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache = None

    # ---- records
    def path(self, iid):
        return self.dir / f"{iid}.json"

    def payload(self, iid):
        p = self.payload_root / iid
        p.mkdir(parents=True, exist_ok=True)
        return p

    def new_id(self):
        return f"{C.now(self.cfg):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"

    def load(self, iid):
        return json.loads(self.path(iid).read_text(encoding="utf-8"))

    def save(self, item):
        item["updated"] = stamp(self.cfg)
        atomic_write(self.path(item["id"]), json.dumps(item, ensure_ascii=False, indent=1))
        if self._cache is not None:
            self._cache[item["id"]] = item
        return item

    def all(self):
        """Every readable record. OneDrive conflict copies (other names) are listed by conflicts()."""
        if self._cache is None:
            self._cache = {}
            for p in sorted(self.dir.glob("*.json")):
                if not ID_RE.match(p.stem):
                    continue
                try:
                    it = json.loads(p.read_text(encoding="utf-8"))
                    self._cache[it["id"]] = it
                except Exception:
                    continue
        return list(self._cache.values())

    def conflicts(self):
        return [p.name for p in self.dir.glob("*.json") if not ID_RE.match(p.stem)]

    def get(self, iid):
        for it in self.all():
            if it["id"] == iid:
                return it
        return None

    def find(self, **ref):
        """First item whose source_ref has all these values (e.g. message_id=...)."""
        for it in self.all():
            sr = it.get("source_ref") or {}
            if all(sr.get(k) == v for k, v in ref.items()):
                return it
        return None

    def resolve(self, part):
        hits = [it for it in self.all() if part in it["id"]]
        if len(hits) != 1:
            raise SystemExit(f"'{part}' matches {len(hits)} items; give more of the id")
        return hits[0]

    def children(self, item):
        return [c for c in (self.get(i) for i in item.get("children") or []) if c]

    def create(self, **fields):
        item = {
            "id": self.new_id(), "source": None, "source_ref": {}, "parent": None, "children": [],
            "kind": None, "title": None, "file": None, "url": None,
            "route": "auto", "route_from": None, "doug_note": "",
            "status": "new", "needs": None, "why": None, "priority": None, "priority_why": None,
            "estimate": None, "manual": {"priority": None, "hold": False, "route": None, "note": None,
                                         "released": False},
            "attempts": 0, "work": {}, "result": {"notes": [], "original": None, "source_removed": False},
            "added": stamp(self.cfg), "history": [],
        }
        item.update(fields)
        event(item, f"added from {item.get('source')}" + (f" ({item['source_ref'].get('folder')})"
                                                           if item["source_ref"].get("folder") else ""), self.cfg)
        return item


# ---------------------------------------------------------------- state changes
def event(item, text, cfg=None):
    item.setdefault("history", []).append([stamp(cfg), text])
    del item["history"][:-60]


def set_status(item, status, why=None, needs=None, cfg=None):
    assert status in STATUSES, status
    item["status"], item["why"], item["needs"] = status, why, needs
    event(item, status + (f" ({needs})" if needs else "") + (f": {why}" if why else ""), cfg)


def route_of(item):
    return (item.get("manual") or {}).get("route") or item.get("route") or "auto"


# ---------------------------------------------------------------- ordering
def _any(val, test):
    vals = val if isinstance(val, list) else [val]
    return any(test(v) for v in vals)


def rule_matches(match, item, now):
    f = item.get("file") or {}
    for k, v in (match or {}).items():
        if k == "source" and not _any(v, lambda x: x == item.get("source")):
            return False
        if k == "route" and not _any(v, lambda x: x == route_of(item)):
            return False
        if k == "kind" and not _any(v, lambda x: x == item.get("kind")):
            return False
        if k == "status" and not _any(v, lambda x: x == item.get("status")):
            return False
        if k == "ext" and not _any(v, lambda x: x.lower() == (f.get("ext") or "").lower()):
            return False
        if k == "folder":
            fold = ((item.get("source_ref") or {}).get("folder") or "").lower()
            if not _any(v, lambda x: fold == x.lower() or fold.startswith(x.lower() + "/")):
                return False
        if k == "pages_over" and not (f.get("pages") or 0) > v:
            return False
        if k == "words_over" and not (f.get("words") or item.get("words") or 0) > v:
            return False
        if k == "older_than_days":
            try:
                added = dt.datetime.fromisoformat(item["added"])
            except Exception:
                return False
            if (now - added).days <= v:
                return False
        if k not in ("source", "route", "kind", "status", "ext", "folder", "pages_over", "words_over",
                     "older_than_days"):
            return False  # unknown key: never match rather than guess
    return True


def priority(item, rules, now):
    """(value, why). The owner's manual priority always wins over the rules."""
    mp = (item.get("manual") or {}).get("priority")
    if mp is not None:
        return int(mp), "manual"
    for i, r in enumerate(rules):
        if rule_matches(r.get("match"), item, now):
            return int(r.get("priority", 50)), r.get("why") or f"rule {i + 1}"
    return 50, "no rule matched"


def ordered(items, cfg, statuses=("ready",), now=None):
    """Items to work on, best first: priority (high first), then oldest first. Held-by-hand items
    and failed items past max_attempts are left out."""
    q = qcfg(cfg)
    now = now or C.now(cfg)
    out = []
    for it in items:
        if it.get("status") not in statuses or (it.get("manual") or {}).get("hold"):
            continue
        if it.get("status") == "failed" and it.get("attempts", 0) >= int(q["max_attempts"]):
            continue
        it["priority"], it["priority_why"] = priority(it, q["rules"], now)
        out.append(it)
    return sorted(out, key=lambda it: (-it["priority"], it.get("added") or ""))


# ---------------------------------------------------------------- command line
def _brief(it, rules, now):
    p, why = priority(it, rules, now)
    flag = " HOLD" if (it.get("manual") or {}).get("hold") else ""
    st = it["status"] + (f"/{it['needs']}" if it.get("needs") else "")
    return f"{it['id']}  {st:<14} {p:>4}{flag:<5} {route_of(it):<9} {it.get('source') or '?':<6} {(it.get('title') or '')[:60]}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Vault intake queue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--status"); p.add_argument("--route"); p.add_argument("--all", action="store_true")
    for name in ("show", "top", "release", "skip", "cancel", "retry"):
        sub.add_parser(name).add_argument("id")
    p = sub.add_parser("priority"); p.add_argument("id"); p.add_argument("value")
    p = sub.add_parser("hold"); p.add_argument("id"); p.add_argument("why", nargs="*")
    p = sub.add_parser("route"); p.add_argument("id"); p.add_argument("route", choices=ROUTES)
    p = sub.add_parser("resolved"); p.add_argument("id"); p.add_argument("note")
    sub.add_parser("rules")
    p = sub.add_parser("analyze"); p.add_argument("target", nargs="+"); p.add_argument("--redo", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--related-only", action="store_true", help="refresh only the Related list (cheap: the item isn't reread)")
    p.add_argument("--parts", action="store_true", help="long items/books: also redo the part notes (default: reuse them)")
    a = ap.parse_args(argv)
    cfg = C.load_config()
    store, q, now = Store(cfg), qcfg(cfg), C.now(cfg)
    rules = q["rules"]

    if a.cmd == "analyze":
        import analyze
        for ln in analyze.enqueue(cfg, store, a.target, redo=a.redo or a.related_only or a.parts, force=a.force,
                                  related_only=a.related_only, parts=a.parts):
            print(ln)
        return 0

    if a.cmd == "rules":
        for i, r in enumerate(rules, 1):
            print(f"{i}. priority {r.get('priority')}: {json.dumps(r.get('match'))}  - {r.get('why', '')}")
        print("Manual priority (vqueue.py top / priority) always wins. Ties: oldest first.")
        return 0
    if a.cmd == "list":
        items = store.all()
        if not a.all:
            items = [i for i in items if i["status"] in OPEN]
        if a.status:
            items = [i for i in items if i["status"] == a.status]
        if a.route:
            items = [i for i in items if route_of(i) == a.route]
        items.sort(key=lambda it: (-priority(it, rules, now)[0], it.get("added") or ""))
        for it in items:
            print(_brief(it, rules, now))
        print(f"{len(items)} item(s)" + (f"; conflict copies to check: {store.conflicts()}" if store.conflicts() else ""))
        return 0

    it = store.resolve(a.id)
    m = it.setdefault("manual", {})
    if a.cmd == "show":
        pv, why = priority(it, rules, now)
        print(json.dumps(it, ensure_ascii=False, indent=1))
        print(f"\npriority {pv}: {why}")
        return 0
    if a.cmd == "top":
        m["priority"] = 1000; event(it, "manual: top", cfg)
    elif a.cmd == "priority":
        m["priority"] = None if a.value == "clear" else int(a.value)
        event(it, f"manual: priority {a.value}", cfg)
    elif a.cmd == "hold":
        m["hold"] = True; m["note"] = " ".join(a.why) or m.get("note")
        event(it, "manual: hold" + (f" ({m['note']})" if m.get("note") else ""), cfg)
    elif a.cmd == "release":
        m["hold"] = False
        if it["status"] == "held":
            m["released"] = True       # The owner said go: collectors must not hold it again
            m["dup_ok"] = True         # ... and a costly route may spend (checked in routes.gate)
            set_status(it, "ready" if it.get("kind") in ("email", "link", "note") or (it.get("work") or {}).get("extracted") else "new",
                       "released by you", cfg=cfg)
        event(it, "manual: release", cfg)
    elif a.cmd == "route":
        m["route"] = a.route; event(it, f"manual: route {a.route}", cfg)
    elif a.cmd in ("skip", "cancel"):
        set_status(it, "skipped" if a.cmd == "skip" else "cancelled", "by you", cfg=cfg)
    elif a.cmd == "retry":
        it["attempts"] = 0
        set_status(it, "ready", "retry by you", cfg=cfg)
    elif a.cmd == "resolved":
        it["result"].setdefault("notes", []).append(a.note)
        set_status(it, "done", f"saved by hand: {a.note}", cfg=cfg)
    store.save(it)
    print(_brief(it, rules, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
