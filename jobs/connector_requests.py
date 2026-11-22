#!/usr/bin/env python3
"""Apply requests the Vault connector queued (service\\vault_mcp.py -> queue\\connector\\<id>.json).

Plan: 2026-10-08 Vault search - plan.md, sections 6 and 7 (phase 3). Called by intake.py on every daytime run
(every 30 minutes, 7:00-23:00), before the intake queue runs, so a link queued here is processed in the same run.

  vault_save_link         -> an intake-queue item (route link, source connector); queue_run saves it with the
                             usual duplicate, name and confidentiality checks; The owner's my_note is written when filed
  vault_request_analysis  -> analyze.enqueue (the same path as `vqueue.py analyze`)
  vault_add_note          -> my_note.set_my_note / see_also via apply_plan.py (vault lock held)
  vault_answer            -> held on the day's questions ("answered via Claude"; the answer page pre-selects them);
                             a record with apply=true applies the day's held answers with
                             organize.apply_answers(..., via="Claude") (vault lock held). If the day was answered
                             by email or the web page first, the held answers are marked superseded.

Record statuses: new -> done | held (an answer waiting to be applied) | rejected | superseded | failed.
Never raises into the caller; a busy vault lock leaves the record "new" for the next run.

Command line:  python connector_requests.py [--dry-run]     process now (This PC)
               python connector_requests.py --list           show the last 30 records
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

FINAL = ("done", "rejected", "superseded", "failed")


def requests_dir():
    d = C.APP / "queue" / "connector"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_all():
    out = []
    for p in sorted(requests_dir().glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except Exception:  # noqa: BLE001
            continue
    return out


def save(cfg, p, rec, status, result):
    rec["status"], rec["result"] = status, str(result)[:1000]
    rec["updated"] = C.now(cfg).isoformat(timespec="seconds")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def held_answers(day):
    """{qid(str): letter} and {qid: note} recorded through the connector for a day (newest wins);
    only records still new or held. Used by the answer page and by apply."""
    ans, notes = {}, {}
    for _p, r in load_all():
        if r.get("tool") != "vault_answer" or (r.get("args") or {}).get("day") != day:
            continue
        if r.get("status") not in ("new", "held"):
            continue
        ans.update(r["args"].get("answers") or {})
        notes.update(r["args"].get("notes") or {})
    return ans, notes


# ---------------------------------------------------------------- one handler per tool
def do_save_link(cfg, log, rec, dry):
    import vqueue as Q
    a = rec["args"]
    store = Q.Store(cfg)
    for it in store.all():                                # the same link already waiting in the queue
        if it.get("url") == a["url"] and it.get("status") in Q.OPEN:
            return "done", f"already in the intake queue ({it['id']}, {it['status']})"
    if dry:
        return "new", "would queue the link"
    item = store.create(source="connector", source_ref={"request": rec["id"]}, kind="link", route="link",
                        route_from="connector", url=a["url"], title=a["url"], doug_note=a.get("my_note", ""))
    if a.get("my_note"):
        item["my_note"] = a["my_note"]                    # written into the note when it is filed (queue_run)
    Q.set_status(item, "ready", cfg=cfg)
    store.save(item)
    return "done", f"queued for intake as {item['id']}"


def do_analysis(cfg, log, rec, dry):
    import analyze
    import vqueue as Q
    a = rec["args"]
    if dry:
        return "new", f"would queue an analysis of {a['path']}"
    lines = analyze.enqueue(cfg, Q.Store(cfg), [a["path"]], redo=bool(a.get("redo")))
    text = "; ".join(lines) or "not eligible for analysis (type or folder)"
    return ("done" if "queued (" in text else "rejected"), text


def do_add_note(cfg, log, rec, dry):
    import my_note as MN
    a = rec["args"]
    vault = Path(cfg["vault_path"])
    p = vault / a["path"]
    if not p.is_file():
        return "rejected", f"{a['path']} is no longer in the vault (moved or deleted?)"
    fm = MN.read_fm(p)
    if not MN.wants_props(fm):
        return "rejected", f"{a['path']} is a {fm.get('type') or 'note'} type that doesn't take my_note / see_also"
    if dry:
        return "new", "would update my_note / see_also"
    done = []
    if a.get("my_note"):
        cur = MN.clean(fm.get("my_note") or "")
        new = MN.clean(a["my_note"] if not cur or cur == MN.clean(a["my_note"]) else f"{cur} | {a['my_note']}")
        ok, msg = MN.set_my_note(cfg, log, a["path"], new)
        if not ok:
            return "failed", f"my_note not written: {msg}"
        done.append("my_note " + ("added" if not cur else "extended"))
    if a.get("see_also"):
        sa = fm.get("see_also")
        sa = sa if isinstance(sa, list) else ([sa] if sa else [])
        have = {str(x).strip("[] ").lower() for x in sa}
        add = [f"[[{n}]]" for n in a["see_also"] if str(n).strip("[] ").lower() not in have]
        if add:
            pj = Path(cfg["local_root"]) / "work" / f"see-also-{rec['id']}.json"
            pj.write_text(json.dumps({"actor": C.owner(cfg)["id"], "log": "see_also added through the Vault connector",
                                      "items": [{"path": a["path"], "set": {"see_also": sa + add,
                                                                            "my_note": MN.my_note_of(p)}}]},
                                     ensure_ascii=False), encoding="utf-8")
            rc, out = C.run(log, C.VL / "apply_plan.py", vault, pj)
            try:
                pj.unlink()
            except OSError:
                pass
            if rc:
                return "failed", "see_also not written: " + (out.strip().splitlines() or [""])[-1]
        done.append(f"see_also +{len(add)}" if add else "see_also already there")
    return "done", f"{a['path']}: " + ", ".join(done)


def do_answers(cfg, log, recs, dry):
    """All vault_answer records of one day: hold them, or apply the day when one asks (apply=true)."""
    import organize
    day = recs[0][1]["args"]["day"]
    f = C.APP / "reports" / f"organize-{day}.json"
    try:
        prop = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return [(p, r, "rejected", f"no questions for {day}") for p, r in recs]
    if prop.get("status") != "sent":
        why = f"the {day} questions were answered {prop.get('via', '')} first; these answers were not used"
        return [(p, r, "superseded", why) for p, r in recs]
    want = any(r["args"].get("apply") for _p, r in recs)
    if not want:
        return [(p, r, "held", "held as 'answered via Claude'; applied when you ask, or with the page")
                for p, r in recs if r.get("status") == "new"]
    ans_s, notes_s = held_answers(day)
    ids = {q["id"] for k in ("questions", "new_tags", "new_folders", "dups", "queue", "held") for q in prop.get(k, [])}
    ans = {int(k): v for k, v in ans_s.items() if k.isdigit() and int(k) in ids}
    notes = {int(k): v for k, v in notes_s.items() if k.isdigit() and int(k) in ids}
    if dry:
        return [(p, r, r.get("status"), f"would apply {ans}") for p, r in recs]
    prop["answered_via_claude"] = sorted(ans)
    summary = organize.apply_answers(cfg, log, f, prop, ans, "letters", "Claude", notes)
    return [(p, r, "done", summary) for p, r in recs]


DRAFT_TYPES = ("Atomic Note", "Capture", "Project Note", "Area Note", "Reference", "Meeting Note")


def _approved_tags(vault):
    import re
    try:
        t = C.vpath(vault, "_claude-tags.md").read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(re.findall(r"(?m)^-\s*`([^`]+)`", t))


def do_file_draft(cfg, log, rec, dry):
    """A draft Claude wrote from the vault, filed in Inbox/ for the morning filing questions (phase 4, 2026-10-12).
    OKF frontmatter (type, title, description, approved tags only, generated, status: draft, sources); checked with
    okf_lint.py and removed again if it fails; logged in log.md. Never overwrites; the name gets " (draft N)" if taken."""
    import re
    a = rec["args"]
    vault = Path(cfg["vault_path"])
    ok_tags = _approved_tags(vault)
    tags = [t for t in a.get("tags") or [] if t in ok_tags]
    dropped = [t for t in a.get("tags") or [] if t not in ok_tags]
    base = re.sub(r'[\\/:*?"<>|#^\[\]]', "-", a["title"]).strip(" .-")[:120] or "Draft"
    names = {p.stem.lower() for p in vault.rglob("*.md")}
    name, i = base, 1
    while name.lower() in names:
        i += 1
        name = f"{base} (draft {i})"
    rel = f"Inbox/{name}.md"
    srcs = []
    try:
        import vault_search as VS
        con = VS.connect(cfg)
        try:
            for x in a.get("sources") or []:
                p = VS.resolve(con, x)
                if p:
                    t = con.execute("SELECT title FROM notes WHERE path=?", (p,)).fetchone()
                    srcs.append({"id": re.sub(r"[^a-z0-9]+", "-", Path(p).stem.lower()).strip("-")[:40],
                                 "resource": "/" + p, "title": (t[0] if t else Path(p).stem), "name": Path(p).stem})
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass
    if dry:
        return "new", f"would file {rel}"
    now = C.now(cfg).isoformat(timespec="seconds")
    q = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731  JSON strings are valid YAML
    fm = ["---", f"type: {q(a['type'])}", f"title: {q(a['title'])}", f"description: {q(a['description'])}",
          f"tags: {q(tags)}", f"generated: {{ by: claude/vault-connector, at: {now} }}", "status: draft"]
    if srcs:
        fm.append("sources:")
        fm += [f"  - {{ id: {q(x['id'])}, resource: {q(x['resource'])}, title: {q(x['title'])} }}" for x in srcs]
    fm.append("---")
    body = a["body"].strip() + ("\n\nSources: " + ", ".join(f"[[{x['name']}]]" for x in srcs) if srcs else "")
    f = vault / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "x", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(fm) + "\n\n" + body + "\n")
    rc, out = C.run(log, C.VL / "okf_lint.py", vault, "--paths", rel, "--no-orphans")
    last = (out.strip().splitlines() or ["?"])[-1]
    if rc:
        f.unlink()
        return "rejected", f"not filed: the draft failed the OKF check ({last})"
    with open(vault / "log.md", "a", encoding="utf-8") as fh:
        fh.write(f"\n## {now[:10]}\n\n**Draft filed through the Vault connector**\n\n- Created `{rel}` (status: draft)\n")
    return "done", f"filed {rel} (status draft; asked about in the next morning questions)" + \
        (f"; tags not on the approved list were left off: {', '.join(dropped)}" if dropped else "") + f"; OKF: {last}"


HANDLERS = {"vault_save_link": do_save_link, "vault_request_analysis": do_analysis, "vault_add_note": do_add_note}
NEEDS_LOCK = ("vault_add_note", "vault_answer", "vault_file_draft")


def process(cfg, log, dry=False):
    """Apply every new request (and held answers whose day someone asked to apply). Returns a short summary."""
    try:
        todo = [(p, r) for p, r in load_all() if r.get("status") == "new"]
        if not todo:
            return ""
        lines, lock, locked, wrote = [], None, False, False
        for p, r in [(p, r) for p, r in todo if r["tool"] not in NEEDS_LOCK]:
            try:
                st, res = HANDLERS[r["tool"]](cfg, log, r, dry)
            except Exception as e:  # noqa: BLE001
                st, res = "failed", f"{type(e).__name__}: {e}"
            if not dry:
                save(cfg, p, r, st, res)
            lines.append(f"{r['tool']} {r['id']}: {st}: {res}")
        rest = [(p, r) for p, r in todo if r["tool"] in NEEDS_LOCK]
        if rest:
            lock = C.VaultLock(cfg, f"{C.pc_name()} connector requests")
            if not dry:
                locked, note = lock.acquire()
                if not locked:
                    lines.append(f"{len(rest)} request(s) wait for the next run ({note})")
                    rest = []
            try:
                for p, r in [(p, r) for p, r in rest if r["tool"] in ("vault_add_note", "vault_file_draft")]:
                    try:
                        st, res = (do_add_note if r["tool"] == "vault_add_note" else do_file_draft)(cfg, log, r, dry)
                        wrote = wrote or st == "done"
                    except Exception as e:  # noqa: BLE001
                        st, res = "failed", f"{type(e).__name__}: {e}"
                    if not dry:
                        save(cfg, p, r, st, res)
                    lines.append(f"{r['tool']} {r['id']}: {st}: {res}")
                days = {}
                for p, r in rest:
                    if r["tool"] == "vault_answer":
                        days.setdefault(r["args"].get("day"), []).append((p, r))
                for day, recs in days.items():
                    if any(r["args"].get("apply") for _p, r in recs):   # apply also uses answers held earlier
                        recs = recs + [(p, r) for p, r in load_all() if r.get("tool") == "vault_answer"
                                       and r.get("status") == "held" and r["args"].get("day") == day]
                    try:
                        out = do_answers(cfg, log, recs, dry)
                        wrote = wrote or any(st == "done" for _p, _r, st, _x in out)
                    except Exception as e:  # noqa: BLE001
                        out = [(p, r, "failed", f"{type(e).__name__}: {e}") for p, r in recs]
                    for p, r, st, res in out:
                        if not dry:
                            save(cfg, p, r, st, res)
                        lines.append(f"vault_answer {r['id']}: {st}: {res}")
            finally:
                if locked:
                    if wrote:
                        try:   # vault search index catches up (plan: 2026-10-08 Vault search, phase 1)
                            import vault_search
                            vault_search.hook(cfg, log)
                        except Exception:  # noqa: BLE001
                            pass
                    lock.release()
        for ln in lines:
            log("connector: " + ln[:400])
        return "; ".join(lines)
    except Exception as e:  # noqa: BLE001
        log(f"connector requests failed: {type(e).__name__}: {e}")
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    cfg = C.load_config()
    if a.list:
        for _p, r in load_all()[-30:]:
            print(f"{r['id']}  {r['tool']:<22} {r.get('status'):<10} {str(r.get('result') or '')[:100]}")
        return 0
    print(process(cfg, C.Log(cfg, "connector"), dry=a.dry_run) or "nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
