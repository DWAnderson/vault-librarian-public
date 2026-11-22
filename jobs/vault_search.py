"""Vault search (plan: 2026-10-08 Vault search - plan.md, phase 1): a passage index of the vault with keyword
(SQLite FTS5, BM25, porter stemming) and meaning (fastembed bge-small vectors, brute-force numpy) search, merged
by reciprocal rank.

Index: %LOCALAPPDATA%\\VaultLibrarian\\index\\search.db (WAL, so readers work while a job updates). Vectors are
stored by a hash of the passage text, so moved notes and unchanged passages are never re-embedded. The first full
build commits in batches and resumes where it stopped.

Reads the vault only; never writes to it. Settings: DEFAULTS below, overridden by an optional "search" section in
config\\vault.json.

Command line (This PC runtime python):
    vault_search.py build                    full update + stats + bench, report to reports\\search-build-*.md
    vault_search.py update [--full] [--no-vectors] [--minutes N] [--paths P ...]
    vault_search.py search "question" [--variant V ...] [-k 10] [--folder F] [--type T] [--layer L] [--tag G] [--json]
    vault_search.py get NOTE [--lines 120:40 | --heading H] [--full]
    vault_search.py stats
    vault_search.py bench [--modes kw,vec,hybrid] [--report]
Testing elsewhere: --vault PATH and --local PATH replace vault_path and local_root.

For jobs (none of these raise or write to the vault):
    hook(cfg, log)             after a job writes: compare the vault with the index, embed new passages (<= 2 min)
    hook_background(cfg, log)  the same in a thread (vault_server)
    nightly_section(cfg, log)  Nightly: compare + up to 30 min of embedding; returns the report line
    safe_update(cfg, paths, log)  just the given notes (a missing path is removed from the index)
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

DEFAULTS = {
    "enabled": True, "hook_minutes": 2, "nightly_minutes": 30,
    "model": "BAAI/bge-small-en-v1.5",
    "query_prefix": "Represent this sentence for searching relevant passages: ",
    "passage_words": 250, "max_words": 350, "overlap_words": 40, "min_words": 8, "desc_words": 40,
    "batch": 16, "commit_every": 256,
    "skip_dirs": ["_snapshots"],
    "skip_subdirs": ["_Obsidian Internal/Archives", "_Obsidian Internal/Templates"],
    "skip_files": ["index.md", "log.md", "_claude-tags.md", "CLAUDE.md"],
    "skip_types": ["Topic Map", "Tag Vocabulary", "Agent Instructions", "index"],
    "layers": {
        "mine_types": ["Personal Note", "Daily Note"], "mine_by": [],   # plus the config owner.id (layer_of)
        "chat_types": ["Claude Chat"],
        "summary_types": ["Summary", "Book Summary", "Video Summary", "Podcast Summary", "Web Summary"],
        "compiled_types": ["Analysis", "Part Analysis", "Concept", "Atomic Note", "Project Note", "Area Note"],
    },
    "mine_headings": ["My notes", "Your notes"],
    "list_headings": {"Concept": ["Discussed in"],
                      "Analysis": ["Related", "Later items that refer to this", "Parts"]},
    "weights": {"title": 10.0, "heads": 4.0, "meta": 2.0, "body": 1.0, "seealso": 0.5},
    "rrf_k": 60, "original_weight": 2.0, "kw_weight": 1.0, "vec_weight": 1.0, "list_size": 50, "per_note": 3, "k": 10,
    "snippet_words": 60, "get_max_bytes": 40000,
}
SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS notes(path TEXT PRIMARY KEY, name TEXT, title TEXT, type TEXT, layer TEXT, folder TEXT,
  mtime REAL, size INTEGER, fhash TEXT, description TEXT, tags TEXT, aliases TEXT, author TEXT, published TEXT,
  resource TEXT, concepts TEXT, analysis_of TEXT, part_of TEXT, see_also TEXT, has_my_note INTEGER);
CREATE INDEX IF NOT EXISTS notes_name ON notes(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS notes_analysis_of ON notes(analysis_of COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS passages(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, kind TEXT, layer TEXT,
  heads TEXT, line_start INTEGER, line_end INTEGER, text TEXT, header TEXT, thash TEXT);
CREATE INDEX IF NOT EXISTS passages_path ON passages(path);
CREATE INDEX IF NOT EXISTS passages_thash ON passages(thash);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, heads, meta, body, seealso, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS vecs(thash TEXT PRIMARY KEY, v BLOB);
CREATE TABLE IF NOT EXISTS originals(path TEXT PRIMARY KEY, file TEXT, fsize INTEGER, fmtime REAL, units INTEGER,
  passages INTEGER, status TEXT, why TEXT, at TEXT);
"""
STOP = set("""a about above after again all also am an and any are as at be been before being below between both but
by can could did do does doing down during each few for from further had has have having he her here hers him his
how i if in into is it its itself just me more most my no nor not now of off on once only or other our ours out over
own same she should so some such than that the their theirs them then there these they this those through to too
under until up very was we were what when where which while who whom why will with would you your yours tell
please find show give know say said think""".split())
FM = re.compile(r"(?s)^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)")
HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
_model = None
_vm = None          # (generation, ids, matrix)
_concepts = None    # (generation, [(term, [names])])


# ---------------------------------------------------------------- settings, database
def scfg(cfg):
    d = json.loads(json.dumps(DEFAULTS))
    for k, v in (cfg.get("search") or {}).items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k].update(v)
        else:
            d[k] = v
    return d


def local_root(cfg):
    return Path(os.path.expandvars(cfg["local_root"]))


def db_path(cfg):
    p = local_root(cfg) / "index"
    p.mkdir(parents=True, exist_ok=True)
    return p / "search.db"


def connect(cfg):
    con = sqlite3.connect(str(db_path(cfg)), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    s = scfg(cfg)
    if get_meta(con, "model") not in (None, s["model"]):
        con.execute("DELETE FROM vecs")                      # a different model: old vectors are useless
    set_meta(con, "model", s["model"])
    set_meta(con, "schema", SCHEMA_VERSION)
    con.commit()
    return con


def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(con, k, v):
    con.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def bump(con):
    set_meta(con, "generation", int(get_meta(con, "generation", "0")) + 1)


def stamp(cfg):
    return C.now(cfg).isoformat(timespec="seconds")


# ---------------------------------------------------------------- notes and passages (plan sections 2 and 3)
def included(rel_parts, s):
    """rel_parts: the vault-relative path split into parts (file name last)."""
    if any(p.startswith(".") for p in rel_parts[:-1]) or any(p in s["skip_dirs"] for p in rel_parts[:-1]):
        return False
    for sub in s["skip_subdirs"]:
        sp = tuple(sub.split("/"))
        if tuple(rel_parts[:len(sp)]) == sp:
            return False
    return rel_parts[-1].endswith(".md") and rel_parts[-1] not in s["skip_files"]


def iter_notes(vault, s):
    for root, dirs, files in os.walk(vault):
        rel = Path(root).relative_to(vault).parts
        dirs[:] = [d for d in dirs if included(rel + (d, "x.md"), s)]
        for f in files:
            if included(rel + (f,), s):
                yield Path(root) / f


def parse(raw):
    """Return (frontmatter dict, body, number of lines before the body)."""
    m = FM.match(raw)
    if not m:
        return {}, raw, 0
    try:
        import yaml
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:  # noqa: BLE001
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, raw[m.end():], raw[:m.end()].count("\n")


def aslist(v):
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


def linkname(v):
    """'[[Target#x|shown]]' -> 'Target'."""
    v = str(v).strip()
    m = re.match(r"^\[\[([^\]|#]*)", v)
    return (m.group(1) if m else v).strip()


def clean(text):
    text = re.sub(r"!\[\[[^\]]*\]\]", " ", text)                                  # image embeds
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[\[([^\]|#]*)(#[^\]|]*)?\|([^\]]*)\]\]", r"\1 \3", text)      # [[Target|shown]] -> both
    text = re.sub(r"\[\[([^\]|#]*)(#[^\]]*)?\]\]", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\((https?:[^)]*)\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    return text


def layer_of(fm, ntype, s):
    L = s["layers"]
    gen = fm.get("generated")
    by = str(gen.get("by", "")) if isinstance(gen, dict) else ""
    if ntype in L["mine_types"] or by in L["mine_by"] or (by and by == C.owner()["id"]):
        return "mine"
    if ntype in L["chat_types"]:
        return "claude chat"
    if ntype in L["summary_types"]:
        return "summary"
    if ntype in L["compiled_types"]:
        return "compiled"
    return "source"


def split_passages(body, off, title, desc, ntype, layer, s):
    """Split the body at headings, then long sections at paragraph breaks (about passage_words, overlapping by
    overlap_words). Returns dicts: kind, layer, heads, ls, le (1-based file lines), text, header."""
    pw, mx, ov, mn = s["passage_words"], s["max_words"], s["overlap_words"], s["min_words"]
    mine_h = {h.lower() for h in s["mine_headings"]}
    list_h = {h.lower() for h in s["list_headings"].get(ntype, [])}
    dshort = " ".join(str(desc).split()[:s["desc_words"]])
    out, heads, sec = [], [], []

    def flush():
        hp = " > ".join(h for h in heads if h)
        last = next((h for h in reversed(heads) if h), "")
        kind = "list" if last.lower() in list_h else "text"
        lay = "mine" if any(h.lower() in mine_h for h in heads if h) else layer
        paras, cur = [], []
        for ln, t in sec:
            if t.strip():
                cur.append((ln, t))
            elif cur:
                paras.append(cur)
                cur = []
        if cur:
            paras.append(cur)
        units = []
        for p in paras:
            w = clean(" ".join(t for _l, t in p)).split()
            if len(w) > mx:                                   # one very long paragraph: plain pieces
                units += [(p[0][0], p[-1][0], w[i:i + pw]) for i in range(0, len(w), pw)]
            elif w:
                units.append((p[0][0], p[-1][0], w))
        if not units:
            return
        chunks, cw, cls, cle, fresh = [], [], None, None, 0
        for uls, ule, w in units:
            if fresh and len(cw) + len(w) > mx:
                chunks.append((cls, cle, cw))
                cw, cls, fresh = cw[-ov:], None, 0
            if cls is None:
                cls = uls
            cw, cle, fresh = cw + w, ule, fresh + len(w)
            if fresh >= pw:
                chunks.append((cls, cle, cw))
                cw, cls, fresh = cw[-ov:], None, 0
        if fresh:
            chunks.append((cls if cls is not None else units[-1][0], cle, cw))
        header = title + (f" > {hp}" if hp else "") + (f". {dshort}" if dshort else "")
        for cls_, cle_, w in chunks:
            if len(w) < mn and out:
                continue
            out.append({"kind": kind, "layer": lay, "heads": hp, "ls": cls_, "le": cle_, "text": " ".join(w),
                        "header": header})

    infence = False
    for i, line in enumerate(body.split("\n")):
        line = line.rstrip("\r")
        ln = off + i + 1
        if FENCE.match(line):
            infence = not infence
        h = None if infence else HEAD.match(line)
        if h:
            flush()
            sec = []
            lvl = len(h.group(1))
            heads = heads[:lvl - 1] + [""] * max(0, lvl - 1 - len(heads)) + [h.group(2).strip()]
        else:
            sec.append((ln, line))
    flush()
    return out


def index_note(con, vault, path, s, known_hash=None):
    """(Re)index one note. Returns 'unchanged', 'skipped' or 'indexed'."""
    rel = path.relative_to(vault).as_posix()
    raw = path.read_text(encoding="utf-8", errors="replace")
    st = path.stat()
    fhash = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()
    if known_hash == fhash:
        con.execute("UPDATE notes SET mtime=?, size=? WHERE path=?", (st.st_mtime, st.st_size, rel))
        return "unchanged"
    delete_note(con, rel, keep_originals=True)
    fm, body, off = parse(raw)
    ntype = str(fm.get("type") or "")
    name = path.stem
    title = str(fm.get("title") or name)
    desc = str(fm.get("description") or "")
    aliases = aslist(fm.get("aliases"))
    tags = aslist(fm.get("tags"))
    author = aslist(fm.get("author"))
    concepts = [linkname(x) for x in aslist(fm.get("concepts"))]
    see_also = [linkname(x) for x in aslist(fm.get("see_also"))]
    analysis_of = linkname(aslist(fm.get("analysis_of"))[0]) if aslist(fm.get("analysis_of")) else ""
    part_of = linkname(aslist(fm.get("part_of"))[0]) if aslist(fm.get("part_of")) else ""
    my_note = " ".join(str(fm.get("my_note") or "").split())
    skip = ntype in s["skip_types"]
    layer = "skip" if skip else layer_of(fm, ntype, s)
    folder = str(Path(rel).parent.as_posix()) if "/" in rel else ""
    con.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rel, name, title, ntype, layer, folder, st.st_mtime, st.st_size, fhash, desc, json.dumps(tags),
                 json.dumps(aliases), ", ".join(author), str(fm.get("published") or ""),
                 ", ".join(aslist(fm.get("resource"))), json.dumps(concepts), analysis_of, part_of,
                 json.dumps(see_also), int(bool(my_note))))
    if skip:
        return "skipped"
    ps = split_passages(body, off, title, desc, ntype, layer, s)
    if not ps:                    # an image or capture with no body: title and description still searchable
        ps = [{"kind": "text", "layer": layer, "heads": "", "ls": 1, "le": max(1, off), "header": title,
               "text": desc or title}]
    if my_note:
        ps.append({"kind": "mynote", "layer": "mine", "heads": "", "ls": 1, "le": max(1, off), "header": "",
                   "text": f"{C.owner()['possessive']} note on {title}: {my_note}"})
    tfield = " ".join([name, title] + aliases)
    mfield = " ".join(author + tags + concepts)
    sfield = " ".join(see_also)
    for i, p in enumerate(ps):
        etext = embed_text(p)
        th = None if p["kind"] == "list" else hashlib.sha1(etext.encode("utf-8")).hexdigest()
        cur = con.execute("INSERT INTO passages(path, ord, kind, layer, heads, line_start, line_end, text, header, "
                          "thash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (rel, i, p["kind"], p["layer"], p["heads"], p["ls"], p["le"], p["text"], p["header"], th))
        con.execute("INSERT INTO fts(rowid, title, heads, meta, body, seealso) VALUES(?,?,?,?,?,?)",
                    (cur.lastrowid, tfield, p["heads"], mfield, p["text"], sfield))
    return "indexed"


def embed_text(p):
    return f"{p['header']}\n{p['text']}" if p.get("header") else p["text"]


def delete_note(con, rel, keep_originals=False):
    """keep_originals: re-indexing a changed note keeps the passages of its embedded original (jobs\\originals.py);
    a note that left the vault loses them too."""
    extra = " AND kind != 'original'" if keep_originals else ""
    con.execute(f"DELETE FROM fts WHERE rowid IN (SELECT id FROM passages WHERE path=?{extra})", (rel,))
    con.execute(f"DELETE FROM passages WHERE path=?{extra}", (rel,))
    con.execute("DELETE FROM notes WHERE path=?", (rel,))
    if not keep_originals:
        con.execute("DELETE FROM originals WHERE path=?", (rel,))


# ---------------------------------------------------------------- embeddings
def vectors_available():
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def model(cfg):
    global _model
    if _model is None:
        mdir = local_root(cfg) / "models"
        mdir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        if any(mdir.glob("models--*")):
            os.environ.setdefault("HF_HUB_OFFLINE", "1")         # downloaded once (phase B): no network check
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=scfg(cfg)["model"], cache_dir=str(mdir))
    return _model


def embed(cfg, texts):
    import numpy as np
    v = np.array(list(model(cfg).embed(texts, batch_size=int(scfg(cfg)["batch"]))), dtype="float32")
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


def embed_pending(cfg, con, log, deadline=None, originals=False, path=None):
    """Embed passages whose text has no vector yet, committing in batches (resumable). Returns (done, left).
    Passages of embedded originals are left to jobs\\originals.py (originals=True), so a job hook never spends
    daytime processor time on a book."""
    cond = "p.kind = 'original'" if originals else "p.kind != 'original'"
    args = []
    if path:
        cond += " AND p.path = ?"
        args.append(path)
    rows = con.execute("SELECT p.thash, MIN(p.header), MIN(p.text) FROM passages p LEFT JOIN vecs v "
                       f"ON v.thash=p.thash WHERE p.thash IS NOT NULL AND v.thash IS NULL AND {cond} GROUP BY p.thash",
                       args).fetchall()
    if not rows:
        return 0, 0
    if not vectors_available():
        log(f"meaning search: fastembed not installed; {len(rows)} passages left without vectors")
        return 0, len(rows)
    n = int(scfg(cfg)["commit_every"])
    done, t0 = 0, time.time()
    log(f"embedding {len(rows):,} passages")
    for i in range(0, len(rows), n):
        if deadline and time.time() > deadline:
            break
        part = rows[i:i + n]
        vecs = embed(cfg, [f"{h}\n{t}" if h else t for _th, h, t in part])
        con.executemany("INSERT OR REPLACE INTO vecs(thash, v) VALUES(?, ?)",
                        [(th, vecs[j].tobytes()) for j, (th, _h, _t) in enumerate(part)])
        bump(con)
        con.commit()
        done += len(part)
        rate = done / max(time.time() - t0, 1e-6)
        log(f"  embedded {done:,}/{len(rows):,} ({rate:.1f}/s, about {(len(rows) - done) / rate / 60:.0f} min left)")
    return done, len(rows) - done


# ---------------------------------------------------------------- update
def update(cfg, paths=None, full=False, vectors=True, log=print, minutes=None):
    """Bring the index up to date. paths=None compares the whole vault (new, changed, moved, removed);
    otherwise only those notes. full=True re-reads every note even if its time and size are unchanged."""
    s = scfg(cfg)
    vault = Path(cfg["vault_path"])
    t0 = time.time()
    deadline = t0 + 60 * minutes if minutes else None
    con = connect(cfg)
    st = {"indexed": 0, "unchanged": 0, "skipped": 0, "removed": 0, "errors": 0}
    try:
        known = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT path, mtime, size, fhash FROM notes")}
        todo = []
        if paths is None:
            seen = set()
            for p in iter_notes(vault, s):
                rel = p.relative_to(vault).as_posix()
                seen.add(rel)
                k = known.get(rel)
                try:
                    pst = p.stat()
                except OSError:
                    continue
                if k and not full and k[0] == pst.st_mtime and k[1] == pst.st_size:
                    st["unchanged"] += 1
                    continue
                todo.append((p, rel))
            gone = set(known) - seen
        else:
            gone = set()
            for x in paths:
                p = Path(x)
                p = p if p.is_absolute() else vault / p
                try:
                    rel = p.relative_to(vault).as_posix()
                except ValueError:
                    continue
                if p.is_file() and included(tuple(rel.split("/")), s):
                    todo.append((p, rel))
                elif rel in known:
                    gone.add(rel)
        for i, (p, rel) in enumerate(todo, 1):
            try:
                r = index_note(con, vault, p, s, None if full else (known.get(rel) or (None, None, None))[2])
                st[r if r in st else "indexed"] += 1
            except Exception as e:  # noqa: BLE001
                st["errors"] += 1
                log(f"  could not index {rel}: {type(e).__name__}: {e}")
            if i % 200 == 0:
                bump(con)
                con.commit()
                log(f"  read {i:,}/{len(todo):,} notes")
        for rel in gone:
            delete_note(con, rel)
            st["removed"] += 1
        bump(con)
        con.commit()
        if paths is None:
            con.execute("DELETE FROM vecs WHERE thash NOT IN (SELECT thash FROM passages WHERE thash IS NOT NULL)")
            con.commit()
        if vectors:
            st["embedded"], st["vectors_left"] = embed_pending(cfg, con, log, deadline)
        set_meta(con, "last_update", stamp(cfg))
        if paths is None:
            set_meta(con, "last_full_compare", stamp(cfg))
        con.commit()
    finally:
        con.close()
    st["seconds"] = round(time.time() - t0, 1)
    return st


def build_running(cfg):
    """True while a full build (vault_search.py build) is embedding, so job hooks don't duplicate its work."""
    try:
        con = connect(cfg)
        try:
            t = get_meta(con, "build_started")
        finally:
            con.close()
        if not t:
            return False
        import datetime as dt
        return (C.now(cfg) - dt.datetime.fromisoformat(t)).total_seconds() < 3 * 3600
    except Exception:  # noqa: BLE001
        return False


def hook(cfg, log=print, minutes=None):
    """For a job that just wrote to the vault: compare the vault with the index (a few seconds when little
    changed) and embed the new passages, for at most `hook_minutes`. Never raises; never writes to the vault."""
    s = scfg(cfg)
    if not s.get("enabled", True):
        return None
    try:
        st = update(cfg, vectors=not build_running(cfg), log=lambda m: None,
                    minutes=minutes if minutes is not None else s.get("hook_minutes", 2))
        changed = {k: v for k, v in st.items() if v and k not in ("unchanged", "seconds")}
        log(f"search index: {', '.join(f'{k} {v}' for k, v in changed.items()) or 'up to date'} "
            f"({st['seconds']} s)")
        return st
    except Exception as e:  # noqa: BLE001
        log(f"search index update failed (search still works on the previous index): {type(e).__name__}: {e}")
        return None


_bg = None


def hook_background(cfg, log=print):
    """hook() in a background thread (for vault_server after a web-page change); at most one at a time."""
    global _bg
    import threading
    if _bg is not None and _bg.is_alive():
        return
    _bg = threading.Thread(target=hook, args=(cfg, log), daemon=True)
    _bg.start()


def nightly_section(cfg, log=print):
    """Nightly: full comparison plus up to `nightly_minutes` of embedding; one line for the Nightly report."""
    s = scfg(cfg)
    if not s.get("enabled", True):
        return "off"
    try:
        st = update(cfg, vectors=not build_running(cfg), log=log, minutes=s.get("nightly_minutes", 30))
        i = stats(cfg)
        left = f"; {i['waiting_for_vectors']:,} passages still waiting for meaning vectors" \
            if i["waiting_for_vectors"] else ""
        return (f"{i['passages']:,} passages in {i['notes']:,} notes ({i['size_mb']} MB); tonight "
                f"{st['indexed']} notes re-read, {st['removed']} removed, {st.get('embedded', 0)} passages "
                f"embedded in {st['seconds']:.0f} s{left}")
    except Exception as e:  # noqa: BLE001
        return f"FAILED: {type(e).__name__}: {e}"


def safe_update(cfg, paths=None, log=print, minutes=10):
    """For jobs: update the index for the notes a job wrote (or the whole vault if paths is None). Never raises."""
    try:
        if not scfg(cfg).get("enabled", True):
            return None
        st = update(cfg, paths=[str(p) for p in paths] if paths is not None else None, log=log, minutes=minutes)
        log("search index: " + ", ".join(f"{k} {v}" for k, v in st.items() if v))
        return st
    except Exception as e:  # noqa: BLE001
        log(f"search index update failed (search still works on the previous index): {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------- search (plan section 4)
def tokens(q):
    return [w for w in re.findall(r"[A-Za-z0-9]+", q.lower()) if w not in STOP and len(w) > 1]


def concept_terms(con):
    global _concepts
    gen = get_meta(con, "generation", "0")
    if _concepts is None or _concepts[0] != gen:
        out = []
        for name, title, aliases in con.execute("SELECT name, title, aliases FROM notes WHERE type='Concept'"):
            names = list(dict.fromkeys([title, name] + json.loads(aliases or "[]")))
            for t in names:
                if len(t) >= 3:
                    out.append((t.lower(), names))
        _concepts = (gen, out)
    return _concepts[1]


def fts_query(con, q):
    terms = [f'"{w}"' for w in tokens(q)]
    ql = " " + " ".join(re.findall(r"[a-z0-9]+", q.lower())) + " "
    for term, names in concept_terms(con):
        tl = " " + " ".join(re.findall(r"[a-z0-9]+", term)) + " "
        if tl.strip() and tl in ql:
            terms += ['"' + n.replace('"', '""') + '"' for n in names]
    return " OR ".join(dict.fromkeys(terms))


def kw_search(con, q, s, allowed):
    expr = fts_query(con, q)
    if not expr:
        return []
    w = s["weights"]
    rows = con.execute(f"SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, {w['title']}, {w['heads']}, "
                       f"{w['meta']}, {w['body']}, {w['seealso']}) LIMIT 2000", (expr,)).fetchall()
    out = [r[0] for r in rows if allowed is None or r[0] in allowed]
    return out[:s["list_size"]]


def vector_matrix(con):
    global _vm
    import numpy as np
    gen = get_meta(con, "generation", "0")
    if _vm is None or _vm[0] != gen:
        rows = con.execute("SELECT p.id, v.v FROM passages p JOIN vecs v ON v.thash=p.thash").fetchall()
        ids = np.array([r[0] for r in rows], dtype="int64")
        mat = np.frombuffer(b"".join(r[1] for r in rows), dtype="float32").reshape(len(rows), -1) if rows \
            else np.zeros((0, 384), dtype="float32")
        _vm = (gen, ids, mat)
    return _vm[1], _vm[2]


def vec_search(cfg, con, q, s, allowed):
    import numpy as np
    ids, mat = vector_matrix(con)
    if not len(ids):
        return []
    qv = embed(cfg, [s["query_prefix"] + q])[0]
    sims = mat @ qv
    if allowed is not None:
        sims = np.where(np.isin(ids, np.fromiter(allowed, dtype="int64")), sims, -9.0)
    n = min(s["list_size"], len(ids))
    top = np.argpartition(-sims, n - 1)[:n]
    top = top[np.argsort(-sims[top])]
    return [int(ids[i]) for i in top if sims[i] > -9.0]


def allowed_ids(con, folder=None, ntype=None, layer=None, tag=None):
    cond, args = [], []
    if folder:
        f = folder.strip("/").replace("\\", "/")
        cond.append("(n.folder = ? OR n.folder LIKE ?)")
        args += [f, f + "/%"]
    if ntype:
        cond.append("n.type = ? COLLATE NOCASE")
        args.append(ntype)
    if layer:
        cond.append("p.layer = ?")
        args.append(layer)
    if tag:
        cond.append("n.tags LIKE ?")
        args.append('%"' + tag.lstrip("#") + '"%')
    if not cond:
        return None
    return {r[0] for r in con.execute("SELECT p.id FROM passages p JOIN notes n ON n.path=p.path WHERE "
                                      + " AND ".join(cond), args)}


def snippet(text, q, n):
    w = text.split()
    toks = [t[:5] for t in tokens(q)]
    start = 0
    for i, x in enumerate(w):
        xl = re.sub(r"[^a-z0-9]", "", x.lower())
        if any(xl.startswith(t) for t in toks if t):
            start = max(0, i - 15)
            break
    return ("... " if start else "") + " ".join(w[start:start + n]) + (" ..." if start + n < len(w) else "")


def search(cfg, question, variants=None, k=None, folder=None, ntype=None, layer=None, tag=None, modes=("kw", "vec"),
           con=None):
    s = scfg(cfg)
    k = k or s["k"]
    own = con is None
    con = con or connect(cfg)
    try:
        allowed = allowed_ids(con, folder, ntype, layer, tag)
        use_vec = "vec" in modes and vectors_available() and \
            con.execute("SELECT 1 FROM vecs LIMIT 1").fetchone() is not None
        lists = []
        for j, (q, w) in enumerate([(question, s["original_weight"])] + [(v, 1.0) for v in variants or []]):
            tag_ = "" if j == 0 else f" (variant {j})"
            if "kw" in modes:
                lists.append((w * s["kw_weight"], "keyword" + tag_, kw_search(con, q, s, allowed)))
            if use_vec:
                lists.append((w * s["vec_weight"], "meaning" + tag_, vec_search(cfg, con, q, s, allowed)))
        score, found = {}, {}
        for w, name, pids in lists:
            for r, pid in enumerate(pids, 1):
                score[pid] = score.get(pid, 0.0) + w / (s["rrf_k"] + r)
                found.setdefault(pid, []).append(f"{name} #{r}")
        info = {}
        if score:
            qm = ",".join("?" * len(score))
            for pid, path, kind in con.execute(f"SELECT id, path, kind FROM passages WHERE id IN ({qm})", list(score)):
                info[pid] = (path, kind)
        # The owner's my_note: counts toward the item, but the item's best passage is what is shown
        bonus = {}
        for pid in [p for p in score if info.get(p, ("", ""))[1] == "mynote"]:
            path = info[pid][0]
            b = bonus.setdefault(path, [0.0, []])
            b[0] += score.pop(pid)
            b[1] += found.pop(pid)
        for path, (b, _f) in bonus.items():
            cands = [p for p in score if info.get(p, ("", ""))[0] == path]
            if cands:
                best = max(cands, key=score.get)
            else:
                r = con.execute("SELECT id FROM passages WHERE path=? AND kind!='mynote' ORDER BY ord LIMIT 1",
                                (path,)).fetchone()
                if not r:
                    continue
                best = r[0]
                info[best] = (path, "text")
                score[best] = 0.0
            score[best] += b
            found.setdefault(best, []).append("your note")
        ranked, per, lists_shown = [], {}, set()
        for pid in sorted(score, key=lambda p: -score[p]):
            path, kind = info[pid]
            if per.get(path, 0) >= s["per_note"] or (kind == "list" and path in lists_shown):
                continue                                  # a generated link list shows once per note
            if kind == "list":
                lists_shown.add(path)
            per[path] = per.get(path, 0) + 1
            ranked.append(pid)
            if len(ranked) >= k:
                break
        results = [result(con, pid, score[pid], found[pid], question, s) for pid in ranked]
        return {"query": question, "index_updated": get_meta(con, "last_update"), "results": results,
                "meaning_search": use_vec}
    finally:
        if own:
            con.close()


def result(con, pid, sc, found, q, s):
    path, heads, ls, le, text, layer, kind = con.execute(
        "SELECT path, heads, line_start, line_end, text, layer, kind FROM passages WHERE id=?", (pid,)).fetchone()
    name, ntype, folder, analysis_of, see_also = con.execute(
        "SELECT name, type, folder, analysis_of, see_also FROM notes WHERE path=?", (path,)).fetchone()
    section = heads.split(" > ")[-1] if heads else ""
    r = {"cite": f"[[{name}#{section}]]" if section else f"[[{name}]]", "note": name, "path": path,
         "section": heads, "type": ntype, "layer": layer, "folder": folder, "lines": f"{ls}-{le}",
         "found_by": found, "score": round(sc, 4), "snippet": snippet(text, q, s["snippet_words"])}
    if kind == "original":   # text of the PDF / e-book behind the note (jobs\\originals.py)
        unit = heads.split(" > ")[-1] if heads else ""
        r["cite"] = f"[[{name}#Original]], {unit}" if unit else f"[[{name}#Original]]"
        r["section"] = heads
        r["from_original"] = True
        r.pop("lines", None)
        r["pages" if unit.startswith("p") else "parts"] = f"{ls}-{le}" if le != ls else str(ls)
    if analysis_of:
        r["analysis_of"] = f"[[{analysis_of}]]"
    an = con.execute("SELECT name FROM notes WHERE analysis_of = ? COLLATE NOCASE AND path != ? LIMIT 1",
                     (name, path)).fetchone()
    if an:
        r["analysis"] = f"[[{an[0]}]]"
    sa = json.loads(see_also or "[]")
    if sa:
        r["see_also"] = [f"[[{x}]]" for x in sa]
    return r


# ---------------------------------------------------------------- related notes (phase 4, 2026-10-12)
def related(cfg, rel, k=20, con=None):
    """Notes most like `rel` by meaning, from the passage index: the note's own passages (its text, and the owner's
    my_note counted twice) averaged into one vector, compared with every passage; a candidate note scores its best
    passage. Generated link lists don't count. Returns [(path, score)] best first (empty if no vectors)."""
    import numpy as np
    own = con is None
    con = con or connect(cfg)
    try:
        ids, mat = vector_matrix(con)
        if not len(ids):
            return []
        info = {pid: (path, kind) for pid, path, kind in con.execute("SELECT id, path, kind FROM passages")}
        # only notes an analysis could offer as Related: not analyses, parts, transcripts, concepts, maps, Claude/
        # housekeeping notes (2026-10-12: the first comparison was crowded with an item's own companions)
        skip = {r[0] for r in con.execute(
            "SELECT path FROM notes WHERE type IN ('Analysis','Part Analysis','Concept','Transcript',"
            "'Podcast Transcript','Topic Map','Tag Vocabulary','Agent Instructions','index') OR layer='skip' "
            "OR path LIKE 'Claude/%' OR path LIKE '\\_Obsidian Internal/%' ESCAPE '\\' OR analysis_of != ''")}
        pos = {int(x): i for i, x in enumerate(ids)}
        mine = [(pos[pid], 2.0 if kind == "mynote" else 1.0) for pid, (path, kind) in info.items()
                if path == rel and pid in pos and kind not in ("list", "original")]
        if not mine:
            return []
        w = np.array([x[1] for x in mine], dtype="float32")
        q = (mat[[x[0] for x in mine]] * w[:, None]).sum(0)
        q /= max(float(np.linalg.norm(q)), 1e-9)
        sims = mat @ q
        best = {}
        for i in np.argsort(-sims)[:4000]:
            path, kind = info.get(int(ids[i]), ("", ""))
            if not path or path == rel or kind == "list" or path in skip:
                continue
            if path not in best:
                best[path] = float(sims[i])
                if len(best) >= k:
                    break
        return sorted(best.items(), key=lambda x: -x[1])
    finally:
        if own:
            con.close()


def similar_pairs(cfg, exclude=(), limit=5, con=None):
    """Possible duplicates by meaning (phase 4, 2026-10-12): pairs of notes whose text passages say nearly the
    same thing even where the words differ (a re-summarized copy, a translation-like rewrite). Two steps: the notes'
    average passage vectors are very close (search.dup_note >= 0.95), then most of the smaller note's passages
    (search.dup_share >= 0.6) have a near-identical passage in the other (search.dup_passage >= 0.93).
    Analyses, concepts, transcripts, generated lists, the owner's my_note and originals don't take part.
    exclude: pairs (frozenset of two paths) already asked about. Returns [{"notes": [a, b], "why": [...], "score"}]."""
    import numpy as np
    s = scfg(cfg)
    t_note, t_pass, t_share = s.get("dup_note", 0.95), s.get("dup_passage", 0.93), s.get("dup_share", 0.6)
    own = con is None
    con = con or connect(cfg)
    try:
        ids, mat = vector_matrix(con)
        if not len(ids):
            return []
        pos = {int(x): i for i, x in enumerate(ids)}
        rows = con.execute("SELECT p.id, p.path FROM passages p JOIN notes n ON n.path=p.path WHERE p.kind='text' AND "
                           "n.layer != 'skip' AND n.type NOT IN ('Analysis','Part Analysis','Concept','Transcript',"
                           "'Podcast Transcript','Topic Map')").fetchall()
        by = {}
        for pid, path in rows:
            if pid in pos:
                by.setdefault(path, []).append(pos[pid])
        paths = [p for p, v in by.items() if len(v) >= 2]
        if len(paths) < 2:
            return []
        cent = np.stack([mat[by[p]].mean(0) for p in paths])
        cent /= np.maximum(np.linalg.norm(cent, axis=1, keepdims=True), 1e-9)
        sim = cent @ cent.T
        np.fill_diagonal(sim, -1)
        out = []
        for i, j in zip(*np.where(np.triu(sim) >= t_note)):
            a, b = paths[i], paths[j]
            if frozenset((a, b)) in exclude:
                continue
            small, big = (a, b) if len(by[a]) <= len(by[b]) else (b, a)
            best = (mat[by[small]] @ mat[by[big]].T).max(1)
            share = float((best >= t_pass).mean())
            if share >= t_share:
                out.append({"notes": [a, b], "score": round(float(sim[i, j]), 3),
                            "why": [f"same meaning in {round(100 * share)}% of the shorter note's passages"]})
        out.sort(key=lambda x: -x["score"])
        return out[:limit]
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------- get
def resolve(con, ref):
    ref = linkname(ref) if ref.strip().startswith("[[") else ref.strip()
    ref = ref.split("#")[0].strip()
    for sql in ("SELECT path FROM notes WHERE path = ?", "SELECT path FROM notes WHERE path = ? || '.md'",
                "SELECT path FROM notes WHERE name = ? COLLATE NOCASE",
                "SELECT path FROM notes WHERE title = ? COLLATE NOCASE"):
        r = con.execute(sql, (ref.replace("\\", "/"),)).fetchone()
        if r:
            return r[0]
    for path, aliases in con.execute("SELECT path, aliases FROM notes WHERE aliases LIKE ?", (f"%{ref}%",)):
        if ref.lower() in (a.lower() for a in json.loads(aliases or "[]")):
            return path
    return None


def get(cfg, note, lines=None, heading=None, full=False, con=None, pages=None):
    s = scfg(cfg)
    own = con is None
    con = con or connect(cfg)
    try:
        if heading is None and "#" in note and not note.strip().startswith("#"):
            heading = re.sub(r"[\]|].*$", "", note.split("#", 1)[1]).strip() or None
        rel = resolve(con, note)
        if rel and pages:   # the embedded original, by page (PDF) or part (e-book): jobs\\originals.py
            m = re.match(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$", str(pages))
            if not m:
                return {"error": "pages must look like 37 or 37-40"}
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            if b - a > 30:
                return {"error": "at most 30 pages at a time"}
            rows = con.execute("SELECT heads, line_start, line_end, text FROM passages WHERE path=? AND "
                               "kind='original' AND line_end >= ? AND line_start <= ? ORDER BY ord", (rel, a, b)).fetchall()
            if not rows:
                st = con.execute("SELECT status, why FROM originals WHERE path=?", (rel,)).fetchone()
                return {"error": f"no indexed original text for {rel} at {pages}"
                                 + (f" (original: {st[0]}{'; ' + st[1] if st[1] else ''})" if st else
                                    " (its original hasn't been indexed)")}
            text = "\n\n".join(f"[{h.split(' > ')[-1]}]\n{t}" for h, _a, _b, t in rows)
            return {"note": Path(rel).stem, "path": rel, "original": True, "pages": f"{a}-{b}", "text": text}
    finally:
        if own:
            con.close()
    if not rel:
        return {"error": f"no note named {note!r} in the index"}
    p = Path(cfg["vault_path"]) / rel
    if not p.is_file():
        return {"error": f"{rel} is in the index but no longer in the vault (moved or deleted?)"}
    L = p.read_text(encoding="utf-8", errors="replace").split("\n")
    a, b = 1, len(L)
    if lines:
        m = re.match(r"^\s*(\d+)\s*[:+]\s*(\d+)\s*$", lines) or re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", lines)
        if not m:
            return {"error": "lines must look like 120:40 (start:count) or 120-160"}
        a = int(m.group(1))
        b = a + int(m.group(2)) - 1 if ":" in lines or "+" in lines else int(m.group(2))
    elif heading:
        hl = heading.strip().lower()
        start = lvl = None
        for i, line in enumerate(L, 1):
            h = HEAD.match(line.rstrip("\r"))
            if h and start is None and h.group(2).strip().lower() == hl:
                start, lvl = i, len(h.group(1))
            elif h and start is not None and len(h.group(1)) <= lvl:
                b = i - 1
                break
        if start is None:
            heads = [x.rstrip("\r") for x in L if HEAD.match(x.rstrip("\r"))]
            return {"error": f"no heading {heading!r} in {rel}", "headings": heads[:80]}
        a = start
    a, b = max(1, a), min(len(L), b)
    text = "\n".join(x.rstrip("\r") for x in L[a - 1:b])
    if not (lines or heading) and len(text.encode("utf-8")) > s["get_max_bytes"] and not full:
        heads = [f"{i}: {x.rstrip(chr(13))}" for i, x in enumerate(L, 1) if HEAD.match(x.rstrip("\r"))]
        return {"error": f"{rel} is {len(text.encode('utf-8')) // 1024} KB; ask for lines or a heading "
                         f"(or full=true)", "path": rel, "lines_total": len(L), "headings": heads[:120]}
    return {"note": Path(rel).stem, "path": rel, "lines": f"{a}-{b}", "lines_total": len(L), "text": text}


# ---------------------------------------------------------------- stats and bench
def stats(cfg, con=None):
    own = con is None
    con = con or connect(cfg)
    try:
        q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
        st = {
            "notes": q("SELECT count(*) FROM notes WHERE layer != 'skip'"),
            "notes_skipped": q("SELECT count(*) FROM notes WHERE layer = 'skip'"),
            "passages": q("SELECT count(*) FROM passages"),
            "vectors": q("SELECT count(*) FROM vecs"),
            "waiting_for_vectors": q("SELECT count(DISTINCT p.thash) FROM passages p LEFT JOIN vecs v ON "
                                     "v.thash=p.thash WHERE p.thash IS NOT NULL AND v.thash IS NULL AND p.kind != 'original'"),
            "by_layer": dict(con.execute("SELECT layer, count(*) FROM passages GROUP BY layer ORDER BY 2 DESC")),
            "by_kind": dict(con.execute("SELECT kind, count(*) FROM passages GROUP BY kind")),
            "last_update": get_meta(con, "last_update"),
            "last_full_compare": get_meta(con, "last_full_compare"),
            "model": get_meta(con, "model"),
            "originals": dict(con.execute("SELECT status, count(*) FROM originals GROUP BY status")),
            "original_passages": q("SELECT count(*) FROM passages WHERE kind='original'"),
        }
    finally:
        if own:
            con.close()
    p = db_path(cfg)
    st["size_mb"] = round(sum(x.stat().st_size for x in p.parent.glob(p.name + "*")) / 2**20, 1)
    return st


def bench(cfg, modes=("kw", "vec", "hybrid"), log=print):
    tq = C.APP / "tests" / "search_questions.json"
    qs = json.loads(tq.read_text(encoding="utf-8"))["questions"]
    con = connect(cfg)
    run = {"kw": ("kw",), "vec": ("vec",), "hybrid": ("kw", "vec")}
    if not (vectors_available() and con.execute("SELECT 1 FROM vecs LIMIT 1").fetchone()):
        modes = [m for m in modes if m == "kw"]
    rows, summary = [], {}
    try:
        for m in modes:
            h5 = h10 = 0
            times = []
            for q in qs:
                t = time.time()
                res = search(cfg, q["question"], k=50, modes=run[m], con=con)["results"]
                times.append(time.time() - t)
                notes = list(dict.fromkeys(r["path"] for r in res))
                exp = {e.lower() for e in q["expect"]}
                rank = None
                for i, path in enumerate(notes, 1):
                    name, title, aliases = con.execute("SELECT name, title, aliases FROM notes WHERE path=?",
                                                       (path,)).fetchone()
                    names = {name.lower(), title.lower()} | {x.lower() for x in json.loads(aliases or "[]")}
                    if names & exp:
                        rank = i
                        break
                h5 += bool(rank and rank <= 5)
                h10 += bool(rank and rank <= 10)
                rows.append((q["id"], m, rank, q["question"]))
            summary[m] = {"top5": h5, "top10": h10, "of": len(qs), "avg_ms": round(1000 * sum(times) / len(times))}
            log(f"{m}: top 5 {h5}/{len(qs)}, top 10 {h10}/{len(qs)}, average {summary[m]['avg_ms']} ms")
    finally:
        con.close()
    return summary, rows


def bench_markdown(summary, rows):
    modes = list(summary)
    out = ["| id | " + " | ".join(modes) + " | question |", "|---|" + "---|" * len(modes) + "---|"]
    by = {}
    for qid, m, rank, qtext in rows:
        by.setdefault((qid, qtext), {})[m] = rank
    for (qid, qtext), r in by.items():
        out.append(f"| {qid} | " + " | ".join(str(r.get(m) or "-") for m in modes) + f" | {qtext} |")
    out.append("")
    for m, v in summary.items():
        out.append(f"- **{m}**: top 5 {v['top5']}/{v['of']}, top 10 {v['top10']}/{v['of']}, "
                   f"average {v['avg_ms']} ms per search")
    out.append("- Rank = position of the first expected note among distinct notes (top 50); '-' = not found.")
    return out


def tune(cfg, log=print):
    """Try merge settings (keyword weight, RRF k) on the test set. Each question's keyword and meaning lists are
    computed once and reused, so the whole sweep takes about as long as one bench. Changes no settings."""
    global kw_search, vec_search
    tq = C.APP / "tests" / "search_questions.json"
    qs = json.loads(tq.read_text(encoding="utf-8"))["questions"]
    combos = [(kw, k) for k in (60, 30) for kw in (1.0, 0.75, 0.5, 0.35, 0.25)]
    orig_kw, orig_vec, memo = kw_search, vec_search, {}
    kw_search = lambda con, q, s, allowed: memo.setdefault(("kw", q, str(allowed)), orig_kw(con, q, s, allowed))  # noqa: E731
    vec_search = lambda c, con, q, s, allowed: memo.setdefault(("vec", q, str(allowed)), orig_vec(c, con, q, s, allowed))  # noqa: E731
    saved = json.loads(json.dumps(cfg.get("search") or {}))
    con = connect(cfg)
    names = {}
    for path, name, title, aliases in con.execute("SELECT path, name, title, aliases FROM notes"):
        names[path] = {name.lower(), title.lower()} | {a.lower() for a in json.loads(aliases or "[]")}
    out = []
    try:
        for kw, k in combos:
            cfg["search"] = dict(saved, kw_weight=kw, rrf_k=k)
            ranks = []
            for q in qs:
                res = search(cfg, q["question"], k=50, con=con)["results"]
                notes = list(dict.fromkeys(r["path"] for r in res))
                exp = {e.lower() for e in q["expect"]}
                ranks.append(next((i for i, p in enumerate(notes, 1) if names.get(p, set()) & exp), None))
            t10 = sum(1 for r in ranks if r and r <= 10)
            t5 = sum(1 for r in ranks if r and r <= 5)
            mrr = sum(1 / r for r in ranks if r) / len(ranks)
            out.append((kw, k, t10, t5, mrr, ranks))
            log(f"keyword weight {kw}, k {k}: top 10 {t10}/{len(qs)}, top 5 {t5}, MRR {mrr:.3f}")
    finally:
        cfg["search"] = saved
        kw_search, vec_search = orig_kw, orig_vec
        con.close()
    md = ["| keyword weight | RRF k | top 10 | top 5 | MRR | " + " | ".join(q["id"] for q in qs) + " |",
          "|---|---|---|---|---|" + "---|" * len(qs)]
    for kw, k, t10, t5, mrr, ranks in out:
        md.append(f"| {kw} | {k} | {t10}/{len(qs)} | {t5} | {mrr:.3f} | "
                  + " | ".join(str(r or "-") for r in ranks) + " |")
    md += ["", "- Meaning weight 1.0 throughout; the current setting is keyword weight 1.0, k 60.",
           "- MRR = mean of 1/rank (1.0 = every expected note first). Ranks among distinct notes, top 50."]
    return out, md


# ---------------------------------------------------------------- command line
def load(args):
    if not (args.vault or args.local):
        return C.load_config()
    cfg = json.loads((C.APP / "config" / "vault.json").read_text(encoding="utf-8"))
    cfg = json.loads(os.path.expandvars(json.dumps(cfg)))
    if args.vault:
        cfg["vault_path"] = args.vault
    if args.local:
        cfg["local_root"] = args.local
    return cfg


def lock_note(cfg):
    """A different job holding a fresh vault lock -> a reason not to start a long build now."""
    lk = C.VaultLock(cfg, "vault search build")
    if not lk.path.exists():
        return None
    owner, created, age = lk._read()
    if owner and owner != lk.owner and (age is None or age < lk.stale):
        return f"{owner} holds the vault lock (since {created})"
    return None


def main(argv=None):
    for f in (sys.stdout, sys.stderr):
        try:
            f.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Vault search: passage index, keyword + meaning search")
    ap.add_argument("--vault")
    ap.add_argument("--local")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--ignore-lock", action="store_true")
    u = sub.add_parser("update")
    u.add_argument("--full", action="store_true")
    u.add_argument("--no-vectors", action="store_true")
    u.add_argument("--minutes", type=float)
    u.add_argument("--paths", nargs="*")
    u.add_argument("--ignore-lock", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("question")
    s.add_argument("--variant", action="append", default=[])
    s.add_argument("-k", type=int)
    s.add_argument("--folder")
    s.add_argument("--type")
    s.add_argument("--layer")
    s.add_argument("--tag")
    s.add_argument("--modes", default="kw,vec")
    s.add_argument("--json", action="store_true")
    g = sub.add_parser("get")
    g.add_argument("note")
    g.add_argument("--lines")
    g.add_argument("--heading")
    g.add_argument("--full", action="store_true")
    sub.add_parser("stats")
    be = sub.add_parser("bench")
    be.add_argument("--modes", default="kw,vec,hybrid")
    be.add_argument("--report", action="store_true")
    sub.add_parser("tune")
    a = ap.parse_args(argv)
    cfg = load(a)
    (local_root(cfg) / "logs").mkdir(parents=True, exist_ok=True)
    log = C.Log(cfg, "search")

    if a.cmd in ("build", "update") and not a.ignore_lock and (a.cmd == "build" or not a.paths):
        why = lock_note(cfg)
        if why:
            log(f"not started: {why}. Try again later (or --ignore-lock).")
            return 2
    if a.cmd == "build":
        t0 = C.now(cfg)
        log("full build: reading the vault")
        con = connect(cfg)
        set_meta(con, "build_started", stamp(cfg))           # job hooks leave the embedding to this run
        con.commit()
        con.close()
        try:
            st = update(cfg, full=True, log=log)
        finally:
            con = connect(cfg)
            con.execute("DELETE FROM meta WHERE k='build_started'")
            con.commit()
            con.close()
        log("update: " + json.dumps(st))
        info = stats(cfg)
        summary, rows = bench(cfg, log=log)
        out = [f"# Vault search: index build ({t0:%Y-%m-%d %H:%M} to {C.now(cfg):%H:%M}, America/Chicago)", "",
               "Plan: `2026-10-08 Vault search - plan.md`, phase 1.", "", "## Update", ""]
        out += [f"- {k}: {v}" for k, v in st.items()]
        out += ["", "## Index", ""] + [f"- {k}: {v}" for k, v in info.items()]
        out += ["", "## Test questions", ""] + bench_markdown(summary, rows)
        rp = C.APP / "reports" / f"search-build-{t0:%Y-%m-%d-%H%M}.md"
        rp.write_text("\n".join(out) + "\n", encoding="utf-8")
        log(f"report: {rp}")
        return 0
    if a.cmd == "update":
        st = update(cfg, paths=a.paths, full=a.full, vectors=not a.no_vectors, log=log, minutes=a.minutes)
        log("update: " + json.dumps(st))
        return 0
    if a.cmd == "search":
        res = search(cfg, a.question, a.variant, a.k, a.folder, a.type, a.layer, a.tag, tuple(a.modes.split(",")))
        if a.json:
            print(json.dumps(res, indent=1, ensure_ascii=False))
            return 0
        print(f"index updated {res['index_updated']}; meaning search {'on' if res['meaning_search'] else 'off'}")
        for i, r in enumerate(res["results"], 1):
            extra = "".join(f"; {k} {r[k]}" for k in ("analysis", "analysis_of") if k in r)
            print(f"{i:2}. {r['cite']}  [{r['type']}, {r['layer']}, lines {r['lines']}{extra}]")
            print(f"    found by: {', '.join(r['found_by'])}")
            print(f"    {r['snippet']}")
        return 0
    if a.cmd == "get":
        print(json.dumps(get(cfg, a.note, a.lines, a.heading, a.full), indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "stats":
        print(json.dumps(stats(cfg), indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "bench":
        summary, rows = bench(cfg, tuple(a.modes.split(",")), log=log)
        md = bench_markdown(summary, rows)
        print("\n".join(md))
        if a.report:
            rp = C.APP / "reports" / f"search-bench-{C.now(cfg):%Y-%m-%d-%H%M}.md"
            rp.write_text("# Vault search: test questions\n\n" + "\n".join(md) + "\n", encoding="utf-8")
            print(f"report: {rp}")
        return 0
    if a.cmd == "tune":
        _out, md = tune(cfg, log=log)
        rp = C.APP / "reports" / f"search-tune-{C.now(cfg):%Y-%m-%d-%H%M}.md"
        rp.write_text("# Vault search: merge settings compared\n\n" + "\n".join(md) + "\n", encoding="utf-8")
        print(f"report: {rp}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
