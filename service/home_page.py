"""The vault home page (plan: 2026-11-12 Home page - plan.md). Registered by vault_server.py.

  GET  /vault/home?t=<home token>&tab=today|status|settings
  POST /vault/home/settings?t=   save one group of settings (times, pacing, claude, switches)
  POST /vault/home/undo?t=       undo the last settings change
  POST /vault/home/run?t=        run a task now
  POST /vault/home/reset?t=      new home link (the old one stops working)

The home token is in %LOCALAPPDATA%\\VaultLibrarian\\credentials\\home_token.txt (made on first use). Every link and
form here is relative, so it works whether or not Tailscale strips /vault. Answering opens the existing day page
(organize/<day>?t=<that day's token>), so there is one answering form for the email links and the home page.
Each block is wrapped: one that can't be read says so and the rest of the page still shows.
"""
import html
from urllib.parse import quote

import common as C
import home_data as H
import settings_store as SS

S = None            # the vault_server module (set by register)


def e(x):
    return html.escape(str(x if x is not None else ""))


CSS = ("<style>.tabs{display:flex;gap:4px;margin:12px 0 0 0}.tabs a{flex:1;text-align:center;padding:9px 4px;border-radius:6px 6px 0 0;"
       "background:#e1dfdd;color:#483d8b;text-decoration:none;font-weight:bold}.tabs a.on{background:#fff;border:1px solid #e1dfdd;border-bottom:none}"
       ".banner{background:#fff4ce;border:1px solid #f2c94c;border-radius:4px;padding:10px 12px;margin:12px 0}"
       ".msg{background:#e8f5e9;border:1px solid #a5d6a7;border-radius:4px;padding:10px 12px;margin:12px 0}"
       ".err{background:#fde7e9;border:1px solid #f1aeb5;border-radius:4px;padding:10px 12px;margin:12px 0}"
       "table.t{border-collapse:collapse;width:100%;font-size:10.5pt}table.t td,table.t th{padding:4px 6px;border-bottom:1px solid #edebe9;"
       "text-align:left;vertical-align:top}.big{font-size:20pt;font-weight:bold;color:#483d8b}"
       ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}.grid div{background:#faf9f8;padding:8px;border-radius:4px}"
       ".row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}.row label{flex:1 1 260px}"
       ".row input[type=text],.row input[type=number],.row input[type=date]{width:120px;font:inherit;padding:5px}"
       "a.btn{display:inline-block;padding:9px 14px;border-radius:6px;background:#483d8b;color:#fff;text-decoration:none;font-weight:bold}"
       "h3{color:#483d8b;margin:18px 0 0 0}.bad{color:#a4262c}.good{color:#2e7d32}"
       "button.s{padding:6px 12px;font-size:10.5pt}</style>")


def page(title, tab, t, inner, msg=None, err=None, sub=False):
    tabs = "".join(f"<a class='{'on' if k == tab else ''}' href='home?t={e(t)}&tab={k}'>{lab}</a>"
                   for k, lab in (("today", "Today"), ("status", "Status"), ("settings", "Settings")))
    note = (f"<div class=msg>{e(msg)}</div>" if msg else "") + (f"<div class=err>{e(err)}</div>" if err else "")
    out = S.opage(title, f"{CSS}<div class=tabs>{tabs}</div>{note}{inner}")
    if sub:   # shown from /home/<x>: relative links resolve from /home, as on the page itself
        out = out.replace("<meta charset=utf-8>", "<meta charset=utf-8><base href='../home'>", 1)
    return out


def safe(fn, *a):
    try:
        return fn(*a)
    except Exception as ex:  # noqa: BLE001  never let one block stop the page
        S.LOG(f"home: {fn.__name__} failed: {type(ex).__name__}: {ex}")
        return f"<div class=card><span class=bad>Couldn't read this part ({e(type(ex).__name__)}).</span></div>"


def short_err(x):
    """'502 Server Error: Bad Gateway for url: https://…' -> '502 Server Error: Bad Gateway'."""
    return str(x or "").split(" for url:")[0].replace("HTTPError: ", "")[:160]


def when(iso):
    return e(str(iso or "")[:16].replace("T", " "))


# ---------------------------------------------------------------- Today
def mail_banner(cfg):
    m = H.mail(cfg)
    undelivered = [d for d in H.open_days() if (d["email"] or {}).get("sent") is False
                   and applying(d["day"]).get("state") != "running"]
    if not m["down"] and not undelivered:
        return ""
    bits = []
    if undelivered:
        bits.append("The update email for " + ", ".join(e(d["day"]) for d in undelivered)
                    + f" didn't go out. <b>Answer here</b>; {C.pc_name()} sends it again once mail works.")
    if m["down"]:
        r = m["read"]
        bits.append(f"Mail has been failing since {when(r.get('down_since') or m['send'].get('down_since'))} "
                    f"({e(short_err(r.get('last_error') or m['send'].get('last_error')))}). Email replies wait; "
                    "everything else carries on.")
    return "<div class=banner>" + "<br>".join(bits) + "</div>"


def applying(day):
    """The day page's answer job for this day, as the server holds it (state running|done|busy|already|error)."""
    with S._lock:
        st = dict(S.APPLY.get(day) or {})
        st["steps"] = list(st.get("steps") or [])
    return st


def latest_card(cfg, p):
    """The latest update once answered, or while its answers are being applied (2026-11-12: after answering,
    the page still offered 'Answer 31 question(s)' until the job finished)."""
    import time
    import update_email as U
    day, t = p["day"], p["web_token"]
    st = applying(day)
    title = f"<b>{e(U.fmt_day(day))}</b>"
    watch = f"organize/{e(day)}/applying?t={e(t)}"
    if p.get("status") == "applied":
        what = {"ok": " (all suggestions)", "skip": " (left everything as it was)"}.get(p.get("mode"), "")
        return (f"<div class=card style='border-left-color:#2e7d32'>{title} <span class=good>answered and applied</span>"
                f"<div class=sub>{e(p.get('via', ''))} at {when(p.get('applied'))}{e(what)} &middot; "
                f"{int(p.get('filed') or 0)} note(s) filed</div>"
                f"<div style='margin-top:6px'><a href='organize/{e(day)}?t={e(t)}'>See the results and add your notes</a></div></div>")
    if st.get("state") == "running":
        step = st["steps"][-1]["text"] if st["steps"] else "waiting for the vault"
        mins = int((time.time() - float(st.get("started") or time.time())) // 60)
        return (f"<div class=card style='border-left-color:#f2c94c'>{title} <span class=warn>your answers are being applied</span>"
                f"<div class=sub>Now: {e(step)} &middot; {len(st['steps'])} step(s) done or under way &middot; started {mins} min ago. "
                "This page refreshes itself.</div><div style='margin-top:6px'><a href='" + watch + "'>Watch the progress</a></div>"
                "<meta http-equiv=refresh content=15></div>")
    if st.get("state") in ("busy", "error"):
        return (f"<div class=card style='border-left-color:#a4262c'>{title} <span class=bad>your answers weren't applied</span>"
                f"<div class=sub>{e(st.get('result') or st.get('state'))}</div>"
                f"<div style='margin-top:6px'><a href='organize/{e(day)}?t={e(t)}'>Open the questions and try again</a></div></div>")
    return None


def open_block(cfg):
    import update_email as U
    latest = H.latest_day()
    done = latest_card(cfg, latest) if latest else None
    days = [] if done else H.open_days()
    out = [done] if done else []
    if not days and not done:
        out.append("<div class=card><b>No open questions.</b> <span class=sub>The latest update is answered.</span></div>")
    for i, d in enumerate(reversed(days)):         # newest first
        c = d["counts"]
        parts = [f"{c[k]} {lab}" for k, lab in (("filing", "filing"), ("tags", "new tag/folder"), ("duplicates", "duplicate"),
                                                  ("queue", "queue"), ("held", "held")) if c[k]]
        em = d["email"] or {}
        mail_ = ("<span class=good>emailed</span>" if em.get("sent") else "<span class=bad>email not delivered</span>"
                 if em.get("sent") is False else "<span class=sub>no email</span>" if em else "")
        out.append(f"<div class=card><b>{e(U.fmt_day(d['day']))}</b> <span class=sub>{mail_}</span>"
                   f"<div class=sub>{e(', '.join(parts) or 'no questions')} &middot; {d['items']} note(s) to file</div>"
                   f"<div style='margin-top:8px'><a class=btn href='{e(d['link'])}'>Answer {c['total']} question(s)</a></div></div>")
    return "".join(out) + new_block(cfg)


def new_block(cfg, t=None):
    n = H.new_since(cfg)
    if not n["notes"] and not n["waiting"]:
        return ""
    latest = H.latest_day() or {}
    busy = applying(latest.get("day", "")).get("state") == "running"
    rows = [f"<li>{e(x.rsplit('/', 1)[-1][:-3])} <span class=sub>({e(x.split('/', 1)[0])})</span></li>" for x in n["notes"][:20]]
    rows += [f"<li>{e(str(x)[:120])} <span class=sub>(waiting for you)</span></li>" for x in n["waiting"][:20]]
    more = len(n["notes"]) + len(n["waiting"]) - len(rows)
    ask = ("<form method=post action='home/run?t=" + e(_T[0]) + "'><input type=hidden name=what value=Organize>"
           "<button class='sec s'>Ask about these now</button> <span class=sub>Rebuilds today's questions with them "
           "(takes a few minutes; same link).</span></form>") if n["can_ask"] and not busy else (
        "<span class=sub>Your answers are being applied; ask about these once that's done.</span>" if busy else
        "<span class=sub>Today's questions are answered: these come in tomorrow morning's.</span>")
    return (f"<div class=card style='border-left-color:#8764b8'><b>New since that email</b> "
            f"<span class=sub>({len(n['notes']) + len(n['waiting'])}, not asked yet)</span>"
            f"<ul class=sub style='margin:6px 0;padding-left:18px'>{''.join(rows)}"
            + (f"<li>and {more} more</li>" if more > 0 else "") + f"</ul>{ask}</div>")


def results_block(cfg):
    rows = H.recent_results(5)
    if not rows:
        return ""
    tr = "".join(f"<tr><td>{e(r['day'])}</td><td>{e(r['via'])}<br><span class=sub>{when(r['applied'])}</span></td>"
                 f"<td>{r['filed']} filed &middot; {r['answered']} answered"
                 + (" (all suggestions)" if r["mode"] == "ok" else " (left in Inbox)" if r["mode"] == "skip" else "")
                 + f"<br><a href='{e(r['link'])}'>see / add notes</a></td></tr>" for r in rows)
    return f"<h3>Recent answers</h3><div class=card><table class=t>{tr}</table></div>"


def tonight_block(cfg):
    import update_email as U
    import usage_plan
    day = H.latest_day()
    if not day:
        return ""
    t, d = day["web_token"], day["day"]
    fc = usage_plan.forecast(cfg)
    inner = U.usage_card(cfg, d, {"web_token": t}, fc)
    cur = usage_plan.LABEL.get(fc.get("mode", "normal"), "Normal")
    base = f"organize/{d}/usage"
    wl = "".join(f"<div class=sub>Extra window: <b>{e(w['label'])}</b> "
                 f"<a href='{e(base)}?t={e(t)}&cancel={quote(w['start'])}'>cancel</a>"
                 + (f"<br><span class=warn>{e(w['warning'])}</span>" if w.get("warning") else "") + "</div>"
                 for w in fc.get("windows") or [])
    opts = "".join(f"<option value='{h:02d}:00'{' selected' if h == 2 else ''}>{(h % 12) or 12} {'AM' if h < 12 else 'PM'}</option>"
                   for h in range(24))
    form = (f"<form action='{e(base)}' method=get class=row style='font-size:10.5pt'><input type=hidden name=t value='{e(t)}'>"
            f"Extra window: start <select name=window>{opts}</select> for <select name=hours>"
            + "".join(f"<option{' selected' if h == 5 else ''}>{h}</option>" for h in range(1, 6))
            + "</select> h, up to <select name=pct>" + "".join(f"<option{' selected' if p == 90 else ''}>{p}</option>"
                                                                for p in (50, 60, 70, 80, 90, 95))
            + "</select> % of the session <button class='sec s'>Book it</button></form>")
    return (f"<h3>Tonight</h3><div class=card style='border-left-color:#483d8b'>{inner}"
            f"<div class=sub>Tonight is set to: <b>{e(cur)}</b></div>{wl}{form}</div>")


def needs_block(cfg):
    n = H.needs_you(cfg)
    rows = [f"<li>{e(x['title'])} <span class=sub>({e(x['route'])}: {e(str(x['why'])[:140])})</span></li>" for x in n["queue"][:15]]
    rows += [f"<li>{e(str(x['what'])[:120])} <span class=sub>(needs {e(x['needs'])}: {e(str(x['why'])[:120])})</span></li>"
             for x in n["held"][:15]]
    rows += [f"<li>{e(x)} <span class=sub>(Nightly {e(n.get('nightly_day'))})</span></li>" for x in n["nightly"]]
    rows += [f"<li class=bad>{e(x)}</li>" for x in n["failures"]]
    day = H.latest_day()
    ql = f"<p><a href='organize/{e(day['day'])}/queue?t={e(day['web_token'])}'>Manage the intake queue</a></p>" if day else ""
    if not rows:
        return f"<h3>Needs you</h3><div class=card>Nothing.{ql}</div>"
    return f"<h3>Needs you</h3><div class=card><ul style='margin:0;padding-left:18px'>{''.join(rows)}</ul>{ql}</div>"


SAVE = ("<h3>Save a link</h3><div class=card><form action='save' method=get><input name=url type=url required "
        "placeholder='https://…' style='width:100%;font:inherit;padding:6px'><textarea name=note rows=2 placeholder='Note (optional)' "
        "style='width:100%;font:inherit;padding:6px;margin-top:6px'></textarea><button class='pri s' style='margin-top:6px'>Save</button></form></div>")


_T = [""]      # the request's home token, for forms built deep inside a block


def today(cfg, t):
    _T[0] = t
    return (safe(mail_banner, cfg) + "<h3>Questions</h3>" + safe(open_block, cfg) + safe(results_block, cfg)
            + safe(tonight_block, cfg) + safe(needs_block, cfg) + SAVE)


# ---------------------------------------------------------------- Status
TASK_ORDER = ("Intake", "Organize", "SignInCheck", "QueueNight", "QueueWindow", "YouTubeRemember", "Nightly", "Originals",
              "Weekly", "Server", "Connector", "QueueNow")


def health_block(cfg):
    rows = {r["name"]: r for r in SS.tasks_info(cfg)}
    if not rows:
        return "<div class=card><span class=bad>Couldn't read Task Scheduler.</span></div>"
    names = [n for n in TASK_ORDER if n in rows] + sorted(n for n in rows if n not in TASK_ORDER)
    tr = "".join(f"<tr><td><b>{e(n)}</b><br><span class=sub>{e(rows[n]['state'])}</span></td><td>{e(rows[n]['last'])}<br>"
                 f"<span class='{'good' if rows[n]['ok'] else 'bad'}'>{e(rows[n]['result_text'])}</span></td>"
                 f"<td>{e(rows[n]['next'] or '—')}</td></tr>" for n in names)
    return f"<div class=card><table class=t><tr><th>Job</th><th>Last run</th><th>Next</th></tr>{tr}</table></div>"


def mail_block(cfg):
    m = H.mail(cfg)

    def line(name, x):
        if not x:
            return f"<tr><td>{name}</td><td class=sub>no record yet</td></tr>"
        st = (f"<span class=bad>failing ({x.get('failed_in_row')} in a row, since {when(x.get('down_since'))})</span><br>"
              f"<span class=sub>{e(short_err(x.get('last_error')))}</span>") if x.get("failed_in_row") else "<span class=good>working</span>"
        return f"<tr><td>{name}</td><td>{st}<br><span class=sub>last worked {when(x.get('last_ok')) or 'not yet'}</span></td></tr>"
    return f"<div class=card><table class=t>{line('Reading (replies, Remember)', m['read'])}{line('Sending', m['send'])}</table></div>"


def usage_block(cfg):
    import usage_plan
    fc = usage_plan.forecast(cfg)
    if not fc.get("ok"):
        return f"<div class=card>Usage couldn't be read: {e(fc.get('why'))}</div>"
    w = "".join(f"<li>{e(x)}</li>" for x in fc.get("warnings") or [])
    return (f"<div class=card><div class=grid><div><div class=big>{fc['week_pct']:.0f}%</div>week used</div>"
            f"<div><div class=big>{fc['session_pct']:.0f}%</div>5-hour session</div>"
            f"<div><div class=big>{fc.get('projected', 0):.0f}%</div>projected at reset (goal {fc.get('goal')}%)</div></div>"
            f"<div class=sub style='margin-top:6px'>Resets {when(fc.get('reset'))} &middot; {fc.get('nights_left')} night(s) left &middot; "
            f"tonight {len(fc.get('tonight_books') or [])} book(s), {fc.get('tonight_cheap', 0)} other item(s), about "
            f"{fc.get('tonight_share', 0)}% of the week</div>" + (f"<ul class=sub>{w}</ul>" if w else "") + "</div>")


def stats_block(cfg):
    st = H.vault_stats(cfg)
    if st.get("error"):
        return f"<div class=card><span class=bad>Search index unavailable: {e(st['error'])}</span></div>"
    tiles = "".join(f"<div><div class=big>{e(v)}</div>{e(k)}</div>" for k, v in (
        ("notes", f"{st.get('notes', 0):,}"), ("in Inbox", st.get("inbox")), ("in Clippings", st.get("clippings")),
        ("changed this week", st.get("changed_week")), ("analyses this week", st.get("analyses_week")),
        ("concepts", st.get("concepts")), ("open in the queue", st.get("queue_open"))))
    folders = ", ".join(f"{e(k)} {n:,}" for k, n in (st.get("by_folder") or [])[:10])
    types = ", ".join(f"{e(k)} {n:,}" for k, n in (st.get("by_type") or []))
    q = ", ".join(f"{e(r)} {e(s)} {n}" for (r, s), n in (st.get("queue") or [])[:10])
    s = st.get("search") or {}
    o = st.get("originals") or {}
    return (f"<div class=card><div class=grid>{tiles}</div>"
            f"<p class=sub><b>Folders:</b> {folders}</p><p class=sub><b>Types:</b> {types}</p>"
            f"<p class=sub><b>Queue:</b> {q or 'empty'}</p>"
            f"<p class=sub><b>Search index:</b> {s.get('passages') or 0:,} passages, {e(s.get('size_mb'))} MB, updated "
            f"{when(s.get('last_update'))}; originals {', '.join(f'{e(k)} {v}' for k, v in o.items()) or 'none'}.</p></div>")


def activity_block(cfg):
    a = H.activity(cfg)
    saves = "".join(f"<li>{'✓' if r.get('ok') else '✗'} {e(str(r.get('title') or r.get('url'))[:100])} "
                    f"<span class=sub>{when(r.get('at'))}</span></li>" for r in a["saves"][:8]) or "<li class=sub>none</li>"
    log = "".join(f"<li><span class=sub>{e(d)}</span> {e(x[:200])}</li>" for d, x in a["log"][:15]) or "<li class=sub>none</li>"
    con = "".join(f"<li>{e(r.get('tool'))} <span class=sub>{e(r.get('status'))} &middot; {e(str(r.get('result') or '')[:100])}</span></li>"
                  for r in a["connector"][:8]) or "<li class=sub>none</li>"
    return (f"<div class=card><b>Saved from the phone</b><ul class=sub>{saves}</ul><b>Vault log</b><ul class=sub>{log}</ul>"
            f"<b>Requests from Claude</b><ul class=sub>{con}</ul></div>")


def status(cfg, t):
    return ("<h3>Jobs</h3>" + safe(health_block, cfg) + "<h3>Mail connection</h3>" + safe(mail_block, cfg)
            + "<h3>Claude usage</h3>" + safe(usage_block, cfg) + "<h3>Vault and queue</h3>" + safe(stats_block, cfg)
            + "<h3>Recent activity</h3>" + safe(activity_block, cfg))


# ---------------------------------------------------------------- Settings
def field(sid, v):
    grp, label, kind, _p, bounds, task, hlp = SS.SPEC[sid]
    h = f"<br><span class=sub>{e(hlp)}</span>" if hlp else ""
    if kind == "bool":
        inp = (f"<input type=hidden name={sid} value=0><input type=checkbox name={sid} value=1{' checked' if v else ''} "
               "style='transform:scale(1.4)'>")
    elif kind == "date":
        inp = f"<input type=date name={sid} value='{e(v or '')}'>"
    elif kind == "choice":
        inp = (f"<select name={sid}>" + "".join(f"<option value='{e(o)}'{' selected' if o == v else ''}>{e(o.title())}</option>"
                                                for o in bounds) + "</select>")
    elif kind in ("int", "float", "hour"):
        step = "0.05" if kind == "float" else "1"
        lo, hi = bounds or ("", "")
        inp = f"<input type=number name={sid} value='{e(v)}' step={step} min='{lo}' max='{hi}'>"
    else:
        inp = f"<input type=text name={sid} value='{e(v)}' placeholder='{'Sun 03:30' if kind == 'weekly' else '06:00'}'>"
    return f"<div class=row><label>{e(label)}{h}</label>{inp}</div>"


def settings(cfg, t, confirm=None):
    cur = SS.current()
    a, b = SS.ds_slot(cfg)
    out = []
    for grp, title, note in (("times", "Run times", f"Checked before saving: the night jobs run in order, and Claude work stays "
                                                    f"out of the Daily Summary's {a}-{b} run. Saving also updates Task Scheduler."),
                             ("pacing", "Pacing of Claude usage", "How much of your Claude plan the vault work may use."),
                             ("claude", "Claude models and concepts",
                              "Which model does which job, and how the concept list is sent and tidied. "
                              "Short jobs (titles, descriptions, classification) always use Haiku."),
                             ("switches", "On and off", "")):
        rows = "".join(field(sid, cur.get(sid)) for sid, s in SS.SPEC.items() if s[0] == grp)
        out.append(f"<h3>{e(title)}</h3><div class=card>" + (f"<div class=sub>{e(note)}</div>" if note else "")
                   + f"<form method=post action='home/settings?t={e(t)}'><input type=hidden name=group value={grp}>{rows}"
                   "<button class='pri s'>Save</button></form></div>")
    if confirm:
        hid = "".join(f"<input type=hidden name='{e(k)}' value='{e(v)}'>" for k, v in confirm.items())
        out.insert(0, f"<div class=banner><b>Are you sure?</b> {e(confirm.pop('_why', ''))}"
                      f"<form method=post action='home/settings?t={e(t)}'>{hid}<input type=hidden name=confirm value=1>"
                      "<button class='pri s'>Yes, save it</button> <a href='home?t=" + e(t) + "&tab=settings'>Cancel</a></form></div>")
    runs = "".join(f"<form method=post action='home/run?t={e(t)}' style='display:inline'><input type=hidden name=what value={k}>"
                   f"<button class='sec s' style='margin:3px 0'>{e(lab)}</button></form><br>" for k, lab in SS.RUNNABLE.items())
    out.append(f"<h3>Run now</h3><div class=card>{runs}</div>")
    last = SS.last_undoable()
    undo = (f"<form method=post action='home/undo?t={e(t)}'><button class='sec s'>Undo last change</button> "
            f"<span class=sub>{e(', '.join(c['label'] + ': ' + str(c['old']) + ' → ' + str(c['new']) for c in last['changes']))}</span></form>"
            if last else "<span class=sub>No change to undo.</span>")
    rows = "".join(f"<tr><td>{when(r.get('at'))}<br><span class=sub>{e(r.get('via'))}</span></td><td>"
                   + (f"ran {e(r['run'])}" if r.get("run") else "<br>".join(f"{e(c['label'])}: {e(c['old'])} → {e(c['new'])}"
                                                                         for c in r.get("changes") or []))
                   + "</td></tr>" for r in SS.changes(15))
    out.append(f"<h3>Changes</h3><div class=card>{undo}<table class=t style='margin-top:8px'>{rows}</table></div>")
    out.append(f"<h3>This page's link</h3><div class=card><span class=sub>Bookmark this page. A new link stops the old one at once "
               f"(use it if the link was shared by mistake).</span><form method=post action='home/reset?t={e(t)}'>"
               "<button class='sec s' onclick=\"return confirm('Make a new link? The old one stops working.')\">Make a new link</button></form></div>")
    return "".join(out)


def needs_confirm(new, cur):
    why = []
    if cur.get("intake_on") and new.get("intake_on") is False:
        why.append("Intake stops changing the vault (dry runs only).")
    if cur.get("email_on") and new.get("email_on") is False:
        why.append("No more morning emails; the questions are only on this page.")
    if new.get("pause_until") and new.get("pause_until") != cur.get("pause_until"):
        why.append(f"Intake, the queue, analyses, books and YouTube Remember stop until {new['pause_until']} (inclusive).")
    return " ".join(why)


# ---------------------------------------------------------------- routes
def register(server):
    global S
    S = server
    from flask import abort, redirect, request
    CFG, route = S.CFG, S.route

    def reload_cfg():   # the server keeps one CFG dict: refresh it in place so the page shows the new values
        fresh = C.load_config()
        CFG.clear()
        CFG.update(fresh)

    def tok():
        t = request.values.get("t", "")
        if not H.token_ok(CFG, t):
            abort(404)
        return t

    def back(t, tab, msg=None, err=None):
        q = f"../home?t={quote(t)}&tab={tab}"
        if msg:
            q += "&msg=" + quote(msg[:400])
        if err:
            q += "&err=" + quote(err[:600])
        return redirect(q, code=303)

    @route("/home")
    def home_page():
        t = tok()
        tab = request.args.get("tab", "today")
        body = {"status": status, "settings": settings}.get(tab, today)(CFG, t)
        return page("Vault home", tab if tab in ("status", "settings") else "today", t, body,
                    request.args.get("msg"), request.args.get("err"))

    @route("/home/settings", methods=["POST"])
    def home_settings():
        t = tok()
        grp = request.form.get("group")
        cur = SS.current()
        new, bad = {}, []
        for sid, s in SS.SPEC.items():
            if s[0] != grp or sid not in request.form:
                continue
            vals = request.form.getlist(sid)
            try:
                new[sid] = SS.parse(sid, vals[-1] if vals else "")
            except ValueError as ex:
                bad.append(str(ex))
        if bad:
            return back(t, "settings", err=" ".join(bad))
        why = needs_confirm(new, cur)
        if why and not request.form.get("confirm"):
            pend = {k: ("1" if v is True else "0" if v is False else v) for k, v in new.items()}
            pend.update(group=grp, _why=why)
            return page("Vault home", "settings", t, settings(CFG, t, confirm=pend), sub=True)
        ok, msgs = SS.change(CFG, new, "home page")
        if ok:
            reload_cfg()
        S.LOG(f"home settings ({grp}): {'ok' if ok else 'refused'}: {'; '.join(msgs)[:300]}")
        return back(t, "settings", msg=" ".join(msgs) if ok else None, err=None if ok else " ".join(msgs))

    @route("/home/undo", methods=["POST"])
    def home_undo():
        t = tok()
        ok, msgs = SS.undo(CFG, "home page")
        if ok:
            reload_cfg()
        S.LOG(f"home undo: {'; '.join(msgs)[:300]}")
        return back(t, "settings", msg=" ".join(msgs) if ok else None, err=None if ok else " ".join(msgs))

    @route("/home/run", methods=["POST"])
    def home_run():
        t = tok()
        with S._lock:
            running = [d for d, st in S.APPLY.items() if (st or {}).get("state") == "running"]
        if request.form.get("what") == "Organize" and running:
            return back(t, "today", err="Your answers are still being applied; ask again once that's done.")
        ok, msg = SS.run_now(CFG, request.form.get("what", ""), "home page")
        S.LOG(f"home run {request.form.get('what')}: {msg}")
        tab = "today" if request.form.get("what") == "Organize" else "settings"
        return back(t, tab, msg=msg if ok else None, err=None if ok else msg)

    @route("/home/reset", methods=["POST"])
    def home_reset():
        tok()
        t = H.home_token(CFG, reset=True)
        S.LOG("home link reset")
        return back(t, "settings", msg="New link made: bookmark this page again. The old link no longer works.")
