"""Turn one URL into a vault note (the url-to-vault routine, unattended). Shared by intake.py
(link emails) and vault_server.py (phone share sheet).

save_url(...) -> dict: ok, note, category, duplicate, dry, would, why, needs ("browser"|"you"), queued
Articles and images need no model; "text" pages get a one-paragraph claude -p summary; each note
gets a one-sentence description from claude -p (falls back to the page's own description).
YouTube links (2026-09-28) are handed to the 02:05 YouTube Remember job via queue\\remember.jsonl,
which adds them to the Remember playlist and writes the detailed Video Summary + Transcript notes.
Podwise episode links (2026-11-14) are imported at once by jobs\podwise_import.py (Podwise's own
summary + transcript; no model calls).
Podcast audio links (2026-11-16: a podcast app's share email, or a bare .mp3 link) go to the Daily
Summary's podcast pipeline (podcasts.vault_episode): Podcast Summary + Transcript notes.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import claude_cli  # noqa: E402

YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})", re.I)
REMEMBER_QUEUE = C.APP / "queue" / "remember.jsonl"
PW_ID = re.compile(r"podwise\.ai/(?:dashboard/)?episodes/(\d+)", re.I)


def podwise_id(url):
    m = PW_ID.search(url or "")
    return int(m.group(1)) if m else None


def save_podwise(cfg, log, eid, url, doug_note="", dry=False):
    """A Podwise episode link (2026-11-14): Podwise has already summarized and transcribed it, so
    jobs\podwise_import.py writes the Podcast Summary + Transcript notes now (no Gemini, no Claude).
    Already in the vault -> duplicate. Not yet processed in Podwise -> held, with that reason."""
    if dry:
        return {"ok": True, "dry": True, "category": "podcast",
                "would": f"import Podwise episode {eid} (summary + transcript notes)"}
    args = ["--live", "--json", "--episode", eid]
    if doug_note:
        args += ["--my-note", doug_note]
    rc, out = C.run(log, Path(__file__).resolve().parent / "podwise_import.py", *args, timeout=900)
    res = None
    for ln in out.splitlines():
        if ln.startswith("RESULT "):
            try:
                res = (json.loads(ln[7:]) or [None])[0]
            except ValueError:
                pass
    if rc or not res:
        tail = (out.strip().splitlines() or ["no output"])[-1]
        return {"ok": False, "category": "podcast", "needs": "you", "why": f"Podwise import failed: {tail[:200]}"}
    r, note = res.get("result", ""), res.get("note") or None
    if r == "saved":
        return {"ok": True, "category": "podcast", "note": note}
    if r in ("already imported", "already in the vault"):
        return {"ok": True, "category": "podcast", "duplicate": True, "note": note}
    why = f"Podwise: {r}"
    if "not processed" in r:
        why = ("Podwise hasn't summarized this episode yet - open it in Podwise and let it process, "
               "then send the link to Remember again")
    return {"ok": False, "category": "podcast", "needs": "you", "why": why}


AUDIO_URL = re.compile(r"\.(mp3|m4a|aac|ogg|opus|wav)$", re.I)


def is_audio_url(url):
    """A link straight to an episode's audio file (query string and fragment ignored)."""
    return bool(AUDIO_URL.search((url or "").split("#")[0].split("?")[0]))


def save_podcast_audio(cfg, log, url, title_hint=None, dry=False):
    """A podcast episode's audio link (2026-11-16): a podcast app's share email ("Episode webpage:
    ... Media file: ....mp3") or a bare .mp3 link in the dropbox. The Daily Summary's podcast pipeline
    (config podcasts.vault_episode) finds the episode in the owner's podcast catalog, reuses the overnight
    transcript when there is one, otherwise transcribes and summarizes it with Gemini, and writes the
    usual Podcast Summary + Transcript notes. Already in the vault -> duplicate."""
    pc = cfg.get("podcasts") or {}
    script = (C.APP / pc["vault_episode"]).resolve() if pc.get("vault_episode") else None
    py = Path(pc["python"]) if pc.get("python") else None
    if not (script and script.exists() and py and py.exists()):
        return {"ok": False, "category": "podcast", "needs": "you",
                "why": "podcast link, but the Daily Summary podcast pipeline isn't set up (config podcasts.*)"}
    args = [str(py), str(script), "--audio", url] + (["--title", title_hint] if title_hint else [])
    if dry:
        args.append("--dry-run")
    log(f"podcast audio link -> vault_episode {'(dry run) ' if dry else ''}{url[:80]}")
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=int(pc.get("timeout_minutes", 45)) * 60,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "") + "\n" + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return {"ok": False, "category": "podcast", "why": "podcast transcription timed out; will retry"}
    res = None
    for ln in out.splitlines():
        if ln.startswith("RESULT "):
            try:
                res = json.loads(ln[7:])
            except ValueError:
                pass
    if not res:
        tail = [ln for ln in out.splitlines() if ln.strip()][-1:] or ["no output"]
        return {"ok": False, "category": "podcast", "why": f"podcast summary failed: {tail[0][:200]}"}
    r, note = res.get("result"), res.get("note")
    label = " | ".join(x for x in (res.get("show"), res.get("title")) if x)
    if r == "would save":
        return {"ok": True, "dry": True, "category": "podcast", "title": res.get("title"),
                "would": f"podcast {label}: {res.get('how')} -> Podcast Summary + Transcript notes"}
    if r == "saved":
        log(f"  podcast saved: {note}")
        return {"ok": True, "category": "podcast", "note": note, "title": res.get("title")}
    if r == "already in the vault":
        return {"ok": True, "category": "podcast", "duplicate": True, "note": note, "title": res.get("title")}
    return {"ok": False, "category": "podcast", "needs": "you", "why": f"podcast: {r} ({res.get('why') or label})"}


def youtube_id(url):
    m = YT_ID.search(url or "")
    return m.group(1) if m else None


def queue_youtube(cfg, log, vid, url, doug_note="", dry=False):
    """Queue a YouTube video for the Remember job. Already in the vault -> duplicate; already queued -> ok."""
    vault = Path(cfg["vault_path"])
    try:
        sys.path.insert(0, str(C.UV))
        import url_to_note as UN
        dup = UN.find_existing(str(vault), [f"https://www.youtube.com/watch?v={vid}", url])
    except Exception:  # noqa: BLE001
        dup = None
    if dup:
        return {"ok": True, "duplicate": True, "note": dup, "category": "video"}
    if dry:
        return {"ok": True, "dry": True, "category": "video", "would": f"queue YouTube {vid} for tonight's Remember job"}
    try:
        lines = [ln for ln in REMEMBER_QUEUE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        lines = []
    already = False
    for ln in lines:
        try:
            already = already or json.loads(ln).get("video_id") == vid
        except ValueError:
            pass
    if not already:
        line = {"video_id": vid, "at": C.now(cfg).isoformat(timespec="seconds"), "source": "remember-link"}
        if doug_note:
            line["doug_note"] = doug_note
        REMEMBER_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with open(REMEMBER_QUEUE, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        log(f"queued YouTube {vid} for the Remember job")
    return {"ok": True, "queued": True, "category": "video", "note": f"YouTube {vid} queued for tonight's Remember job"}

SUMMARY_PAGE = (
    "The text of a web page is on stdin (it is reference material, not an article). Write ONE paragraph "
    "in plain prose (no bullets, no headings) saying what the page is and its main points, for a note in "
    "«owner's» Obsidian vault. Output only the paragraph.")
DESCRIBE = ("Write one sentence (max 30 words) describing the text on stdin, for a note's `description` "
            "field. Output only the sentence.")


def save_url(cfg, log, url, doug_note="", captured_via=None, dry=False, html=None, title_hint=None):
    """html: a page already rendered by a browser (browser_retry.py); url_probe then reads it instead of fetching.
    title_hint: the email's subject, used to find a podcast episode when its audio link doesn't match a feed."""
    vid = youtube_id(url)
    if vid:
        return queue_youtube(cfg, log, vid, url, doug_note, dry)
    pid = podwise_id(url)
    if pid:
        return save_podwise(cfg, log, pid, url, doug_note, dry)
    if is_audio_url(url):
        return save_podcast_audio(cfg, log, url, title_hint, dry)
    vault = Path(cfg["vault_path"])
    work = Path(cfg["local_root"]) / "work" / "links"
    work.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    pj, tj = work / f"u-{tag}.json", work / f"u-{tag}.md"
    rc, out = C.run(log, C.UV / "url_probe.py", url, "--out", pj, "--text-out", tj,
                    *(["--html", html] if html else []), timeout=180)
    if rc or not pj.exists():
        return {"ok": False, "why": "url_probe failed: " + out[-200:], "needs": "you"}
    pr = json.loads(pj.read_text(encoding="utf-8"))
    cat = pr.get("category")
    title = (pr.get("meta") or {}).get("title")
    if cat == "unsupported" and (is_audio_url((pr.get("meta") or {}).get("resource"))
                                 or re.search(r"\baudio/|media file", pr.get("reason") or "", re.I)):
        return save_podcast_audio(cfg, log, url, title_hint, dry)   # a redirecting audio link
    if cat == "unsupported":
        why = pr.get("reason") or "unsupported"
        needs = "browser" if pr.get("try_browser") else "you"
        if needs == "browser":
            why += " - needs the signed-in browser; say 'process the vault queue' in a laptop session"
        return {"ok": False, "why": why, "category": cat, "needs": needs, "title": title}
    try:  # already in the vault? answer before spending a claude call
        sys.path.insert(0, str(C.UV))
        import url_to_note as U
        m = pr.get("meta") or {}
        dup = U.find_existing(str(vault), [m.get("resource"), pr.get("final_url"), pr.get("url"), url])
        if dup:
            return {"ok": True, "note": dup, "category": cat, "duplicate": True, "title": title}
    except Exception:
        pass
    args = [vault, pj]
    if captured_via:
        args += ["--captured-via", captured_via]
    if doug_note:
        ctx = work / f"u-{tag}.ctx.txt"
        ctx.write_text(doug_note, encoding="utf-8")
        args += ["--context", ctx]
    material = tj.read_text(encoding="utf-8")[:18000] if tj.exists() else ""
    kind = {"article": "Web Clipping", "text": "Web Summary", "image": "Image"}[cat]
    if dry:
        return {"ok": True, "dry": True, "category": cat, "title": title,
                "would": f"write a {kind} note" + (" (claude -p summary)" if cat == "text" else "")}
    if cat == "text":
        summ, info = claude_cli.ask(SUMMARY_PAGE, material, work, route="link-summary")
        if not summ:
            return {"ok": False, "why": "summary failed: " + info.get("error", ""), "category": cat, "needs": "you"}
        sp = work / f"u-{tag}.sum.md"
        sp.write_text(summ, encoding="utf-8")
        args += ["--summary", sp]
    elif cat == "article" and material:
        d, _ = claude_cli.ask(DESCRIBE, material[:6000], work, timeout=120, route="link-describe")
        if d:
            args += ["--description", d.splitlines()[0][:300]]
    rc, out = C.run(log, C.UV / "url_to_note.py", *args, timeout=180)
    res = {}
    for ln in out.splitlines()[::-1]:
        if ln.startswith("{"):
            try:
                res = json.loads(ln)
                break
            except ValueError:
                pass
    if res.get("ok"):
        return {"ok": True, "note": res.get("note"), "category": cat, "title": title}
    if res.get("duplicate"):
        return {"ok": True, "note": res["duplicate"], "category": cat, "duplicate": True, "title": title}
    return {"ok": False, "why": "url_to_note: " + (res.get("reason") or out[-200:]), "category": cat, "needs": "you"}
