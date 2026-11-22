"""Pull the text out of a file, locally and at no cost (plan section 6).

  extract(path, work_dir) -> dict with
     text        Markdown/plain text of the whole document
     words, pages, chapters [{title, page, level}], title, author [..], published (ISO), url,
     publication, ocr (bool), original (the file to keep in the vault: the OCR'd PDF when OCR ran),
     format
  Raises ExtractError(why, needs) for files that need the owner (DRM, password, unsupported, OCR failure).
Per-page text of PDFs goes to <work_dir>/pages.json (used by the summary and book routes).
Tools: PyMuPDF, OCRmyPDF + Tesseract (no Ghostscript: --output-type pdf), MarkItDown, Word via COM
for .doc, Calibre's ebook-convert / ebook-meta for e-books.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
TEXT_EXT = {".txt", ".md", ".markdown", ".text"}
HTML_EXT = {".html", ".htm", ".xhtml"}
EBOOK_EXT = {".mobi", ".epub", ".azw", ".azw3", ".fb2", ".rtf", ".lit"}
EMAIL_EXT = {".msg", ".eml"}
MHTML_EXT = {".mhtml", ".mht"}                     # saved web pages ("Save as: single file"), added 2026-10-01
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}   # photos/scans -> PDF + OCR (2026-10-01)
SUPPORTED = {".pdf", ".docx", ".doc"} | TEXT_EXT | HTML_EXT | EBOOK_EXT | MHTML_EXT | IMAGE_EXT
# The owner (2026-10-01): a file whose OCR still gives no usable text is filed as the original only (a note that
# embeds it, saying so) rather than held; the daily email links to it.
MIN_WORDS, MIN_WORDS_PER_PAGE = 10, 5
DOCX_PHOTO_WORDS = 150                             # a .docx with pictures and fewer words than this is "mostly photos"
TOOL_DIRS = [r"C:\Program Files\Tesseract-OCR", r"C:\Program Files\Calibre2", r"C:\Program Files (x86)\Calibre2"]
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ExtractError(Exception):
    def __init__(self, why, needs="you"):
        super().__init__(why)
        self.why, self.needs = why, needs


def words(t):
    return len(re.findall(r"\w+", t or ""))


def _env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env["PATH"] = os.pathsep.join([d for d in TOOL_DIRS if os.path.isdir(d)] + [env.get("PATH", "")])
    return env


def _tool(name):
    for d in [""] + TOOL_DIRS:
        p = shutil.which(name, path=d or None) if d else shutil.which(name)
        if p:
            return p
    return None


def read_text(p):
    b = Path(p).read_bytes()
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not b[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    try:
        import chardet
        enc = (chardet.detect(b[:200000]) or {}).get("encoding") or "cp1252"
    except ImportError:
        enc = "cp1252"
    return b.decode(enc, errors="replace")


def _iso(v):
    if not v:
        return None
    s = str(v)
    m = re.match(r"D:(\d{4})(\d{2})?(\d{2})?", s)
    if m:
        return "-".join(x for x in m.groups() if x)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return "-".join(m.groups()) if m else None


def _authors(v):
    if not v:
        return []
    parts = re.split(r"\s*(?:;|&| and )\s*", str(v))
    return [p.strip() for p in parts if p.strip() and not re.search(r"microsoft|adobe|user|owner|admin", p, re.I)]


def _good_title(t, path):
    t = (t or "").strip()
    if not t or re.match(r"(?i)^(microsoft word - |untitled|document\d*$)", t) or t.lower().endswith((".doc", ".docx", ".pdf")):
        return None
    return t


# ---------------------------------------------------------------- PDF
def _page_blocks(page):
    out = []
    for b in page.get_text("blocks"):
        if b[6] != 0:   # image block
            continue
        t = re.sub(r"-\n(?=[a-z])", "", b[4]).replace("\u00ad", "").replace("\u200b", "")   # soft hyphens, zero-width spaces
        t = " ".join(t.split())
        if t:
            out.append(t)
    return out


def _unrepeated_chars(raw):
    """Characters of a PDF's text layer outside lines that repeat (digits ignored) on over 40% of pages
    (on both pages of a two-page file): watermarks, permission notices, running heads."""
    from collections import Counter
    n = len(raw)
    key = lambda s: re.sub(r"\d+", "#", re.sub(r"\s+", " ", s.strip().lower()))
    per = [{key(ln) for ln in pg.splitlines() if ln.strip()} for pg in raw]
    cnt = Counter(k for s in per for k in s)
    rep = {k for k, c in cnt.items() if n >= 2 and c > max(1, 0.4 * n)}
    return sum(len(ln.strip()) for pg in raw for ln in pg.splitlines() if ln.strip() and key(ln) not in rep)


def _pdf(p, work, ocr=True):
    docs = []
    try:
        return _pdf_inner(p, work, ocr, docs)
    finally:
        for d in docs:
            try:
                d.close()
            except Exception:
                pass


def _pdf_inner(p, work, ocr, docs):
    import pymupdf
    try:
        doc = pymupdf.open(p)
    except Exception as e:
        raise ExtractError(f"the PDF couldn't be opened (damaged or not really a PDF): {type(e).__name__}")
    docs.append(doc)
    if doc.needs_pass:
        raise ExtractError("the PDF is password-protected")
    raw = [pg.get_text("text") for pg in doc]
    used_ocr, original = False, Path(p)
    # The owner (2026-10-10): judge the text layer after removing lines repeated on most pages. Course-packet scans
    # (Hayek, Friedman, Bauer) carried only a ~140-character permissions notice on every page, which passed the
    # 80-characters-a-page test, so OCR never ran and Claude "summarized" the notice.
    if doc.page_count and _unrepeated_chars(raw) / doc.page_count < 80:
        if not ocr:
            raise ExtractError("scanned PDF (no text layer) and OCR is off")
        out = Path(work) / "ocr.pdf"
        # --force-ocr (2026-10-01, was --skip-text): a copier's thin text layer made --skip-text skip every
        # page, so 4 scanned pages came out with no text. The text layer here is under 80 characters a page anyway.
        r = subprocess.run([sys.executable, "-m", "ocrmypdf", "--force-ocr", "--output-type", "pdf", "-q",
                            str(p), str(out)], capture_output=True, text=True, env=_env(), timeout=3600,
                           creationflags=NOWIN)
        if r.returncode not in (0, 6) or not out.exists():
            raise ExtractError(f"OCR failed (exit {r.returncode}): {(r.stderr or '').strip()[-200:]}")
        doc, used_ocr, original = pymupdf.open(out), True, out
        docs.append(doc)
    pages = [_page_blocks(pg) for pg in doc]
    # running heads / footers: blocks that repeat (digits ignored) on over 40% of pages
    from collections import Counter
    key = lambda s: re.sub(r"\d+", "#", s.lower())[:80]
    # 300 (was 120, 2026-10-10): a permissions notice repeated on every page runs ~140 characters
    cnt = Counter(k for blocks in pages for k in {key(b) for b in blocks if len(b) <= 300})
    rep = {k for k, n in cnt.items() if len(pages) >= 3 and n > 0.4 * len(pages)}
    clean = [[b for b in blocks if key(b) not in rep and not re.fullmatch(r"\d{1,4}", b)] for blocks in pages]
    (Path(work) / "pages.json").write_text(json.dumps(["\n\n".join(b) for b in clean], ensure_ascii=False), encoding="utf-8")
    text = "\n\n".join("\n\n".join(b) for b in clean)
    meta = doc.metadata or {}
    chapters = [{"title": t.strip(), "page": pg, "level": lvl} for lvl, t, pg in doc.get_toc(simple=True) if t.strip()]
    return {"text": text, "pages": doc.page_count, "chapters": chapters, "ocr": used_ocr, "original": str(original),
            "title": _good_title(meta.get("title"), p), "author": _authors(meta.get("author")),
            "published": _iso(meta.get("creationDate")), "format": "pdf"}


# ---------------------------------------------------------------- Word, HTML, text, e-books
def _markitdown(p):
    from markitdown import MarkItDown
    return (MarkItDown().convert(str(p)).text_content or "").strip()


def _docx_text(p):
    """Paragraphs, headings (Heading n / Title styles), list items and tables from word/document.xml."""
    import xml.etree.ElementTree as ET
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(zipfile.ZipFile(p).read("word/document.xml"))

    def ptext(el):
        parts = []
        for n in el.iter():
            if n.tag == W + "t":
                parts.append(n.text or "")
            elif n.tag == W + "tab":
                parts.append("\t")
            elif n.tag in (W + "br", W + "cr"):
                parts.append("\n")
        return "".join(parts).strip()

    out = []
    body = root.find(W + "body")
    for el in (body if body is not None else []):
        if el.tag == W + "p":
            t = ptext(el)
            if not t:
                continue
            st = el.find(f"{W}pPr/{W}pStyle")
            val = (st.get(W + "val") if st is not None else "") or ""
            m = re.match(r"(?i)heading\s*(\d)", val)
            if val.lower() == "title":
                out.append("# " + t)
            elif m:
                out.append("#" * min(int(m.group(1)) + 1, 6) + " " + t)
            elif el.find(f"{W}pPr/{W}numPr") is not None:
                out.append("- " + t)
            else:
                out.append(t)
        elif el.tag == W + "tbl":
            rows = []
            for tr in el.iter(W + "tr"):
                cells = [" ".join(ptext(p) for p in tc.iter(W + "p")).replace("|", "/") for tc in tr.iter(W + "tc")]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                ncol = rows[0].count("|") - 1
                out.append("\n".join([rows[0], "|" + "---|" * ncol] + rows[1:]))
    return "\n\n".join(out)


def _docx_meta(p):
    try:
        x = zipfile.ZipFile(p).read("docProps/core.xml").decode("utf-8", "replace")
    except Exception:
        return {}
    g = lambda tag: (re.search(rf"<{tag}[^>]*>([^<]*)</{tag}>", x) or [None, None])[1]
    return {"title": g("dc:title"), "author": g("dc:creator"), "published": g("dcterms:created")}


def _doc_to_docx(p, work):
    import pythoncom
    import win32com.client
    out = Path(work) / (Path(p).stem + ".docx")
    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible, word.DisplayAlerts = False, 0
    try:
        d = word.Documents.Open(str(Path(p).resolve()), ReadOnly=True, AddToRecentFiles=False)
        d.SaveAs2(str(out.resolve()), FileFormat=16)
        d.Close(False)
    finally:
        word.Quit()
    return out


def _html_meta(h):
    def meta(*names):
        for n in names:
            m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]+content=["\']([^"\']+)', h, re.I) or \
                re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(n)}["\']', h, re.I)
            if m:
                return m.group(1).strip()
        return None
    canon = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', h, re.I)
    title = meta("og:title") or (re.search(r"<title[^>]*>([^<]+)", h, re.I) or [None, None])[1]
    return {"url": (canon.group(1) if canon else None) or meta("og:url"), "title": (title or "").strip() or None,
            "publication": meta("og:site_name"), "author": _authors(meta("author", "article:author")),
            "published": _iso(meta("article:published_time", "date", "pubdate"))}


def _ebook(p, work):
    conv = _tool("ebook-convert")
    if not conv:
        raise ExtractError("Calibre's ebook-convert is not installed (setup\\install_file_tools.ps1)")
    out = Path(work) / "book.txt"
    r = subprocess.run([conv, str(p), str(out), "--txt-output-formatting=markdown"], capture_output=True, text=True,
                       env=_env(), timeout=1800, creationflags=NOWIN, encoding="utf-8", errors="replace")
    if r.returncode or not out.exists():
        err = (r.stderr or r.stdout or "")
        if "DRM" in err:
            raise ExtractError("the e-book is DRM-protected (a Kindle purchase, for example), so it can't be converted")
        raise ExtractError(f"ebook-convert failed: {err.strip()[-200:]}")
    meta = {}
    em = _tool("ebook-meta")
    if em:
        m = subprocess.run([em, str(p)], capture_output=True, text=True, env=_env(), timeout=120,
                           creationflags=NOWIN, encoding="utf-8", errors="replace").stdout or ""
        f = lambda k: (re.search(rf"(?m)^{k}\s*:\s*(.+)$", m) or [None, None])[1]
        meta = {"title": f("Title"), "author": _authors((f(r"Author\(s\)") or "").split("[")[0]),
                "published": _iso(f("Published")), "publication": f("Publisher")}
    return read_text(out), meta


def _mhtml(p, work):
    """A saved web page (.mhtml/.mht): its HTML part written to work/page.html, and the page's own address."""
    import email
    from email import policy
    msg = email.message_from_bytes(Path(p).read_bytes(), policy=policy.default)
    url = msg.get("Snapshot-Content-Location") or msg.get("Content-Location")
    html_part = None
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html_part = part
            break
    if html_part is None:
        raise ExtractError("the saved web page has no HTML part")
    url = url or html_part.get("Content-Location")
    raw = html_part.get_payload(decode=True) or b""
    cs = html_part.get_content_charset()
    if not cs:   # Chrome often leaves the charset out of the part header: use the page's own <meta charset>
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4000], re.I)
        cs = m.group(1).decode() if m else "utf-8"
    try:
        h = raw.decode(cs, "replace")
    except LookupError:
        h = raw.decode("utf-8", "replace")
    out = Path(work) / "page.html"
    out.write_text(h, encoding="utf-8")
    return out, h, (url if url and url.startswith(("http://", "https://")) else None)


def _page_text(h, page_file, url):
    """Main text of a web page: trafilatura (article text, as for links), else markitdown (whole page)."""
    try:
        import trafilatura
        t = trafilatura.extract(h, url=url, output_format="markdown", include_links=True, favor_recall=True)
        if t and words(t) >= 150:
            return t
    except Exception:  # noqa: BLE001
        pass
    return _markitdown(page_file)


def _image_to_pdf(p, work):
    """A photo or scan as a one-page (or multi-frame TIFF) PDF, so it is OCR'd and filed like any PDF."""
    import pymupdf
    img = pymupdf.open(p)
    out = Path(work) / (Path(p).stem + ".pdf")
    pymupdf.open("pdf", img.convert_to_pdf()).save(out)
    img.close()
    return out


def _docx_images(p):
    try:
        return [n for n in zipfile.ZipFile(p).namelist() if n.startswith("word/media/")]
    except Exception:  # noqa: BLE001
        return []


def _docx_to_pdf(p, work):
    """Word file -> PDF with Word (as for .doc); if Word can't, a PDF of its pictures, one per page."""
    out = Path(work) / (Path(p).stem + ".pdf")
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible, word.DisplayAlerts = False, 0
        try:
            d = word.Documents.Open(str(Path(p).resolve()), ReadOnly=True, AddToRecentFiles=False)
            d.SaveAs2(str(out.resolve()), FileFormat=17)
            d.Close(False)
        finally:
            word.Quit()
        if out.exists():
            return out
    except Exception:  # noqa: BLE001
        pass
    import pymupdf
    z = zipfile.ZipFile(p)
    pdf = pymupdf.open()
    for n in _docx_images(p):
        try:
            img = pymupdf.open(stream=z.read(n), filetype=n.rsplit(".", 1)[-1])
            pdf.insert_pdf(pymupdf.open("pdf", img.convert_to_pdf()))
        except Exception:  # noqa: BLE001
            continue
    if not pdf.page_count:
        raise ExtractError("the Word file's pictures couldn't be read")
    pdf.save(out)
    return out


def _md_chapters(text):
    return [{"title": m.group(2).strip(), "page": None, "level": len(m.group(1))}
            for m in re.finditer(r"(?m)^(#{1,2})\s+(.+)$", text)]


# ---------------------------------------------------------------- entry point
def extract(path, work_dir, ocr=True):
    p, work = Path(path), Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise ExtractError(f"{ext or 'this'} files aren't handled yet")
    res = {"title": None, "author": [], "published": None, "url": None, "publication": None, "pages": None,
           "chapters": [], "ocr": False, "original": str(p), "format": ext.lstrip(".")}
    if ext == ".pdf":
        res.update(_pdf(p, work, ocr))
    elif ext in IMAGE_EXT:
        # The owner (2026-10-01): save the picture as a PDF, then process it like any other PDF (OCR included)
        res.update(_pdf(_image_to_pdf(p, work), work, ocr))
        res["made_pdf"] = True
        res["title"] = None
    elif ext in (".docx", ".doc"):
        src = _doc_to_docx(p, work) if ext == ".doc" else p
        try:
            text = _docx_text(src)
        except Exception as e:
            raise ExtractError(f"the Word file couldn't be read: {type(e).__name__}: {e}")
        m = _docx_meta(src)
        res.update({"text": text, "title": _good_title(m.get("title"), p), "author": _authors(m.get("author")),
                    "published": _iso(m.get("published")), "chapters": _md_chapters(text)})
        if _docx_images(src) and words(text) < DOCX_PHOTO_WORDS:
            # mostly photos (e.g. a manual photographed into Word): as a PDF, OCR'd like any PDF (2026-10-01)
            keep = {k: res[k] for k in ("title", "author", "published") if res.get(k)}
            res.update(_pdf(_docx_to_pdf(src, work), work, ocr))
            res.update(keep)
            res["made_pdf"] = True
            if words(text) > words(res.get("text") or ""):
                res["text"] = text
    elif ext in MHTML_EXT:
        page, h, url = _mhtml(p, work)
        res.update({k: v for k, v in _html_meta(h).items() if v})
        res["url"] = res.get("url") or url
        res["text"] = _page_text(h, page, res.get("url"))
        res["format"] = "mhtml"
    elif ext in HTML_EXT:
        h = read_text(p)
        res.update({k: v for k, v in _html_meta(h).items() if v})
        res["text"] = _markitdown(p)
    elif ext in EBOOK_EXT:
        text, m = _ebook(p, work)
        res.update({k: v for k, v in m.items() if v})
        res.update({"text": text, "chapters": _md_chapters(text)})
    else:
        res["text"] = read_text(p)
        res["chapters"] = _md_chapters(res["text"])
    res["text"] = re.sub(r"\n{3,}", "\n\n", (res.get("text") or "").replace("\r\n", "\n")).strip()
    res["words"] = words(res["text"])
    if res.get("format") == "pdf" and (res["words"] < MIN_WORDS or
                                       (res.get("pages") and res["words"] < MIN_WORDS_PER_PAGE * res["pages"])):
        # even after OCR there is (almost) no text: filed as the original only (routes.do_original_only)
        res["no_text"] = True
        return res
    if not res["text"]:
        raise ExtractError("no text could be read from the file")
    if not res.get("url"):
        m = URL_RE.search(res["text"][:600])   # only a link at the very top names the source
        res["url_in_text"] = m.group(0).rstrip(".,;") if m else None
    if not res.get("title"):
        h = re.search(r"(?m)^#\s+(.+)$", res["text"][:3000])
        res["title"] = h.group(1).strip() if h else None
    return res
