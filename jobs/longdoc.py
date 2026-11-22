"""Book and summary routes for the intake queue (plan sections 4a and 5, phase 3).

Map-reduce through llm.py:
  1. plan   split the document into parts: PDF bookmarks (chapters), else Markdown headings,
            else runs of pages (~9,000 words or 20 pages); tiny parts are merged, very long ones split,
            and front/back matter (contents, index, bibliography, notes...) is left out.
  2. map    one request per part ("c01", "c02", ...): a summary with page cites, key ideas, short quotes.
  3. reduce one request ("overview"): bibliographic facts from the opening pages, an overview of the
            whole work and its key concepts, written from the part summaries.
  4. write  a Book Summary note (route book) or a Reference note with content: summary (route summary),
            with the original PDF embedded, through file_to_note.py.
Every answer is cached under payload\\<id>\\answers\\, so a run stopped by the Pro usage limit (or a
crash) resumes where it left off the next night and never pays twice. Token use and the
API-equivalent cost (claude -p total_cost_usd) are totalled per item for the report.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

MAX_WORDS = 16000      # a longer part is split
MIN_WORDS = 1200       # a shorter part is merged into its neighbour
RUN_WORDS = 9000       # page runs when there are no chapters
RUN_PAGES = 20
SKIP_RE = re.compile(r"^\s*(table of )?contents$|^\s*index\b|bibliograph|^\s*references\s*$|works cited|"
                     r"acknowledg|^\s*copyright|^\s*about the\b|^\s*also by|^\s*(end)?notes\s*$|"
                     r"^\s*cover\s*$|^\s*title page|^\s*dedication|^\s*praise for|^\s*appendi(x|xes|ces)\b", re.I)
# chapter openers in a PDF without bookmarks: a page whose first text block is "CHAPTER 3" / "C H A P T E R 1 0" /
# "Chapter Three", "Part two", or one of these words on its own (a running head carries a page number, so it doesn't match)
OPEN_CH = re.compile(r"^(c\s*h\s*a\s*p\s*t\s*e\s*r|p\s*a\s*r\s*t)\s*([\d\s]{1,5}|[ivxlc]{1,6}|one|two|three|four|five|six|"
                     r"seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)$", re.I)
OPEN_WORD = re.compile(r"^(foreword|preface|prologue|introduction|conclusions?|epilogue|afterword|appendi(x|xes|ces)|"
                       r"(end)?notes|index|bibliography|references|glossary|acknowledg(e)?ments|about the authors?|"
                       r"about the [\w ]{3,40}|contents|table of contents)$", re.I)
PAGENO = re.compile(r"^([\divxlc]{1,6})$", re.I)
SHY = "\u00ad"

CHAPTER = """You are writing part of a detailed summary of {kind} for «owner's» Obsidian notes.
The text on stdin is one part of "{title}"{by}: {section} ({span}).{pagehint}
Write Markdown with exactly these three headings and nothing before, between or after them except their content:
### Summary
About {n} words. Follow this part's argument in order: its main claims, the reasoning and evidence behind them, and any recommendations, figures or examples that matter. {cite} Plain, neutral prose; no preamble, no "this chapter" filler.
### Key ideas
3 to 6 bullets, each `- **idea**: one-sentence explanation{pcite}`.
### Quotes
Up to 2 short quotations (under 30 words each) copied exactly from the text, each as `- "quotation"{pcite}`. Write `- none` if nothing is worth quoting."""

OVERVIEW = """The text on stdin has two parts: OPENING PAGES of {kind} (title page, copyright page, contents), then PART SUMMARIES written earlier.
First output ONE line of JSON with the bibliographic facts stated in the opening pages (omit any key not stated; never guess):
{{"title": "...", "subtitle": "...", "author": ["full names as printed, e.g. Jane Q. Writer"], "publisher": "...", "published": "YYYY or YYYY-MM-DD", "isbn": "...", "description": "one sentence, max 30 words, saying what the work argues or covers"}}
Then these Markdown headings:
### Overview
{n} words: what the work is about, its thesis, how the argument develops across the parts, and its main conclusions or recommendations.{cite}
### Key concepts
6 to 12 bullets drawn from across the whole work, each `- **concept**: one-sentence explanation{pcite}`."""


def words(t):
    return len(re.findall(r"\w+", t or ""))


# ---------------------------------------------------------------- 1. plan
def _pages(store, item):
    p = store.payload_root / item["id"] / "pages.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _page_parts(pages, doc):
    n = len(pages)
    wc = [words(t) for t in pages]
    chs = [c for c in (doc.get("chapters") or []) if c.get("page") and 1 <= int(c["page"]) <= n]
    picked = []
    for lvl in sorted({c.get("level", 1) for c in chs})[:2]:
        picked = [c for c in chs if c.get("level", 1) <= lvl]
        if len({c["page"] for c in picked}) >= 3:
            break
    starts = {}
    for c in picked:
        starts.setdefault(int(c["page"]), c["title"].strip())
    if len(starts) < 3:   # no usable bookmarks: look for chapter-opening pages instead
        starts = _openers(pages)
    parts = []
    if len(starts) >= 2:
        sp = sorted(starts)
        if sp[0] > 1:
            parts.append({"title": "Front matter", "a": 1, "b": sp[0] - 1})
        for i, a in enumerate(sp):
            parts.append({"title": starts[a], "a": a, "b": (sp[i + 1] - 1) if i + 1 < len(sp) else n})
    else:   # no usable bookmarks: runs of pages
        a = 1
        while a <= n:
            b, w = a, wc[a - 1]
            while b < n and w + wc[b] <= RUN_WORDS and b - a + 1 < RUN_PAGES:
                b += 1
                w += wc[b - 1]
            parts.append({"title": f"Pages {a}-{b}", "a": a, "b": b, "run": True})
            a = b + 1
    for p in parts:
        p["words"] = sum(wc[p["a"] - 1:p["b"]])
    return parts, wc


def _clean(t):
    return " ".join(t.replace(SHY, "").split())


def _openers(pages):
    found = {}
    for i, t in enumerate(pages, 1):
        blocks = [_clean(b) for b in t.split("\n\n") if _clean(b)]
        while blocks and PAGENO.match(blocks[0]):
            blocks = blocks[1:]
        if not blocks or len(blocks[0]) > 60:
            continue
        b0 = blocks[0]
        if OPEN_CH.match(b0):
            word = "Part" if re.match(r"p\s*a\s*r\s*t", b0, re.I) else "Chapter"
            num = re.sub(r"(?i)^(c\s*h\s*a\s*p\s*t\s*e\s*r|p\s*a\s*r\s*t)\s*", "", b0)
            num = num.replace(" ", "") if re.fullmatch(r"[\d\s]+", num) else num.strip().lower()
            sub = ""
            for b in blocks[1:4]:   # a title wrapped over several short blocks ("Thanos's intellectual and" / "practical progenitors")
                if len(b) > 90 or len(sub) + len(b) > 140 or (sub and not re.match(r"[a-z0-9(]", b) and len(b.split()) > 4):
                    break   # a wrapped title goes on in lower case or with a few words; an epigraph is a new, longer sentence
                sub = f"{sub} {b}".strip()
                if re.search(r"[.?!:]$", b):
                    break
            found[i] = f"{word} {num}" + (f": {sub}" if sub else "")
        elif OPEN_WORD.match(b0):
            found[i] = b0[:1].upper() + b0[1:].lower()
    return found if len(found) >= 3 else {}


def _split_long(parts, wc):
    out = []
    for p in parts:
        if p["words"] <= MAX_WORDS or p["b"] == p["a"]:
            out.append(p)
            continue
        k = -(-p["words"] // (MAX_WORDS * 3 // 4))   # parts of ~12,000 words
        target, a, i = p["words"] / k, p["a"], 1
        while a <= p["b"]:
            b, w = a, wc[a - 1]
            while b < p["b"] and w + wc[b] <= target * 1.1:
                b += 1
                w += wc[b - 1]
            out.append({"title": f"{p['title']} (part {i})", "a": a, "b": b, "words": w})
            a, i = b + 1, i + 1
    return out


def _merge_short(parts):
    out = []
    for p in parts:
        if out and (p["words"] < MIN_WORDS or out[-1]["words"] < MIN_WORDS) and \
                out[-1]["words"] + p["words"] <= MAX_WORDS and not out[-1].get("front"):
            q = out[-1]
            if not q.get("run") or not p.get("run"):
                q["title"] = q["title"] if p["words"] < 200 else f"{q['title']}; {p['title']}"
            q["b"], q["words"] = p["b"], q["words"] + p["words"]
            if q.get("run"):
                q["title"] = f"Pages {q['a']}-{q['b']}"
        else:
            out.append(dict(p))
    return out


def _text_parts(text):
    heads = list(re.finditer(r"(?m)^(#{1,2})\s+(.+)$", text))
    for lvl in (1, 2):
        hs = [h for h in heads if len(h.group(1)) <= lvl]
        if len(hs) >= 3:
            break
    else:
        hs = []
    parts = []
    if hs:
        if hs[0].start() > 0:
            parts.append({"title": "Front matter", "text": text[:hs[0].start()]})
        for i, h in enumerate(hs):
            parts.append({"title": h.group(2).strip(), "text": text[h.start():hs[i + 1].start() if i + 1 < len(hs) else len(text)]})
    else:
        paras, cur, n = text.split("\n\n"), [], 1
        for para in paras:
            cur.append(para)
            if words("\n\n".join(cur)) >= RUN_WORDS:
                parts.append({"title": f"Part {n}", "text": "\n\n".join(cur), "run": True})
                cur, n = [], n + 1
        if cur:
            parts.append({"title": f"Part {n}", "text": "\n\n".join(cur), "run": True})
    out = []
    for p in parts:   # long sections split at paragraph breaks
        p["words"] = words(p["text"])
        if p["words"] <= MAX_WORDS:
            out.append(p)
            continue
        cur, i = [], 1
        for para in p["text"].split("\n\n"):
            cur.append(para)
            if words("\n\n".join(cur)) >= MAX_WORDS * 3 // 4:
                t = "\n\n".join(cur)
                out.append({"title": f"{p['title']} (part {i})", "text": t, "words": words(t)})
                cur, i = [], i + 1
        if cur:
            t = "\n\n".join(cur)
            out.append({"title": f"{p['title']} (part {i})", "text": t, "words": words(t)})
    merged = []
    for p in out:
        if merged and (p["words"] < MIN_WORDS or merged[-1]["words"] < MIN_WORDS) and \
                merged[-1]["words"] + p["words"] <= MAX_WORDS:
            merged[-1]["text"] += "\n\n" + p["text"]
            merged[-1]["words"] += p["words"]
            if p["words"] >= 200:
                merged[-1]["title"] += "; " + p["title"]
        else:
            merged.append(p)
    return merged


def plan(store, item, doc):
    """-> (parts, skipped, has_pages). Each part: {id, title, a, b, words} (pages) or {id, title, text, words}."""
    pages = _pages(store, item)
    skipped = []
    if pages:
        parts, wc = _page_parts(pages, doc)
        keep = []
        for p in parts:
            if SKIP_RE.search(p["title"]) or (p["title"] == "Front matter" and p["words"] < 3000):
                skipped.append(p)
            else:
                keep.append(p)
        parts = _merge_short(_split_long(keep, wc))
    else:
        parts = []
        for p in _text_parts(doc["text"]):
            if SKIP_RE.search(p["title"]) or (p["title"] == "Front matter" and p["words"] < 1500):
                skipped.append(p)
            else:
                parts.append(p)
    parts = [p for p in parts if p["words"] >= 50]
    for i, p in enumerate(parts, 1):
        p["id"] = f"c{i:02d}"
    return parts, skipped, bool(pages)


def estimate(store, item, doc):
    """Rough size of the job, stored on the item at extraction (for the daily email and the pilot)."""
    parts, _, _ = plan(store, item, doc)
    w = sum(p["words"] for p in parts)
    # overhead per claude -p call: ~40,000 tokens in the pilot; ~500 with claude_cli.LEAN (2026-09-28)
    return {"model_calls": len(parts) + 1, "input_tokens": int(w * 1.4) + 1000 * (len(parts) + 1),
            "output_tokens": sum(_target(p["words"]) for p in parts) * 2 + 2500, "backend": "claude-cli"}


def _target(w):
    return max(150, min(600, w // 25))


def span(p, has_pages):
    if not has_pages:
        return "section"
    return f"p. {p['a']}" if p["a"] == p["b"] else f"pp. {p['a']}-{p['b']}"


# ---------------------------------------------------------------- 2-3. requests
def material(pages, p, has_pages):
    if not has_pages:
        return p["text"]
    return "\n\n".join(f"[page {i}]\n{pages[i - 1]}" for i in range(p["a"], p["b"] + 1))


def chapter_request(kind, title, by, p, has_pages, pages):
    ph = (" Markers like [page 12] give the page numbers; cite those numbers." if has_pages
          else " The text has no page numbers, so cite nothing.")
    instr = CHAPTER.format(kind=kind, title=title, by=by, section=p["title"], span=span(p, has_pages), pagehint=ph,
                           n=_target(p["words"]),
                           cite="Cite pages after the sentences they support, like (p. 12) or (pp. 12-14)." if has_pages else "",
                           pcite=" (p. N)" if has_pages else "")
    return {"id": p["id"], "instructions": instr, "material": material(pages, p, has_pages), "timeout": 900}


BIB_RE = re.compile(r"isbn|copyright|\u00a9|library of congress|all rights reserved|published by|first published", re.I)


def opening_pages(pages, limit=14000):
    """The first 3 pages, plus the title and copyright pages wherever they are in the first 30 (praise pages
    often come first)."""
    pick = set(range(1, min(3, len(pages)) + 1))
    for i in range(1, min(30, len(pages)) + 1):
        if BIB_RE.search(pages[i - 1]):
            pick.update({i - 1, i} if i > 1 else {i})
    out, size = [], 0
    for i in sorted(pick, key=lambda i: (0 if BIB_RE.search(pages[i - 1]) else 1, i)):
        chunk = f"[page {i}]\n{pages[i - 1]}"
        if size + len(chunk) > limit:
            continue
        out.append((i, chunk))
        size += len(chunk)
    return "\n\n".join(c for _, c in sorted(out))


def overview_request(kind, doc, pages, parts, answers, has_pages, n):
    opening = opening_pages(pages) if has_pages else doc["text"][:14000]
    summ = "\n\n".join(f"## {p['title']} ({span(p, has_pages)})\n\n{answers[p['id']]['text']}" for p in parts)
    instr = OVERVIEW.format(kind=kind, n=n, cite=" Cite pages where useful, like (p. 12)." if has_pages else "",
                            pcite=" (pp. N, M)" if has_pages else "")
    return {"id": "overview", "instructions": instr, "material": f"OPENING PAGES\n\n{opening}\n\nPART SUMMARIES\n\n{summ}",
            "timeout": 900}


def sections(text):
    """'### Summary ... ### Key ideas ...' -> {'summary': ..., 'key ideas': ...}"""
    out, cur = {}, None
    for ln in (text or "").splitlines():
        m = re.match(r"^#{2,4}\s+(.+?)\s*$", ln)
        if m:
            cur = m.group(1).strip().lower()
            out[cur] = []
        elif cur:
            out[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def first_json(text):
    head = (text or "").split("###")[0]
    m = re.search(r"\{.*\}", head, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


def usage_of(answers):
    u = {"calls": 0, "input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
         "output_tokens": 0, "cost_usd": 0.0, "ms": 0, "models": []}
    for a in answers.values():
        if "text" not in a:
            continue
        u["calls"] += 1
        u["cost_usd"] += float(a.get("cost_usd") or 0)
        u["ms"] += int(a.get("ms") or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
            u[k] += int((a.get("usage") or {}).get(k) or 0)
        for m in a.get("models") or []:
            if m not in u["models"]:
                u["models"].append(m)
    u["cost_usd"] = round(u["cost_usd"], 4)
    return u


def usage_line(u):
    tin = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
    return (f"{u['calls']} calls, {tin:,} tokens in ({u['cache_read_input_tokens']:,} cached), "
            f"{u['output_tokens']:,} out, ${u['cost_usd']:.2f} API-equivalent, {u['ms'] // 60000} min"
            + (f", {', '.join(u['models'])}" if u["models"] else ""))


# ---------------------------------------------------------------- 4. note
def _book_cite(meta, title):
    """Chicago bibliography entry: Last, First, and First Last. *Title: Subtitle*. Publisher, Year."""
    auth = meta.get("author") or []
    a = ""
    if auth:
        first = auth[0].split()
        a = (f"{first[-1]}, {' '.join(first[:-1])}" if len(first) > 1 else auth[0])
        if len(auth) == 2:
            a += f", and {auth[1]}"
        elif len(auth) > 2:
            a += ", et al"
        a = a.rstrip(".") + ". "
    t = title + (f": {meta['subtitle']}" if meta.get("subtitle") else "")
    pub = ", ".join(x for x in (meta.get("publisher"), str(meta.get("published") or "")[:4]) if x)
    return f"{a}*{t}*." + (f" {pub}." if pub else "")


def run(RT, cfg, log, store, item, dry, work, route):
    doc = RT.load_doc(store, item)
    kind = "a book" if route == "book" else "a long document"
    parts, skipped, has_pages = plan(store, item, doc)
    if not parts:
        return {"ok": False, "why": "no text to summarize"}
    if dry:
        return {"ok": True, "dry": True, "would": f"{route}: {len(parts)} parts + overview "
                f"({sum(p['words'] for p in parts):,} words)"}
    pages = _pages(store, item) or []
    title = RT.title_of(item, doc)
    by = f" by {', '.join(doc['author'])}" if doc.get("author") else ""
    item["work"]["step"] = "parts"
    item["work"]["parts"] = [{k: p[k] for k in ("id", "title", "words") if k in p} for p in parts]
    store.save(item)
    reqs = [chapter_request(kind, title, by, p, has_pages, pages) for p in parts]
    ans = RT.ask(cfg, log, store, item, route, reqs)
    for p in parts:
        RT.text_of(ans, p["id"])            # raises LimitHit when the usage limit stopped the run
        if "text" not in ans[p["id"]]:
            return {"ok": False, "why": f"part {p['id']} failed: {ans[p['id']].get('error')}"}
    item["work"]["step"] = "overview"
    store.save(item)
    n = "400 to 700" if route == "book" else "250 to 450"
    ans.update(RT.ask(cfg, log, store, item, route, [overview_request(kind, doc, pages, parts, ans, has_pages, n)]))
    ov = RT.text_of(ans, "overview")
    if not ov:
        return {"ok": False, "why": f"overview failed: {ans['overview'].get('error')}"}
    meta = first_json(ov)
    osec = sections(ov)
    item["work"]["step"] = "write"
    usage = usage_of(ans)
    item["work"]["usage"] = usage

    # body
    body = ["## Overview", "", osec.get("overview") or ov, ""]
    body += ["## Chapter summaries" if route == "book" else "## Section summaries", ""]
    if has_pages:
        body += ["*Page numbers are pages of the PDF under Original.*", ""]
    quotes = []
    for p in parts:
        s = sections(ans[p["id"]]["text"])
        head = f"### {p['title']}" + (f" ({span(p, True)})" if has_pages else "")
        body += [head, "", s.get("summary") or ans[p["id"]]["text"], ""]
        if s.get("key ideas"):
            body += ["**Key ideas**", "", s["key ideas"], ""]
        for q in (s.get("quotes") or "").splitlines():
            if q.strip().startswith("-") and "none" not in q.lower()[:8]:
                quotes.append(f"{q.strip()} ({p['title']})" if not has_pages else q.strip())
    if osec.get("key concepts"):
        body += ["## Key concepts", "", osec["key concepts"], ""]
    if quotes:
        body += ["## Short quotes", ""] + quotes[:12] + [""]
    if skipped:
        body += ["## Not summarized", "", "; ".join(f"{p['title']}" + (f" ({span(p, True)})" if has_pages else "")
                                                   for p in skipped) + ".", ""]

    if not has_pages and not meta.get("title") and parts and title == parts[0]["title"].split(";")[0].strip():
        title = item.get("title") or title      # a text file's first heading is a section, not the title
    t = (meta.get("title") or title).strip()
    pub = meta.get("published") or (doc.get("published") if route != "book" else None)
    try:
        sys.path.insert(0, str(C.UV))
        import citation
        pub = citation.iso_date(pub) or pub     # "2019" -> "2019-01-01", as url-to-vault does
    except Exception:
        pass
    authors = meta.get("author") or doc.get("author") or []
    if isinstance(authors, str):
        authors = [authors]
    orig, oname = RT.original_of(cfg, store, item, doc)
    if route == "book":
        isbn = re.sub(r"[^0-9Xx]", "", str(meta.get("isbn") or "")) or None
        fields = {"subtitle": meta.get("subtitle"), "author": authors or None, "publisher": meta.get("publisher"),
                  "published": pub, "isbn": isbn, "pages": doc.get("pages"),
                  "word_count": doc.get("words"), "work_type": "book", "content": "summary",
                  "cite_as": _book_cite(dict(meta, author=authors), t)}
        ntype, resource = "Book Summary", (f"urn:isbn:{isbn}" if isbn else None)
    else:
        fields = {"author": authors or None, "published": pub,
                  "pages": doc.get("pages"), "word_count": doc.get("words"), "content": "summary"}
        ntype, resource = "Reference", doc.get("url") or doc.get("url_in_text")
    if doc.get("ocr"):
        fields["ocr"] = True
    res = RT.write_note(cfg, log, {
        "id": item["id"], "type": ntype, "title": t, "description": (meta.get("description") or "")[:300],
        "fields": fields, "resource": resource, "body": "\n".join(body), "doug_note": item.get("doug_note", ""),
        "original": orig, "original_name": oname, "generated_by": RT.GEN_CLAUDE, "status": "draft"}, work)
    res["usage"] = usage
    if res.get("ok"):
        res["note_detail"] = usage_line(usage)
    return res
