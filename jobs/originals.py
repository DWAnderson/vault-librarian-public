#!/usr/bin/env python3
"""Index the text of embedded originals: the PDF or e-book behind a card note, into the vault search index.

Plan: 2026-10-08 Vault search - plan.md, phase 4 (2026-10-11/12). Order: the books behind Book Summary notes,
then the few card-only PDF articles, references and recipes, then Manuals. Screenshots are skipped.

What it does: for each note whose body links a file in attachments/Sources, extract the file's text locally
(jobs\\extract.py: PyMuPDF per page for PDFs, Calibre for e-books; no OCR, no Claude), split it into passages
(PDF: per page, long pages split; e-book: about 250-word parts), add them to the search index as passages of the
card note (kind "original", layer source, cited "[[Note#Original]], p. 37"), and compute their meaning vectors.
Never writes to the vault and needs no vault lock. Resumable: each file is recorded in the index's `originals`
table (done / no text / failed) and redone only when the file changes.

When it runs (2026-10-12):
  * overnight, in leftover time: task \\VaultLibrarian\\Originals at 02:45; it waits while the intake queue is
    still working (vault lock held by a queue job, or an item "working"), then runs at below-normal priority
    until originals.stop_by (05:30);
  * on request: `python originals.py request [--scope books|other|manuals|all]` (or a laptop session writing
    queue\\originals-request.json); Intake starts the task on its next daytime run, and it runs for up to
    3 hours (the task's limit), then carries on in later nights.

Command line:
  python originals.py run [--scope ...] [--until HH:MM] [--now]     --now: skip the night/queue checks
  python originals.py status                                        counts by status, next files in order
  python originals.py request [--scope ...]
Report: reports\\originals-<date>.md (appended per run).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import vault_search as VS  # noqa: E402

DEFAULTS = {"enabled": True, "stop_by": "05:30", "night_start": "02:00", "card_max_words": 600,
            "max_words": 350, "overlap_words": 40, "part_words": 250, "min_words": 8, "max_file_mb": 200}
LINK = re.compile(r"\[\[([^\]|#]*?attachments/Sources/[^\]|#]+?\.(pdf|epub|mobi|azw3?|docx?))(?:[|#][^\]]*)?\]\]", re.I)
SCOPES = {"books": (1,), "other": (2,), "manuals": (3,), "all": (1, 2, 3)}
REQUEST = C.APP / "queue" / "originals-request.json"


def ocfg(cfg):
    d = dict(DEFAULTS)
    d.update(cfg.get("originals") or {})
    return d


def hhmm(cfg, text, base=None):
    base = base or C.now(cfg)
    h, m = (int(x) for x in text.split(":"))
    t = base.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > base else t + dt.timedelta(days=1)


def below_normal():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- what to index
def candidates(cfg, con):
    """[(priority, path, title, file)] in the owner's order: 1 Book Summary, 2 card-only notes, 3 Manual."""
    o = ocfg(cfg)
    vault = Path(cfg["vault_path"])
    out = []
    for path, ntype, title in con.execute("SELECT path, type, title FROM notes WHERE layer != 'skip'"):
        if ntype in ("Analysis", "Part Analysis", "Concept", "Transcript"):
            continue
        try:
            raw = (vault / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = LINK.search(raw)
        if not m:
            continue
        f = vault / m.group(1).strip()
        if not f.is_file():
            f = C.vpath(vault, "attachments") / "Sources" / Path(m.group(1)).name
        if not f.is_file():
            continue
        if ntype == "Book Summary":
            pr = 1
        elif ntype == "Manual":
            pr = 3
        else:
            _fm, body, _off = VS.parse(raw)
            if len(body.split()) >= o["card_max_words"]:
                continue          # the note already carries the full text
            pr = 2
        out.append((pr, path, title or Path(path).stem, f))
    out.sort(key=lambda x: (x[0], x[1].lower()))
    return out


# ---------------------------------------------------------------- passages
def chunks(words, size, overlap):
    if len(words) <= size:
        return [words]
    out, i = [], 0
    while i < len(words):
        out.append(words[i:i + size])
        if i + size >= len(words):
            break
        i += size - overlap
    return out


def passages_for(cfg, f, work):
    """[(unit label, start, end, text)] for a file. PDF: one or more per page; e-book/Word: numbered parts."""
    import extract as X
    o = ocfg(cfg)
    res = X.extract(str(f), str(work), ocr=False)
    pj = Path(work) / "pages.json"
    out = []
    if pj.exists():
        for n, page in enumerate(json.loads(pj.read_text(encoding="utf-8")), 1):
            w = page.split()
            if len(w) < o["min_words"]:
                continue
            for c in chunks(w, o["max_words"], o["overlap_words"]):
                out.append((f"p. {n}", n, n, " ".join(c)))
        return out, res.get("pages") or 0
    w = (res.get("text") or "").split()
    for k, c in enumerate(chunks(w, o["part_words"], o["overlap_words"]), 1):
        if len(c) >= o["min_words"]:
            out.append((f"part {k}", k, k, " ".join(c)))
    return out, len(out)


def add_passages(con, path, title, rows):
    con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM passages WHERE path=? AND kind='original')", (path,))
    con.execute("DELETE FROM passages WHERE path=? AND kind='original'", (path,))
    name, ntitle, aliases = con.execute("SELECT name, title, aliases FROM notes WHERE path=?", (path,)).fetchone()
    tfield = " ".join([name, ntitle] + json.loads(aliases or "[]"))
    header = f"{title} (original)"
    for i, (unit, a, b, text) in enumerate(rows):
        th = hashlib.sha1(f"{header}\n{text}".encode("utf-8")).hexdigest()
        cur = con.execute("INSERT INTO passages(path, ord, kind, layer, heads, line_start, line_end, text, header, "
                          "thash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (path, 100000 + i, "original", "source", f"Original > {unit}", a, b, text, header, th))
        con.execute("INSERT INTO fts(rowid, title, heads, meta, body, seealso) VALUES(?,?,?,?,?,?)",
                    (cur.lastrowid, tfield, f"Original {unit}", "", text, ""))


def record(cfg, con, path, f, status, units=0, n=0, why=""):
    try:
        st = f.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = None, None
    con.execute("INSERT OR REPLACE INTO originals VALUES(?,?,?,?,?,?,?,?,?)",
                (path, str(f), size, mtime, units, n, status, why[:300], C.now(cfg).isoformat(timespec="seconds")))


def todo(cfg, con, scope):
    pri = SCOPES.get(scope, SCOPES["all"])
    done = {r[0]: r[1:] for r in con.execute("SELECT path, file, fsize, fmtime, status FROM originals")}
    out = []
    for pr, path, title, f in candidates(cfg, con):
        if pr not in pri:
            continue
        d = done.get(path)
        try:
            st = f.stat()
        except OSError:
            continue
        if d and d[0] == str(f) and d[1] == st.st_size and d[2] == st.st_mtime and d[3] in ("done", "no text", "failed"):
            if d[3] != "done" or not con.execute("SELECT 1 FROM passages p LEFT JOIN vecs v ON v.thash=p.thash WHERE "
                                                 "p.path=? AND p.kind='original' AND v.thash IS NULL LIMIT 1",
                                                 (path,)).fetchone():
                continue      # finished (or not readable): skip; a done file with vectors left is resumed
        out.append((pr, path, title, f))
    return out


# ---------------------------------------------------------------- the run
def queue_busy(cfg):
    lk = C.VaultLock(cfg, "originals check")
    if lk.path.exists():
        owner, created, age = lk._read()
        if owner and "queue" in owner.lower() and (age is None or age < lk.stale):
            return f"{owner} holds the vault lock"
    try:
        import vqueue as Q
        recent = C.now(cfg) - dt.timedelta(minutes=30)
        for it in Q.Store(cfg).all():
            if it.get("status") == "working" and dt.datetime.fromisoformat(it.get("updated") or it["added"]) > recent:
                return f"queue item {it['id']} is working"
    except Exception:  # noqa: BLE001
        pass
    return None


def run(cfg, log, scope="all", until=None, now_mode=False):
    o = ocfg(cfg)
    started = C.now(cfg)
    if not o.get("enabled", True):
        return "originals.enabled is false"
    if not VS.vectors_available():
        return "fastembed not installed: nothing done"
    req = None
    if REQUEST.exists():
        try:
            req = json.loads(REQUEST.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            req = {}
        scope = req.get("scope") or scope
        REQUEST.unlink(missing_ok=True)
        log(f"on request ({req.get('at', '?')}): scope {scope}")
    night_start = hhmm(cfg, o["night_start"], started - dt.timedelta(hours=12))
    in_night = night_start <= started < hhmm(cfg, o["stop_by"], night_start)
    if until:
        deadline = hhmm(cfg, until)
    elif req is not None or now_mode:
        deadline = started + dt.timedelta(hours=3)          # the task's own limit is 3 hours
    elif in_night:
        deadline = hhmm(cfg, o["stop_by"], night_start)
    else:
        return "not night time and not requested: nothing done"
    if req is None and not now_mode:
        while True:   # leftover time only: wait while the night's queue work is still going
            why = queue_busy(cfg)
            if not why:
                break
            if C.now(cfg) + dt.timedelta(minutes=10) > deadline:
                return f"no leftover time tonight ({why} until {deadline:%H:%M})"
            log(f"waiting: {why}")
            time.sleep(300)
    if VS.build_running(cfg):
        return "a full search-index build is running: nothing done"
    below_normal()
    con = VS.connect(cfg)
    work_root = Path(cfg["local_root"]) / "work" / "originals"
    work_root.mkdir(parents=True, exist_ok=True)
    stats = {"done": 0, "no text": 0, "failed": 0, "passages": 0, "embedded": 0}
    lines = []
    try:
        queue = todo(cfg, con, scope)
        log(f"{len(queue)} original(s) to do (scope {scope}); stop by {deadline:%H:%M}")
        for pr, path, title, f in queue:
            if C.now(cfg) >= deadline:
                break
            have = con.execute("SELECT status FROM originals WHERE path=? AND file=?", (path, str(f))).fetchone()
            try:
                if not (have and have[0] in ("done", "embedding")):   # "embedding": passages in, vectors to finish
                    if f.stat().st_size > o["max_file_mb"] * 2**20:
                        raise ValueError(f"file over {o['max_file_mb']} MB")
                    work = work_root / hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]
                    shutil.rmtree(work, ignore_errors=True)
                    work.mkdir(parents=True)
                    rows, units = passages_for(cfg, f, work)
                    shutil.rmtree(work, ignore_errors=True)
                    if not rows:
                        record(cfg, con, path, f, "no text", units, 0, "no readable text (scanned? OCR is off here)")
                        con.commit()
                        stats["no text"] += 1
                        lines.append(f"- no text: {path}")
                        continue
                    add_passages(con, path, title, rows)
                    record(cfg, con, path, f, "embedding", units, len(rows))
                    VS.bump(con)
                    con.commit()
                    stats["passages"] += len(rows)
                d, left = VS.embed_pending(cfg, con, lambda m: None, deadline.timestamp(), originals=True, path=path)
                stats["embedded"] += d
                if left:
                    log(f"stopped mid-way in {path}: {left} passages left for next time")
                    break
                n = con.execute("SELECT count(*) FROM passages WHERE path=? AND kind='original'", (path,)).fetchone()[0]
                units = con.execute("SELECT max(line_end) FROM passages WHERE path=? AND kind='original'",
                                    (path,)).fetchone()[0] or 0
                record(cfg, con, path, f, "done", units, n)
                VS.bump(con)
                con.commit()
                stats["done"] += 1
                lines.append(f"- done: {path} ({n} passages)")
                log(f"done: {path} ({n} passages)")
            except Exception as e:  # noqa: BLE001
                con.rollback()
                record(cfg, con, path, f, "failed", 0, 0, f"{type(e).__name__}: {e}")
                con.commit()
                stats["failed"] += 1
                lines.append(f"- failed: {path}: {type(e).__name__}: {str(e)[:150]}")
                log(f"failed: {path}: {type(e).__name__}: {e}")
        left = len(todo(cfg, con, scope))
    finally:
        con.close()
    end = C.now(cfg)
    summary = (f"{started:%H:%M}-{end:%H:%M} (scope {scope}{', on request' if req is not None else ''}): "
               f"{stats['done']} done, {stats['no text']} without text, {stats['failed']} failed; "
               f"{stats['passages']:,} passages added, {stats['embedded']:,} embedded; {left} left")
    rep = C.APP / "reports" / f"originals-{started:%Y-%m-%d}.md"
    with rep.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {summary}\n\n" + ("\n".join(lines) + "\n" if lines else ""))
    log(summary)
    return summary


def status(cfg):
    con = VS.connect(cfg)
    try:
        counts = dict(con.execute("SELECT status, count(*) FROM originals GROUP BY status"))
        q = todo(cfg, con, "all")
        names = {1: "books", 2: "other", 3: "manuals"}
        left = {}
        for pr, *_ in q:
            left[names[pr]] = left.get(names[pr], 0) + 1
        nxt = [p for _pr, p, _t, _f in q[:5]]
        n = con.execute("SELECT count(*) FROM passages WHERE kind='original'").fetchone()[0]
    finally:
        con.close()
    return {"indexed": counts, "left": left, "passages": n, "next": nxt,
            "requested": json.loads(REQUEST.read_text(encoding="utf-8")) if REQUEST.exists() else None}


def request(cfg, scope):
    REQUEST.parent.mkdir(parents=True, exist_ok=True)
    REQUEST.write_text(json.dumps({"scope": scope, "at": C.now(cfg).isoformat(timespec="minutes")}), encoding="utf-8")
    return f"requested (scope {scope}); Intake starts it on its next run (7:00-23:00), otherwise tonight at 02:45"


def start_if_requested(cfg, log):
    """Called by intake.py each daytime run: start the Originals task when a request is waiting. Never raises."""
    try:
        if not REQUEST.exists() or os.name != "nt":
            return None
        import subprocess
        tp = f"-TaskPath '{C.task_path(cfg)}' -TaskName Originals"
        ps = (f"$t = Get-ScheduledTask {tp} -ErrorAction SilentlyContinue; if (-not $t) {{ 'missing' }} "
              f"elseif ($t.State -eq 'Running') {{ 'running' }} else {{ Start-ScheduledTask {tp}; 'started' }}")
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "").strip()
        log(f"originals request: task {out}")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"originals request: {type(e).__name__}: {e}")
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--scope", default="all", choices=list(SCOPES))
    r.add_argument("--until")
    r.add_argument("--now", action="store_true")
    r.add_argument("--vault")
    r.add_argument("--local")
    sub.add_parser("status")
    q = sub.add_parser("request")
    q.add_argument("--scope", default="all", choices=list(SCOPES))
    a = ap.parse_args(argv)
    cfg = C.load_config()
    if a.cmd == "run" and (a.vault or a.local):   # laptop test against a stand-in index
        if a.vault:
            cfg["vault_path"] = a.vault
        if a.local:
            cfg["local_root"] = a.local
    log = C.Log(cfg, "originals") if a.cmd == "run" and not a.local else print
    if a.cmd == "run":
        print(run(cfg, log, a.scope, a.until, a.now))
    elif a.cmd == "status":
        print(json.dumps(status(cfg), indent=1))
    else:
        print(request(cfg, a.scope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
