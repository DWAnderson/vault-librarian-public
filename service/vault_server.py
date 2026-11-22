#!/usr/bin/env python3
"""Vault Librarian service on this PC: save links to the vault from the phone, see status.

Listens on 127.0.0.1:8766 only. Tailscale Serve publishes it on the tailnet at
https://<pc>.<tailnet>.ts.net/vault/ (the Daily Digest link server keeps "/"). Nothing listens on
the LAN. Started at logon by the \\VaultLibrarian\\Server task (pythonw, no window).

  GET  /vault/                 form: paste a link (+ optional note) and save it
  GET  /vault/save?url=&note=  save a link (phone share sheet); waits up to 40 s, else "queued"
  GET  /vault/status           last runs, held items, recent saves
  GET  /vault/home?t=          the home page: open questions, status, settings (service/home_page.py, 2026-11-12)
  GET  /vault/organize/<day>?t= the morning questions (web alternative to replying); its notes and files can be
                               opened read-only and compared side by side: <day>/note, /compare, /original,
                               /dropbox (jobs/note_view.py; only what that day's questions name, same token)
  GET/POST /vault/organize/<day>/queue?t=   the intake queue: order, do next, hold, release, change route, skip
  GET  /vault/open?f=<note path>  opens a vault note in Obsidian (a clickable https link for chat/mail)
  GET  /vault/organize/<day>/usage?t=&mode=more|light|normal   tonight's Claude usage mode (2026-10-06)
  GET/POST /vault/organize/<day>/pick?t=&q=<id>   choose another folder for a filing question: the Subjects and
                               Manuals trees (any depth), or create a new subject / subfolder (2026-10-07)
  POST /vault/api/note         for other this PC apps (Daily Digest): write a prepared note.
                               Header X-Vault-Token: <%LOCALAPPDATA%\\VaultLibrarian\\credentials\\api_token.txt>
Routes also answer without the /vault prefix (Tailscale may strip it).
Guard rails: http(s) only; no private/loopback addresses; 60 saves per hour; vault lock respected.
"""
import datetime as dt
import html
import ipaddress
import json
import re
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
import common as C  # noqa: E402
import linkjob  # noqa: E402

from flask import Flask, abort, jsonify, redirect, request, send_file  # noqa: E402

PORT = 8766
app = Flask(__name__)
CFG = C.load_config()
LOG = C.Log(CFG, "server")
LOCAL = Path(CFG["local_root"])
RECENT = LOCAL / "queue" / "recent.json"
HELD = C.APP / "queue" / "held.json"
_lock = threading.Lock()
_hits = []
_jobs = {}
ALLOWED_KEYS = {"type", "title", "description", "resource", "author", "publication", "publisher", "published",
                "accessed", "work_type", "duration", "season", "episode", "platform", "summary_by", "content",
                "audio_url", "language", "cite_as", "aliases"}
NOTE_TYPES = {"Video Summary", "Podcast Transcript"}
CSS = ("<style>body{font:17px/1.45 system-ui,sans-serif;max-width:720px;margin:1.2rem auto;padding:0 1rem;color:#222}"
       "input,textarea{width:100%;font:inherit;padding:.5rem;box-sizing:border-box;margin:.2rem 0 .8rem}"
       "button{font:inherit;padding:.5rem 1.2rem}td{padding:.25rem .5rem;vertical-align:top;border-bottom:1px solid #eee}"
       ".ok{color:#16701d}.no{color:#a33}.muted{color:#777;font-size:.9rem}</style>")


def page(title, body):
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>{CSS}<h2>{html.escape(title)}</h2>{body}"
            f"<p class=muted><a href='status'>Status</a> · <a href='./'>Save a link</a></p>")


def token():
    p = LOCAL / "credentials" / "api_token.txt"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


def public_url(u):
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def rate_ok():
    now = time.time()
    with _lock:
        _hits[:] = [t for t in _hits if now - t < 3600]
        if len(_hits) >= 60:
            return False
        _hits.append(now)
    return True


def jload(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def jsave(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def with_vault_lock(fn, owner):
    lk = C.VaultLock(CFG, owner)
    for _ in range(12):  # wait up to ~2 min for a nightly/intake run to finish
        ok, note = lk.acquire()
        if note:
            LOG(note)
        if ok:
            try:
                return fn()
            finally:
                lk.release()
                try:   # vault search index catches up (plan: 2026-10-08 Vault search - plan.md, phase 1)
                    import vault_search
                    vault_search.hook_background(CFG, LOG)
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(10)
    return {"ok": False, "why": "vault busy (another job holds the lock); try again in a few minutes", "needs": "you"}


def work_save(jid, url, note):
    res = with_vault_lock(lambda: linkjob.save_url(CFG, LOG, url, note, "share:phone"), f"{C.pc_name()} vault service")
    res["url"], res["at"] = url, C.now(CFG).isoformat(timespec="seconds")
    if not res.get("ok"):
        with _lock:
            held = jload(HELD, {})
            key = "web-" + jid
            held[key] = {"subject": "(shared from phone)", "url": url, "why": res.get("why"), "needs": res.get("needs") or "you",
                         "status": "held", "doug_note": note, "at": res["at"]}
            jsave(HELD, held)
    with _lock:
        rec = jload(RECENT, [])
        rec.insert(0, res)
        jsave(RECENT, rec[:50])
        _jobs[jid] = res
    LOG(f"save {url} -> {'ok ' + str(res.get('note')) if res.get('ok') else 'held: ' + str(res.get('why'))[:120]}")


def result_html(res):
    if res.get("ok"):
        what = "Already in your vault" if res.get("duplicate") else "Saved to your vault"
        return page(what, f"<p class=ok><b>{html.escape(str(res.get('title') or ''))}</b></p>"
                          f"<p>{html.escape(str(res.get('note')))}</p><p class=muted>Filed on the next organize pass.</p>")
    return page("Not saved", f"<p class=no>{html.escape(str(res.get('why')))}</p>"
                             f"<p class=muted>Kept in the vault queue{' for a laptop session' if res.get('needs') == 'browser' else ''}.</p>")


def route(path, **kw):
    def deco(f):
        app.add_url_rule(path, f.__name__, f, **kw)
        app.add_url_rule("/vault" + path, f.__name__ + "_v", f, **kw)
        return f
    return deco


@route("/")
def home():
    import home_data
    if home_data.token_ok(CFG, request.args.get("t")):
        return redirect(f"home?t={request.args['t']}", code=303)
    return page("Save a link to the vault",
                "<form action='save' method=get><label>Link</label><input name=url type=url required placeholder='https://…'>"
                "<label>Note (optional)</label><textarea name=note rows=2></textarea><button>Save</button></form>")


@route("/save")
def save():
    url = (request.args.get("url") or "").strip()
    note = (request.args.get("note") or "").strip()[:500]
    m = re.search(r"https?://\S+", url)          # share sheets often send "Title https://…"
    if m:
        if not note and url[:m.start()].strip():
            note = ""                               # a title the share sheet added is not the owner's note
        url = m.group(0).rstrip(".,;)")
    if not public_url(url):
        return page("Not saved", "<p class=no>That isn't a public http(s) link.</p>"), 400
    if not rate_ok():
        return page("Not saved", "<p class=no>Too many saves this hour; try later.</p>"), 429
    jid = secrets.token_hex(5)
    t = threading.Thread(target=work_save, args=(jid, url, note), daemon=True)
    t.start()
    t.join(40)
    res = _jobs.get(jid)
    if res:
        return result_html(res)
    return page("Working on it", f"<p>{html.escape(url)}</p><p class=muted>Still processing; it will appear in "
                                 f"<a href='status'>Status</a> and in your vault's Inbox in a minute or two.</p>")


@route("/status")
def status():
    import home_data
    if home_data.token_ok(CFG, request.args.get("t")):   # 2026-11-12: the home page's Status tab replaces it
        return redirect(f"home?t={request.args['t']}&tab=status", code=303)
    rec = jload(RECENT, [])[:15]
    held = [h for h in jload(HELD, {}).values() if h.get("status") == "held"]
    rows = "".join(f"<tr><td class={'ok' if r.get('ok') else 'no'}>{'✓' if r.get('ok') else '✗'}</td>"
                   f"<td>{html.escape(str(r.get('title') or r.get('url')))}<br><span class=muted>"
                   f"{html.escape(str(r.get('note') or r.get('why') or ''))[:160]} · {html.escape(str(r.get('at', ''))[:16])}</span></td></tr>"
                   for r in rec) or "<tr><td>none yet</td></tr>"
    hrows = "".join(f"<tr><td>{html.escape(h.get('needs', ''))}</td><td>{html.escape(str(h.get('url') or h.get('subject')))}"
                    f"<br><span class=muted>{html.escape(str(h.get('why', '')))[:160]}</span></td></tr>" for h in held) or "<tr><td>none</td></tr>"
    reps = sorted((C.APP / "reports").glob("*.md"))[-6:]
    rlist = "".join(f"<li>{html.escape(p.stem)}</li>" for p in reversed(reps))
    ql = latest_queue_link()
    qlink = f"<p><a href='{html.escape(ql)}'>Manage the intake queue</a> (order, hold, release, change route)</p>" if ql else ""
    conn = ""
    try:   # search index and Vault connector (plan: 2026-10-08 Vault search - plan.md, phase 3)
        import vault_search
        st = vault_search.stats(CFG)
        logs = sorted((C.APP / "reports").glob("connector-log-*.md"))
        tools = [ln for ln in logs[-1].read_text(encoding="utf-8").splitlines() if " tool " in ln] if logs else []
        today = C.now(CFG).strftime("%Y-%m-%d")
        nreq = len(list((C.APP / "queue" / "connector").glob(today.replace("-", "") + "-*.json")))
        lim = (CFG.get("mcp") or {}).get("daily_requests", 20)
        conn = (f"<h3>Search and the Vault connector</h3><p>Index: {st['notes']:,} notes, {st['passages']:,} passages, "
                f"{st['size_mb']} MB; updated {html.escape(str(st.get('last_update') or '?'))[:16].replace('T', ' ')}"
                + (f"; {st['waiting_for_vectors']:,} passages waiting for meaning vectors" if st["waiting_for_vectors"] else "")
                + f".<br>Connector: {sum(1 for x in tools if x[2:12] == today)} tool call(s) today"
                + (f", last {html.escape(tools[-1][2:18])}" if tools else "") + f"; {nreq} of {lim} requests used today.</p>")
    except Exception as e:  # noqa: BLE001
        conn = f"<h3>Search and the Vault connector</h3><p class=no>unavailable: {html.escape(type(e).__name__)}</p>"
    return page("Vault status", qlink + conn + f"<h3>Recent saves from the phone</h3><table>{rows}</table>"
                                f"<h3>Held (need you or the laptop browser)</h3><table>{hrows}</table>"
                                f"<h3>Latest reports on {C.pc_name()}</h3><ul>{rlist}</ul>")


# ------------------------------------------------------------------ organize answers (web alternative to replying)
OCSS = ("<style>*{box-sizing:border-box}body{overflow-wrap:anywhere;font:12pt/1.45 Aptos,'Aptos Display',Calibri,'Segoe UI',system-ui,sans-serif;background:#f3f2f1;margin:0;color:#242424}"
        ".wrap{max-width:680px;margin:0 auto;padding:0 12px 40px}.hd{background:#483d8b;color:#fff;padding:16px 20px}"
        ".hd small{font-size:9pt;letter-spacing:1px;text-transform:uppercase}.hd b{display:block;font-size:16pt}"
        ".card{background:#fff;border:1px solid #e1dfdd;border-left:4px solid #483d8b;border-radius:4px;margin:12px 0;padding:12px 14px}"
        ".card.tag{border-left-color:#8764b8}.chip{font-size:9pt;background:#edebe9;border-radius:3px;padding:1px 6px}"
        ".sub{font-size:10.5pt;color:#616161}label.opt{display:flex;gap:10px;align-items:flex-start;padding:9px 8px;margin:4px 0;"
        "border:1px solid #edebe9;border-radius:4px;cursor:pointer}label.opt:has(input:checked){background:#e8f5e9;border-color:#a5d6a7}"
        "input[type=radio]{margin-top:4px;transform:scale(1.3)}.sug{font-size:9pt;background:#2e7d32;color:#fff;border-radius:3px;padding:1px 6px;margin-left:6px}"
        ".note{color:#616161}.bar{position:sticky;bottom:0;background:#f3f2f1;padding:12px 0;display:flex;gap:10px}"
        "button{font:inherit;font-weight:bold;padding:11px 18px;border-radius:6px;border:1px solid #483d8b;cursor:pointer}"
        ".pri{background:#483d8b;color:#fff}.sec{background:#fff;color:#483d8b}.ok{color:#2e7d32}.warn{color:#7a5a00}"
        ".lnk{font-size:10.5pt;margin:4px 0 2px 0}.lnk a{color:#483d8b;margin-right:14px}a.nm{color:#242424;text-decoration-color:#c8c6c4}</style>")


def opage(title, inner):
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>{OCSS}<div class=hd><small>Obsidian Vault Update{home_link()}</small>"
            f"<b>{html.escape(title)}</b></div><div class=wrap>{inner}</div>")


def home_link():
    """On the day pages (organize/<day>/...): a link back to the home page (plan: 2026-11-12 Home page)."""
    try:
        parts = request.path.strip("/").split("/")
        if "organize" not in parts:
            return ""
        ups = len(parts) - parts.index("organize") - 1
        import home_data
        return (f" &middot; <a style='color:#fff' href='{'../' * ups}home?t={html.escape(home_data.home_token(CFG))}'>"
                "Home</a>")
    except Exception:  # noqa: BLE001
        return ""


def pending_folders(prop, existing):
    """Folders the owner created on this day's page (pick page: a filing question's choice or one note of a folder
    card) that don't exist yet: offered everywhere on the page at once, created when the answers are applied (2026-10-14). Their parent folders under Subjects/Manuals count too."""
    import organize
    have = set(existing)
    vals = [v for v in (prop.get("picks") or {}).values()]
    vals += [v for row in (prop.get("nf_pre") or {}).values() for v in (row or {}).values()]
    vals += [v for row in (prop.get("q_pre") or {}).values() for v in (row or {}).values()]
    out = []
    for v in vals:
        v = str(v or "").strip("/")
        parts = v.split("/")
        for i in range(2, len(parts) + 1):
            f = "/".join(parts[:i])
            if f.startswith(organize.FIXED_SUBS) and f not in have and f not in out:
                out.append(f)
    return sorted(out)


def load_prop(day, t):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        abort(404)
    f = C.APP / "reports" / f"organize-{day}.json"
    prop = jload(f, None)
    if not prop or not t or not secrets.compare_digest(str(prop.get("web_token", "")), t):
        abort(404)
    return f, prop


@route("/organize/<day>", methods=["GET", "POST"])
def organize_page(day):
    import organize
    import update_email as U
    t = request.values.get("t", "")
    f, prop = load_prop(day, t)
    title = U.fmt_day(day)
    if prop.get("status") == "applied":
        return opage(title, f"<div class=card><p class=ok><b>Already answered</b></p><p>These questions were answered "
                            f"{html.escape(str(prop.get('via', '')))} at {html.escape(str(prop.get('applied', ''))[11:16])}: "
                            f"{prop.get('filed', 0)} note(s) filed. Nothing more to do.</p></div>" + notes_after(day, t, prop))
    if prop.get("status") == "superseded":   # 2026-11-12: asked again in a later update; never apply stale answers
        import home_data
        later = str(prop.get("superseded_by") or "")
        return opage(title, f"<div class=card><p><b>Asked again later</b></p><p>These questions were asked again in the "
                            f"{html.escape(U.fmt_day(later) if later else 'next')} update, with anything new since. Answer "
                            f"them there: <a href='../home?t={html.escape(home_data.home_token(CFG))}'>Vault home</a>.</p></div>")
    if request.method == "POST":
        ids = ([q["id"] for q in prop["questions"]] + [nt["id"] for nt in prop.get("new_tags", [])]
               + [nf["id"] for nf in prop.get("new_folders", [])]
               + [q["id"] for q in prop.get("dups", [])] + [q["id"] for q in prop.get("queue", [])]
               + [q["id"] for q in prop.get("held", [])])
        notes = {q["id"]: (request.form.get(f"n{q['id']}") or "")[:2000] for q in prop.get("held", [])}
        own_titles = {str(q["id"]): (request.form.get(f"tt{q['id']}") or "").strip()[:300]   # Name check: his own title
                      for q in prop.get("queue", []) if (request.form.get(f"tt{q['id']}") or "").strip()}
        # new-folder questions: where each note goes (2026-10-12): the new folder (default), another folder,
        # or "" = decide later. Only folders that exist (or the new one) are accepted.
        fset = set(organize.folders(Path(CFG["vault_path"])))
        fset |= set(pending_folders(prop, fset))   # folders made earlier on this page (2026-10-14)
        nf_picks = {}
        for nf in prop.get("new_folders", []):
            row = {}
            pre = (prop.get("nf_pre") or {}).get(str(nf["id"])) or {}   # new folders made on the pick page
            for i, pth in enumerate(nf.get("paths", [])):
                v = request.form.get(f"nf{nf['id']}_{i}")
                if v is None:
                    continue
                if v in ("", nf["folder"]) or v in fset or (v and v == pre.get(pth)):
                    row[pth] = v
            if row:
                nf_picks[str(nf["id"])] = row
        # several notes in one filing question: each can go its own way (2026-10-15). "__q" = follow the
        # question's answer; "" = leave it where it is; otherwise an existing folder, an option's folder, or one
        # made on the pick page for that note.
        q_picks = {}
        for q in prop.get("questions", []):
            if len(q.get("paths", [])) < 2:
                continue
            okd = {o.get("move_to") for o in q["options"].values() if o.get("move_to")}
            pre = (prop.get("q_pre") or {}).get(str(q["id"])) or {}
            row = {}
            for i, pth in enumerate(q["paths"]):
                v = request.form.get(f"qn{q['id']}_{i}")
                if v is None or v.startswith("__"):
                    continue
                if v == "" or v in fset or v in okd or v == pre.get(pth):
                    row[pth] = v
            if row:
                q_picks[str(q["id"])] = row
        if request.form.get("action") == "skip":
            ans, mode = {}, "skip"
        else:
            ans = {i: request.form.get(f"q{i}") for i in ids if request.form.get(f"q{i}") in ("a", "b", "c", "d", "p")}
            mode = "letters"

        with _lock:
            running = (APPLY.get(day) or {}).get("state") == "running"
            if not running:
                APPLY[day] = {"state": "running", "started": time.time(), "steps": [], "detail": [], "result": None}
        if not running:   # The owner (2026-10-13): show what is being done while it runs, not a page that just waits
            threading.Thread(target=apply_job, args=(day, f, ans, mode, notes, nf_picks, q_picks, own_titles),
                             daemon=True).start()
        return redirect(f"{day}/applying?t={t}", 303)
    with _lock:
        if (APPLY.get(day) or {}).get("state") == "running":
            return redirect(f"{day}/applying?t={t}")
    import note_view as V
    items = {it["path"]: it for it in prop["items"]}
    cards = []
    mnu = f"{day}/mynote"

    def mnp(p):   # "Add a note" for a vault note whose my_note is empty (2026-10-01)
        cur = V.note_my_note(CFG, prop, p)
        return "" if cur is None or cur else V.mn_button(mnu, t, p=p)

    def mnq(q):   # ... and for a file not in the vault yet: kept on its queue item, written when it is filed
        if not q.get("item"):
            return ""
        return V.mn_button(mnu, t, q=q["id"], current=q.get("my_note") or "")

    def nm(p, text):   # a note's name, linked to its read-only view (opens in a new tab)
        return f"<a class=nm href='{html.escape(day + '/note' + V.qs(t, p=p))}' target=_blank>{html.escape(text)}</a>"

    def lnk(q):
        ls = V.links_for(q, f"{day}/", t)
        return ("<div class=lnk>" + "".join(f"<a href='{html.escape(u)}' target=_blank>{html.escape(x)}</a>" for x, u in ls)
                + "</div>") if ls else ""
    all_f0 = organize.folders(Path(CFG["vault_path"]))
    pend0 = pending_folders(prop, all_f0)
    multi = False
    for q in prop["questions"]:
        rows = []
        many = len(q["paths"]) > 1
        multi = multi or many
        for i, p in enumerate(q["paths"]):
            sel = ""
            if many:   # The owner (2026-10-15): choose a different place (or leave it) for any one of the notes
                mine = ((prop.get("q_pre") or {}).get(str(q["id"])) or {}).get(p)
                dests = []
                for k, o in sorted(q["options"].items()):
                    d = o.get("move_to")
                    if d and d not in dests:
                        dests.append(d)
                dests += [x for x in pend0 if x not in dests]
                o_html = "".join(f"<option value='{html.escape(d)}'{' selected' if d == mine else ''}>&rarr; {html.escape(d.replace('/', ' / '))}"
                                 f"{'' if d in all_f0 else ' (new folder)'}</option>" for d in dests)
                if mine and mine not in dests:
                    o_html = (f"<option value='{html.escape(mine)}' selected>&rarr; {html.escape(mine.replace('/', ' / '))}"
                              f"{'' if mine in all_f0 else ' (new folder)'}</option>") + o_html
                pick_url = f"{day}/pick?t={t}&q={q['id']}&i={i}"
                sel = (f"<div class=nfr><span class=sub>This note:</span> <select name=qn{q['id']}_{i} class=nfs data-pick='{html.escape(pick_url)}'"
                       f"{' data-mine=1' if mine else ''}><option value='__q'{'' if mine else ' selected'}>Same as the answer below</option>"
                       f"{o_html}<option value=''>Leave it where it is for now</option>"
                       f"<option value='__pick'>New folder or subfolder&hellip;</option>"
                       f"<option value='__more'>Another folder&hellip;</option></select></div>")
            it = items.get(p, {})
            old = p.rsplit("/", 1)[-1][:-3]
            new = it.get("rename_to")
            typ = (it.get("set") or {}).get("type")
            rows.append(f"<div><b>{nm(p, new or old)}</b>{' <span class=chip>' + html.escape(typ) + '</span>' if typ else ''}"
                        + (f"<div class=sub>New name; was &ldquo;{html.escape(old)}&rdquo;</div>" if new else "") + mnp(p) + sel + "</div>")
        opts = ""
        for k, o in sorted(q["options"].items()):
            lab = o.get("label") or ""
            m = re.match(r"^(.*?)\s*(\(.*\))\s*$", lab)
            main, note = (m.group(1), m.group(2)) if m else (lab, "")
            opts += (f"<label class=opt><input type=radio name=q{q['id']} value={k}{' checked' if k == 'a' else ''}>"
                     f"<span><b>{k})</b> {html.escape(main.replace('/', ' / '))} <span class=note>{html.escape(note)}</span>"
                     f"{'<span class=sug>Suggested</span>' if k == 'a' else ''}</span></label>")
        pk = (prop.get("picks") or {}).get(str(q["id"]))
        if pk:
            opts = opts.replace(" checked>", ">")
            new = "" if pk in organize.folders(Path(CFG["vault_path"])) else " <span class=chip>new folder</span>"
            opts += (f"<label class=opt><input type=radio name=q{q['id']} value=p checked><span><b>Your choice:</b> "
                     f"{html.escape(pk.replace('/', ' / '))}{new}</span></label>")
        opts += (f"<label class=opt><input type=radio name=q{q['id']} value=''><span class=note>Leave in the Inbox for now</span></label>")
        opts += (f"<div class=lnk><a href='{html.escape(day + '/pick?t=' + t + '&q=' + str(q['id']))}' class=pickl>"
                 f"Choose a different folder, or create a new subject or subfolder &rarr;</a></div>")
        hint = ("<div class=sub>Each note follows the answer below unless you pick something else in its own list.</div>"
                if many else "")
        cards.append(f"<div class=card id=q{q['id']}>{hint}<div class=sub>{q['id']}. {html.escape(re.sub(r'\s*-\s*where should (it|they) go\?$', '', q.get('text', '')))}</div>{''.join(rows)}{opts}</div>")
    for nt in prop.get("new_tags", []):
        cards.append(f"<div class='card tag'><span class=chip>New tag</span><div><b>{nt['id']}. Add the tag "
                     f"<code>{html.escape(nt['tag'])}</code>?</b></div><div class=sub>{html.escape(nt.get('covers', ''))}</div>"
                     f"<label class=opt><input type=radio name=q{nt['id']} value=a><span><b>a)</b> Yes</span></label>"
                     f"<label class=opt><input type=radio name=q{nt['id']} value=b checked><span><b>b)</b> No"
                     f"<span class=sug>Suggested</span></span></label></div>")
    all_folders = organize.folders(Path(CFG["vault_path"]))
    pend = pending_folders(prop, all_folders)   # made on this page, not yet created (2026-10-14)
    for nf in prop.get("new_folders", []):
        # The owner (2026-10-12): create the folder, then decide note by note: file it there (default), somewhere else,
        # or decide later. "Somewhere else" lists every folder (filled in by NF_JS when the list is opened).
        exists = nf["folder"] in all_folders
        rows = []
        pre = (prop.get("nf_pre") or {}).get(str(nf["id"])) or {}
        for i, p in enumerate(nf.get("paths", [])):
            mine = pre.get(p)   # chosen or created on the pick page (2026-10-13)
            mine_opt = (f"<option value='{html.escape(mine)}' selected>&rarr; {html.escape(mine.replace('/', ' / '))}"
                        f"{'' if mine in all_folders else ' (new folder)'}</option>") if mine else ""
            pick_url = f"{day}/pick?t={t}&nf={nf['id']}&i={i}"
            pend_opts = "".join(f"<option value='{html.escape(x)}'>&rarr; {html.escape(x.replace('/', ' / '))} (new folder)</option>"
                                for x in pend if x != mine and x != nf["folder"])
            rows.append(f"<div class=nfr><span class=nfn>{nm(p, p.rsplit('/', 1)[-1][:-3])} <span class=sub>({html.escape(p.rsplit('/', 1)[0] if '/' in p else '')})</span></span>"
                        f"<select name=nf{nf['id']}_{i} class=nfs data-pick='{html.escape(pick_url)}'{' data-mine=1' if mine else ''}>{mine_opt}"
                        f"<option value='{html.escape(nf['folder'])}'{'' if mine else ' selected'}>"
                        f"&rarr; {html.escape(nf['folder'].rsplit('/', 1)[-1])}</option>{pend_opts}<option value=''>Decide later (leave it where it is)</option>"
                        f"<option value='__pick'>New folder or subfolder&hellip;</option>"
                        f"<option value='__more'>Another folder&hellip;</option></select></div>")
        verb = "File these in" if exists else "Create"
        cards.append(f"<div class=card style='border-left-color:#2e7d32'><span class=chip>{'Waiting for a folder' if exists else 'New folder'}</span><div><b>{nf['id']}. "
                     f"{verb} <code>{html.escape(nf['folder'])}</code>{'' if exists else ' and file these there'}?</b></div>"
                     f"<div class=sub>For {html.escape(nf.get('covers', ''))}. Notes filed there get the folder's name as a tag. "
                     f"With <b>a)</b>, each note below goes where its list says: the {'' if exists else 'new '}folder, another folder, or "
                     f"<i>Decide later</i> (asked again tomorrow).</div>"
                     f"<details class=nfd open><summary>{len(rows)} note(s) &mdash; choose for each</summary>"
                     f"<div class=nfa><a href='#' data-nf='{nf['id']}' data-v=''>All: decide later</a> &middot; "
                     f"<a href='#' data-nf='{nf['id']}' data-v='{html.escape(nf['folder'])}'>All: {html.escape(nf['folder'].rsplit('/', 1)[-1])}</a></div>"
                     f"{''.join(rows)}</details>"
                     f"<label class=opt><input type=radio name=q{nf['id']} value=a><span><b>a)</b> Yes{'' if exists else ', create it'} and file the notes as chosen above</span></label>"
                     f"<label class=opt><input type=radio name=q{nf['id']} value=b checked><span><b>b)</b> No (don't ask again)"
                     f"<span class=sug>Suggested</span></span></label>"
                     f"<label class=opt><input type=radio name=q{nf['id']} value=''><span class=note>Decide later (asked again tomorrow)</span></label></div>")
    nf_js = ("" if not (prop.get("new_folders") or multi) else
             "<style>.nfr{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;padding:3px 0;border-bottom:1px solid #f3f2f1}"
             ".nfn{flex:1 1 260px;min-width:0}.nfs{font:inherit;font-size:10pt;max-width:100%;padding:2px 4px}"
             ".nfa{font-size:9.5pt;margin:4px 0}.nfd summary{cursor:pointer;color:#483d8b;margin:6px 0}</style>"
             "<script>(function(){var F=" + json.dumps(sorted(set(all_folders) | set(pend))) + ";"
             "function fill(s){if(s.dataset.full)return;s.dataset.full=1;var m=s.querySelector('option[value=__more]');"
             "var g=document.createElement('optgroup');g.label='Another folder';F.forEach(function(f){var o=document.createElement('option');"
             "o.value=f;o.textContent=f.split('/').join(' / ');g.appendChild(o);});if(m)m.remove();s.appendChild(g);}window.vqFill=fill;"
             "document.querySelectorAll('select.nfs').forEach(function(s){['focus','mousedown','touchstart'].forEach(function(ev){"
             "s.addEventListener(ev,function(){fill(s);});});});"
             "document.querySelectorAll('.nfa a').forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();"
             "document.querySelectorAll('select[name^=nf'+a.dataset.nf+'_]').forEach(function(s){s.value=a.dataset.v;});"
             "var r=document.querySelector('input[name=q'+a.dataset.nf+'][value=a]');if(r)r.checked=true;});});"
             "document.querySelectorAll('select.nfs').forEach(function(s){s.addEventListener('change',function(){"
             "if(s.value==='__pick'){if(window.vqSave)window.vqSave();location.href=s.dataset.pick;return;}"
             "if(s.name.indexOf('nf')!==0)return;"
             "var r=document.querySelector('input[name=q'+s.name.split('_')[0].slice(2)+'][value=a]');if(r)r.checked=true;});});"
             "})();</script>")
    if prop.get("dups"):
        cards.append("<h3 style='margin:18px 0 0 0;color:#483d8b'>Duplicates to decide</h3>")
    for q in prop.get("dups", []):
        paths = [q["path"]] if q["kind"] == "archived" else q["paths"]
        head = "".join(f"<div><b>{nm(p, p.rsplit('/', 1)[-1][:-3])}</b> <span class=sub>({html.escape(p.rsplit('/', 1)[0] if '/' in p else '')})</span>{mnp(p)}</div>"
                       for p in paths)
        opts = "".join(f"<label class=opt><input type=radio name=q{q['id']} value={k}{' checked' if k == 'a' else ''}>"
                       f"<span><b>{k})</b> {html.escape(o.get('label', ''))}{'<span class=sug>Suggested</span>' if k == 'a' else ''}</span></label>"
                       for k, o in sorted(q["options"].items()))
        opts += f"<label class=opt><input type=radio name=q{q['id']} value=''><span class=note>Decide later</span></label>"
        cards.append(f"<div class=card style='border-left-color:#c4314b'><span class=chip>"
                     f"{'Archived duplicate' if q['kind'] == 'archived' else 'Possible duplicates'}</span>"
                     f"<div class=sub>{q['id']}. {html.escape(q.get('text', ''))}</div>{head}{lnk(q)}{opts}</div>")
    if prop.get("queue"):
        cards.append("<h3 style='margin:18px 0 0 0;color:#483d8b'>From your Remember folders and new analyses: to decide</h3>")
    for q in prop.get("queue", []):
        opts = "".join(f"<label class=opt><input type=radio name=q{q['id']} value={k}{' checked' if k == 'a' else ''}>"
                       + (f"<span><b>{k})</b> Use my own title: <input class=ownt name=tt{q['id']} data-q={q['id']} data-k={k}"
                          f" value='{html.escape(q.get('title') or '')}' style='width:100%;max-width:520px;margin-top:4px'></span></label>"
                          if o.get("action") == "title_own" else
                          f"<span><b>{k})</b> {html.escape(o.get('label', ''))}{'<span class=sug>Suggested</span>' if k == 'a' else ''}</span></label>")
                       for k, o in sorted(q["options"].items()))
        opts += f"<label class=opt><input type=radio name=q{q['id']} value=''><span class=note>Decide later</span></label>"
        chip = {"concept": "New concept", "concept_dup": "Same concept?", "title": "Name check"}.get(q.get("kind"), "Intake queue")
        cards.append(f"<div class=card style='border-left-color:#c4314b'><span class=chip>{chip}</span>"
                     f"<div><b>{q['id']}. {html.escape(q['title'])}</b></div>"
                     f"<div class=sub>{html.escape(q['text'])}{(' &middot; ' + html.escape(q['size'])) if q.get('size') else ''}"
                     f"<br>{html.escape(q['where'])}</div>{mnq(q)}{lnk(q)}{opts}</div>")
    if prop.get("held"):
        cards.append("<h3 style='margin:18px 0 0 0;color:#483d8b'>From your Remember folder: couldn't be saved automatically</h3>")
    chips = {"notext": "No readable text", "junk": "Not a document", "dead": "Dead link", "browser": "Needs your signed-in browser", "other": "Couldn't be saved"}
    for q in prop.get("held", []):
        opts = "".join(f"<label class=opt><input type=radio name=q{q['id']} value={k}{' checked' if k == 'a' else ''}>"
                       f"<span><b>{k})</b> {html.escape(o.get('label', ''))}{'<span class=sug>Suggested</span>' if k == 'a' else ''}</span></label>"
                       for k, o in sorted(q["options"].items()))
        if opts:
            opts += f"<label class=opt><input type=radio name=q{q['id']} value=''><span class=note>Decide later</span></label>"
        note = mnq(q) if q.get("item") else V.mn_button(mnu, t, q=q["id"], current=q.get("note") or "")
        cards.append(f"<div class=card style='border-left-color:#c19c00'><span class=chip>{chips.get(q.get('sub'), 'Held')}</span>"
                     f"<div><b>{q['id']}. {html.escape(q['title'])}</b></div>"
                     f"<div class=sub>{html.escape(q.get('text') or q.get('why') or '')}"
                     + (f"<br>{html.escape(q['where'])}" if q.get("where") else "") + f"</div>{note}{lnk(q)}{opts}</div>")
    try:   # answers given through the Vault connector, held for this page (plan: 2026-10-08 Vault search, phase 3)
        import connector_requests as CR
        cl = CR.held_answers(day)[0]
    except Exception:  # noqa: BLE001
        cl = {}
    banner = (f"<div class=card style='border-left-color:#8764b8'><b>Answered via Claude:</b> {len(cl)} question(s) "
              "answered through the Vault connector are pre-selected below and marked <i>Answered via Claude</i>. "
              "Check them, change any you disagree with, then apply.</div>") if cl else ""
    cjs = ("<script>(function(){var c=" + json.dumps(cl) + ",f=document.querySelector('form[method=post]');if(!f)return;"
           "Object.keys(c).forEach(function(n){var e=f.querySelector('input[type=radio][name=\"q'+n+'\"][value=\"'+c[n]+'\"]');"
           "if(!e)return;e.checked=true;var s=document.createElement('span');s.className='sug';s.style.background='#8764b8';"
           "s.textContent='Answered via Claude';e.nextElementSibling.appendChild(s);});})();</script>")
    form = banner + usage_section(day, t) + (f"<p>The Suggested choices are already selected. Change any you disagree with, then press <b>Apply these answers</b>.</p>"
            f"<form method=post><input type=hidden name=t value='{html.escape(t)}'>{''.join(cards)}"
            f"{V.fresh_section(CFG, prop, day, t, day + '/')}"
            f"<div class=bar><button class=pri name=action value=apply>Apply these answers</button>"
            f"<button class=sec name=action value=skip>Leave everything as it is</button></div></form>")
    keep = ("<script>(function(){var k='vq-" + day + "',f=document.querySelector('form[method=post]');if(!f)return;"
            "var picked=" + json.dumps([str(x) for x in (prop.get("picks") or {})]) + ";"
            "try{var s=JSON.parse(sessionStorage.getItem(k)||'{}');Object.keys(s).forEach(function(n){"
            "if(picked.indexOf(n.slice(1))>=0)return;var e=f.querySelector('input[type=radio][name=\"'+n+'\"][value=\"'+s[n]+'\"]');"
            "if(e)e.checked=true;"
            "var sel=f.querySelector('select[name=\"'+n+'\"]');if(sel&&!sel.dataset.mine){"
            "var has=function(){return [].some.call(sel.options,function(o){return o.value===s[n];});};"
            "if(!has()&&window.vqFill)window.vqFill(sel);if(has())sel.value=s[n];}});}catch(e){}"
            "function save(){var o={};f.querySelectorAll('input[type=radio]:checked').forEach(function(e){o[e.name]=e.value;});"
            "f.querySelectorAll('select.nfs').forEach(function(e){if(e.value!=='__pick')o[e.name]=e.value;});"
            "try{sessionStorage.setItem(k,JSON.stringify(o));}catch(e){}}window.vqSave=save;"
            "f.querySelectorAll('input.ownt').forEach(function(e){['focus','input'].forEach(function(v){e.addEventListener(v,function(){"
            "var r=f.querySelector('input[type=radio][name=\"q'+e.dataset.q+'\"][value=\"'+e.dataset.k+'\"]');if(r&&!r.checked){r.checked=true;save();}});});});"
            "f.addEventListener('change',save);document.querySelectorAll('a.pickl').forEach(function(a){a.addEventListener('click',save);});"
            "})();</script>")
    return opage(title, f"<style>{V.MN_CSS}</style>" + form + V.MN_JS + nf_js + cjs + keep)


# ---------------------------------------------------------------- live progress for "Apply these answers" (2026-10-13)
APPLY = {}   # {day: {"state": running|done|busy|already|error, "started", "steps": [...], "detail": [...], "result"}}


def apply_job(day, f, ans, mode, notes, nf_picks, q_picks=None, own_titles=None):
    import organize
    st = APPLY[day]

    def progress(step, text, items=None):
        with _lock:
            if st["steps"]:
                st["steps"][-1]["done"] = time.time()
            st["steps"].append({"step": step, "text": text, "items": list(items or [])[:200], "at": time.time()})

    def log(msg):
        LOG(msg)
        m = str(msg)
        if m.startswith("run "):
            w = m.split(" ")
            m = ("running " + w[2]) if len(w) > 2 else m
        elif m.strip().startswith("exit "):
            return
        with _lock:
            st["detail"].append({"at": time.time(), "text": m[:300]})
            del st["detail"][:-400]

    def work():
        fresh = jload(f, {})
        if fresh.get("status") != "sent":        # answered by email while the page was open
            return None
        if nf_picks:
            fresh["nf_picks"] = nf_picks
        if q_picks and mode != "skip":
            fresh["q_picks"] = q_picks
        if own_titles and mode != "skip":
            fresh["own_titles"] = own_titles
        progress("Start", f"{len(ans)} answer(s) received; the vault is locked for this")
        return organize.apply_answers(CFG, log, f, fresh, ans, mode, "web page", notes, progress=progress)
    try:
        progress("Waiting", "waiting for the vault (another job may be using it)")
        res = with_vault_lock(work, f"{C.pc_name()} organize-apply")
        with _lock:
            if st["steps"]:
                st["steps"][-1]["done"] = time.time()
            if isinstance(res, dict):
                st.update(state="busy", result=res.get("why"))
            elif res is None:
                st.update(state="already", result="An email reply got there first; nothing was applied twice.")
            else:
                st.update(state="done", result=res)
    except Exception as e:  # noqa: BLE001
        LOG(f"apply failed: {type(e).__name__}: {e}")
        with _lock:
            st.update(state="error", result=f"{type(e).__name__}: {e}")
    st["finished"] = time.time()


@route("/organize/<day>/progress")
def organize_progress(day):
    t = request.values.get("t", "")
    load_prop(day, t)
    with _lock:
        st = json.loads(json.dumps(APPLY.get(day) or {}))
    if not st:
        prop = load_prop(day, t)[1]
        st = {"state": "done" if prop.get("status") == "applied" else "none",
              "result": (f"Answered {prop.get('via', '')} at {str(prop.get('applied', ''))[11:16]}: "
                         f"{prop.get('filed', 0)} note(s) filed.") if prop.get("status") == "applied" else None,
              "steps": [], "detail": []}
    st["now"] = time.time()
    return jsonify(st)


@route("/organize/<day>/applying")
def organize_applying(day):
    import update_email as U
    t = request.values.get("t", "")
    load_prop(day, t)
    title = U.fmt_day(day)
    css = ("<style>.st{display:flex;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px solid #f3f2f1}"
           ".st .ic{width:18px;flex:none;text-align:center}.st.run .ic{animation:sp 1s linear infinite;display:inline-block}"
           "@keyframes sp{to{transform:rotate(360deg)}}.st .tm{margin-left:auto;color:#616161;font-size:9.5pt;flex:none}"
           ".st .it{font-size:9.5pt;color:#616161;margin-top:2px}#det{font:9.5pt/1.4 Consolas,monospace;max-height:260px;"
           "overflow:auto;background:#faf9f8;border:1px solid #edebe9;padding:6px 8px;white-space:pre-wrap}"
           "#el{color:#616161;font-size:10pt}</style>")
    js = ("<script>(function(){var u='progress?t=" + html.escape(t) + "',box=document.getElementById('steps'),"
          "det=document.getElementById('det'),res=document.getElementById('res'),el=document.getElementById('el');"
          "function e(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}"
          "function sec(a,b){var x=Math.max(0,Math.round(b-a));return x<60?x+'s':Math.floor(x/60)+'m '+(x%60)+'s';}"
          "function draw(s){var h='';(s.steps||[]).forEach(function(x,i){var last=i===s.steps.length-1,run=last&&s.state==='running';"
          "h+='<div class=\"st'+(run?' run':'')+'\"><span class=ic>'+(run?'&#9696;':'&#10003;')+'</span><div><b>'+e(x.step)+'</b> &mdash; '+e(x.text)"
          "+(x.items&&x.items.length?'<div class=it>'+x.items.slice(0,40).map(e).join(' &middot; ')+(x.items.length>40?' &hellip; and '+(x.items.length-40)+' more':'')+'</div>':'')"
          "+'</div><span class=tm>'+sec(x.at,x.done||s.now)+'</span></div>';});box.innerHTML=h||'<p class=sub>Starting&hellip;</p>';"
          "var dd=(s.detail||[]).map(function(x){return e(x.text);}).join('\\n');if(det.innerHTML!==dd){var b=det.scrollTop+det.clientHeight>=det.scrollHeight-8;"
          "det.innerHTML=dd;if(b)det.scrollTop=det.scrollHeight;}"
          "if(s.started)el.textContent='Elapsed: '+sec(s.started,s.finished||s.now);"
          "if(s.state&&s.state!=='running'){var ok=s.state==='done'||s.state==='already';document.getElementById('hd').textContent=ok?'Finished':'Stopped';"
          "res.innerHTML='<div class=card><p class='+(ok?'ok':'warn')+'><b>'+({done:'Done',already:'Already answered',busy:'Not applied yet',"
          "error:'Something went wrong',none:'Nothing running'}[s.state]||s.state)+'</b></p><p>'+e(s.result||'')+'</p>'"
          "+(s.state==='done'?'<p class=sub>A confirmation email is on its way.</p>':'')+(s.state==='busy'||s.state==='error'?"
          "'<p><a href=\"../'+location.pathname.split('/').slice(-2,-1)[0]+'?t=" + html.escape(t) + "\">Back to the questions</a></p>':'')+'</div>';return true;}return false;}"
          "function tick(){fetch(u,{cache:'no-store'}).then(function(r){return r.json();}).then(function(s){if(!draw(s))setTimeout(tick,1000);})"
          ".catch(function(){setTimeout(tick,3000);});}tick();})();</script>")
    inner = ("<div class=card><p><b id=hd>Applying your answers&hellip;</b> <span id=el></span></p>"
             "<p class=sub>Each stage appears as it starts; you can leave this page open or close it (the work carries on).</p>"
             "<div id=steps></div></div><div id=res></div>"
             "<details class=card open><summary>What the jobs are doing (details)</summary><div id=det></div></details>")
    return opage(title, css + inner + js)


PCSS = ("<style>ul.tree{list-style:none;margin:0;padding-left:18px}ul.tree.top{padding-left:0}"
        "ul.tree li{margin:2px 0}ul.tree label{display:inline-flex;gap:8px;align-items:center;padding:4px 6px;border-radius:4px;cursor:pointer}"
        "ul.tree label:has(input:checked){background:#e8f5e9}details.add{display:inline-block;margin-left:6px}"
        "details.add summary{font-size:9.5pt;color:#483d8b;cursor:pointer;list-style:none}details.add input{width:16em;font:inherit;"
        "padding:4px 6px;margin:4px 0}input.newsub{width:100%;font:inherit;padding:6px 8px;margin:4px 0}"
        "h3{color:#483d8b;margin:16px 0 4px 0}</style>")


def _tree(paths):
    root = {}
    for p in paths:
        node = root
        for part in p.split("/"):
            node = node.setdefault(part, {})
    return root


def _tree_html(node, prefix, current, addable):
    out = []
    for name in sorted(node, key=str.lower):
        path = f"{prefix}/{name}" if prefix else name
        add = (f"<details class=add><summary>+ subfolder</summary><input name='sub::{html.escape(path)}' "
               f"placeholder='New subfolder of {html.escape(name)}'></details>") if addable else ""
        kids = _tree_html(node[name], path, current, addable) if node[name] else ""
        out.append(f"<li><label><input type=radio name=dest value='{html.escape(path)}'{' checked' if path == current else ''}>"
                   f"{html.escape(name)}</label>{add}{kids}</li>")
    return f"<ul class=tree>{''.join(out)}</ul>" if out else ""


@route("/organize/<day>/pick", methods=["GET", "POST"])
def organize_pick(day):
    """Choose another destination for one filing question: any existing folder, or a new subject / subfolder at
    any depth under Subjects (or Manuals). Saved on the day's proposal; applied with the other answers (2026-10-07)."""
    import organize
    t = request.values.get("t", "")
    f, prop = load_prop(day, t)
    nf_path = None
    try:
        if request.values.get("nf"):   # one note of a new-folder card (2026-10-13)
            qid = int(request.values.get("nf", ""))
            nfq = next((x for x in prop.get("new_folders", []) if x["id"] == qid), None)
            if not nfq:
                abort(404)
            nf_path = nfq["paths"][int(request.values.get("i", ""))]
            q = {"id": qid, "text": f"{nfq['folder']}: one note", "paths": [nf_path], "options": {"a": {"move_to": nfq["folder"]}}}
        else:
            qid = int(request.values.get("q", ""))
            q = next((x for x in prop.get("questions", []) if x["id"] == qid), None)
            if q and request.values.get("i", "") != "":   # one note of a several-note question (2026-10-15)
                one = q["paths"][int(request.values.get("i"))]
                q = dict(q, paths=[one], text=(q.get("text") or "") + " (this note only)")
    except (ValueError, IndexError):
        abort(404)
    if not q:
        abort(404)
    back = f"../{day}?t={t}#q{qid}"
    if prop.get("status") != "sent":
        return redirect(back)
    vault = Path(CFG["vault_path"])
    fset = [x for x in organize.folders(vault) if not x.startswith("Personal Notes")]
    fset = sorted(set(fset) | set(pending_folders(prop, fset)))   # folders made earlier on this page (2026-10-14)
    err = ""
    if request.method == "POST":
        dest = None
        for k, v in request.form.items():                 # a new folder typed anywhere wins over the radio choice
            if k.startswith("sub::") and v.strip():
                parent = k[5:]
                name = organize.clean_folder_name(v)
                if name and parent.startswith(organize.FIXED_SUBS) and (parent in fset or parent in organize.FIXED_SUBS):
                    dest = f"{parent}/{name}"
                else:
                    err = "That folder name can't be used; try another."
                break
        if not dest and not err:
            d = request.form.get("dest", "")
            dest = d if d in fset else None
            err = "" if dest else "Pick a folder, or type a name for a new one."
        if dest:
            fresh = jload(f, {})
            if fresh.get("status") == "sent":
                if nf_path:
                    fresh.setdefault("nf_pre", {}).setdefault(str(qid), {})[nf_path] = dest
                elif len(q["paths"]) == 1 and request.values.get("i", "") != "":
                    fresh.setdefault("q_pre", {}).setdefault(str(qid), {})[q["paths"][0]] = dest
                else:
                    fresh.setdefault("picks", {})[str(qid)] = dest
                jsave(f, fresh)
            return redirect(back)
    one_q = (not nf_path) and request.values.get("i", "") != ""
    cur = (((prop.get("nf_pre") or {}).get(str(qid)) or {}).get(nf_path) if nf_path
           else ((prop.get("q_pre") or {}).get(str(qid)) or {}).get(q["paths"][0]) if one_q
           else (prop.get("picks") or {}).get(str(qid))) \
        or (q["options"].get("a") or {}).get("move_to")
    items = {it["path"]: it for it in prop["items"]}
    what = "".join(f"<div><b>{html.escape(items.get(p, {}).get('rename_to') or p.rsplit('/', 1)[-1][:-3])}</b></div>" for p in q["paths"])
    subj = [x for x in fset if x.startswith("Subjects/")]
    man = [x for x in fset if x.startswith("Manuals/")]
    other = [x for x in fset if not x.startswith(organize.FIXED_SUBS) and x not in organize.FIXED_SUBS]
    t_subj = _tree_html(_tree([x[len("Subjects/"):] for x in subj]), "Subjects", cur, True)
    t_man = _tree_html(_tree([x[len("Manuals/"):] for x in man]), "Manuals", cur, True)
    t_oth = _tree_html(_tree(other), "", cur, False)
    inner = (f"<p><a href='{html.escape(back)}'>&larr; Back to the questions</a> (nothing changes until you press Use this folder)</p>"
             + (f"<div class=card><p class=warn>{html.escape(err)}</p></div>" if err else "")
             + f"<div class=card><div class=sub>{qid}. {html.escape(q.get('text', ''))}</div>{what}</div>"
             f"<form method=post><input type=hidden name=t value='{html.escape(t)}'>"
             + (f"<input type=hidden name=nf value={qid}><input type=hidden name=i value='{html.escape(request.values.get('i', ''))}'>"
                if nf_path else f"<input type=hidden name=q value={qid}>"
                + (f"<input type=hidden name=i value='{html.escape(request.values.get('i', ''))}'>" if request.values.get("i", "") != "" else "")) +
             f"<div class=card><h3 style='margin-top:0'>Subjects</h3>"
             f"<div class=sub>New subject:</div><input class=newsub name='sub::Subjects' placeholder='Name of a new subject folder'>"
             f"<div class=sub>&hellip;or choose a folder below (+ subfolder adds one under it, at any depth):</div>{t_subj}</div>"
             f"<div class=card><h3 style='margin-top:0'>Manuals</h3>{t_man}"
             f"<details class=add><summary>+ new Manuals folder</summary><input name='sub::Manuals' placeholder='New Manuals folder'></details></div>"
             f"<div class=card><h3 style='margin-top:0'>Other places</h3>{t_oth}</div>"
             f"<div class=bar><button class=pri>Use this folder</button></div></form>")
    return opage(f"Choose a folder ({U_day(day)})", PCSS + inner)


def U_day(day):
    import update_email as U
    return U.fmt_day(day)


@route("/open")
def open_in_obsidian():
    """Clickable https link that opens a vault note in Obsidian (2026-10-06): chat apps and some mail
    clients don't make obsidian:// links clickable. /vault/open?f=<vault-relative path, .md optional>.
    Only existing .md notes inside the vault; shows nothing of the note itself."""
    from urllib.parse import quote
    rel = (request.values.get("f") or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        abort(404)
    rel = rel[:-3] if rel.lower().endswith(".md") else rel
    vault = Path(CFG["vault_path"])
    p = (vault / (rel + ".md")).resolve()
    if not str(p).startswith(str(vault.resolve())) or not p.is_file():
        LOG(f"open: no note {rel!r} under {vault}")
        return opage("Open in Obsidian", f"<div class=card><p><b>No such note in the vault</b></p><p>{html.escape(rel)}.md "
                     f"was not found under {html.escape(str(vault))} (moved or renamed since the link was made?).</p></div>"), 404
    url = f"obsidian://open?vault={quote(vault.name)}&file={quote(rel)}"
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv=refresh content='0;url={html.escape(url)}'><title>Open in Obsidian</title>{OCSS}"
            f"<div class=wrap><div class=card><p><b>{html.escape(p.stem)}</b></p>"
            f"<p><a href='{html.escape(url)}'>Open in Obsidian</a> (if it didn't open by itself)</p></div></div>")


QCSS = ("<style>.qbar{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.qbar span{background:#fff;border:1px solid #e1dfdd;"
        "border-radius:14px;padding:3px 12px;font-size:10.5pt}.qbar b{color:#483d8b}h3.qh{margin:22px 0 4px 0;color:#483d8b}"
        ".qh small{color:#8a8886;font-weight:normal;font-size:10pt}.qi{background:#fff;border:1px solid #e1dfdd;border-radius:6px;"
        "margin:8px 0;padding:10px 12px}.qi.need{border-left:4px solid #c4314b}.qi .t{font-weight:bold}.qi .m{font-size:10pt;color:#616161}"
        ".qi .why{font-size:10pt;color:#424242;margin-top:2px}.qa{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;align-items:center}"
        ".qa button,.qe button{font-size:10pt;padding:5px 11px}.qa select{font:inherit;font-size:10pt}"
        "details.qe{margin-top:8px;border-top:1px dashed #e1dfdd;padding-top:6px}details.qe summary{cursor:pointer;color:#483d8b;font-weight:bold;font-size:10.5pt}"
        ".qe .pv{font-size:10pt;color:#424242;background:#faf9f8;border-radius:4px;padding:8px;margin:6px 0;max-height:12em;overflow:auto}"
        ".qe label{display:block;font-size:9.5pt;color:#616161;margin-top:6px}.qe input,.qe textarea{font:inherit;font-size:10.5pt;width:100%;"
        "box-sizing:border-box;border:1px solid #c8c6c4;border-radius:4px;padding:5px 7px}.sugg{background:#e8f0fe;border-radius:4px;"
        "padding:6px 8px;font-size:10pt;margin:6px 0}.num{color:#8a8886;font-weight:normal}</style>")
QROUTES = ("article", "document", "summary", "book", "recipe", "manual", "analyze", "analyze_long", "auto")


def _q_cmd(argv):
    """Run one vqueue command in-process (the same code as the command line on this PC)."""
    import contextlib
    import io
    import vqueue as Q
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            Q.main(argv)
        except SystemExit:
            pass
    return buf.getvalue().strip()


def _q_details(cfg, store, x):
    """(display name, current fields, preview text, Obsidian link or None) for an item's Open & edit panel."""
    sys.path.insert(0, str(C.VL))
    from vaultlib import split_frontmatter
    ref = x.get("source_ref") or {}
    vault = Path(cfg["vault_path"])
    if ref.get("path") and (vault / ref["path"]).exists():
        fm, _, body = split_frontmatter((vault / ref["path"]).read_text(encoding="utf-8", errors="replace"))
        fm = fm if isinstance(fm, dict) else {}
        name = Path(ref["path"]).stem
        f = {"name": name, "title": fm.get("title") or name, "description": fm.get("description") or "",
             "author": ", ".join(fm.get("author") or []) if isinstance(fm.get("author"), list) else (fm.get("author") or ""),
             "published": str(fm.get("published") or ""), "tags": ", ".join(fm.get("tags") or []) if isinstance(fm.get("tags"), list) else "",
             "my_note": fm.get("my_note") or ""}
        from urllib.parse import quote
        return name, f, body, f"open?f={quote(ref['path'][:-3])}"
    meta, text = {}, ""
    try:
        pay = store.payload_root / x["id"]
        meta = jload(pay / "extract.json", {})
        text = (pay / "text.md").read_text(encoding="utf-8", errors="replace")[:6000] if (pay / "text.md").exists() else ""
    except Exception:  # noqa: BLE001
        pass
    name = x.get("title") or "?"
    au = meta.get("author") or []
    f = {"name": "", "title": meta.get("title") or name, "description": meta.get("description") or "",
         "author": ", ".join(au) if isinstance(au, list) else str(au), "published": str(meta.get("published") or ""),
         "tags": "", "my_note": x.get("my_note") or ""}
    return name, f, text, None


@route("/organize/<day>/queue", methods=["GET", "POST"])
def organize_queue(day):
    """The intake queue (intake plan phase 4, redesigned 2026-10-06): what needs the owner first, then tonight's run and the
    daytime runs in order; each item can be opened (preview) and its name and details edited before deciding.
    Same key as the day's questions; also linked from /status."""
    import vqueue as Q
    import titlecheck
    t = request.values.get("t", "")
    load_prop(day, t)
    cfg = C.load_config()
    store = Q.Store(cfg)
    msg = ""
    if request.method == "POST":
        iid, act = request.form.get("id", ""), request.form.get("action", "")
        if not re.fullmatch(r"[0-9a-f-]{6,40}", iid or ""):
            abort(400)
        if act == "save":
            x = store.get(iid)
            if not x:
                abort(404)
            g = lambda k: (request.form.get(k) or "").strip()[:2000]
            fields = {"name": g("name"), "title": g("title"), "description": g("description"), "published": g("published"),
                      "author": [a.strip() for a in g("author").split(",") if a.strip()],
                      "tags": [a.strip() for a in g("tags").split(",") if a.strip()], "my_note": g("my_note")}
            if fields["name"] and re.search(r'[\\/:*?"<>|#^\[\]]', fields["name"]):
                msg = "<div class=card><p class=warn><b>Not saved:</b> a note name can't contain \\ / : * ? \" &lt; &gt; | # ^ [ ]</p></div>"
            else:
                res = with_vault_lock(lambda: titlecheck.apply(cfg, LOG, store, x, fields), "queue page edit")
                ok = isinstance(res, str)
                LOG(f"queue page: save {iid} -> {res if ok else res.get('why')}")
                msg = (f"<div class=card><p class=ok><b>Saved:</b> {html.escape(res)}</p></div>" if ok else
                       f"<div class=card><p class=warn><b>Not saved:</b> {html.escape(res.get('why', ''))}</p></div>")
        else:
            argv = {"top": ["top", iid], "hold": ["hold", iid, "held on the queue page"], "release": ["release", iid],
                    "skip": ["skip", iid], "route": ["route", iid, request.form.get("route", "")]}.get(act)
            if not argv or (act == "route" and argv[2] not in QROUTES):
                abort(400)
            out = _q_cmd(argv)
            LOG(f"queue page: {' '.join(argv)} -> {out[:120]}")
            label = {"top": "moved to the front", "hold": "held", "release": "released", "skip": "skipped",
                     "route": "route changed"}[act]
            msg = f"<div class=card><p class=ok><b>Done:</b> {html.escape(label)}</p></div>"
        store = Q.Store(cfg)
    items = store.all()
    order = Q.ordered(items, cfg, statuses=("ready", "working", "failed"))
    night = [y for y in order if Q.route_of(y) in Q.COSTLY]
    day_ = [y for y in order if Q.route_of(y) not in Q.COSTLY]
    pos = {x["id"]: i for i, x in enumerate(night, 1)}
    pos.update({x["id"]: i for i, x in enumerate(day_, 1)})
    need = [x for x in items if x["status"] == "held"]
    need.sort(key=lambda x: ((x.get("work") or {}).get("hold_kind") != "title", x.get("added") or ""))
    waiting = [x for x in items if x["status"] in ("new", "extracted", "waiting")]
    done = sorted([x for x in items if x["status"] in Q.FINAL], key=lambda x: x.get("updated") or "", reverse=True)[:15]
    KIND = {"title": "Name check", "duplicate": "Already in the vault?", "costly": "Large item: OK?",
            "confidential": "Firm or client material?", "notext": "No readable text"}
    esc = html.escape

    def card(x, actions):
        name, f, preview, olink = _q_details(cfg, store, x)
        fl = x.get("file") or {}
        words = fl.get("words") or x.get("words")
        size = ", ".join(v for v in (f"{fl['pages']} pp" if fl.get("pages") else "", f"{words:,} words" if words else "") if v)
        hk = (x.get("work") or {}).get("hold_kind")
        chip = f"<span class=chip>{esc(KIND.get(hk, x.get('needs') or ''))}</span> " if x["status"] == "held" and (hk or x.get("needs")) else ""
        n = f"<span class=num>#{pos[x['id']]}</span> " if x["id"] in pos else ""
        hid = f"<input type=hidden name=t value='{esc(t)}'><input type=hidden name=id value='{x['id']}'>"
        b = lambda a, l: f"<button class=sec name=action value={a}>{l}</button>"
        acts = "".join(b(a, l) for a, l in (("top", "Do next"), ("hold", "Hold"), ("release", "Release"), ("skip", "Skip")) if a in actions)
        if "route" in actions:
            sel = "".join(f"<option{' selected' if r == Q.route_of(x) else ''}>{r}</option>" for r in QROUTES)
            acts += f"<select name=route>{sel}</select>{b('route', 'Change route')}"
        sug = x.get("title_suggest") or {}
        stext = ""
        if sug.get("title") or sug.get("name"):
            stext = (f"<div class=sugg><b>Suggested:</b> {esc(sug.get('name') or sug.get('title'))}"
                     + (f" &middot; {esc(', '.join(sug.get('author') or []))}" if sug.get("author") else "")
                     + (f" &middot; {esc(str(sug.get('published')))}" if sug.get("published") else "")
                     + " &mdash; filled in below; change anything, then Save.</div>")
            f = dict(f, name=sug.get("name") or f["name"], title=sug.get("title") or f["title"],
                     author=", ".join(sug.get("author") or []) or f["author"], published=str(sug.get("published") or f["published"]))
        inp = lambda k, label, area=False: (f"<label>{label}</label>" + (f"<textarea name={k} rows=2>{esc(f.get(k, ''))}</textarea>" if area
                                            else f"<input name={k} value=\"{esc(f.get(k, ''))}\">"))
        fields = ((inp("name", "Note name in the vault (renaming updates links to it)") if f["name"] else "")
                  + inp("title", "Title") + inp("description", "Description", True) + inp("author", "Author(s), comma-separated")
                  + inp("published", "Published (YYYY or YYYY-MM-DD)") + (inp("tags", "Tags, comma-separated") if f["name"] else "")
                  + inp("my_note", "Your note (my_note)", True))
        pv = " ".join(re.sub(r"(?s)^---.*?\n---\n", "", preview or "").split()[:220])
        openl = f"<a href='../../{olink}' target=_blank>Open in Obsidian</a>" if olink else ""
        panel = (f"<details class=qe{' open' if hk == 'title' else ''}><summary>Open &amp; edit</summary>{stext}"
                 + (f"<div class=pv>{esc(pv)}{'…' if pv else ''}</div>" if pv else "<div class=pv>No text to preview yet.</div>")
                 + (f"<div class=lnk>{openl}</div>" if openl else "")
                 + f"<form method=post>{hid}{fields}<div class=qa><button class=pri name=action value=save>Save details</button>"
                 + "</div></form></details>")
        return (f"<div class='qi{' need' if x['status'] == 'held' else ''}'><div class=t>{n}{esc(name)}</div>"
                f"<div class=m>{chip}<span class=chip>{esc(Q.route_of(x))}</span> {esc(x['status'])}"
                + (f" &middot; {esc(size)}" if size else "") + "</div>"
                f"<div class=why>{esc(str(x.get('why') or ''))[:220]}</div>"
                f"<form method=post class=qa>{hid}{acts}</form>{panel}</div>")

    def section(title, sub, rows, empty="Nothing here."):
        return f"<h3 class=qh>{title} <small>{sub}</small></h3>" + ("".join(rows) or f"<p class=note>{empty}</p>")
    bar = (f"<div class=qbar><span>Needs you <b>{len(need)}</b></span><span>Tonight <b>{len(night)}</b></span>"
           f"<span>Daytime <b>{len(day_)}</b></span><span>Being read <b>{len(waiting)}</b></span></div>")
    usage = usage_section(day, t).replace(f"action='{day}/usage'", "action='usage'").replace(f"href='{day}/usage", "href='usage")
    body = (QCSS + msg + usage + bar
            + "<p class=note>Open &amp; edit shows the start of each item and lets you fix its name and details before it is "
              "filed or analyzed. Do next moves an item to the front of its run; Hold keeps it out until you release it.</p>"
            + section("Needs you", "answer here or in the morning email", [card(x, ("release", "skip", "route")) for x in need])
            + section("Tonight", "books, long summaries and long analyses, 11:45 PM to 1:55 AM, in this order",
                      [card(x, ("top", "hold", "skip", "route")) for x in night[:40]])
            + section("Daytime", "articles, analyses and other short items, every 30 minutes, in this order",
                      [card(x, ("top", "hold", "skip", "route")) for x in day_[:40]])
            + (f"<p class=note>and {len(day_) - 40} more</p>" if len(day_) > 40 else "")
            + section("Being read", "files still being extracted", [card(x, ("hold", "skip")) for x in waiting[:30]])
            + section("Recently finished", "", [f"<div class=qi><div class=t>{esc(x.get('title') or '?')}</div><div class=m>"
                      f"<span class=chip>{esc(Q.route_of(x))}</span> {esc(x['status'])} &middot; {esc(str(x.get('updated') or '')[:16])}"
                      f"</div><div class=why>{esc(str(x.get('why') or ''))[:200]}</div></div>" for x in done]))
    return opage("Intake queue", body)


def latest_queue_link():
    """For /status: the queue page under the newest day's key."""
    for f in sorted((C.APP / "reports").glob("organize-*.json"), reverse=True)[:5]:
        p = jload(f, {})
        if p.get("web_token"):
            return f"organize/{f.stem[9:]}/queue?t={p['web_token']}"
    return None


@route("/organize/<day>/usage")
def organize_usage(day):
    """Tonight's usage mode from the email buttons / the page (2026-10-06): mode=more|light|normal."""
    import usage_plan
    t = request.values.get("t", "")
    load_prop(day, t)                               # same token as the day's questions
    back = f"<p><a href='../{day}/queue?t={html.escape(t)}'>Intake queue</a> &middot; <a href='../{day}?t={html.escape(t)}'>The day's questions</a></p>"
    if request.values.get("cancel"):
        usage_plan.cancel_window(CFG, request.values["cancel"])
        LOG(f"extra window cancelled: {request.values['cancel']} (web)")
        return opage("Extra run window", f"<div class=card><p class=ok><b>Window cancelled.</b></p>{back}</div>")
    if request.values.get("window"):
        hhmm = request.values["window"]
        if not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
            abort(400)
        try:
            w = usage_plan.add_window(CFG, usage_plan.next_at(CFG, hhmm.zfill(5)), request.values.get("hours", 5),
                                      request.values.get("pct", 90), "web")
        except ValueError:
            abort(400)
        LOG(f"extra window booked: {w} (web)")
        warn = usage_plan.window_warning(w)
        return opage("Extra run window", f"<div class=card><p class=ok><b>Booked: {html.escape(usage_plan.window_label(w))}</b></p>"
                     f"<p>{C.pc_name()} works through the queue (books and long analyses first, then other analyses) until the "
                     "5-hour session reaches that share, and never past the week's goal.</p>"
                     + (f"<p class=warn>{html.escape(warn)}</p>" if warn else "") + back + "</div>")
    mode = request.values.get("mode", "")
    if mode not in usage_plan.MODES:
        abort(400)
    rec = usage_plan.set_mode(CFG, mode, "web")
    LOG(f"usage mode for the night of {rec['night']}: {mode} (web)")
    return opage("Tonight's vault run",
                 f"<div class=card><p class=ok><b>{html.escape(usage_plan.LABEL[mode])}</b></p>"
                 f"<p>Set for the night window starting {html.escape(rec['night'])} evening. It lasts one night; "
                 f"you can change it again any time before then.</p>{back}</div>")


def usage_section(day, t):
    """The 'Claude usage' block on the questions page (same content as the email)."""
    try:
        import update_email as U
        import usage_plan
        fc = usage_plan.forecast(CFG)
        inner = U.usage_card(CFG, day, {"web_token": t}, fc)
        cur = usage_plan.LABEL.get(fc.get("mode", "normal"), "Normal")
        wl = "".join(f"<div class=sub>Extra window: <b>{html.escape(w['label'])}</b> "
                     f"<a href='{day}/usage?t={html.escape(t)}&cancel={html.escape(w['start'])}'>cancel</a>"
                     + (f"<br><span class=warn>{html.escape(w['warning'])}</span>" if w.get("warning") else "") + "</div>"
                     for w in fc.get("windows") or [])
        opts = "".join(f"<option value='{h:02d}:00'{' selected' if h == 2 else ''}>{(h % 12) or 12} {'AM' if h < 12 else 'PM'}</option>"
                       for h in range(24))
        form = (f"<form action='{day}/usage' method=get style='display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px;font-size:10.5pt'>"
                f"<input type=hidden name=t value='{html.escape(t)}'>Extra window: start <select name=window>{opts}</select>"
                "for <select name=hours>" + "".join(f"<option{' selected' if h == 5 else ''}>{h}</option>" for h in range(1, 6))
                + "</select> h, up to <select name=pct>" + "".join(f"<option{' selected' if p == 90 else ''}>{p}</option>"
                                                                       for p in (50, 60, 70, 80, 90, 95))
                + "</select> % of the session <button class=sec style='padding:5px 11px;font-size:10pt'>Book it</button></form>")
        return (f"<div class=card style='border-left-color:#483d8b'>{inner}<div class=sub>Tonight is set to: <b>{html.escape(cur)}</b></div>"
                f"{wl}{form}</div>")
    except Exception as e:  # noqa: BLE001  never let it stop the page
        LOG(f"usage section skipped: {type(e).__name__}: {e}")
        return ""


def notes_after(day, t, prop):
    """After the questions are answered: the notes asked about that still have no my_note, and the new ones."""
    import note_view as V
    mnu = f"{day}/mynote"
    rows = []
    for p in sorted(V.allowed_notes(prop)):
        if "Archives/" in p or V.note_my_note(CFG, prop, p) != "":
            continue
        cur = V.find_note(CFG["vault_path"], p, prop)
        rel = cur.relative_to(CFG["vault_path"]).as_posix()
        rows.append(f"<div style='margin:8px 0'><b><a class=nm href='{html.escape(day + '/note' + V.qs(t, p=p))}' target=_blank>"
                    f"{html.escape(cur.stem)}</a></b> <span class=sub>({html.escape(rel.rsplit('/', 1)[0] if '/' in rel else '')})</span>"
                    f"{V.mn_button(mnu, t, p=p)}</div>")
    asked = ("<h3 style='margin:18px 0 0 0;color:#483d8b'>Add your notes</h3><div class=card><div class=sub>Items from these "
             f"questions with no note of yours yet.</div>{''.join(rows)}</div>") if rows else ""
    return f"<style>{V.MN_CSS}</style>" + asked + V.fresh_section(CFG, prop, day, t, day + "/") + V.MN_JS


@route("/organize/<day>/mynote", methods=["POST"])
def organize_mynote(day):
    """Save the owner's one-line note: into a vault note's my_note now, or onto a queue/held item for when it's filed."""
    import my_note
    import note_view as V
    t = request.form.get("t", "")
    f, prop = load_prop(day, t)
    text = my_note.clean(request.form.get("text", ""))
    if not text:
        return jsonify({"ok": False, "why": "empty note"})
    p, qid = request.form.get("p"), request.form.get("q")
    if p:
        fresh = {x["path"] for x in V.fresh_list(CFG, prop)}
        if p not in V.allowed_notes(prop) and p not in fresh:
            abort(404)
        note = V.find_note(CFG["vault_path"], p, prop)
        if not note:
            return jsonify({"ok": False, "why": "the note isn't in the vault any more"})
        rel = note.relative_to(CFG["vault_path"]).as_posix()
        res = with_vault_lock(lambda: my_note.set_my_note(CFG, LOG, rel, text), f"{C.pc_name()} my_note (answer page)")
        if isinstance(res, dict):
            return jsonify({"ok": False, "why": res["why"]})
        ok, msg = res
        return jsonify({"ok": ok, "text": text, "why": None if ok else msg})
    q = V.question(prop, qid) if qid else None
    if not q:
        abort(404)
    import vqueue as Q
    store = Q.Store(CFG)
    it = store.get(q["item"]) if q.get("item") else None
    if it:
        if it.get("status") == "done" and (it.get("result") or {}).get("notes"):   # filed meanwhile: write it now
            res = with_vault_lock(lambda: my_note.apply_pending(CFG, LOG, dict(it, my_note=text), it["result"]["notes"]),
                                  f"{C.pc_name()} my_note (answer page)")
            if isinstance(res, dict):
                return jsonify({"ok": False, "why": res["why"]})
        it["my_note"] = text
        store.save(it)
        return jsonify({"ok": True, "text": text})
    if q.get("kind") == "held" and q.get("key"):   # a link waiting for the laptop: its note goes with it
        import held_bridge as HB
        fresh_h = HB.load()
        if q["key"] in fresh_h:
            fresh_h[q["key"]]["doug_note"] = text
            Q.atomic_write(HB.held_path(), json.dumps(fresh_h, ensure_ascii=False, indent=1))
            return jsonify({"ok": True, "text": text})
    return jsonify({"ok": False, "why": "this item is no longer waiting"})


def _view_prop(day):
    t = request.args.get("t", "")
    return t, load_prop(day, t)[1]


def _send(path):
    import note_view as V
    p, mt = V.file_response(path)
    return send_file(p, mimetype=mt, as_attachment=False, download_name=p.name, conditional=True)


@route("/organize/<day>/note")
def organize_note(day):
    import note_view as V
    t, prop = _view_prop(day)
    h = V.note_page(CFG, prop, day, t, request.args.get("p", ""), full=bool(request.args.get("all")))
    if h is None:
        abort(404)
    return h


@route("/organize/<day>/compare")
def organize_compare(day):
    import note_view as V
    t, prop = _view_prop(day)
    h = V.compare_page(CFG, prop, day, t, request.args.get("q", ""), only_diff=bool(request.args.get("diff")),
                       full=bool(request.args.get("all")))
    if h is None:
        abort(404)
    return h


@route("/organize/<day>/original")
def organize_original(day):
    import note_view as V
    from vaultlib import split_frontmatter
    t, prop = _view_prop(day)
    rel = request.args.get("p", "")
    if rel not in V.allowed_notes(prop):
        abort(404)
    note = V.find_note(CFG["vault_path"], rel, prop)
    sp = split_frontmatter(note.read_text(encoding="utf-8", errors="replace")) if note else (None, None, "")
    fm = sp[0]
    orig = V.original_of(CFG["vault_path"], fm if isinstance(fm, dict) else {}, sp[2])
    if not orig:
        abort(404)
    return _send(orig)


@route("/organize/<day>/dropbox")
def organize_dropbox(day):
    import note_view as V
    t, prop = _view_prop(day)
    q = V.question(prop, request.args.get("q", ""))
    if not q or not str(q.get("where") or "").startswith("dropbox: "):
        abort(404)
    f, why = V.dropbox_file(CFG, q)
    if not f:
        return opage(q.get("title") or "File", f"<div class=card>The file can't be opened: {html.escape(why)}.</div>"), 404
    return _send(f)


@route("/api/note", methods=["POST"])
def api_note():
    if not secrets.compare_digest(request.headers.get("X-Vault-Token", ""), token()):
        abort(403)
    d = request.get_json(force=True, silent=True) or {}
    meta, body = d.get("meta") or {}, (d.get("body") or "").strip()
    if meta.get("type") not in NOTE_TYPES or not meta.get("title") or not meta.get("resource") or not body:
        return jsonify({"ok": False, "error": f"need meta.type in {sorted(NOTE_TYPES)}, meta.title, meta.resource and body"}), 400
    meta = {k: v for k, v in meta.items() if k in ALLOWED_KEYS and v not in (None, "", [])}
    by = d.get("generated_by") or "process:daily-digest"

    def write():
        sys.path.insert(0, str(C.UV))
        import url_to_note as U  # reuse its helpers: duplicate check, safe names, YAML lines, log.md
        vault = Path(CFG["vault_path"])
        dup = U.find_existing(vault, [meta["resource"]])
        if dup:
            return {"ok": True, "duplicate": True, "note": dup}
        inbox = vault / "Inbox"
        inbox.mkdir(exist_ok=True)
        name = U.safe_name(meta["title"], str(inbox))
        now = C.now(CFG).isoformat(timespec="seconds")
        order = ["type", "title", "description", "resource"] + [k for k in meta if k not in ("type", "title", "description", "resource")]
        fm = (["---"] + [U.yaml_line(k, meta[k]) for k in order if k in meta] + [U.yaml_line("generated", {"by": by, "at": now})]
              + U.owner_props(meta.get("type"), set(meta)) + ["---", ""])   # see_also / my_note last (2026-09-30)
        import yaml
        yaml.safe_load("\n".join(fm[1:-2]))
        (inbox / f"{name}.md").open("x", encoding="utf-8", newline="\n").write("\n".join(fm) + body + "\n")
        U.append_log(str(vault), f"- Created `Inbox/{name}.md` ({meta['type']}) from <{meta['resource']}> via {by}")
        return {"ok": True, "note": f"Inbox/{name}.md"}
    res = with_vault_lock(write, f"{C.pc_name()} vault service")
    LOG(f"api note {meta.get('type')} {meta.get('resource')} -> {res}")
    return jsonify(res), (200 if res.get("ok") else 409)


import home_page  # noqa: E402  the home page (plan: 2026-11-12 Home page - plan.md)
home_page.register(sys.modules[__name__])


if __name__ == "__main__":
    token()
    LOG(f"vault service on 127.0.0.1:{PORT}")
    try:
        from waitress import serve
        serve(app, host="127.0.0.1", port=PORT, threads=6)
    except ImportError:
        app.run(host="127.0.0.1", port=PORT, threaded=True)
