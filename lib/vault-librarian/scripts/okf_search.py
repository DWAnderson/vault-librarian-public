#!/usr/bin/env python3
"""Search and read an exported OKF knowledge bundle (written by vault-librarian's okf_export.py).

This file ships inside every export as _tools/okf_search.py. It needs only Python 3.8+ (standard library):
keyword search uses SQLite FTS5 (BM25 ranking, porter stemming). Search by meaning is optional: after
`pip install fastembed numpy`, run `embed` once and searches then combine keyword and meaning results.

    python _tools/okf_search.py search "question" [--variant V ...] [-k 10] [--folder F] [--type T]
                                       [--layer L] [--tag G] [--json]
    python _tools/okf_search.py get NOTE [--lines 120:40 | --heading H | --pages 37-40] [--full]
    python _tools/okf_search.py stats
    python _tools/okf_search.py mcp          read-only MCP server on stdin/stdout (tools vault_search, vault_get)
    python _tools/okf_search.py build        rebuild _tools/search.db from the notes (after editing the bundle)
    python _tools/okf_search.py embed        add meaning vectors (needs fastembed + numpy; about 1 hour per
                                             15,000 passages on a laptop CPU)

The index splits every note into passages of about 250 words (at headings first), each with a one-line
header "Note title > Section. Description." Results are merged by reciprocal rank; the question itself counts
double, and variants (rewordings, exact names) add their own lists. Each result carries the note's `layer`
(see AGENTS.md at the bundle root): source, summary, compiled, mine or claude chat.

Nothing here writes to the notes; only _tools/search.db is (re)written.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

VERSION = "1.0"
HERE = Path(__file__).resolve().parent
SETTINGS = {
    "passage_words": 250, "max_words": 350, "overlap_words": 40, "min_words": 8, "desc_words": 40,
    "weights": {"title": 10.0, "heads": 4.0, "meta": 2.0, "body": 1.0, "seealso": 0.5},
    "rrf_k": 30, "kw_weight": 0.75, "vec_weight": 1.0, "original_weight": 2.0, "list_size": 50, "per_note": 3,
    "k": 10, "snippet_words": 60, "get_max_bytes": 40000,
    "model": "BAAI/bge-small-en-v1.5",
    "query_prefix": "Represent this sentence for searching relevant passages: ",
    "skip_files": ["index.md", "log.md", "AGENTS.md", "CLAUDE.md"],
    "skip_types": ["Topic Map", "Tag Vocabulary", "Agent Instructions", "index"],
    "mine_headings": ["My notes", "Your notes", "My thoughts", "My comments"]
                     + ([os.environ["VAULT_OWNER_NAME"].strip() + "'s notes"] if os.environ.get("VAULT_OWNER_NAME", "").strip() else []),
    "list_headings": {"Concept": ["Discussed in"], "Analysis": ["Related", "Later items that refer to this", "Parts"]},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS notes(path TEXT PRIMARY KEY, name TEXT, title TEXT, type TEXT, layer TEXT, folder TEXT,
  description TEXT, tags TEXT, aliases TEXT, verified INTEGER, status TEXT, analysis_of TEXT, original TEXT);
CREATE TABLE IF NOT EXISTS passages(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, kind TEXT, layer TEXT,
  heads TEXT, line_start INTEGER, line_end INTEGER, text TEXT, header TEXT, thash TEXT);
CREATE INDEX IF NOT EXISTS passages_path ON passages(path);
CREATE TABLE IF NOT EXISTS vecs(thash TEXT PRIMARY KEY, v BLOB);
"""
FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, heads, meta, body, seealso, content='', "
       "tokenize='porter unicode61')")    # contentless: the text is kept once, in passages
STOP = set("""a about above after again all also am an and any are as at be been before being below between both but
by can could did do does doing down during each few for from further had has have having he her here hers him his
how i if in into is it its itself just me more most my no nor not now of off on once only or other our ours out over
own same she should so some such than that the their theirs them then there these they this those through to too
under until up very was we were what when where which while who whom why will with would you your yours tell
please find show give know say said think""".split())
FM = re.compile(r"(?s)^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)")
HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
MDLINK = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?\s*\)")
ORIG_EXT = (".pdf",)
_model = None


# ---------------------------------------------------------------- bundle, database
def bundle_root(arg=None):
    if arg:
        return Path(arg).resolve()
    return HERE.parent if HERE.name == "_tools" else Path.cwd()


def db_file(root):
    return root / "_tools" / "search.db"


def has_fts5():
    try:
        c = sqlite3.connect(":memory:")
        c.execute(FTS)
        c.close()
        return True
    except sqlite3.Error:
        return False


def connect(root, create=False):
    p = db_file(root)
    if not p.exists() and not create:
        print("search index missing: building it now (a minute or two) ...", file=sys.stderr)
        build(root, quiet=True)
    con = sqlite3.connect(str(p))
    con.executescript(SCHEMA)
    if has_fts5():
        con.execute(FTS)
    return con


def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, str(v)))


# ---------------------------------------------------------------- notes
def parse_fm(raw):
    """(frontmatter dict, body, lines before body). PyYAML if present, else a small reader for the fields used."""
    m = FM.match(raw)
    if not m:
        return {}, raw, 0
    block = m.group(1)
    fm = None
    try:
        import yaml
        fm = yaml.safe_load(block)
    except Exception:  # noqa: BLE001  (no PyYAML, or odd YAML: fall back)
        fm = None
    if not isinstance(fm, dict):
        fm, key = {}, None
        for line in block.splitlines():
            if re.match(r"^\s+-\s+", line) and key:
                fm.setdefault(key, [])
                if isinstance(fm[key], list):
                    fm[key].append(line.split("-", 1)[1].strip().strip("'\""))
                continue
            mm = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if not mm:
                continue
            key, v = mm.group(1), mm.group(2).strip()
            if v.startswith("[") and v.endswith("]"):
                fm[key] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            elif v == "":
                fm[key] = []
            else:
                fm[key] = v.strip("'\"")
    return fm, raw[m.end():], raw[:m.end()].count("\n")


def aslist(v):
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


def link_label(v):
    m = MDLINK.search(str(v))
    return m.group(2) if m else str(v)


def clean(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = MDLINK.sub(lambda m: m.group(2), text)
    text = re.sub(r"https?://\S+", " ", text)
    return text


def iter_notes(root):
    for dp, dirs, files in os.walk(root):
        rel = Path(dp).relative_to(root).parts
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (not rel and d == "_tools"))
        for f in sorted(files):
            if f.endswith(".md") and f not in SETTINGS["skip_files"]:
                yield Path(dp) / f


def split_passages(body, off, title, desc, ntype, layer):
    s = SETTINGS
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
            if len(w) > mx:
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
        for a, b, w in chunks:
            if len(w) < mn and out:
                continue
            out.append({"kind": kind, "layer": lay, "heads": hp, "ls": a, "le": b, "text": " ".join(w),
                        "header": header})

    infence = False
    for i, line in enumerate(body.split("\n")):
        line = line.rstrip("\r")
        if FENCE.match(line):
            infence = not infence
        h = None if infence else HEAD.match(line)
        if h:
            flush()
            sec = []
            lvl = len(h.group(1))
            heads = heads[:lvl - 1] + [""] * max(0, lvl - 1 - len(heads)) + [h.group(2).strip()]
        else:
            sec.append((off + i + 1, line))
    flush()
    return out


def original_of(root, body):
    """Bundle path of the first original (PDF in attachments/Sources) the note links to and the bundle holds."""
    for m in MDLINK.finditer(body):
        href = unquote(m.group(3).split("#")[0]).lstrip("/")
        if "attachments/Sources/" in href and href.lower().endswith(ORIG_EXT) and (root / href).is_file():
            return href
    return ""


def pdf_pages(path):
    """List of page texts, or None if no extractor is available."""
    if shutil.which("pdftotext"):
        try:
            r = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"], capture_output=True, timeout=600)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace").split("\f")
        except Exception:  # noqa: BLE001
            pass
    try:
        from pypdf import PdfReader
        return [(pg.extract_text() or "") for pg in PdfReader(str(path)).pages]
    except Exception:  # noqa: BLE001
        return None


def original_passages(root, href):
    pages = pdf_pages(root / href)
    if not pages:
        return []
    out, pw = [], SETTINGS["passage_words"]
    for n, t in enumerate(pages, 1):
        w = t.split()
        for i in range(0, len(w), pw):
            piece = w[i:i + pw]
            if len(piece) >= SETTINGS["min_words"]:
                out.append({"kind": "original", "layer": "source", "heads": f"Original > p. {n}", "ls": n, "le": n,
                            "text": " ".join(piece), "header": ""})
    return out


def build(root, quiet=False, originals=True):
    p = db_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    con = sqlite3.connect(str(p))
    con.executescript(SCHEMA)
    fts = has_fts5()
    if fts:
        con.execute(FTS)
    n_notes = n_pass = n_orig = 0
    for path in iter_notes(root):
        rel = path.relative_to(root).as_posix()
        raw = path.read_text(encoding="utf-8", errors="replace")
        fm, body, off = parse_fm(raw)
        ntype = str(fm.get("type") or "")
        if ntype in SETTINGS["skip_types"]:
            continue
        name, title = path.stem, str(fm.get("title") or path.stem)
        desc = str(fm.get("description") or "")
        layer = str(fm.get("layer") or "source")
        tags, aliases = aslist(fm.get("tags")), aslist(fm.get("aliases"))
        meta = aslist(fm.get("author")) + tags + [link_label(x) for x in aslist(fm.get("concepts"))]
        see_also = [link_label(x) for x in aslist(fm.get("see_also"))]
        an = aslist(fm.get("analysis_of"))
        orig = original_of(root, body) if originals else ""
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        con.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rel, name, title, ntype, layer, folder, desc, json.dumps(tags), json.dumps(aliases),
                     int(bool(fm.get("verified"))), str(fm.get("status") or ""), link_label(an[0]) if an else "", orig))
        ps = split_passages(body, off, title, desc, ntype, layer) or \
            [{"kind": "text", "layer": layer, "heads": "", "ls": 1, "le": max(1, off), "header": title,
              "text": desc or title}]
        my_note = " ".join(str(fm.get("my_note") or "").split())
        if my_note:
            ps.append({"kind": "mynote", "layer": "mine", "heads": "", "ls": 1, "le": max(1, off), "header": "",
                       "text": f"Owner's note on {title}: {my_note}"})
        if orig:
            op = original_passages(root, orig)
            n_orig += len(op)
            ps += op
        tfield, mfield, sfield = " ".join([name, title] + aliases), " ".join(meta), " ".join(see_also)
        for i, q in enumerate(ps):
            et = f"{q['header']}\n{q['text']}" if q["header"] else q["text"]
            th = None if q["kind"] == "list" else hashlib.sha1(et.encode("utf-8")).hexdigest()
            cur = con.execute("INSERT INTO passages(path, ord, kind, layer, heads, line_start, line_end, text, header, "
                              "thash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (rel, i, q["kind"], q["layer"], q["heads"], q["ls"], q["le"], q["text"], q["header"], th))
            if fts:
                con.execute("INSERT INTO fts(rowid, title, heads, meta, body, seealso) VALUES(?,?,?,?,?,?)",
                            (cur.lastrowid, tfield, q["heads"] if q["kind"] != "original" else "Original",
                             mfield, q["text"], sfield))
        n_notes += 1
        n_pass += len(ps)
    set_meta(con, "built", __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"))
    set_meta(con, "version", VERSION)
    con.commit()
    con.execute("VACUUM")
    con.close()
    if not quiet:
        print(f"indexed {n_notes:,} notes, {n_pass:,} passages ({n_orig:,} from original PDFs)"
              + ("" if fts else "; SQLite FTS5 not available, so keyword search uses a slower plain scan"))


# ---------------------------------------------------------------- search
def tokens(q):
    return [w for w in re.findall(r"[A-Za-z0-9]+", q.lower()) if w not in STOP and len(w) > 1]


def concept_terms(con):
    out = []
    for name, title, aliases in con.execute("SELECT name, title, aliases FROM notes WHERE type='Concept'"):
        names = list(dict.fromkeys([title, name] + json.loads(aliases or "[]")))
        out += [(t.lower(), names) for t in names if len(t) >= 3]
    return out


def kw_search(con, q, allowed, concepts):
    s = SETTINGS
    terms = [f'"{w}"' for w in tokens(q)]
    ql = " " + " ".join(re.findall(r"[a-z0-9]+", q.lower())) + " "
    for term, names in concepts:
        tl = " " + " ".join(re.findall(r"[a-z0-9]+", term)) + " "
        if tl.strip() and tl in ql:
            terms += ['"' + n.replace('"', '""') + '"' for n in names]
    if not terms:
        return []
    if con.execute("SELECT name FROM sqlite_master WHERE name='fts'").fetchone():
        w = s["weights"]
        rows = con.execute(f"SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, {w['title']}, {w['heads']}, "
                           f"{w['meta']}, {w['body']}, {w['seealso']}) LIMIT 2000",
                           (" OR ".join(dict.fromkeys(terms)),)).fetchall()
        ids = [r[0] for r in rows]
    else:   # no FTS5: plain term counts
        tt = [t.strip('"').lower() for t in terms]
        sc = []
        for pid, text, title in con.execute("SELECT p.id, p.text, n.title FROM passages p JOIN notes n ON n.path=p.path"):
            tl, bl = title.lower(), text.lower()
            v = sum(10 * tl.count(t) + bl.count(t) for t in tt)
            if v:
                sc.append((-v, pid))
        ids = [pid for _v, pid in sorted(sc)[:2000]]
    return [i for i in ids if allowed is None or i in allowed][:s["list_size"]]


def vectors_ready(con):
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return con.execute("SELECT 1 FROM vecs LIMIT 1").fetchone() is not None


def model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=SETTINGS["model"])
    return _model


def embed(texts):
    import numpy as np
    v = np.array(list(model().embed(texts, batch_size=16)), dtype="float32")
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


def vec_search(con, q, allowed):
    import numpy as np
    rows = con.execute("SELECT p.id, v.v FROM passages p JOIN vecs v ON v.thash=p.thash").fetchall()
    if not rows:
        return []
    ids = np.array([r[0] for r in rows], dtype="int64")
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype="float32").reshape(len(rows), -1)
    sims = mat @ embed([SETTINGS["query_prefix"] + q])[0]
    if allowed is not None:
        sims = np.where(np.isin(ids, np.fromiter(allowed, dtype="int64")), sims, -9.0)
    n = min(SETTINGS["list_size"], len(ids))
    top = np.argpartition(-sims, n - 1)[:n]
    return [int(ids[i]) for i in top[np.argsort(-sims[top])] if sims[i] > -9.0]


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
    w, toks, start = text.split(), [t[:5] for t in tokens(q)], 0
    for i, x in enumerate(w):
        if any(re.sub(r"[^a-z0-9]", "", x.lower()).startswith(t) for t in toks if t):
            start = max(0, i - 15)
            break
    return ("... " if start else "") + " ".join(w[start:start + n]) + (" ..." if start + n < len(w) else "")


def href(path, heading=None):
    from urllib.parse import quote
    return "/" + quote(path, safe="/-_.~!$&'*+,;=:@") + ("#" + quote(heading, safe="-_.~") if heading else "")


def search(root, question, variants=None, k=None, folder=None, ntype=None, layer=None, tag=None, con=None):
    s = SETTINGS
    k = int(k or s["k"])
    own = con is None
    con = con or connect(root)
    try:
        allowed = allowed_ids(con, folder, ntype, layer, tag)
        use_vec = vectors_ready(con)
        concepts = concept_terms(con)
        lists = []
        for j, q in enumerate([question] + list(variants or [])):
            w, tag_ = (s["original_weight"], "") if j == 0 else (1.0, f" (variant {j})")
            lists.append((w * s["kw_weight"], "keyword" + tag_, kw_search(con, q, allowed, concepts)))
            if use_vec:
                lists.append((w * s["vec_weight"], "meaning" + tag_, vec_search(con, q, allowed)))
        score, found = {}, {}
        for w, name, pids in lists:
            for r, pid in enumerate(pids, 1):
                score[pid] = score.get(pid, 0.0) + w / (s["rrf_k"] + r)
                found.setdefault(pid, []).append(f"{name} #{r}")
        info = {}
        if score:
            qm = ",".join("?" * len(score))
            info = {pid: (path, kind) for pid, path, kind in
                    con.execute(f"SELECT id, path, kind FROM passages WHERE id IN ({qm})", list(score))}
        for pid in [p for p in score if info[p][1] == "mynote"]:   # owner's note counts toward the item
            path = info[pid][0]
            b, f = score.pop(pid), found.pop(pid)
            cands = [p for p in score if info[p][0] == path]
            if cands:
                best = max(cands, key=score.get)
            else:
                r = con.execute("SELECT id FROM passages WHERE path=? AND kind!='mynote' ORDER BY ord LIMIT 1",
                                (path,)).fetchone()
                if not r:
                    continue
                best = r[0]
                info[best], score[best] = (path, "text"), 0.0
            score[best] += b
            found.setdefault(best, []).append("owner's note")
        ranked, per, shown_lists = [], {}, set()
        for pid in sorted(score, key=lambda p: -score[p]):
            path, kind = info[pid]
            if per.get(path, 0) >= s["per_note"] or (kind == "list" and path in shown_lists):
                continue
            if kind == "list":
                shown_lists.add(path)
            per[path] = per.get(path, 0) + 1
            ranked.append(pid)
            if len(ranked) >= k:
                break
        return {"query": question, "meaning_search": use_vec, "index_built": get_meta(con, "built"),
                "results": [result(con, pid, score[pid], found[pid], question) for pid in ranked]}
    finally:
        if own:
            con.close()


def result(con, pid, sc, found, q):
    path, heads, ls, le, text, layer, kind = con.execute(
        "SELECT path, heads, line_start, line_end, text, layer, kind FROM passages WHERE id=?", (pid,)).fetchone()
    name, title, ntype, folder, analysis_of, verified, status = con.execute(
        "SELECT name, title, type, folder, analysis_of, verified, status FROM notes WHERE path=?", (path,)).fetchone()
    section = heads.split(" > ")[-1] if heads else ""
    r = {"note": name, "path": path, "section": heads, "type": ntype, "layer": layer, "folder": folder,
         "lines": f"{ls}-{le}", "found_by": found, "score": round(sc, 4),
         "snippet": snippet(text, q, SETTINGS["snippet_words"])}
    if kind == "original":
        r.pop("lines")
        r["from_original"] = True
        r["page"] = ls
        r["cite"] = f"[{name}]({href(path)}), original p. {ls}"
    else:
        r["cite"] = f"[{name} > {section}]({href(path, section)})" if section else f"[{name}]({href(path)})"
    if verified:
        r["verified"] = True
    if status:
        r["status"] = status
    if analysis_of:
        r["analysis_of"] = analysis_of
    an = con.execute("SELECT path FROM notes WHERE analysis_of = ? COLLATE NOCASE AND path != ? LIMIT 1",
                     (name, path)).fetchone()
    if an:
        r["analysis"] = an[0]
    return r


# ---------------------------------------------------------------- get
def resolve(con, ref):
    m = MDLINK.search(ref)
    ref = unquote(m.group(3) if m else ref).strip()      # a pasted cite: use its path
    ref = ref.split("#")[0].strip().lstrip("/")
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


def get(root, note, lines=None, heading=None, pages=None, full=False, con=None):
    own = con is None
    con = con or connect(root)
    try:
        if heading is None and "#" in note and not note.strip().startswith("#"):
            heading = unquote(re.sub(r"[)\]|].*$", "", note.split("#", 1)[1])).strip() or None
        rel = resolve(con, note)
        if not rel:
            return {"error": f"no note named {note!r} in this bundle"}
        if pages:
            m = re.match(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$", str(pages))
            if not m:
                return {"error": "pages must look like 37 or 37-40"}
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            if b - a > 30:
                return {"error": "at most 30 pages at a time"}
            rows = con.execute("SELECT line_start, text FROM passages WHERE path=? AND kind='original' AND "
                               "line_start BETWEEN ? AND ? ORDER BY ord", (rel, a, b)).fetchall()
            if not rows:
                return {"error": f"no original text for {rel} at pages {pages} (original not included in this export?)"}
            return {"note": Path(rel).stem, "path": rel, "original": True, "pages": f"{a}-{b}",
                    "text": "\n\n".join(f"[p. {n}]\n{t}" for n, t in rows)}
    finally:
        if own:
            con.close()
    L = (root / rel).read_text(encoding="utf-8", errors="replace").split("\n")
    a, b = 1, len(L)
    if lines:
        m = re.match(r"^\s*(\d+)\s*[:+]\s*(\d+)\s*$", lines) or re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", lines)
        if not m:
            return {"error": "lines must look like 120:40 (start:count) or 120-160"}
        a = int(m.group(1))
        b = a + int(m.group(2)) - 1 if (":" in lines or "+" in lines) else int(m.group(2))
    elif heading:
        hl, start, lvl = heading.strip().lower(), None, None
        for i, line in enumerate(L, 1):
            h = HEAD.match(line.rstrip("\r"))
            if h and start is None and h.group(2).strip().lower() == hl:
                start, lvl = i, len(h.group(1))
            elif h and start is not None and len(h.group(1)) <= lvl:
                b = i - 1
                break
        if start is None:
            return {"error": f"no heading {heading!r} in {rel}",
                    "headings": [x.rstrip("\r") for x in L if HEAD.match(x.rstrip("\r"))][:80]}
        a = start
    a, b = max(1, a), min(len(L), b)
    text = "\n".join(x.rstrip("\r") for x in L[a - 1:b])
    if not (lines or heading) and len(text.encode("utf-8")) > SETTINGS["get_max_bytes"] and not full:
        return {"error": f"{rel} is {len(text.encode('utf-8')) // 1024} KB; ask for lines or a heading (or full)",
                "path": rel, "lines_total": len(L),
                "headings": [f"{i}: {x.rstrip(chr(13))}" for i, x in enumerate(L, 1) if HEAD.match(x.rstrip("\r"))][:120]}
    return {"note": Path(rel).stem, "path": rel, "lines": f"{a}-{b}", "lines_total": len(L), "text": text}


def stats(root):
    con = connect(root)
    out = {"index_built": get_meta(con, "built"), "notes": con.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
           "passages": con.execute("SELECT COUNT(*) FROM passages").fetchone()[0],
           "original_passages": con.execute("SELECT COUNT(*) FROM passages WHERE kind='original'").fetchone()[0],
           "vectors": con.execute("SELECT COUNT(*) FROM vecs").fetchone()[0],
           "fts5": bool(con.execute("SELECT name FROM sqlite_master WHERE name='fts'").fetchone()),
           "by_layer": dict(con.execute("SELECT layer, COUNT(*) FROM notes GROUP BY layer").fetchall())}
    con.close()
    return out


def embed_all(root):
    con = connect(root)
    rows = con.execute("SELECT p.thash, MIN(p.header), MIN(p.text) FROM passages p LEFT JOIN vecs v ON v.thash=p.thash "
                       "WHERE p.thash IS NOT NULL AND v.thash IS NULL GROUP BY p.thash").fetchall()
    print(f"embedding {len(rows):,} passages with {SETTINGS['model']} ...")
    for i in range(0, len(rows), 256):
        part = rows[i:i + 256]
        v = embed([f"{h}\n{t}" if h else t for _th, h, t in part])
        con.executemany("INSERT OR REPLACE INTO vecs(thash, v) VALUES(?, ?)",
                        [(th, v[j].tobytes()) for j, (th, _h, _t) in enumerate(part)])
        con.commit()
        print(f"  {min(i + 256, len(rows)):,}/{len(rows):,}")
    con.close()


# ---------------------------------------------------------------- MCP (stdio, JSON-RPC 2.0, read-only)
def guide_text(root):
    p = root / "AGENTS.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


TOOLS = [
    {"name": "vault_search",
     "description": "Search this knowledge bundle. Pass the question plus 1-3 variants you write (names, exact "
                    "phrases, rewordings). Returns passages with cite, path, section, type, layer "
                    "(source/summary/compiled/mine/claude chat) and a snippet. Filters: folder, type, layer, tag.",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"}, "variants": {"type": "array", "items": {"type": "string"}},
         "k": {"type": "integer", "default": 10}, "folder": {"type": "string"}, "type": {"type": "string"},
         "layer": {"type": "string", "enum": ["source", "summary", "compiled", "mine", "claude chat"]},
         "tag": {"type": "string"}}, "required": ["question"]}},
    {"name": "vault_get",
     "description": "Read part of a note: by heading, by lines (120:40 or 120-160), or the original PDF's pages "
                    "(pages='37-40', for results marked from_original). Whole notes only when short (or full=true).",
     "inputSchema": {"type": "object", "properties": {
         "note": {"type": "string", "description": "note name, title or bundle path; 'Note#Heading' also works"},
         "heading": {"type": "string"}, "lines": {"type": "string"}, "pages": {"type": "string"},
         "full": {"type": "boolean"}}, "required": ["note"]}},
]


def mcp(root):
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    instructions = ("Read-only search over an exported OKF knowledge bundle. Search first with vault_search "
                    "(question plus variants), read only what you need with vault_get, and cite with the cite "
                    "field. Facts come from 'source' and 'mine' passages; 'summary' is a summary of a source; "
                    "'compiled' (analyses, concepts) supports synthesis; 'claude chat' is never a source. "
                    "Full rules: AGENTS.md at the bundle root.")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue                                   # notifications need no reply
        try:
            if method == "initialize":
                res = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "okf-bundle-search", "version": VERSION},
                       "instructions": instructions}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                a = params.get("arguments") or {}
                if params.get("name") == "vault_search":
                    out = search(root, a["question"], a.get("variants"), a.get("k"), a.get("folder"),
                                 a.get("type"), a.get("layer"), a.get("tag"))
                elif params.get("name") == "vault_get":
                    out = get(root, a["note"], a.get("lines"), a.get("heading"), a.get("pages"), bool(a.get("full")))
                else:
                    raise ValueError(f"unknown tool {params.get('name')}")
                res = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}],
                       "isError": "error" in out}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as e:  # noqa: BLE001
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}],
                                                          "isError": True}})


# ---------------------------------------------------------------- command line
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bundle", help="bundle root (default: the folder above _tools)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--no-originals", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("question")
    s.add_argument("--variant", action="append", default=[])
    s.add_argument("-k", type=int, default=10)
    for f in ("folder", "type", "layer", "tag"):
        s.add_argument("--" + f)
    s.add_argument("--json", action="store_true")
    g = sub.add_parser("get")
    g.add_argument("note")
    g.add_argument("--lines")
    g.add_argument("--heading")
    g.add_argument("--pages")
    g.add_argument("--full", action="store_true")
    for c in ("stats", "mcp", "embed"):
        sub.add_parser(c)
    a = ap.parse_args(argv)
    root = bundle_root(a.bundle)
    if a.cmd == "build":
        build(root, originals=not a.no_originals)
    elif a.cmd == "search":
        out = search(root, a.question, a.variant, a.k, a.folder, a.type, a.layer, a.tag)
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            for i, r in enumerate(out["results"], 1):
                print(f"{i:2}. {r['cite']}  [{r['layer']}; {r['type'] or 'untyped'}]\n    {r['snippet']}\n")
    elif a.cmd == "get":
        out = get(root, a.note, a.lines, a.heading, a.pages, a.full)
        print(out["text"] if "text" in out else json.dumps(out, ensure_ascii=False, indent=1))
    elif a.cmd == "stats":
        print(json.dumps(stats(root), indent=1))
    elif a.cmd == "embed":
        embed_all(root)
    elif a.cmd == "mcp":
        mcp(root)


if __name__ == "__main__":
    main()
