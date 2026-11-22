#!/usr/bin/env python3
r"""One-off import of Podwise episodes into the vault (2026-11-14). Not scheduled.

Podwise (app.podwise.ai) has already done the transcript and summary, so no Gemini or Claude
is used. For each episode ID in queue\podwise_remember_ids.txt (the owner's Podwise "Remember" list,
read by Claude from app.podwise.ai/dashboard/lists/3935), this fetches Podwise's summary and
transcript through Podwise's open API and writes the same pair of notes the Daily Summary's
"Save to Vault" makes for a podcast: "<title> (<show>) (Summary)" (type Podcast Summary) and a
linked "<title> (<show>) (Transcript)" (type Transcript), in the vault's Inbox. Organize, concept
analysis and the morning email then treat them like any other podcast notes.

Skipped: episodes already imported (ledger key pw:<id>), episodes already in the vault (same
resource link, or a Podcast/Video Summary with the same title), and episodes Podwise hasn't
processed yet (listed in the report; --process asks Podwise to process them, which can use
Podwise credits).

  one time: put Podwise's official CLI at %LOCALAPPDATA%\Programs\VaultLibrarian\podwise\podwise.exe
            (github.com/hardhackerlabs/podwise-cli/releases) and run  podwise auth  (approve in any browser)
  python podwise_import.py             dry run: preview notes + report in reports\, vault untouched
  python podwise_import.py --live      write the notes into the vault Inbox
  options: --only ID ...   --limit N   --process   --ids FILE
           --episode ID ... [--my-note TEXT] [--json]   import just these episodes (used by linkjob for
                                       Podwise links sent to Remember, the dropbox or the phone share)

Data comes through Podwise's official CLI (its own sign-in and API key; Podwise refuses other clients
with HTTP 403). The CLI keeps full JSON responses in its cache, here %LOCALAPPDATA%\VaultLibrarian\podwise_cache,
which is what this script reads. Fetching a summary also marks the episode read in Podwise.
Paths resolve from this file's location.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP / "lib"))
from remember import vault_writer as VW  # noqa: E402

IDS_FILE = APP / "queue" / "podwise_remember_ids.txt"
CACHE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "VaultLibrarian" / "podwise_cache"
DEFAULT_EXE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "VaultLibrarian" / "podwise" / "podwise.exe"
GENERATED_BY = "process:vault-podwise"
TYPE_SUMMARY = "Podcast Summary"
TYPE_TRANSCRIPT = VW.TYPE_TRANSCRIPT
DUP_TYPES = ("Podcast Summary", "Podcast Transcript", "Video Summary")


# ------------------------------------------------------------------ Podwise's official CLI
class NotProcessed(Exception):
    pass


def exe():
    for c in (os.environ.get("PODWISE_EXE"), str(DEFAULT_EXE), shutil.which("podwise")):
        if c and Path(c).exists():
            return c
    sys.exit("Podwise's CLI (podwise.exe) wasn't found. Put it at " + str(DEFAULT_EXE)
             + " (or on PATH, or set PODWISE_EXE), then run:  podwise auth")


def cli(*args, timeout=300):
    env = {**os.environ, "PODWISE_CACHE_DIR": str(CACHE)}
    r = subprocess.run([exe(), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=timeout)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit code {r.returncode}"
        if "not been processed" in msg.lower():
            raise NotProcessed(msg)
        raise RuntimeError(msg)
    return r.stdout


def fetch(eid):
    """(summary result dict incl. 'episode', transcript segments). The CLI keeps the full API
    responses as JSON in its cache folder; we read those rather than its printed text."""
    url = f"https://podwise.ai/dashboard/episodes/{eid}"
    cli("get", "summary", url)
    cli("get", "transcript", url, "--format", "json")
    summ = json.loads((CACHE / f"{eid}_summary.json").read_text(encoding="utf-8"))
    tr = json.loads((CACHE / f"{eid}_transcript.json").read_text(encoding="utf-8"))
    segs = tr.get("segments") if isinstance(tr, dict) else tr
    return summ, (segs or []), (summ.get("episode") or (tr.get("episode") if isinstance(tr, dict) else {}) or {})


# ------------------------------------------------------------------ helpers
def read_ids(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sid, _, title = line.partition("|")
        if sid.strip().isdigit():
            out.append((int(sid.strip()), title.strip()))
    return out


def iso_from_epoch(v):
    if not v:
        return None
    v = float(v)
    if v > 1e12:
        v /= 1000.0
    return dt.datetime.fromtimestamp(v, VW.TZ).replace(microsecond=0).isoformat()


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


_title_index = None


def title_index():
    """normalized title -> vault path, for Podcast/Video Summary notes already in the vault."""
    global _title_index
    if _title_index is None:
        _title_index = {}
        root = VW.vault_root()
        for p in VW._walk_md(root):
            t, _ = VW._head_meta(p)
            if t not in DUP_TYPES:
                continue
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    fm, _ = VW._split(f.read(6000))
                m = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', fm or "", re.M)
            except OSError:
                m = None
            if m:
                _title_index[norm_title(m.group(1))] = os.path.relpath(p, root).replace("\\", "/")
    return _title_index


_PRIVATE = re.compile(r"\s*\((?:private feed|members?[- ]only feed)[^)]*\)", re.I)


def show_name(ep):
    """Podwise's podcast name without '(private feed for <email>)'."""
    return _PRIVATE.sub("", (ep.get("podcastName") or "")).strip() or None


def public_link(u):
    """A link fit to keep in a note: http(s), and no private-feed token/key in it."""
    u = (u or "").strip()
    if not u.startswith("http") or re.search(r"[?&](token|key|auth|sig|signature)=", u, re.I):
        return None
    return u


def clean_text(s):
    return VW.escape_body((s or "").strip())


def ts(t):
    t = (t or "").strip()
    return f"[{t}] " if t else ""


def seg_time(seg):
    if seg.get("time"):
        return seg["time"]
    return VW.fmt_hms(seg.get("start")) if seg.get("start") is not None else ""


# ------------------------------------------------------------------ notes
def build(eid, ep, summ, segs, name, tname, my_note=""):
    title = (ep.get("title") or "").strip() or f"Podwise episode {eid}"
    show = show_name(ep)
    pw_url = f"https://podwise.ai/dashboard/episodes/{eid}"
    resource = pw_url                      # stable page; the raw audio link goes in audio_url when public
    audio = public_link(ep.get("link"))
    published = iso_from_epoch(ep.get("publishTime"))
    duration = VW.fmt_hms(ep.get("duration")) if ep.get("duration") else None
    accessed = dt.datetime.now(VW.TZ).date().isoformat()
    cite = VW.cite_podcast([], title, show, None, None, None, duration, published, resource)
    first = re.split(r"(?<=[.!?])\s+", re.sub(r"[*_`#>]", "", (summ.get("summary") or "")).strip())[0][:250]
    desc_src = first or f"Podwise summary of “{title}”."
    common = {
        "resource": resource, "audio_url": audio,
        "publication": show, "published": published, "accessed": accessed,
        "work_type": "podcast-episode", "duration": duration, "cite_as": cite,
        "generated": {"by": GENERATED_BY, "at": VW.now_iso()},
    }
    smeta = {"title": title, "aliases": [], **common, "summary_by": "podwise", "content": "summary",
             "description": desc_src, "see_also": [f"[[{tname}]]"], "my_note": (my_note or "").strip() or None}
    tmeta = {"title": f"{title} (transcript)", "aliases": [],
             "description": f"Transcript of “{title}”" + (f" ({show})." if show else "."),
             **common, "content": "transcript", "captured_via": "transcribed by Podwise",
             "see_also": [f"[[{name}]]"]}

    b = ["## Summary", "", f"**{show} — {title}**" if show else f"**{title}**", ""]
    if summ.get("summary"):
        b += [clean_text(summ["summary"]), ""]
    chapters = [c for c in (summ.get("chapters") or []) if (c.get("title") or c.get("summary"))]
    if chapters:
        b += ["---", ""]
        for c in chapters:
            b += [f"{ts('~' + c['time'] if c.get('time') else '')}**{clean_text(c.get('title'))}**".strip()]
            if c.get("summary"):
                b += [clean_text(c["summary"])]
            b += [""]
    if summ.get("takeaways"):
        b += ["---", "", "**Bottom line:**"] + [f"- {clean_text(x)}" for x in summ["takeaways"] if x] + [""]
    if summ.get("highlights"):
        b += ["## Highlights", ""] + [f"- {ts(h.get('time'))}{clean_text(h.get('content'))}"
                                      for h in summ["highlights"] if h.get("content")] + [""]
    qas = [q for q in (summ.get("qas") or []) if q.get("question") and q.get("answer")]
    if qas:
        b += ["## Questions and answers", ""]
        for q in qas:
            b += [f"**{clean_text(q['question'])}**", clean_text(q["answer"]), ""]
    if summ.get("keywords"):
        b += ["## Key terms", ""] + [f"- **{clean_text(k.get('key'))}**: {clean_text(k.get('desc'))}"
                                     for k in summ["keywords"] if k.get("key")] + [""]
    b += ["## Transcript", "", f"[[{tname}]]", "", "*Summary and transcript by Podwise.*"]
    sbody = "\n".join(b)

    t = [f"Summary: [[{name}]]", "", "## Transcript", "", "*Transcript by Podwise; times approximate.*", ""]
    para, p_time, p_start, p_sp = [], "", None, None

    def flush():
        if para:
            who = f"{p_sp}: " if p_sp else ""
            t.extend([f"{ts(p_time)}{who}{clean_text(' '.join(para))}", ""])

    for sg in segs:
        txt = (sg.get("content") or "").strip()
        if not txt:
            continue
        sp = (sg.get("speaker") or "").strip() or p_sp
        start = None                      # seconds; from the hh:mm:ss label (Podwise's "start" unit varies)
        if sg.get("time"):
            hms = [int(x) for x in re.findall(r"\d+", sg["time"])][-3:]
            start = sum(v * m for v, m in zip(reversed(hms), (1, 60, 3600)))
        new = not para or sp != p_sp or (start is not None and p_start is not None and start - p_start >= 60)
        if new:
            flush()
            para, p_time, p_start = [], seg_time(sg), start
        p_sp = sp
        para.append(txt)
    flush()
    tbody = "\n".join(t)
    return smeta, sbody, tmeta, tbody


# ------------------------------------------------------------------ run
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--process", action="store_true", help="ask Podwise to process unprocessed episodes (may use Podwise credits)")
    ap.add_argument("--only", nargs="*", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids", default=str(IDS_FILE))
    ap.add_argument("--episode", nargs="*", type=int, help="import these episode IDs instead of the IDs file")
    ap.add_argument("--my-note", default="", help="your note for the summary's my_note (single-episode use)")
    ap.add_argument("--json", action="store_true", help="also print the results as one JSON line (for linkjob)")
    a = ap.parse_args()
    ids = [(e, "") for e in a.episode] if a.episode else read_ids(a.ids)
    if a.only:
        ids = [x for x in ids if x[0] in set(a.only)]
    if a.limit:
        ids = ids[:a.limit]
    stamp = dt.datetime.now(VW.TZ)
    preview = APP / "reports" / f"{stamp:%Y-%m-%d %H%M} podwise preview"
    rows = []
    root = VW.vault_root()
    for eid, list_title in ids:
        label = list_title or str(eid)
        try:
            if VW.ledger_get(f"pw:{eid}").get("summary"):
                rows.append((label, "already imported", VW.ledger_get(f"pw:{eid}")["summary"]))
                continue
            try:
                summ, segs, ep = fetch(eid)
            except NotProcessed:
                if a.process:
                    out = cli("process", f"https://podwise.ai/dashboard/episodes/{eid}", timeout=900)
                    rows.append((label, "sent to Podwise for processing; run again later: "
                                 + (out.strip().splitlines() or [""])[-1][:80], ""))
                else:
                    rows.append((label, "not processed in Podwise yet (use --process)", ""))
                continue
            title = (ep.get("title") or list_title or "").strip()
            dup = VW.find_by_resource(f"https://podwise.ai/dashboard/episodes/{eid}", DUP_TYPES)
            if not dup and public_link(ep.get("link")):
                dup = VW.find_by_resource(public_link(ep.get("link")), DUP_TYPES)
            if not dup and title_index().get(norm_title(title)):
                dup = [{"path": title_index()[norm_title(title)]}]
            if dup:
                rows.append((label, "already in the vault", dup[0]["path"]))
                continue
            if not segs:
                rows.append((label, "Podwise returned no transcript; skipped", ""))
                continue
            base = VW.safe_name(title, show_name(ep))
            name = VW.unique_name(root, f"{base} (Summary)")
            tname = VW.unique_name(root, f"{base} (Transcript)")
            smeta, sbody, tmeta, tbody = build(eid, ep, summ, segs, name, tname, a.my_note)
            if not a.live:
                preview.mkdir(parents=True, exist_ok=True)
                for n, kind, m, body in ((name, TYPE_SUMMARY, smeta, sbody), (tname, TYPE_TRANSCRIPT, tmeta, tbody)):
                    (preview / f"{n}.md").write_text(VW.frontmatter({"type": kind, **m}) + "\n" + body.strip() + "\n",
                                                     encoding="utf-8", newline="\n")
                rows.append((label, f"preview ({len(segs)} transcript segments)", f"reports/{preview.name}/{name}.md"))
                continue
            s_rel = VW.write_named(TYPE_SUMMARY, smeta, sbody, name)
            t_rel = VW.write_named(TYPE_TRANSCRIPT, tmeta, tbody, tname)
            VW.ledger_record(f"pw:{eid}", {"summary": s_rel, "transcript": t_rel, "saved_at": VW.now_iso(),
                                           "by": GENERATED_BY})
            rows.append((label, "saved", s_rel))
        except Exception as e:  # keep going; report the failure
            rows.append((label, f"FAILED: {e}", ""))
        time.sleep(0.5)

    if a.json and a.episode:            # single link from intake: the intake log/queue records it, no report file
        print("RESULT " + json.dumps([{"episode": l, "result": r, "note": p} for l, r, p in rows], ensure_ascii=False))
        return
    rep = APP / "reports" / f"{stamp:%Y-%m-%d %H%M} podwise import{'' if a.live else ' (dry run)'}.md"
    lines = ["---", 'type: "Report"', f'title: "Podwise import {stamp:%Y-%m-%d %H:%M}"',
             f'generated: {{ by: "{GENERATED_BY}", at: "{VW.now_iso()}" }}', "---", "",
             f"# Podwise import — {'live' if a.live else 'dry run'}", "",
             "| Episode | Result | Note |", "|---|---|---|"]
    lines += [f"| {l.replace('|', '/')} | {r.replace('|', '/')} | {p} |" for l, r, p in rows]
    rep.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if a.json:
        print("RESULT " + json.dumps([{"episode": l, "result": r, "note": p} for l, r, p in rows], ensure_ascii=False))
    from collections import Counter
    c = Counter(r.split(" (")[0].split(":")[0] for _, r, _ in rows)
    print("; ".join(f"{k}: {v}" for k, v in c.items()))
    print(f"Report: {rep}")


if __name__ == "__main__":
    main()
