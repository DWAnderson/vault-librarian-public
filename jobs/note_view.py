"""Read-only views of the notes and files named in a day's organize questions (2026-10-01).

The owner asked to see the items behind a "duplicate" or "already in the vault?" question from the answer page.
vault_server.py serves, for /organize/<day>?t=<web_token>:
  <day>/note?t=&p=<vault path>       one note, rendered read-only (with "Open in Obsidian")
  <day>/compare?t=&q=<question id>   the two items side by side; paragraphs found in only one are marked
  <day>/original?t=&p=<vault path>   the file the note was made from (its PDF in attachments/Sources)
  <day>/dropbox?t=&q=<question id>   the file in the Remember dropbox that an intake question is about
Only paths named in that day's proposal (reports/organize-<day>.json) can be opened, and only with its
web_token. A note that has moved since (filed, renamed, archived) is found again by name.
The daily email (update_email.py) links to the same pages.
"""
import html
import json
import mimetypes
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

sys.path.insert(0, str(C.VL))
from vaultlib import split_frontmatter  # noqa: E402

SKIP_DIRS = {".obsidian", ".trash", ".git", ".smart-env", ".sync-backups", "_snapshots", "node_modules"}
MAX_BLOCKS = 600            # paragraphs shown before "Show the rest"

CSS = ("<style>*{box-sizing:border-box}body{overflow-wrap:anywhere;font:12pt/1.5 Aptos,'Aptos Display',Calibri,'Segoe UI',"
       "system-ui,sans-serif;background:#f3f2f1;margin:0;color:#242424}.hd{background:#483d8b;color:#fff;padding:14px 20px}"
       ".hd small{font-size:9pt;letter-spacing:1px;text-transform:uppercase}.hd b{display:block;font-size:15pt}"
       ".hd a{color:#fff}.wrap{max-width:900px;margin:0 auto;padding:0 12px 40px}.wide{max-width:1500px}"
       ".card{background:#fff;border:1px solid #e1dfdd;border-radius:4px;margin:12px 0;padding:12px 16px}"
       ".meta{font-size:10.5pt;color:#616161}.meta td{padding:1px 10px 1px 0;vertical-align:top}.meta td:first-child{color:#8a8886;white-space:nowrap}"
       ".acts a{display:inline-block;margin:6px 14px 0 0;font-size:10.5pt}a{color:#483d8b}"
       ".grid{display:grid;grid-template-columns:1fr;gap:12px}@media(min-width:900px){.grid{grid-template-columns:1fr 1fr}"
       ".grid .body{max-height:78vh;overflow:auto}}.col h3{margin:0 0 2px 0;font-size:10pt;letter-spacing:1px;"
       "text-transform:uppercase;color:#8a8886}.col h2{margin:0 0 6px 0;font-size:14pt}"
       ".body{border-top:1px solid #edebe9;margin-top:8px;padding-top:6px}.only{background:#fff4ce;border-left:3px solid #c19c00;"
       "padding:2px 8px;margin-left:-11px}.tagonly{font-size:8.5pt;color:#7a5a00;font-weight:bold}"
       "pre{white-space:pre-wrap;background:#f6f6f6;padding:8px;font-size:10pt}blockquote{border-left:3px solid #c8c6c4;"
       "margin:8px 0;padding:2px 10px;color:#424242}.wl{color:#483d8b;font-weight:600}.emb{color:#8a8886}"
       ".sum{font-size:11pt}.muted{color:#8a8886;font-size:10pt}h1,h2,h3,h4{line-height:1.25}</style>")


# ------------------------------------------------------------------ what may be opened
def allowed_notes(prop):
    """Vault-relative paths named anywhere in the day's proposal."""
    s = set()
    for it in prop.get("items", []):
        s.add(it["path"])
    for q in prop.get("questions", []):
        s.update(q.get("paths", []))
    for q in prop.get("dups", []):
        s.update(x for x in (q.get("path"), q.get("keeper")) if x)
        s.update(q.get("paths", []))
    for nf in prop.get("new_folders", []):   # the per-note list in a folder card (2026-10-13: these 404'd)
        s.update(nf.get("paths", []))
    for q in prop.get("queue", []):
        if str(q.get("match") or "").lower().endswith(".md"):
            s.add(q["match"])
        if str(q.get("where") or "").startswith("vault: "):
            s.add(q["where"][7:])
    return s


def question(prop, qid):
    for q in prop.get("dups", []) + prop.get("queue", []) + prop.get("held", []):
        if str(q.get("id")) == str(qid):
            return q
    return None


def _inside(base, p):
    try:
        return Path(base).resolve() in Path(p).resolve().parents
    except OSError:
        return False


def _clean(n):
    try:
        import organize
        return organize._clean_name(n)
    except Exception:  # noqa: BLE001
        return n[:-3] if n.lower().endswith(".md") else n


def find_note(vault, rel, prop=None):
    """The note now: at its path, else found by its name (or the name it was renamed to) anywhere in the vault."""
    vault = Path(vault)
    p = vault / rel
    if p.is_file() and _inside(vault, p):
        return p
    if "Archives/" in rel:
        return None     # an archived copy often shares its kept note's name: never show the other one instead
    try:                                    # renamed by vault_names (2026-10-10): follow the rename ledger
        import vault_names as VN
        now = VN.follow(rel)
        if now != rel and (vault / now).is_file():
            return vault / now
        st = Path(rel).stem
        want = {Path(rel).name.lower()} | {f"{x}.md".lower() for x in VN.new_forms(st)}
    except Exception:  # noqa: BLE001
        want = {Path(rel).name.lower()}
    for it in (prop or {}).get("items", []):
        if it.get("path") == rel and it.get("rename_to"):
            want.add(_clean(it["rename_to"]).lower() + ".md")
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.lower() in want:
                return Path(root) / f
    return None


EMBED_RE = re.compile(r"!\[\[([^\]|#]+\.(?:pdf|docx?|epub|mobi|azw3?|txt|html?|mhtml?|rtf))(?:[#|][^\]]*)?\]\]", re.I)


def original_of(vault, fm, body=None):
    """The file a note was made from: its `resource` when that is a vault file, else attachments/Sources/<source_file>,
    else (2026-10-12, e.g. a resource that is an ISBN) the first embedded document in the body."""
    vault = Path(vault)
    res = str(fm.get("resource") or "")
    cands = []
    if res and "://" not in res and ":" not in res.split("/", 1)[0]:
        cands.append(vault / res)
    if fm.get("source_file"):
        cands.append(C.vpath(vault, "attachments") / "Sources" / str(fm["source_file"]))
    for m in EMBED_RE.finditer(body or ""):
        e = m.group(1).strip()
        cands += [vault / e, C.vpath(vault, "attachments") / "Sources" / e.rsplit("/", 1)[-1]]
        break
    for c in cands:
        if c.is_file() and _inside(vault, c):
            return c
    return None


def dropbox_file(cfg, q):
    """(path or None, why) of the dropbox file behind an intake-queue question."""
    import vqueue as Q
    try:
        item = Q.Store(cfg).load(q["item"])
    except Exception:  # noqa: BLE001
        return None, "its queue record can't be read"
    ref = item.get("source_ref") or {}
    root = Path((Q.qcfg(cfg).get("dropbox_path") or "").strip() or "\0")
    if item.get("source") != "folder" or not ref.get("path"):
        return None, "it isn't a file in the dropbox"
    f = root / ref["path"]
    if not (root.is_absolute() and f.is_file() and _inside(root, f)):
        return None, "it is no longer in the dropbox"
    return f, ""


def dropbox_text(cfg, q):
    """(extract.json dict, text) the queue extracted from that file, or ({}, "")."""
    import vqueue as Q
    pay = Q.Store(cfg).payload_root / str(q.get("item"))
    try:
        doc = json.loads((pay / "extract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    try:
        text = (pay / "text.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return doc, text


# ------------------------------------------------------------------ Markdown -> HTML (enough to read a note)
def inline(s):
    s = html.escape(s)
    s = re.sub(r"!\[\[([^\]]+)\]\]", r"<span class=emb>[embedded: \1]</span>", s)
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"<span class=wl>\2</span>", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"<span class=wl>\1</span>", s)
    s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", r"<span class=emb>[image: \1]</span>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<a href='\2' target=_blank rel=noopener>\1</a>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
    return s


def blocks(body):
    """Paragraph-sized blocks (blank-line separated; a fenced code block is one block)."""
    out, buf, fence = [], [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            if not fence and buf:
                out.append("\n".join(buf))
                buf = []
            buf.append(line)
            fence = not fence
            if not fence:
                out.append("\n".join(buf))
                buf = []
            continue
        if not fence and not line.strip():
            if buf:
                out.append("\n".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return out


def render_block(b):
    lines = b.split("\n")
    first = lines[0].lstrip()
    if first.startswith("```"):
        inner = lines[1:-1] if len(lines) > 1 and lines[-1].strip().startswith("```") else lines[1:]
        return f"<pre>{html.escape(chr(10).join(inner))}</pre>"
    m = re.match(r"(#{1,6})\s+(.*)", first)
    if m:
        n = min(len(m.group(1)) + 1, 6)
        rest = "\n".join(lines[1:]).strip()
        return f"<h{n}>{inline(m.group(2))}</h{n}>" + (render_block(rest) if rest else "")
    if re.fullmatch(r"\s*([-*_]\s*){3,}", b):
        return "<hr>"
    if all(re.match(r"\s*([-*+]|\d+[.)])\s+", ln) or ln.startswith("  ") for ln in lines):
        items = []
        for ln in lines:
            mm = re.match(r"\s*([-*+]|\d+[.)])\s+(.*)", ln)
            if mm:
                items.append(inline(mm.group(2)))
            elif items:
                items[-1] += " " + inline(ln.strip())
        tag = "ol" if re.match(r"\d", first) else "ul"
        return f"<{tag}>" + "".join(f"<li>{x}</li>" for x in items) + f"</{tag}>"
    if all(ln.lstrip().startswith(">") for ln in lines):
        inner = [re.sub(r"^\s*>\s?", "", ln) for ln in lines]
        cm = re.match(r"\[!(\w+)\][-+]?\s*(.*)", inner[0])
        head = ""
        if cm:
            head, inner = f"<b>{html.escape(cm.group(2) or cm.group(1).title())}</b><br>", inner[1:]
        return f"<blockquote>{head}{'<br>'.join(inline(x) for x in inner)}</blockquote>"
    if first.startswith("|"):
        return f"<pre>{html.escape(b)}</pre>"
    return "<p>" + "<br>".join(inline(ln) for ln in lines) + "</p>"


def pkey(b):
    """Paragraph key, as resolve_duplicates.py compares them."""
    return " ".join(re.sub(r"[^\w\s]|_", " ", b.lower()).split())


# ------------------------------------------------------------------ page pieces
def qs(t, **kw):
    return "?" + urlencode({"t": t, **kw})


def shell(title, inner, back, wide=False):
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=robots content=noindex><title>{html.escape(title)}</title>{CSS}"
            f"<div class=hd><small><a href='{html.escape(back)}'>&larr; Back to the questions</a></small>"
            f"<b>{html.escape(title)}</b></div><div class='wrap{' wide' if wide else ''}'>{inner}</div>")


def _meta_rows(fm, keys=("type", "author", "published", "publication", "status", "duplicate_of", "archived_from",
                         "source_file", "description")):
    rows = []
    for k in keys:
        v = fm.get(k)
        if v in (None, "", []):
            continue
        v = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        rows.append(f"<tr><td>{k.replace('_', ' ')}</td><td>{inline(v)}</td></tr>")
    return f"<table class=meta>{''.join(rows)}</table>" if rows else ""


def note_side(cfg, prop, t, rel, label, alone=False):
    """dict(label, title, meta, acts, blocks) for one vault note, or None if it can't be found."""
    vault = Path(cfg["vault_path"])
    p = find_note(vault, rel, prop)
    if not p:
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    fm, _, body = split_frontmatter(text)
    fm = fm if isinstance(fm, dict) else {}
    now = p.relative_to(vault).as_posix()
    folder = now.rsplit("/", 1)[0] if "/" in now else "(vault root)"
    meta = f"<div class=meta>In <b>{html.escape(folder)}</b>" + ("" if now == rel else " (moved since the question was asked)") + "</div>"
    meta += _meta_rows(fm)
    acts = [f"<a href='obsidian://open?vault={quote(vault.name)}&amp;file={quote(now[:-3])}'>Open in Obsidian</a>"]
    res = str(fm.get("resource") or "")
    if original_of(vault, fm):
        acts.append(f"<a href='original{html.escape(qs(t, p=rel))}' target=_blank>Open the original file</a>")
    if res.startswith(("http://", "https://")):
        acts.append(f"<a href='{html.escape(res)}' target=_blank rel=noopener>Web page</a>")
    if not alone:      # on a compare page: a link to the single-note view
        acts.append(f"<a href='note{html.escape(qs(t, p=rel))}' target=_blank>This note alone</a>")
    ftitle = str(fm.get("title") or "").strip()
    return {"label": label, "title": p.stem, "sub": f"title property: {ftitle}" if ftitle and ftitle != p.stem else "",
            "meta": meta, "acts": acts, "blocks": blocks(body)}


def dropbox_side(cfg, t, q):
    f, why = dropbox_file(cfg, q)
    doc, text = dropbox_text(cfg, q)
    meta = f"<div class=meta>{html.escape(q.get('where') or '')}" + (f" &middot; {html.escape(q['size'])}" if q.get("size") else "") + "</div>"
    if doc.get("title") and doc["title"] != q.get("title"):
        meta += f"<div class=meta>Title inside the file: {html.escape(str(doc['title']))}</div>"
    acts = [f"<a href='dropbox{html.escape(qs(t, q=q['id']))}' target=_blank>Open the file</a>"] if f else []
    bl = blocks(text)
    if not bl:
        meta += "<div class=meta>No text has been extracted from this file yet; open the file to see it.</div>"
    if not f:
        meta += f"<div class=meta>The file itself can't be opened: {html.escape(why)}.</div>"
    return {"label": "In the dropbox", "title": q.get("title") or "?", "sub": "", "meta": meta, "acts": acts, "blocks": bl}


def _col(side, mark=None, only_diff=False, full=False, more_href=""):
    out, shown, hidden_same = [], 0, 0
    if not side["blocks"]:
        out.append("<p class=muted>(no text to show)</p>")
    for b in side["blocks"]:
        only = mark is not None and len(pkey(b)) > 3 and pkey(b) not in mark
        if only_diff and not only:
            hidden_same += 1
            continue
        if shown >= MAX_BLOCKS and not full:
            out.append(f"<p class=muted>&hellip; {len(side['blocks']) - shown - hidden_same} more paragraph(s). "
                       f"<a href='{html.escape(more_href)}'>Show the rest</a></p>")
            break
        r = render_block(b)
        out.append(f"<div class=only><span class=tagonly>ONLY HERE</span>{r}</div>" if only else r)
        shown += 1
    if only_diff and hidden_same:
        out.insert(0, f"<p class=muted>{hidden_same} paragraph(s) that are also in the other one are hidden.</p>")
    return (f"<div class='card col'><h3>{html.escape(side['label'])}</h3><h2>{html.escape(side['title'])}</h2>"
            + (f"<div class=meta>{html.escape(side['sub'])}</div>" if side.get("sub") else "")
            + side["meta"] + f"<div class=acts>{''.join(side['acts'])}</div><div class=body>{''.join(out)}</div></div>")


# ------------------------------------------------------------------ pages
def note_page(cfg, prop, day, t, rel, full=False):
    """HTML for one note, or None (-> 404) if the path isn't in the day's questions."""
    if rel not in allowed_notes(prop):
        try:   # "Also new since last night" names notes too
            ok = rel in {x["path"] for x in fresh_list(cfg, prop)}
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            return None
    back = f"../{day}{qs(t)}"
    side = note_side(cfg, prop, t, rel, "Note", alone=True)
    if not side:
        return shell(Path(rel).stem, "<div class=card>This note isn't in the vault any more (deleted, or merged "
                     "into another note).</div>", back)
    return shell(side["title"], _col(side, full=full, more_href=f"note{qs(t, p=rel, all=1)}"), back)


def compare_sides(cfg, prop, t, q):
    if q.get("kind") == "archived":
        a = note_side(cfg, prop, t, q["path"], "Archived copy")
        b = note_side(cfg, prop, t, q["keeper"], "Kept note") if q.get("keeper") else None
        return a, b
    if q.get("kind") == "pair":
        return (note_side(cfg, prop, t, q["paths"][0], "First note"), note_side(cfg, prop, t, q["paths"][1], "Second note"))
    if q.get("kind") == "duplicate":
        b = note_side(cfg, prop, t, q["match"], "Already in the vault?") if str(q.get("match") or "").endswith(".md") else None
        return dropbox_side(cfg, t, q), b
    return None, None


def compare_page(cfg, prop, day, t, qid, only_diff=False, full=False):
    """HTML for the two items behind a duplicate / already-in-the-vault question, or None (-> 404)."""
    q = question(prop, qid)
    if not q or q.get("kind") not in ("archived", "pair", "duplicate"):
        return None
    back = f"../{day}{qs(t)}"
    a, b = compare_sides(cfg, prop, t, q)
    missing = "<div class='card col'><h3>Not found</h3>This note isn't in the vault any more (deleted, or merged into another note).</div>"
    if not (a and b) or not (a["blocks"] and b["blocks"]):
        cols = (_col(a, full=full) if a else missing) + (_col(b, full=full) if b else missing)
        summary = f"<div class='card sum'><b>{q['id']}.</b> {html.escape(q.get('text') or '')}</div>"
        return shell(f"Question {q['id']}: side by side", summary + f"<div class=grid>{cols}</div>", back, wide=True)
    ka, kb = {pkey(x) for x in a["blocks"]}, {pkey(x) for x in b["blocks"]}
    na = sum(1 for x in a["blocks"] if len(pkey(x)) > 3 and pkey(x) not in kb)
    nb = sum(1 for x in b["blocks"] if len(pkey(x)) > 3 and pkey(x) not in ka)
    same = sum(1 for x in a["blocks"] if len(pkey(x)) > 3 and pkey(x) in kb)
    tot_a = sum(1 for x in a["blocks"] if len(pkey(x)) > 3)
    tot_b = sum(1 for x in b["blocks"] if len(pkey(x)) > 3)
    if na == 0 and tot_a:
        verdict = "Everything in the left one is also in the right one."
    elif tot_a and tot_b and same < 0.1 * min(tot_a, tot_b):
        verdict = "They have very little in common: probably <b>different works</b>."
    else:
        verdict = ""
    toggle = (f"<a href='compare{html.escape(qs(t, q=qid))}'>Show everything</a>" if only_diff else
              f"<a href='compare{html.escape(qs(t, q=qid, diff=1))}'>Show only the paragraphs that differ</a>")
    summary = (f"<div class='card sum'><b>{q['id']}.</b> {html.escape(q.get('text') or '')}<br>"
               f"{na} paragraph(s) only on the left &middot; {nb} only on the right &middot; {same} in both "
               f"(marked <span class=only style='margin:0;padding:0 4px'>ONLY HERE</span>). {verdict}"
               f"<div class=acts>{toggle}</div></div>")
    more = f"compare{qs(t, q=qid, all=1, **({'diff': 1} if only_diff else {}))}"
    cols = _col(a, kb, only_diff, full, more) + _col(b, ka, only_diff, full, more)
    return shell(f"Question {q['id']}: side by side", summary + f"<div class=grid>{cols}</div>", back, wide=True)


def file_response(path):
    """(path, mimetype) for Flask send_file; PDFs and text open in the browser, others download."""
    mt = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path, mt


# ------------------------------------------------------------------ links for the answer page and the email
def links_for(q, prefix, t):
    """HTML links for a duplicate / intake question: compare, view each note, open the dropbox file.
    prefix: '<day>/' on the answer page, '<web_base>/organize/<day>/' in the email."""
    out = []
    kind = q.get("kind")
    if kind == "held":   # an item left in Remember (2026-10-01): the file, the link, the archived copy
        if str(q.get("where") or "").startswith("dropbox: "):
            out.append(("Open the file", f"{prefix}dropbox{qs(t, q=q['id'])}"))
        if str(q.get("where") or "").startswith("vault: "):   # e.g. a failed analysis (2026-11-19)
            out.append(("The note", f"{prefix}note{qs(t, p=q['where'][7:])}"))
        if q.get("url"):
            out.append(("Open the source", q["url"]))
        if q.get("archive"):
            out.append(("Internet Archive copy", q["archive"]))
        return out
    if kind in ("archived", "pair") or (kind == "duplicate" and q.get("match")):
        out.append(("Compare side by side", f"{prefix}compare{qs(t, q=q['id'])}"))
    if kind == "archived":
        out.append(("Archived copy", f"{prefix}note{qs(t, p=q['path'])}"))
        if q.get("keeper"):
            out.append(("Kept note", f"{prefix}note{qs(t, p=q['keeper'])}"))
    elif kind == "pair":
        for i, p in enumerate(q["paths"]):
            out.append((("First" if i == 0 else "Second") + " note", f"{prefix}note{qs(t, p=p)}"))
        for i, (p, f) in enumerate(zip(q["paths"], q.get("sources") or [])):   # The owner (2026-10-12): look at the files
            if f:
                out.append((("First" if i == 0 else "Second") + " source file", f"{prefix}original{qs(t, p=p)}"))
    else:
        if str(q.get("where") or "").startswith("dropbox: "):
            out.append(("The file in the dropbox", f"{prefix}dropbox{qs(t, q=q['id'])}"))
        if str(q.get("match") or "").endswith(".md"):
            out.append(("The note in the vault", f"{prefix}note{qs(t, p=q['match'])}"))
        if str(q.get("where") or "").startswith("vault: "):
            out.append(("The note", f"{prefix}note{qs(t, p=q['where'][7:])}"))
    return out


# ------------------------------------------------------------------ "Add a note" (my_note) on the answer page (2026-10-01)
MN_CSS = (".mn{display:block;margin:3px 0 2px 0}.mnb{font:inherit;font-size:9.5pt;font-weight:normal;padding:2px 9px;"
          "border-radius:4px;border:1px solid #c8c6c4;background:#fff;color:#483d8b;cursor:pointer}"
          ".mn input{font:inherit;font-size:10.5pt;width:calc(100% - 70px);padding:4px 6px}.mn .mns{font-size:9.5pt;"
          "padding:4px 10px;margin-left:4px}.mnok{font-size:10pt;color:#2e7d32}.mnerr{font-size:10pt;color:#a4262c}")
MN_JS = """<script>
function mnOpen(b){var s=b.parentNode;s.innerHTML="<input maxlength=300 placeholder='Your note (one line)'>"+
 "<button type=button class='mnb mns'>Save</button>";var i=s.querySelector('input');i.focus();
 i.onkeydown=function(e){if(e.key==='Enter'){e.preventDefault();mnSave(s)}};s.querySelector('.mns').onclick=function(){mnSave(s)};}
function mnSave(s){var i=s.querySelector('input'),v=i.value.trim();if(!v)return;var f=new URLSearchParams();
 f.append('t',s.dataset.t);f.append('text',v);if(s.dataset.p)f.append('p',s.dataset.p);if(s.dataset.q)f.append('q',s.dataset.q);
 fetch(s.dataset.u,{method:'POST',body:f}).then(function(r){return r.json()}).then(function(j){
  s.innerHTML=j.ok?"<span class=mnok>&#10003; Your note: "+j.text.replace(/</g,'&lt;')+"</span>":"<span class=mnerr>Not saved: "+
  (j.why||'error')+"</span>"}).catch(function(){s.innerHTML="<span class=mnerr>Not saved (no connection)</span>"});}
</script>"""


def mn_button(url, t, p=None, q=None, current=""):
    """The 'Add a note' button for a vault note (p) or a queue/held item (q); shows the note when there is one."""
    if current:
        return f"<span class=mn><span class=mnok>Your note: {html.escape(current)}</span></span>"
    data = f" data-p='{html.escape(p)}'" if p else f" data-q='{html.escape(str(q))}'"
    return (f"<span class=mn data-u='{html.escape(url)}' data-t='{html.escape(t)}'{data}>"
            f"<button type=button class=mnb onclick='mnOpen(this)'>Add a note</button></span>")


def note_my_note(cfg, prop, rel):
    import my_note
    p = find_note(cfg["vault_path"], rel, prop)
    return my_note.my_note_of(p) if p else None   # None: note gone (merged, deleted)


def asked_names(prop):
    names = {Path(p).stem.lower() for p in allowed_notes(prop)}
    names |= {_clean(it["rename_to"]).lower() for it in prop.get("items", []) if it.get("rename_to")}
    return names


def fresh_list(cfg, prop):
    """Notes ingested since the night before this page's questions, not asked about, my_note empty."""
    import datetime as dt
    import my_note
    try:
        made = dt.datetime.fromisoformat(str(prop.get("created")))
    except (TypeError, ValueError):
        made = dt.datetime.now().astimezone()
    names = asked_names(prop)
    return [x for x in my_note.fresh_notes(cfg, made - dt.timedelta(hours=24)) if x["name"].lower() not in names]


def fresh_section(cfg, prop, day, t, prefix):
    """HTML: the 'Also new since last night' list with Add-a-note buttons ('' if none)."""
    rows = fresh_list(cfg, prop)
    if not rows:
        return ""
    url = f"{prefix}mynote"
    items = "".join(f"<div style='margin:8px 0'><b><a class=nm href='{html.escape(prefix + 'note' + qs(t, p=x['path']))}' "
                    f"target=_blank>{html.escape(x['name'])}</a></b> <span class=sub>({html.escape(x['type'] or 'note')}, "
                    f"{html.escape(x['path'].rsplit('/', 1)[0] if '/' in x['path'] else 'vault root')})</span>"
                    f"{mn_button(url, t, p=x['path'])}</div>" for x in rows[:60])
    more = f"<p class=sub>and {len(rows) - 60} more</p>" if len(rows) > 60 else ""
    return ("<h3 style='margin:18px 0 0 0;color:#483d8b'>Also new since last night (no questions)</h3>"
            "<div class=card><div class=sub>Items added to the vault since last night that weren't asked about and have "
            f"no note of yours yet.</div>{items}{more}</div>")
