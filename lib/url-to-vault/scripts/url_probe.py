#!/usr/bin/env python3
"""Fetch a URL, classify it for the vault, and extract citation metadata + text.

Usage:
  python url_probe.py <url> --out probe.json [--text-out probe.md] [--metadata-only]
  python url_probe.py <url> --html page.html --out probe.json     # page saved from a browser

Writes probe.json (no page text in it) and, unless --metadata-only, the extracted main text as
Markdown to --text-out (default: <out>.md). Prints a short summary only, so page text never
has to pass through the chat.

Categories:  article | text | image | unsupported
For unsupported, `reason` says why and `try_browser: true` means a signed-in browser may get it
(bot wall, paywall stub, JavaScript-only page).
Needs: requests, lxml, trafilatura, extruct (pip install trafilatura extruct); pypdf for PDFs.
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlencode, urlunparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import citation  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0 Safari/537.36")
ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "scholarlyarticle", "report",
                 "opinionnewsarticle", "analysisnewsarticle", "reportagenewsarticle",
                 "backgroundnewsarticle", "reviewnewsarticle", "techarticle", "socialmediaposting",
                 "medicalscholarlyarticle", "liveblogposting", "posting"}
IMAGE_TYPES = {"imageobject", "photograph"}
AV_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "spotify.com", "podcasts.apple.com",
            "tiktok.com", "soundcloud.com", "twitch.tv", "rumble.com", "music.apple.com")
IMAGE_HOSTS = ("flickr.com/photos/", "imgur.com/", "unsplash.com/photos/", "500px.com/photo/",
               "commons.wikimedia.org/wiki/file:", "instagram.com/p/", "pinterest.com/pin/")
TRACK_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|_hsenc$|_hsmi$|mkt_tok$|cmpid$|"
                          r"ref$|ref_src$|smid$|sh$|igshid$)", re.I)
MIN_ARTICLE, MIN_TEXT, MAX_IMAGE_TEXT = 300, 150, 150
# Reference works: always "text" (summary), no personal author; value = (publication, publisher)
REFERENCE_HOSTS = {"wikipedia.org": ("Wikipedia", "Wikimedia Foundation"),
                   "wiktionary.org": ("Wiktionary", "Wikimedia Foundation"),
                   "britannica.com": ("Encyclopaedia Britannica", None),
                   "law.cornell.edu": ("Legal Information Institute, Cornell Law School", None),
                   "investopedia.com": ("Investopedia", None)}


def unwrap(url):
    """Undo Outlook Safe Links / Google / Facebook redirect wrappers and strip tracking params."""
    for _ in range(3):
        p = urlparse(url)
        q = parse_qs(p.query)
        host = p.netloc.lower()
        if "safelinks.protection.outlook.com" in host and "url" in q:
            url = q["url"][0]
        elif host.endswith("google.com") and p.path == "/url" and ("q" in q or "url" in q):
            url = (q.get("q") or q.get("url"))[0]
        elif host in ("l.facebook.com", "lm.facebook.com") and "u" in q:
            url = q["u"][0]
        else:
            break
    p = urlparse(url.strip())
    keep = [(k, v) for k, v in parse_qs(p.query, keep_blank_values=True).items() if not TRACK_PARAMS.match(k)]
    query = urlencode([(k, x) for k, vs in keep for x in vs])
    return urlunparse(p._replace(query=query, fragment=""))


def ld_items(data):
    """Flatten JSON-LD into a list of dicts with a lower-cased set of types under '_types'."""
    out = []

    def visit(o):
        if isinstance(o, list):
            for x in o:
                visit(x)
        elif isinstance(o, dict):
            if "@graph" in o:
                visit(o["@graph"])
            t = o.get("@type")
            if t:
                o = dict(o)
                o["_types"] = {str(x).lower().split("/")[-1] for x in (t if isinstance(t, list) else [t])}
                out.append(o)
    visit(data)
    return out


def first(*vals):
    for v in vals:
        if isinstance(v, list):
            v = next((x for x in v if x), None)
        if isinstance(v, dict):
            v = v.get("name") or v.get("@value") or v.get("url")
        if v not in (None, "", []):
            return v.strip() if isinstance(v, str) else v
    return None


def names(v):
    if not v:
        return []
    v = v if isinstance(v, list) else [v]
    return [x.get("name") if isinstance(x, dict) else x for x in v if x]


def meta_tags(tree):
    m = {}
    for el in tree.xpath("//meta"):
        k = (el.get("property") or el.get("name") or el.get("itemprop") or "").strip().lower()
        c = el.get("content")
        if k and c and c.strip():
            m.setdefault(k, []).append(c.strip())
    return m


def one(m, *keys):
    for k in keys:
        if m.get(k):
            return m[k][0]
    return None


def probe_html(url, html, final_url, status):
    import extruct
    import lxml.html
    import trafilatura
    res = {"status": status, "final_url": final_url}
    tree = lxml.html.fromstring(html)
    mt = meta_tags(tree)
    try:
        ex = extruct.extract(html, base_url=final_url, syntaxes=["json-ld"], uniform=True, errors="ignore")
        lds = ld_items(ex.get("json-ld", []))
    except Exception:
        lds = []
    types = set().union(*[x["_types"] for x in lds]) if lds else set()
    art = next((x for x in lds if x["_types"] & ARTICLE_TYPES), None)
    img_ld = next((x for x in lds if x["_types"] & IMAGE_TYPES), None)
    canon = tree.xpath("//link[@rel='canonical']/@href")
    canonical = urljoin(final_url, canon[0]) if canon else (one(mt, "og:url") or final_url)
    if not canonical.startswith("http"):
        canonical = final_url

    text = trafilatura.extract(html, url=final_url, output_format="markdown", include_links=True,
                               include_formatting=True, include_tables=True, include_images=False,
                               include_comments=False, favor_precision=True) or ""
    tm = trafilatura.extract_metadata(html, default_url=final_url)
    tmd = tm.as_dict() if tm else {}
    words = len(re.findall(r"\w+", text))

    try:   # web comics (2026-10-18): only the cartoon, its hover text and bonus panel (comics.py)
        import comics
        if comics.is_comic_page(final_url) or (not art and words <= MAX_IMAGE_TEXT * 2 and comics.looks_like_comic(tree)):
            cr = comics.probe(final_url, tree, mt)
            if cr:
                cr["meta"].update(resource=canonical if comics.site_for(canonical) or not comics.site_for(final_url) else final_url,
                                  accessed=citation.today())
                res.update(cr)
                return res, ""
    except Exception as e:  # noqa: BLE001  (fall back to the general rules below)
        res["comic_error"] = f"{type(e).__name__}: {e}"

    title = first(one(mt, "citation_title", "og:title", "twitter:title"), art and art.get("headline"),
                  tmd.get("title"), (tree.findtext(".//title") or "").strip())
    host = urlparse(final_url).netloc.lower().replace("www.", "")
    ref = next((v for k, v in REFERENCE_HOSTS.items() if host == k or host.endswith("." + k)), None)
    if title:
        title = re.sub(r"\s+[-–—|]\s+(Wikipedia|Wikimedia Commons|Wiktionary)$", "", str(title))
        title = re.sub(r"^File:", "", title) if host.endswith("commons.wikimedia.org") else title
    auth = (names(art.get("author")) if art else []) or mt.get("citation_author", []) \
        or ([one(mt, "author", "article:author", "parsely-author", "sailthru.author", "dc.creator")]
            if one(mt, "author", "article:author", "parsely-author", "sailthru.author", "dc.creator") else []) \
        or ([tmd["author"]] if tmd.get("author") else [])
    auth = [a for a in auth if a and not str(a).startswith("http")]
    pub_org = first(art and art.get("publisher"))
    site = first(one(mt, "og:site_name", "application-name"), tmd.get("sitename"), pub_org)
    published = first(art and art.get("datePublished"),
                      one(mt, "article:published_time", "citation_publication_date", "citation_date",
                          "dc.date", "date", "pubdate", "parsely-pub-date"), tmd.get("date"))
    og_type = (one(mt, "og:type") or "").lower()
    scholarly = bool(mt.get("citation_title") or mt.get("citation_doi") or "scholarlyarticle" in types)
    lang = (tree.get("lang") or one(mt, "og:locale", "language") or "").split("-")[0].split("_")[0] or None

    meta = {
        "title": title,
        "description": first(art and art.get("description"), one(mt, "description", "og:description"),
                             tmd.get("description")),
        "resource": canonical,
        "author": auth,
        "publication": first(mt.get("citation_journal_title"), site) if scholarly else site,
        "publisher": pub_org if pub_org and pub_org != site else one(mt, "citation_publisher"),
        "published": published,
        "accessed": citation.today(),
        "doi": one(mt, "citation_doi", "dc.identifier", "prism.doi"),
        "journal": one(mt, "citation_journal_title"),
        "volume": one(mt, "citation_volume", "prism.volume"),
        "issue": one(mt, "citation_issue", "prism.number"),
        "pages": ("-".join(x for x in (one(mt, "citation_firstpage"), one(mt, "citation_lastpage")) if x) or None),
        "section": first(art and art.get("articleSection"), one(mt, "article:section")),
        "language": lang,
    }
    if meta["doi"] and not re.search(r"10\.\d{4,}/", str(meta["doi"])):
        meta["doi"] = None
    wt = ("article-journal" if scholarly else
          "article-newspaper" if art and art["_types"] & {"newsarticle", "reportagenewsarticle", "analysisnewsarticle",
                                                          "opinionnewsarticle", "backgroundnewsarticle"} else
          "post-weblog" if art and "blogposting" in art["_types"] else
          "article" if art or og_type == "article" else None)

    if ref:  # reference work: summarize, cite the work itself, no "author"
        wt, scholarly, art = None, False, None
        meta.update(author=[], publication=ref[0], publisher=ref[1])
        mod = first(*[x.get("dateModified") for x in lds], one(mt, "article:modified_time"))
        if mod:
            meta["published"] = mod  # reference works are cited by their last revision
    if host.endswith("commons.wikimedia.org"):
        meta.update(publication="Wikimedia Commons", publisher="Wikimedia Foundation")

    # ---- image signals
    host_path = (urlparse(final_url).netloc + urlparse(final_url).path).lower().replace("www.", "")
    og_image = one(mt, "og:image", "og:image:url", "twitter:image")
    image_host = any(h in host_path for h in IMAGE_HOSTS)
    image_page = (og_type in ("photo", "image", "og:photo") or bool(img_ld and not art) or image_host)
    main_img = None
    og_base = (og_image or "").split("?")[0].rsplit("/", 1)[-1].lower()
    for el in tree.xpath("//img[@src]"):
        src_base = el.get("src").split("?")[0].rsplit("/", 1)[-1].lower()
        idc = ((el.get("id") or "") + " " + (el.get("class") or "")).lower()
        if (og_base and src_base == og_base) or re.search(r"\b(cc-comic|comic|main-image|hero-image)\b", idc):
            main_img = el
            break
    if image_page and (image_host or words <= MAX_IMAGE_TEXT * 3) and (og_image or img_ld):
        img_url = first(img_ld and img_ld.get("contentUrl"), img_ld and img_ld.get("url"), og_image)
        alt = None
        if img_url:
            for el in tree.xpath("//img[@alt]"):
                if el.get("src") and (el.get("src") in img_url or img_url.endswith(el.get("src").split("/")[-1])):
                    alt = el.get("alt").strip() or None
                    break
        if meta.get("title"):
            meta["title"] = re.sub(r"\.(jpe?g|png|gif|webp|tiff?|heic)$", "", meta["title"], flags=re.I)
        meta.update({"work_type": "image",
                     "author": auth or names(img_ld.get("creator") or img_ld.get("author")) if img_ld else auth})
        res.update(category="image", image_url=unwrap(urljoin(final_url, img_url)) if img_url else None,
                   caption=first(img_ld and img_ld.get("caption"), img_ld and img_ld.get("description"),
                                 meta["description"]),
                   alt=alt, credit=first(img_ld and img_ld.get("creditText"),
                                         img_ld and img_ld.get("copyrightHolder"),
                                         img_ld and img_ld.get("license")),
                   context=text if text and words <= 400 else None)
        meta["word_count"] = None
    elif (wt or scholarly) and words >= MIN_ARTICLE:
        meta["work_type"] = wt or "article"
        res.update(category="article")
    elif (wt or scholarly) and words < MIN_ARTICLE:
        res.update(category="unsupported", try_browser=True,
                   reason=f"article page but only {words} words of text came through (paywall, sign-in or JavaScript)")
    elif words >= MIN_TEXT:
        meta["work_type"] = "webpage"
        res.update(category="text")
    elif words <= MAX_IMAGE_TEXT and main_img is not None:
        # one main picture on a text-light page (web comics, photo posts): the <img> that is the
        # page's own preview image, or a comic/hero image; its title/alt text is the caption
        if meta.get("title"):
            meta["title"] = re.sub(r"\s*[-|]\s*" + re.escape(str(site or "")) + r"\s*$", "", meta["title"]) if site else meta["title"]
        meta["work_type"] = "image"
        cap = (main_img.get("title") or "").strip() or None
        res.update(category="image", image_url=unwrap(urljoin(final_url, main_img.get("src"))),
                   caption=cap or meta.get("description"), alt=(main_img.get("alt") or "").strip() or None,
                   credit=None, context=text if text and words <= 150 else None)
        meta["word_count"] = None
    else:
        res.update(category="unsupported", try_browser=True,
                   reason=f"only {words} words of readable text and no single main image")
    if res.get("category") != "image":
        meta["word_count"] = words or None
    res["meta"] = meta
    return res, text


def probe_pdf(data, final_url, status):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    info = r.metadata or {}
    text = "\n\n".join((p.extract_text() or "") for p in r.pages[:80])
    text = re.sub(r"[ \t]+\n", "\n", text)
    words = len(re.findall(r"\w+", text))
    doi = re.search(r"\b(10\.\d{4,9}/[^\s\"<>]+)", text[:6000])
    scholarly = bool(doi) or bool(re.search(r"\babstract\b", text[:4000], re.I))
    meta = {"title": (info.get("/Title") or "").strip() or None,
            "author": citation.authors(info.get("/Author")) if info.get("/Author") else [],
            "resource": final_url, "accessed": citation.today(),
            "doi": doi.group(1).rstrip(".,;)") if doi else None,
            "published": citation.iso_date((info.get("/CreationDate") or "")[2:10]) if scholarly else None,
            "word_count": words}
    res = {"status": status, "final_url": final_url, "pdf": True, "meta": meta}
    if words < MIN_TEXT:
        res.update(category="unsupported", reason="PDF has no extractable text (scanned image?)")
    elif scholarly and words >= 1500:
        meta["work_type"] = "article-journal"
        res.update(category="article")
    else:
        meta["work_type"] = "webpage"
        res.update(category="text")
    return res, text


def probe(url, html_file=None):
    import requests
    url = unwrap(url)
    host = urlparse(url).netloc.lower().replace("www.", "")
    if any(host == h or host.endswith("." + h) for h in AV_HOSTS):
        return {"url": url, "category": "unsupported", "reason": "video or audio page (not handled by this routine)",
                "meta": {"resource": url}}, ""
    if html_file:
        html = open(html_file, encoding="utf-8", errors="replace").read()
        res, text = probe_html(url, html, url, "browser")
        res["url"], res["via"] = url, "browser"
        if res.pop("try_browser", None):
            res["reason"] = res["reason"].replace("(paywall, sign-in or JavaScript)", "even in the signed-in browser")
        return res, text
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8",
                                       "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                         timeout=30, allow_redirects=True)
    except Exception as e:
        return {"url": url, "category": "unsupported", "try_browser": True,
                "reason": f"could not fetch: {type(e).__name__}: {str(e)[:160]}", "meta": {"resource": url}}, ""
    if "charset" not in (r.headers.get("content-type") or "").lower():
        # No charset in the header: requests would assume ISO-8859-1 for text/*, which garbles
        # UTF-8 pages (e.g. therundown.ai) into C1 control characters that YAML then rejects.
        try:
            r.content.decode("utf-8")
            r.encoding = "utf-8"
        except UnicodeDecodeError:
            r.encoding = r.apparent_encoding or r.encoding
    final, ctype = r.url, (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    base = {"url": url, "final_url": final, "status": r.status_code, "content_type": ctype}
    if r.status_code in (401, 403, 429, 451) or (r.status_code >= 400 and "captcha" in r.text[:5000].lower()):
        return {**base, "category": "unsupported", "try_browser": True,
                "reason": f"site refused the download (HTTP {r.status_code}; bot wall or sign-in)",
                "meta": {"resource": final}}, ""
    if r.status_code >= 500 and "html" in ctype:
        # Some web-comic sites (SMBC, 2026-10-24) send their full, normal comic page with a
        # server-error status (a PHP warning on their side); a browser shows it fine. Keep the page
        # when it really is a comic page with its comic image in it; anything else stays an error.
        try:
            cres, ctext = probe_html(url, r.text, final, r.status_code)
            if cres.get("comic"):
                cres["served_with_error"] = f"HTTP {r.status_code}"
                return {**base, **cres}, ctext
        except Exception:  # noqa: BLE001
            pass
    if r.status_code >= 400:
        return {**base, "category": "unsupported", "reason": f"HTTP {r.status_code}", "meta": {"resource": final}}, ""
    if ctype.startswith("image/"):
        name = unquote(urlparse(final).path.rsplit("/", 1)[-1]) or "image"
        return {**base, "category": "image", "image_url": final, "caption": None, "alt": None, "credit": None,
                "meta": {"title": os.path.splitext(name)[0].replace("_", " ").replace("-", " "),
                         "resource": final, "accessed": citation.today(), "work_type": "image"}}, ""
    if ctype == "application/pdf" or final.lower().endswith(".pdf"):
        res, text = probe_pdf(r.content, final, r.status_code)
        return {**base, **res}, text
    if ctype.startswith(("video/", "audio/")):
        return {**base, "category": "unsupported", "reason": f"{ctype} media file", "meta": {"resource": final}}, ""
    if "html" not in ctype and "xml" not in ctype and ctype not in ("", "text/plain"):
        return {**base, "category": "unsupported", "reason": f"download of type {ctype}", "meta": {"resource": final}}, ""
    if ctype == "text/plain":
        text = r.text
        words = len(re.findall(r"\w+", text))
        cat = "text" if words >= MIN_TEXT else "unsupported"
        return {**base, "category": cat, "reason": None if cat == "text" else "too little text",
                "meta": {"resource": final, "accessed": citation.today(), "work_type": "webpage",
                         "word_count": words}}, text
    res, text = probe_html(url, r.text, final, r.status_code)
    return {**base, **res}, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", required=True)
    ap.add_argument("--text-out")
    ap.add_argument("--html", help="HTML saved from a browser (fallback for blocked/paywalled pages)")
    ap.add_argument("--metadata-only", action="store_true")
    a = ap.parse_args()
    res, text = probe(a.url, a.html)
    meta = res.get("meta") or {}
    title = meta.get("title")
    meta.update(citation.clean(meta))
    for k in [k for k, v in meta.items() if citation.empty(v)]:
        del meta[k]
    if title:
        meta["title"] = " ".join(str(title).split())
    if not a.metadata_only and text:
        tp = a.text_out or os.path.splitext(a.out)[0] + ".md"
        with open(tp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        res["text_path"] = os.path.abspath(tp)
    res["word_count"] = meta.get("word_count", 0)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False, default=str)
    print(f"category: {res['category']}" + (f"  ({res.get('reason')})" if res.get("reason") else ""))
    print(f"title: {meta.get('title')}")
    print("citation: " + ", ".join(f"{k}={meta[k]}" for k in ("author", "publication", "published", "work_type", "doi")
                                    if meta.get(k)))
    print(f"words: {res['word_count']}" + (f"  image: {res.get('image_url')}" if res.get("image_url") else "")
          + ("  try_browser: yes" if res.get("try_browser") else ""))


if __name__ == "__main__":
    main()
