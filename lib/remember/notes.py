"""Build the two Remember notes (plan section 5) from a cached result:
    <name>.md               type: Video Summary   (detailed summary)
    <name> - Transcript.md  type: Transcript      (cleaned transcript, timestamp links)
They link to each other with wikilinks (allowed inside the vault). Returns text only;
jobs\\remember.py decides whether to write them to the vault (live) or to a preview folder (dry run).
"""
from __future__ import annotations

import datetime as dt

from . import vault_writer as VW

TZ = VW.TZ


def _hms(t) -> str:
    t = int(t or 0)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ts(vid: str, t) -> str:
    try:
        t = int(t)
    except (TypeError, ValueError):
        return ""
    return f"[{_hms(t)}](https://youtu.be/{vid}?t={t})"


def _txt(s) -> str:
    if isinstance(s, (dict, list)):
        from .summarizer import as_text
        s = as_text(s)
    return VW.escape_body(" ".join(str(s or "").split()))


def _bullets(items, fmt) -> str:
    lines = [fmt(i) for i in items if i]
    return "\n".join(f"- {l}" for l in lines if l.strip())


def names(meta: dict, root: str | None) -> tuple[str, str]:
    """(summary note name, transcript note name), unique across the vault when root is given."""
    base = VW.safe_name(meta["title"] or meta["video_id"], meta.get("channel"))
    # "<title> (<channel>) (Summary)" / "(Transcript)": names that tell them apart from the video (2026-10-10)
    name = VW.unique_name(root, f"{base} (Summary)") if root else f"{base} (Summary)"
    tname = f"{base} (Transcript)"
    if root:
        tname = VW.unique_name(root, tname)
    return name, tname


def build(meta: dict, rec: dict, name: str, tname: str) -> tuple[dict, str, dict, str]:
    """-> (summary_meta, summary_body, transcript_meta, transcript_body). rec = cached result."""
    vid = meta["video_id"]
    url = f"https://www.youtube.com/watch?v={vid}"
    d = rec["summary"]["data"]
    dur = VW.fmt_hms(meta["duration_sec"])
    published = VW.to_local_iso(meta.get("published"))
    channel = meta.get("channel")
    models = ", ".join(dict.fromkeys(m for m in rec["summary"]["models"] if m))
    lang = d.get("language") or meta.get("language")
    now = VW.now_iso()
    common = {
        "resource": url,
        "author": [channel] if channel else None,
        "publication": "YouTube",
        "published": published,
        "accessed": dt.datetime.now(TZ).date().isoformat(),
        "work_type": "video",
        "duration": dur,
        "platform": "YouTube",
        "language": lang,
        "cite_as": VW.cite_video(channel, meta["title"], dur, published, url),
    }
    smeta = {"title": meta["title"],
             "description": _txt(d.get("one_line")) or VW.first_sentence(d.get("overview", "")),
             **common, "summary_by": models, "content": "detailed summary",
             "generated": {"by": VW.GENERATED_BY, "at": now}}

    b = [f"## Overview\n\n{_txt(d['overview'])}"]
    secs = []
    for s in d["sections"]:
        head = f"### {ts(vid, s.get('start'))} {_txt(s.get('heading'))}".rstrip()
        secs.append(f"{head}\n\n{_txt(s.get('summary'))}")
    b.append("## Detailed summary\n\n" + "\n\n".join(secs))
    if d["claims"]:
        b.append("## Key arguments and claims\n\n" + _bullets(d["claims"], lambda c: (
            f"**{_txt(c.get('claim'))}** — evidence: {_txt(c.get('evidence')) or 'none given'}"
            + (f" ({ts(vid, c.get('t'))})" if ts(vid, c.get('t')) else "")) if isinstance(c, dict) else _txt(c)))
    if d["figures"]:
        b.append("## Figures, data and quotes\n\n" + _bullets(d["figures"], lambda f: (
            _txt(f.get("item")) + (f" ({ts(vid, f.get('t'))})" if ts(vid, f.get('t')) else ""))
            if isinstance(f, dict) else _txt(f)))
    if d["on_screen"]:
        b.append("## On screen\n\n" + _bullets(d["on_screen"], lambda f: (
            _txt(f.get("item")) + (f" ({ts(vid, f.get('t'))})" if ts(vid, f.get('t')) else ""))
            if isinstance(f, dict) else _txt(f)))
    ment_urls = set()

    def _m(m):
        if not isinstance(m, dict):
            return _txt(m)
        u = str(m.get("url") or "").strip()
        if u.startswith("http"):
            ment_urls.add(u)
        kind = f" ({_txt(m.get('kind'))})" if m.get("kind") else ""
        note = f" — {_txt(m.get('note'))}" if m.get("note") else ""
        link = f" [link]({u})" if u.startswith("http") else ""
        return f"**{_txt(m.get('name'))}**{kind}{note}{link}"
    people = _bullets(d["mentions"], _m)
    extra = [u for u in meta.get("links", []) if u not in ment_urls][:15]
    if people or extra:
        block = people
        if extra:
            block += ("\n\n" if block else "") + "Other links in the description:\n\n" + "\n".join(f"- <{u}>" for u in extra)
        b.append("## People, works and sources mentioned\n\n" + block)
    if d["takeaways"]:
        b.append("## Takeaways\n\n" + _bullets(d["takeaways"], _txt))
    b.append("## Caveats and disputed points\n\n" + (_bullets(d["caveats"], _txt) or "None noted."))
    tr = rec["transcript"]
    b.append(f"## Transcript\n\n[[{tname}]] ({tr['how']})")
    b.append(f"[Watch on YouTube]({url})")
    sbody = "\n\n".join(b)

    tmeta = {"title": f"{meta['title']} (transcript)",
             "description": f"Transcript of “{meta['title']}”" + (f" by {channel}." if channel else "."),
             **common, "content": "transcript", "captured_via": tr["source"],
             "generated": {"by": VW.GENERATED_BY, "at": now}}
    paras = "\n\n".join(f"{ts(vid, p['t'])} {_txt(p['text'])}" for p in tr["paragraphs"])
    tbody = f"Summary: [[{name}]]\n\n## Transcript\n\n{paras}\n\n[Watch on YouTube]({url})"
    return smeta, sbody, tmeta, tbody


def render(kind: str, meta: dict, body: str) -> str:
    m = {"type": kind, **meta}
    return VW.frontmatter(m) + "\n" + body.strip() + "\n"


def word_count(body: str) -> int:
    return len(VW.plain(body).split())
