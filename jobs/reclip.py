"""Sites Claude's browsers refuse to open (2026-10-06; first: nytimes.com). Links from them can't be saved by
this PC or by the laptop's "process the vault queue", so the owner re-clips them himself with the Obsidian Web Clipper.

  blocked(cfg, url)   -> True when the link's host is in config `blocked_hosts`
  check(cfg, log)     -> marks held links "saved" once a vault note with the same address exists (his clip), so the
                         Remember email clears on the next intake run. Returns report rows.
The laptop's find_partials.py keeps its own BLOCKED_HOSTS list (separate apps, duplicated on purpose): change both.

2026-10-12 (the owner): check() now covers EVERY held link that needs a signed-in browser, not just blocked sites, so he can
clear those cards while answering the morning questions on the laptop: open the link in his own (signed-in) browser,
clip it with the Web Clipper, and the next intake run (every 30 min) matches the clip by address and clears the card.
A clip only counts when it has real text (MIN_WORDS), so a teaser/stub clip of the same address doesn't clear it.
A note he typed on the card (doug_note) is copied into the clip's empty my_note.

2026-11-12: a Substack post clipped on the newsletter's own domain matches the <pub>.substack.com link that was
held (and vice versa); the stub itself never counts as the clip, and stubs are judged by clippings.is_stub, the
same test the nightly uses, so a cleared stub isn't held again the next night.
"""
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402


def hosts(cfg):
    return [h.lower() for h in (cfg.get("blocked_hosts") or ["nytimes.com"])]


def host(url):
    h = (urlsplit(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def blocked(cfg, url):
    h = host(url)
    return bool(h) and any(h == b or h.endswith("." + b) for b in hosts(cfg))


def _norm(u):
    u = re.sub(r"[?#].*$", "", (u or "").strip().lower()).rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", u)


MIN_WORDS = 150      # a clip shorter than this (paywall teaser, share-page stub) doesn't count as the article


def _body_words(head_and_body):
    m = re.match(r"(?s)^---\s*\n.*?\n---\s*\n?(.*)$", head_and_body)
    return len(re.findall(r"\w+", m.group(1) if m else head_and_body))


def _substack_slug(norm):
    """'p/<slug>' for a Substack-style post address. A Substack on its own domain serves the same post at
    <pub>.substack.com/p/<slug> and <domain>/p/<slug>, and the clip records whichever the browser ended on."""
    m = re.search(r"^[^/]+/p/([\w-]{8,})$", norm)
    return "p/" + m.group(1) if m else None


def _vault_resources(vault):
    """{normalized address: [(words, note)]} for notes with real text; Web Clipper notes still say `source:`
    until nightly. 2026-11-12: a note the nightly stub test calls a stub doesn't count either - before this a
    stub of 150+ words (counting its link addresses) was matched as its own re-clip, so its card came back
    every morning."""
    import clippings
    out = {}
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "Archives"]
        for f in files:
            if not f.endswith(".md"):
                continue
            p = os.path.join(root, f)
            try:
                text = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            # 2026-10-15 fix: the note starts with "---", so the front matter is head[0] (minus that line), not
            # head[1] (the body) - before this no clip was ever matched and held cards never cleared
            m0 = re.match(r"(?s)^---\s*\n(.*?)\n---", text[:6000])
            fm = m0.group(1) if m0 else ""
            m = re.search(r"(?m)^(?:resource|source|url):\s*[\"']?(https?://[^\"'\n]+)", fm)
            n = _body_words(text)
            if m and n >= MIN_WORDS and not clippings.is_stub(clippings._split(text)[1]):
                out.setdefault(_norm(m.group(1)), []).append((n, os.path.relpath(p, vault).replace(os.sep, "/")))
    return out


def _find_clip(res, url, stub_path):
    """The note that holds the article at `url`: same address, else (Substack) the same post on the other
    domain. Never the stub that put it on hold; a filed note before one still in Clippings, then the longest."""
    norm = _norm(url)
    cands = list(res.get(norm, []))
    slug = _substack_slug(norm)
    if not cands and slug:
        cands = [c for k, v in res.items() if _substack_slug(k) == slug
                 and ("substack.com/" in k or "substack.com/" in norm) for c in v]
    cands = [c for c in cands if c[1] != stub_path]
    if not cands:
        return None
    return max(cands, key=lambda c: (not c[1].startswith("Clippings/"), c[0]))[1]


def check(cfg, log=print):
    hq = C.APP / "queue" / "held.json"
    try:
        held = json.loads(hq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    want = {k: h for k, h in held.items() if isinstance(h, dict) and h.get("status") == "held"
            and h.get("url") and (blocked(cfg, h["url"]) or h.get("needs") == "browser")}
    if not want:
        return []
    res = _vault_resources(cfg["vault_path"])
    rows, done = [], {}
    for k, h in want.items():
        note = _find_clip(res, h["url"], h.get("stub_path"))
        if note:
            done[k] = note
    if not done:
        return []
    held = json.loads(hq.read_text(encoding="utf-8"))          # re-read just before writing (the laptop writes it too)
    now = C.now(cfg).isoformat(timespec="seconds")
    for k, note in done.items():
        if k in held and held[k].get("status") == "held":
            held[k].update(status="saved", note=note, saved_at=now, saved_by="your Web Clipper capture")
            rows.append((held[k].get("title") or held[k]["url"], "re-clip", f"your clip found: {note}"))
            if (held[k].get("doug_note") or "").strip():
                _carry_note(cfg, log, note, held[k]["doug_note"].strip())
            gone = _drop_stub(cfg, log, held[k].get("stub_path"), note)
            if gone:
                rows.append((held[k].get("title") or held[k]["url"], "re-clip", f"old stub removed: {gone}"))
    tmp = hq.with_suffix(".tmp")
    tmp.write_text(json.dumps(held, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(hq)
    log(f"reclip: {len(rows)} held link(s) matched to your clips")
    return rows


def _drop_stub(cfg, log, stub_rel, clip_rel):
    """The short share-page stub that put the link on hold goes to the vault's .trash once the owner's own clip of the
    same address is found (2026-10-13: otherwise it was archived overnight as a duplicate and asked about the next
    morning). Only a stub under MIN_WORDS, never the clip itself, and only one the owner hasn't written a note on."""
    if not stub_rel or stub_rel == clip_rel:
        return None
    vault = Path(cfg["vault_path"])
    p = vault / stub_rel
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"(?m)^my_note:\s*(.*)$", text[:4000].split("\n---", 1)[0])
    import clippings      # the nightly stub test, so a stub it would hold again is removed (2026-11-12)
    if not (_body_words(text) < MIN_WORDS or clippings.is_stub(clippings._split(text)[1])) \
            or (m and m.group(1).strip().strip("\"'")):
        return None
    dst = vault / ".trash" / f"{C.now(cfg):%Y-%m-%d} superseded stubs" / p.name
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(p, dst)
    except OSError as e:
        log(f"reclip: stub {stub_rel} not removed: {e}")
        return None
    try:
        lp = vault / "log.md"
        lp.write_text(lp.read_text(encoding="utf-8").rstrip() + f"\n\n## {C.now(cfg).date().isoformat()}\n\n"
                      f"**Your Web Clipper capture replaced a stub**\n\n- Moved `{stub_rel}` to `.trash/` "
                      f"(superseded by `{clip_rel}`)\n", encoding="utf-8")
    except OSError:
        pass
    log(f"reclip: stub {stub_rel} moved to .trash (your clip: {clip_rel})")
    return stub_rel


def _carry_note(cfg, log, rel, text):
    """The owner's note from the card goes into the clip's my_note when that is empty (best effort)."""
    try:
        vault = Path(cfg["vault_path"])
        head = (vault / rel).read_text(encoding="utf-8", errors="replace")[:4000].split("\n---", 1)[0]
        m = re.search(r"(?m)^my_note:\s*(.*)$", head)
        if m and m.group(1).strip().strip("\"'"):
            return
        work = Path(cfg["local_root"]) / "work"
        work.mkdir(parents=True, exist_ok=True)
        pj = work / "reclip-my-note.json"
        pj.write_text(json.dumps({"actor": "claude/reclip", "log": "Your note from the morning questions",
                                  "items": [{"path": rel, "set": {"my_note": text[:500]}}]}, ensure_ascii=False), encoding="utf-8")
        C.run(log, C.VL / "apply_plan.py", vault, pj)
    except Exception as e:  # noqa: BLE001
        log(f"reclip: note not copied to {rel}: {e}")
