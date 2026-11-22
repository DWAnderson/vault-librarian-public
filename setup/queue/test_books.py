#!/usr/bin/env python3
"""Phase 3 tests: books and long summaries (map-reduce by chapter), on a throwaway vault, dropbox and
local folder, with real extraction and a stubbed claude -p. Never touches the real vault, dropbox,
queue, reports or Outlook. Runs chdir'd into its temp folder (2026-09-28 incident).

  python setup\\queue\\test_books.py
"""
import json
import os
import shutil
import subprocess as sp
import sys
import tempfile
import types
from pathlib import Path

APPDIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPDIR / "jobs"))
for name in ("organize", "update_email"):
    m = types.ModuleType(name)
    m.has_pending = lambda: False
    sys.modules.setdefault(name, m)
import common as C  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="vq-books-"))
VAULT, LOCAL, DROP = TMP / "vault", TMP / "local", TMP / "Remember"
(VAULT / "Inbox").mkdir(parents=True)
(VAULT / ".obsidian").mkdir()
(TMP / "reports").mkdir()
for d in ("Books", "Summarize", "Other"):
    (DROP / d).mkdir(parents=True)
REAL_APP = C.APP
CFG = {
    "owner": {"name": "Pat", "id": "human:pat", "note_label": "Pat's note"}, "pc_name": "TESTPC",
    "vault_path": str(VAULT), "local_root": str(LOCAL), "timezone": "America/Chicago", "mailbox": "you@example.com",
    "lock_stale_minutes": 120,
    "intake": {"enabled": True, "batch": 20, "remember_folder": "Remember", "confidential_domains": ["lawfirm.example"],
               "signature_markers": ["Pat Example"], "verbatim_max_words": 2000},
    "queue": {"enabled": True, "files_enabled": True, "dropbox_path": str(DROP.resolve()), "settle_minutes": 0,
              "books_per_night": 1, "claude_calls_per_day": 60},
}
for sub in ("logs", "work", "snapshots", "queue"):
    (LOCAL / sub).mkdir(parents=True, exist_ok=True)
C.APP = TMP
os.chdir(TMP)
C.load_config = lambda: json.loads(json.dumps(CFG))
os.environ.update(VAULT_OWNER_NAME="Pat", VAULT_OWNER_ID="human:pat", VAULT_OWNER_NOTE_LABEL="Pat's note")   # lib scripts

import vqueue as Q  # noqa: E402
import claude_cli  # noqa: E402
import intake as I  # noqa: E402
import queue_run as R  # noqa: E402
import longdoc  # noqa: E402

CALLS, STATE = [], {"limit_on_call": None, "n": 0}
USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 5000, "output_tokens": 400}


def fake_ask(instructions, material, cwd, timeout=300, **kw):
    STATE["n"] += 1
    kind = "chapter" if instructions.startswith("You are writing part") else \
        "overview" if instructions.startswith("The text on stdin has two parts") else "other"
    CALLS.append(kind)
    if STATE["limit_on_call"] and STATE["n"] == STATE["limit_on_call"]:
        return None, {"error": "Claude AI usage limit reached|1759300000"}
    info = {"cost_usd": 0.05, "ms": 30000, "usage": dict(USAGE), "models": ["claude-test"]}
    if kind == "chapter":
        pg = "(p. 5)" if "[page" in material else ""
        return (f"### Summary\nThe part argues that hangar rents track airport demand {pg}.\n\n"
                f"### Key ideas\n- **Demand pricing**: rent follows demand {pg}\n\n"
                f"### Quotes\n- \"rent follows the runway\" {pg}"), info
    if kind == "overview":
        meta = {"description": "A study of airport leasing policy."}
        if "ISBN 978-0-306-40615-7" in material:
            meta.update({"title": "Airport Leasing Policy", "subtitle": "Rents and Runways", "author": ["Jane Writer"],
                         "publisher": "Test Press", "published": "2019", "isbn": "978-0-306-40615-7"})
        return (json.dumps(meta) + "\n### Overview\nThe work surveys leasing (p. 3).\n\n"
                "### Key concepts\n- **Ground lease**: land leased for hangars (pp. 4, 20)"), info
    return "A test description.", info


claude_cli.ask = fake_ask
I.graph = lambda cfg: (None, None)   # no mailbox in these tests

import pymupdf  # noqa: E402
VOCAB = ["hangar", "lease", "airport", "rent", "tenant", "runway", "apron", "fuel", "tower", "ramp", "policy", "grant"]


def book_pdf(path, pages, toc=None, title=None, first=""):
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 40), "Running head of the book", fontsize=8)
        body = (first if i == 0 else "") + " ".join(f"{VOCAB[(i * 7 + k) % 12]}{(i * 13 + k) % 97}" for k in range(250))
        pg.insert_textbox(pymupdf.Rect(60, 60, 550, 780), body, fontsize=7)
        pg.insert_text((300, 810), str(i + 1), fontsize=8)
    if toc:
        doc.set_toc(toc)
    doc.set_metadata({"title": title or path.stem, "author": "Jane Writer"})
    doc.save(path)


book_pdf(DROP / "Books" / "airport-leasing.pdf", 40, title="Airport Leasing Policy",
         first="Airport Leasing Policy. Test Press 2019. ISBN 978-0-306-40615-7. ",
         toc=[[1, "Contents", 2], [1, "Chapter 1. Ground leases", 3], [1, "Chapter 2. Rents", 10],
              [1, "Chapter 3. Runways", 18], [2, "Section 3.1", 20], [1, "Chapter 4. Conclusions", 34], [1, "Index", 39]])
book_pdf(DROP / "Books" / "runway-funding.pdf", 30, title="Runway Funding Handbook")
(DROP / "Summarize" / "tower-study.md").write_text(
    "\n\n".join(f"# Finding {n}\n\n" + " ".join(f"{VOCAB[(n + k) % 12]}{k % 50}" for k in range(1500)) for n in (1, 2, 3)),
    encoding="utf-8")

FAILS = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def item(title):
    return next((x for x in Q.Store(C.load_config()).all() if x.get("title") == title), None)


def note_text(it):
    n = (it or {}).get("result", {}).get("notes") or []
    return (VAULT / n[0]).read_text(encoding="utf-8") if n and (VAULT / n[0]).exists() else ""


try:
    # 1. the daytime (cheap) runs collect and extract, but never spend on a book
    R.main(["--live"])
    R.main(["--live"])
    al, rf, ts = item("airport-leasing"), item("runway-funding"), item("tower-study")
    check(al and al["status"] == "ready" and al["route"] == "book" and rf["status"] == "ready" and ts["route"] == "summary",
          "books and the summary are extracted and ready")
    check("chapter" not in CALLS and "overview" not in CALLS, "the daytime run spends nothing on books")
    check(al["estimate"]["model_calls"] == 5, f"estimate: 4 chapters + overview ({al['estimate']})")
    parts, skipped, hp = longdoc.plan(Q.Store(C.load_config()), al, json.load(open(LOCAL / "queue" / "payload" / al["id"] / "extract.json")))
    check([p["title"] for p in parts] == ["Chapter 1. Ground leases", "Chapter 2. Rents", "Chapter 3. Runways", "Chapter 4. Conclusions"]
          and parts[2]["a"] == 18 and parts[2]["b"] == 33, "bookmarks -> chapters with page ranges; level-2 bookmark stays inside its chapter")
    check(sorted(p["title"] for p in skipped) == ["Contents", "Front matter", "Index"], "front matter, contents and index are left out")
    rparts, _, _ = longdoc.plan(Q.Store(C.load_config()), rf, json.load(open(LOCAL / "queue" / "payload" / rf["id"] / "extract.json")))
    check(len(rparts) == 2 and rparts[0]["title"] == "Pages 1-20", "no bookmarks -> runs of up to 20 pages")

    # 2. night run: books_per_night = 1, the summary (priority 50) goes first, the books wait
    R.main(["--live", "--routes", "costly", "--force"])
    ts = item("tower-study")
    t = note_text(ts)
    check(ts["status"] == "done" and 'type: "Reference"' in t and 'content: "summary"' in t and "## Section summaries" in t
          and "### Finding 2" in t and "(p." not in t.split("## Section summaries")[1], "summary route -> Reference, content: summary, no page cites for text")
    check(item("airport-leasing")["status"] == "ready" and item("runway-funding")["status"] == "ready", "books_per_night: the books wait")
    check(ts["work"]["usage"]["calls"] == 4 and ts["work"]["usage"]["cost_usd"] == 0.2, f"usage totalled on the item ({ts['work']['usage']})")
    rep = (TMP / "reports").glob("* queue.md")
    check(any("API-equivalent" in p.read_text(encoding="utf-8") for p in rep), "the report shows tokens and cost")

    # 3. usage limit in the middle of a book: stops, keeps what it has, nothing marked failed
    CFG["queue"]["books_per_night"] = 3
    STATE["n"], STATE["limit_on_call"] = 0, 3
    before = len(CALLS)
    R.main(["--live", "--routes", "costly", "--force"])
    stopped = [x for x in (item("airport-leasing"), item("runway-funding")) if x["status"] == "ready" and "usage limit" in (x["why"] or "")]
    check(len(stopped) == 1 and stopped[0]["attempts"] == 0, "usage limit -> item back to ready, not failed")
    cached = list((LOCAL / "queue" / "payload" / stopped[0]["id"] / "answers").glob("c*.json"))
    check(len(cached) == 2 and len(CALLS) - before == 3, "the two answers before the limit are cached; no more calls that run")
    other = item("runway-funding") if stopped[0]["title"] == "airport-leasing" else item("airport-leasing")
    check(other["status"] == "ready" and "usage limit" not in (other["why"] or ""), "the other book waits untouched")

    # 4. next night: resumes from the cache, then does the next book
    STATE["limit_on_call"] = None
    before = len(CALLS)
    R.main(["--live", "--routes", "costly", "--force"])
    al, rf = item("airport-leasing"), item("runway-funding")
    check(al["status"] == "done" and rf["status"] == "done", "both books done")
    check(len(CALLS) - before == 6, f"resumed without paying twice ({len(CALLS) - before} calls; 9 needed in all, 3 done before the limit)")
    t = note_text(al)
    check('type: "Book Summary"' in t and 'title: "Airport Leasing Policy"' in t and 'isbn: "9780306406157"' in t
          and 'resource: "urn:isbn:9780306406157"' in t and 'work_type: "book"' in t and 'status: "draft"' in t
          and "cite_as:" in t and "Test Press" in t, "Book Summary frontmatter: title, isbn, resource, cite_as, draft")
    check("## Overview" in t and "### Chapter 3. Runways (pp. 18-33)" in t and "## Key concepts" in t and "## Short quotes" in t
          and "**Key ideas**" in t and "## Not summarized" in t and "Index (pp. 39-40)" in t and "![[attachments/Sources/" in t,
          "Book Summary body: overview, chapters with pages, key concepts, quotes, what was left out, the PDF")
    check("Running head" not in t, "no running heads leak into the note")

    # 5. the daily model budget stops a book before it starts spending
    book_pdf(DROP / "Books" / "tower-staffing.pdf", 12, title="Tower Staffing Study")
    R.main(["--live"])
    R.main(["--live"])
    CFG["queue"]["claude_calls_per_day"] = 1
    CFG["queue"]["books_per_night"] = 10
    before = len(CALLS)
    R.main(["--live", "--routes", "costly", "--force"])
    tsb = item("tower-staffing")
    check(tsb["status"] == "ready" and len(CALLS) == before, "daily model budget used -> the book waits, no calls")
    CFG["queue"]["claude_calls_per_day"] = 60

    # 6. a book cut off mid-run (status left 'working') is picked up again
    tsb["status"] = "working"
    Q.Store(C.load_config()).save(tsb)
    R.main(["--live", "--routes", "costly", "--force"])
    check(item("tower-staffing")["status"] == "done", "a book left 'working' by a crash is resumed")

    # 7. PDFs without bookmarks: chapter-opening pages; the title and copyright pages found for the overview
    body = "word " * 400
    pg = ["PRAISE FOR || nice"] + [f"Praise {i}\n\n{body}" for i in range(6)] + \
         ["SUPER\n\nThe Story of\n\nSomething Big\n\nJane Q. Writer", "Copyright \u00a9 2022. ISBN: 978-1-952223-58-7",
          "contents\n\nChapter 1 . . . 3", "introduction\n\n" + body, body,
          "C H A P T E R 1\n\nAre we in the midst of pro\u00adgress, or are we facing\n\nthe apocalypse?\n\n" + body * 2]
    pg += [body * 4] * 3 + ["C H A P T E R 2\n\nThanos's intellectual and\n\npractical progenitors\n\n" + body * 2] + [body * 4] * 3 + \
          ["notes\n\n" + body, "index\n\n" + body]
    op = longdoc._openers(pg)
    check(op.get(13) == "Chapter 1: Are we in the midst of progress, or are we facing the apocalypse?"
          and op.get(17) == "Chapter 2: Thanos's intellectual and practical progenitors" and op.get(10) == "Contents"
          and op.get(11) == "Introduction" and op.get(21) == "Notes", f"chapter openers found without bookmarks ({op})")
    o = longdoc.opening_pages(pg)
    check("[page 8]" in o and "[page 9]" in o and "[page 1]" in o, "overview gets the title and copyright pages even after praise pages")
    check(longdoc._book_cite({"author": ["Marian L. Tupy", "Gale L. Pooley"], "subtitle": "The Story", "publisher": "Cato Institute",
                              "published": "2022-01-01"}, "Superabundance")
          == "Tupy, Marian L., and Gale L. Pooley. *Superabundance: The Story*. Cato Institute, 2022.", "Chicago book citation")

    out = sp.run([sys.executable, str(C.VL / "okf_lint.py"), str(VAULT), "--no-orphans"], capture_output=True, text=True,
                 encoding="utf-8", errors="replace").stdout
    if " 0 errors" not in out:
        print(out[-3000:])
    check(" 0 errors" in out, "all new notes pass the OKF check: " + (out.strip().splitlines() or ["?"])[-1][:120])
finally:
    os.chdir(APPDIR.parent)
    C.APP = REAL_APP
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
