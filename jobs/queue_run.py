#!/usr/bin/env python3
"""Intake queue runner (plan: 2026-09-28 Intake queue for files and email - plan.md).

  python queue_run.py                  dry run unless queue.enabled is true in config
  python queue_run.py --dry-run        collect, order and report only: nothing saved, written,
                                       moved or sent to a model
  python queue_run.py --live           force live (refused while intake.py is still live and
                                       queue.enabled is false, so the two never both work Remember)
  --no-collect                         skip the collectors (work on what is queued)
  --routes cheap|costly|all            default cheap; costly (book, summary) only inside
                                       queue.night_window unless --force
  --max N                              items this run (default queue.per_run)

Steps: collect (mail now; the folder collector arrives in phase 2) -> work the ready items in
queue order (vqueue.ordered) within the budget -> finish (move an email to Deleted Items only when
its note and every attachment's note exist) -> report in reports\\<date> queue[ (dry run)].md.
Phase 1 routes: email and link (same results as intake.py). File routes arrive in phase 2.
"""
import argparse
import copy
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import vqueue as Q  # noqa: E402
import llm  # noqa: E402
import intake as I  # noqa: E402
import linkjob  # noqa: E402
import collect_mail  # noqa: E402
import held_bridge  # noqa: E402
import collect_folder  # noqa: E402
import routes as RT  # noqa: E402
import analyze as AN  # noqa: E402
import pacing  # noqa: E402

CHEAP_ONLY = "cheap"


class StopModels(Exception):
    pass


def in_window(cfg, q):
    a, b = q["night_window"]
    t = f"{C.now(cfg):%H:%M}"
    if (a <= t <= b if a <= b else (t >= a or t <= b)):
        return True
    try:   # or an extra run window the owner booked (usage_plan, 2026-10-06)
        import usage_plan
        return usage_plan.active_window(cfg) is not None
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- routes
def do_email(cfg, log, store, item, dry, work):
    pay = store.payload_root / item["id"] / "message.json"
    msg = json.loads(pay.read_text(encoding="utf-8")) if pay.exists() else item.get("_msg")
    if not msg:
        return {"ok": False, "why": "captured email is missing on this PC", "needs": "you"}
    ccfg = cfg
    if (item.get("manual") or {}).get("released"):  # The owner released a confidential hold
        ccfg = copy.deepcopy(cfg)
        ccfg["intake"]["confidential_domains"] = []
    c = I.classify(msg, ccfg)
    if c["kind"] != "email":  # a link or held email that was re-routed to email by hand
        c = {"md": c.get("md", ""), "words": c.get("words", 0), "long": c.get("words", 0) >
             int(cfg["intake"].get("verbatim_max_words", 2000))}
    hint = (item.get("work") or {}).get("route_hint")
    if hint in ("article", "document", "recipe", "manual") and Q.qcfg(cfg)["files_enabled"]:
        # an email in Remember/Recipes (etc.): its body is handled like a dropped file of that route
        if dry:
            return {"ok": True, "dry": True, "would": f"email body -> {hint} note"}
        pay = store.payload(item["id"])
        (pay / "text.md").write_text(c["md"], encoding="utf-8")
        (pay / "extract.json").write_text(json.dumps({
            "title": msg.get("subject"), "url": "mid:" + (msg.get("internetMessageId") or "").strip("<>"),
            "words": c["words"], "published": (msg.get("receivedDateTime") or "")[:10] or None,
            "no_original": True}, ensure_ascii=False), encoding="utf-8")
        return RT.ROUTES[hint](cfg, log, store, item, dry, work)
    if hint in (None, "auto", "") and not (item.get("manual") or {}).get("route"):
        res = do_comic_email(cfg, log, store, item, dry, work, msg)
        if res:
            return res
    if dry:
        return {"ok": True, "dry": True, "would": f"{c['words']} words -> " +
                ("claude summary" if c["long"] else "verbatim") + " Email note"}
    reqs = []
    if c["long"]:
        reqs.append({"id": "summary", "instructions": I.SUMMARY_EMAIL, "material": c["md"][:60000]})
    reqs.append({"id": "describe", "instructions": I.DESCRIBE, "material": c["md"][:6000], "timeout": 120})
    ans = llm.run(cfg, "email", reqs, store.payload(item["id"]) / "answers", log, item["id"])
    body, content = c["md"], "full"
    if c["long"]:
        a = ans.get("summary") or {}
        if "text" not in a:
            if a.get("limit") or a.get("skipped"):
                raise StopModels(a.get("error"))
            return {"ok": False, "why": "summary failed: " + str(a.get("error"))}
        body, content = a["text"], "summary"
    d = (ans.get("describe") or {}).get("text") or ""
    e = {"id": item["id"], "subject": msg.get("subject") or "(no subject)", "from": I.addr(msg.get("from")),
         "to": [I.addr(x) for x in msg.get("toRecipients") or []],
         "cc": [I.addr(x) for x in msg.get("ccRecipients") or []],
         "received": msg.get("receivedDateTime"), "internet_message_id": msg.get("internetMessageId"),
         "web_link": msg.get("webLink"), "word_count": c["words"], "content": content,
         "description": d.splitlines()[0][:300] if d else "",
         "attachments": item.get("work", {}).get("attachment_names") or [], "body": body}
    ej = work / f"email-{item['id']}.json"
    ej.write_text(json.dumps([e], ensure_ascii=False), encoding="utf-8")
    rc, out = C.run(log, C.VL / "email_to_note.py", cfg["vault_path"], ej)
    for ln in out.splitlines():
        if ln.startswith("{"):
            x = json.loads(ln)
            if x.get("ok"):
                return {"ok": True, "note": x.get("note")}
            return {"ok": False, "why": "note failed: " + str(x.get("error"))}
    return {"ok": False, "why": "email_to_note gave no result: " + out[-200:]}


def do_comic_email(cfg, log, store, item, dry, work, msg):
    """A web-comic newsletter (SMBC and the like; 2026-10-18): file the cartoon itself as an Image
    note - from the comic's web page when the email links to it (hover text, bonus panel), else the
    comic image(s) in the email - instead of an Email note of tracking links. None: not a comic."""
    sys.path.insert(0, str(C.UV))
    try:
        import comics
        cm = comics.from_email(msg, fetch=not dry)
    except Exception as e:  # noqa: BLE001
        log(f"  comic check skipped: {type(e).__name__}: {e}")
        return None
    if not cm:
        return None
    imid = (msg.get("internetMessageId") or "").strip("<>")
    via = ("mid:" + imid) if imid else None
    if cm.get("url"):
        log(f"  comic newsletter -> {cm['url']}")
        res = linkjob.save_url(cfg, log, cm["url"], item.get("doug_note", ""), via, dry)
        if res.get("ok") and res.get("duplicate"):
            res["status"] = "duplicate"
        if res.get("ok") or not cm.get("images"):
            return res
        # the page couldn't be saved (e.g. HTTP 500): file the cartoon from the email instead of
        # holding a comic for a retry (2026-10-24)
        log(f"  comic page not saved ({res.get('why')}); filing the comic image(s) from the email")
    if not cm.get("images"):
        return None
    if dry:
        return {"ok": True, "dry": True, "would": f"comic newsletter -> Image note ({len(cm['images'])} image(s) from the email)"}
    pub = cm.get("publication")
    meta = {"title": cm.get("title"), "resource": cm.get("url") or via or cm["images"][0]["url"], "work_type": "image",
            "publication": pub, "author": cm.get("author") or [],
            "published": (msg.get("receivedDateTime") or "")[:10] or None,
            "description": f"{pub + ' webcomic' if pub else 'Webcomic'} from the newsletter email “{cm.get('title')}”."}
    pj = work / f"comic-{item['id']}.json"
    pj.write_text(json.dumps({"category": "image", "comic": True, "images": cm["images"],
                              "image_url": cm["images"][0]["url"], "caption": cm["images"][0].get("hover"),
                              "meta": {k: v for k, v in meta.items() if v not in (None, [], "")}},
                             ensure_ascii=False), encoding="utf-8")
    args = [cfg["vault_path"], pj] + (["--captured-via", via] if via else [])
    rc, out = C.run(log, C.UV / "url_to_note.py", *args, timeout=180)
    for ln in out.splitlines()[::-1]:
        if ln.startswith("{"):
            x = json.loads(ln)
            if x.get("ok"):
                return {"ok": True, "note": x.get("note")}
            if x.get("duplicate"):
                return {"ok": True, "note": x["duplicate"], "duplicate": True, "status": "duplicate"}
            return {"ok": False, "why": "comic note failed: " + str(x.get("reason") or x)}
    return {"ok": False, "why": "comic note failed: " + out[-200:]}


def do_link(cfg, log, store, item, dry, work):
    imid = ((item.get("source_ref") or {}).get("internet_message_id") or "").strip("<>")
    # an email's subject helps find a podcast episode by title (dropbox file names are just dates)
    hint = item.get("title") if item.get("source") != "folder" else None
    res = linkjob.save_url(cfg, log, item["url"], item.get("doug_note", ""), ("mid:" + imid) if imid else None, dry,
                           title_hint=hint)
    if res.get("ok") and res.get("duplicate"):
        res["status"] = "duplicate"
    if res.get("ok") and res.get("queued"):   # YouTube: handed to the 02:05 Remember job (linkjob, 2026-09-28)
        res["why_done"], res["note"] = res.get("note"), None
    return res


ROUTE_FN = {"email": do_email, "link": do_link, "analyze": AN.do_analyze, "analyze_long": AN.do_analyze}   # analyze_long: phase C   # analyze: concept analysis plan, phase A


def work_item(cfg, log, store, item, dry, work):
    route = Q.route_of(item)
    fn = ROUTE_FN.get(route)
    if not fn and route in RT.ROUTES and item.get("kind") == "file":
        if not (item.get("work") or {}).get("extracted"):
            return {"ok": False, "later": True, "why": "not extracted yet"}
        again = lambda it: work_item(cfg, log, store, it, dry, work)
        try:
            return RT.ROUTES[route](cfg, log, store, item, dry, work, again)
        except RT.LimitHit as e:
            raise StopModels(e)
    if not fn:
        return {"ok": False, "later": True, "why": f"no handler for route '{route}'"}
    try:
        return fn(cfg, log, store, item, dry, work)
    except RT.LimitHit as e:
        raise StopModels(e)


# ---------------------------------------------------------------- finish
def finish(cfg, log, store, s, base, dry):
    """Close containers whose attachments are all finished; move an email to Deleted Items only
    when its own note and every attachment's note exist. Returns report rows."""
    rows = []
    for it in store.all():
        if it.get("source") == "folder" and not it.get("parent"):
            rows += finish_file(cfg, log, store, it, dry)
            continue
        if it.get("source") != "mail" or it.get("parent"):
            continue
        kids = store.children(it)
        open_kids = [k for k in kids if k["status"] in Q.OPEN]
        if it["status"] == "waiting" and not open_kids and kids:
            if dry:
                rows.append((it["title"], "finish", "would close: all attachments finished"))
                continue
            Q.set_status(it, "done", "all attachments finished", cfg=cfg)
            it["result"]["notes"] = sum((k["result"].get("notes") or [] for k in kids), [])
            store.save(it)
        remove = it["status"] in ("done", "duplicate") or (it["status"] in ("skipped", "cancelled")
                                                           and it["result"].get("remove_source"))   # cancelled: dropped in the daily email
        if not remove or it["result"].get("source_removed"):
            continue
        if open_kids:
            if it["status"] != "waiting" and not dry:
                Q.set_status(it, "waiting", f"{len(open_kids)} attachment(s) still in progress", cfg=cfg)
                store.save(it)
            continue
        if dry:
            rows.append((it["title"], "finish", "would move the email to Deleted Items"))
            continue
        mid = it["source_ref"]["message_id"]
        rm = s.post(f"{base}/messages/{mid}/move", json={"destinationId": "deleteditems"}, timeout=30)
        if rm.ok or rm.status_code == 404:
            it["result"]["source_removed"] = True
            Q.event(it, "email moved to Deleted Items", cfg)
            store.save(it)
            rows.append((it["title"], "finish", "email removed from Remember"))
        else:
            rows.append((it["title"], "finish", f"move failed: HTTP {rm.status_code}"))
    return rows


def finish_file(cfg, log, store, it, dry):
    """A dropbox file whose note (and every attachment's, for .msg/.eml) is written: remove it from the
    dropbox (with its sidecar .txt). A dropped .msg/.eml is first kept in attachments/Sources and linked
    from its Email note. Returns report rows."""
    kids = store.children(it)
    if it["status"] == "waiting" and kids and not [k for k in kids if k["status"] in Q.OPEN]:
        if dry:
            return [(it["title"], "finish", "would close: all attachments finished")]
        Q.set_status(it, "done", "all attachments finished", cfg=cfg)
        it["result"]["notes"] = sum((k["result"].get("notes") or [] for k in kids), [])
        store.save(it)
    remove = it["status"] in ("done", "duplicate") or (it["status"] == "skipped" and it["result"].get("remove_source"))
    if not remove or it["result"].get("source_removed"):
        return []
    if [k for k in kids if k["status"] in Q.OPEN]:
        return []
    droot = Path((Q.qcfg(cfg).get("dropbox_path") or "").strip() or "\0")
    src = droot / it["source_ref"]["path"]
    try:   # only ever delete inside the configured, absolute dropbox (2026-09-28 incident)
        safe = droot.is_absolute() and droot.is_dir() and droot.resolve() in src.resolve().parents
    except (OSError, ValueError):
        safe = False
    if not safe:
        return [(it["title"], "finish", f"not removed: {src} is not inside the configured dropbox")]
    if dry:
        return [(it["title"], "finish", f"would remove {src.name} from the dropbox")]
    vault = Path(cfg["vault_path"])
    try:
        if it.get("kind") == "email" and it["result"].get("notes") and src.exists() and it["route"] == "email":
            import shutil
            note = vault / it["result"]["notes"][0]
            src_rel = C.vrel(vault, "attachments") + "/Sources"
            dest_dir = vault / src_rel
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / (note.stem + src.suffix.lower())
            n = 2
            while dest.exists():
                dest, n = dest_dir / f"{note.stem} {n}{src.suffix.lower()}", n + 1
            shutil.copy2(src, dest)
            with open(note, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"\n## Original\n\n[[{src_rel}/{dest.name}|{dest.name}]]\n")
            it["result"]["original"] = f"{src_rel}/{dest.name}"
        if src.exists():
            src.unlink()
        side = src.with_suffix(".txt")
        if src.suffix.lower() != ".txt" and side.exists():
            side.unlink()
    except OSError as e:
        return [(it["title"], "finish", f"couldn't remove {src.name}: {e}")]
    it["result"]["source_removed"] = True
    Q.event(it, "removed from the dropbox", cfg)
    store.save(it)
    return [(it["title"], "finish", f"{src.name} removed from the dropbox")]


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    ap.add_argument("--no-collect", action="store_true")
    ap.add_argument("--routes", choices=("cheap", "costly", "all"), default="cheap")
    ap.add_argument("--max", type=int)
    ap.add_argument("--force", action="store_true", help="costly routes outside the night window")
    ap.add_argument("--window", action="store_true", help="QueueWindow task: run only inside an extra window you booked")
    a = ap.parse_args(argv)
    cfg = C.load_config()
    q = Q.qcfg(cfg)
    if C.paused(cfg) and not a.force:   # home page "Pause until" (2026-11-12); QueueNow by hand still works
        return 0
    win = None
    if a.window:
        try:   # every 15 min: while the Claude sign-in is broken, re-test it and catch up once it works (2026-10-24)
            import claude_auth
            claude_auth.recover(cfg)
        except Exception:  # noqa: BLE001
            pass
        import usage_plan
        win = usage_plan.active_window(cfg)
        if not win:
            return 0                       # no extra window booked for now: nothing to do, no log noise
        a.routes = "costly"
    dry = True if a.dry_run else False if a.live else not bool(q["enabled"])
    log = C.Log(cfg, "queue")
    now = C.now(cfg)
    if not dry and not q["enabled"] and cfg.get("intake", {}).get("enabled"):
        log("refused: intake.py is live and queue.enabled is false - switch over first (plan, phase 1)")
        return 2
    store = Q.Store(cfg)
    work = Path(cfg["local_root"]) / "work" / "queue"
    work.mkdir(parents=True, exist_ok=True)
    log(f"=== queue start ({'DRY RUN' if dry else 'LIVE'}), routes {a.routes}")
    s = base = None
    try:
        s, base = I.graph(cfg)
    except Exception as e:
        log(f"Graph error: {e}")
        C.mail_health(cfg, "read", False, f"{type(e).__name__}: {e}")
    lock = C.VaultLock(cfg, f"{C.pc_name()} queue")
    if not dry:
        ok, note = lock.acquire()
        if not ok and a.routes != CHEAP_ONLY:   # the night run waits up to 40 min for an evening job to finish
            import time
            for _ in range(40):
                time.sleep(60)
                ok, note = lock.acquire()
                if ok:
                    break
        if not ok:
            log(note)
            return 0
    crow, prow, frow, hrow = [], [], [], []
    P = None
    try:
        new, present = [], None
        if s and not a.no_collect:
            try:   # 2026-11-12: mail being down skips the mail part only; the dropbox and the queue go on
                crow, new, present = collect_mail.collect(cfg, log, store, s, base, dry, skip=held_bridge.legacy_ids())
                C.mail_health(cfg, "read", True)
            except Exception as e:  # noqa: BLE001
                log(f"Graph error (collect): {type(e).__name__}: {str(e)[:200]}")
                C.mail_health(cfg, "read", False, f"{type(e).__name__}: {e}")
                crow, new, present, s = [], [], None, None
        if not a.no_collect:
            frows, fnew = collect_folder.collect(cfg, log, store, dry)
            crow += frows
            new += fnew
            try:   # clean display names with author/source for items waiting to be filed (the owner's rules, 2026-10-06)
                import titlecheck
                crow += titlecheck.retitle_pending(cfg, log, store, int(q.get("retitle_per_run", 25)))
            except Exception as e:
                log(f"retitle error: {type(e).__name__}: {e}")
            try:   # held links from blocked sites that the owner has re-clipped himself (2026-10-06)
                import reclip
                crow += reclip.check(cfg, log)
            except Exception as e:
                log(f"reclip check error: {type(e).__name__}: {e}")
            try:   # new filed notes of the analysis types (only when analysis.auto is on)
                arows, anew = AN.collect(cfg, log, store, dry)
                crow += arows
                new += anew
            except Exception as e:
                log(f"analysis collect error: {type(e).__name__}: {e}")
        hrow = held_bridge.sync(cfg, log, store, dry, present)   # results from the headless retry / laptop
        xrow = []
        if not dry:   # held for a reason since fixed (a format now handled, OCR that found nothing): try once more
            for x in store.all():
                ext = (x.get("file") or {}).get("ext") or ""
                why = x.get("why") or ""
                if (x.get("status") == "held" and x.get("kind") == "file" and not (x.get("work") or {}).get("format_retry")
                        and ((why.endswith("files aren't handled yet") and ext in RT.X.SUPPORTED)
                             or why == "no text could be read from the file")):
                    x["work"] = {"format_retry": True}
                    Q.set_status(x, "new", "trying again: this kind of file is handled now", cfg=cfg)
                    store.save(x)
        to_extract = Q.ordered(store.all() + (new if dry else []), cfg, statuses=("new",), now=now)
        for x in [i for i in to_extract if i.get("kind") == "file"][:int(q["extract_per_run"])]:
            try:
                what = RT.extract_item(cfg, log, store, x, dry)
            except RT.LimitHit as e:   # model budget/usage limit (title check): not a failure - stays "new", next run retries (2026-10-18)
                Q.event(x, f"waits: {e}", cfg)
                if not dry:
                    store.save(x)
                xrow.append((x.get("title") or "?", "extract", f"waits: {e}"))
                break
            except Exception as e:
                log(traceback.format_exc()[-800:])
                Q.set_status(x, "held", f"extraction error: {type(e).__name__}: {e}", "you", cfg)
                what = x["why"]
            if not dry:
                store.save(x)
            xrow.append((x.get("title") or "?", "extract", what))
        crow += xrow
        pool = store.all() + ([x for x in new] if dry else [])
        cands = Q.ordered(pool, cfg, statuses=("ready", "failed", "working"), now=now)   # "working": a book cut off mid-run (the lock means no other run has it)
        costly_ok = a.routes in ("costly", "all") and (a.force or in_window(cfg, q))
        pc = pacing.pcfg(cfg)
        # 2026-10-06 (the owner): the night run also works through cheap items (analyses...) once the books are
        # started, within tonight's weekly allowance (pacing.night_cheap); books go first at night.
        night_cheap = bool(costly_ok and pc["enabled"] and pc.get("night_cheap", True) and a.routes == "costly"
                           and (a.force or in_window(cfg, q)))
        cands = [x for x in cands if (Q.route_of(x) in Q.COSTLY and costly_ok) or
                 (Q.route_of(x) not in Q.COSTLY and (a.routes in ("cheap", "all") or night_cheap))]
        if night_cheap:
            cands.sort(key=lambda x: Q.route_of(x) not in Q.COSTLY)          # stable: books first, queue order kept
        if costly_ok:   # alternate long analyses with books/summaries so neither kind waits on the other (2026-10-06)
            longs = [x for x in cands if Q.route_of(x) == "analyze_long"]
            bks = [x for x in cands if Q.route_of(x) in Q.COSTLY and Q.route_of(x) != "analyze_long"]
            rest = [x for x in cands if Q.route_of(x) not in Q.COSTLY]
            mixed = []
            for i in range(max(len(longs), len(bks))):
                mixed += [x[i] for x in (bks, longs) if i < len(x)]
            cands = mixed + rest
        budget = a.max or (int(pc.get("night_items_max", 80)) if night_cheap else int(q["per_run"]))
        night = f"{C.now(cfg) - __import__('datetime').timedelta(hours=12):%Y-%m-%d}"   # 00:30-01:55 counts as the evening before
        if win:
            night = "w" + win["start"]     # an extra window has its own books_max count
        # Pacing (2026-09-29): costly work is paced by Claude plan usage; fixed limits apply when usage is unknown.
        P = None
        if costly_ok and not dry and pc["enabled"] and (night_cheap or any(Q.route_of(x) in Q.COSTLY for x in cands[:budget])):
            if not win:
                try:
                    import usage_plan
                    win = usage_plan.active_window(cfg)
                except Exception:  # noqa: BLE001
                    win = None
            P = pacing.Night(cfg, log, q["night_window"][0], window=win)
        if not pc["enabled"]:
            books_cap, calls_cap = int(q["books_per_night"]), int(q["claude_calls_per_day"])
        elif P and P.ok:
            books_cap, calls_cap = int(pc["books_max"]), int(pc["calls_max"])
        else:
            books_cap = int(pc["books_fallback"])
            calls_cap = int(q["claude_calls_per_day"]) if a.routes == CHEAP_ONLY else int(pc["calls_fallback"])
        if llm.pool_now(cfg) == "night" or (costly_ok and a.force):
            cfg["_night_calls_cap"] = calls_cap        # llm.run enforces the same cap (2026-10-06)
        if llm.pool_now(cfg) == "day" and a.routes == CHEAP_ONLY:
            calls_cap = int(q["claude_calls_per_day"])
        busy = pacing.session_busy(cfg) if (a.routes == CHEAP_ONLY and not dry and cands) else None
        models_left = calls_cap - llm.calls_today(cfg)
        books_left = books_cap - sum(
            1 for i in store.all() if Q.route_of(i) in Q.COSTLY and (i.get("work") or {}).get("night") == night
            and i["status"] in Q.FINAL)
        for x in cands[:budget]:
            title = x.get("title") or "?"
            if models_left <= 0 and not dry:
                prow.append((title, Q.route_of(x), f"waits: daily model budget used ({calls_cap})"))
                continue
            if busy:
                prow.append((title, Q.route_of(x), f"waits: {busy}"))
                continue
            costly = Q.route_of(x) in Q.COSTLY
            if not costly and night_cheap and not dry and not a.force and not in_window(cfg, q):
                prow.append((title, Q.route_of(x), f"waits: night window ({'-'.join(q['night_window'])}) has closed"))
                continue
            if not costly and night_cheap and not dry and P and P.ok:
                go, why = P.may_continue()
                if not go:
                    prow.append((title, Q.route_of(x), f"waits: {why}"))
                    continue
            if costly and books_left <= 0:
                prow.append((title, Q.route_of(x), f"waits: {books_cap} book(s) tonight reached"
                             + ("" if P and P.ok else " (usage unknown: fixed limit)")))
                continue
            if costly and not dry and not a.force and not in_window(cfg, q):
                prow.append((title, Q.route_of(x), f"waits: night window ({'-'.join(q['night_window'])}) has closed"))
                continue
            if costly and not dry and P and P.ok:
                go, why = P.may_start(x, Q.route_of(x))
                if not go:
                    prow.append((title, Q.route_of(x), f"waits: {why}"))
                    continue
            if costly and not dry and x["status"] != "working":
                try:   # a copy of something filed since this file was extracted (2026-10-13)
                    import routes as RT_
                    held_why = RT_.recheck(cfg, store, x)
                except Exception as e:  # noqa: BLE001
                    held_why = None
                    log(f"duplicate recheck failed for {title}: {type(e).__name__}: {e}")
                if held_why:
                    store.save(x)
                    prow.append((title, Q.route_of(x), f"held: {held_why}"))
                    continue
            if costly and not dry:
                books_left -= 1
                x.setdefault("work", {})["night"] = night
                if x["status"] != "working":
                    Q.set_status(x, "working", "summarizing by chapter", cfg=cfg)
                    store.save(x)
            try:
                if dry and x in new and x.get("kind") in ("email", "file", "note"):
                    res = {"ok": True, "dry": True, "would": f"{Q.route_of(x)} (captured this run)"}
                else:
                    res = work_item(cfg, log, store, x, dry, work)
            except StopModels as e:
                prow.append((title, Q.route_of(x), f"waits: model usage limit ({e})"))
                models_left = 0
                if x["status"] == "working" and not dry:   # resumes from the cached answers next night
                    Q.set_status(x, "ready", f"stopped by the usage limit at step {(x.get('work') or {}).get('step')}; "
                                 "resumes from the cached answers", cfg=cfg)
                    store.save(x)
                continue
            except Exception as e:
                log(traceback.format_exc()[-800:])
                res = {"ok": False, "why": f"{type(e).__name__}: {e}"}
            if costly and not dry and P and P.ok and not res.get("later"):
                P.finished(x, Q.route_of(x))
            what = res.get("would") or res.get("note") or res.get("why_done") or res.get("why")
            if res.get("usage"):
                x.setdefault("work", {})["usage"] = res["usage"]
                import longdoc
                what = f"{what} ({longdoc.usage_line(res['usage'])})"
            prow.append((title, Q.route_of(x) + f" [{x['priority']}]", str(what)))
            if dry or res.get("later"):
                continue
            if x["status"] == "working":
                x["status"] = "ready"   # set_status below records the outcome
            if res.get("ok"):
                x["result"]["notes"] = [res["note"]] if res.get("note") else []
                if res.get("original"):
                    x["result"]["original"] = res["original"]
                Q.set_status(x, res.get("status") or "done", res.get("note") or res.get("why_done"), cfg=cfg)
                if x.get("my_note") and x["result"]["notes"]:   # The owner's note from the answer page (2026-10-01)
                    try:
                        import my_note
                        my_note.apply_pending(cfg, log, x, x["result"]["notes"])
                    except Exception as e:  # noqa: BLE001
                        log(f"my_note not written: {type(e).__name__}: {e}")
            elif res.get("needs"):
                if res.get("hold_kind"):   # e.g. "notext": a summary that found no readable text (2026-10-10)
                    x.setdefault("work", {})["hold_kind"] = res["hold_kind"]
                Q.set_status(x, "held", res.get("why"), res.get("needs"), cfg)
            else:
                x["attempts"] = x.get("attempts", 0) + 1
                if x["attempts"] >= int(q["max_attempts"]):
                    Q.set_status(x, "held", f"failed {x['attempts']} times: {res.get('why')}", "you", cfg)
                else:
                    Q.set_status(x, "failed", res.get("why"), cfg=cfg)
            store.save(x)
        if s:
            frow = finish(cfg, log, store, s, base, dry)
        hrow += held_bridge.sync(cfg, log, store, dry, present)   # held items -> morning email, web page, laptop
        if not dry:   # a dry run saves nothing, so a second pass would only repeat the first
            hrow += held_bridge.sync(cfg, log, store, dry, present)   # held items -> morning email, web page, laptop
    finally:
        if not dry:
            try:   # vault search index catches up (plan: 2026-10-08 Vault search - plan.md, phase 1)
                import vault_search
                vault_search.hook(cfg, log)
            except Exception:  # noqa: BLE001
                pass
            lock.release()
    counts = {}
    for x in store.all():
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    rep = C.APP / "reports" / f"{now:%Y-%m-%d} queue{' (dry run)' if dry else ''}.md"
    with open(rep, "a", encoding="utf-8") as f:
        f.write(f"\n## {now:%H:%M} ({'dry run' if dry else 'live'}, routes {a.routes})\n\n")
        for head, rows in (("Collected", crow), ("Worked", prow), ("Finished", frow), ("held.json", hrow)):
            if rows:
                f.write(f"**{head}**\n\n| Item | Where / route | Result |\n|---|---|---|\n")
                for t, k, w in rows:
                    f.write(f"| {str(t).replace('|', '/')[:90]} | {k} | {str(w).replace('|', '/')[:200]} |\n")
                f.write("\n")
        if P:
            sm = P.summary()
            if sm["ok"]:
                f.write(f"Pacing: session {sm['start']['session_pct']}% -> {sm['end']['session_pct']}%, week "
                        f"{sm['start']['week_pct']}% -> {sm['end']['week_pct']}% (tonight's allowance {sm['allowance']}%, "
                        f"{sm['nights_left']} night(s) to the weekly reset); {len(sm['done'])} done"
                        + (f"; stopped: {sm['stopped']}" if sm.get("stopped") else "") + "\n\n")
            else:
                f.write(f"Pacing: usage unknown ({sm['why']}); fixed limits used\n\n")
            morning = f"{now + __import__('datetime').timedelta(hours=12):%Y-%m-%d}"
            pname = (f"pacing-window-{win['start'][:16].replace(':', '')}.json" if win else f"pacing-{morning}.json")
            (C.APP / "reports" / pname).write_text(json.dumps(sm, indent=1), encoding="utf-8")
        f.write("Queue: " + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "empty") + "\n")
    if not dry:
        try:   # the laptop's "process the vault queue" reads this (usage_plan, 2026-10-06)
            import usage_plan
            usage_plan.write_snapshot(cfg)
        except Exception:  # noqa: BLE001
            pass
    for t, k, w in crow + prow + frow + hrow:
        log(f"  [{k}] {str(t)[:70]} -> {w}")
    log(f"=== queue end: {len(crow)} collected, {len(prow)} worked, {len(frow)} finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
