#!/usr/bin/env python3
"""Concept analysis of vault notes (plan: 2026-09-28 Concept analysis and links - plan.md, phase A).

A queue route, "analyze": one note in, one companion note "<name> - Analysis" out, written beside it:
what the item is, its key concepts (linked to concept notes in Concepts/), its argument if it argues
a case, notable claims, and up to 5 related notes with the kind of relation. Concepts the register
doesn't have yet are asked about in the 06:00 email (organize.queue_questions / apply_queue) and
appear as plain text until the owner approves them.

Items come from:
  collect()   new notes of the scope types, filed out of Inbox/Clippings after analysis.since
              (only when analysis.auto is true)
  enqueue()   the owner's request: vqueue.py analyze <target> [--redo] [--force]
              target = a vault path or note name, folder:<path>, tag:<tag> or concept:<name>

Config (config\\vault.json "analysis"; defaults below). The item's own note is never changed.
"""
import datetime as dt
import hashlib
import json
import math
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import llm  # noqa: E402
import vqueue as Q  # noqa: E402

sys.path.insert(0, str(C.VL))
from vaultlib import RESERVED, split_frontmatter, top_folder, walk  # noqa: E402

GEN = "claude/claude-code-cli"
SUFFIX = " (Analysis)"   # was " - Analysis" until 2026-10-10; vault_names finds both forms
import vault_names as VN  # noqa: E402
PENDING_MARK = " *(new concept, awaiting approval)*"
RELATIONS = ("agrees", "disagrees", "extends", "applies", "same subject", "background")
NOT_RELATED_TYPES = {"Analysis", "Part Analysis", "Concept", "Transcript", "Podcast Transcript", "Topic Map",
                     "Agent Instructions", "Tag Vocabulary", "Daily Note", "index"}
DEFAULTS = {
    "auto": False,          # collect new notes by itself (off until the pilot is approved)
    "since": None,          # ISO time: only notes created after it are collected (no silent backfill)
    "types": ["Article", "Web Clipping", "Web Summary", "Video Summary", "Podcast Summary", "Reference"],
    "skip_folders": ["Inbox", "Clippings", "Archives", "_Obsidian Internal", "Claude", "Concepts", "Topics", "Daily Notes",
                     "attachments", "Templates"],
    "min_words": 150,
    "max_words": 40000,     # longer: always analyzed in parts (phase C, night window)
    "split_over_words": 6000,   # an article this long with >= split_min_sections headed sections is analyzed in parts
    "split_min_sections": 3,
    "books": True,          # phase C: a new Book Summary (since `since`) queues a per-chapter analysis of the book
    "concepts_max": 8,
    "related_max": 5,
    "candidates": 20,
    "usd_in_per_mtok": 6.25,    # API-equivalent estimate (claude -p writes the prompt to cache)
    "usd_out_per_mtok": 25.0,
    "out_tokens": 4500,        # pilot 2026-09-28: 2,500-5,800 output tokens per item
    "ask_after_uses": 2,        # (B) a new concept is asked about once this many ITEMS use it (2026-11-23: a book's
                                # chapter analyses count once; before, two chapters of one book were enough)
    "later_days": 7,            # "Not now" on a concept question: asked again after this many days (2026-11-23)
    "reference_folders": ["Resources"],   # automatic analysis of Reference notes only here (2026-09-28)
    "notes_settle_minutes": 60,           # redo after the owner's notes change, once they've been left alone this long
    "notes_max_words": 4000,
    # register sent to Claude (2026-11-13, the owner): the full register with descriptions had grown to ~79K tokens and
    # went out with every call. Now the NAMES go in the system prompt (same text all run -> prompt-cached) and only
    # the register_detail concepts most like the item get their descriptions, with the item.
    "register_detail": 60,
    "register_pending": False,   # send concepts still awaiting the owner's OK too (they were 809 of 1,309 on 2026-11-13)
}


def acfg(cfg):
    a = dict(DEFAULTS)
    a.update(cfg.get("analysis") or {})
    return a


def pending_path():
    return C.APP / "queue" / "concepts.json"


def load_pending():
    p = pending_path()
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d.setdefault("pending", {})
            d.setdefault("rejected", [])
            return d
        except ValueError:
            pass
    return {"pending": {}, "rejected": []}


def save_pending(d):
    pending_path().parent.mkdir(parents=True, exist_ok=True)
    Q.atomic_write(pending_path(), json.dumps(d, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- the vault as data
def scan(vault):
    """{name: {rel, name, type, title, description, tags, fm, top}} for every note (names are unique)."""
    notes = {}
    for rel, ab in walk(str(vault)):
        if not rel.lower().endswith(".md") or rel.rsplit("/", 1)[-1] in RESERVED:
            continue
        try:
            text = Path(ab).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, _, _ = split_frontmatter(text)
        fm = fm if isinstance(fm, dict) else {}
        name = rel.rsplit("/", 1)[-1][:-3]
        tags = fm.get("tags") or []
        notes[name] = {"rel": rel, "name": name, "type": str(fm.get("type") or ""),
                       "title": str(fm.get("title") or name), "description": str(fm.get("description") or "").strip(),
                       "tags": [str(t).lstrip("#") for t in (tags if isinstance(tags, list) else [tags])],
                       "fm": fm, "top": top_folder(rel) if "/" in rel else ""}
    return notes


def eligible(n, a, on_request=False):
    if VN.is_derived(n["name"]):
        return False
    if str(n["fm"].get("analysis") or "").lower() == "skip":
        return False
    if on_request:
        return n["type"] not in NOT_RELATED_TYPES
    if n["type"] == "Reference" and n["top"] not in (a.get("reference_folders") or []):
        return False
    return n["type"] in a["types"] and n["top"] not in a["skip_folders"]


def _created(n, vault):
    g = n["fm"].get("generated")
    at = g.get("at") if isinstance(g, dict) else None
    try:
        if at:
            d = dt.datetime.fromisoformat(str(at))
            return d if d.tzinfo else d.astimezone()
    except ValueError:
        pass
    return dt.datetime.fromtimestamp((Path(vault) / n["rel"]).stat().st_mtime).astimezone()


def _open_item_for(store, rel, open_only=False):
    for it in store.all():
        if Q.route_of(it) in ("analyze", "analyze_long") and (it.get("source_ref") or {}).get("path") == rel:
            if open_only and it["status"] not in Q.OPEN:
                continue
            return it
    return None


def _new_item(cfg, store, n, mode, released=False, force=False, related_only=False, parts=False):
    it = store.create(source="vault", kind="note", title=n["title"], route="analyze",
                      source_ref={"path": n["rel"], "name": n["name"], "mode": mode, "force": bool(force),
                                  "related_only": bool(related_only), "parts": bool(parts)})
    it["status"] = "ready"
    if released:
        it["manual"]["released"] = True
    return it


def collect(cfg, log, store, dry):
    """New notes of the scope types without an analysis -> ready items. Returns (report rows, new items)."""
    a = acfg(cfg)
    if not a["auto"] or not a["since"]:
        return [], []
    vault = Path(cfg["vault_path"])
    since = dt.datetime.fromisoformat(a["since"])
    since = since if since.tzinfo else since.astimezone()
    notes = scan(vault)
    rows, new = [], []
    settle = dt.timedelta(minutes=int(a["notes_settle_minutes"]))
    now = C.now(cfg)
    for n in notes.values():                     # The owner added or changed notes on an analyzed item -> redo
        an = VN.find(notes, VN.analysis_names(n["name"]))
        if not an or an["type"] != "Analysis" or not eligible(n, a, on_request=True):
            continue
        prior = _open_item_for(store, n["rel"], open_only=True)
        if prior:
            continue
        text, from_, h, _ = my_notes(vault, notes, n, a)
        if h == str(an["fm"].get("notes_hash") or ""):
            continue
        srcs = [n] + [notes[x] for x in from_ if x in notes]
        newest = max(dt.datetime.fromtimestamp((vault / x["rel"]).stat().st_mtime).astimezone() for x in srcs)
        if now - newest < settle:
            continue
        it = _new_item(cfg, store, n, "redo")
        it["source_ref"]["why"] = "your notes changed"
        new.append(it)
        if not dry:
            store.save(it)
        rows.append((n["title"], "analyze", "your notes changed: queued to redo the analysis"))
    for n in notes.values():
        if not eligible(n, a) or VN.find(notes, VN.analysis_names(n["name"])) or _open_item_for(store, n["rel"]):
            continue
        try:
            if _created(n, vault) < since:
                continue
        except (OSError, ValueError):
            continue
        it = _new_item(cfg, store, n, "new")
        new.append(it)
        if not dry:
            store.save(it)
        rows.append((n["title"], "analyze", "queued for analysis"))
    try:                                   # phase C: per-chapter analyses of new books (2026-10-06)
        rows += queue_books(cfg, log, store, dry)
    except Exception as e:  # noqa: BLE001
        log(f"  book analyses not queued: {type(e).__name__}: {e}")
    return rows, new


def resolve_targets(vault, notes, target):
    """Vault notes named by a target: path or name, folder:<path>, tag:<tag>, concept:<name>."""
    t = target.strip().replace("\\", "/")
    if t.lower().startswith("tag:"):
        tag = t[4:].lstrip("#").lower()
        return [n for n in notes.values() if tag in [x.lower() for x in n["tags"]]]
    if t.lower().startswith("concept:"):
        c = t[8:].strip().lower()
        out = []
        for n in notes.values():
            if n["type"] == "Analysis" and c in [str(x).lower() for x in (n["fm"].get("concepts") or [])]:
                m = re.match(r"\[\[([^\]|]+)", str(n["fm"].get("analysis_of") or ""))
                if m and m.group(1) in notes:
                    out.append(notes[m.group(1)])
        return out
    if t.lower().startswith("folder:") or (Path(vault) / t).is_dir():
        f = t[7:] if t.lower().startswith("folder:") else t
        f = f.strip("/")
        return [n for n in notes.values() if n["rel"].startswith(f + "/")]
    name = t[:-3] if t.lower().endswith(".md") else t
    name = name.rsplit("/", 1)[-1]
    if VN.is_derived(name) or name.endswith(" (Analysis)"):
        name = VN.base(name)
    hit = VN.find(notes, [name] + VN.summary_names(name))
    return [hit] if hit else []


def enqueue(cfg, store, targets, redo=False, force=False, related_only=False, parts=False):
    """The owner's request -> lines saying what was queued."""
    a = acfg(cfg)
    vault = Path(cfg["vault_path"])
    notes = scan(vault)
    lines = []
    for target in targets:
        hits = resolve_targets(vault, notes, target)
        if not hits:
            lines.append(f"{target}: no matching note")
            continue
        for n in hits:
            if not eligible(n, a, on_request=True):
                continue
            prior = _open_item_for(store, n["rel"])
            if prior and prior["status"] in Q.OPEN:
                lines.append(f"{n['name']}: already queued ({prior['id']}, {prior['status']})")
                continue
            has = VN.find(notes, VN.analysis_names(n["name"])) is not None
            if related_only and not has:
                lines.append(f"{n['name']}: has no analysis yet, so there is no Related list to refresh")
                continue
            if has and not redo:
                lines.append(f"{n['name']}: already has an analysis (use --redo to redo it)")
                continue
            it = _new_item(cfg, store, n, "redo" if has else "new", released=True, force=force,
                           related_only=related_only, parts=parts)
            store.save(it)
            kind = ", related links only" if related_only else (", redo" + (" with parts" if parts else "") if has else "")
            lines.append(f"{n['name']}: queued ({it['id']}{kind})")
    return lines


# ---------------------------------------------------------------- material for the model
def _body(vault, rel):
    text = (Path(vault) / rel).read_text(encoding="utf-8", errors="replace")
    fm, _, body = split_frontmatter(text)
    body = re.split(r"(?m)^## Original\s*$", body)[0]          # the embedded original isn't text
    body = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("![["))
    return (fm if isinstance(fm, dict) else {}), body.strip()


_OWN = C.owner()   # config owner: "## <Name>'s notes" headings and the "[!note] <note_label>" callout are the owner's
MYNOTES_HEAD = re.compile(r"(?mi)^(#{2,4})\s*(my notes|my thoughts|my comments"
                          + (r"|" + re.escape(_OWN["given"]) + r"'?s notes?" if _OWN["given"] else "") + r")\s*$")
CALLOUT = re.compile(r"(?m)^> \[!note\] " + re.escape(_OWN["note_label"]) + r"\s*\n((?:>.*\n?)*)")


def split_my_notes(body):
    """(item text without the owner's notes, the owner's notes text) from a note body."""
    mine = []
    m = MYNOTES_HEAD.search(body)
    while m:
        level = len(m.group(1))
        nxt = re.compile(r"(?m)^#{1,%d}\s" % level).search(body, m.end())
        end = nxt.start() if nxt else len(body)
        mine.append(body[m.end():end].strip())
        body = body[:m.start()] + body[end:]
        m = MYNOTES_HEAD.search(body)
    for c in CALLOUT.finditer(body):
        mine.append("\n".join(ln[1:].strip() for ln in c.group(1).splitlines()).strip())
    body = CALLOUT.sub("", body)
    return body.strip(), "\n\n".join(x for x in mine if x)


def linked_my_notes(vault, notes, n):
    """The owner's own notes (generated.by = config owner.id, or type Personal Note) that link to the item: [(name, text)]."""
    pat = re.compile(r"\[\[" + re.escape(n["name"]) + r"(\|[^\]]*)?\]\]")
    out = []
    for m in notes.values():
        g = m["fm"].get("generated")
        mine = (isinstance(g, dict) and str(g.get("by") or "").startswith(C.owner()["id"])) or m["type"] == "Personal Note"
        if not mine or m["name"] == n["name"] or m["type"] in NOT_RELATED_TYPES:
            continue
        try:
            _, b = _body(vault, m["rel"])
        except OSError:
            continue
        if pat.search(b):
            out.append((m["name"], b))
    return sorted(out)


def see_also_of(fm):
    """Note names from the owner's `see_also` property ([[Name]], [[Name|alias]] or a bare name)."""
    v = fm.get("see_also") or []
    out = []
    for x in (v if isinstance(v, list) else [v]):
        m = re.match(r"\s*\[\[([^\]|#]+)", str(x or ""))
        nm = (m.group(1) if m else str(x or "")).strip()
        if nm and nm not in out:
            out.append(nm)
    return out


def my_notes(vault, notes, n, a):
    """The owner's notes on an item -> (text for the model, [linked note names], hash, [see_also names]).
    Sources: his `my_note` property (a one-line take), his `see_also` links, a `## My notes` section (ends at the
    next heading of the same level or higher), the file-intake callout, and his own notes that link to the item."""
    fm, body = _body(vault, n["rel"])
    _, own = split_my_notes(body)
    parts = []
    take = str(fm.get("my_note") or "").strip()
    if take:
        parts.append(f"His one-line take: {take}")
    # links to the work's own notes (Analysis, Transcript, parts; families.py adds them) aren't the owner's views
    see = [x for x in see_also_of(fm) if VN.work_of(x) != VN.work_of(n["name"])]
    if see:
        parts.append("He links this item to: " + ", ".join(f"[[{x}]]" for x in see))
    if own:
        parts.append(own)
    linked = linked_my_notes(vault, notes, n)
    parts += [f"(from the owner's note [[{nm}]])\n{t}" for nm, t in linked]
    words = "\n\n".join(parts).split()
    text = " ".join(words[:int(a["notes_max_words"])]) if words else ""
    return text, [nm for nm, _ in linked], _hash(text) if text else "", see


def material_of(vault, notes, n):
    _, body = _body(vault, n["rel"])
    body, _ = split_my_notes(body)
    parts = [body]
    tr = VN.find(notes, VN.transcript_names(n["name"]))
    if tr:
        _, tb = _body(vault, tr["rel"])
        tb = re.sub(r"(?m)^Summary: \[\[[^\]]*\]\]\s*$", "", tb).strip()
        parts.append("## Transcript\n\n" + tb)
    return "\n\n".join(parts)


def register(vault, notes):
    """Concept notes + pending concepts: [{name, aliases, description, pending}]."""
    out = []
    for n in notes.values():
        if n["type"] == "Concept":
            al = n["fm"].get("aliases") or []
            out.append({"name": n["name"], "aliases": [str(x) for x in (al if isinstance(al, list) else [al])],
                        "description": n["description"], "pending": False})
    for name, p in load_pending()["pending"].items():
        out.append({"name": name, "aliases": [], "description": (p.get("definition") or "")[:200], "pending": True})
    return sorted(out, key=lambda x: x["name"].lower())


WORD = re.compile(r"[a-z][a-z'\-]{2,}")
STOP = set("the and for with that this from are was were has have had not but you your his her its our their "
           "about into than then them they what when where which who why how can will would should could also "
           "more most some such only other over after before between under while there here these those been "
           "being does did just very much many any all each out one two new use using used".split())


def _toks(s):
    return [w for w in WORD.findall(s.lower()) if w not in STOP]


def candidates(vault, notes, n, k, cfg=None):
    """Up to k notes most like this one: tf-idf over title, description and tags (shared tags count double), merged
    with meaning-based neighbours from the local embedding index when it is installed (phase B, related.py)."""
    pool = [m for m in notes.values() if m["name"] != n["name"] and m["type"] not in NOT_RELATED_TYPES
            and m["top"] not in ("Claude", "Archives", "Templates", "attachments", "_Obsidian Internal")
            and not VN.is_derived(m["name"])]
    docs = {m["name"]: _toks(f"{m['title']} {m['description']} " + " ".join(m["tags"] * 2)) for m in pool}
    try:
        _, body = _body(vault, n["rel"])
    except OSError:
        body = ""
    me = _toks(f"{n['title']} {n['description']} " + " ".join(n["tags"] * 2) + " " + " ".join(body.split()[:300]))
    df = {}
    for ws in docs.values():
        for w in set(ws):
            df[w] = df.get(w, 0) + 1
    N = len(docs) + 1

    def vec(ws):
        tf = {}
        for w in ws:
            tf[w] = tf.get(w, 0) + 1
        return {w: c * math.log(N / (1 + df.get(w, 0))) for w, c in tf.items()}
    mv = vec(me)
    mn = math.sqrt(sum(x * x for x in mv.values())) or 1
    scored = []
    for m in pool:
        v = vec(docs[m["name"]])
        dot = sum(mv.get(w, 0) * x for w, x in v.items())
        if dot <= 0:
            continue
        scored.append((dot / (mn * (math.sqrt(sum(x * x for x in v.values())) or 1)), m))
    scored.sort(key=lambda x: -x[0])
    if cfg is None:
        return [m for _, m in scored[:k]]
    emb = []
    src = (cfg.get("analysis") or {}).get("candidates_from", "passages")
    if src in ("passages", "both"):   # passage index, with the owner's my_note (vault search plan, phase 4; 2026-10-12)
        try:
            import vault_search
            byrel = {m["rel"]: m["name"] for m in pool if m.get("rel")}
            emb = [byrel[p] for p, _sc in vault_search.related(cfg, n["rel"], 4 * k) if p in byrel][:2 * k]
        except Exception:  # noqa: BLE001
            emb = []
    if src in ("notes", "both") or not emb:   # phase B note-level index (also the fallback)
        try:
            import related
            old = related.neighbours(cfg, n["name"], related.note_text(n["title"], n["description"], n["tags"], body),
                                     2 * k)
            emb = old if not emb else list(dict.fromkeys(x for pair in zip(emb, old) for x in pair))
        except Exception:  # noqa: BLE001
            pass
    if not emb:
        return [m for _, m in scored[:k]]
    byname = {m["name"]: m for m in pool}
    rrf = {}                                   # reciprocal-rank fusion of the two rankings
    for rank, (_, m) in enumerate(scored[:2 * k]):
        rrf[m["name"]] = rrf.get(m["name"], 0) + 1 / (60 + rank)
    for rank, nm in enumerate(x for x in emb if x in byname):
        rrf[nm] = rrf.get(nm, 0) + 1 / (60 + rank)
    return [byname[nm] for nm, _ in sorted(rrf.items(), key=lambda x: -x[1])[:k]]


PROMPT = """You analyze one item from «owner's» Obsidian vault (an article, web clipping, video or podcast summary with \
its transcript, or a reference document) so that its ideas can be found and connected to the owner's other notes. «owner_about» \
Work only from the ITEM TEXT on stdin; never invent facts, authors or cites.

Return ONLY one JSON object, no prose around it:
{
 "description": "one sentence (max 35 words): what the item argues or covers",
 "in_brief": "2-4 sentences: what kind of work it is, its subject, and its main point",
 "argues": true if the item makes a case for a position (a thesis supported by reasons), else false,
 "argument": null, or {
   "thesis": "...", "thesis_cite": "...", "thesis_quote": "...",
   "premises": [{"claim": "...", "evidence": ["short item", "..."], "cite": "...", "quote": "..."}],   (2-6)
   "objections": [{"objection": "...", "reply": "...", "cite": "...", "quote": "..."}],   (0-4, only ones the item itself answers)
   "conclusion": "..."},
 "concepts": [up to MAXC, most central first:
   {"name": "...", "new": false, "use": "one sentence: how THIS item uses or treats the concept", "cite": "...", "quote": "..."}
   or, only when no register concept fits:
   {"name": "...", "new": true, "use": "...", "cite": "...", "quote": "...",
    "definition": "2-3 sentences defining the concept in general, not tied to this item",
    "why_new": "why no register concept fits"}],
 "claims": [up to 6 notable factual or analytical claims worth remembering: {"claim": "...", "cite": "...", "quote": "..."}],
 "your_notes": "only when «OWNER'S» NOTES are given: 2-4 sentences on how those notes relate to the item: what \
they emphasize, where they agree or disagree with it, questions they raise; else \"\"",
 "related": [up to MAXR notes from CANDIDATE NOTES only, closest first:
   {"note": "exact candidate name", "relation": "agrees|disagrees|extends|applies|same subject|background",
    "why": "one sentence; for agrees/disagrees name the claim concerned"}]
}

Concepts: an idea, doctrine, theory, mechanism, method or term of art that other items could also discuss \
(e.g. "Regulatory capture", "Adverse possession", "Comparative advantage"). Not a person, organization, place, \
product, event or the item's own title. Name it with a short noun phrase in sentence case, at the general level \
that lets other items share it. When a CONCEPT REGISTER entry fits, even loosely, use its exact name with \
"new": false (entries marked [pending] count as register entries). Propose a new concept only when none fits.
Cites: a page ("p. 12") when the text has page markers, a timestamp when a transcript has them, else the section \
heading. Use "" when nothing fits.
Quotes: every "quote" / "thesis_quote" is 5-25 words copied EXACTLY, character for character, from ITEM TEXT \
(no paraphrase, no joining of separate sentences; "..." only to skip words inside one passage). They are checked \
against the text by a script, and a quote that isn't found is marked unverified. Use "" if no passage supports it.
«Owner's» notes: never attribute them to the item or its author. Treat what they flag as important: prefer the \
concepts and claims they point to. Quotes and cites come only from ITEM TEXT, never from those notes.
Candidates marked (owner's see_also) are notes the owner personally linked to this item: include every one of them in \
"related", with the relation that fits best (they don't count toward the limit).
Related: only candidates that are genuinely related to this item's ideas; an empty list is fine."""


_RULES = PROMPT[PROMPT.index("Concepts: an idea"):PROMPT.index("«Owner's» notes: never")]

PART_PROMPT = """You analyze ONE PART (a chapter or section) of a longer item from «owner's» Obsidian vault, so that its \
ideas can be found and connected to the owner's other notes. «owner_about» Work only from the PART TEXT on stdin; never \
invent facts, authors or cites.

Return ONLY one JSON object, no prose around it:
{
 "summary": "150-300 words of plain prose: what this part says, in order, with its main evidence",
 "in_brief": "1-3 sentences: this part's main point and its role in the whole",
 "argues": true if this part makes a case for a position, else false,
 "argument": null, or {"thesis": "...", "thesis_cite": "...", "thesis_quote": "...",
   "premises": [{"claim": "...", "evidence": ["..."], "cite": "...", "quote": "..."}], (1-5)
   "objections": [{"objection": "...", "reply": "...", "cite": "...", "quote": "..."}], (0-3)
   "conclusion": "..."},
 "concepts": [up to MAXC, as in the register rules below:
   {"name": "...", "new": false, "use": "one sentence: how THIS part uses the concept", "cite": "...", "quote": "..."}
   or {"name": "...", "new": true, "use": "...", "cite": "...", "quote": "...", "definition": "...", "why_new": "..."}],
 "claims": [up to 4 notable claims: {"claim": "...", "cite": "...", "quote": "..."}]
}

""" + _RULES.replace("ITEM TEXT", "PART TEXT")

SYNTH_NOTE = """

THIS ITEM IS LONG: stdin holds PART ANALYSES written earlier from each chapter or section (summary, thesis, concepts, \
claims, each with quotes copied from the text and already checked) instead of the full text. Treat them as the ITEM \
TEXT: analyze the work as a whole (its overall argument, not a list of parts). Take every quote and cite ONLY from \
the quotes and cites shown in PART ANALYSES, unchanged; use "" when none fits."""


def part_name(n, i, title):
    """'<work> (Part 03 Analysis) - <chapter>' (was '<item> - Part 03 - <chapter>' until 2026-10-10)."""
    return VN.part_name(n["name"], i, title)


PATH_MAX = 240   # Windows: a full path longer than ~260 fails with FileNotFoundError (2026-10-12: 3 items held)


def fit_part_name(folder, nm, n, i):
    """A part note name whose full path stays under PATH_MAX: drop the chapter, then shorten the work's name
    (word boundary), keeping ' (Part NN Analysis)' so it stays unique and recognizable."""
    def fits(x):
        return len(str(folder / (x + ".md"))) <= PATH_MAX
    if fits(nm):
        return nm
    bare = VN.part_name(n["name"], i, "")
    if fits(bare):
        return bare
    tail = f" (Part {int(i):02d} Analysis)"
    room = PATH_MAX - len(str(folder)) - 1 - len(tail) - 3
    b = VN.base(n["name"])[:max(room, 20)]
    b = (b.rsplit(" ", 1)[0] if " " in b[10:] else b).rstrip(" ,;:-(")
    if b.count("(") > b.count(")"):          # don't leave "... (Decoder" half-open
        b = b[:b.rfind("(")].rstrip(" ,;:-(")
    return b + tail


def part_note(notes, n, i, title):
    """The existing part note, under the new or the old name, or a shortened one (fit_part_name): matched by its
    analysis_of + part properties."""
    hit = VN.find(notes, VN.part_names(n["name"], i, title))
    if hit:
        return hit
    want = f"[[{n['name']}]]"
    for x in notes.values():
        fm = x.get("fm") or {}
        if fm.get("type") == "Part Analysis" and str(fm.get("analysis_of")) == want and str(fm.get("part")) == str(i):
            return x
    return None


def needs_split(a, text, words, ref):
    if ref.get("text_from"):
        return True
    if words > int(a["max_words"]):
        return True
    heads = len(re.findall(r"(?m)^#{1,2}\s+\S", text))
    return words > int(a.get("split_over_words", 6000)) and heads >= int(a.get("split_min_sections", 3))


def long_parts(cfg, store, n, text, ref):
    """-> (parts [{id, title, text, words}], full text for quote checks). Phase C (2026-10-06).
    A book: the chapters longdoc planned for its summary (the book's own extracted text).
    Anything else: the note's text split by headings (or by length), as longdoc does for text files."""
    import longdoc
    import routes as RT
    if ref.get("text_from"):
        bid = ref["text_from"]
        bitem = store.load(bid)
        doc = RT.load_doc(store, bitem)
        parts, _, has_pages = longdoc.plan(store, bitem, doc)
        pages = longdoc._pages(store, bitem) or []
        out = [{"id": p["id"], "title": p["title"], "words": p["words"],
                "span": longdoc.span(p, has_pages) if has_pages else "",
                "text": longdoc.material(pages, p, has_pages)} for p in parts]
        full = "\n\n".join(f"[page {i}]\n{t}" for i, t in enumerate(pages, 1)) if has_pages else doc["text"]
        return out, full
    parts, _, _ = longdoc.plan(store, {"id": "__no_payload__"}, {"text": text})
    return [{"id": p["id"], "title": p["title"], "words": p["words"], "span": "", "text": p["text"]} for p in parts], text


def part_digest(i, p, j):
    """A part's answer, compacted for the synthesis call."""
    L = [f"### PART {i}: {p['title']}" + (f" ({p['span']})" if p.get("span") else ""),
         "Summary: " + _norm(j.get("summary")), "In brief: " + _norm(j.get("in_brief"))]
    arg = j.get("argument") if j.get("argues") and isinstance(j.get("argument"), dict) else None
    if arg:
        L.append(f"Thesis: {_norm(arg.get('thesis'))} [cite: {_norm(arg.get('thesis_cite'))}] [quote: {_norm(arg.get('thesis_quote'))}]")
        for x in arg.get("premises") or []:
            L.append(f"- Premise: {_norm(x.get('claim'))} [cite: {_norm(x.get('cite'))}] [quote: {_norm(x.get('quote'))}]")
    for c in j.get("concepts") or []:
        L.append(f"- Concept: {_norm(c.get('name'))}{' (new)' if c.get('new') else ''}: {_norm(c.get('use'))} "
                 f"[cite: {_norm(c.get('cite'))}] [quote: {_norm(c.get('quote'))}]")
    for c in j.get("claims") or []:
        L.append(f"- Claim: {_norm(c.get('claim'))} [cite: {_norm(c.get('cite'))}] [quote: {_norm(c.get('quote'))}]")
    return "\n".join(L)


def do_parts(cfg, log, store, item, n, notes, text, a, reg, cands, mine, mine_from, mine_hash, see, old):
    """Phase C: one call per part -> Part Analysis notes in <item folder>/<item name>/, then one call over the
    part analyses -> the item's Analysis note with a Parts list. Answers are cached, so a run stopped by a
    limit resumes where it stopped."""
    import routes as RT
    vault = Path(cfg["vault_path"])
    ref = item.get("source_ref") or {}
    parts, full = long_parts(cfg, store, n, text, ref)
    if not parts:
        return {"ok": False, "why": "no parts to analyze"}
    reuse = {}
    if old and not ref.get("parts"):          # phase D: a redo reuses the part notes unless --parts (2026-10-06)
        for i, p in enumerate(parts, 1):
            po = part_note(notes, n, i, p["title"])
            if po:
                reuse[i] = _body(vault, po["rel"])[1]
        if len(reuse) != len(parts):
            reuse = {}                         # parts changed shape: redo them all
    item.setdefault("work", {})["step"] = "parts"
    item["work"]["parts"] = [{k: p[k] for k in ("id", "title", "words")} for p in parts]
    store.save(item)
    pinstr = PART_PROMPT.replace("MAXC", str(min(6, int(a["concepts_max"]))))
    head = {"title": n["title"], "type": n["type"], "tags": n["tags"], "description": n["description"]}
    system = register_system(reg, a)
    reqs = [{"id": f"p{p['id']}", "instructions": pinstr, "timeout": 900, "system": system, "model_key": "analyze_part",
             "material": build_material(head, p["text"], reg, [], "", (), a).split("CANDIDATE NOTES")[0]
                         + f"\nPART: {p['title']}" + (f" ({p['span']})" if p.get("span") else "")
                         + "\n\nPART TEXT:\n\n" + p["text"]} for p in parts]
    if reuse:
        reqs = []
    ans = llm.run(cfg, "analyze", reqs, store.payload(item["id"]) / "answers", log, item["id"]) if reqs else {}
    pj = []
    for p, r in zip(parts, reqs or [None] * len(parts)):
        if r is None:
            pj.append(None)
            continue
        raw = RT.text_of(ans, r["id"])                      # raises LimitHit: resumes next night
        j = RT.as_json(raw) if raw else None
        if not isinstance(j, dict) or not j.get("summary"):
            bad = store.payload(item["id"]) / "answers" / f"{r['id']}.json"
            if bad.exists():
                bad.rename(bad.with_suffix(".bad.json"))
            return {"ok": False, "why": f"part '{p['title']}' answer was not the JSON asked for"}
        pj.append(j)
    item["work"]["step"] = "synthesis"
    store.save(item)
    digest = "\n\n".join(part_digest(i, p, j) if j is not None else
                         f"### PART {i}: {p['title']}\n" + re.sub(r"(?s)^.*?## Summary", "Summary:", reuse[i])[:4000]
                         for i, (p, j) in enumerate(zip(parts, pj), 1))
    sinstr = PROMPT.replace("MAXC", str(a["concepts_max"])).replace("MAXR", str(a["related_max"])) + SYNTH_NOTE
    mat = build_material(n, "PART ANALYSES:\n\n" + digest, reg, cands, mine, see, a)
    rid = "synthesis-" + _hash(sinstr + mat)[:10]
    ans.update(llm.run(cfg, "analyze", [{"id": rid, "instructions": sinstr, "material": mat, "timeout": 900,
                                         "system": system}],
                       store.payload(item["id"]) / "answers", log, item["id"]))
    raw = RT.text_of(ans, rid)
    j = RT.as_json(raw) if raw else None
    if not isinstance(j, dict) or not j.get("in_brief"):
        bad = store.payload(item["id"]) / "answers" / f"{rid}.json"
        if bad.exists():
            bad.rename(bad.with_suffix(".bad.json"))
        return {"ok": False, "why": "the synthesis answer was not the JSON asked for"}
    # write: part notes first (their names go into the Analysis note's Parts list)
    # 2+ parts: the work's own folder (main note, Analysis and parts together); 1 part: beside the main note
    # (families.py, 2026-10-13; was always "<folder>/<work>/")
    import families
    folder = families.parts_dir(vault, n["rel"], len(parts))
    folder.mkdir(parents=True, exist_ok=True)
    names, qok, qbad = [], 0, 0
    bdir = None
    for i, (p, pjj) in enumerate(zip(parts, pj), 1):
        po = part_note(notes, n, i, p["title"])  # an existing part note keeps its name (old or new form)
        nm = po["name"] if po else fit_part_name(folder, part_name(n, i, p["title"]), n, i)
        if pjj is None:                       # reused unchanged
            names.append(nm)
            continue
        if po:
            bdir = bdir or vault / ".analysis-backups" / C.now(cfg).strftime("%Y%m%d-%H%M%S")
            bdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vault / po["rel"], bdir / Path(po["rel"]).name)
        part = {"name": nm, "num": i, "title": p["title"], "span": p.get("span", ""), "summary": _norm(pjj.get("summary")),
                "path": (vault / po["rel"]) if po else folder / (nm + ".md")}
        _, _, qs = write_analysis(cfg, vault, notes, n, pjj, reg, [], a, None, p["text"], part=part)
        names.append(nm)
        qok, qbad = qok + qs.ok, qbad + qs.bad
    if old:
        bdir = bdir or vault / ".analysis-backups" / C.now(cfg).strftime("%Y%m%d-%H%M%S")
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vault / old["rel"], bdir / Path(old["rel"]).name)
    before = snapshot(vault, old["rel"]) if old else None
    rel, related, qs = write_analysis(cfg, vault, notes, n, j, reg, cands, a, old, full, (mine_from, mine_hash, see),
                                      parts_links=names)
    if before:
        item["work"]["changes"] = changes(before, snapshot(vault, rel))
    item["work"]["usage"] = {"calls": len(reqs) + 1,
                             "cost_usd": round(sum((ans.get(k) or {}).get("cost_usd") or 0 for k in ans), 2)}
    item["work"]["quotes"] = {"verified": qs.ok + qok, "unverified": qs.bad + qbad}
    told = backlink(cfg, vault, notes, n, related)
    if told:
        log(f"  later-items line added to {len(told)} older analysis note(s)")
    try:   # main note and Analysis join the parts' folder; family see_also links (families.py, 2026-10-13)
        families.regroup(cfg, log, only=[n["name"]])
        rel = VN.follow(rel) if not (vault / rel).exists() else rel
        if not (vault / rel).exists():
            hit = next(iter(vault.rglob(Path(rel).name)), None)
            rel = hit.relative_to(vault).as_posix() if hit else rel
    except Exception as e:  # noqa: BLE001
        log(f"  families regroup failed: {type(e).__name__}: {e}")
    C.run(log, C.VL / "build_concepts.py", vault)
    C.run(log, C.VL / "build_indexes.py", vault, "--folders", rel.split("/")[0])
    return {"ok": True, "note": rel, "why_done": f"analysis in {len(parts)} parts written; quotes "
                                                 f"{qs.ok + qok} verified, {qs.bad + qbad} unverified"}


# ---------------------------------------------------------------- phase D: change report, related-only redo
REL_LINE = re.compile(r"(?m)^- \*\*([a-z ]+)\*\* — \[\[([^\]|]+)")


def snapshot(vault, rel):
    """What a change report compares: concepts, related links, thesis."""
    try:
        fm, body = _body(vault, rel)
    except OSError:
        return None
    th = re.search(r"(?m)^\*\*Thesis:\*\* (.+)$", body)
    return {"concepts": list(fm.get("concepts") or []) + list(fm.get("concepts_pending") or []),
            "related": [f"{k}: {t}" for k, t in REL_LINE.findall(body)],
            "thesis": re.sub(r"\s*\*?\(.*$|\s*\*\s*$", "", th.group(1)).strip() if th else ""}


def changes(a, b):
    if not a or not b:
        return {}
    out = {"concepts_added": [x for x in b["concepts"] if x not in a["concepts"]],
           "concepts_dropped": [x for x in a["concepts"] if x not in b["concepts"]],
           "related_added": [x for x in b["related"] if x not in a["related"]],
           "related_dropped": [x for x in a["related"] if x not in b["related"]]}
    if a["thesis"] != b["thesis"]:
        out["thesis"] = {"was": a["thesis"], "now": b["thesis"]}
    return {k: v for k, v in out.items() if v}


RELATED_PROMPT = """You update the RELATED list of one analysis note in «owner's» Obsidian vault. «owner_about»
stdin has the item's ANALYSIS (already written: summary, argument, concepts, claims) and CANDIDATE NOTES.
Return ONLY one JSON object: {"related": [up to MAXR notes from CANDIDATE NOTES only, closest first:
 {"note": "exact candidate name", "relation": "agrees|disagrees|extends|applies|same subject|background",
  "why": "one sentence; for agrees/disagrees name the claim concerned"}]}
Candidates marked (owner's see_also) must all be included. Only genuinely related notes; an empty list is fine."""


def do_related(cfg, log, store, item, n, notes, a, old):
    """Phase D --related-only: re-pick the Related list from fresh candidates without rereading the item (one small
    call over the existing analysis); only the Related section and backlinks change."""
    import routes as RT
    vault = Path(cfg["vault_path"])
    ofm, obody = _body(vault, old["rel"])
    cands = candidates(vault, notes, n, int(a["candidates"]), cfg)
    _, _, _, see = my_notes(vault, notes, n, a)
    forced = [notes[x] for x in see if x in notes and x != n["name"]]
    cands = forced + [c for c in cands if c["name"] not in {f["name"] for f in forced}]
    lines = ["CANDIDATE NOTES:"] + [f"- \"{m['name']}\" [{m['type']}]" + (" (owner's see_also)" if m["name"] in see else "")
                                    + (f" - {m['description'][:200]}" if m["description"] else "") for m in cands]
    analysis = re.split(r"(?m)^## Related\s*$", obody)[0]
    mat = f"ITEM: {n['title']}\n\nANALYSIS:\n\n{analysis[:12000]}\n\n" + "\n".join(lines)
    instr = RELATED_PROMPT.replace("MAXR", str(a["related_max"]))
    rid = "related-" + _hash(instr + mat)[:10]
    ans = llm.run(cfg, "analyze", [{"id": rid, "instructions": instr, "material": mat, "timeout": 300,
                                    "model_key": "analyze_related"}],
                  store.payload(item["id"]) / "answers", log, item["id"])
    j = RT.as_json(RT.text_of(ans, rid) or "")
    if not isinstance(j, dict) or "related" not in j:
        return {"ok": False, "why": "the related-links answer was not the JSON asked for"}
    names = {m["name"].lower(): m["name"] for m in cands}
    related, rl_lines = [], []
    for r in j.get("related") or []:
        nm = names.get(_norm(r.get("note")).strip('"').lower())
        kind = _norm(r.get("relation")).lower()
        if nm and kind in RELATIONS and nm not in [x[0] for x in related]:
            related.append((nm, kind, _norm(r.get("why"))))
            rl_lines.append(f"- **{kind}** — [[{nm}]]: {_norm(r.get('why'))}")
    before = snapshot(vault, old["rel"])
    bdir = vault / ".analysis-backups" / C.now(cfg).strftime("%Y%m%d-%H%M%S")
    bdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(vault / old["rel"], bdir / Path(old["rel"]).name)
    import yaml
    fm, _, body = split_frontmatter((vault / old["rel"]).read_text(encoding="utf-8"))
    fm = fm if isinstance(fm, dict) else {}
    edited = fm.get("analysis_hash") and fm.get("analysis_hash") != _hash(body.strip())
    new_sec = "## Related\n\n" + "\n".join(rl_lines or ["- none found yet"]) + "\n"
    body2, k = re.subn(r"(?ms)^## Related\s*\n.*?(?=^## |\Z)", new_sec + "\n", body, count=1)
    if not k:
        body2 = body.rstrip() + "\n\n" + new_sec
    if not edited:                              # keep "edited by hand" detection working for later redos
        fm["analysis_hash"] = _hash(body2.strip())
    (vault / old["rel"]).write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000)
                                    + "---\n\n" + body2.lstrip("\n"), encoding="utf-8", newline="\n")
    item.setdefault("work", {})["changes"] = changes(before, snapshot(vault, old["rel"]))
    backlink(cfg, vault, notes, n, related)
    return {"ok": True, "note": old["rel"], "why_done": f"related links refreshed ({len(related)})"}


def queue_books(cfg, log, store, dry=False):
    """Phase C: every Book Summary written by the book route since analysis.since gets one analyze_long item
    (released: the book itself was already approved). Returns report rows."""
    a = acfg(cfg)
    if not a.get("books", True):
        return []
    rows = []
    since = a.get("since") or ""
    have = {(it.get("source_ref") or {}).get("text_from") for it in store.all() if Q.route_of(it) == "analyze_long"}
    vault = Path(cfg["vault_path"])
    for it in store.all():
        if Q.route_of(it) != "book" or it.get("status") != "done" or it["id"] in have:
            continue
        rel = ((it.get("result") or {}).get("notes") or [None])[0]
        if not rel or (it.get("updated") or "") < since:
            continue
        p = vault / rel
        if not p.exists() and (vault / VN.follow(rel)).exists():   # renamed by vault_names (2026-10-10)
            rel = VN.follow(rel)
            p = vault / rel
        if not p.exists():                      # renamed or moved when filed: find it by name
            hit = next(iter(vault.rglob(Path(rel).name)), None) \
                or next((h for nm in VN.summary_names(Path(rel).stem) for h in vault.rglob(nm + ".md")), None)
            if not hit:
                continue
            p, rel = hit, hit.relative_to(vault).as_posix()
        if rel.split("/")[0] in a["skip_folders"]:   # wait until the morning filing has moved it out of Inbox
            continue
        name = p.stem
        if any(p.with_name(x + ".md").exists() for x in VN.analysis_names(name)):
            continue
        if dry:
            rows.append((it.get("title") or name, "analyze_long", "would queue a per-chapter analysis"))
            continue
        x = store.create(source="vault", kind="note", title=it.get("title") or name, route="analyze_long",
                         source_ref={"path": rel, "name": name, "mode": "new", "force": False, "text_from": it["id"]})
        x["status"] = "ready"
        x["manual"]["released"] = True
        x["words"] = int((it.get("file") or {}).get("words") or 0) or None
        store.save(x)
        rows.append((it.get("title") or name, "analyze_long", "queued: per-chapter analysis (night window)"))
    return rows


def estimate(a, words, reg_n, cand_n):
    tin = int(words * 1.8) + reg_n * 25 + cand_n * 45 + 2000   # pilot: ~1.3x the first guess
    usd = tin / 1e6 * a["usd_in_per_mtok"] + a["out_tokens"] / 1e6 * a["usd_out_per_mtok"]
    return tin, round(usd, 3)


def sent_register(reg, a=None):
    """The register entries Claude is shown: approved concepts, plus pending ones if analysis.register_pending."""
    a = a or DEFAULTS
    return [c for c in reg if not c["pending"] or a.get("register_pending")]


def register_system(reg, a=None):
    """Concept names (and aliases) for the system prompt: identical for every call while the register is unchanged,
    so Claude's prompt cache serves it after the first call of a run (2026-11-13)."""
    lines = ["CONCEPT REGISTER (names of the existing concepts in «owner's» vault; when one fits, even loosely, use its "
             "exact name with \"new\": false; descriptions of the entries most likely to matter for an item are given "
             "with the item under CONCEPT DETAILS):"]
    lines += [f"- {c['name']}" + (f" (also: {', '.join(c['aliases'])})" if c["aliases"] else "")
              + (" [pending]" if c["pending"] else "") for c in sent_register(reg, a)] or ["(empty so far)"]
    return "\n".join(lines)


def concept_detail(reg, text, a=None):
    """Descriptions of the register_detail sent-register concepts whose words occur most in `text`."""
    a = a or DEFAULTS
    k = int(a.get("register_detail") or 0)
    if not k or not text:
        return []
    tf = {}
    for w in _toks(text[:200000]):
        tf[w] = tf.get(w, 0) + 1
    scored = []
    for c in sent_register(reg, a):
        ws = set(_toks(c["name"] + " " + " ".join(c["aliases"])))
        if not ws or not c["description"]:
            continue
        hit = sum(1 for w in ws if w in tf)
        if hit:
            scored.append((hit / len(ws), sum(tf.get(w, 0) for w in ws), c["name"].lower(), c))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [x[3] for x in scored[:k]]


def build_material(n, text, reg, cands, mine="", see=(), a=None):
    lines = [f"ITEM: {n['title']} [{n['type']}]" + (f"; tags: {', '.join(n['tags'])}" if n["tags"] else ""),
             f"Description: {n['description']}" if n["description"] else "", "",
             "CONCEPT DETAILS (register entries most likely relevant here; the full list of names is in the system prompt):"]
    lines += [f"- {c['name']} - {c['description'][:200]}" for c in concept_detail(reg, text, a)] or ["(none)"]
    lines += ["", "CANDIDATE NOTES (for related):"]
    lines += [f"- \"{m['name']}\" [{m['type']}]" + (" (owner's see_also)" if m["name"] in see else "")
              + (f" - {m['description'][:200]}" if m["description"] else "") for m in cands] or ["(none)"]
    if mine:
        lines += ["", C.personalize("«OWNER'S» NOTES ON THIS ITEM (the owner's own views, not part of the item):"), "", mine]
    lines += ["", "ITEM TEXT:", "", text]
    return "\n".join(x for x in lines if x is not None)


# ---------------------------------------------------------------- the route
def _hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _cite(c):
    c = _norm(c)
    return f" ({c})" if c else ""


# (A) quotes are checked against the item's text; what can't be found is marked, never silently kept
QMAP = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                      "\u00a0": " ", "\u00ad": None, "*": None, "_": None, "`": None, "#": None})


def _nq(s):
    import unicodedata
    s = unicodedata.normalize("NFKC", str(s or "")).translate(QMAP).lower()
    s = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", s)          # [[a|b]] -> b
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)                      # [b](url) -> b
    return re.sub(r"\s+", " ", s).strip(" .,;:'\"")


def quote_found(quote, hay):
    """True / False, or None when no usable quote was given. hay = _nq(item text)."""
    q = _nq(quote)
    if len(q.split()) < 4:
        return None
    parts = [p.strip(" .,;:'\"") for p in re.split(r"\s*(?:\.\.\.|\u2026)\s*", q)]
    parts = [p for p in parts if len(p) >= 8]
    return bool(parts) and all(p in hay for p in parts)


class Quotes:
    def __init__(self, text):
        self.hay, self.ok, self.bad = _nq(text), 0, 0

    def cite(self, cite, quote):
        """ (p. 3: "exact words")  |  (p. 3) *(unverified)*"""
        c, found = _norm(cite), quote_found(quote, self.hay)
        if found:
            self.ok += 1
            q = _norm(quote)
            return f" ({c + ': ' if c else ''}\u201c{q}\u201d)"
        self.bad += 1
        return (f" ({c})" if c else "") + " *(unverified)*"


LATER = "## Later items that refer to this"
PHRASE = {"agrees": "agrees with this", "disagrees": "disagrees with this", "extends": "extends this",
          "applies": "applies this", "same subject": "covers the same subject", "background": "draws on this as background"}


def _later_section(body):
    m = re.search(r"(?ms)^" + re.escape(LATER) + r"\s*\n.*?(?=^## |\Z)", body)
    return m.group(0).rstrip() + "\n" if m else ""


def backlink(cfg, vault, notes, n, related):
    """(C) Tell the older items' Analysis notes (Claude's own; never the owner's notes) about this new item.
    related = [(target item name, relation, why)]. Returns the names updated."""
    import yaml
    done = []
    for target, rl, why in related:
        an = VN.find(notes, VN.analysis_names(target))
        if not an or an["type"] != "Analysis":
            continue
        p = vault / an["rel"]
        t = p.read_text(encoding="utf-8")
        fm, _, body = split_frontmatter(t)
        if not isinstance(fm, dict) or fm.get("verified"):
            continue
        edited = fm.get("analysis_hash") and fm.get("analysis_hash") != _hash(body.strip())
        line = f"- [[{n['name']}]] {PHRASE.get(rl, rl)}: {why}"
        if LATER in body:
            lines = [ln for ln in body.splitlines() if not ln.startswith(f"- [[{n['name']}]] ")]
            i = lines.index(LATER) + 1
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("- ")):
                i += 1
            while i > 0 and not lines[i - 1].strip():
                i -= 1
            lines.insert(i, line)
            body = "\n".join(lines).rstrip() + "\n"
        else:
            body = body.rstrip() + f"\n\n{LATER}\n\n{line}\n"
        if not edited:
            fm["analysis_hash"] = _hash(body.strip())
        p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + body,
                     encoding="utf-8", newline="\n")
        done.append(target)
    return done


def _closest(name, reg):
    """The existing concept most like a proposed one (for the 'merge into' option), or None."""
    a = set(_toks(name))
    best, score = None, 0.0
    for c in reg:
        if c["pending"]:
            continue
        b = set(_toks(c["name"] + " " + " ".join(c["aliases"])))
        if a and b:
            s = len(a & b) / len(a | b)
            if s > score:
                best, score = c["name"], s
    return best if score >= 0.25 else None


def do_analyze(cfg, log, store, item, dry, work):
    import routes as RT
    a = acfg(cfg)
    vault = Path(cfg["vault_path"])
    ref = item.get("source_ref") or {}
    notes = scan(vault)
    now_rel = VN.follow(ref.get("path"))                         # renamed by vault_names since it was queued
    n = next((x for x in notes.values() if x["rel"] in (ref.get("path"), now_rel)), None) \
        or VN.find(notes, [ref.get("name") or ""] + VN.summary_names(ref.get("name") or ""))
    if not n:
        return {"ok": False, "needs": "you", "why": f"note not found: {ref.get('path')}"}
    if n["rel"] != ref.get("path"):
        ref["path"] = n["rel"]                                    # moved since it was queued
    aname = VN.analysis_name(n["name"])
    old = VN.find(notes, VN.analysis_names(n["name"]))
    if old and ref.get("mode") != "redo":
        return {"ok": True, "note": old["rel"], "why_done": "already has an analysis"}
    if old and not ref.get("force"):
        ofm, obody = _body(vault, old["rel"])
        if ofm.get("verified"):
            return {"ok": False, "needs": "you", "why": "the analysis is verified; redo with --force to replace it"}
        # 2026-10-19: the hash is written over the whole body (text_body), but _body() drops the ## Original
        # section and ![[embeds]], so compare with both; str() because YAML reads an all-digit hash as a number
        full = split_frontmatter((vault / old["rel"]).read_text(encoding="utf-8", errors="replace"))[2].strip()
        if ofm.get("analysis_hash") and str(ofm.get("analysis_hash")) not in (_hash(obody), _hash(full)):
            return {"ok": False, "needs": "you", "why": "the analysis was edited by hand; redo with --force to replace it"}
    if ref.get("related_only") and old:
        return do_related(cfg, log, store, item, n, notes, a, old)
    if not ref.get("text_from") and n["type"] == "Book Summary":   # a redo of a book uses the book again, not its summary
        prior = next((x for x in store.all() if Q.route_of(x) == "analyze_long" and x["id"] != item["id"]
                      and (x.get("source_ref") or {}).get("text_from")
                      and (x.get("source_ref") or {}).get("name") == n["name"]), None)
        if prior:
            ref["text_from"] = prior["source_ref"]["text_from"]
            item["words"] = prior.get("words")
    text = material_of(vault, notes, n)
    words = len(text.split())
    if ref.get("text_from"):
        words = int(item.get("words") or 0) or words
    if words < a["min_words"]:
        return {"ok": True, "note": None, "status": "skipped", "why_done": f"too short to analyze ({words} words)"}
    try:   # title check (2026-10-06): never analyze a note still named after its file
        import titlecheck
        held = titlecheck.check_note(cfg, log, store, item, n, text)
        if held:
            return {"ok": False, "needs": "confirm", "why": held}
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "LimitHit":
            raise
        log(f"  title check skipped: {type(e).__name__}: {e}")
    split = needs_split(a, text, words, ref)
    if split and Q.route_of(item) != "analyze_long":
        # phase C (2026-10-06): long items are analyzed in parts, in the night window under pacing
        item["route"] = "analyze_long"
        item["words"] = words
        Q.event(item, f"analyze -> analyze_long ({words:,} words: analyzed in parts at night)", cfg)
        store.save(item)
        return {"ok": False, "later": True, "why": "long item: analyzed in parts in the night window"}
    reg = register(vault, notes)
    cands = candidates(vault, notes, n, int(a["candidates"]), cfg)
    mine, mine_from, mine_hash, see = my_notes(vault, notes, n, a)
    forced = [notes[x] for x in see if x in notes and x != n["name"]]      # The owner's see_also: always candidates
    cands = forced + [c for c in cands if c["name"] not in {f["name"] for f in forced}]
    tin, usd = estimate(a, words, len(sent_register(reg, a)) // 10 + 2 * int(a.get("register_detail") or 0), len(cands))
    item["estimate"] = {"tokens_in": tin, "usd": usd}
    # Confirm rule in % of the weekly Claude limit, not dollars (2026-10-06): runs use his
    # subscription through claude -p, so the $ figure is only an API-equivalent estimate.
    import pacing
    wk = pacing.week_share(cfg, words, "analyze_long" if split else "analyze")
    item["estimate"]["week_pct"] = wk
    limit = float(Q.qcfg(cfg).get("confirm_over_week_pct") or 0)
    if limit and wk > limit and not (item.get("manual") or {}).get("released"):
        item.setdefault("work", {})["hold_kind"] = "costly"
        return {"ok": False, "needs": "confirm",
                "why": f"analysis of {words:,} words, about {wk:.1f}% of your weekly Claude limit"}
    if dry:
        return {"ok": True, "dry": True, "would": f"analyze {words:,} words{' in parts' if split else ''} "
                                                  f"(~{tin:,} tokens in, ~{wk:.1f}% of the week)"}
    if split:
        return do_parts(cfg, log, store, item, n, notes, text, a, reg, cands, mine, mine_from, mine_hash, see, old)
    instr = PROMPT.replace("MAXC", str(a["concepts_max"])).replace("MAXR", str(a["related_max"]))
    mat = build_material(n, text, reg, cands, mine, see, a)
    rid = "analysis-" + _hash(instr + mat)[:10]
    ans = llm.run(cfg, "analyze", [{"id": rid, "instructions": instr, "material": mat, "timeout": 900,
                                    "system": register_system(reg, a)}],
                  store.payload(item["id"]) / "answers", log, item["id"])
    raw = RT.text_of(ans, rid)                                   # raises LimitHit on a usage limit
    if not raw:
        return {"ok": False, "why": "no answer: " + str((ans.get(rid) or {}).get("error"))[:200]}
    j = RT.as_json(raw)
    if not isinstance(j, dict) or not j.get("in_brief"):
        bad = store.payload(item["id"]) / "answers" / f"{rid}.json"
        if bad.exists():
            bad.rename(bad.with_suffix(".bad.json"))             # let a retry ask again
        return {"ok": False, "why": "the answer was not the JSON asked for"}
    u = (ans.get(rid) or {})
    item.setdefault("work", {})["usage"] = {"calls": 1, "cost_usd": u.get("cost_usd"), "usage": u.get("usage"),
                                            "models": u.get("models")}
    if old:
        bdir = vault / ".analysis-backups" / C.now(cfg).strftime("%Y%m%d-%H%M%S")
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vault / old["rel"], bdir / Path(old["rel"]).name)
    before = snapshot(vault, old["rel"]) if old else None
    rel, related, qs = write_analysis(cfg, vault, notes, n, j, reg, cands, a, old, text, (mine_from, mine_hash, see))
    if before:
        item["work"]["changes"] = changes(before, snapshot(vault, rel))
    told = backlink(cfg, vault, notes, n, related)
    if told:
        log(f"  later-items line added to {len(told)} older analysis note(s)")
    try:   # the work's notes cross-linked in see_also, kept in one place (families.py, 2026-10-13)
        import families
        families.regroup(cfg, log, only=[n["name"]])
    except Exception as e:  # noqa: BLE001
        log(f"  families regroup failed: {type(e).__name__}: {e}")
    C.run(log, C.VL / "build_concepts.py", vault)
    C.run(log, C.VL / "build_indexes.py", vault, "--folders", rel.split("/")[0])
    cost =f", ${u['cost_usd']:.2f}" if isinstance(u.get("cost_usd"), (int, float)) else ""
    item["work"]["quotes"] = {"verified": qs.ok, "unverified": qs.bad}
    return {"ok": True, "note": rel, "why_done": f"analysis written{cost}; quotes {qs.ok} verified, {qs.bad} unverified"}


def write_analysis(cfg, vault, notes, n, j, reg, cands, a, old=None, text="", mine=((), "", ()), part=None, parts_links=()):
    """The item's Analysis note, or (part=...) one Part Analysis note (phase C, 2026-10-06)."""
    byname = {c["name"].lower(): c["name"] for c in reg if not c["pending"]}
    for c in reg:
        if not c["pending"]:
            for al in c["aliases"]:
                byname.setdefault(al.lower(), c["name"])
    pend = load_pending()
    rejected = {x.lower() for x in pend["rejected"]}
    pend_by = {k.lower(): k for k in pend["pending"]}
    for k, p in pend["pending"].items():          # names merged into a pending concept (concept_merge, 2026-11-13)
        for al in p.get("aliases") or []:
            pend_by.setdefault(al.lower(), k)
    aname = part["name"] if part else VN.analysis_name(n["name"])
    qs = Quotes(text)
    concepts, pending, lines_c = [], [], []
    for c in (j.get("concepts") or [])[:int(a["concepts_max"])]:
        name = _norm(c.get("name")).strip(".")
        if not name or name.lower() in rejected:
            continue
        use = _norm(c.get("use"))
        if name.lower() in byname:
            nm = byname[name.lower()]
            if nm in concepts:
                continue
            concepts.append(nm)
            lines_c.append(f"- [[{nm}]] — {use}{qs.cite(c.get('cite'), c.get('quote'))}")
            continue
        nm = pend_by.get(name.lower()) or name[:1].upper() + name[1:]
        if nm in pending:
            continue
        p = pend["pending"].setdefault(nm, {"definition": _norm(c.get("definition")), "why_new": _norm(c.get("why_new")),
                                            "first_from": n["name"], "added": C.now(cfg).isoformat(timespec="seconds"),
                                            "merge_into": _closest(nm, reg), "analyses": []})
        if aname not in p["analyses"]:
            p["analyses"].append(aname)
        pend_by[nm.lower()] = nm
        pending.append(nm)
        lines_c.append(f"- `{nm}`{PENDING_MARK} — {use}{qs.cite(c.get('cite'), c.get('quote'))}")
    cand_names = {m["name"].lower(): m["name"] for m in cands}
    rel_lines, seen, related = [], set(), []
    for r in (j.get("related") or []):
        nm = cand_names.get(_norm(r.get("note")).strip('"').lower())
        rl = _norm(r.get("relation")).lower()
        if not nm or nm in seen or rl not in RELATIONS:
            continue
        mine_see = nm in (mine[2] if len(mine) > 2 else ())
        if not mine_see and sum(1 for x in related if x[0] not in (mine[2] if len(mine) > 2 else ())) >= int(a["related_max"]):
            continue
        seen.add(nm)
        related.append((nm, rl, _norm(r.get("why"))))
        rel_lines.append(f"- **{rl}** — [[{nm}]]: {_norm(r.get('why'))}")
    for nm in (mine[2] if len(mine) > 2 else ()):            # The owner's see_also always appears
        if nm in notes and nm not in seen and nm != n["name"]:
            seen.add(nm)
            related.append((nm, "same subject", "you linked it (see_also)"))
            rel_lines.append(f"- **same subject** — [[{nm}]]: you linked it (see_also)")
    arg = j.get("argument") if j.get("argues") and isinstance(j.get("argument"), dict) else None
    now = C.now(cfg)
    if part:
        body = [f"Part {part['num']} of [[{n['name']}]]: *{part['title']}*" + (f" ({part['span']})" if part.get("span") else "")
                + f" — a draft written by Claude on {now:%Y-%m-%d}. Whole-work analysis: [[{VN.analysis_name(n['name'])}]].", "",
                "## Summary", "", part.get("summary") or "", "", "## In brief", "", _norm(j.get("in_brief")), ""]
    else:
        body = [f"Analysis of [[{n['name']}]] — a draft written by Claude on {now:%Y-%m-%d}; edit freely "
                "(a later redo keeps a note you have edited unless you force it).", "", "## In brief", "", _norm(j.get("in_brief")), ""]
    if arg:
        body += ["## Argument", "", f"**Thesis:** {_norm(arg.get('thesis'))}{qs.cite(arg.get('thesis_cite'), arg.get('thesis_quote'))}",
                 "", "**Reasons given:**", ""]
        for i, p in enumerate(arg.get("premises") or [], 1):
            ev = "; ".join(_norm(x) for x in (p.get("evidence") or []) if _norm(x))
            body.append(f"{i}. {_norm(p.get('claim'))}{qs.cite(p.get('cite'), p.get('quote'))}" + (f"  \n   *Evidence:* {ev}" if ev else ""))
        obj = [o for o in (arg.get("objections") or []) if _norm(o.get("objection"))]
        if obj:
            body += ["", "**Objections answered:**", ""]
            body += [f"- {_norm(o.get('objection'))} → {_norm(o.get('reply'))}{qs.cite(o.get('cite'), o.get('quote'))}" for o in obj]
        if _norm(arg.get("conclusion")):
            body += ["", f"**Conclusion:** {_norm(arg.get('conclusion'))}"]
        body.append("")
    if lines_c:
        body += ["## Key concepts", ""] + lines_c + [""]
    claims = [c for c in (j.get("claims") or []) if _norm(c.get("claim"))]
    if claims:
        body += ["## Notable claims", ""] + [f"- {_norm(c.get('claim'))}{qs.cite(c.get('cite'), c.get('quote'))}" for c in claims] + [""]
    yn = _norm(j.get("your_notes"))
    if mine[1] and yn:
        body += ["## Your notes", "", yn]
        if mine[0]:
            body += ["", "From: " + ", ".join(f"[[{x}]]" for x in mine[0])]
        body.append("")
    if parts_links:
        body += ["## Parts", ""] + [f"{i}. [[{x}|{VN.part_label(x)}]]" for i, x in enumerate(parts_links, 1)] + [""]
    if not part:
        body += ["## Related", ""] + (rel_lines or ["- none found yet"]) + [""]
    if qs.bad:
        body += [f"*{qs.bad} cite(s) marked unverified: no quote supporting them was found word for word in the item's text.*", ""]
    if old:                                    # (C) keep what later items said about this one
        _, obody = _body(vault, old["rel"])
        keep = _later_section(obody)
        if keep:
            body += [keep.rstrip(), ""]
    text_body = "\n".join(body).rstrip() + "\n"
    if part:
        fm = {"type": "Part Analysis", "title": f"{n['title']} - Part {part['num']}: {part['title']}"[:250],
              "description": (_norm(j.get("in_brief")) or f"Part {part['num']} of {n['title']}.")[:300],
              "analysis_of": f"[[{n['name']}]]", "part_of": f"[[{VN.analysis_name(n['name'])}]]", "part": part["num"],
              "argues": bool(arg), "concepts": concepts}
        if part.get("span"):
            fm["pages"] = part["span"]
    else:
        fm = {"type": "Analysis", "title": f"{n['title']} (Analysis)",
              "description": _norm(j.get("description"))[:300] or f"Analysis of {n['title']}.",
              "analysis_of": f"[[{n['name']}]]", "argues": bool(arg), "concepts": concepts}
        if parts_links:
            fm["parts"] = len(parts_links)
    if pending:
        fm["concepts_pending"] = pending
    fm["notes_hash"] = mine[1]
    fm.update({"quotes_verified": qs.ok, "quotes_unverified": qs.bad,
               "generated": {"by": GEN, "at": now.isoformat(timespec="seconds")}, "status": "draft",
               "analysis_hash": _hash(text_body.strip())})
    import yaml
    out = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n" + text_body
    if part:
        path = Path(part["path"])
    else:
        path = (vault / old["rel"]) if old else (vault / n["rel"]).with_name(VN.analysis_name(n["name"]) + ".md")
    path.write_text(out, encoding="utf-8", newline="\n")
    save_pending(pend)
    return path.relative_to(vault).as_posix(), related, qs


# ---------------------------------------------------------------- concept approvals (06:00 email / web page)
def concept_items(p):
    """How many items (works) use a pending concept: a book's chapter analyses and its whole-work analysis count
    once (vault_names.work_of)."""
    return len({VN.work_of(a) for a in p.get("analyses") or []})


def concept_where(p):
    """The question's 'where' line: how many items use the concept (2026-11-23, the owner), then where it began."""
    k, n = concept_items(p), len(p.get("analyses") or [])
    return (f"Linked to {k} item{'s' if k != 1 else ''}"
            + (f" ({n} analyses, counting chapters separately)" if n != k else "")
            + f"; first proposed in the analysis of \"{p.get('first_from')}\"")


def concept_questions(cfg, first_id, limit):
    """New concepts waiting for the owner, as queue-style questions (organize.queue_questions)."""
    out, i = [], first_id
    a = acfg(cfg)
    pend = load_pending()
    for d in pend.get("dup_pairs") or []:                   # (E) found by the weekly concept check
        if len(out) >= limit:
            return out
        k, m = d["keep"], d["merge"]
        out.append({"id": i, "kind": "concept_dup", "item": f"concept-dup:{m}>{k}", "title": f"{m} / {k}",
                    "keep": k, "merge": m, "text": f"These concepts look like the same idea ({d.get('why', '')})",
                    "where": f"\"{k}\" is used by {d.get('keep_uses', '?')} analyses, \"{m}\" by {d.get('merge_uses', '?')}",
                    "size": "", "why": "", "options": {
                        "a": {"label": f"Merge \"{m}\" into \"{k}\"", "action": "dup_merge"},
                        "b": {"label": f"Merge \"{k}\" into \"{m}\"", "action": "dup_merge_rev"},
                        "c": {"label": "They're different ideas (don't ask again)", "action": "dup_keep"},
                        "d": {"label": "Not now (ask again next week)", "action": None}}})
        i += 1
    today = C.now(cfg).strftime("%Y-%m-%d")
    later = int(a.get("later_days") or 7)
    # Most-used first (2026-11-23, the owner): oldest-first showed the same few concepts every morning while
    # 296 waited. A concept many analyses share matters most; ties go to the oldest.
    ranked = sorted(pend["pending"].items(), key=lambda kv: (-concept_items(kv[1]), -len(kv[1].get("analyses") or []),
                                                             kv[1].get("added") or ""))
    for name, p in ranked:
        if concept_items(p) < int(a["ask_after_uses"]) and not p.get("ask"):
            continue                                          # (B) asked once a second item uses it
        if (p.get("snooze_until") or "") > today:
            continue                                          # "Not now": back after later_days
        opts = {"a": {"label": "Add it as a concept note", "action": "concept_add"}}
        if p.get("merge_into"):
            opts["b"] = {"label": f"Same as the existing concept \"{p['merge_into']}\" (link there; keep this name as an alias)",
                         "action": "concept_merge"}
        opts["c"] = {"label": "Not a concept worth a note (keep it as plain text)", "action": "concept_reject"}
        opts["d"] = {"label": f"Not now (ask again in {later} days)", "action": "concept_later"}
        k = concept_items(p)
        # the count in the heading too (2026-11-23, the owner); answers use "concept", never the title
        out.append({"id": i, "kind": "concept", "item": f"concept:{name}", "concept": name,
                    "title": f"{name} (linked to {k} item{'s' if k != 1 else ''})",
                    "text": p.get("definition") or "(no definition)",
                    "where": concept_where(p),
                    "size": "", "why": p.get("why_new") or "", "options": opts})
        i += 1
        if len(out) >= limit:
            break
    return out


def _sub_in_analyses(vault, notes, analyses, fn):
    for an in analyses:
        n = notes.get(an)
        if not n:
            continue
        p = vault / n["rel"]
        t = p.read_text(encoding="utf-8")
        fm, _, body = split_frontmatter(t)
        if not isinstance(fm, dict):
            continue
        fm, body = fn(fm, body)
        import yaml
        edited_by_hand = fm.get("analysis_hash") and fm.get("analysis_hash") != _hash(split_frontmatter(t)[2].strip())
        if not edited_by_hand:
            fm["analysis_hash"] = _hash(body.strip())
        p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + body,
                     encoding="utf-8", newline="\n")


def merge_concepts(cfg, keep, merge):
    """(E) Fold concept note `merge` into `keep`: the analyses link to `keep` (showing the old name), `keep` gets
    the old name as an alias, and the `merge` note goes to Archives as a duplicate (links to it still resolve)."""
    import yaml
    vault = Path(cfg["vault_path"])
    notes = scan(vault)
    if keep not in notes or merge not in notes or notes[keep]["type"] != "Concept" or notes[merge]["type"] != "Concept":
        return f"{merge} / {keep}: one of them is gone or isn't a concept note; nothing changed"
    mfm = notes[merge]["fm"]
    kp = vault / notes[keep]["rel"]
    fm, _, body = split_frontmatter(kp.read_text(encoding="utf-8"))
    al = fm.get("aliases") or []
    al = al if isinstance(al, list) else [al]
    for x in [merge] + list(mfm.get("aliases") or []):
        if x not in al and x != keep:
            al.append(x)
    fm["aliases"] = al
    kp.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + body,
                  encoding="utf-8", newline="\n")
    users = [x["name"] for x in notes.values() if x["type"] in ("Analysis", "Part Analysis")
             and merge in [str(c) for c in (x["fm"].get("concepts") or [])]]

    def fn(afm, abody):
        abody = abody.replace(f"[[{merge}]]", f"[[{keep}|{merge}]]").replace(f"[[{merge}|", f"[[{keep}|")
        cs = []
        for c in afm.get("concepts") or []:
            c = keep if c == merge else c
            if c not in cs:
                cs.append(c)
        afm["concepts"] = cs
        return afm, abody
    _sub_in_analyses(vault, notes, users, fn)
    mp = vault / notes[merge]["rel"]
    dest = C.vpath(vault, "Archives") / mp.name
    mfm2, _, mbody = split_frontmatter(mp.read_text(encoding="utf-8"))
    mfm2.update({"status": "deprecated", "duplicate_of": f"[[{keep}]]", "archived_from": notes[merge]["rel"].rsplit("/", 1)[0]})
    if dest.exists():
        return f"{merge}: merged into \"{keep}\", but Archives already has a note of that name, so it was left in place"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text("---\n" + yaml.safe_dump(mfm2, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + mbody,
                    encoding="utf-8", newline="\n")
    mp.unlink()
    return f"{merge}: merged into \"{keep}\" ({len(users)} analyses relinked; the old note is in Archives)"


def apply_concept(cfg, log, q, action):
    """Carry out the owner's answer for one pending concept -> a result line. Caller holds the vault lock."""
    vault = Path(cfg["vault_path"])
    if q.get("kind") == "concept_dup":
        if not action:
            return None
        pend = load_pending()
        k, m = q["keep"], q["merge"]
        pend["dup_pairs"] = [d for d in pend.get("dup_pairs") or [] if {d["keep"], d["merge"]} != {k, m}]
        if action == "dup_keep":
            pend.setdefault("not_duplicates", []).append(sorted([k, m]))
            save_pending(pend)
            return f"{m} / {k}: kept apart; not asked again"
        save_pending(pend)
        return merge_concepts(cfg, k, m) if action == "dup_merge" else merge_concepts(cfg, m, k)
    pend = load_pending()
    name = q["concept"]
    p = pend["pending"].get(name)
    if not p:
        return f"{name}: already handled"
    if action == "concept_later":      # 2026-11-23: "Not now" means a week, not tomorrow
        import datetime as _dt
        days = int(acfg(cfg).get("later_days") or 7)
        p["snooze_until"] = (C.now(cfg) + _dt.timedelta(days=days)).strftime("%Y-%m-%d")
        save_pending(pend)
        return f"{name}: asked again after {p['snooze_until']}"
    if action == "concept_wait":       # review of a one-item concept: keep waiting for a second item to use it
        p.setdefault("review", {})["answer"] = "wait"
        save_pending(pend)
        return f"{name}: left pending until another item uses it"
    notes = scan(vault)
    mark = f"`{name}`{PENDING_MARK}"

    def relink(target_link, into_name):
        def fn(fm, body):
            body = body.replace(mark, target_link)
            fm["concepts_pending"] = [x for x in (fm.get("concepts_pending") or []) if x != name]
            if not fm["concepts_pending"]:
                fm.pop("concepts_pending")
            if into_name:
                cs = list(fm.get("concepts") or [])
                if into_name not in cs:
                    cs.append(into_name)
                fm["concepts"] = cs
            return fm, body
        return fn

    if action == "concept_add":
        fname = name if name not in notes else f"{name} (concept)"
        d = vault / "Concepts"
        d.mkdir(exist_ok=True)
        definition = p.get("definition") or ""
        first = re.split(r"(?<=[.!?])\s", definition, 1)[0] if definition else f"The concept {name}."
        fm = {"type": "Concept", "title": name, "description": first[:300], "aliases": [name] if fname != name else [],
              "generated": {"by": GEN, "at": C.now(cfg).isoformat(timespec="seconds")}, "status": "draft"}
        import yaml
        (d / f"{fname}.md").write_text(
            "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n" + definition + "\n\n"
            + f"*Draft definition by Claude, first proposed in [[{VN.analysis_name(p.get('first_from'))}]]; edit freely.*\n",
            encoding="utf-8", newline="\n")
        link = f"[[{fname}]]" if fname == name else f"[[{fname}|{name}]]"
        _sub_in_analyses(vault, notes, p.get("analyses") or [], relink(link, fname))
        line = f"{name}: concept note added"
    elif action == "concept_merge":
        into = p.get("merge_into")
        if not into or into not in notes:
            return f"{name}: the concept to merge into is gone; asked again tomorrow"
        cp = vault / notes[into]["rel"]
        t = cp.read_text(encoding="utf-8")
        fm, _, body = split_frontmatter(t)
        al = fm.get("aliases") or []
        al = al if isinstance(al, list) else [al]
        if name not in al:
            al.append(name)
        fm["aliases"] = al
        import yaml
        cp.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + body,
                      encoding="utf-8", newline="\n")
        _sub_in_analyses(vault, notes, p.get("analyses") or [], relink(f"[[{into}|{name}]]", into))
        line = f"{name}: linked to the existing concept \"{into}\""
    elif action == "concept_reject":
        _sub_in_analyses(vault, notes, p.get("analyses") or [], relink(name, None))
        pend["rejected"].append(name)
        line = f"{name}: kept as plain text; not proposed again"
    else:
        return None
    pend["pending"].pop(name, None)
    save_pending(pend)
    return line


def refresh_after_concepts(cfg, log):
    vault = Path(cfg["vault_path"])
    C.run(log, C.VL / "build_concepts.py", vault)
    C.run(log, C.VL / "build_indexes.py", vault, "--folders", "Concepts")


def recent(cfg, hours=26):
    """Analyses written in the last `hours` (for the daily email): [{title, note, concepts, pending, related}]."""
    store = Q.Store(cfg)
    cut = C.now(cfg) - dt.timedelta(hours=hours)
    vault = Path(cfg["vault_path"])
    out = []
    for it in store.all():
        if Q.route_of(it) not in ("analyze", "analyze_long") or it.get("status") != "done" or not (it.get("result") or {}).get("notes"):
            continue
        try:
            if dt.datetime.fromisoformat(it.get("updated") or "") < cut:
                continue
        except ValueError:
            continue
        rel = it["result"]["notes"][0]
        p = vault / rel
        if not p.exists():
            continue
        fm, _, body = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        fm = fm if isinstance(fm, dict) else {}
        rels = re.findall(r"(?m)^- \*\*([a-z ]+)\*\* — \[\[([^\]|]+)", body)
        out.append({"title": it.get("title") or rel, "note": rel, "concepts": len(fm.get("concepts") or []),
                    "pending": len(fm.get("concepts_pending") or []), "related": rels,
                    "changes": (it.get("work") or {}).get("changes"),
                    "redo": (it.get("source_ref") or {}).get("mode") == "redo"})
    return out
