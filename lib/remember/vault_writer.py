r"""Write Remember notes as OKF v0.2 notes in the owner's Obsidian vault (Inbox/).

COPY, adapted, of the Daily Summary's platform/digest-core/vault_writer.py, taken 2026-09-26.
The apps keep separate copies on purpose; when you change one, consider the same change in
the other (see README.md "Code kept in step with the Daily Summary").

Differences from the Daily Summary copy:
  * vault location: env REMEMBER_VAULT_PATH (tests) -> Obsidian\config\vault.json "vault_path";
  * generated.by = process:vault-remember; adds the "Transcript" note type;
  * ledger: %LOCALAPPDATA%\VaultLibrarian\remember_saved.json (Remember's own).
Refuses to write unless the folder contains .obsidian and Inbox\. Never overwrites a file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "VaultLibrarian")
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "vault.json")
LEDGER = os.path.join(LOCAL, "remember_saved.json")
INBOX = "Inbox"
GENERATED_BY = "process:vault-remember"
TYPE_VIDEO = "Video Summary"
TYPE_PODCAST = "Podcast Transcript"
TYPE_TRANSCRIPT = "Transcript"
MAX_NAME = 110
FORBIDDEN_KEYS = {"verified", "timestamp", "tags"}   # The owner verifies; Organize tags
_BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
_SKIP_DIRS = {".obsidian", ".trash", ".stversions", ".sync-backups", ".git", ".stfolder"}
_lock = threading.RLock()


class VaultError(Exception):
    """Vault missing / misconfigured -- shown to the owner as a friendly page."""


# ------------------------------------------------------------------ small io helpers
def _load(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def now_iso() -> str:
    return dt.datetime.now(TZ).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ vault location
def vault_root() -> str:
    p = os.environ.get("REMEMBER_VAULT_PATH", "").strip()
    if not p:
        p = (_load(CONFIG, {}) or {}).get("vault_path") or ""
    if not p:
        p = os.path.join(os.environ.get("OneDrive", ""), "Documents", "Obsidian", "DWA")
    p = os.path.normpath(os.path.expandvars(os.path.expanduser(p)))
    if not os.path.isdir(os.path.join(p, ".obsidian")):
        raise VaultError(f"Obsidian vault not found at {p} (no .obsidian folder). "
                         f"Set vault_path in {CONFIG}.")
    if not os.path.isdir(os.path.join(p, INBOX)):
        raise VaultError(f"The vault at {p} has no {INBOX} folder.")
    return p


def vault_inbox() -> str:
    return os.path.join(vault_root(), INBOX)


def vault_name(root: str | None = None) -> str:
    return os.path.basename(os.path.normpath(root or vault_root()))


def _walk_md(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith(".")]
        for f in files:
            if f.lower().endswith(".md"):
                yield os.path.join(d, f)


# ------------------------------------------------------------------ naming
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _BAD_CHARS.sub("", s or "")).strip(" .")


def safe_name(title: str, source: str | None) -> str:
    """'<title> (<source>)', <= MAX_NAME chars. The title is shortened, never the '(source)'."""
    t, src = _clean(title), _clean(source or "")
    if src and src.lower() not in t.lower() and len(src) <= 50:
        room = MAX_NAME - len(src) - 3
        if len(t) > room:
            t = t[:room].rsplit(" ", 1)[0].rstrip(" .,-")
        base = f"{t} ({src})"
    else:
        base = t[:MAX_NAME].rstrip(" .")
    return base or "Untitled"


def demote_headings(md_text: str) -> str:
    """Nest a section's own headings under the note's '## ' sections (# -> ###, ## -> ###...)."""
    out = []
    for line in (md_text or "").splitlines():
        m = re.match(r"^(#{1,5})\s", line)
        out.append(("#" * max(3, len(m.group(1)) + 1)) + line[len(m.group(1)):] if m else line)
    return "\n".join(out)


def unique_name(root: str, base: str) -> str:
    """Note names must be unique vault-wide (wikilinks resolve by name)."""
    taken = {os.path.splitext(os.path.basename(p))[0].lower() for p in _walk_md(root)}
    name, n = base, 2
    while name.lower() in taken:
        suffix = f" {n}"
        name = base[:MAX_NAME - len(suffix)].rstrip(" .") + suffix
        n += 1
    return name


# ------------------------------------------------------------------ frontmatter
def _y(v) -> str:
    """YAML scalar/flow value. Strings are JSON-quoted, which is valid YAML."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_y(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k}: {_y(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)


def _empty(v) -> bool:
    return v is None or (isinstance(v, (str, list, tuple, dict)) and len(v) == 0)


OWNER_PROPS_SKIP = {"Transcript", "Podcast Transcript", "Analysis", "Part Analysis", "Concept"}


def frontmatter(meta: dict) -> str:
    """Omits empty keys, except the owner's `see_also` / `my_note` (2026-09-30), which every summary note ends with
    (empty unless he has filled them in; a replaced note keeps his values). Transcripts don't get them.
    Kept in step by hand with the Daily Summary's platform\\digest-core\\vault_writer.py: consider the same change there."""
    lines, written = ["---"], set()
    for k, v in meta.items():
        if _empty(v):
            continue                       # omit unknowns; never leave a key empty
        if isinstance(v, (list, tuple)):
            v = [x for x in v if not _empty(x)]
            if not v:
                continue
        lines.append(f"{k}: {_y(v)}")
        written.add(k)
    if meta.get("type") not in OWNER_PROPS_SKIP:
        lines += [x for k, x in (("see_also", "see_also: []"), ("my_note", 'my_note: ""')) if k not in written]
    lines.append("---")
    return "\n".join(lines) + "\n"


def _split(text: str):
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end < 0:
        return None, text
    nl = text.find("\n", end + 4)
    return text[4:end], (text[nl + 1:] if nl >= 0 else "")


def parse_frontmatter(fm_text: str) -> dict:
    try:
        import yaml  # PyYAML (installed in the venv by the 2026-09-25 update)
        d = yaml.safe_load(fm_text)
        if not isinstance(d, dict):
            raise ValueError("frontmatter is not a mapping")
        return d
    except ImportError:
        pass
    d = {}
    for line in fm_text.splitlines():               # minimal fallback, our own format only
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            raise ValueError(f"unparseable frontmatter line: {line[:80]}")
        k, v = m.groups()
        try:
            d[k] = json.loads(v)
        except ValueError:
            d[k] = v
    return d


_RES_LINE = re.compile(r"^resource:\s*[\"']?(.*?)[\"']?\s*$", re.M)
_TYPE_LINE = re.compile(r"^type:\s*[\"']?(.*?)[\"']?\s*$", re.M)


def _head_meta(path):
    """(type, resource) from a note's frontmatter, reading only its start."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(6000)
    except OSError:
        return None, None
    fm, _ = _split(head)
    if fm is None:
        return None, None
    t = _TYPE_LINE.search(fm)
    r = _RES_LINE.search(fm)
    return (t.group(1).strip() if t else None), (r.group(1).strip() if r else None)


# ------------------------------------------------------------------ write / find / update
def write_note(kind: str, meta: dict, body: str, name_title: str, name_source: str | None) -> str:
    """Create a new note in Inbox/. Returns the vault-relative path (forward slashes)."""
    bad = FORBIDDEN_KEYS & set(meta)
    if bad:
        raise ValueError(f"refusing to write keys {sorted(bad)}")
    with _lock:
        root = vault_root()
        m = {"type": kind}
        m.update({k: v for k, v in meta.items() if k not in ("type", "generated")})
        m["generated"] = meta.get("generated") or {"by": GENERATED_BY, "at": now_iso()}
        text = frontmatter(m) + "\n" + body.strip() + "\n"
        name = unique_name(root, safe_name(name_title, name_source))
        path = os.path.join(root, INBOX, name + ".md")
        with open(path, "x", encoding="utf-8", newline="\n") as f:   # "x": never overwrite
            f.write(text)
        verify_note(path, kind)
        return os.path.relpath(path, root).replace("\\", "/")


def verify_note(path: str, kind: str | None = None) -> dict:
    with open(path, encoding="utf-8") as f:
        fm_text, _ = _split(f.read())
    if fm_text is None:
        raise ValueError(f"{path}: no frontmatter after write")
    d = parse_frontmatter(fm_text)
    if not d.get("type") or (kind and d.get("type") != kind):
        raise ValueError(f"{path}: frontmatter type check failed")
    return d


def locate(key: str, kind: str, resource: str | None) -> str | None:
    """Absolute path of an already-saved note: ledger path, else a vault search by resource."""
    root = vault_root()
    rec = (_load(LEDGER, {}) or {}).get(key) or {}
    if rec.get("path"):
        p = os.path.join(root, rec["path"])
        if os.path.exists(p):
            return p
    if resource:
        for p in _walk_md(root):
            t, r = _head_meta(p)
            if r == resource and t == kind:
                return p
    return None


def ledger_put(key: str, root: str, abs_path: str, has_detail: bool):
    d = _load(LEDGER, {}) or {}
    prev = d.get(key) or {}
    d[key] = {"path": os.path.relpath(abs_path, root).replace("\\", "/"),
              "saved_at": prev.get("saved_at") or now_iso(), "has_detail": bool(has_detail)}
    if prev.get("saved_at"):
        d[key]["updated_at"] = now_iso()
    _save(LEDGER, d)


def insert_section(path: str, heading: str, text: str, before: str | None = None) -> bool:
    """Add '## heading' to an existing note (before '## <before>' if present). False if already there."""
    with open(path, encoding="utf-8") as f:
        t = f.read()
    if re.search(rf"^##\s+{re.escape(heading)}\s*$", t, re.M):
        return False
    block = f"## {heading}\n\n{text.strip()}\n\n"
    m = re.search(rf"^##\s+{re.escape(before)}", t, re.M) if before else None
    if m:
        t = t[:m.start()] + block + t[m.start():]
    else:
        t = t.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"
    tmp = path + ".dd-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(t)
    os.replace(tmp, path)
    return True


def save(key: str, kind: str, resource: str, make_note, upgrade=None):
    """Idempotent save. make_note() -> (meta, body, name_title, name_source, has_detail).
    upgrade(abs_path) -> (changed: bool, has_detail: bool) for an existing note.
    Returns (status, vault_rel_path) with status 'saved' | 'updated' | 'exists'."""
    with _lock:
        root = vault_root()
        p = locate(key, kind, resource)
        if p:
            changed, has_detail = upgrade(p) if upgrade else (False, False)
            ledger_put(key, root, p, has_detail)
            return ("updated" if changed else "exists"), os.path.relpath(p, root).replace("\\", "/")
        meta, body, name_title, name_source, has_detail = make_note()
        rel = write_note(kind, meta, body, name_title, name_source)
        ledger_put(key, root, os.path.join(root, rel), has_detail)
        return "saved", rel


def obsidian_uri(rel: str, root: str | None = None) -> str:
    from urllib.parse import quote
    f = rel[:-3] if rel.lower().endswith(".md") else rel
    return f"obsidian://open?vault={quote(vault_name(root))}&file={quote(f, safe='')}"


# ------------------------------------------------------------------ text helpers
def fmt_hms(secs) -> str | None:
    try:
        s = int(round(float(secs)))
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def to_local_iso(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(TZ).replace(microsecond=0).isoformat()


def plain(md_text: str) -> str:
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md_text or "")
    t = re.sub(r"[*_`>#]+", "", t)
    return re.sub(r"[ \t]+", " ", t)


def first_sentence(md_text: str, limit: int = 300) -> str | None:
    """One-sentence description from a summary: skips labels like '**Summary**' and
    'Here is a summary of the podcast episode:'."""
    for line in plain(md_text).splitlines():
        s = line.strip().lstrip("-• ").strip()
        if not s or s.endswith(":") or len(s.split()) < 5:
            continue
        m = re.match(r"(.+?[.!?])(\s|$)", s)
        s = m.group(1) if m else s
        if len(s) > limit:
            s = s[:limit].rsplit(" ", 1)[0] + "…"
        return s
    return None


def escape_body(text: str) -> str:
    """Stop plain text lines from turning into headings or wikilinks."""
    out = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith("#"):
            line = line.replace("#", "\\#", 1)
        out.append(line.replace("[[", "[\u200b["))
    return "\n".join(out)


def long_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def cite_video(channel, title, duration, published, url) -> str:
    parts = [f"{channel}." if channel else "", f"\u201c{title}.\u201d" if title else "",
             f"YouTube video, {duration.lstrip('0:') if duration else ''}".rstrip(", ") + ".",
             f"{long_date(published)}." if long_date(published) else "", f"{url}." if url else ""]
    return " ".join(p for p in parts if p).replace(" .", ".")


def cite_podcast(hosts, title, show, season, episode, publisher, duration, published, url) -> str:
    who = (", ".join(hosts) + ", host" + ("s" if len(hosts) > 1 else "") + ".") if hosts else ""
    se = ", ".join(x for x in (f"season {season}" if season else "", f"episode {episode}" if episode else "") if x)
    show_part = f"{show}" + (f", {se}" if se else "") + "." if show else ""
    prod = f"Produced by {publisher}." if publisher and publisher not in (hosts or []) else ""
    audio = "Podcast audio" + (f", {duration.lstrip('0:')}" if duration else "") + "."
    parts = [who, f"\u201c{title}.\u201d" if title else "", show_part, prod, audio,
             f"{long_date(published)}." if long_date(published) else "",
             f"{url}." if url and url.startswith("http") else ""]
    return " ".join(p for p in parts if p)


class _H2M(HTMLParser):
    """Tiny HTML -> Markdown converter for cached detail pages (headings, lists, links, emphasis)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.href, self.lists = [], None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self.out.append("\n\n")
        elif tag in ("ul", "ol"):
            self.lists.append(tag)
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n" + "  " * (len(self.lists) - 1) + "- ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "br":
            self.out.append("  \n")
        elif tag == "a":
            self.href = a.get("href")
            self.out.append("[")

    def handle_endtag(self, tag):
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.out.append("\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a":
            self.out.append(f"]({self.href})" if self.href else "]")
            self.href = None

    def handle_data(self, data):
        self.out.append(re.sub(r"\s+", " ", data))


def html_to_md(fragment: str) -> str:
    p = _H2M()
    p.feed(fragment)
    t = "".join(p.out)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# ------------------------------------------------------------------ Remember additions (not in the Daily Summary copy)
def write_named(kind: str, meta: dict, body: str, name: str, folder: str | None = None) -> str:
    """Create <folder or Inbox>/<name>.md exactly (the caller has checked the name is unique).
    Never overwrites. Returns the vault-relative path."""
    bad = FORBIDDEN_KEYS & set(meta)
    if bad:
        raise ValueError(f"refusing to write keys {sorted(bad)}")
    with _lock:
        root = vault_root()
        m = {"type": kind}
        m.update({k: v for k, v in meta.items() if k not in ("type", "generated")})
        m["generated"] = meta.get("generated") or {"by": GENERATED_BY, "at": now_iso()}
        text = frontmatter(m) + "\n" + body.strip() + "\n"
        path = os.path.join(root, *(folder or INBOX).split("/"), name + ".md")
        with open(path, "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
        verify_note(path, kind)
        return os.path.relpath(path, root).replace("\\", "/")


_GEN_BY = re.compile(r"^generated:.*?\bby:\s*[\"']?([^\"',}\s]+)", re.M)


def find_by_resource(resource: str, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Every note whose frontmatter resource equals `resource`: [{path (vault-relative), type, by}]."""
    root = vault_root()
    out = []
    for p in _walk_md(root):
        t, r = _head_meta(p)
        if r != resource or (kinds and t not in kinds):
            continue
        by = None
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                fm, _ = _split(f.read(6000))
            m = _GEN_BY.search(fm or "")
            by = m.group(1) if m else None
        except OSError:
            pass
        out.append({"path": os.path.relpath(p, root).replace("\\", "/"), "type": t, "by": by})
    return out


def ledger_record(key: str, rec: dict) -> None:
    d = _load(LEDGER, {}) or {}
    d[key] = {**(d.get(key) or {}), **rec}
    _save(LEDGER, d)


def ledger_get(key: str) -> dict:
    return (_load(LEDGER, {}) or {}).get(key) or {}


# ------------------------------------------------------------------ generated-section fingerprint
# KEEP IN STEP: the Daily Summary (platform/digest-core/vault_writer.py) and the Vault Librarian
# (lib/remember/vault_writer.py) must compute this identically. The Daily Summary stores it when it
# saves a note (vault_saved.json and the Remember queue line); the Remember job recomputes it before
# replacing that note, and leaves the note alone if the owner has edited a generated section.
DS_GENERATED_HEADINGS = ("Summary", "Detailed summary", "Description (from YouTube)", "Transcript")


def split_body(body: str) -> list[tuple[str | None, str]]:
    """[(heading or None for text before the first '## '), section text without the heading line)]."""
    out, head, buf = [], None, []
    for line in (body or "").splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if head is not None or any(x.strip() for x in buf):
                out.append((head, "\n".join(buf)))
            head, buf = m.group(1), []
        else:
            buf.append(line)
    if head is not None or any(x.strip() for x in buf):
        out.append((head, "\n".join(buf)))
    return out


def generated_hash(body: str, headings=DS_GENERATED_HEADINGS) -> str:
    """sha256 of the generated sections (and any text before the first heading), whitespace-normalized."""
    import hashlib
    parts = []
    for h, t in split_body(body):
        if h is None or h in headings:
            norm = "\n".join(l.rstrip() for l in t.strip().splitlines() if l.strip())
            parts.append(f"## {h or ''}\n{norm}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def file_generated_hash(path: str, headings=DS_GENERATED_HEADINGS) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            _, body = _split(f.read())
        return generated_hash(body, headings)
    except OSError:
        return None
