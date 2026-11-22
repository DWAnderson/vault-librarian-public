"""Captions for the Remember job, via youtube-transcript-api (yt-dlp subtitle downloads get
HTTP 429 from this PC -- Phase 1 finding, 2026-09-26).

fetch() prefers a MANUAL transcript over an auto-generated one: English first, then the
video's own language, then anything. Returns None when the video has no captions -- the
Gemini call then supplies the transcript itself.
"""
from __future__ import annotations


def fetch(video_id: str, lang: str | None = None) -> dict | None:
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    try:
        tl = api.list(video_id)
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        if name in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable", "VideoUnplayable"):
            return None
        raise
    prefs = ["en", "en-US", "en-GB"] + ([lang, (lang or "").split("-")[0]] if lang else [])
    prefs = [p for p in dict.fromkeys(prefs) if p]
    tr = None
    for finder in ("find_manually_created_transcript", "find_generated_transcript"):
        try:
            tr = getattr(tl, finder)(prefs)
            break
        except Exception:  # noqa: BLE001
            continue
    if tr is None:
        allt = list(tl)
        if not allt:
            return None
        allt.sort(key=lambda t: t.is_generated)          # manual first
        tr = allt[0]
    got = tr.fetch()
    snippets = [{"t": round(s.start, 2), "d": round(getattr(s, "duration", 0) or 0, 2),
                 "text": " ".join((s.text or "").split())} for s in got.snippets]
    snippets = [s for s in snippets if s["text"] and s["text"] not in ("[Music]", "[Applause]")]
    return {"language": tr.language_code, "generated": bool(tr.is_generated),
            "source": "youtube-auto-captions" if tr.is_generated else "youtube-captions",
            "snippets": snippets, "words": sum(len(s["text"].split()) for s in snippets)}


def paragraphs(snippets: list[dict], every: int = 60) -> list[dict]:
    """Group caption snippets into ~every-second paragraphs: [{t, text}]. No re-wording."""
    out, cur, start = [], [], None
    for s in snippets:
        if start is None:
            start = s["t"]
        cur.append(s["text"])
        ends_sentence = s["text"].rstrip().endswith((".", "?", "!", '"'))
        if s["t"] - start >= every and (ends_sentence or s["t"] - start >= every * 1.5):
            out.append({"t": int(start), "text": " ".join(cur)})
            cur, start = [], None
    if cur:
        out.append({"t": int(start or 0), "text": " ".join(cur)})
    return out


def as_prompt_text(snippets: list[dict], max_words: int = 40000) -> str:
    """'[mm:ss] text' lines for the Gemini prompt, grouped ~30 s, truncated at max_words."""
    lines, words = [], 0
    for p in paragraphs(snippets, every=30):
        n = len(p["text"].split())
        if words + n > max_words:
            lines.append("[... transcript truncated ...]")
            break
        m, s = divmod(p["t"], 60)
        h, m = divmod(m, 60)
        stamp = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        lines.append(f"[{stamp}] {p['text']}")
        words += n
    return "\n".join(lines)
