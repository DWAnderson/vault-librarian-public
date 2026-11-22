"""Nightly clean-up of new Web Clipper notes (called by nightly.py; added 2026-09-28).

  fix_stubs(cfg, log, dry)    A clipping with almost no text (e.g. a Substack "X shared this with you"
                              share page) is re-fetched from the article's own URL with the url-to-vault
                              routine (linkjob.save_url). If a full note is written (or the article is
                              already in the vault), the stub is archived as its duplicate with
                              superseded_stub: true, so the morning email suggests deleting it. A paywalled
                              article goes to queue\\held.json ("stub-..." key, needs the laptop browser) and
                              the stub stays in Clippings, skipped by Organize until the laptop saves it.
                              Anything else: the stub is marked refetch_tried and left for Organize.
  fill_basics(cfg, log, dry)  Every clipping in Clippings/ without a `type` gets `type: Web Clipping`;
                              then normalize_props maps the Clipper's `source` -> resource and
                              `created` -> generated (by tool/obsidian-web-clipper); any clipping still
                              without `generated` gets one from the file date. Descriptions and tags are
                              left to the morning Organize proposal.
Both return (summary line, detail lines). The caller holds the vault lock.
"""
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import linkjob  # noqa: E402

STUB_WORDS = 120
STUB_MARKERS = re.compile(r"shared this with you|Follow on Substack|a (paid )?subscriber of .{2,80}, shared", re.I)
HELD = C.APP / "queue" / "held.json"


def _split(text):
    m = re.match(r"(?s)^---\s*\n(.*?)\n---\s*\n?(.*)$", text)
    return (m.group(1), m.group(2)) if m else ("", text)


def _fm(fm_text):
    import yaml
    try:
        d = yaml.safe_load(fm_text) or {}
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _words(body):
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", body)          # images
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)        # links -> text
    return len(re.findall(r"[A-Za-z]{2,}", body))


def is_stub(body):
    """A share-page stub or teaser by the nightly test (reclip.py uses the same one, 2026-11-12)."""
    n = _words(body)
    return n < STUB_WORDS or (n < 2 * STUB_WORDS and bool(STUB_MARKERS.search(body)))


def canonical(url):
    """Article URL for a share-page link: open.substack.com/pub/<pub>/p/<slug> -> <pub>.substack.com/p/<slug>;
    Substack-style /p/<slug> links lose their query string (utm_*, r=...)."""
    p = urlparse(url.strip())
    m = re.match(r"^/pub/([\w-]+)/p/([\w-]+)", p.path)
    if (p.hostname or "").lower() == "open.substack.com" and m:
        return f"https://{m.group(1)}.substack.com/p/{m.group(2)}"
    if re.match(r"^/p/[\w-]+/?$", p.path):
        return urlunparse((p.scheme or "https", p.netloc, p.path, "", "", ""))
    return url.strip()


def _clippings(vault):
    d = vault / "Clippings"
    return sorted(x for x in d.glob("*.md") if x.name.lower() not in ("index.md", "log.md")) if d.is_dir() else []


def _held():
    try:
        return json.loads(HELD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _apply(log, vault, work, name, items, dry, actor="process:vault-nightly", title="Nightly clipping clean-up"):
    if not items:
        return 0, ""
    pj = work / f"nightly-{name}.json"
    pj.write_text(json.dumps({"actor": actor, "log": title, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    return C.run(log, C.VL / "apply_plan.py", vault, pj, *(["--dry-run"] if dry else []))


def find_stubs(vault):
    """[(rel, url, words)] for Clippings notes that are share-page stubs worth re-fetching."""
    out, held_paths = [], {h.get("stub_path") for h in _held().values() if h.get("stub_path")}
    for p in _clippings(vault):
        rel = p.relative_to(vault).as_posix()
        if rel in held_paths:
            continue
        fm_text, body = _split(p.read_text(encoding="utf-8", errors="replace"))
        fm = _fm(fm_text)
        if fm.get("refetch_tried") or fm.get("not_a_duplicate"):
            continue
        url = next((fm.get(k) for k in ("resource", "source", "url") if isinstance(fm.get(k), str) and fm.get(k, "").startswith("http")), None)
        if url and is_stub(body):
            out.append((rel, url, _words(body)))
    return out


def fix_stubs(cfg, log, dry):
    vault = Path(cfg["vault_path"])
    work = Path(cfg["local_root"]) / "work"
    work.mkdir(parents=True, exist_ok=True)
    stubs = find_stubs(vault)
    if not stubs:
        return "no stub clippings", []
    lines, archive, tried, queued = [], [], [], {}
    import reclip
    clips = reclip._vault_resources(vault)
    for rel, url, n in stubs:
        curl = canonical(url)
        name = rel.rsplit("/", 1)[-1][:-3]
        # 2026-11-12: The owner's own clip of the article is already in the vault -> archive the stub, don't hold it again
        clip = reclip._find_clip(clips, curl, rel) or reclip._find_clip(clips, url, rel)
        if clip:
            keep = clip[:-3] if clip.endswith(".md") else clip
            archive.append({"path": rel, "move_to": "Archives", "add_generated": True, "generated_by": "tool/obsidian-web-clipper",
                            "set": {"type": "Web Clipping", "status": "deprecated", "duplicate_of": f"[[{keep}]]",
                                    "archived_from": "Clippings", "superseded_stub": True}})
            lines.append(f"{name}: your clip is already in the vault as {keep}; stub archived")
            continue
        res = linkjob.save_url(cfg, log, curl, "", "clipper-stub", dry)
        if res.get("ok") and res.get("dry"):
            lines.append(f"{name}: would re-fetch {curl} ({res.get('would')})")
        elif res.get("ok"):
            keep = str(res.get("note") or "")
            keep = keep[:-3] if keep.endswith(".md") else keep
            archive.append({"path": rel, "move_to": "Archives", "add_generated": True, "generated_by": "tool/obsidian-web-clipper",
                            "set": {"type": "Web Clipping", "status": "deprecated", "duplicate_of": f"[[{keep}]]",
                                    "archived_from": "Clippings", "superseded_stub": True}})
            lines.append(f"{name}: {'already in the vault as' if res.get('duplicate') else 'full text saved as'} {keep}; stub archived")
        elif res.get("needs") == "browser":
            key = "stub-" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]
            queued[key] = {"subject": "(Web Clipper stub)", "url": curl, "title": res.get("title") or name,
                           "why": res.get("why"), "needs": "browser", "status": "held", "doug_note": "",
                           "stub_path": rel, "at": C.now(cfg).isoformat(timespec="seconds")}
            lines.append(f"{name}: paywalled - queued for the laptop browser")
        else:
            tried.append({"path": rel, "set": {"refetch_tried": True}})
            lines.append(f"{name}: could not re-fetch ({str(res.get('why'))[:120]}); left for Organize")
    rc1, out1 = _apply(log, vault, work, "stub-archive", archive, dry)
    rc2, out2 = _apply(log, vault, work, "stub-tried", tried, dry)
    if rc1 or rc2:
        lines.append("apply_plan problem: " + ((out1 if rc1 else out2).strip().splitlines() or ["?"])[-1])
    if queued and not dry:
        held = _held()
        held.update(queued)
        HELD.write_text(json.dumps(held, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = (f"{len(stubs)} stub clipping(s): {len(archive)} replaced by the full article, "
               f"{len(queued)} queued for the laptop browser, {len(tried)} left for Organize")
    return summary, lines


def fill_basics(cfg, log, dry):
    vault = Path(cfg["vault_path"])
    work = Path(cfg["local_root"]) / "work"
    work.mkdir(parents=True, exist_ok=True)
    notype = []
    for p in _clippings(vault):
        fm = _fm(_split(p.read_text(encoding="utf-8", errors="replace"))[0])
        if not fm.get("type") or not fm.get("generated") or not fm.get("resource"):
            notype.append((p.relative_to(vault).as_posix(), fm))
    if not notype:
        return "all clippings have type, resource and generated", []
    paths = [r for r, _ in notype]
    rc, out = _apply(log, vault, work, "clip-type", [{"path": r, "set": {"type": "Web Clipping"}} for r, fm in notype if not fm.get("type")], dry)
    lines = [f"{r.rsplit('/', 1)[-1][:-3]}: type Web Clipping" for r, fm in notype if not fm.get("type")]
    if dry:   # the type isn't really set in a dry run, so normalize_props can't be previewed faithfully
        return f"{len(notype)} clipping(s) would get missing basics", lines
    plan = work / "nightly-clip-props-all.json"
    rc, out = C.run(log, C.VL / "normalize_props.py", vault, "--plan", plan, "--actor", "tool/obsidian-web-clipper")
    items = []
    if not rc and plan.exists():
        items = [it for it in json.loads(plan.read_text(encoding="utf-8")).get("items", []) if it.get("path") in paths]
    rc, out = _apply(log, vault, work, "clip-props", items, dry, actor="tool/obsidian-web-clipper")
    lines += [f"{it['path'].rsplit('/', 1)[-1][:-3]}: properties mapped (" + ", ".join(sorted(set(it.get('set', {})) | set(it.get('unset', [])))) + ")"
              for it in items]
    rc, out = _apply(log, vault, work, "clip-generated",
                     [{"path": r, "add_generated": True, "generated_by": "tool/obsidian-web-clipper"} for r in paths], dry)
    return f"{len(notype)} clipping(s) given missing basics (type, resource, generated)", lines
