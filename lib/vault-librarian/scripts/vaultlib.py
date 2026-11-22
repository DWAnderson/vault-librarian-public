"""Shared helpers for vault-librarian scripts: vault walking, frontmatter, wikilink resolution."""
import os
import re

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SKIP_DIRS = {".obsidian", ".trash", ".git", "_snapshots", "node_modules", "Templates", "_templates"}
RESERVED = {"index.md", "log.md"}


def owner():
    """The vault owner, from environment variables (the app sets them from its config owner block; on another PC
    set them once as user variables, e.g. setx VAULT_OWNER_NAME Sam):
      VAULT_OWNER_NAME        first name, '' for none
      VAULT_OWNER_ID          generated.by of the owner's own notes (default human:owner)
      VAULT_OWNER_NOTE_LABEL  title of the owner's note callout on an item (default "<Name>'s note" or "My note")"""
    name = os.environ.get("VAULT_OWNER_NAME", "").strip()
    return {"name": name, "id": os.environ.get("VAULT_OWNER_ID", "").strip() or "human:owner",
            "note_label": os.environ.get("VAULT_OWNER_NOTE_LABEL", "").strip() or (f"{name}'s note" if name else "My note")}

# Titles that PDF/Word tools write into a file's metadata and that say nothing about the work
# (2026-10-01: "INTRODUCTION" matched "Introduction to Obsidian Web Clipper"; three Engage articles all
# titled "11Engage3(November2010).indd" were asked about as duplicates). Duplicate checks skip them.
_JUNK_TITLES = {"introduction", "untitled", "untitled document", "contents", "table of contents", "no job name",
                "document", "title", "abstract", "preface", "foreword", "chapter", "article", "paper", "draft",
                "front matter", "cover", "book", "pdf", "slide 1", "presentation", "powerpoint presentation",
                "layout 1", "main", "user", "unknown", "word document", "new document", "print", "scan"}


def junk_title(t):
    """True if a title is empty or a meaningless metadata title ("INTRODUCTION", "x.indd", "No Job Name 2",
    "Microsoft Word - draft3.doc", "OP-ROFF210038 1..42", "CD8332.531-574")."""
    s = re.sub(r"\s+", " ", str(t or "")).strip().lower().rstrip(". ")
    if not s:
        return True
    s2 = re.sub(r"\s*\(?\d+\)?$", "", s)          # "no job name 2", "untitled (3)"
    if s in _JUNK_TITLES or s2 in _JUNK_TITLES:
        return True
    if re.search(r"\.(indd|dvi|docx?|pdf|qxd|qxp|tex|rtf|wpd|pmd|p65|ps|eps|pages)\b", s):
        return True
    if re.match(r"(microsoft (word|powerpoint|excel)|untitled)\s*-", s):
        return True
    if re.search(r"\d+\.\.\d+$", s):                  # page-range junk from journal typesetting: "1..42"
        return True
    return not re.search(r"[a-z]{3,}", s)               # no real word at all: "00-9389-2 fm", "cd8332.531-574"

# The owner's housekeeping folder (2026-09-29). Archives, attachments, Templates, Topics,
# _claude-tags.md and Vault Review.base may live either at the vault root or inside it;
# vault_path() finds whichever exists, so the scripts work before and after the move.
# index.md, log.md and CLAUDE.md always stay at the root (OKF bundle root / Claude).
INTERNAL_DIR = "_Obsidian Internal"
INTERNAL_ITEMS = ("Archives", "attachments", "Templates", "Topics", "_claude-tags.md", "Vault Review.base")


def _find_ci(parent, name):
    """Actual on-disk name of `name` in `parent`, case-insensitive; None if absent."""
    try:
        for n in os.listdir(parent):
            if n.lower() == name.lower():
                return n
    except OSError:
        pass
    return None


def vault_rel(vault, name):
    """Vault-relative path (forward slashes) of an internal folder or file.

    Uses `_Obsidian Internal/<name>` when it exists there, else the root copy if it
    exists; when neither exists, the new location if `_Obsidian Internal` exists, else the root.
    Matching is case-insensitive (Windows: "Attachments" == "attachments").
    """
    idir = _find_ci(vault, INTERNAL_DIR)
    if idir:
        hit = _find_ci(os.path.join(vault, idir), name)
        if hit:
            return f"{idir}/{hit}"
    hit = _find_ci(vault, name)
    if hit:
        return hit
    return f"{idir}/{name}" if idir else name


def vault_path(vault, name):
    """Absolute path for vault_rel(vault, name)."""
    return os.path.join(vault, *vault_rel(vault, name).split("/"))


def is_archived(rel):
    """True for a note in Archives/, at the root or in the housekeeping folder."""
    parts = rel.replace("\\", "/").split("/")
    if parts[0].lower() == INTERNAL_DIR.lower():
        parts = parts[1:]
    return len(parts) > 1 and parts[0].lower() == "archives"


def map_internal(vault, folder):
    """Point a plan's move_to at the real location of an internal folder.

    "Archives" or "Archives/x" -> "_Obsidian Internal/Archives[/x]" once that is where Archives
    lives; other folders are returned unchanged.
    """
    f = folder.replace("\\", "/").strip("/")
    head, _, rest = f.partition("/")
    for name in INTERNAL_ITEMS:
        if head.lower() == name.lower():
            real = vault_rel(vault, name)
            return real + ("/" + rest if rest else "")
    return f


def top_folder(rel):
    """Top-level folder of a note for grouping, looking through the housekeeping folder."""
    parts = rel.split("/")
    if parts[0].lower() == INTERNAL_DIR.lower() and len(parts) > 2:
        return parts[1]
    return parts[0] if len(parts) > 1 else "(vault root)"

FENCE_RE = re.compile(r"(?ms)^(```|~~~).*?^\1[^\n]*$")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def walk(vault):
    """Yield (relpath with forward slashes, abspath) for every file in the vault."""
    for root, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            ab = os.path.join(root, f)
            yield os.path.relpath(ab, vault).replace(os.sep, "/"), ab


def split_frontmatter(text):
    """Return (frontmatter dict or None, error string or None, body)."""
    if not text.startswith("---"):
        return None, None, text
    m = re.match(r"(?s)^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)", text)
    if not m:
        return None, "frontmatter not closed", text
    if yaml is None:
        raise SystemExit("PyYAML is required: pip install pyyaml")
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        return None, f"YAML error: {str(e).splitlines()[0]}", text[m.end():]
    if not isinstance(data, dict):
        return None, "frontmatter is not a mapping", text[m.end():]
    return data, None, text[m.end():]


def mask_code(text):
    """Blank out fenced and inline code so links inside code are ignored (offsets preserved)."""
    blank = lambda m: re.sub(r"[^\n]", " ", m.group(0))
    return INLINE_CODE_RE.sub(blank, FENCE_RE.sub(blank, text))


class Resolver:
    """Resolve Obsidian wikilink targets the way Obsidian does (by name, or by partial path)."""

    def __init__(self, relpaths):
        self.paths = list(relpaths)
        self.pathset = set(self.paths)
        self.by_name = {}
        for p in self.paths:
            base = p.rsplit("/", 1)[-1]
            keys = {base.lower()}
            if base.lower().endswith(".md"):
                keys.add(base[:-3].lower())
            for k in keys:
                self.by_name.setdefault(k, []).append(p)

    def add_aliases(self, vault):
        """Let [[alias]] resolve to the note whose frontmatter lists that alias (as Obsidian does)."""
        for rel in self.paths:
            if not rel.lower().endswith(".md"):
                continue
            try:
                with open(os.path.join(vault, *rel.split("/")), encoding="utf-8", errors="replace") as f:
                    head = f.read(4096)
                fm, _, _ = split_frontmatter(head if head.count("---") >= 2 else "")
            except Exception:
                continue
            al = (fm or {}).get("aliases") or []
            for a in (al if isinstance(al, list) else [al]):
                if isinstance(a, str) and a.strip() and a.strip().lower() not in self.by_name:
                    self.by_name[a.strip().lower()] = [rel]
        return self

    def duplicate_notes(self):
        """Note names (without .md) shared by more than one .md file."""
        return {k: v for k, v in self.by_name.items()
                if len(v) > 1 and not k.endswith(".md") and k + ".md" not in RESERVED
                and all(p.lower().endswith(".md") for p in v)}

    def resolve(self, target):
        """Return (path or None, status) where status is ok | missing | ambiguous."""
        t = target.strip().replace("\\", "/").lstrip("/")
        if not t:
            return None, "missing"
        tl = t.lower()
        if "/" in t:
            cands = [p for p in self.paths
                     if p.lower() in (tl, tl + ".md")
                     or p.lower().endswith("/" + tl) or p.lower().endswith("/" + tl + ".md")]
        else:
            cands = self.by_name.get(tl, [])
        if not cands:
            return None, "missing"
        if len(cands) > 1:
            return sorted(cands, key=len)[0], "ambiguous"
        return cands[0], "ok"


def parse_wikilink(inner):
    """Split 'Target#Heading|Alias' into (target, heading, alias)."""
    target, alias = (inner.split("|", 1) + [None])[:2]
    target, heading = (target.split("#", 1) + [None])[:2]
    return target.strip(), (heading or "").strip() or None, (alias or "").strip() or None
