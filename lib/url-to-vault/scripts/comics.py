"""Web comics (2026-10-18): recognise a web cartoon - a comic page linked in Remember, or a
comic newsletter email such as SMBC's - and keep only the cartoon itself: the strip, any bonus
panel and the hover (title) text. The site's navigation, store and book ads, share buttons and the
newsletter's tracking links are left out.

Used by url_probe.py (link intake) and the app's jobs/queue_run.py do_email (Remember emails).
The laptop url-to-vault skill has its own copy of url_probe/url_to_note: consider the same change there.

  site_for(url)              -> (host, facts) for a known comic site, else None
  probe(final_url, tree, mt) -> url_probe result (category "image", comic=True, images=[...]) or None
  looks_like_comic(tree)     -> True for an unknown site whose page is plainly one comic image
  from_email(msg)            -> {"url": comic page} or {"images": [...], ...} for a comic newsletter, else None
"""
import re
from urllib.parse import urljoin, urlparse

# host -> facts. main/bonus: XPath for the comic <img>(s), tried in order. Hover text is the main
# image's title attribute (xkcd, SMBC) unless the site keeps it elsewhere.
SITES = {
    "smbc-comics.com": {"publication": "Saturday Morning Breakfast Cereal", "author": ["Zach Weinersmith"],
                        "main": ["//img[@id='cc-comic']"], "bonus": ["//div[@id='aftercomic']//img"]},
    "xkcd.com": {"publication": "xkcd", "author": ["Randall Munroe"], "main": ["//div[@id='comic']//img"]},
    "existentialcomics.com": {"publication": "Existential Comics", "author": ["Corey Mohler"],
                              "main": ["//img[contains(@class,'comicImg')]"]},
    "poorlydrawnlines.com": {"publication": "Poorly Drawn Lines", "author": ["Reza Farazmand"],
                             "main": ["//div[contains(@class,'entry-content')]//img",
                                      "//figure[contains(@class,'wp-block-image')]//img"]},
    "theoatmeal.com": {"publication": "The Oatmeal", "author": ["Matthew Inman"],
                       "main": ["//div[@id='comic']//img"]},
    "phdcomics.com": {"publication": "PHD Comics", "author": ["Jorge Cham"],
                      "main": ["//img[@id='comic2']", "//img[@id='comic']"]},
    "explosm.net": {"publication": "Cyanide & Happiness", "main": ["//div[contains(@class,'MainComic__ComicImage')]//img"]},
    "qwantz.com": {"publication": "Dinosaur Comics", "author": ["Ryan North"], "main": ["//img[@class='comic']"]},
    "questionablecontent.net": {"publication": "Questionable Content", "author": ["Jeph Jacques"],
                                "main": ["//img[@id='strip']"]},
    "pbfcomics.com": {"publication": "The Perry Bible Fellowship", "author": ["Nicholas Gurewitch"],
                      "main": ["//div[@id='comic']//img"]},
    "wondermark.com": {"publication": "Wondermark", "author": ["David Malki"], "main": ["//div[@id='comic']//img"]},
    "buttersafe.com": {"publication": "Buttersafe", "main": ["//div[@id='comic']//img"]},
    "mrlovenstein.com": {"publication": "Mr. Lovenstein", "author": ["J. L. Westover"],
                         "main": ["//img[contains(@src,'/images/comics/')]"]},
    "monkeyuser.com": {"publication": "MonkeyUser", "main": ["//div[contains(@class,'content')]//img"]},
    "commitstrip.com": {"publication": "CommitStrip", "main": ["//div[contains(@class,'entry-content')]//img"]},
    "sarahcandersen.com": {"publication": "Sarah's Scribbles", "author": ["Sarah Andersen"],
                           "main": ["//div[contains(@class,'post')]//img"]},
    "loadingartist.com": {"publication": "Loading Artist", "author": ["Gregor Czaykowski"],
                          "main": ["//div[contains(@class,'main-image-container')]//img"]},
    "gocomics.com": {"publication": "GoComics", "main": ["//img[contains(@class,'Comic_comic__image')]",
                                                          "//picture[contains(@class,'item-comic-image')]//img"]},
}
GENERIC_MAIN = ["//img[@id='cc-comic']", "//*[@id='comic']//img", "//img[@id='comic']", "//img[@id='strip']",
                "//img[contains(concat(' ',normalize-space(@class),' '),' comic ')]"]
# pages on a comic host that are not a comic (store, about, archive lists, blog, book promos)
NOT_COMIC_PATH = re.compile(r"^/(store|shop|merch|about|contact|archive|archives|blog|news|books?|faq|"
                            r"privacy|terms|rss|feed|patreon|support|newsletter)\b", re.I)
CHROME_IMG = re.compile(r"logo|icon|avatar|social|facebook|twitter|instagram|patreon|mastodon|bluesky|"
                        r"reddit|tumblr|share|button|banner|spacer|pixel|badge|open\.php|/track/|"
                        r"\bstore\b|merch|header|footer|divider", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def _host(url):
    return urlparse(url or "").netloc.lower().split(":")[0].removeprefix("www.")


def site_for(url):
    h = _host(url)
    for k, v in SITES.items():
        if h == k or h.endswith("." + k):
            return k, v
    return None


def is_comic_page(url):
    """A comic site's page that shows a comic (not its store, archive list, about page...)."""
    return bool(site_for(url)) and not NOT_COMIC_PATH.search(urlparse(url).path or "/")


def _first(tree, xpaths):
    for xp in xpaths:
        for el in tree.xpath(xp):
            src = el.get("src") or el.get("data-src") or ""
            if src and not CHROME_IMG.search(src):
                return el
    return None


def _src(el, base):
    s = el.get("src") or el.get("data-src") or ""
    if el.get("srcset") and not s:
        s = el.get("srcset").split(",")[-1].strip().split(" ")[0]
    return urljoin(base, s)


def _published(tree, mt, img_url):
    txt = " ".join(tree.xpath("//body//text()"))[:200000]
    m = re.search(r"Posted\s+(?:on\s+)?(" + MONTHS + r")\s+(\d{1,2}),?\s+(\d{4})", txt)
    if m:
        import datetime as dt
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
    for k in ("article:published_time", "og:published_time", "date", "dc.date"):
        v = (mt.get(k) or [None])[0] if isinstance(mt.get(k), list) else mt.get(k)
        if v and re.match(r"\d{4}-\d{2}-\d{2}", str(v)):
            return str(v)[:10]
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", img_url or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _meta1(mt, *keys):
    for k in keys:
        v = mt.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            return str(v).strip()
    return None


def probe(final_url, tree, mt, page_title=None):
    """url_probe result for a comic page, or None when no comic image can be found."""
    hit = site_for(final_url)
    facts = hit[1] if hit else {}
    main = _first(tree, facts.get("main", []) + GENERIC_MAIN)
    if main is None and hit:
        og = _meta1(mt, "og:image", "twitter:image")
        if og and not CHROME_IMG.search(og):
            images = [{"url": urljoin(final_url, og), "role": "comic", "hover": None}]
        else:
            return None
    elif main is None:
        return None
    else:
        images = [{"url": _src(main, final_url), "role": "comic", "hover": (main.get("title") or "").strip() or None,
                   "alt": (main.get("alt") or "").strip() or None}]
    for xp in facts.get("bonus", []):
        for el in tree.xpath(xp):
            u = _src(el, final_url)
            if u and u not in [i["url"] for i in images]:
                images.append({"url": u, "role": "bonus panel", "hover": None})
    hover = images[0].get("hover")
    title = page_title or _meta1(mt, "og:title", "twitter:title") or (tree.findtext(".//title") or "").strip() or final_url
    title = re.sub(r"\s*[|]\s*.*$", "", title).strip()
    pub = facts.get("publication") or _meta1(mt, "og:site_name")
    published = _published(tree, mt, images[0]["url"])
    short = re.sub(r"^" + re.escape(pub) + r"\s*[-:–—]\s*", "", title) if pub else title
    desc = (f"{pub} webcomic “{short}”" if pub else f"Webcomic “{title}”") \
        + (f" ({published})" if published else "") \
        + (", saved with its hover text" if hover else "") \
        + (" and bonus panel" if len(images) > 1 else "") + "."
    meta = {"title": title, "resource": final_url, "work_type": "image", "publication": pub,
            "author": facts.get("author") or [], "published": published, "description": desc, "word_count": None}
    return {"category": "image", "comic": True, "images": images, "image_url": images[0]["url"],
            "caption": hover, "alt": images[0].get("alt"), "credit": None, "context": None,
            "meta": {k: v for k, v in meta.items() if v not in (None, [], "")}}


def looks_like_comic(tree):
    """Unknown site: does the page mark one image as 'the comic' (WordPress comic themes, ComicControl...)?"""
    return _first(tree, GENERIC_MAIN) is not None


# ------------------------------------------------------------------ newsletter emails
def _resolve(url, session, hops=5):
    """Follow redirects (newsletter click-tracking links) without downloading the page."""
    for _ in range(hops):
        try:
            r = session.get(url, allow_redirects=False, timeout=20, stream=True)
            r.close()
        except Exception:  # noqa: BLE001
            return url
        loc = r.headers.get("location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            url = urljoin(url, loc)
            continue
        return url
    return url


def from_email(msg, fetch=True):
    """A comic newsletter -> {"url": the comic's web page} (preferred: the page has the hover text and
    bonus panel) or {"images": [...], "title", "publication"} taken from the email itself; else None."""
    import lxml.html
    body = msg.get("body") or {}
    if (body.get("contentType") or "").lower() != "html" or not body.get("content"):
        return None
    tree = lxml.html.fromstring(body["content"])
    sender = ((msg.get("from") or {}).get("emailAddress") or {})
    s_dom = (sender.get("address") or "").lower().rsplit("@", 1)[-1]
    hit = site_for("https://" + s_dom + "/")
    imgs = []
    for el in tree.xpath("//img[@src]"):
        src = el.get("src")
        try:
            w = int(re.sub(r"\D", "", el.get("width") or "") or 0)
        except ValueError:
            w = 0
        if CHROME_IMG.search(src) or (w and w < 250):
            continue
        comic_src = bool(site_for(src)) or re.search(r"/(comics?|strips?|cartoons?)/", src, re.I)
        if comic_src or w >= 400:
            imgs.append({"url": src, "role": "comic", "hover": (el.get("title") or "").strip() or None,
                         "alt": (el.get("alt") or "").strip() or None, "comic_src": bool(comic_src)})
    if not hit and not any(i["comic_src"] for i in imgs):
        return None   # not a comic newsletter: an ordinary email note
    # the comic image(s) in the email itself - returned with the page link too, so the cartoon is
    # still filed from the email when the comic's page can't be saved (SMBC's HTTP 500, 2026-10-24)
    kept = [dict(i) for i in ([i for i in imgs if i["comic_src"]] or imgs)]
    for i in kept[1:]:
        i["role"] = "bonus panel"
    fallback = {"images": kept, "title": msg.get("subject") or "Comic",
                "publication": (hit[1]["publication"] if hit else sender.get("name")) or None,
                "author": hit[1].get("author", []) if hit else []} if kept else {}
    # 1) a direct link to a comic page on the comic's site
    links = [(a.get("href"), " ".join(a.itertext()).strip()) for a in tree.xpath("//a[@href]")]
    for href, _ in links:
        if href and is_comic_page(href) and urlparse(href).path.strip("/"):
            return {"url": href.split("#")[0], **fallback}
    # 2) click-tracked links: "See this update on the website" first, then the others (not the
    #    mailing-list archive, preferences or unsubscribe links)
    if fetch:
        import requests
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0"
        want = re.compile(r"(see|view|read)\b.*\b(website|site|online)|bonus|comic|today", re.I)
        skip = re.compile(r"in your browser|unsubscribe|preferences|profile|forward|store|shop|book|patreon|mailto:", re.I)
        cands = [h for h, t in links if h and h.startswith("http") and want.search(t) and not skip.search(t + " " + h)]
        cands += [h for h, t in links if h and h.startswith("http") and h not in cands and not skip.search(t + " " + h)]
        for h in cands[:8]:
            u = _resolve(h, s)
            if is_comic_page(u) and urlparse(u).path.strip("/"):
                return {"url": u.split("#")[0].split("?")[0], **fallback}
    # 3) no page: keep the comic image(s) from the email itself
    return fallback or None
