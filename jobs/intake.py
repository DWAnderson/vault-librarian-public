#!/usr/bin/env python3
"""Remember-folder intake on this PC (Task Scheduler, every 30 min, 07:00-23:00).

For each email in Inbox\\Remember of the configured mailbox (Graph app token, scoped to that
mailbox only):
  - sender domain in intake.confidential_domains  -> left in Remember, listed ("held")
  - link email (one URL, <= ~30 words of other text) -> url-to-vault routine:
        article / image -> note written by url_to_note.py
        text            -> one-paragraph summary by claude -p, then url_to_note.py
        already in vault (duplicate) -> counts as saved
        YouTube video -> queued for the 02:05 YouTube Remember job (queue\\remember.jsonl); counts as saved
        unsupported / needs the signed-in browser -> left in Remember, listed
  - other emails: <= verbatim_max_words -> verbatim Email note; longer -> claude -p summary
    (summary + key facts + excerpts), content: summary
Only emails whose note was written (or already existed) are moved to Deleted Items.
Two or more URLs with little text = ordinary email (the owner's decision 2026-09-25).

  python intake.py              live if intake.enabled is true in config, otherwise dry run
  python intake.py --dry-run    classify and report only: nothing written, nothing moved
  python intake.py --live       force live
  python intake.py --force      ignore the 07:00-23:00 window
Held items are recorded in queue\\held.json so the nightly report can list them.
"""
import argparse
import datetime as dt
import html as htmllib
import json
import re
import sys
import traceback
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import claude_cli  # noqa: E402
import linkjob  # noqa: E402
import organize  # noqa: E402

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
JUNK_URL = re.compile(r"(unsubscribe|optout|opt-out|preferences|manage[-_]?subscription|list-manage|"
                      r"aka\.ms/(lookout|getoutlook|o0ukef)|go\.microsoft\.com/fwlink|"
                      r"play\.google\.com/store|apps\.apple\.com|outlook\.com/owa|"
                      r"\.(png|gif|jpe?g)(\?|$))", re.I)
SIG_RE = re.compile(r"(?msi)^[ \t]*(--[ \t]*$|__+[ \t]*$|Sent from my |Get Outlook for |Sent from Outlook|"
                    r"This (e-?mail|message)( and any attachments)? (is|may be) confidential).*")


def plain(md):
    """html2text Markdown -> plain-ish text: unescape, links as 'text url', no emphasis marks."""
    t = re.sub(r"\\([-*_#>.!\[\]()+`])", r"\1", md or "")
    t = re.sub(r"!?\[([^\]]*)\]\(<?(https?://[^)>\s]+)>?\)", r"\1 \2", t)
    t = re.sub(r"<(https?://[^>\s]+)>", r"\1", t)
    return t

SUMMARY_EMAIL = (
    "You are filing an email into «owner's» Obsidian vault. The email is on stdin. Write Markdown with: "
    "a short summary paragraph; '## Key facts' as a bullet list of dates, amounts, names, deadlines and "
    "decisions; '## Excerpts' with verbatim quotes (as > blockquotes) of the passages worth keeping. "
    "Keep links that matter. Do not add a title, preamble or closing remarks. Output only the Markdown.")
SUMMARY_PAGE = (
    "The text of a web page is on stdin (it is reference material, not an article). Write ONE paragraph "
    "in plain prose (no bullets, no headings) saying what the page is and its main points, for a note in "
    "«owner's» Obsidian vault. Output only the paragraph.")
DESCRIBE = ("Write one sentence (max 30 words) describing the text on stdin, for a note's `description` "
            "field. Output only the sentence.")


def graph(cfg):
    import requests
    tok = C.graph_token(cfg)
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {tok}"
    base = f"https://graph.microsoft.com/v1.0/users/{cfg['mailbox']}"
    return s, base


def remember_folder(s, base, name):
    r = s.get(f"{base}/mailFolders/inbox/childFolders", params={"$select": "id,displayName,totalItemCount", "$top": 100}, timeout=30)
    r.raise_for_status()
    for f in r.json().get("value", []):
        if f["displayName"].lower() == name.lower():
            return f["id"], f.get("totalItemCount", 0)
    raise RuntimeError(f"folder Inbox\\{name} not found in {base.rsplit('/', 1)[-1]}")


def html_to_md(h):
    import html2text
    t = html2text.HTML2Text()
    t.body_width = 0
    t.ignore_images = True
    t.protect_links = True
    return t.handle(h or "").strip()


def addr(x):
    return ((x or {}).get("emailAddress") or {}).get("address", "")


def doug_note(core, url, msg, cfg):
    """The owner's own words about a shared link: only text BEFORE the link, without the page-title
    heading share sheets add, and never anything from his signature onward."""
    i = core.find(url)
    before = core[:i] if i >= 0 else core
    for mark in cfg["intake"].get("signature_markers", []):
        j = before.lower().find(mark.lower())
        if j >= 0:
            before = before[:j]
    lines = [ln.strip() for ln in before.splitlines()]
    keep, subj = [], (msg.get("subject") or "").strip().lower()
    for k, ln in enumerate(lines):
        nxt = lines[k + 1] if k + 1 < len(lines) else ""
        if not ln or re.fullmatch(r"[=\-_*#\s]+", ln):
            continue
        if re.fullmatch(r"[=\-]{3,}", nxt) or ln.startswith("#"):
            continue  # a heading = the page title the share sheet inserted
        if subj and (ln.lower() == subj or ln.lower().startswith(subj + " - ")):
            continue
        keep.append(ln)
    note = " ".join(re.sub(r"[<>\[\]()*_]", " ", URL_RE.sub("", " ".join(keep))).split()).strip(" -:")
    return note if len(note.split()) >= 3 else ""


def classify(msg, cfg):
    """Return dict: kind (held|link|email), plus details."""
    sender = addr(msg.get("from")).lower()
    dom = sender.rsplit("@", 1)[-1]
    if any(dom == d or dom.endswith("." + d) for d in cfg["intake"].get("confidential_domains", [])):
        return {"kind": "held", "reason": f"sender domain {dom} is on the confidential list"}
    body_html = (msg.get("body") or {}).get("content", "")
    md = html_to_md(body_html) if (msg.get("body") or {}).get("contentType") == "html" else body_html
    full = plain(md)
    core = SIG_RE.sub("", full).strip()
    sig = full[m.start():] if (m := SIG_RE.search(full)) else ""
    # a link in the signature ("Get Outlook for Android" -> aka.ms/AAb9ysg) isn't shared content (2026-11-03)
    hrefs = []
    for u, txt in re.findall(r"href=[\"'](https?://[^\"']+)[^>]*>(.*?)</a>", body_html or "", re.I | re.S):
        txt = " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", txt)).split())
        if not (txt and txt in sig and txt not in core):
            hrefs.append(u)
    urls = []
    for u in URL_RE.findall(core) + hrefs:
        u = htmllib.unescape(u).rstrip(".,;:!?")
        if JUNK_URL.search(u) or u.startswith("mailto:"):
            continue
        key = re.sub(r"^https?://(www\.)?", "", u.lower()).rstrip("/")
        if key not in [k for k, _ in urls]:
            urls.append((key, u))
    text_wo = URL_RE.sub(" ", core)
    text_wo = re.sub(r"[\[\]()<>*_#>`|-]", " ", text_wo)
    words = re.findall(r"\w+", text_wo)
    subj_words = set(re.findall(r"\w+", (msg.get("subject") or "").lower()))
    own = [w for w in words if w.lower() not in subj_words]   # a share email often repeats the title
    wc = len(re.findall(r"\w+", core))
    # A podcast app's share email ("<show>: <episode>  Episode webpage: ...  Media file: ....mp3"; The owner,
    # 2026-11-16): the audio link is the item -> a Podcast Summary + Transcript, not an Email note.
    audio = [u for _, u in urls if linkjob.is_audio_url(u)]
    if audio and len(own) <= 30:
        return {"kind": "link", "url": audio[0], "podcast": True, "title_hint": (msg.get("subject") or "").strip(),
                "doug_note": "", "md": md, "words": wc}   # the share text is the app's, not the owner's
    if len(urls) == 1 and len(own) <= 30:
        return {"kind": "link", "url": urls[0][1], "doug_note": doug_note(core, urls[0][1], msg, cfg), "md": md, "words": wc}
    return {"kind": "email", "md": core or md, "words": wc,
            "long": wc > int(cfg["intake"].get("verbatim_max_words", 2000))}


def run_link(cfg, log, vault, work, msg, c, dry):
    # YouTube links are handed to the Remember job inside linkjob.save_url (2026-09-28)
    return linkjob.save_url(cfg, log, c["url"], c.get("doug_note", ""),
                            "mid:" + (msg.get("internetMessageId") or "").strip("<>"), dry,
                            title_hint=c.get("title_hint"))


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = C.load_config()
    it = cfg["intake"]
    dry = True if a.dry_run else False if a.live else not bool(it.get("enabled"))
    now = C.now(cfg)
    if not a.force and not (int(it.get("first_hour", 7)) <= now.hour < int(it.get("last_hour", 23))):
        return 0
    log = C.Log(cfg, "intake")
    vault = Path(cfg["vault_path"])
    work = Path(cfg["local_root"]) / "work" / "intake"
    work.mkdir(parents=True, exist_ok=True)
    held_path = C.APP / "queue" / "held.json"   # synced: a laptop session can resolve browser-only links
    held_path.parent.mkdir(exist_ok=True)
    held = json.loads(held_path.read_text(encoding="utf-8")) if held_path.exists() else {}
    try:
        s, base = graph(cfg)
        fid, total = remember_folder(s, base, it.get("remember_folder", "Remember"))
        C.mail_health(cfg, "read", True)
    except Exception as e:
        log(f"Graph error: {e}")
        C.mail_health(cfg, "read", False, f"{type(e).__name__}: {e}")
        # 2026-11-12: mail being down no longer stops the run. Email replies wait; connector requests, the
        # dropbox and the queue go on (answers come in through the web pages meanwhile).
        if not (cfg.get("queue") or {}).get("enabled"):
            return 1
        s = base = None
    if s is not None:
        try:   # mail is back after a failed morning send: send that day's update once (home page plan, 2026-11-12)
            organize.catch_up_send(cfg, C.Log(cfg, "organize"), dry)
        except Exception as e:  # noqa: BLE001
            C.Log(cfg, "organize")(f"catch-up send failed: {type(e).__name__}: {e}")
    if s is not None and not dry and cfg.get("organize", {}).get("approve_by_email_reply") and organize.has_pending():
        lk = C.VaultLock(cfg, f"{C.pc_name()} organize-apply")
        ok, lnote = lk.acquire()
        if not ok or lnote:
            C.Log(cfg, "organize")(lnote)   # skipped: the reply stays pending for the next run
        if ok:
            try:
                organize.check_replies(cfg, C.Log(cfg, "organize"), s, base)
            except Exception as e:
                C.Log(cfg, "organize")(f"reply check failed: {type(e).__name__}: {e}")
            try:   # notes just filed out of Inbox get "(Summary)" etc. now, not at 02:30 (2026-10-10)
                import vault_names as VN
                VN.enforce(cfg, C.Log(cfg, "organize"))
            except Exception as e:  # noqa: BLE001
                C.Log(cfg, "organize")(f"note names failed: {type(e).__name__}: {e}")
            finally:
                try:   # vault search index catches up (plan: 2026-10-08 Vault search - plan.md, phase 1)
                    import vault_search
                    vault_search.hook(cfg, C.Log(cfg, "organize"))
                except Exception:  # noqa: BLE001
                    pass
                lk.release()
    if not dry:   # requests from the Vault connector (plan: 2026-10-08 Vault search - plan.md, phase 3)
        try:
            import connector_requests
            connector_requests.process(cfg, C.Log(cfg, "connector"))
        except Exception as e:  # noqa: BLE001
            C.Log(cfg, "connector")(f"connector requests failed: {type(e).__name__}: {e}")
        try:   # "index my book originals" etc. (jobs\originals.py; plan: 2026-10-08 Vault search, phase 4)
            import originals
            originals.start_if_requested(cfg, C.Log(cfg, "originals"))
        except Exception as e:  # noqa: BLE001
            C.Log(cfg, "originals")(f"originals request check failed: {type(e).__name__}: {e}")
    if C.paused(cfg):   # home page "Pause until" (2026-11-12): connector requests above still run
        log(f"paused until {C.paused(cfg)}: no queue run")
        return 0
    if (cfg.get("queue") or {}).get("enabled"):
        # 2026-09-28: the intake queue does the Remember work (plan: 2026-09-28 Intake queue for files and
        # email - plan.md); organize replies above stay here. queue_run also keeps queue\held.json current.
        import queue_run
        return queue_run.main(["--dry-run"] if dry else ["--live"])
    if not total:
        if not dry:   # Remember is empty, so every held email is gone (stale-entry fix, 2026-09-28)
            stale = [k for k, h in held.items() if h.get("status") == "held" and not k.startswith(("web-", "stub-"))]
            for k in stale:
                held.pop(k)
            if stale:
                held_path.write_text(json.dumps(held, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    log(f"=== intake start ({'DRY RUN' if dry else 'LIVE'}): {total} email(s) in Remember")
    lock = C.VaultLock(cfg, f"{C.pc_name()} intake")
    lnote = None
    if not dry:
        ok, lnote = lock.acquire()
        if not ok:
            log(lnote)
            rep = C.APP / "reports" / f"{now:%Y-%m-%d} intake.md"
            with open(rep, "a", encoding="utf-8") as f:
                f.write(f"\n## {now:%H:%M} (live)\n\n{lnote}; the Remember emails wait for the next run.\n")
            return 0
    rows, emails, saved = [], [], []
    if not dry and lnote:
        rows.append(("(vault lock)", "lock", lnote))
    try:
        # items a laptop session resolved (status "saved"): the note exists, so the email can go
        for mid, h in list(held.items()):
            if h.get("status") == "saved" and not dry and mid.startswith(("web-", "stub-")):
                held.pop(mid)
                continue
            # YouTube links held before 2026-09-28 ("video or audio page"): release them so this run
            # hands them to the Remember job like any new YouTube link
            if h.get("status") == "held" and not dry and not mid.startswith(("web-", "stub-")) and linkjob.youtube_id(h.get("url")):
                held.pop(mid)
                continue
            # Podwise links held because Podwise hadn't processed the episode yet (2026-11-14): release
            # them so this run tries the import again (one quick Podwise call; no model use)
            if h.get("status") == "held" and not dry and not mid.startswith(("web-", "stub-")) and linkjob.podwise_id(h.get("url")):
                held.pop(mid)
                continue
            if h.get("status") == "saved" and not dry:
                rm = s.post(f"{base}/messages/{mid}/move", json={"destinationId": "deleteditems"}, timeout=30)
                if rm.ok or rm.status_code == 404:
                    rows.append((h.get("subject", "?"), "queue", f"saved from laptop: {h.get('note')}; email removed"))
                    held.pop(mid)
        r = s.get(f"{base}/mailFolders/{fid}/messages", timeout=60, params={
            "$top": int(it.get("batch", 20)), "$orderby": "receivedDateTime asc",
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,internetMessageId,webLink,body,hasAttachments"})
        r.raise_for_status()
        for msg in r.json().get("value", []):
            subj = msg.get("subject") or "(no subject)"
            if msg["id"] in held:
                continue  # already in the queue; stays until the owner (or a laptop session) resolves it
            try:
                c = classify(msg, cfg)
                if c["kind"] == "held":
                    rows.append((subj, "held", c["reason"]))
                    held[msg["id"]] = {"subject": subj, "why": c["reason"], "needs": "you", "status": "held",
                                       "received": msg.get("receivedDateTime"), "at": now.isoformat()}
                    continue
                if c["kind"] == "link":
                    res = run_link(cfg, log, vault, work, msg, c, dry)
                    if res.get("ok"):
                        rows.append((subj, "link", res.get("would") or (("already saved: " if res.get("duplicate") else "saved: ") + str(res.get("note")))))
                        if not res.get("dry"):
                            saved.append(msg["id"])
                    else:
                        rows.append((subj, "link held", f"{c['url']} - {res.get('why')}"))
                        held[msg["id"]] = {"subject": subj, "url": c["url"], "why": res.get("why"),
                                           "needs": res.get("needs") or "you",
                                           "status": "held", "doug_note": c.get("doug_note", ""),
                                           "internet_message_id": msg.get("internetMessageId"),
                                           "received": msg.get("receivedDateTime"), "at": now.isoformat()}
                    continue
                # ordinary email
                atts = []
                if msg.get("hasAttachments"):
                    ra = s.get(f"{base}/messages/{msg['id']}/attachments", params={"$select": "name"}, timeout=30)
                    atts = [x["name"] for x in ra.json().get("value", [])] if ra.ok else []
                body, content = c["md"], "full"
                desc = ""
                if dry:
                    rows.append((subj, "email", f"{c['words']} words -> {'claude summary' if c['long'] else 'verbatim'} Email note"))
                    continue
                if c["long"]:
                    body, info = claude_cli.ask(SUMMARY_EMAIL, c["md"][:60000], work, route="intake-summary")
                    if not body:
                        rows.append((subj, "email held", "summary failed: " + info.get("error", "")))
                        continue
                    content = "summary"
                d, _ = claude_cli.ask(DESCRIBE, c["md"][:6000], work, timeout=120, route="intake-describe")
                desc = (d or "").splitlines()[0][:300] if d else ""
                emails.append({"id": msg["id"], "subject": subj, "from": addr(msg.get("from")),
                               "to": [addr(x) for x in msg.get("toRecipients") or []],
                               "cc": [addr(x) for x in msg.get("ccRecipients") or []],
                               "received": msg.get("receivedDateTime"), "internet_message_id": msg.get("internetMessageId"),
                               "web_link": msg.get("webLink"), "word_count": c["words"], "content": content,
                               "description": desc, "attachments": atts, "body": body})
            except Exception as e:
                rows.append((subj, "error", f"{type(e).__name__}: {e}"))
                log(traceback.format_exc()[-800:])
        if emails:
            ej = work / f"emails-{now:%Y%m%d-%H%M}.json"
            ej.write_text(json.dumps(emails, ensure_ascii=False), encoding="utf-8")
            rc, out = C.run(log, C.VL / "email_to_note.py", vault, ej)
            byid = {e["id"]: e["subject"] for e in emails}
            for ln in out.splitlines():
                if ln.startswith("{"):
                    x = json.loads(ln)
                    if x.get("ok"):
                        saved.append(x["id"])
                        rows.append((byid.get(x["id"], "?"), "email", f"saved: {x.get('note')}"))
                    else:
                        rows.append((byid.get(x["id"], "?"), "email held", f"note failed: {x.get('error')}"))
        for mid in saved:  # Deleted Items (recoverable); only emails whose note exists
            rm = s.post(f"{base}/messages/{mid}/move", json={"destinationId": "deleteditems"}, timeout=30)
            if not rm.ok:
                log(f"move failed for {mid[-10:]}: HTTP {rm.status_code}")
    finally:
        if not dry:
            try:   # vault search index catches up (plan: 2026-10-08 Vault search - plan.md, phase 1)
                import vault_search
                vault_search.hook(cfg, log)
            except Exception:  # noqa: BLE001
                pass
        lock.release()
    if not dry:
        try:
            present = {m["id"] for m in r.json().get("value", [])} if total <= int(it.get("batch", 20)) else None
            if present is not None:
                for mid in [k for k in held if k not in present and held[k].get("status") == "held"
                            and not k.startswith(("web-", "stub-"))]:
                    held.pop(mid)
        except Exception:
            pass
    if not dry:  # a dry run must not mark anything as held for later live runs
        held_path.write_text(json.dumps(held, ensure_ascii=False, indent=1), encoding="utf-8")
    for subj, kind, what in rows:
        log(f"  [{kind}] {subj[:80]} -> {what}")
    rep = C.APP / "reports" / f"{now:%Y-%m-%d} intake{' (dry run)' if dry else ''}.md"
    with open(rep, "a", encoding="utf-8") as f:
        f.write(f"\n## {now:%H:%M} ({'dry run' if dry else 'live'})\n\n| Email | Kind | Result |\n|---|---|---|\n")
        for subj, kind, what in rows:
            f.write(f"| {subj.replace('|', '/')[:90]} | {kind} | {str(what).replace('|', '/')[:200]} |\n")
    log(f"=== intake end: {len(rows)} processed, {len(saved)} removed from Remember")
    return 0


if __name__ == "__main__":
    sys.exit(main())
