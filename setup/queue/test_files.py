#!/usr/bin/env python3
"""Phase 2 tests: files in the OneDrive dropbox and in Remember emails, on a throwaway vault and dropbox,
with real extraction (PyMuPDF, OCRmyPDF, MarkItDown, Calibre) and a stubbed claude -p.
Never touches the real vault, dropbox, queue, reports or Outlook.

  python setup\\queue\\test_files.py
"""
import email.message
import json
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile
from pathlib import Path

APPDIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPDIR / "jobs"))
for name in ("organize", "update_email"):
    m = types.ModuleType(name)
    m.has_pending = lambda: False
    m.has_unapplied = lambda: False                 # saved answers awaiting a retry (2026-11-23)
    m.catch_up_send = lambda *a, **k: None
    sys.modules.setdefault(name, m)
import common as C  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="vq-files-"))
VAULT, LOCAL, DROP = TMP / "vault", TMP / "local", TMP / "Remember"
(VAULT / "Inbox").mkdir(parents=True)
(VAULT / ".obsidian").mkdir()
(TMP / "reports").mkdir()
for d in ("Articles", "Books", "Manuals", "Full text", "Other"):
    (DROP / d).mkdir(parents=True)
REAL_APP = C.APP
CFG = {
    "owner": {"name": "Pat", "id": "human:pat", "note_label": "Pat's note"}, "pc_name": "TESTPC",
    "vault_path": str(VAULT), "local_root": str(LOCAL), "timezone": "America/Chicago", "mailbox": "you@example.com",
    "lock_stale_minutes": 120,
    "intake": {"enabled": True, "batch": 20, "remember_folder": "Remember", "confidential_domains": ["lawfirm.example"],
               "signature_markers": ["Pat Example"], "verbatim_max_words": 2000},
    "queue": {"enabled": True, "files_enabled": True, "dropbox_path": str(DROP), "settle_minutes": 0},
}
for sub in ("logs", "work", "snapshots", "queue"):
    (LOCAL / sub).mkdir(parents=True, exist_ok=True)
C.APP = TMP
import os  # noqa: E402
os.chdir(TMP)   # never run with the app folder as the current folder (2026-09-28 incident)
C.load_config = lambda: json.loads(json.dumps(CFG))
os.environ.update(VAULT_OWNER_NAME="Pat", VAULT_OWNER_ID="human:pat", VAULT_OWNER_NOTE_LABEL="Pat's note")   # lib scripts

import vqueue as Q  # noqa: E402
import claude_cli  # noqa: E402
import linkjob  # noqa: E402
import intake as I  # noqa: E402
import queue_run as R  # noqa: E402

CALLS = []


def fake_ask(instructions, material, cwd, timeout=300, **kw):
    CALLS.append(instructions[:25])
    if instructions.startswith("Decide how to file"):
        return '{"route": "article", "reason": "an essay"}', {"cost_usd": 0.001}
    if instructions.startswith("The document on stdin is a recipe"):
        return json.dumps({"title": "Grandma's pound cake", "description": "A classic pound cake.", "servings": "12",
                           "ingredients": ["2 cups flour", "1 cup butter"], "steps": ["Preheat oven to 325F.", "Bake 70 minutes."]}), {}
    if instructions.startswith("The text on stdin is the start of a product manual"):
        return json.dumps({"title": "XR-200 dishwasher - owner's manual", "description": "Owner's manual for the XR-200.",
                           "brand": "Acme", "model": "XR-200", "product": "dishwasher", "doc_kind": "owner's manual",
                           "covers": "Installation, use and cleaning.", "key_sections": [{"title": "Cleaning the filter", "page": 2}]}), {}
    return "A test description.", {"cost_usd": 0.001}


claude_cli.ask = fake_ask
LINKS = []


def fake_save_url(cfg, log, url, doug_note="", captured_via=None, dry=False, **kw):   # html, title_hint
    LINKS.append(url)
    return {"ok": True, "note": "Inbox/Linked page.md", "category": "article"}


linkjob.save_url = fake_save_url

# ---------------------------------------------------------------- fake mailbox: one recipe email in Remember/Recipes
RECIPE_BODY = ("Grandma's pound cake. Ingredients: 2 cups flour, 1 cup butter, 1 cup sugar, 4 eggs, 1 tsp vanilla. "
               "Preheat the oven to 325. Cream butter and sugar, add eggs, fold in flour. Bake 70 minutes. Serves 12.")
BOX = {"REM": [], "RECIPES": [{"id": "r1", "subject": "Pound cake", "from": {"emailAddress": {"address": "aunt@example.com"}},
                               "toRecipients": [], "ccRecipients": [], "receivedDateTime": "2026-09-28T10:00:00Z",
                               "internetMessageId": "<r1@test>", "webLink": None,
                               "body": {"contentType": "text", "content": RECIPE_BODY}, "hasAttachments": False}]}
MOVES = []


class Resp:
    def __init__(self, data=None, status=200):
        self._d, self.status_code, self.ok = data, status, status < 400

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class FakeSession:
    def get(self, url, params=None, timeout=None):
        if url.endswith("/mailFolders/inbox/childFolders"):
            return Resp({"value": [{"id": "REM", "displayName": "Remember", "totalItemCount": len(BOX["REM"])}]})
        if url.endswith("/mailFolders/REM/childFolders"):
            return Resp({"value": [{"id": "RECIPES", "displayName": "Recipes", "totalItemCount": len(BOX["RECIPES"])}]})
        for f in BOX:
            if url.endswith(f"/mailFolders/{f}/messages"):
                return Resp({"value": list(BOX[f])})
        return Resp({"value": []})

    def post(self, url, json=None, timeout=None):
        mid = url.split("/messages/")[1].split("/")[0]
        MOVES.append(mid)
        for f in BOX:
            BOX[f] = [m for m in BOX[f] if m["id"] != mid]
        return Resp({}, 201)


I.graph = lambda cfg: (FakeSession(), "https://graph/users/me")

# ---------------------------------------------------------------- make test files
import pymupdf  # noqa: E402


def text_pdf(path, pages, first="", head="Running head"):
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 40), head, fontsize=8)
        words = ["hangar", "lease", "airport", "rent", "tenant", "runway", "apron", "fuel", "tower", "ramp"]
        body = (first if i == 0 else "") + " ".join(f"{words[(i * 7 + k) % 10]}{(i * 13 + k) % 97}" for k in range(60)) + \
            f" Page {i + 1} discusses hangar leases and airport rent."
        pg.insert_textbox(pymupdf.Rect(72, 72, 540, 760), body, fontsize=11)
        pg.insert_text((300, 800), str(i + 1), fontsize=8)
    doc.set_metadata({"title": path.stem.replace("-", " ").title(), "author": "Jane Writer"})
    doc.save(path)


def scanned_pdf(path, text):
    src = pymupdf.open()
    pg = src.new_page()
    pg.insert_textbox(pymupdf.Rect(72, 72, 540, 400), text, fontsize=18)
    pix = pg.get_pixmap(dpi=200)
    out = pymupdf.open()
    p2 = out.new_page(width=pg.rect.width, height=pg.rect.height)
    p2.insert_image(p2.rect, pixmap=pix)
    out.save(path)


def docx(path, paras):
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" for p in paras)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body>{body}</w:body></w:document>")


text_pdf(DROP / "Articles" / "hangar-lease-economics.pdf", 3, first="https://example.com/hangar-leases\n")
(DROP / "Articles" / "hangar-lease-economics.txt").write_text("Useful for the Smith hangar renewal.", encoding="utf-8")
scanned_pdf(DROP / "Manuals" / "dishwasher.pdf", "Owner's Manual. Model No. XR-200 dishwasher. Cleaning the filter.")
docx(DROP / "pound-cake.docx", ["Grandma's pound cake", "Ingredients: 2 cups flour, 1 cup butter, 1 cup sugar, 4 eggs, 1 tsp vanilla, pinch of salt",
                                "Preheat oven to 325. Cream the butter; bake 70 minutes in a loaf pan. Serves 12. Use 2 tbsp milk if dry."])
(DROP / "Full text" / "gate-codes.txt").write_text("Hangar 12 gate procedures.\n\nCall the FBO desk before towing.", encoding="utf-8")
(DROP / "article-link.url").write_text("[InternetShortcut]\nURL=https://example.com/a-page\n", encoding="utf-8")
(DROP / "memo.txt").write_text("PRIVILEGED & CONFIDENTIAL\nDraft advice on the lease dispute.", encoding="utf-8")
text_pdf(DROP / "long-report.pdf", 160)
text_pdf(DROP / "Books" / "policy-book.pdf", 20)
text_pdf(DROP / "Books" / "fresh-treatise.pdf", 12)
(VAULT / "Resources").mkdir()
(VAULT / "Resources" / "Policy Book.md").write_text(
    '---\ntype: "Book Summary"\ntitle: "Policy Book"\ndescription: "Summary of an earlier copy."\nauthor: ["Jane Writer"]\ngenerated: { by: "human:pat", at: "2026-01-01T00:00:00-06:00" }\n---\n\n# Policy Book\n', encoding="utf-8")
(DROP / "Articles" / "saved-page.html").write_text(
    '<html><head><title>Airport rates - Aviation Weekly</title><meta property="og:site_name" content="Aviation Weekly">'
    '<link rel="canonical" href="https://aviation.example/rates"><meta name="author" content="Pat Pilot">'
    '<meta property="article:published_time" content="2026-05-01T10:00:00Z"></head><body><h1>Airport rates</h1>'
    '<p>Hangar rents rose sharply this year at general aviation airports.</p></body></html>', encoding="utf-8")
(DROP / "essay.txt").write_text("On public choice\n\n" + "Politics without romance is the heart of public choice. " * 40, encoding="utf-8")
# a dropped .eml with a PDF attachment and a short body
att = TMP / "install.pdf"
text_pdf(att, 2, first="Installation Guide. Model Number ZZ-9 water heater.\n")
em = email.message.EmailMessage()
em["Subject"], em["From"], em["To"] = "Water heater docs", "Pat <you@example.com>", "you@example.com"
em["Date"], em["Message-ID"] = "Mon, 28 Sep 2026 09:00:00 -0500", "<eml1@test>"
em.set_content("for the vault")
em.add_attachment(att.read_bytes(), maintype="application", subtype="pdf", filename="install.pdf")
(DROP / "Other" / "water-heater.eml").write_bytes(bytes(em))
# an e-book via Calibre
ebook_ok = False
import extract as X  # noqa: E402
conv = X._tool("ebook-convert")
if conv:
    src = TMP / "novella.txt"
    src.write_text("# Chapter One\n\nIt was a dark and stormy night at the airport.\n\n# Chapter Two\n\nThe hangar door rolled open.",
                   encoding="utf-8")
    subprocess.run([conv, str(src), str(DROP / "Full text" / "novella.epub")], capture_output=True, env=X._env(), timeout=300)
    ebook_ok = (DROP / "Full text" / "novella.epub").exists()

FAILS = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def items():
    return Q.Store(C.load_config()).all()


def _key(title):
    return (title or "").lower().replace("-", " ")


def item(title):
    """The queue item for a file stem; intake now gives items readable titles ('memo' -> 'Memo')."""
    xs = items()
    return next((x for x in xs if x.get("title") == title), None) or \
        next((x for x in xs if _key(x.get("title")) == _key(title)), None)


def note_text(it):
    n = (it or {}).get("result", {}).get("notes") or []
    return (VAULT / n[0]).read_text(encoding="utf-8") if n and (VAULT / n[0]).exists() else ""


try:
    for _ in range(3):   # collect + extract + route, then finish
        R.main(["--live"])
    a = item("hangar-lease-economics")
    t = note_text(a)
    check(a and a["status"] == "done" and 'type: "Article"' in t, "PDF in Articles -> Article note")
    check("> [!note] Pat's note" in t and "Smith hangar renewal" in t, "sidecar .txt becomes the owner's note")
    check('resource: "https://example.com/hangar-leases"' in t and "cite_as:" in t, "article keeps its source URL and a citation")
    check("![[attachments/Sources/" in t and list((VAULT / "attachments" / "Sources").glob("*.pdf")), "original PDF copied and embedded")
    check("Running head" not in t, "running heads removed from the text")
    check(not (DROP / "Articles" / "hangar-lease-economics.pdf").exists() and not (DROP / "Articles" / "hangar-lease-economics.txt").exists(),
          "file and sidecar removed from the dropbox after filing")
    m = item("dishwasher")
    t = note_text(m)
    check(m and m["file"].get("ocr") and 'type: "Manual"' in t and 'model: "XR-200"' in t and "(p. 2)" in t,
          "scanned PDF in Manuals -> OCR -> Manual note")
    check('status: "draft"' in t and "claude/" in t, "model-written note is a draft, generated by claude")
    r = item("pound-cake")
    t = note_text(r)
    check(r and r["route"] == "recipe" and 'type: "Recipe"' in t and "- 2 cups flour" in t and "1. Preheat" in t,
          "Word recipe at the root -> auto -> Recipe note")
    h = item("saved-page")
    t = note_text(h)
    check(h and 'resource: "https://aviation.example/rates"' in t and 'publication: "Aviation Weekly"' in t and "Hangar rents rose" in t,
          "saved web page -> Article with its URL, site and text")
    g = item("gate-codes")
    check(g and 'type: "Reference"' in note_text(g) and "Call the FBO desk" in note_text(g), "text in Full text -> Reference, full text")
    lk = item("article-link")
    check(lk and lk["status"] == "done" and "https://example.com/a-page" in LINKS, ".url file -> link route")
    c = item("memo")
    held = json.loads((TMP / "queue" / "held.json").read_text(encoding="utf-8")) if (TMP / "queue" / "held.json").exists() else {}
    check(c and c["status"] == "held" and ("q-" + c["id"]) in held and (DROP / "memo.txt").exists(),
          "confidential marker -> held for the owner, listed for the morning email, file left in place")
    lr = item("long-report")
    check(lr and lr["status"] == "held" and lr["needs"] == "confirm" and lr["route"] == "book"
          and lr["work"].get("hold_kind") == "costly", "160-page PDF at the root -> held to confirm (book)")
    check((DROP / "Books" / "long-report.pdf").exists() and lr["source_ref"]["path"] == "Books/long-report.pdf",
          "root file moved into the subfolder for its route (Books)")
    check((DROP / "Recipes" / "pound-cake.docx").exists() is False and item("pound-cake")["source_ref"]["path"] == "Recipes/pound-cake.docx",
          "root recipe was filed via Recipes (then removed once its note existed)")
    check(item("article-link")["source_ref"]["path"] == "Other/article-link.url", "root link file moved into Other")
    check(item("essay")["source_ref"]["path"] == "Articles/essay.txt", "root essay moved into Articles")
    check(sorted(x.name for x in DROP.iterdir() if x.is_file()) == ["memo.txt"], "only the held confidential file is left at the root")
    pb = item("policy-book")
    check(pb and pb["status"] == "held" and pb["work"].get("hold_kind") == "duplicate"
          and pb["work"]["dup_of"][0]["note"] == "Resources/Policy Book.md", "book already in the vault -> held before any tokens are spent")
    ft = item("fresh-treatise")
    check(ft and ft["status"] == "ready" and ft["route"] == "book", "book in Books with no match -> waits for phase 3 (no model call)")
    es = item("essay")
    check(es and es["route"] == "article" and 'type: "Article"' in note_text(es), "unclear text at the root -> Claude classifies -> Article")
    wh = item("Water heater docs")
    kids = Q.Store(C.load_config()).children(wh) if wh else []
    check(wh and wh["route"] == "container" and kids and kids[0]["route_from"] == "classifier", ".eml with attachment -> container + child (auto-routed)")
    check(kids and 'type: "Manual"' in note_text(kids[0]) and wh["status"] == "done", "attachment -> Manual note; container done")
    check(not (DROP / "Other" / "water-heater.eml").exists(), ".eml removed from the dropbox once its attachment is filed")
    rc = next((x for x in items() if (x.get("source_ref") or {}).get("message_id") == "r1"), None)
    check(rc and 'type: "Recipe"' in note_text(rc) and "r1" in MOVES, "email in Remember/Recipes -> Recipe note, email removed")
    if ebook_ok:
        nv = item("novella")
        check(nv and nv["status"] == "done" and "hangar door" in note_text(nv), ".epub -> Calibre -> full text")
    else:
        print("SKIP e-book (ebook-convert not found)")
    # questions in the daily email, answered and applied
    for name in ("organize", "update_email"):
        sys.modules.pop(name, None)
    import organize as O
    import update_email as UE
    qq = O.queue_questions(C.load_config(), print, 1)
    kinds = {_key(q["title"]): q["kind"] for q in qq}   # titles are readable now: 'policy-book' -> 'Policy Book'
    check(kinds.get(_key("policy-book")) == "duplicate" and kinds.get(_key("long-report")) == "costly"
          and kinds.get(_key("memo")) == "confidential",
          f"daily email asks about the duplicate, the costly book and the confidential file ({kinds})")
    prop = {"items": [], "questions": [], "new_tags": [], "dups": [], "queue": qq, "day": "2026-09-29", "web_token": "x"}
    subj, html, n = UE.build_daily(C.load_config(), "2026-09-29", prop, [], None)
    check("to decide" in html and "Already in the vault?" in html and n == 3, "questions appear in the email")
    ids = {_key(q["title"]): q["id"] for q in qq}
    ans, mode = O.parse_answers(f"{ids[_key('policy-book')]}a {ids[_key('long-report')]}c {ids[_key('memo')]}b", prop)
    lines = O.apply_queue(C.load_config(), print, prop, ans)
    check(len(lines) == 3, "answers applied: " + " | ".join(lines))
    R.main(["--live"])
    R.main(["--live"])
    check(item("policy-book")["status"] == "duplicate" and not (DROP / "Books" / "policy-book.pdf").exists(),
          "a) don't add: marked a duplicate, removed from the dropbox, no summary")
    check(item("long-report")["status"] == "skipped" and not (DROP / "Books" / "long-report.pdf").exists(),
          "c) don't add: removed from the dropbox")
    mm = item("memo")
    check(mm["status"] == "done" and 'type: "Article"' in note_text(mm), "b) it's fine: the confidential-looking file is filed")
    ans2, mode2 = O.parse_answers("ok", {"questions": [], "dups": [], "queue": [{"id": 7}]})
    check(ans2 == {7: "a"} and mode2 == "ok", "'ok' takes the suggested answer for queue questions")

    # duplicate: the same PDF dropped again
    orig = (item("hangar-lease-economics") or {}).get("result", {}).get("original")
    check(bool(orig), "item records where its original went")
    if orig:   # the very same bytes as the filed original
        shutil.copy2(VAULT / orig, DROP / "Other" / "again.pdf")
    R.main(["--live"])
    R.main(["--live"])
    d = item("again")
    check(d and d["status"] == "duplicate" and not (DROP / "Other" / "again.pdf").exists(), "same file again -> duplicate, removed, no second note")
    # OKF check of every note written
    import subprocess as sp
    out = sp.run([sys.executable, str(C.VL / "okf_lint.py"), str(VAULT), "--no-orphans"], capture_output=True, text=True,
                 encoding="utf-8", errors="replace").stdout
    check(" 0 errors" in out, "all new notes pass the OKF check: " + out.strip().splitlines()[-1][:120])
    work = [c for c in CALLS if not c.startswith("You read the OPENING")]   # title checks (titlecheck.py) counted apart
    check(len(work) <= 12, f"model calls kept small ({len(work)} + {len(CALLS) - len(work)} title checks: {sorted(set(work))})")
finally:
    C.APP = REAL_APP
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
