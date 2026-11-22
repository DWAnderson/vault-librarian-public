"""Keeps queue\\held.json in step with the intake queue (switch-over 2026-09-28).

held.json is still read and written by: nightly.py (report), browser_retry.py (headless retry),
clippings.py (stub-* entries), organize.py (held stubs), update_email.py (morning email cards),
vault_server.py (web-* phone shares, status page) and the laptop's "process the vault queue"
routine in the vault-librarian skill. Rather than change all of them at once, the queue treats
held.json as their shared view of held Remember emails:

  1. An entry marked "saved" by one of them (headless retry, laptop browser)  -> its queue item is done,
     so queue_run.finish() moves the email to Deleted Items; the entry is dropped once the email is gone.
     Saved web-*/stub-* entries (not Remember emails) are dropped, as intake.py used to do.
  2. A queue item that is held                    -> an entry keyed by its Outlook message id. Fields the
     others added (headless_tries, headless_gave_up...) are kept.
     A queue item no longer held (released, done, cancelled) -> its entry is dropped.
  3. An entry keyed by a message id with no queue item (written before the switch-over):
     email still in Remember -> adopted as a held queue item (so it isn't captured again);
     email gone              -> dropped (this is the stale-entry fix of 2026-09-28).
Changes are applied to a fresh read of the file just before writing, because the others write it too.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import vqueue as Q  # noqa: E402

OTHER = ("web-", "stub-")
QKEY = "q-"   # held items that aren't top-level Remember emails (dropbox files, attachments): "q-<item id>"


def held_path():
    return C.APP / "queue" / "held.json"


def load():
    p = held_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def legacy_ids():
    """Message ids held.json lists that the queue doesn't know yet (the collector must skip them)."""
    return {k for k in load() if not k.startswith(OTHER + (QKEY,))}


def sync(cfg, log, store, dry, present=None):
    """present: set of message ids now in Remember (None when the collector couldn't see them all).
    Returns report rows (title, 'held.json', what)."""
    held, rows, put, drop = load(), [], {}, set()
    by_mid = {}
    for it in store.all():
        mid = (it.get("source_ref") or {}).get("message_id")
        if it.get("source") == "mail" and mid and not it.get("parent"):
            by_mid[mid] = it
        else:
            by_mid[QKEY + it["id"]] = it
    # 1. results others wrote
    for k, h in held.items():
        if h.get("status") != "saved":
            continue
        if k.startswith(OTHER):
            drop.add(k)
            continue
        it = by_mid.get(k)
        if it and it["status"] not in ("done", "duplicate"):
            rows.append((it.get("title") or "?", "held.json", f"saved by {h.get('saved_by') or 'the laptop'}: {h.get('note')}"))
            if not dry:
                it["result"]["notes"] = [h["note"]] if h.get("note") else []
                Q.set_status(it, "done", f"saved by {h.get('saved_by') or 'the laptop'}: {h.get('note')}", cfg=cfg)
                store.save(it)
        elif not it or it["result"].get("source_removed"):
            drop.add(k)
    # 2. held queue items -> entries
    for mid, it in by_mid.items():
        if it["status"] == "held":
            e = dict(held.get(mid) or {})
            if e.get("status") == "saved":
                continue
            e.update({"subject": (it.get("title") or "?") + (" (file in the dropbox)" if it.get("source") == "folder" and not it.get("parent")
                                                             else " (attachment)" if it.get("parent") else ""),
                      "why": it.get("why"), "needs": it.get("needs") or "you",
                      "status": "held", "queue_id": it["id"],
                      "received": (it.get("source_ref") or {}).get("received"),
                      "internet_message_id": (it.get("source_ref") or {}).get("internet_message_id")})
            e.setdefault("at", it.get("updated") or Q.stamp(cfg))
            if it.get("url"):
                e["url"], e["doug_note"] = it["url"], it.get("doug_note", "")
            if e != held.get(mid):
                put[mid] = e
        elif held.get(mid, {}).get("status") == "held":
            drop.add(mid)
            rows.append((it.get("title") or "?", "held.json", f"no longer held in the queue ({it['status']})"))
    # 3. entries from before the switch-over
    for k, h in held.items():
        if k.startswith(QKEY) and k not in by_mid:
            drop.add(k)   # its item is gone
            continue
        if k.startswith(OTHER) or k in by_mid or k in drop or h.get("status") != "held":
            continue
        if present is None:
            continue
        if k not in present:
            drop.add(k)
            rows.append((h.get("subject") or "?", "held.json", "dropped: the email is no longer in Remember"))
            continue
        rows.append((h.get("subject") or "?", "held.json", "adopted into the queue (held)"))
        if not dry:
            it = store.create(source="mail", title=h.get("subject"), kind="link" if h.get("url") else "email",
                              route="link" if h.get("url") else "email", route_from="held.json",
                              url=h.get("url"), doug_note=h.get("doug_note", ""),
                              source_ref={"message_id": k, "internet_message_id": h.get("internet_message_id"),
                                          "folder": "Remember", "received": h.get("received")})
            Q.set_status(it, "held", h.get("why"), h.get("needs") or "you", cfg)
            store.save(it)
            put[k] = dict(h, queue_id=it["id"])
    if dry or not (put or drop):
        return rows
    fresh = load()
    for k in drop:
        fresh.pop(k, None)
    for k, e in put.items():
        cur = fresh.get(k) or {}
        if cur.get("status") == "saved":
            continue   # someone saved it meanwhile; the next run picks that up
        fresh[k] = dict(cur, **e)
    Q.atomic_write(held_path(), json.dumps(fresh, ensure_ascii=False, indent=1))
    return rows
