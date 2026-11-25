#!/usr/bin/env python3
"""Nightly vault maintenance on this PC (Task Scheduler, 02:30).

Steps: lock -> snapshot -> stub clippings re-fetched + clipping basics filled (jobs/clippings.py) ->
citation clean-up of new clippings -> STRONG duplicates archived ->
indexes + topic hubs -> OKF lint -> archived duplicates awaiting a decision -> report.
Cowork sync is skipped on this PC (config cowork_sync.enabled = false).

  pythonw nightly.py            uses config nightly.dry_run (true until the owner says go live)
  python  nightly.py --dry-run  force a dry run: snapshot + proposals only, nothing in the vault changes
  python  nightly.py --live     force a live run

Dry run: the report goes to reports\\ only; plans are applied with --dry-run; indexes/hubs are
not rebuilt. Live: the report is also written to the vault note in config report.vault_note.
Email (Graph) only when something needs the owner or a step failed.
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import traceback
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

SKIP_DIRS = {".trash", ".stversions", ".git", "node_modules"}


def snapshot(cfg, log):
    vault = Path(cfg["vault_path"])
    snaps = Path(cfg["local_root"]) / "snapshots"
    today = C.now(cfg)
    kind = "weekly" if today.weekday() == 6 else "daily"
    out = snaps / f"DWA-{today:%Y-%m-%d}-{kind}.zip"
    n = size = 0
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for p in vault.rglob("*"):
            rel = p.relative_to(vault)
            parts = rel.parts
            if any(x in SKIP_DIRS for x in parts) or not p.is_file():
                continue
            if parts[0] == ".obsidian" and p.name.startswith("workspace"):
                continue
            if p.name == ".vault-librarian.lock":
                continue
            try:
                z.write(p, rel.as_posix())
                n += 1
                size += p.stat().st_size
            except (PermissionError, OSError) as e:  # cloud-only / locked file
                log(f"  snapshot skipped {rel}: {e}")
    tmp.replace(out)
    for k, keep in (("daily", cfg["nightly"].get("snapshots_daily", 7)), ("weekly", cfg["nightly"].get("snapshots_weekly", 4))):
        old = sorted(snaps.glob(f"DWA-*-{k}.zip"))[:-keep]
        for o in old:
            o.unlink()
    log(f"  snapshot {out.name}: {n} files, {size/1e6:.1f} MB")
    return f"{out.name} ({n} files, {size/1e6:.1f} MB)"


def tail(text, n=12):
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    dry = True if a.dry_run else False if a.live else bool(cfg["nightly"].get("dry_run", True))
    log = C.Log(cfg, "nightly")
    vault = Path(cfg["vault_path"])
    work = Path(cfg["local_root"]) / "work"
    started = C.now(cfg)
    log(f"=== nightly start ({'DRY RUN' if dry else 'LIVE'}) vault={vault}")
    sections, needs, failures = [], [], []
    # Memory first (2026-11-19: a leaking Dell service used up the commit limit and three steps failed with
    # MemoryError / allocation errors). The report names the cause; failures get a pointer to it below.
    import memory_check
    mem_txt, mem_needs, mem_low = memory_check.check(cfg, log)
    mem_section = ("Memory", mem_txt)
    needs += mem_needs

    if not (vault / ".obsidian").is_dir():
        failures.append(f"vault not found at {vault}")
        return finish(cfg, log, dry, started, sections, needs, failures)
    lock = C.VaultLock(cfg, f"{C.pc_name()} nightly")
    ok, note = lock.acquire()
    # Wait for the lock instead of skipping (2026-10-11: skipped on 10-08 and 10-11 because the 02:00 usage
    # window still held it at 02:30). Retry every 5 min for up to 90 min; the task's 2-hour limit leaves room
    # for the ~10-minute run after that.
    waited = 0
    while not ok and waited < 90:
        log(f"{note}; waiting 5 min (waited {waited} min so far)")
        time.sleep(300)
        waited += 5
        ok, note = lock.acquire()
    if ok and waited:
        note = (note + "; " if note else "") + f"waited {waited} min for the vault lock"
    if not ok:
        note = f"{note} (waited {waited} min)"
        log(note)
        # The restart doesn't touch the vault, so do it even when the vault work is skipped (2026-10-08: the 02:00
        # usage window held the lock, the restart was skipped too, and the answer page ran day-old code all day).
        txt, rok = restart_server(log, dry)
        if not rok:
            needs.append("the vault web service didn't come back after its nightly restart")
        ctxt, cok = restart_server(log, dry, "Connector", CSTAMP)
        txt += f"; connector: {ctxt}"
        if not cok:
            needs.append("the Vault connector didn't come back after its nightly restart")
        return finish(cfg, log, dry, started, [mem_section, ("Vault lock", note), ("Vault service", txt)], needs, failures, skipped=note)
    sections.append(mem_section)
    if note:
        log(note)
        sections.append(("Vault lock", note))
    try:
        # 1. snapshot (always real: it only writes to %LOCALAPPDATA%)
        try:
            sections.append(("Snapshot", snapshot(cfg, log)))
        except Exception as e:
            failures.append(f"snapshot failed: {e}")

        # 1b. new Web Clipper notes (2026-09-28): share-page stubs re-fetched from the article's own URL,
        #     then missing basics (type, resource, generated) filled so they pass OKF before Organize files them
        import clippings as CL
        import browser_retry as BR   # 1c. held links retried in a headless browser, no sign-ins (2026-09-28)
        for title, fn in (("Stub clippings re-fetched", CL.fix_stubs), ("Clipping basics", CL.fill_basics),
                          ("Held links retried in a headless browser", BR.retry)):
            try:
                head, lines = fn(cfg, log, dry)
                sections.append((title, head + ("\n" + "\n".join(lines[:20]) if lines else "")))
            except Exception as e:  # noqa: BLE001
                failures.append(f"{title.lower()} failed: {type(e).__name__}: {e}")

        # 2. citation clean-up of new clippings (decision 8.3: automatic)
        plan = work / "nightly-cite.json"
        rc, out = C.run(log, C.UV / "citation_fill.py", vault, "--fetch", "--plan", plan)
        if rc:
            failures.append("citation_fill failed: " + tail(out, 3))
        else:
            n = len(json.loads(plan.read_text(encoding="utf-8")).get("items", []))
            if n:
                rc2, out2 = C.run(log, C.VL / "apply_plan.py", vault, plan, *(["--dry-run"] if dry else []))
                if rc2:
                    failures.append("citation plan apply failed: " + tail(out2, 3))
            sections.append(("Citation clean-up (new clippings)", f"{n} note(s) {'would be' if dry else ''} updated\n" + tail(out, 6)))

        # 3. STRONG duplicates archived automatically (standing rule); weak ones listed
        plan = work / "nightly-dups.json"
        rc, out = C.run(log, C.VL / "find_duplicates.py", vault, "--plan", plan, "--json", work / "nightly-dups-report.json")
        if rc:
            failures.append("find_duplicates failed: " + tail(out, 3))
        else:
            strong = [ln for ln in out.splitlines() if ln.startswith("STRONG")]
            weak = [ln.strip() for ln in out.splitlines() if ln.startswith("weak")]
            items = json.loads(plan.read_text(encoding="utf-8")).get("items", []) if plan.exists() else []
            if items:
                rc2, out2 = C.run(log, C.VL / "apply_plan.py", vault, plan, *(["--dry-run"] if dry else []))
                if rc2:
                    failures.append("duplicate archive failed: " + tail(out2, 3))
            txt = f"{len(items)} STRONG duplicate(s) {'would be' if dry else ''} archived; {len(weak)} weak match(es) for you to decide"
            if weak:
                txt += "\n" + "\n".join(weak[:15])
                needs.append(f"{len(weak)} possible duplicate pair(s) to decide")
            sections.append(("Duplicates", txt))

        # 3a. The owner's own corrections (2026-10-10): renames/moves he made in Obsidian are followed through and
        #     learned from; `fix:` requests on notes are carried out (corrections.py)
        try:
            import corrections as CR
            for title, fn in (("Your corrections", CR.detect), ("Fix requests", CR.fixes)):
                ls, ns = fn(cfg, log, dry)
                sections.append((title, "\n".join(ls[:30]) or "none"))
                needs += ns
        except Exception as e:  # noqa: BLE001
            failures.append(f"corrections failed: {type(e).__name__}: {e}")

        # 3b. names that tell a summary / analysis / transcript apart from the work (2026-10-10):
        #     "<work> (Summary)", "(Analysis)", "(Transcript)", "(Part 03 Analysis)"; links rewritten, old name kept
        try:
            import vault_names as VN
            rows = VN.enforce(cfg, log, dry)
            sections.append(("Note names", (f"{len(rows)} note(s) {'would be ' if dry else ''}renamed" + (
                "\n" + "\n".join(f"{o} -> {n.rsplit('/', 1)[-1]}" for o, n in rows[:15]) if rows else "")) if rows
                else "all summaries, analyses and transcripts carry their suffix"))
        except Exception as e:  # noqa: BLE001
            failures.append(f"note names failed: {type(e).__name__}: {e}")

        # 3c. each work's notes together and cross-linked (2026-10-13): 2+ part analyses -> the work's own
        #     folder holds the main note, Analysis, Transcript and parts; otherwise all side by side
        try:
            import families
            rows = families.regroup(cfg, log, dry)
            moved = [r for r in rows if " moved to " in r or "removed the empty" in r or "not moved" in r or "skipped" in r]
            sections.append(("Works kept together", (f"{len(rows)} change(s)" + ("\n" + "\n".join(moved[:25]) if moved else ""))
                             if rows else "every work's notes are together and cross-linked"))
        except Exception as e:  # noqa: BLE001
            failures.append(f"families regroup failed: {type(e).__name__}: {e}")

        try:   # combine overlapping concepts every analysis.concept_merge.every_days days (2026-11-13)
            import concept_merge
            line = concept_merge.run_if_due(cfg, log, dry)
            if line:
                sections.append(("Concepts combined", line))
        except Exception as e:  # noqa: BLE001
            failures.append(f"concept merge failed: {type(e).__name__}: {e}")

        try:   # one-item concepts: Claude suggests keep / merge / drop for the morning email (2026-11-23)
            import concept_review
            line = concept_review.run_if_due(cfg, log, dry)
            if line:
                sections.append(("Concepts reviewed", line))
        except Exception as e:  # noqa: BLE001
            failures.append(f"concept review failed: {type(e).__name__}: {e}")

        # 4. indexes and hubs (live only: they write files)
        if not dry:
            for s in ("build_indexes.py", "build_hubs.py"):
                rc, out = C.run(log, C.VL / s, vault)
                if rc:
                    failures.append(f"{s} failed: " + tail(out, 3))
            sections.append(("Indexes and topic hubs", "rebuilt"))
            try:   # phase B: meaning-based index for related suggestions (2026-10-06)
                if (cfg.get("analysis") or {}).get("candidates_from", "passages") in ("notes", "both"):
                    import related
                    sections.append(("Related-notes index", related.update_index(cfg, log)))
                else:   # retired 2026-10-12: Related candidates come from the search index (vault search, phase 4)
                    sections.append(("Related-notes index", "retired: analyses take Related candidates from the "
                                     "search index (analysis.candidates_from = passages)"))
            except Exception as e:  # noqa: BLE001
                failures.append(f"related index failed: {type(e).__name__}: {e}")
            try:   # vault search index: full compare + embedding (plan: 2026-10-08 Vault search - plan.md)
                import vault_search
                line = vault_search.nightly_section(cfg, log)
                sections.append(("Search index", line))
                if line.startswith("FAILED"):
                    failures.append("search index " + line)
            except Exception as e:  # noqa: BLE001
                failures.append(f"search index failed: {type(e).__name__}: {e}")
            try:   # documents kept in sync from the app folder, e.g. the User Manual (2026-10-12)
                import doc_sync
                dl, dn = doc_sync.sync(cfg, log)
                sections.append(("Document sync", "\n".join(dl) or "nothing configured"))
                needs += dn
            except Exception as e:  # noqa: BLE001
                failures.append(f"document sync failed: {type(e).__name__}: {e}")
        else:
            sections.append(("Indexes and topic hubs", "skipped (dry run)"))

        # 5. archived duplicates awaiting the owner's restore / merge / delete
        rc, out = C.run(log, C.VL / "resolve_duplicates.py", vault, "--list")
        if not rc:
            n = len([ln for ln in out.splitlines()
                     if re.match(r"(- )?(_Obsidian Internal[/\\])?Archives[/\\]", ln.strip(), re.I)])
            sections.append(("Archived duplicates awaiting your decision", tail(out, 12) or "none"))
            if n:
                needs.append(f"{n} archived duplicate(s) to restore, merge or delete")

        # 6. lint
        lj = work / "nightly-lint.json"
        rc, out = C.run(log, C.VL / "okf_lint.py", vault, "--json", lj)
        try:
            lint = json.loads(lj.read_text(encoding="utf-8"))
            cnt = lint["counts"]
            errs = [f"{x['file']}: {x['msg']}" for x in lint["findings"] if x["level"] == "ERROR"]
            txt = f"{lint['notes']} notes: {cnt['ERROR']} errors, {cnt['WARN']} warnings, {cnt['INFO']} info"
            if errs:
                txt += "\n" + "\n".join(errs[:20])
                needs.append(f"{cnt['ERROR']} OKF error(s)")
            sections.append(("OKF compliance", txt))
        except Exception:
            failures.append("okf_lint produced no report: " + tail(out, 3))

        # 7. links and emails the intake held (queue\held.json, shared with laptop sessions)
        try:
            hq = C.APP / "queue" / "held.json"
            held = json.loads(hq.read_text(encoding="utf-8")) if hq.exists() else {}
            waiting = [h for h in held.values() if h.get("status") == "held"]
            if waiting:
                br = [h for h in waiting if h.get("needs") == "browser"]
                you = [h for h in waiting if h.get("needs") != "browser"]
                txt = []
                if br:
                    txt.append(f"{len(br)} link(s) need the signed-in browser - say 'process the vault queue' in a laptop session:")
                    txt += [f"  {h.get('url')}" for h in br]
                if you:
                    txt.append(f"{len(you)} item(s) for you to decide (left in Remember):")
                    txt += [f"  {h.get('url') or h.get('subject')}: {str(h.get('why'))[:120]}" for h in you]
                sections.append(("Held from Remember", "\n".join(txt)))
                needs.append(f"{len(waiting)} held Remember item(s)")
            else:
                sections.append(("Held from Remember", "none"))
        except Exception as e:
            sections.append(("Held from Remember", f"could not read queue: {e}"))

        if not cfg.get("cowork_sync", {}).get("enabled"):
            sections.append(("Cowork sync", f"not run on {C.pc_name()}: " + cfg.get("cowork_sync", {}).get("reason", "")))
        if not dry:   # last: what the vault looks like after Claude's own changes, so tomorrow sees only the owner's
            try:
                import corrections as CR
                CR.save_state(cfg, log)
            except Exception as e:  # noqa: BLE001
                failures.append(f"saving the vault state failed: {type(e).__name__}: {e}")
    except Exception:
        failures.append("unexpected error:\n" + traceback.format_exc()[-1500:])
    finally:
        lock.release()
    txt, ok = restart_server(log, dry)          # after the vault work: the service picks up today's code
    sections.append(("Vault service", txt))
    if not ok:
        needs.append("the vault web service didn't come back after its nightly restart")
    try:   # Vault connector: last 24 hours, then a restart so it runs today's code (2026-10-12)
        line, cneeds = connector_section(cfg)
        ctxt, cok = restart_server(log, dry, "Connector", CSTAMP)
        sections.append(("Vault connector", f"{line}\nrestart: {ctxt}"))
        needs += cneeds
        if not cok:
            needs.append("the Vault connector didn't come back after its nightly restart")
    except Exception as e:  # noqa: BLE001
        sections.append(("Vault connector", f"report failed: {type(e).__name__}: {e}"))
    if failures and mem_low:   # say where to look first; the Memory section has the details
        failures.append("memory was short when the run started, which can make the steps above fail; "
                        "see the Memory section for the programs holding the most")
    return finish(cfg, log, dry, started, sections, needs, failures)


def connector_section(cfg, hours=24):
    """Summary of the Vault connector's last 24 hours from reports\\connector-log-<YYYY-MM>.md and
    queue\\connector\\ (plan: 2026-10-08 Vault search - plan.md, section 6). Returns (text, needs)."""
    import collections
    import datetime as _dt
    now = C.now(cfg)
    since = now - _dt.timedelta(hours=hours)
    tools, refused, outside, crashes, starts = collections.Counter(), 0, 0, [], 0
    for p in sorted((C.APP / "reports").glob("connector-log-*.md"))[-2:]:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$", ln)
            if not m:
                continue
            try:
                t = _dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
            except ValueError:
                continue
            if t < since:
                continue
            rest = m.group(2)
            if rest.startswith("tool "):
                tools[rest.split()[1]] += 1
            elif rest.startswith("http "):
                if "token=ok" not in rest:
                    refused += 1
                elif "anthropic_range=False" in rest and "caller=127.0.0.1" not in rest:
                    outside += 1
            elif rest.startswith("crashed"):
                crashes.append(rest[:160])
            elif rest.startswith("started"):
                starts += 1
    reqs = collections.Counter()
    failed = []
    for p in (C.APP / "queue" / "connector").glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            if _dt.datetime.fromisoformat(r.get("updated") or r["at"]) < since:
                continue
        except Exception:  # noqa: BLE001
            continue
        reqs[r.get("status")] += 1
        if r.get("status") == "failed":
            failed.append(f"{r['tool']} {r['id']}: {str(r.get('result'))[:120]}")
    calls = ", ".join(f"{k} {v}" for k, v in tools.most_common()) or "none"
    text = (f"last {hours} h: {sum(tools.values())} tool call(s) ({calls}); "
            f"requests: {', '.join(f'{k} {v}' for k, v in reqs.items()) or 'none'}; "
            f"refused without a valid token: {refused}; token OK from outside Anthropic's range: {outside}; "
            f"starts: {starts}; crashes: {len(crashes)}")
    if crashes:
        text += "\n" + "\n".join(crashes[-3:])
    if failed:
        text += "\nfailed requests:\n" + "\n".join(failed[:5])
    needs = []
    if failed:
        needs.append(f"{len(failed)} Vault connector request(s) failed")
    if outside:
        needs.append(f"{outside} connector call(s) with the right token came from outside Anthropic's addresses")
    return text, needs



STAMP = C.APP / "queue" / "server-restarted.txt"   # when restart_server last brought the service back
CSTAMP = C.APP / "queue" / "connector-restarted.txt"   # the same for the Vault connector (2026-10-12)


def code_mtime():
    """Newest change to the code the web service loads (jobs, service, vault-librarian scripts)."""
    newest = 0.0
    for d in (C.APP / "jobs", C.APP / "service", C.VL):
        for f in d.glob("*.py"):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
    return newest


def restart_if_stale(log, dry):
    """Restart the web service when its code changed after its last restart (called by organize --prep at 06:00,
    so the morning's answer page always runs the current code even if the nightly restart was missed)."""
    def last(p):
        try:
            return float(p.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0.0
    newest = code_mtime()
    txt, ok = ("vault service already current", True) if newest <= last(STAMP) else restart_server(log, dry)
    if newest > last(CSTAMP):   # the connector loads vault_search and the jobs' code at start, like the service
        ctxt, cok = restart_server(log, dry, "Connector", CSTAMP)
        txt, ok = f"{txt}; connector: {ctxt}", ok and cok
    return txt, ok


def restart_server(log, dry, task="Server", stamp=None):
    """Restart the vault web service (Task Scheduler task Server) so it runs the current code: it loads its code
    only when it starts, so without this a change to the jobs could break the answer page (2026-09-30).
    task="Connector" restarts the Vault connector the same way (2026-10-12). Returns (report text, ok)."""
    import os
    import subprocess
    stamp = stamp or STAMP
    if dry or os.name != "nt":
        return "not restarted (dry run, or not on Windows)", True
    tp = f"-TaskPath '{C.task_path()}' -TaskName {task}"
    if task != "Server":   # a task that may not be installed yet
        chk = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                              f"if (Get-ScheduledTask {tp} -ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}"],
                             capture_output=True, text=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if "yes" not in (chk.stdout or ""):
            return f"{task} task not installed", True
    ps = (f"Stop-ScheduledTask {tp} -ErrorAction SilentlyContinue; Start-Sleep -Seconds 3; "
          f"Start-ScheduledTask {tp}; Start-Sleep -Seconds 8; (Get-ScheduledTask {tp}).State")
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:  # noqa: BLE001
        return f"restart failed: {e}", False
    state = ((p.stdout or "").strip().splitlines() or ["?"])[-1].strip()
    log(f"{task} task restarted; task state {state}")
    ok = state == "Running"
    if ok:
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
    return (f"restarted; task state {state}" + ("" if ok else f" ({(p.stderr or '').strip()[-300:]})")), ok


def finish(cfg, log, dry, started, sections, needs, failures, skipped=None):
    end = C.now(cfg)
    status = "failed" if failures else ("skipped (vault locked)" if skipped else ("needs you" if needs else "all clear"))
    head = (f"Nightly vault maintenance on {C.pc_name()}, {started:%A %B %d, %Y}: **{status}**"
            f"{' (dry run: nothing in the vault was changed)' if dry else ''}. "
            f"Ran {started:%H:%M}-{end:%H:%M}.")
    body = [head, ""]
    if failures:
        body += ["## Failures", ""] + [f"- {f}" for f in failures] + [""]
    if needs:
        body += ["## Needs you", ""] + [f"- {n}" for n in needs] + [""]
    for title, txt in sections:
        body += [f"## {title}", "", "```", txt.strip() or "-", "```", ""]
    md = "\n".join(body)
    fm = ("---\ntype: Reference\ntitle: \"Vault Maintenance Report\"\n"
          f"description: \"Latest nightly maintenance run on {C.pc_name()} ({status}), {started:%Y-%m-%d}.\"\n"
          f"generated: {{ by: \"process:vault-nightly\", at: \"{end.isoformat(timespec='seconds')}\" }}\n---\n\n")
    reports = C.APP / "reports"
    reports.mkdir(exist_ok=True)
    (reports / f"{started:%Y-%m-%d} nightly{' (dry run)' if dry else ''}.md").write_text(fm + md, encoding="utf-8")
    if not dry and not skipped:   # a skipped run must not write to the vault
        note = Path(cfg["vault_path"]) / cfg["report"]["vault_note"]
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(fm + "# Vault Maintenance Report\n\n" + md, encoding="utf-8")
    # Structured summary for the 06:00 "Obsidian Vault Update" email (jobs/update_email.py), which now
    # carries the "needs you" items. This job emails on its own only when a step FAILED.
    short = {"Note names": "Note names", "Your corrections": "Your corrections", "Fix requests": "Fix requests",
             "Snapshot": "Backup snapshot", "Citation clean-up (new clippings)": "Citation clean-up",
             "Duplicates": "Duplicates", "Indexes and topic hubs": "Indexes and hubs", "OKF compliance": "Compliance",
             "Cowork sync": "Cowork sync"}
    health = []
    for title, txt in sections:
        if title in short:
            first = (txt.strip().splitlines() or ["-"])[0].strip()
            first = re.sub(r"^not run on " + re.escape(C.pc_name()) + r":.*", f"not run on {C.pc_name()} (laptop only)", first)
            health.append([short[title], re.sub(r"\s+", " ", first)])
    summary = {"day": f"{started:%Y-%m-%d}", "status": status, "dry_run": dry, "needs": needs, "failures": failures,
               "health": health, "started": started.isoformat(timespec="seconds"), "ended": end.isoformat(timespec="seconds")}
    if not dry:
        (reports / f"nightly-{started:%Y-%m-%d}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    if failures:
        try:
            import update_email as U
            items = "".join(f"<li style=\"margin-bottom:6px;\">{html.escape(x)}</li>" for x in failures)
            body = (f"<p style=\"margin:0 0 10px 0;\">Last night's vault maintenance on {C.pc_name()} hit a problem in "
                    f"{len(failures)} step{'s' if len(failures) != 1 else ''}; the other steps ran normally.</p><ul style=\"margin:0;padding-left:20px;"
                    f"font-size:11pt;\">{items}</ul><p style=\"margin:10px 0 0 0;\">What to do: in a Claude session on {C.pc_name()}, "
                    f"say <b>&ldquo;check last night's vault maintenance.&rdquo;</b> The full report is in "
                    f"<i>Claude/Other/Vault Maintenance Report</i> and {C.pc_name()}'s reports folder.</p>")
            code = C.send_mail(cfg, f"[{U.NOTICE} {started:%Y-%m-%d}] Overnight maintenance failed",
                               U.simple(f"{started:%A, %B} {started.day}, {started.year}", "Obsidian Vault: overnight maintenance",
                                        body, f"{len(failures)} step{'s' if len(failures) != 1 else ''} failed", "bad"))
            log(f"failure email sent ({code})")
        except Exception as e:
            log(f"email failed: {e}")
    log(f"=== nightly end: {status}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
