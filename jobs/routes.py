"""File routes for the intake queue (plan section 5): extraction step, auto, article, document,
recipe, manual; book and summary (costly, phase 3) are in longdoc.py.

Each route gets a "doc": the extracted text and metadata (payload\\<id>\\extract.json + text.md), and
returns {"ok", "note"} / {"ok": False, "why", "needs"} / {"later": True} like the email and link routes.
Notes are written by lib\\vault-librarian\\scripts\\file_to_note.py, which also copies the original into
attachments/Sources/. Model work goes through llm.py (claude -p now; Batch API later).
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import extract as X  # noqa: E402
import llm  # noqa: E402
import vqueue as Q  # noqa: E402
import vault_match  # noqa: E402

GEN_CLAUDE = "claude/claude-code-cli"
DESCRIBE = ("Write one sentence (max 30 words) describing the text on stdin, for a note's `description` "
            "field. Output only the sentence.")
CLASSIFY = """Decide how to file the document on stdin (its first pages, plus its file name, page and word count)
into «owner's» Obsidian vault. Choose ONE route:
  article  - a published article, essay, post, column or paper to keep in full
  recipe   - a recipe
  manual   - an owner's/user manual, installation guide, warranty or product documentation
  book     - a book (usually over 100 pages)
  summary  - a long report, study or other long document best kept as a detailed summary
  document - anything else to keep in full (letters, notes, reference sheets, short reports)
Output ONLY JSON: {"route": "...", "reason": "a few words"}"""
RECIPE = """The document on stdin is a recipe (the owner's note on it, if any, comes first). Extract it faithfully:
copy quantities, units and steps as written; never invent anything; omit fields the recipe doesn't state.
Output ONLY JSON:
{"title": "...", "description": "one sentence", "servings": "...", "prep_time": "...", "cook_time": "...",
 "cuisine": "...", "course": "...", "source": "author/book/site if stated",
 "ingredients": ["2 cups flour", "..."], "steps": ["...", "..."], "notes": ["..."]}"""
MANUAL = """The text on stdin is the start of a product manual or product document (the owner's note on it, if any,
comes first; then the table of contents with page numbers, if the PDF has one; then the first pages).
Never guess: omit anything not stated. serial and purchased come ONLY from the owner's note.
Output ONLY JSON:
{"title": "short title, e.g. 'Bosch SHX878 dishwasher - owner's manual'", "description": "one sentence",
 "brand": "...", "model": "...", "product": "e.g. dishwasher", "doc_kind": "owner's manual | installation guide |
 quick start | warranty | service manual | other", "serial": "...", "purchased": "...",
 "covers": "2-4 sentences: what the document covers",
 "key_sections": [{"title": "Cleaning the filter", "page": 34}]}"""
RECIPE_WORDS = re.compile(r"\b(ingredients?|tbsp|tablespoons?|teaspoons?|tsp|cups?|preheat|oven|simmer|bake|serves)\b", re.I)
MANUAL_WORDS = re.compile(r"(owner'?s manual|user'?s? manual|user guide|installation (instructions|guide)|"
                          r"model (no|number)|safety instructions|warranty|troubleshooting)", re.I)
COSTLY = ("book", "summary")


# ---------------------------------------------------------------- the owner's rules of 2026-09-28
def file_into_folder(cfg, store, item, route):
    """Something dropped at the top of the dropbox is moved into the subfolder for its route (Other when
    none fits), so the dropbox shows where each item went. Returns the folder name, or None."""
    ref = item.get("source_ref") or {}
    if item.get("source") != "folder" or item.get("parent") or "/" in (ref.get("path") or "/"):
        return None
    q = Q.qcfg(cfg)
    root = Path((q.get("dropbox_path") or "").strip() or "\0")
    src = root / ref["path"]
    try:
        if not (root.is_absolute() and root.is_dir() and src.exists() and root.resolve() in src.resolve().parents):
            return None
    except OSError:
        return None
    names = {v: k for k, v in q["route_folders"].items()}   # route -> folder name ("auto" -> Other)
    folder = names.get(route) or names.get("auto") or "Other"
    dest_dir = root / folder
    dest_dir.mkdir(exist_ok=True)
    dest, n = dest_dir / src.name, 2
    while dest.exists():
        dest, n = dest_dir / f"{src.stem} ({n}){src.suffix}", n + 1
    os.replace(src, dest)
    side = src.with_suffix(".txt")
    if src.suffix.lower() != ".txt" and side.exists() and not dest.with_suffix(".txt").exists():
        os.replace(side, dest.with_suffix(".txt"))
    ref["path"], ref["folder"] = f"{folder}/{dest.name}", f"Remember/{folder}"
    if item.get("file"):
        item["file"]["name"] = dest.name
    Q.event(item, f"moved into {folder} in the dropbox", cfg)
    return folder


def gate(cfg, store, item, doc):
    """Before a token-intensive route (book, summary) spends anything: a likely copy already in the vault,
    or a costly route Claude chose on its own, is held for the owner's OK in the daily email.
    Returns None to go ahead, or the reason the item was held."""
    m = item.get("manual") or {}
    if m.get("dup_ok"):
        return None
    f = item.get("file") or {}
    cands = vault_match.find(cfg, title_of(item, doc), doc.get("author") or [], f.get("sha256"), f.get("bytes"),
                             f.get("name"))
    what = "book" if Q.route_of(item) == "book" else "long document for a detailed summary"
    if cands:
        item["work"]["dup_of"] = cands
        item["work"]["hold_kind"] = "duplicate"
        b = cands[0]
        Q.set_status(item, "held", f"may already be in the vault as {b['note']} ({b['why']}); nothing spent until you decide",
                     "confirm", cfg)
        return item["why"]
    if item.get("route_from") == "classifier":
        item["work"]["hold_kind"] = "costly"
        import pacing
        wk = pacing.week_share(cfg, int(doc.get("words") or 0), Q.route_of(item))
        share = f", about {wk:.0f}% of your weekly Claude limit" if doc.get("words") else ""
        Q.set_status(item, "held", f"Claude thinks this is a {what} ({(doc.get('pages') or '?')} pages, "
                     f"{doc.get('words') or '?'} words{share}); the summary waits for your OK", "confirm", cfg)
        return item["why"]
    return None


def recheck(cfg, store, item):
    """Just before a book/summary starts in the night run (2026-10-13). The gate above runs when the file is
    extracted, which can be days earlier: the 2026-09-29 Books dropbox held several copies of the same reading
    (03_bastiat_detmar.pdf / 91_2_BastiatDetmar.pdf, two identical 52_author_*.pdf ...), all passed the gate
    while none was in the vault yet, and each was summarized on its own night. Looked at again here, against the
    vault as it is now (fresh index), each later copy is held as a possible duplicate. Returns the hold reason
    or None."""
    if (item.get("manual") or {}).get("dup_ok") or (item.get("result") or {}).get("notes"):
        return None
    if not (item.get("file") or {}).get("sha256") or (item.get("work") or {}).get("step"):
        return None                       # not a file, or work already started (cached answers): carry on
    try:
        doc = json.loads((store.payload_root / item["id"] / "extract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    vault_match._cache.clear()
    return gate(cfg, store, item, doc)


# ---------------------------------------------------------------- extraction step
def source_path(cfg, store, item):
    if item.get("source") == "folder" and not item.get("parent"):
        return Path(Q.qcfg(cfg)["dropbox_path"]) / item["source_ref"]["path"]
    return store.payload_root / item["id"] / (item.get("file") or {}).get("payload", "")


def markers_hit(text, cfg):
    q = Q.qcfg(cfg)
    low = text.lower()
    for m in q.get("confidential_markers") or []:
        if m.lower() in low:
            return m
    return None


def extract_item(cfg, log, store, item, dry):
    """new -> ready (or held). Returns a report string."""
    if (item.get("work") or {}).get("download_error"):
        Q.set_status(item, "held", "the attachment couldn't be downloaded: " + item["work"]["download_error"], "you", cfg)
        return item["why"]
    parent = store.get(item["parent"]) if item.get("parent") else None
    ext = (item.get("file") or {}).get("ext") or ""
    if parent and parent.get("route") == "email" and ext not in X.SUPPORTED:
        # an attachment on an ordinary email (spreadsheet, invite, image...): listed on the Email note, as before
        Q.set_status(item, "skipped", f"{ext or 'this'} attachment isn't filed on its own; it's listed on the email's note", cfg=cfg)
        return item["why"]
    src = source_path(cfg, store, item)
    if not src.exists():
        Q.set_status(item, "held", f"file not found: {src}", "you", cfg)
        return item["why"]
    if dry:
        return f"would extract {src.name}"
    pay = store.payload(item["id"])
    try:
        res = X.extract(src, pay)
    except X.ExtractError as e:
        Q.set_status(item, "held", e.why, e.needs, cfg)
        return e.why
    (pay / "text.md").write_text(res.pop("text"), encoding="utf-8")
    (pay / "extract.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    item["file"].update({"pages": res.get("pages"), "words": res.get("words"), "ocr": res.get("ocr")})
    if not item["file"].get("sha256"):
        h = hashlib.sha256()
        with open(src, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        item["file"]["sha256"] = h.hexdigest()
        item["file"].setdefault("bytes", src.stat().st_size)
    item["work"]["extracted"] = True
    if res.get("no_text"):   # The owner (2026-10-01): asked in the morning email: keep it or delete it from the dropbox
        item["work"]["hold_kind"] = "notext"
        Q.set_status(item, "held", f"{res.get('words') or 0} words"
                     + (f" from {res.get('pages')} page(s)" if res.get("pages") else "") + (" after OCR" if res.get("ocr") else ""),
                     "you", cfg)
        return item["why"]
    head = (pay / "text.md").read_text(encoding="utf-8")[:40000]
    hit = markers_hit(head, cfg)
    if hit and not (item.get("manual") or {}).get("released"):
        item["work"]["hold_kind"] = "confidential"
        Q.set_status(item, "held", f"looks like firm or client material (found '{hit}'); nothing was sent to Claude", "you", cfg)
        return item["why"]
    try:   # title check (2026-10-06): a file-name title is fixed (stated in the text) or asked about before filing
        import titlecheck
        held = titlecheck.check_file(cfg, log, store, item)
        if held:
            return held
    except LimitHit:
        raise
    except Exception as e:  # noqa: BLE001
        Q.event(item, f"title check skipped: {type(e).__name__}: {e}", cfg)
    Q.set_status(item, "ready", f"{res.get('words')} words" + (f", {res.get('pages')} pages" if res.get("pages") else "")
                 + (", OCR" if res.get("ocr") else ""), cfg=cfg)
    if Q.route_of(item) in COSTLY:
        try:
            import longdoc
            item["estimate"] = longdoc.estimate(store, item, dict(res, text=(pay / "text.md").read_text(encoding="utf-8")))
        except Exception as e:
            Q.event(item, f"estimate failed: {type(e).__name__}: {e}", cfg)
        held = gate(cfg, store, item, dict(res, text=""))
        if held:
            return held
    return item["why"]


def load_doc(store, item):
    pay = store.payload_root / item["id"]
    doc = json.loads((pay / "extract.json").read_text(encoding="utf-8"))
    doc["text"] = (pay / "text.md").read_text(encoding="utf-8")
    return doc


# ---------------------------------------------------------------- helpers
def ask(cfg, log, store, item, route, reqs):
    """-> answers, or raises StopModels (usage limit) via the caller's check."""
    return llm.run(cfg, route, reqs, store.payload(item["id"]) / "answers", log, item["id"])


def text_of(ans, key):
    a = ans.get(key) or {}
    if "text" in a:
        return a["text"]
    if a.get("limit") or a.get("skipped"):
        raise LimitHit(a.get("error"))
    return None


class LimitHit(Exception):
    pass


def as_json(s):
    if not s:
        return None
    m = re.search(r"\{.*\}", s, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


# The owner (2026-10-10): a summary that only says the file couldn't be read is not filed; the item is held as
# "No readable text" and reported in the morning email instead. Checked on generated summaries only.
UNREADABLE = re.compile(
    r"not a readable text|no (?:recoverable|extractable|readable) (?:content|text)|no body text"
    r"|(?:permissions?|rights)[- ]notice[- ]only|would be invention rather than"
    r"|(?:contains?|consists? of|carries?) (?:only |entirely of )?(?:a |the )?(?:single )?(?:repeated )?"
    r"(?:permissions?|rights|copyright) (?:notice|statement)", re.I)


def unreadable(spec):
    f = spec.get("fields") or {}
    if f.get("content") != "summary" and not str(spec.get("type") or "").endswith("Summary"):
        return None
    m = UNREADABLE.search((spec.get("description") or "") + "\n" + (spec.get("body") or "")[:5000])
    return m.group(0) if m else None


def write_note(cfg, log, spec, work):
    hit = unreadable(spec)
    if hit:
        log(f"not filed: the summary of {spec.get('title')!r} says the file has no readable text ({hit!r})")
        return {"ok": False, "needs": "you", "hold_kind": "notext",
                "why": f"Claude's summary says the file has no readable text (\"{hit}\"); no note was written"}
    sj = work / f"file-{spec['id']}.json"
    sj.write_text(json.dumps([spec], ensure_ascii=False), encoding="utf-8")
    rc, out = C.run(log, C.VL / "file_to_note.py", cfg["vault_path"], sj)
    for ln in out.splitlines():
        if ln.startswith("{"):
            x = json.loads(ln)
            if x.get("ok"):
                return {"ok": True, "note": x["note"], "original": x.get("original")}
            return {"ok": False, "why": "note failed: " + str(x.get("error") or x)}
    return {"ok": False, "why": "file_to_note gave no result: " + out[-200:]}


def original_of(cfg, store, item, doc):
    """(path to copy into the vault, the name it had) - the OCR'd PDF when OCR ran. None for email bodies."""
    if doc.get("no_original"):
        return None, None
    # the OCR'd copy (kept in the payload) when there is one; otherwise the file where it is NOW
    # (a top-level dropbox file may have been moved into its subfolder since extraction)
    made = doc.get("ocr") or doc.get("made_pdf")   # made_pdf: a photo or picture-only Word file turned into a PDF
    p = doc.get("original") if made and doc.get("original") and Path(doc["original"]).exists() \
        else str(source_path(cfg, store, item))
    name = (item.get("file") or {}).get("name") or Path(p).name
    if doc.get("made_pdf") and Path(p).suffix.lower() == ".pdf":
        name = Path(name).stem + ".pdf"   # the copy in attachments/Sources is the PDF (its extension comes from this)
    return p, name


def title_of(item, doc):
    t = doc.get("title") or ""
    if vault_match.junk_title(t):         # PDF metadata like "INTRODUCTION" or "x.indd": use the file's name
        t = item.get("title") or t or "Untitled"
    return re.sub(r"\s+", " ", t).strip()[:150]


# ---------------------------------------------------------------- routes
def do_auto(cfg, log, store, item, dry, work, run_route):
    doc = load_doc(store, item)
    head = doc["text"][:6000]
    pages, wc = doc.get("pages") or 0, doc.get("words") or 0
    route, why = None, None
    if pages > 150 or wc > 60000:
        route, why = "book", f"{pages or '?'} pages, {wc} words"
    elif len(RECIPE_WORDS.findall(head)) >= 6 and wc < 4000:
        route, why = "recipe", "recipe terms"
    elif MANUAL_WORDS.search(head[:3000]):
        route, why = "manual", "manual terms on the first pages"
    if not route:
        if dry:
            return {"ok": True, "dry": True, "would": "ask Claude which route (one short call)"}
        ans = ask(cfg, log, store, item, "auto", [{"id": "classify", "instructions": CLASSIFY, "timeout": 120,
                   "material": f"File: {(item.get('file') or {}).get('name')}; pages: {pages}; words: {wc}\n\n{head}"}])
        j = as_json(text_of(ans, "classify")) or {}
        route = j.get("route") if j.get("route") in ("article", "recipe", "manual", "book", "summary", "document") else "document"
        why = "Claude: " + str(j.get("reason") or "")[:80]
    item["route"], item["route_from"] = route, "classifier"
    Q.event(item, f"auto -> {route} ({why})", cfg)
    file_into_folder(cfg, store, item, route)
    if route in COSTLY:
        held = gate(cfg, store, item, doc)
        if held:
            return {"ok": False, "needs": "confirm", "why": held}
        return {"ok": False, "later": True, "why": "waits for the night window (QueueNight, 00:30)"}
    return run_route(item)


def _article_fields(doc, item):
    f = {"author": doc.get("author") or None, "publication": doc.get("publication"), "published": doc.get("published"),
         "work_type": "article", "pages": doc.get("pages"), "word_count": doc.get("words")}
    try:
        sys.path.insert(0, str(C.UV))
        import citation
        url = doc.get("url") or doc.get("url_in_text")
        f["cite_as"] = citation.chicago({k: v for k, v in f.items() if k in ("author", "publication", "published", "work_type") and v},
                                        title_of(item, doc), url)
    except Exception:
        pass
    return f


def _full_text(cfg, log, store, item, dry, work, ntype):
    doc = load_doc(store, item)
    if dry:
        return {"ok": True, "dry": True, "would": f"{ntype} note, full text ({doc.get('words')} words)"}
    ans = ask(cfg, log, store, item, Q.route_of(item), [{"id": "describe", "instructions": DESCRIBE,
                                                          "material": doc["text"][:6000], "timeout": 120}])
    desc = (text_of(ans, "describe") or "").splitlines()
    orig, oname = original_of(cfg, store, item, doc)
    fields = _article_fields(doc, item) if ntype == "Article" else \
        {"author": doc.get("author") or None, "published": doc.get("published"), "pages": doc.get("pages"),
         "word_count": doc.get("words")}
    fields["content"] = "full"
    if doc.get("ocr"):
        fields["ocr"] = True
    return write_note(cfg, log, {
        "id": item["id"], "type": ntype, "title": title_of(item, doc), "description": desc[0][:300] if desc else "",
        "fields": fields, "resource": (doc.get("url") or doc.get("url_in_text")) if ntype == "Article" else (doc.get("resource") or doc.get("url")),
        "body": doc["text"], "doug_note": item.get("doug_note", ""), "original": orig, "original_name": oname,
        "generated_by": "process:file-intake"}, work)


def do_article(cfg, log, store, item, dry, work, run_route=None):
    return _full_text(cfg, log, store, item, dry, work, "Article")


def do_document(cfg, log, store, item, dry, work, run_route=None):
    return _full_text(cfg, log, store, item, dry, work, "Reference")


def do_recipe(cfg, log, store, item, dry, work, run_route=None):
    doc = load_doc(store, item)
    if dry:
        return {"ok": True, "dry": True, "would": "Recipe note (one short Claude call)"}
    material = (f"{C.owner(cfg)['note_label']}: {item['doug_note']}\n\n" if item.get("doug_note") else "") + doc["text"][:30000]
    ans = ask(cfg, log, store, item, "recipe", [{"id": "recipe", "instructions": RECIPE, "material": material}])
    j = as_json(text_of(ans, "recipe"))
    if not j or not j.get("ingredients"):
        return {"ok": False, "why": "couldn't read a recipe from the file"}
    body = ["## Ingredients", ""] + [f"- {x}" for x in j["ingredients"]] + ["", "## Steps", ""] + \
        [f"{i}. {x}" for i, x in enumerate(j.get("steps") or [], 1)]
    if j.get("notes"):
        body += ["", "## Notes", ""] + [f"- {x}" for x in j["notes"]]
    orig, oname = original_of(cfg, store, item, doc)
    fields = {k: j.get(k) for k in ("servings", "prep_time", "cook_time", "cuisine", "course")}
    fields["recipe_source"] = j.get("source")
    return write_note(cfg, log, {
        "id": item["id"], "type": "Recipe", "title": j.get("title") or title_of(item, doc),
        "description": j.get("description") or "", "fields": fields, "resource": doc.get("url") or doc.get("url_in_text"),
        "body": "\n".join(body), "doug_note": item.get("doug_note", ""), "original": orig, "original_name": oname,
        "generated_by": GEN_CLAUDE, "status": "draft"}, work)


def do_manual(cfg, log, store, item, dry, work, run_route=None):
    doc = load_doc(store, item)
    if dry:
        return {"ok": True, "dry": True, "would": "Manual note (one short Claude call)"}
    toc = "\n".join(f"{'  ' * (c.get('level', 1) - 1)}{c['title']} ... p. {c.get('page')}" for c in (doc.get("chapters") or [])[:80])
    pay = store.payload_root / item["id"]
    first = doc["text"][:15000]
    if (pay / "pages.json").exists():
        pages = json.loads((pay / "pages.json").read_text(encoding="utf-8"))
        first = "\n\n".join(f"[page {i}]\n{t}" for i, t in enumerate(pages[:8], 1))[:15000]
    material = (f"{C.owner(cfg)['note_label']}: {item['doug_note']}\n\n" if item.get("doug_note") else "") + \
        (f"Table of contents:\n{toc}\n\n" if toc else "") + first
    ans = ask(cfg, log, store, item, "manual", [{"id": "manual", "instructions": MANUAL, "material": material}])
    j = as_json(text_of(ans, "manual"))
    if not j:
        return {"ok": False, "why": "couldn't read the manual's details"}
    body = []
    if j.get("covers"):
        body += [j["covers"], ""]
    if j.get("key_sections"):
        body += ["## Key sections", ""] + [f"- {s.get('title')}" + (f" (p. {s['page']})" if s.get("page") else "")
                                           for s in j["key_sections"] if s.get("title")]
    orig, oname = original_of(cfg, store, item, doc)
    fields = {k: j.get(k) for k in ("brand", "model", "product", "doc_kind", "serial", "purchased")}
    fields["pages"] = doc.get("pages")
    return write_note(cfg, log, {
        "id": item["id"], "type": "Manual", "title": j.get("title") or title_of(item, doc),
        "description": j.get("description") or "", "fields": fields, "body": "\n".join(body),
        "doug_note": item.get("doug_note", ""), "original": orig, "original_name": oname,
        "generated_by": GEN_CLAUDE, "status": "draft"}, work)


def do_book(cfg, log, store, item, dry, work, run_route=None):
    import longdoc
    return longdoc.run(sys.modules[__name__], cfg, log, store, item, dry, work, "book")


def do_summary(cfg, log, store, item, dry, work, run_route=None):
    import longdoc
    return longdoc.run(sys.modules[__name__], cfg, log, store, item, dry, work, "summary")


ROUTES = {"auto": do_auto, "article": do_article, "document": do_document, "recipe": do_recipe, "manual": do_manual,
          "book": do_book, "summary": do_summary}
