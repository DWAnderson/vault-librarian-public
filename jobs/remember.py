#!/usr/bin/env python3
"""YouTube "Remember" intake on this PC (Task Scheduler, 02:05).

Each video on the owner's YouTube playlist "Remember" (plus videos queued by the Daily Summary's
Save to Vault links in queue\\remember.jsonl) becomes a detailed Video Summary note and a
linked Transcript note in the vault's Inbox, and is then deleted from the playlist.
Plan: "2026-09-26 YouTube Remember intake - plan.md" (sections 12-14 hold the amendments).

Gemini: Google AI project "Obsidian Youtube Summary" (free tier), Remember's own key and
quota, so it never competes with the Daily Summary. The run stops when the queue is empty,
every free model is out of daily quota, the next video would exceed the day's video allowance
(remember.video_cap_seconds), or remember.stop_by (local time) is reached. Leftovers keep their
place and roll to the next run. Everything Gemini returns is cached per video before anything
else happens, so a crash or a missed write never pays twice.

  pythonw remember.py            uses config remember.dry_run
  python  remember.py --dry-run  summaries into the cache + preview notes in reports\\ ;
                                 nothing in the vault or the playlist changes
  python  remember.py --live     (Phase 4) write notes, delete finished videos from Remember
  options: --only VIDEOID ...   --limit N   --ignore-stop
"""
import argparse
import os
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import vault_names as VN  # noqa: E402

sys.path.insert(0, str(C.LIB))
from remember import gemini_pool as G  # noqa: E402
from remember import usage_ledger as U  # noqa: E402

PROJECT = "obsidian_youtube"
QUEUE_FILE = C.APP / "queue" / "remember.jsonl"


def _load(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _save(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def read_queue():
    """Save to Vault clicks: one JSON object per line, {"video_id": ..., "at": ...}."""
    out = []
    try:
        for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _hm(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ignore-stop", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    rc = cfg.get("remember", {})
    dry = True if a.dry_run else (False if a.live else bool(rc.get("dry_run", True)))
    log = C.Log(cfg, "remember")
    if rc.get("enabled") is False or C.paused(cfg):   # home page switches (2026-11-12)
        log("Remember is off" if rc.get("enabled") is False else f"paused until {C.paused(cfg)}")
        return 0
    started = C.now(cfg)
    log(f"== Remember {'dry run' if dry else 'live'} start")
    try:
        return run(cfg, rc, log, dry, started, a)
    except Exception as e:  # noqa: BLE001
        log("FATAL " + traceback.format_exc())
        write_report(cfg, dry, started, [], [f"Run failed: {type(e).__name__}: {e}"], {}, {})
        return 1


def run(cfg, rc, log, dry, started, a):
    from remember import notes as N
    from remember import summarizer as S
    from remember import transcript as T
    from remember import vault_writer as VW
    from remember import youtube as Y

    local = Path(cfg["local_root"])
    cache_dir = local / "cache" / "remember"
    cache_dir.mkdir(parents=True, exist_ok=True)
    G.MODEL_CACHE = cache_dir / "model_cache.json"
    state_path = local / "queue" / "remember_state.json"
    state = _load(state_path, {})
    cap = int(rc.get("video_cap_seconds", 27000))
    stop_by = rc.get("stop_by", "05:45")
    failures, rows = [], []

    # ---- playlist + queue
    try:
        yt = Y.service()
    except Y.AuthError as e:
        write_report(cfg, dry, started, [], [str(e)], {}, {})
        return 1
    pid = Y.find_playlist(yt, rc.get("playlist", "Remember"))
    if not pid:
        write_report(cfg, dry, started, [], ["No YouTube playlist named 'Remember' on the signed-in account."], {}, {})
        return 1
    items = Y.playlist_items(yt, pid)
    on_list = {i["video_id"]: i for i in items}
    queued = [q for q in read_queue() if q.get("video_id")]
    to_add = [q["video_id"] for q in queued if q["video_id"] not in on_list]
    if to_add:
        log(f"{len(to_add)} queued video(s) not on Remember yet: " + ", ".join(to_add)
            + (" (dry run: not added)" if dry else ""))
        if not dry:
            for v in to_add:
                try:
                    Y.add_to_playlist(yt, pid, v)
                except Exception as e:  # noqa: BLE001
                    log(f"  could not add {v} to Remember: {e}")
            items = Y.playlist_items(yt, pid)
            on_list = {i["video_id"]: i for i in items}
    order = list(dict.fromkeys([q["video_id"] for q in queued] +
                               [i["video_id"] for i in sorted(items, key=lambda i: i.get("added") or "")]))
    if a.only:
        order = [v for v in order if v in a.only] + [v for v in a.only if v not in order]
    metas = Y.video_meta(yt, order)
    log(f"Queue: {len(order)} video(s) ({len(queued)} from Save to Vault, {len(items)} on Remember)")

    # ---- Gemini
    key = (local / "credentials" / "gemini_api_key.txt").read_text(encoding="utf-8").strip()
    client = G.make_client(key)
    pool = G.ModelPool(client, G.flash_models(key), project=PROJECT, source="remember", minute_backoff=True)
    lite = G.ModelPool(client, G.flash_models(key, lite_first=True), project=PROJECT,
                       source="remember_transcript", minute_backoff=True)
    log(f"Models: {', '.join(pool.models)}; transcript clean-up: {', '.join(lite.models[:3])}")
    used = U.video_seconds(PROJECT) or 0
    log(f"Quota day {U.quota_day()}: {_hm(used)} of {_hm(cap)} video already used; next reset "
        f"{U.next_reset():%H:%M %Z}")

    try:
        vault_root = VW.vault_root()
    except Exception as e:  # noqa: BLE001
        vault_root = None
        log(f"vault not found for name checks: {e}")

    stop = None
    done = 0
    for vid in order:
        m = metas.get(vid)
        row = {"video_id": vid, "title": (m or {}).get("title") or (on_list.get(vid) or {}).get("title") or vid,
               "duration": (m or {}).get("duration_sec", 0), "from": "Save to Vault" if any(q["video_id"] == vid for q in queued) else "Remember"}
        rows.append(row)
        st = state.setdefault(vid, {"first_seen": started.isoformat(timespec="seconds"), "runs": 0})
        if stop:
            row["status"] = f"rolled to next run ({stop})"
            continue
        if not a.ignore_stop and started.date() == C.now(cfg).date() and f"{C.now(cfg):%H:%M}" >= stop_by >= f"{started:%H:%M}":
            stop = f"stop time {stop_by} reached"
            row["status"] = f"rolled to next run ({stop})"
            continue
        if a.limit and done >= a.limit:
            stop = f"--limit {a.limit}"
            row["status"] = f"rolled to next run ({stop})"
            continue
        if not m:
            row["status"] = "unavailable (private or removed) - left on Remember"
            continue
        if m.get("live") in ("live", "upcoming"):
            row["status"] = "live or upcoming - left on Remember"
            continue
        m["chapters"] = Y.chapters(m["description"])
        m["links"] = Y.description_links(m["description"])
        cpath = cache_dir / f"{vid}.json"
        rec = _load(cpath, {}) or {}
        rec["meta"] = m
        dur = m["duration_sec"]
        try:
            if "summary" not in rec:
                if dur > cap:
                    row["status"] = f"longer than a whole day's allowance ({_hm(dur)}) - left on Remember"
                    continue
                if used + dur > cap:
                    stop = f"daily video allowance: {_hm(cap - used)} left, next video is {_hm(dur)}"
                    row["status"] = f"rolled to next run ({stop})"
                    continue
                if "caption" not in rec:
                    try:
                        rec["caption"] = T.fetch(vid, m.get("language"))
                    except Exception as e:  # noqa: BLE001
                        log(f"  captions failed for {vid}: {type(e).__name__}: {str(e)[:200]}")
                        rec["caption"] = None
                        rec["caption_error"] = f"{type(e).__name__}: {str(e)[:300]}"
                    _save(cpath, rec)
                cap_desc = ("none" if not rec["caption"] else
                            f"{rec['caption']['source']} ({rec['caption']['words']:,} words)")
                log(f"[{vid}] {m['title'][:60]} ({_hm(dur)}); captions: {cap_desc}")
                t0 = time.time()
                rec["summary"] = S.summarize(pool, m, rec["caption"], resolution=rc.get("media_resolution", "auto"), log=log)
                rec["summary"]["secs"] = round(time.time() - t0, 1)
                rec["summary"]["at"] = C.now(cfg).isoformat(timespec="seconds")
                U.record(PROJECT, "remember", requests=0, video_seconds=dur)
                used += dur
                _save(cpath, rec)
                row["new"] = True
            if "transcript" not in rec:
                if rec.get("caption"):
                    paras, how = S.clean_transcript(lite, rec["caption"], log=log)
                    src = rec["caption"]["source"]
                    how = f"YouTube {'auto-generated' if rec['caption']['generated'] else 'creator'} captions, {how}"
                else:
                    paras = [{"t": int(float(p.get("t") or 0)), "text": str(p.get("text") or "")}
                             for p in rec["summary"]["data"].get("transcript") or [] if isinstance(p, dict)]
                    src, how = "gemini", f"transcribed by {', '.join(rec['summary']['models'])} (no captions)"
                rec["transcript"] = {"paragraphs": paras, "source": src, "how": how,
                                     "words": sum(len(p["text"].split()) for p in paras)}
                _save(cpath, rec)
            row["status"] = "summarized" if row.get("new") else "cached (summarized earlier)"
            row["models"] = ", ".join(dict.fromkeys(rec["summary"]["models"]))
            row["resolution"] = rec["summary"].get("resolution")
            done += 1
            st.pop("last_error", None)
        except G.QuotaExhausted:
            stop = "every free Gemini model is out of daily quota"
            row["status"] = f"rolled to next run ({stop})"
            _save(cpath, rec)
            continue
        except Exception as e:  # noqa: BLE001
            reason = G.short_reason(e)
            st["last_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            st["failures"] = st.get("failures", 0) + 1
            row["status"] = f"failed - {reason} (left on Remember)"
            failures.append(f"{row['title'][:70]} ({vid}): {reason}")
            log(f"  FAILED {vid}: {traceback.format_exc()[-800:]}")
            _save(cpath, rec)
            continue
        finally:
            st["runs"] = st.get("runs", 0) + 1

        # ---- notes: dry run -> preview files outside the vault; live -> vault Inbox, then off Remember
        row.update({"transcript_words": rec["transcript"]["words"], "transcript_how": rec["transcript"]["how"],
                    "target_words": S.target_words(dur)})
        if dry:
            name, tname = N.names(m, vault_root)
            smeta, sbody, tmeta, tbody = N.build(m, rec, name, tname)
            row.update({"note": name, "summary_words": N.word_count(sbody)})
            pdir = C.APP / "reports" / "remember-preview" / f"{started:%Y-%m-%d}"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / f"{name}.md").write_text(N.render(VW.TYPE_VIDEO, smeta, sbody), encoding="utf-8")
            (pdir / f"{tname}.md").write_text(N.render(VW.TYPE_TRANSCRIPT, tmeta, tbody), encoding="utf-8")
        else:
            try:
                file_live(cfg, log, yt, vid, m, rec, row, [i for i in items if i["video_id"] == vid], N, VW)
            except Exception as e:  # noqa: BLE001
                row["status"] = f"notes not written - {type(e).__name__}: {str(e)[:150]} (left on Remember)"
                failures.append(f"{row['title'][:70]} ({vid}): notes not written ({type(e).__name__}: {str(e)[:150]})")
                log(f"  NOTES FAILED {vid}: {traceback.format_exc()[-800:]}")

    _save(state_path, state)
    lint = {}
    if dry:
        pdir = C.APP / "reports" / "remember-preview" / f"{started:%Y-%m-%d}"
        if pdir.exists():
            lj = Path(cfg["local_root"]) / "work" / "remember-lint.json"
            rc_, out = C.run(log, C.VL / "okf_lint.py", pdir, "--no-orphans", "--json", lj)
            lint = {"exit": rc_, "output": (out or "").strip()[-3000:]}
    day = U.day_entry(PROJECT)
    usage = {"video": _hm(day.get("video_seconds", 0)), "requests": day.get("requests", {}),
             "limit_429": day.get("limit_429", {}), "exhausted": list((day.get("exhausted") or {}).keys())}
    write_report(cfg, dry, started, rows, failures, lint, usage, stop)
    log(f"== Remember end: {sum(1 for r in rows if r.get('new'))} new summaries; stop: {stop or 'queue done'}")
    return 1 if failures else 0


def _lint_paths(cfg, log, rels):
    """OKF lint of just these vault-relative notes. -> (errors, output tail)."""
    import re as _re
    rc_, out = C.run(log, C.VL / "okf_lint.py", cfg["vault_path"], "--no-orphans", "--paths", *rels, timeout=900)
    m = _re.search(r"(\d+) errors?", out or "")
    errors = int(m.group(1)) if m else (0 if rc_ == 0 else 1)
    return errors, (out or "").strip()[-600:]


def _with_lock(cfg, log, fn):
    """Run fn() under the shared vault lock; wait up to 30 min if another job holds it (plan section 8).
    The lock is held only while notes are written, so the 02:30 Nightly is unlikely to meet it."""
    lock = C.VaultLock(cfg, f"{C.pc_name()} remember")
    for attempt in range(31):
        ok, note = lock.acquire()
        if ok:
            try:
                return fn()
            finally:
                try:   # vault search index catches up (plan: 2026-10-08 Vault search - plan.md, phase 1)
                    import vault_search
                    vault_search.hook(cfg, log)
                except Exception:  # noqa: BLE001
                    pass
                lock.release()
        if attempt == 0:
            log(f"  vault lock busy ({note}); waiting up to 30 min")
        time.sleep(60)
    raise TimeoutError("vault lock held for 30 minutes; notes move to the next run (Gemini result stays cached)")


def file_live(cfg, log, yt, vid, m, rec, row, playlist_items, N, VW):
    """Write the two notes (or find ones written by an earlier run), lint them, then delete the video
    from Remember -- only when both notes exist and lint has 0 errors."""
    from remember import youtube as Y
    url = f"https://www.youtube.com/watch?v={vid}"
    found = VW.find_by_resource(url, (VW.TYPE_VIDEO, VW.TYPE_TRANSCRIPT))
    ours_s = [f for f in found if f["type"] == VW.TYPE_VIDEO and f["by"] == VW.GENERATED_BY]
    ours_t = [f for f in found if f["type"] == VW.TYPE_TRANSCRIPT and f["by"] == VW.GENERATED_BY]
    other_s = [f for f in found if f["type"] == VW.TYPE_VIDEO and f["by"] != VW.GENERATED_BY]
    created = []
    if other_s and not ours_s:
        created = replace_saved(cfg, log, vid, m, rec, row, other_s[0], N, VW)
        found = VW.find_by_resource(url, (VW.TYPE_VIDEO, VW.TYPE_TRANSCRIPT))
        ours_s = [f for f in found if f["type"] == VW.TYPE_VIDEO and f["by"] == VW.GENERATED_BY]
        ours_t = [f for f in found if f["type"] == VW.TYPE_TRANSCRIPT and f["by"] == VW.GENERATED_BY]
    if not ours_s or not ours_t:
        def write():
            root = VW.vault_root()
            name, tname = N.names(m, root)
            if ours_s:                                  # summary exists, transcript missing: reuse its name
                name = Path(ours_s[0]["path"]).stem
                tname = VW.unique_name(root, VN.transcript_name(name))
            smeta, sbody, tmeta, tbody = N.build(m, rec, name, tname)
            row["summary_words"] = N.word_count(sbody)
            out = []
            if not ours_s:
                out.append(VW.write_named(VW.TYPE_VIDEO, smeta, sbody, name))
            if not ours_t:
                out.append(VW.write_named(VW.TYPE_TRANSCRIPT, tmeta, tbody, tname))
            import hashlib
            VW.ledger_record(f"v:{vid}", {"summary": out[0] if not ours_s else ours_s[0]["path"],
                                          "transcript": out[-1] if not ours_t else ours_t[0]["path"],
                                          "saved_at": VW.now_iso(),
                                          "generated_hash": hashlib.sha256(sbody.encode("utf-8")).hexdigest()})
            return out
        created = _with_lock(cfg, log, write)
        log(f"  wrote {', '.join(created)}")
    rec_l = VW.ledger_get(f"v:{vid}")
    root = Path(cfg["vault_path"])
    # Organize or Nightly may have moved the notes since they were written: prefer where they are NOW
    # (found by resource), then the ledger path (2026-09-28 fix: a stale ledger path left a video on Remember).
    if not created:
        found = VW.find_by_resource(url, (VW.TYPE_VIDEO, VW.TYPE_TRANSCRIPT))
        ours_s = [f for f in found if f["type"] == VW.TYPE_VIDEO and f["by"] == VW.GENERATED_BY]
        ours_t = [f for f in found if f["type"] == VW.TYPE_TRANSCRIPT and f["by"] == VW.GENERATED_BY]
    paths = [(ours_s[0]["path"] if ours_s else None) or rec_l.get("summary"),
             (ours_t[0]["path"] if ours_t else None) or rec_l.get("transcript")]
    paths = [p for p in paths if p]
    row["note"] = Path(paths[0]).stem if paths else None
    if len(paths) < 2 or not all((root / p).exists() for p in paths):
        row["status"] = "notes incomplete - left on Remember"
        return
    errors, out = _lint_paths(cfg, log, paths)
    if errors:
        row["status"] = f"notes written but OKF lint found {errors} error(s) - left on Remember"
        row["lint"] = out
        return
    removed = 0
    was = row.get("replaced") or row.get("flag")
    for it in playlist_items:
        Y.delete_item(yt, it["item_id"])
        removed += 1
    drop_from_queue(vid)
    row["filed"] = True
    head = row.get("status_head") or ("filed in Inbox" + (" (written earlier)" if not created else ""))
    row["status"] = head + ("; removed from Remember" if removed else "; was not on Remember")


# Properties a Daily Summary Save to Vault note carries that the detailed note replaces (plan section 6).
# Everything else -- tags, aliases, a description Organize rewrote, anything the owner added -- is kept; citation
# keys the old note lacks are filled in from the new note.
REPLACED_KEYS = ("generated", "summary_by", "content")


def queue_hash(vid):
    """generated_hash from the newest Save to Vault queue line for this video (None if none)."""
    h = None
    for q in read_queue():
        if q.get("video_id") == vid and q.get("generated_hash"):
            h = q["generated_hash"]
    return h


def replace_saved(cfg, log, vid, m, rec, row, other, N, VW):
    """A Daily Summary Save to Vault note exists for this video. If its generated sections are unchanged
    since it was saved (fingerprint from the queue line) and it isn't `verified`, replace it IN PLACE:
    keep its name, folder, aliases, tags and any other properties; replace the generated body sections
    and generated.by / summary_by / content; keep non-generated sections (e.g. '## My notes'); add the
    Transcript note beside it. Otherwise leave it alone and write '<name> (detailed)' + transcript to the
    Inbox, flagged for the owner. -> list of paths written."""
    root = Path(cfg["vault_path"])
    path = root / other["path"]
    text = path.read_text(encoding="utf-8")
    fm_text, body = VW._split(text)
    fm = VW.parse_frontmatter(fm_text) if fm_text else {}
    saved_hash = queue_hash(vid)
    now_hash = VW.generated_hash(body)
    edited = (not saved_hash) or now_hash != saved_hash or bool(fm.get("verified"))
    why = ("no fingerprint from the save (saved before 2026-09-26, or not through Save to Vault)" if not saved_hash
           else "marked verified" if fm.get("verified") else "a generated section was edited" if now_hash != saved_hash
           else "")

    def write():
        vroot = VW.vault_root()
        if edited:
            name = VW.unique_name(vroot, VW._clean(f"{path.stem} (detailed)"))
            tname = VW.unique_name(vroot, VN.transcript_name(name))
            smeta, sbody, tmeta, tbody = N.build(m, rec, name, tname)
            row["summary_words"] = N.word_count(sbody)
            out = [VW.write_named(VW.TYPE_VIDEO, smeta, sbody, name),
                   VW.write_named(VW.TYPE_TRANSCRIPT, tmeta, tbody, tname)]
            row["flag"] = (f"Your Save to Vault note \u201c{path.stem}\u201d was left as it is ({why}); the detailed "
                           f"version was written to the Inbox as \u201c{name}\u201d")
            row["status_head"] = f"Save to Vault note left alone ({why}); wrote \u201c{name}\u201d to the Inbox"
        else:
            name = path.stem
            folder = str(Path(other["path"]).parent).replace("\\", "/")
            tname = VW.unique_name(vroot, VN.transcript_name(name))
            smeta, sbody, tmeta, tbody = N.build(m, rec, name, tname)
            row["summary_words"] = N.word_count(sbody)
            newfm = dict(fm)
            for k in REPLACED_KEYS:
                newfm[k] = smeta[k]
            for k, v in smeta.items():
                if k not in newfm or newfm[k] in (None, "", []):
                    newfm[k] = v
            keep = [(h, t) for h, t in VW.split_body(body) if h is not None and h not in VW.DS_GENERATED_HEADINGS]
            newbody = sbody + "".join(f"\n\n## {h}\n{t.rstrip()}" for h, t in keep)
            tmp = path.with_suffix(".md.remember-tmp")
            tmp.write_text(VW.frontmatter(newfm) + "\n" + newbody.strip() + "\n", encoding="utf-8", newline="\n")
            os.replace(tmp, path)
            VW.verify_note(str(path), VW.TYPE_VIDEO)
            out = [other["path"], VW.write_named(VW.TYPE_TRANSCRIPT, tmeta, tbody, tname, folder=folder)]
            row["replaced"] = True
            row["status_head"] = f"replaced your Save to Vault note in place ({other['path']})"
        import hashlib
        VW.ledger_record(f"v:{vid}", {"summary": out[0], "transcript": out[1], "saved_at": VW.now_iso(),
                                      "replaced_save_to_vault": not edited,
                                      "generated_hash": hashlib.sha256(sbody.encode("utf-8")).hexdigest()})
        return out
    out = _with_lock(cfg, log, write)
    log(f"  {'replaced' if not edited else 'kept Save to Vault note; wrote'} {', '.join(out)}")
    return out


def drop_from_queue(vid):
    try:
        lines = QUEUE_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    keep = [l for l in lines if l.strip() and f'"{vid}"' not in l]
    if len(keep) != len([l for l in lines if l.strip()]):
        QUEUE_FILE.write_text("".join(k + "\n" for k in keep), encoding="utf-8")


def write_report(cfg, dry, started, rows, failures, lint, usage, stop=None):
    end = C.now(cfg)
    lines = [f"Remember intake on {C.pc_name()}, {started:%A %B %d, %Y} {started:%H:%M}-{end:%H:%M}"
             f"{' (dry run: no notes written, playlist unchanged)' if dry else ''}.", ""]
    if stop:
        lines += [f"Stopped early: {stop}.", ""]
    if failures:
        lines += ["## Failures", ""] + [f"- {f}" for f in failures] + [""]
    if rows:
        lines += ["## Videos", "", "| # | Video | Length | Status | Models | Summary words (target) | Transcript |",
                  "|---|---|---|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            t = str(r.get("title", "")).replace("|", "/")[:70]
            sw = f"{r['summary_words']:,} ({r['target_words']:,})" if r.get("summary_words") else ""
            tw = f"{r['transcript_words']:,} words; {r['transcript_how']}" if r.get("transcript_words") is not None and r.get("transcript_how") else ""
            lines.append(f"| {i} | [{t}](https://youtu.be/{r['video_id']}) | {_hm(r.get('duration'))} | "
                         f"{r.get('status', '')} | {r.get('models', '')}{' / ' + r['resolution'] if r.get('resolution') else ''} | {sw} | {tw.replace('|', '/')} |")
        lines.append("")
    if dry and rows:
        lines += [f"Preview notes: `reports/remember-preview/{started:%Y-%m-%d}/`", ""]
    if lint:
        lines += ["## OKF lint of the preview notes", "", "```", lint.get("output") or "-", "```", ""]
    if usage:
        lines += ["## Gemini usage today (Obsidian Youtube Summary project)", "", "```",
                  json.dumps(usage, indent=1), "```", ""]
    reports = C.APP / "reports"
    reports.mkdir(exist_ok=True)
    name = f"{started:%Y-%m-%d} remember{' (dry run)' if dry else ''}.md"
    fm = ("---\ntype: Reference\ntitle: \"Remember intake report\"\n"
          f"description: \"Remember intake run on {C.pc_name()}, {started:%Y-%m-%d}.\"\n"
          f"generated: {{ by: \"process:vault-remember\", at: \"{end.isoformat(timespec='seconds')}\" }}\n---\n\n")
    (reports / name).write_text(fm + "\n".join(lines), encoding="utf-8")
    (reports / f"remember-{started:%Y-%m-%d}.json").write_text(json.dumps(
        {"day": f"{started:%Y-%m-%d}", "dry_run": dry, "stop": stop, "failures": failures, "videos": rows,
         "usage": usage, "started": started.isoformat(timespec="seconds"), "ended": end.isoformat(timespec="seconds")},
        ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
