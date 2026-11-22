"""Gemini work for the Remember job: one call that WATCHES the video (URL) with the description,
chapters and captions as context and returns structured JSON (plan section 4-5); long videos in
time-offset segments plus one text-only merge call; and a text-only Flash-Lite pass that
punctuates auto-generated captions for the Transcript note.

The video is charged to the free-tier video allowance once per video (all segments together
add up to its length); text-only calls use no video allowance.
"""
from __future__ import annotations

import json
import re

from google.genai import types

from . import gemini_pool as G
from . import transcript as T

SEGMENT_SECONDS = 3600          # videos longer than MAX_SINGLE are processed in 1 h segments
MAX_SINGLE_SECONDS = 7200       # plan: over 2 h -> segments
MEDIUM_RES_MAX_SECONDS = 2400   # "auto" resolution: MEDIUM up to 40 min (slides/code legible), LOW above
CLEAN_CHUNK_SECONDS = 900       # captions punctuated 15 min at a time
MAX_OUT = 32768


class SummaryError(Exception):
    pass


def as_text(x) -> str:
    """A list item that should be a string: dicts/lists from the model are reduced to their text."""
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        vals = [as_text(v) for v in x.values() if isinstance(v, (str, dict, list))]
        return " ".join(v for v in vals if v).strip()
    if isinstance(x, list):
        return " ".join(as_text(v) for v in x).strip()
    return str(x or "").strip()


def target_words(seconds: int) -> int:
    """Length asked for in the prompt. Gemini writes well past it, and the owner prefers the longer result
    (2026-09-26), so leave this as it is unless he asks for shorter summaries."""
    return max(600, round(175 * seconds / 600))          # 150-200 words per 10 min, min ~600


def _res(name: str):
    return {"low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
            "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH}[name]


def pick_resolution(setting: str, seconds: int) -> str:
    if setting in ("low", "medium", "high"):
        return setting
    return "medium" if seconds <= MEDIUM_RES_MAX_SECONDS else "low"


def _config(res: str | None, max_out: int = MAX_OUT):
    kw = {"temperature": 0.3, "response_mime_type": "application/json", "max_output_tokens": max_out}
    if res:
        kw["media_resolution"] = _res(res)
    return types.GenerateContentConfig(**kw)


def _hms(s: int) -> str:
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_json(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


_KEYS_LIST = ("sections", "claims", "figures", "on_screen", "mentions", "takeaways", "caveats")

PROMPT = """You are preparing a detailed reference note about a YouTube video for a personal knowledge vault. \
The reader will rely on this note instead of watching, so it must carry the real substance.
Watch the whole video (picture and sound). Use the description, chapters and captions below as context; \
captions can contain recognition errors -- trust what you see and hear.
{segment_rule}
Return ONE JSON object and nothing else, with exactly these keys:
  "overview": one paragraph (80-150 words): the thesis or main point, and why it matters.
  "one_line": one sentence (at most 30 words) describing the video, for a catalogue.
  "sections": array in video order covering the ENTIRE {span} from start to finish. {chapter_rule} \
Each item: {{"start": seconds, "end": seconds, "heading": short heading, "summary": 2-6 sentences of specific \
content -- claims, names, numbers, reasoning, conclusions}}.
  "claims": the key arguments and claims: {{"claim": ..., "evidence": the evidence or reasoning the speaker gives, \
or "none given", "t": seconds}}.
  "figures": short items only -- statistics, data points, dates, and brief direct quotes (at most 25 words, in \
quotation marks, with the speaker): {{"item": ..., "t": seconds}}.
  "on_screen": things shown but not said -- slides, charts, code, demos, on-screen text: {{"item": ..., "t": \
seconds}}; [] if nothing notable.
  "mentions": people, organizations, books/papers/works, products and sources named in the video or its \
description: {{"name": ..., "kind": "person|organization|work|product|source|link", "note": why it is \
mentioned (at most 20 words), "url": a URL from the description, or null}}.
  "takeaways": 3-7 strings.
  "caveats": strings -- disputed or unsupported claims, one-sidedness, sponsored content, possibly outdated \
information; [] if none.
  "language": BCP-47 code of the spoken language.
  {transcript_rule}
All times are whole seconds from the start of the FULL video. Across overview, sections, claims and takeaways \
write about {words} words in total. Be factual and specific; no hype, no calls to subscribe, and don't start \
every sentence with "the video".

Title: {title}
Channel: {channel}
Published: {published}
Length: {length}
{chapters}
Description:
{description}

{captions}"""


def _prompt(meta: dict, chaps: list[dict], caption_text: str | None, need_transcript: bool,
            seg: tuple[int, int] | None, words: int) -> str:
    if seg:
        a, b = seg
        segment_rule = (f"THIS REQUEST COVERS ONLY THE PART FROM {_hms(a)} TO {_hms(b)} of a "
                        f"{_hms(meta['duration_sec'])} video. Report only on that part.")
        span = "part"
        chaps = [c for c in chaps if a <= c["start"] < b]
    else:
        segment_rule, span = "", "runtime"
    if chaps:
        chapter_rule = "Use the creator's chapters listed below as the sections, one per chapter, with their start times."
        ctext = "Chapters:\n" + "\n".join(f"  {_hms(c['start'])} {c['title']}" for c in chaps)
    else:
        chapter_rule = "Divide it into its natural segments of roughly 3-8 minutes."
        ctext = "Chapters: none"
    if need_transcript:
        transcript_rule = ('"transcript": array of {"t": seconds, "text": ...} giving a COMPLETE verbatim transcript '
                           'of all speech with punctuation, one entry per ~60 seconds.')
    else:
        transcript_rule = "Do not include a transcript."
    captions = ("Captions (timestamped):\n" + caption_text) if caption_text else "Captions: none available."
    desc = (meta.get("description") or "").strip()[:6000] or "(none)"
    return PROMPT.format(segment_rule=segment_rule, span=span, chapter_rule=chapter_rule,
                         transcript_rule=transcript_rule, words=words, title=meta.get("title"),
                         channel=meta.get("channel"), published=(meta.get("published") or "")[:10],
                         length=_hms(meta["duration_sec"]), chapters=ctext, description=desc, captions=captions)


def _video_part(url: str, seg: tuple[int, int] | None):
    kw = {"file_data": types.FileData(file_uri=url, mime_type="video/*")}
    if seg:
        kw["video_metadata"] = types.VideoMetadata(start_offset=f"{seg[0]}s", end_offset=f"{seg[1]}s")
    return types.Part(**kw)


def _clean(d: dict, need_transcript: bool) -> dict:
    for k in _KEYS_LIST:
        v = d.get(k)
        d[k] = v if isinstance(v, list) else []
    for k in ("takeaways", "caveats"):            # sometimes [{"takeaway": "..."}] instead of ["..."]
        d[k] = [as_text(x) for x in d[k] if as_text(x)]
    for k in ("overview", "one_line", "language"):
        d[k] = str(d.get(k) or "").strip()
    for s in d["sections"]:
        for k in ("start", "end"):
            try:
                s[k] = int(float(s.get(k) or 0))
            except (TypeError, ValueError):
                s[k] = 0
    if need_transcript and not isinstance(d.get("transcript"), list):
        d["transcript"] = []
    if not d["overview"] or not d["sections"]:
        raise SummaryError("Gemini JSON lacks overview or sections")
    return d


def _fix_relative_times(d: dict, seg: tuple[int, int]):
    """If a segment came back with times relative to the clip, shift them to the full video."""
    a, b = seg
    starts = [s["start"] for s in d["sections"]]
    if a > 0 and starts and max(starts) < (b - a) + 60 and min(starts) < a - 60:
        for s in d["sections"]:
            s["start"] += a
            s["end"] += a
        for k in ("claims", "figures", "on_screen", "transcript"):
            for it in d.get(k) or []:
                if isinstance(it, dict) and isinstance(it.get("t"), (int, float)):
                    it["t"] = int(it["t"]) + a


def _one_call(pool: G.ModelPool, meta, chaps, caption, need_transcript, seg, res, words, log):
    url = f"https://www.youtube.com/watch?v={meta['video_id']}"
    cap_text = None
    if caption:
        snips = caption["snippets"]
        if seg:
            snips = [s for s in snips if seg[0] <= s["t"] < seg[1]]
        cap_text = T.as_prompt_text(snips)
    prompt = _prompt(meta, chaps, cap_text, need_transcript, seg, words)
    try:
        txt = pool.generate([_video_part(url, seg), types.Part(text=prompt)], _config(res))
    except Exception as e:  # noqa: BLE001
        size = any(s in str(e).lower() for s in ("too large", "exceeds the maximum", "context length", "token limit"))
        if size and res != "low":
            log(f"    input too large at {res} resolution; retrying at low")
            txt = pool.generate([_video_part(url, seg), types.Part(text=prompt)], _config("low"))
            res = "low"
        else:
            raise
    d = _clean(parse_json(txt), need_transcript)
    if seg:
        _fix_relative_times(d, seg)
    return d, pool.last_used, res


MERGE_PROMPT = """Below are summaries of consecutive parts of one YouTube video ("{title}", {length}). \
Write the whole-video parts of its reference note. Return ONE JSON object and nothing else:
  "overview": one paragraph (100-180 words): the thesis or main point of the WHOLE video and why it matters.
  "one_line": one sentence (at most 30 words) describing the video.
  "takeaways": 4-8 strings for the whole video.
  "caveats": strings; [] if none.
Parts:
{parts}"""


def summarize(pool: G.ModelPool, meta: dict, caption: dict | None, *, resolution: str = "auto",
              log=print) -> dict:
    """Returns {"data": note JSON, "models": [...], "resolution": str, "segments": n}."""
    dur = int(meta["duration_sec"])
    chaps = meta.get("chapters") or []
    need_tr = caption is None
    res = pick_resolution(resolution, dur)
    if dur <= MAX_SINGLE_SECONDS:
        d, model, res = _one_call(pool, meta, chaps, caption, need_tr, None, res, target_words(dur), log)
        return {"data": d, "models": [model], "resolution": res, "segments": 1}
    segs = [(a, min(a + SEGMENT_SECONDS, dur)) for a in range(0, dur, SEGMENT_SECONDS)]
    parts, models = [], []
    for seg in segs:
        log(f"    segment {_hms(seg[0])}-{_hms(seg[1])}")
        d, model, res = _one_call(pool, meta, chaps, caption, need_tr, seg, res,
                                  target_words(seg[1] - seg[0]), log)
        parts.append(d)
        models.append(model)
    brief = json.dumps([{"part": f"{_hms(a)}-{_hms(b)}", "overview": p["overview"], "takeaways": p["takeaways"],
                         "caveats": p["caveats"]} for (a, b), p in zip(segs, parts)], ensure_ascii=False)
    top = parse_json(pool.generate(MERGE_PROMPT.format(title=meta.get("title"), length=_hms(dur), parts=brief),
                                   _config(None, 8192)))
    models.append(pool.last_used)
    merged = {"overview": str(top.get("overview") or parts[0]["overview"]),
              "one_line": str(top.get("one_line") or parts[0]["one_line"]),
              "takeaways": top.get("takeaways") or sum((p["takeaways"] for p in parts), []),
              "caveats": top.get("caveats") or sum((p["caveats"] for p in parts), []),
              "language": parts[0]["language"]}
    for k in ("sections", "claims", "figures", "on_screen", "transcript"):
        merged[k] = sum((p.get(k) or [] for p in parts), [])
    seen, ments = set(), []
    for p in parts:
        for m in p["mentions"]:
            key = str(m.get("name", "")).strip().lower()
            if key and key not in seen:
                seen.add(key)
                ments.append(m)
    merged["mentions"] = ments
    return {"data": merged, "models": models, "resolution": res, "segments": len(segs)}


CLEAN_PROMPT = """Below are auto-generated YouTube captions with start times in seconds. Restore punctuation, \
capitalization and sentence boundaries; fix an obvious mis-recognition only when you are certain; drop filler \
such as "um" and "uh". Do NOT summarize, shorten, reorder or add anything. Group the text into paragraphs of \
roughly 45-75 seconds, each starting at the time of its first caption. Return ONE JSON object and nothing else: \
{{"paragraphs": [{{"t": seconds, "text": ...}}]}}

Captions:
{lines}"""


def clean_transcript(lite_pool: G.ModelPool | None, caption: dict, log=print) -> tuple[list[dict], str]:
    """-> (paragraphs [{t, text}], how). Manual captions: grouped only. Auto captions: punctuated by a
    text-only Flash-Lite call per 15 min, falling back to plain grouping for any chunk that fails or
    comes back more than 20% shorter (a sign the model summarized)."""
    snips = caption["snippets"]
    if not caption.get("generated") or lite_pool is None:
        return T.paragraphs(snips), "grouped"
    out, how = [], "punctuated by " + ""
    used = set()
    chunk, start = [], None
    chunks = []
    for s in snips:
        if start is None:
            start = s["t"]
        chunk.append(s)
        if s["t"] - start >= CLEAN_CHUNK_SECONDS:
            chunks.append(chunk)
            chunk, start = [], None
    if chunk:
        chunks.append(chunk)
    fallbacks = 0
    for ch in chunks:
        words_in = sum(len(s["text"].split()) for s in ch)
        try:
            txt = lite_pool.generate(CLEAN_PROMPT.format(lines="\n".join(f"{int(s['t'])} {s['text']}" for s in ch)),
                                     _config(None, 16384))
            paras = [{"t": int(float(p["t"])), "text": str(p["text"]).strip()}
                     for p in parse_json(txt).get("paragraphs", []) if str(p.get("text", "")).strip()]
            words_out = sum(len(p["text"].split()) for p in paras)
            if not paras or words_out < 0.8 * words_in:
                raise SummaryError(f"cleanup returned {words_out} of {words_in} words")
            out += paras
            used.add(lite_pool.last_used)
        except G.QuotaExhausted:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"    transcript cleanup chunk fell back to plain grouping: {G.short_reason(e) if not isinstance(e, SummaryError) else e}")
            out += T.paragraphs(ch)
            fallbacks += 1
    how = ("punctuated by " + ", ".join(sorted(used))) if used else "grouped"
    if used and fallbacks:
        how += f" ({fallbacks} of {len(chunks)} parts grouped only)"
    return out, how
