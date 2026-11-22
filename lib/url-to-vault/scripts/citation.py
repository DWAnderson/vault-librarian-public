"""Shared citation property set for the owner's vault: clean-up and Chicago-style `cite_as`.

Used by url_probe.py / url_to_note.py / citation_fill.py (this skill) and mirrored by name in
vault-librarian's okf_lint.py and normalize_props.py. Flat keys, because the Obsidian Web
Clipper cannot write nested YAML. Unknown values are omitted, never written empty.
"""
import datetime as dt
import os
import re

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("VAULT_TZ", "America/Chicago"))  # The owner's time zone; the shell runs in UTC
except Exception:  # pragma: no cover
    TZ = None


def now():
    return dt.datetime.now(TZ) if TZ else dt.datetime.now().astimezone()


def today():
    return now().date().isoformat()

KEYS = ["author", "publication", "publisher", "published", "accessed", "work_type", "doi",
        "journal", "volume", "issue", "pages", "section", "language", "word_count", "duration",
        "season", "episode", "platform", "archived_url", "cite_as", "captured_via", "summary_by",
        "audio_url"]

WORK_TYPES = {"article", "article-newspaper", "article-magazine", "article-journal", "post-weblog",
              "webpage", "image", "video", "podcast-episode"}
_WT_MAP = {"website": "webpage", "web page": "webpage", "webpage": "webpage", "blog": "post-weblog",
           "blogposting": "post-weblog", "newsarticle": "article-newspaper",
           "scholarlyarticle": "article-journal", "photo": "image", "image": "image",
           "imageobject": "image", "photograph": "image", "article": "article",
           "video": "video", "videoobject": "video", "podcastepisode": "podcast-episode"}

_ORG_WORDS = re.compile(r"\b(staff|editors?|editorial|board|team|news|press|associated|reuters|"
                        r"inc|llc|ltd|corp|company|university|institute|agency|department|"
                        r"association|society|foundation|council|times|journal|post|magazine)\b", re.I)


def empty(v):
    return v is None or v == "" or v == [] or v == {}


def _strip_link(s):
    s = re.sub(r"^\[\[(?:[^|\]]*\|)?([^\]]*)\]\]$", r"\1", s.strip())
    return re.sub(r"^(by|written by)\s+", "", s, flags=re.I).strip(" ,;")


def authors(v):
    """Normalize an author value (string, list, wikilinks, 'A and B', 'A; B') to a list of names."""
    if empty(v):
        return []
    items = v if isinstance(v, list) else [v]
    out = []
    for it in items:
        if isinstance(it, dict):
            it = it.get("name") or ""
        it = str(it)
        parts = re.split(r"\s*;\s*|\s+and\s+|\s*&\s*|\s*\|\s*", it)
        for p in parts:
            # "A Smith, B Jones" -> two names; "Smith, John" (one word each side) -> one name
            sub = [x.strip() for x in p.split(",")]
            if len(sub) > 1 and all(" " in x for x in sub if x):
                out += sub
            elif (len(sub) == 2 and sub[0] and sub[1] and len(sub[0].split()) <= 2
                  and 1 <= len(sub[1].split()) <= 3
                  and sub[1].rstrip(".").lower() not in ("jr", "sr", "ii", "iii", "iv", "inc", "llc")):
                out.append(f"{sub[1]} {sub[0]}")  # scholarly meta: "Last, First" -> "First Last"
            else:
                out.append(p)
    seen, res = set(), []
    for n in (_strip_link(x) for x in out):
        if n and n.lower() not in seen and not n.lower().startswith("http"):
            seen.add(n.lower())
            res.append(n)
    return res


def iso_date(v):
    """Return 'YYYY-MM-DD' (or the datetime's date) for many date spellings, else None."""
    if empty(v):
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = str(v).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%Y/%m/%d", "%Y",
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return d.date().isoformat() if fmt != "%Y" else s
        except ValueError:
            continue
    return None


def work_type(v, hint=None):
    for x in (v, hint):
        if empty(x):
            continue
        k = str(x).strip().lower()
        if k in WORK_TYPES:
            return k
        if k.startswith("video"):
            return "video"
        k = k.replace("_", "")
        if k in _WT_MAP:
            return _WT_MAP[k]
    return None


def clean(meta):
    """Return a cleaned copy of the citation keys in `meta` (empties dropped)."""
    out = {}
    for k in KEYS:
        v = meta.get(k)
        if empty(v):
            continue
        if k == "author":
            v = authors(v)
        elif k in ("published", "accessed"):
            v = iso_date(v) or (str(v).strip() if k == "published" else None)
        elif k == "work_type":
            v = work_type(v)
        elif k == "word_count":
            try:
                v = int(str(v).replace(",", ""))
            except ValueError:
                v = None
        elif k == "doi":
            v = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", str(v).strip(), flags=re.I)
        elif isinstance(v, str):
            v = " ".join(v.split())
        if not empty(v):
            out[k] = v
    return out


# ------------------------------------------------------------------ Chicago (notes-bibliography)
def _date_long(iso):
    try:
        d = dt.date.fromisoformat(str(iso)[:10])
        return f"{d:%B} {d.day}, {d.year}"
    except ValueError:
        return str(iso)


def _is_person(name):
    w = name.split()
    return 2 <= len(w) <= 4 and not _ORG_WORDS.search(name) and not any(c.isdigit() for c in name)


def _invert(name):
    if not _is_person(name):
        return name
    w = name.split()
    suffix = ""
    if w[-1].rstrip(".").lower() in ("jr", "sr", "ii", "iii", "iv"):
        suffix, w = ", " + w[-1], w[:-1]
    return f"{w[-1]}, {' '.join(w[:-1])}{suffix}"


def _names(auth, role=""):
    if not auth:
        return ""
    if len(auth) > 3:
        s = f"{_invert(auth[0])} et al."
    elif len(auth) == 1:
        s = _invert(auth[0])
    else:
        s = ", ".join([_invert(auth[0])] + auth[1:-1]) + f", and {auth[-1]}"
    return s + (f", {role}" if role else "")


def _end(s):
    s = s.strip()
    return s if s.endswith((".", "?", "!", "\"", "”")) else s + "."


def chicago(meta, title, url):
    """Chicago-style bibliography entry built from cleaned citation keys (Markdown italics)."""
    m = clean(meta)
    t = (title or "").strip()
    wt = m.get("work_type") or "webpage"
    auth = m.get("author", [])
    pub = m.get("publication")
    date = m.get("published")
    link = f"https://doi.org/{m['doi']}" if m.get("doi") else url
    parts = []
    if wt == "article-journal":
        if auth:
            parts.append(_end(_names(auth)))
        parts.append(f"“{_end(t)}”")
        j = m.get("journal") or pub
        seg = f"*{j}*" if j else ""
        if m.get("volume"):
            seg += f" {m['volume']}"
        if m.get("issue"):
            seg += f", no. {m['issue']}"
        if date:
            seg += f" ({str(date)[:4]})"
        if m.get("pages"):
            seg += f": {m['pages']}"
        if seg:
            parts.append(_end(seg))
    elif wt == "video":
        who = auth[0] if auth else None
        if who:
            parts.append(_end(who))
        parts.append(f"“{_end(t)}”")
        seg = f"{m.get('platform') or 'YouTube'} video"
        if m.get("duration"):
            seg += f", {m['duration']}"
        parts.append(_end(seg))
        if date:
            parts.append(_end(_date_long(date)))
    elif wt == "podcast-episode":
        if auth:
            parts.append(_end(_names(auth, "host")))
        parts.append(f"“{_end(t)}”")
        seg = f"*{pub}*" if pub else "Podcast"
        if m.get("season") or m.get("episode"):
            seg += ", " + ", ".join(x for x in (f"season {m['season']}" if m.get("season") else "",
                                               f"episode {m['episode']}" if m.get("episode") else "") if x)
        if m.get("publisher"):
            seg += f". Produced by {m['publisher']}"
        seg += ". Podcast"
        if m.get("duration"):
            seg += f", {m['duration']}"
        parts.append(_end(seg))
        if date:
            parts.append(_end(_date_long(date)))
    elif wt == "image":
        if auth:
            parts.append(_end(_names(auth)))
        parts.append(_end(f"*{t}*" if t else "Untitled image"))
        if date:
            parts.append(_end(str(date)[:4]))
        parts.append("Image.")
        if pub:
            parts.append(_end(pub))
    else:  # articles, blog posts, web pages
        if auth:
            parts.append(_end(_names(auth)))
        parts.append(f"“{_end(t)}”")
        container = pub or m.get("publisher")
        if container:
            italic = wt.startswith("article") or wt == "post-weblog"
            parts.append(_end((f"*{container}*" if italic else container)
                              + (f", {_date_long(date)}" if date else "")))
        elif date:
            parts.append(_end(_date_long(date)))
    if not date and m.get("accessed") and wt not in ("article-journal",):
        parts.append(_end(f"Accessed {_date_long(m['accessed'])}"))
    if link:
        parts.append(_end(link))
    return " ".join(p for p in parts if p and p != ".")
