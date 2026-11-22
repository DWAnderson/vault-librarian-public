#!/usr/bin/env python3
"""Weekly vault housekeeping on this PC (Task Scheduler, Sunday 03:30).

  - citation backfill: up to weekly.backfill_citations older Web Clippings anywhere in the vault
    (page metadata only; frontmatter only) -- the owner's decision 8.4
  - archived duplicates awaiting a decision, orphan count, full OKF lint
  - checks that the newest vault snapshot opens; prunes logs > 60 days and work files > 14 days
Follows nightly.dry_run (test mode) until the nightly job goes live. Report: reports\\<date> weekly.md
"""
import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    dry = True if a.dry_run else False if a.live else bool(cfg["nightly"].get("dry_run", True))
    log = C.Log(cfg, "weekly")
    vault = Path(cfg["vault_path"])
    local = Path(cfg["local_root"])
    work = local / "work"
    started = C.now(cfg)
    out, fails = [], []
    log(f"=== weekly start ({'DRY RUN' if dry else 'LIVE'})")
    lock = C.VaultLock(cfg, f"{C.pc_name()} weekly")
    ok, note = lock.acquire()
    if not ok:
        log(note)
        (C.APP / "reports" / f"{started:%Y-%m-%d} weekly{' (dry run)' if dry else ''}.md").write_text(
            f"Weekly vault housekeeping on {C.pc_name()}, {started:%A %B %d, %Y}: **skipped (vault locked)**.\n\n"
            f"## Vault lock\n\n```\n{note}\n```\n", encoding="utf-8")
        log("=== weekly end: skipped")
        return 0
    if note:
        log(note)
        out.append(("Vault lock", note))
    if True:
        try:
            n = int(cfg.get("weekly", {}).get("backfill_citations", 0))
            if n:
                plan = work / "weekly-cite.json"
                rc, o = C.run(log, C.UV / "citation_fill.py", vault, "--all", "--fetch", "--max", n, "--plan", plan, timeout=3600)
                items = json.loads(plan.read_text(encoding="utf-8")).get("items", []) if plan.exists() else []
                if items:
                    rc2, o2 = C.run(log, C.VL / "apply_plan.py", vault, plan, *(["--dry-run"] if dry else []))
                    if rc2:
                        fails.append("backfill apply failed")
                out.append(("Citation backfill", f"{len(items)} clipping(s) {'would be' if dry else ''} updated\n" + "\n".join(o.strip().splitlines()[-4:])))
            rc, o = C.run(log, C.VL / "resolve_duplicates.py", vault, "--list")
            out.append(("Archived duplicates awaiting your decision", o.strip()[-1500:] or "none"))
            try:   # (E) concept check: likely duplicates -> "Same concept?" questions; the rest is reported
                cj = work / "weekly-concepts.json"
                pj = C.APP / "queue" / "concepts.json"
                rc, o = C.run(log, C.VL / "concept_lint.py", vault, "--pending", pj, "--json", cj)
                if cj.exists() and not dry:
                    import analyze as AN
                    res = json.loads(cj.read_text(encoding="utf-8"))
                    pend = AN.load_pending()
                    # add, don't replace: concept_merge.py also adds pairs (2026-11-13)
                    have = {frozenset((d["keep"], d["merge"])) for d in pend.get("dup_pairs") or []}
                    pend["dup_pairs"] = (pend.get("dup_pairs") or []) + [
                        d for d in res.get("duplicates", []) if frozenset((d["keep"], d["merge"])) not in have]
                    AN.save_pending(pend)
                out.append(("Concept check", o.strip()[-2000:] or "no concepts yet"))
            except Exception as e:
                fails.append(f"concept check: {type(e).__name__}: {e}")
            lj = work / "weekly-lint.json"
            C.run(log, C.VL / "okf_lint.py", vault, "--json", lj)
            lint = json.loads(lj.read_text(encoding="utf-8"))
            orphans = sum(1 for x in lint["findings"] if "orphan" in x["msg"])
            cnt = lint["counts"]
            out.append(("OKF compliance", f"{lint['notes']} notes: {cnt['ERROR']} errors, {cnt['WARN']} warnings; {orphans} orphan notes"))
        except Exception as e:
            fails.append(f"{type(e).__name__}: {e}")
        finally:
            lock.release()
    # snapshot integrity + pruning (no vault access needed)
    snaps = sorted((local / "snapshots").glob("DWA-*.zip"))
    if snaps:
        try:
            with zipfile.ZipFile(snaps[-1]) as z:
                bad = z.testzip()
                out.append(("Latest snapshot", f"{snaps[-1].name}: {len(z.namelist())} files, {'OK' if bad is None else 'CORRUPT at ' + bad}"))
                if bad:
                    fails.append(f"snapshot {snaps[-1].name} is corrupt")
        except Exception as e:
            fails.append(f"snapshot unreadable: {e}")
    else:
        out.append(("Latest snapshot", "none yet"))
    cutoff_logs, cutoff_work, pruned = time.time() - 60 * 86400, time.time() - 14 * 86400, 0
    for folder, cutoff in ((local / "logs", cutoff_logs), (work, cutoff_work)):
        for p in folder.rglob("*"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                pruned += 1
    out.append(("Housekeeping", f"{pruned} old log/work file(s) removed"))
    try:   # search quality on the test questions (plan: 2026-10-08 Vault search - plan.md, section 8; 2026-10-12)
        out.append(("Search test questions", search_check(cfg, log, fails)))
    except Exception as e:  # noqa: BLE001
        out.append(("Search test questions", f"not run: {type(e).__name__}: {e}"))

    status = "failed" if fails else "ok"
    md = [f"Weekly vault housekeeping on {C.pc_name()}, {started:%A %B %d, %Y}: **{status}**{' (dry run)' if dry else ''}.", ""]
    if fails:
        md += ["## Failures", ""] + [f"- {f}" for f in fails] + [""]
    for t, x in out:
        md += [f"## {t}", "", "```", x, "```", ""]
    (C.APP / "reports" / f"{started:%Y-%m-%d} weekly{' (dry run)' if dry else ''}.md").write_text("\n".join(md), encoding="utf-8")
    if fails:
        try:
            C.send_mail(cfg, "Vault: weekly housekeeping failed", "<pre>" + "\n".join(fails) + "</pre>")
        except Exception as e:
            log(f"email failed: {e}")
    log(f"=== weekly end: {status}")
    return 1 if fails else 0


def search_check(cfg, log, fails):
    """Re-run tests\\search_questions.json (keyword only and combined; no Claude usage, a few seconds) and compare
    with the go-live bar (combined top 10 >= 80% and above keyword only) and with last week's result
    (queue\\search-bench-history.jsonl). Below the bar, or 2+ questions fewer in the top 10, counts as a failure
    so the weekly email goes out. Returns one report line."""
    import vault_search
    summary, rows = vault_search.bench(cfg, modes=("kw", "hybrid"), log=log)
    h, k = summary.get("hybrid"), summary.get("kw")
    hist = C.APP / "queue" / "search-bench-history.jsonl"
    prev = None
    try:
        lines = [x for x in hist.read_text(encoding="utf-8").splitlines() if x.strip()]
        prev = json.loads(lines[-1]) if lines else None
    except OSError:
        pass
    rec = {"at": C.now(cfg).isoformat(timespec="minutes"), "summary": summary,
           "misses": sorted(q for q, m, r, _t in rows if m == "hybrid" and not (r and r <= 10))}
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    if not h:   # meaning vectors unavailable: keyword only
        return f"meaning search unavailable; keyword only: top 10 {k['top10']}/{k['of']}, top 5 {k['top5']}/{k['of']}"
    line = (f"combined: top 10 {h['top10']}/{h['of']}, top 5 {h['top5']}/{h['of']}, {h['avg_ms']} ms; "
            f"keyword only: top 10 {k['top10']}/{k['of']}; not in top 10: {', '.join(rec['misses']) or 'none'}")
    ph = ((prev or {}).get("summary") or {}).get("hybrid")
    if ph:
        line += f"; last week combined top 10 {ph['top10']}/{ph['of']}"
    if h["top10"] < 0.8 * h["of"] or h["top10"] <= k["top10"]:
        fails.append(f"vault search below the go-live bar: {line}")
    elif ph and ph["of"] == h["of"] and h["top10"] <= ph["top10"] - 2:
        fails.append(f"vault search got worse since last week: {line}")
    return line


if __name__ == "__main__":
    sys.exit(main())
