"""Outlook-safe HTML for the Vault Librarian emails (Aptos 12pt, tables + inline styles only).

  build_daily(cfg, day, prop, held, nightly)  -> (subject, html, n_decisions)   the 06:00 "Obsidian Vault Update"
  send_daily(cfg, log, day, prop, dry)         builds + sends it when anything needs the owner (else no email)
  simple(heading, kicker, body_html, status)   one-card email for results/errors (done, couldn't read reply, failures)
  load_nightly(day)                            reports\\nightly-<day>.json written by nightly.py

The reply-subject tag is "[Vault update <day>]"; organize.check_replies also still accepts the old
"[Vault organize <day>]". The web page is WEB_BASE/organize/<day>?t=<prop web_token> on vault_server.py.
"""
import html
import json
import re
from pathlib import Path
from urllib.parse import quote, urlparse

import common as C

TAG = "Vault update"      # answerable: replies to it are parsed as answers
NOTICE = "Vault notice"   # results / errors: replies are ignored
FONT = "Aptos,'Aptos Display',Calibri,'Segoe UI',Arial,sans-serif"
ACCENT = "#483d8b"
GREEN = "#2e7d32"
AMBER = ("#fff4ce", "#7a5a00", "#c19c00")   # bg, text, border
SITES = {"washingtonpost.com": "Washington Post", "nytimes.com": "New York Times", "wsj.com": "Wall Street Journal",
         "youtube.com": "YouTube", "youtu.be": "YouTube", "reuters.com": "Reuters", "apnews.com": "AP News",
         "bloomberg.com": "Bloomberg", "ft.com": "Financial Times", "tiktok.com": "TikTok", "wikipedia.org": "Wikipedia"}


def e(s):
    return html.escape(str(s if s is not None else ""))


def web_base(cfg):
    return (cfg.get("organize", {}).get("web_base") or "http://127.0.0.1:8766/vault").rstrip("/")


def home_url(cfg):
    """The bookmarkable home page (plan: 2026-11-12 Home page); falls back to the status page."""
    try:
        import home_data
        return f"{web_base(cfg)}/home?t={home_data.home_token(cfg)}"
    except Exception:  # noqa: BLE001
        return f"{web_base(cfg)}/status"


# ------------------------------------------------------------------ building blocks
def chip(text, bg="#edebe9", fg="#424242"):
    return (f"<span style=\"font-size:9pt;background:{bg};color:{fg};border-radius:3px;padding:1px 6px;"
            f"font-family:{FONT};\">{e(text)}</span>")


def suggested(text="Suggested"):
    return chip(text, GREEN, "#ffffff")


def button(href, label, primary=True):
    if primary:
        return (f"<td style=\"background:{ACCENT};border-radius:6px;\"><a href=\"{e(href)}\" style=\"display:inline-block;"
                f"padding:10px 18px;color:#ffffff;text-decoration:none;font-weight:bold;font-family:{FONT};font-size:12pt;\">"
                f"{e(label)}</a></td>")
    return (f"<td style=\"border:1px solid {ACCENT};border-radius:6px;\"><a href=\"{e(href)}\" style=\"display:inline-block;"
            f"padding:9px 16px;color:{ACCENT};text-decoration:none;font-weight:bold;font-family:{FONT};font-size:12pt;\">"
            f"{e(label)}</a></td>")


def buttons(*cells):
    inner = "<td style=\"width:10px;\"></td>".join(c for c in cells if c)
    return f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\"><tr>{inner}</tr></table>"


def section(title):
    return (f"<tr><td style=\"padding:6px 24px 4px 24px;border-top:1px solid #edebe9;\"><div style=\"font-size:13pt;"
            f"font-weight:bold;color:{ACCENT};padding-top:12px;font-family:{FONT};\">{e(title)}</div></td></tr>")


def card(inner, accent=ACCENT, pad_bottom=8):
    return (f"<tr><td style=\"padding:8px 24px {pad_bottom}px 24px;\"><table role=\"presentation\" width=\"100%\" "
            f"cellpadding=\"0\" cellspacing=\"0\" style=\"border:1px solid #e1dfdd;border-left:4px solid {accent};"
            f"border-radius:4px;\"><tr><td style=\"padding:12px 14px;font-family:{FONT};font-size:12pt;"
            f"line-height:1.45;color:#242424;\">{inner}</td></tr></table></td></tr>")


def shell(kicker, heading, rows, footer=""):
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,"
            f"initial-scale=1\"></head><body style=\"margin:0;padding:0;background:#f3f2f1;\">"
            f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#f3f2f1;\">"
            f"<tr><td align=\"center\" style=\"padding:16px 8px;\">"
            f"<table role=\"presentation\" width=\"640\" cellpadding=\"0\" cellspacing=\"0\" style=\"width:100%;max-width:640px;"
            f"background:#ffffff;border:1px solid #e1dfdd;font-family:{FONT};font-size:12pt;line-height:1.45;color:#242424;\">"
            f"<tr><td style=\"background:{ACCENT};padding:18px 24px;color:#ffffff;font-family:{FONT};\">"
            f"<div style=\"font-size:9pt;letter-spacing:1px;text-transform:uppercase;\">{e(kicker)}</div>"
            f"<div style=\"font-size:16pt;font-weight:bold;padding-top:2px;\">{e(heading)}</div></td></tr>"
            + "".join(rows) +
            (f"<tr><td style=\"padding:12px 24px 18px 24px;font-size:9.5pt;color:#8a8886;font-family:{FONT};\">{footer}</td></tr>"
             if footer else "") +
            "</table></td></tr></table></body></html>")


def para(inner, pad="18px 24px 6px 24px"):
    return f"<tr><td style=\"padding:{pad};font-family:{FONT};font-size:12pt;\">{inner}</td></tr>"


def status_pill(text, kind="warn"):
    bg, fg, bd = AMBER if kind == "warn" else (("#e8f5e9", GREEN, "#a5d6a7") if kind == "ok" else ("#fde7e9", "#a4262c", "#f1a7ad"))
    return (f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\"><tr><td style=\"background:{bg};"
            f"border:1px solid {bd};border-radius:12px;padding:3px 12px;font-size:10.5pt;font-weight:bold;color:{fg};"
            f"font-family:{FONT};\">{e(text)}</td></tr></table>")


def simple(heading, kicker, body_html, status=None, kind="ok"):
    rows = [para((status_pill(status, kind) if status else "") +
                 f"<div style=\"padding-top:{10 if status else 0}px;\">{body_html}</div>", "18px 24px 18px 24px")]
    return shell(kicker, heading, rows)


# ------------------------------------------------------------------ content helpers
def site_of(url):
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    for dom, name in SITES.items():
        if host == dom or host.endswith("." + dom):
            return name
    return host


def title_from_url(url):
    """Readable title from a URL slug when the page itself couldn't be read (e.g. paywalled sites)."""
    p = urlparse(url)
    if "youtube" in (p.hostname or "") or "youtu.be" in (p.hostname or ""):
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})", url)
        return f"YouTube video ({m.group(1)})" if m else "YouTube video"
    segs = [s for s in p.path.split("/") if s and not re.fullmatch(r"\d+|index\.html?|amp", s)]
    slug = max(segs, key=len) if segs else ""
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug)
    words = re.split(r"[-_]+", slug)
    if len(words) < 3:
        return site_of(url) or url
    t = " ".join(words)
    return t[:1].upper() + t[1:]


def fmt_day(day):
    import datetime as dt
    d = dt.date.fromisoformat(day)
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def load_nightly(day):
    p = C.APP / "reports" / f"nightly-{day}.json"
    try:
        n = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    line = pacing_line(day)
    if line and isinstance(n.get("health"), list):
        n["health"].append(["Usage", line])
    return n


def pacing_line(day):
    """One line from reports\\pacing-<day>.json (queue_run.py's night run, added 2026-09-29), or None."""
    try:
        sm = json.loads((C.APP / "reports" / f"pacing-{day}.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        import vqueue as Q
        waiting = sum(1 for i in Q.Store(C.load_config()).all()
                      if Q.route_of(i) in Q.COSTLY and i.get("status") in ("ready", "working", "failed"))
    except Exception:
        waiting = None
    n = len(sm.get("done") or [])
    tail = f"{n} book(s) done" + (f", {waiting} waiting" if waiting is not None else "")
    if not sm.get("ok"):
        return f"usage unknown, fixed limit used; {tail}"
    a, b = sm["start"], sm["end"]
    return (f"session {a['session_pct']:.0f}% \u2192 {b['session_pct']:.0f}%, week {a['week_pct']:.0f}% \u2192 "
            f"{b['week_pct']:.0f}%; {tail}" + (" (last night before the weekly reset)" if sm.get("nights_left") == 1 else ""))


def load_remember(day):
    """reports\\remember-<day>.json from the 02:05 YouTube Remember run (live runs only), or None."""
    p = C.APP / "reports" / f"remember-{day}.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return None if d.get("dry_run") else d
    except (OSError, ValueError):
        return None


def remember_needs(rem):
    """Videos that stayed on Remember for a reason the owner should see (not simply rolled to the next night)."""
    out = list((rem or {}).get("failures") or [])
    for v in (rem or {}).get("videos") or []:
        if v.get("flag"):
            out.append(v["flag"])
        st = v.get("status", "")
        if "left on Remember" in st and not st.startswith("failed") and "notes not written" not in st:
            out.append(f"{v.get('title', v.get('video_id'))}: {st}")
    return out


def remember_card(rem):
    vids = rem.get("videos") or []
    filed = [v for v in vids if v.get("filed")]
    rolled = [v for v in vids if str(v.get("status", "")).startswith("rolled")]
    bits = []
    if filed:
        bits.append(f"<b>{len(filed)} video{'s' if len(filed) != 1 else ''} filed</b> in the Inbox as a detailed "
                    "Video Summary plus a linked Transcript note, and removed from Remember:<ul style=\"margin:6px 0 0 18px;padding:0;\">"
                    + "".join(f"<li>{e(v.get('title', ''))}"
                              + (" <span style=\"color:#616161;\">(replaced your Save to Vault note)</span>" if v.get("replaced") else "")
                              + "</li>" for v in filed) + "</ul>")
    if rolled:
        bits.append(f"<div style=\"padding-top:8px;\"><b>{len(rolled)} rolled to tonight</b> "
                    f"({e(rem.get('stop') or 'limit reached')}); they keep their place on Remember.</div>")
    needs = remember_needs(rem)
    if needs:
        bits.append("<div style=\"padding-top:8px;\"><b style=\"color:#a4262c;\">Needs a look:</b><ul style=\"margin:6px 0 0 18px;padding:0;\">"
                    + "".join(f"<li>{e(x)}</li>" for x in needs) + "</ul></div>")
    if not bits:
        bits.append("Nothing on Remember to process.")
    return card("".join(bits), AMBER[2] if needs else GREEN)


def load_held():
    hq = C.APP / "queue" / "held.json"
    try:
        held = json.loads(hq.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [h for h in held.values() if h.get("status") == "held"]


NEED_HELP = [  # (pattern in the nightly "needs" line, what it means, what to say)
    (r"possible duplicate", "Two notes look alike but not closely enough to archive one automatically.",
     "In a Claude session say <b>&ldquo;organize my vault&rdquo;</b>; the pairs are listed for you to decide."),
    (r"archived duplicate", "Copies archived as duplicates are waiting for you to restore, merge or delete them.",
     "Say <b>&ldquo;review archived duplicates&rdquo;</b> in a Claude session."),
    (r"OKF error", "Some notes are missing required header fields, so they aren't OKF-compliant.",
     "Say <b>&ldquo;check my vault&rdquo;</b> in a Claude session to see and fix them."),
]


# ------------------------------------------------------------------ the daily email
def option_rows(opts, pick="a"):
    rows = []
    for k, o in sorted(opts.items()):
        label = o.get("label") or ""
        m = re.match(r"^(.*?)\s*(\(.*\))\s*$", label)
        main, note = (m.group(1), m.group(2)) if m else (label, "")
        main = main.replace("/", " / ")
        hi = k == pick
        rows.append(f"<tr><td style=\"padding:5px 8px;{'background:#e8f5e9;border-radius:3px;' if hi else ''}"
                    f"font-family:{FONT};font-size:11pt;\"><b>{e(k)})</b> {e(main)} <span style=\"color:#616161;\">{e(note)}</span>"
                    f"{' &nbsp;' + suggested() if hi else ''}</td></tr>")
    return (f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin-top:6px;\">"
            + "".join(rows) + "</table>")


def note_type(cfg, path):
    """The note's own `type:` from its frontmatter (proposals only carry a type when they change it)."""
    try:
        head = (Path(cfg["vault_path"]) / path).read_text(encoding="utf-8", errors="replace")[:3000]
        m = re.search(r"(?m)^type:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", head.split("\n---", 1)[0])
        return m.group(1).strip() if m else None
    except OSError:
        return None


def note_card(q, items, cfg=None):
    paths = q["paths"]
    names = []
    for p in paths:
        it = items.get(p, {})
        old = p.rsplit("/", 1)[-1][:-3]
        new = it.get("rename_to")
        names.append((new or old, old if new else None, (it.get("set") or {}).get("type") or (note_type(cfg, p) if cfg else None)))
    if len(paths) == 1:
        title, old, typ = names[0]
        head = (chip(typ) if typ else "") + f"<div style=\"font-weight:bold;padding-top:6px;\">{q['id']}. {e(title)}</div>"
        sub = e(q.get("text", "")).replace(" - where should it go?", "")
        if old:
            sub += f"<br>New name; it was &ldquo;{e(old)}&rdquo; (the old name is kept as an alias)."
    else:
        head = chip(f"{len(paths)} notes") + f"<div style=\"font-weight:bold;padding-top:6px;\">{q['id']}. {e(q.get('text', ''))}</div>"
        sub = "<br>".join(f"&bull; {e(t)}" + (f" <span style=\"color:#8a8886;\">(was &ldquo;{e(o)}&rdquo;)</span>" if o else "")
                          for t, o, _ in names)
    return card(head + f"<div style=\"font-size:10.5pt;color:#616161;\">{sub}</div>" + option_rows(q["options"])
                + "<div style=\"font-size:9.5pt;color:#8a8886;padding-top:4px;\">Another folder, or a new subject or "
                  "subfolder: use <b>Review &amp; answer on the web</b>, then <b>Choose a different folder</b>.</div>")


def tag_card(nt):
    n = len(nt.get("paths", []))
    if nt.get("folder"):   # a suggested new Subjects/Manuals folder (2026-10-07)
        ex = nt.get("exists")
        body = (chip("Waiting for a folder" if ex else "New folder", "#e6f2ea", "#1e6b38") +
                f"<div style=\"font-weight:bold;padding-top:6px;\">{nt['id']}. {'File these in' if ex else 'Create the folder'} <span style=\"font-family:Consolas,"
                f"monospace;background:#f3f2f1;padding:0 4px;\">{e(nt['folder'])}</span>?</div>"
                f"<div style=\"font-size:10.5pt;color:#616161;\">For {e(nt.get('covers', ''))}. {n} note{'s' if n != 1 else ''} "
                f"would be filed there and tagged with the folder's name. To choose note by note (that folder, another one, "
                f"or decide later), or for a different name or place, use the web page. Replying a) files them all there.</div>"
                f"<div style=\"font-size:11pt;padding-top:6px;\"><b>a)</b> Yes &nbsp;&nbsp; <span style=\"background:#e8f5e9;"
                f"padding:2px 8px;border-radius:3px;\"><b>b)</b> No &nbsp;{suggested()}</span></div>")
        return card(body, "#2e7d32")
    body = (chip("New tag", "#f3eefa", "#5c2e91") +
            f"<div style=\"font-weight:bold;padding-top:6px;\">{nt['id']}. Add the tag <span style=\"font-family:Consolas,"
            f"monospace;background:#f3f2f1;padding:0 4px;\">{e(nt['tag'])}</span>?</div>"
            f"<div style=\"font-size:10.5pt;color:#616161;\">{e(nt.get('covers', ''))}. It would be added to your approved "
            f"tags and applied to {n} note{'s' if n != 1 else ''}.</div>"
            f"<div style=\"font-size:11pt;padding-top:6px;\"><b>a)</b> Yes &nbsp;&nbsp; <span style=\"background:#e8f5e9;"
            f"padding:2px 8px;border-radius:3px;\"><b>b)</b> No &nbsp;{suggested()}</span></div>")
    return card(body, "#8764b8")


def view_links(links):
    """'Compare side by side · Archived copy · Kept note' (note_view.links_for), opened on the web page (Tailscale)."""
    if not links:
        return ""
    return ("<div style=\"font-size:10pt;padding-top:4px;\">" + " &nbsp;&middot;&nbsp; ".join(
        f"<a href=\"{e(u)}\" style=\"color:{ACCENT};\">{e(x)}</a>" for x, u in links) + "</div>")


def dup_card(q, links=None):
    """A duplicate question (organize.dup_questions): archived copy or a weak pair, lettered options, a = suggested."""
    if q["kind"] == "archived":
        name = q["path"].rsplit("/", 1)[-1][:-3]
        head = chip("Archived duplicate", "#fde7e9", "#a4262c") + f"<div style=\"font-weight:bold;padding-top:6px;\">{q['id']}. {e(name)}</div>"
    else:
        names = [p.rsplit("/", 1)[-1][:-3] for p in q["paths"]]
        head = (chip("Possible duplicates", "#fde7e9", "#a4262c") +
                f"<div style=\"font-weight:bold;padding-top:6px;\">{q['id']}. {e(names[0])}<br>&nbsp;&nbsp;&nbsp;&nbsp;and {e(names[1])}</div>")
    sub = e(q.get("text", ""))
    return card(head + f"<div style=\"font-size:10.5pt;color:#616161;\">{sub}</div>" + view_links(links)
                + option_rows(q["options"]), "#c4314b")


def queue_card(q, links=None):
    """An intake-queue question (organize.queue_questions): lettered options, a = suggested."""
    chipl = {"title": ("Name check", "#e8f0fe", "#1a4b8c"), "duplicate": ("Already in the vault?", "#fde7e9", "#a4262c"), "costly": ("Large item: OK?", "#fff4ce", "#7a5a00"),
             "confidential": ("Firm or client material?", "#fde7e9", "#a4262c"),
             "concept": ("New concept?", "#e8f0fe", "#1a4b8c"),
             "concept_dup": ("Same concept?", "#e8f0fe", "#1a4b8c"),
             "concept_review": ("Keep this concept?", "#e8f0fe", "#1a4b8c")}.get(q["kind"], ("Intake queue", "#edebe9", "#424242"))
    sub = e(q["text"]) + (f"<br>{e(q['size'])}" if q.get("size") else "") + f"<br><span style=\"color:#8a8886;\">{e(q['where'])}</span>"
    return card(chip(*chipl) + f"<div style=\"font-weight:bold;padding-top:6px;\">{q['id']}. {e(q['title'])}</div>"
                f"<div style=\"font-size:10.5pt;color:#616161;\">{sub}</div>" + view_links(links) + option_rows(q["options"]), "#c4314b")


def change_lines(a):
    """What a redone analysis changed (phase D, 2026-10-06)."""
    c = a.get("changes")
    if not a.get("redo"):
        return ""
    if not c:
        return "<br><span style=\"color:#616161;font-size:10pt;\">Redone: no change in concepts, related links or thesis.</span>"
    bits = []
    for k, label in (("concepts_added", "concepts added"), ("concepts_dropped", "concepts dropped"),
                     ("related_added", "related added"), ("related_dropped", "related dropped")):
        if c.get(k):
            bits.append(f"{label}: {e(', '.join(c[k][:6]))}" + (" …" if len(c[k]) > 6 else ""))
    if c.get("thesis"):
        bits.append(f"thesis changed: now &ldquo;{e(c['thesis']['now'][:200])}&rdquo;")
    return ("<br><span style=\"color:#616161;font-size:10pt;\"><b>Redone, what changed:</b> " + "; ".join(bits) + "</span>")


def analyses_card(items):
    """Analyses written since yesterday: concepts, new concepts asked about above, related links (by kind)."""
    rows = []
    for a in items[:20]:
        kinds = {}
        for kind, _ in a["related"]:
            kinds[kind] = kinds.get(kind, 0) + 1
        rel = ", ".join(f"{v} {k}" for k, v in kinds.items()) or "none"
        rows.append(f"<tr><td style=\"padding:4px 0;font-family:{FONT};font-size:11pt;\"><b>{e(a['title'])}</b><br>"
                    f"<span style=\"color:#616161;font-size:10pt;\">{a['concepts']} concept(s) linked"
                    + (f", {a['pending']} new one(s) asked about above" if a['pending'] else "")
                    + f"; related notes: {e(rel)}</span>" + change_lines(a) + "</td></tr>")
    more = f"<p style=\"font-size:10pt;color:#616161;\">and {len(items) - 20} more</p>" if len(items) > 20 else ""
    return card("<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\">" + "".join(rows)
                + "</table>" + more + "<div style=\"font-size:9.5pt;color:#8a8886;padding-top:6px;\">Each is a draft "
                "\"&lt;work&gt; (Analysis)\" beside its note. Delete any related link you disagree with in Obsidian; "
                "nothing needs an answer here.</div>")


def held_card(h):
    url = h.get("url") or ""
    site = site_of(url) if url else ""
    title = h.get("title") or (title_from_url(url) if url else h.get("subject") or "Email")
    shown = title if not site or title.startswith(site) else f"{site}: {title}"
    link = (f"<a href=\"{e(url)}\" style=\"color:#242424;\">{e(shown)}</a>" if url else e(shown))
    import reclip
    if url and reclip.blocked(C.load_config(), url):   # a site Claude's browsers can't open (2026-10-06)
        badge = "Re-clip it yourself"
        why = f"{e(site)} can't be opened by {C.pc_name()} or by Claude's browser on the laptop, even signed in."
        todo = ("Open the link above in your own browser, sign in if asked, and save it with the <b>Obsidian Web Clipper</b>. "
                f"{C.pc_name()} finds your clip on its next run and clears this item (and the Remember email).")
        idle = "It stays here each day until your clip is in the vault."
    elif h.get("needs") == "browser":
        badge = "Needs your signed-in browser"
        why = f"{e(site or 'The site')} blocks automated downloads, and {C.pc_name()}'s overnight job has no signed-in browser."
        if h.get("headless_why"):
            why += f" {C.pc_name()}'s headless browser couldn't read it either ({e(h['headless_why'])})."
        todo = ("On the laptop, open the link above in your own browser (you're signed in there) and save it with the "
                f"<b>Obsidian Web Clipper</b>; {C.pc_name()} finds your clip by its address within 30 minutes and clears this item. "
                "Or, in a Claude session on your laptop, say <b>&ldquo;process the vault queue.&rdquo;</b>")
        idle = "It stays queued and appears here each day until it's saved."
    elif re.search(r"video|audio|podcast", str(h.get("why")), re.I):
        badge = "Video or audio: your call"
        why = "Videos and podcasts can't be turned into text notes automatically, so the email was left in your Remember folder."
        todo = "Watch or listen, then delete the email from Remember. Or, in a Claude session, ask for a summary note of it."
        idle = "The email stays in Remember and keeps appearing here."
    else:
        badge = "Left in Remember for you"
        why = e(re.sub(r"\s*-\s*needs .*$", "", str(h.get("why") or "It couldn't be saved automatically.")))
        todo = "Open it and decide; delete it from Remember when you're done."
        idle = "It stays in Remember and keeps appearing here."
    note = f"<br><span style=\"color:#616161;\">Your note: {e(h['doug_note'])}</span>" if h.get("doug_note") else ""
    body = (chip(badge, AMBER[0], AMBER[1]) +
            f"<div style=\"font-weight:bold;padding-top:6px;\">{link}</div>"
            f"<div style=\"font-size:11pt;padding-top:4px;\"><b>Why:</b> {why}<br><b>What to do:</b> {todo}<br>"
            f"<span style=\"color:#616161;\"><b>If you do nothing:</b> {idle}</span>{note}</div>")
    return card(body, AMBER[2])


HELD_CHIP = {"notext": "No readable text", "junk": "Not a document", "dead": "Dead link", "browser": "Needs your signed-in browser", "other": "Couldn't be saved",
             "kept_analysis": "Analysis kept"}


def held_q_card(q, links=None):
    """An item left in Remember with lettered choices (organize.held_questions, 2026-10-01)."""
    sub = e(q.get("text") or q.get("why") or "") + (f"<br><span style=\"color:#8a8886;\">{e(q['where'])}</span>" if q.get("where") else "")
    if q.get("note"):
        sub += f"<br>Your note: {e(q['note'])}"
    return card(chip(HELD_CHIP.get(q.get("sub"), "Couldn't be saved"), AMBER[0], AMBER[1])
                + f"<div style=\"font-weight:bold;padding-top:6px;\">{q['id']}. {e(q['title'])}</div>"
                f"<div style=\"font-size:10.5pt;color:#616161;\">{sub}</div>" + view_links(links) + option_rows(q["options"])
                + "<div style=\"font-size:9.5pt;color:#8a8886;padding-top:4px;\">You can add your note (my_note) to it on the web page.</div>",
                AMBER[2])


def other_need_card(line):
    for pat, what, todo in NEED_HELP:
        if re.search(pat, line, re.I):
            return card(chip("From overnight maintenance", AMBER[0], AMBER[1]) +
                        f"<div style=\"font-weight:bold;padding-top:6px;\">{e(line[:1].upper() + line[1:])}</div>"
                        f"<div style=\"font-size:11pt;padding-top:4px;\"><b>What it means:</b> {what}<br>"
                        f"<b>What to do:</b> {todo}</div>", AMBER[2])
    return card(chip("From overnight maintenance", AMBER[0], AMBER[1]) +
                f"<div style=\"font-weight:bold;padding-top:6px;\">{e(line)}</div>"
                "<div style=\"font-size:11pt;padding-top:4px;\">See the full report in Obsidian for details.</div>", AMBER[2])


def friendly(label, val):
    """Plain-English version of a nightly health value."""
    v = val
    if label == "Backup snapshot":
        m = re.search(r"\((\d+) files, ([\d.]+) MB\)", v)
        return f"Backup snapshot ({m.group(1)} files, {float(m.group(2)):.0f} MB)" if m else "Backup snapshot taken"
    if label == "Duplicates":
        m = re.match(r"(\d+) STRONG.*?(\d+) weak", v)
        if m:
            a, w = int(m.group(1)), int(m.group(2))
            return ("No new duplicates" if not a and not w else
                    f"{a} duplicate(s) archived" + (f", {w} possible for you to decide" if w else ""))
    if label == "Citation clean-up":
        m = re.match(r"(\d+)", v)
        return f"Citations added to {m.group(1)} clipping(s)" if m and m.group(1) != "0" else "No clippings needed citations"
    if label == "Indexes and hubs":
        return "Indexes and hubs rebuilt" if v.startswith("rebuilt") else f"Indexes and hubs {v}"
    if label == "Compliance":
        m = re.search(r"(\d+) errors?, (\d+) warn", v)
        return f"Compliance: {m.group(1)} errors ({m.group(2)} minor warnings)" if m else f"Compliance: {v}"
    if label == "Cowork sync":
        return "Cowork sync: laptop only" if "laptop" in v or "not run" in v else f"Cowork sync: {v}"
    return f"{label}: {v}"


def health_row(nightly):
    if not nightly:
        return (f"<tr><td style=\"padding:12px 24px;border-top:1px solid #edebe9;background:#faf9f8;font-size:10.5pt;"
                f"color:#8a8886;font-family:{FONT};\">No overnight maintenance report was found for today.</td></tr>")
    st = nightly.get("status", "")
    if nightly.get("failures"):
        head = f"<b style=\"color:#a4262c;\">Overnight maintenance: something failed</b> (a separate email has the details)"
    elif "skipped" in st:
        head = "<b style=\"color:#7a5a00;\">Overnight maintenance was skipped</b> because another job was using the vault"
    else:
        head = f"<b style=\"color:{GREEN};\">Overnight maintenance: all clear</b>"
    bits = []
    for label, val in nightly.get("health", []):
        mark = "&ndash;" if val.startswith(("skipped", "not run")) else "&#10003;"
        bits.append(f"{mark} {e(friendly(label, val))}")
    return (f"<tr><td style=\"padding:12px 24px;border-top:1px solid #edebe9;background:#faf9f8;font-size:10.5pt;"
            f"color:#424242;font-family:{FONT};\">{head}<br>" + " &nbsp;&middot;&nbsp; ".join(bits) + "</td></tr>")


def build_daily(cfg, day, prop, held, nightly, remember=None, backlog_counts=None, usage=None):
    qs = (prop or {}).get("questions", [])
    tags = (prop or {}).get("new_tags", []) + (prop or {}).get("new_folders", [])   # new folders asked like tags
    dups = (prop or {}).get("dups", [])
    qq = (prop or {}).get("queue", [])
    hall = (prop or {}).get("held", [])
    hqa = [q for q in hall if q.get("options")]     # items left in Remember with choices (2026-10-01)
    asked = {q["item"] for q in qq} | {q["item"] for q in hqa if q.get("item")}
    held = [h for h in held if h.get("queue_id") not in asked]   # asked as a question below instead
    items = {it["path"]: it for it in (prop or {}).get("items", [])}

    def lk(q):   # read-only views of the items behind a duplicate / intake question (note_view.py, 2026-10-01)
        try:
            import note_view
            return note_view.links_for(q, f"{web_base(cfg)}/organize/{day}/", (prop or {}).get("web_token", ""))
        except Exception:  # noqa: BLE001  the links are a convenience: never let them stop the email
            return []
    others = [n for n in (nightly or {}).get("needs", []) if "held Remember" not in n
              and not (dups and re.search(r"duplicate", n, re.I))]   # duplicates are answered in this email now
    n_notes = len(items)
    rem_needs = remember_needs(remember)
    n = len(qs) + len(tags) + len(dups) + len(qq) + len(hqa) + len(held) + len(others) + len(rem_needs)
    parts = []
    if n_notes:
        parts.append(f"{n_notes} new note{'s are' if n_notes != 1 else ' is'} ready to file")
    n_tags = len((prop or {}).get("new_tags", []))           # 2026-10-19: folders were counted as tags
    n_folders = len(tags) - n_tags
    if n_tags:
        parts.append(f"{n_tags} new tag{'s need' if n_tags != 1 else ' needs'} approval")
    if n_folders:
        parts.append(f"{n_folders} new folder{'s need' if n_folders != 1 else ' needs'} approval")
    if dups:
        parts.append(f"{len(dups)} duplicate question{'s need' if len(dups) != 1 else ' needs'} an answer")
    if qq:
        parts.append(f"{len(qq)} intake-queue question{'s need' if len(qq) != 1 else ' needs'} an answer")
    if hqa:
        parts.append(f"{len(hqa)} item{'s' if len(hqa) != 1 else ''} left in your Remember dropbox need{'' if len(hqa) != 1 else 's'} a decision")
    if held:
        parts.append(f"{len(held)} item{'s' if len(held) != 1 else ''} from your Remember folder couldn't be saved on "
                     f"{'their' if len(held) != 1 else 'its'} own")
    if others:
        parts.append(f"{len(others)} maintenance item{'s need' if len(others) != 1 else ' needs'} a look")
    if rem_needs:
        parts.append(f"{len(rem_needs)} YouTube Remember video{'s need' if len(rem_needs) != 1 else ' needs'} a look")
    summary = (", ".join(parts[:-1]) + (", and " if len(parts) > 2 else " and ") + parts[-1] if len(parts) > 1
               else (parts[0] if parts else "Nothing needs you today")) + "."
    ok_night = nightly and not nightly.get("failures") and "skipped" not in nightly.get("status", "")
    if ok_night:
        summary += " Everything else ran cleanly overnight."
    subj_tag = f"[{TAG} {day}]"
    n_ans, n_look = len(qs) + len(tags) + len(dups) + len(qq) + len(hqa), len(held) + len(others) + len(rem_needs)
    pill = " · ".join(x for x in (f"{n_ans} to answer" if n_ans else "", f"{n_look} to look at" if n_look else "") if x)
    rows = [para(status_pill(pill or "Nothing needs you") +
                 f"<p style=\"margin:12px 0 0 0;\">{summary}</p>" + backlog_card(backlog_counts)
                 + usage_card(cfg, day, prop, usage))]
    if qs or tags or dups or qq or hqa:
        page = f"{web_base(cfg)}/organize/{day}?t={prop.get('web_token', '')}"
        mailto = (f"mailto:{cfg['mailbox']}?subject={quote('RE: ' + subj_tag + ' approve all')}&body=ok")
        letters = (" ".join(f"{q['id']}a" for q in qs) + "".join(f" {t['id']}b" for t in tags)
                   + "".join(f" {q['id']}a" for q in dups) + "".join(f" {q['id']}a" for q in qq + hqa)).strip()
        rows.append(para(buttons(button(page, "Review & answer on the web"), button(mailto, "Approve all suggestions", False)) +
                         f"<div style=\"font-size:9.5pt;color:#616161;padding-top:8px;\">The web page opens when this device is "
                         f"on Tailscale. Otherwise reply to this email: <b>ok</b> approves every Suggested choice; "
                         f"<b>skip</b> leaves everything as it is; or answer each one, e.g. <b>{e(letters)}</b> "
                         f"(anything you leave out stays as it is and is asked again tomorrow).</div>", "14px 24px 18px 24px"))
    if qs:
        rows.append(section("New notes to file"))
        rows += [note_card(q, items, cfg) for q in qs]
    if tags:
        rows += [tag_card(t) for t in tags]
    if dups:
        rows.append(section("Duplicates to decide"))
        rows += [dup_card(q, lk(q)) for q in dups]
    if qq:
        rows.append(section("From your Remember folders and new analyses: to decide"))
        rows += [queue_card(q, lk(q)) for q in qq]
    if held or hqa:
        rows.append(section("From your Remember folder: couldn't be saved automatically"))
        rows += [held_q_card(q, lk(q)) for q in hqa]
        rows += [held_card(h) for h in held]
    if others:
        rows.append(section("Also from overnight maintenance"))
        rows += [other_need_card(x) for x in others]
    try:   # analyses written since yesterday (concept analysis plan, phase A): for a look, nothing to answer
        import analyze
        new_an = analyze.recent(cfg)
    except Exception:  # noqa: BLE001
        new_an = []
    if new_an:
        rows.append(section("New analyses (drafts in the vault)"))
        rows.append(analyses_card(new_an))
    if remember:
        rows.append(section("YouTube Remember"))
        rows.append(remember_card(remember))
    rows.append(health_row(nightly))
    vault_name = Path(cfg["vault_path"]).name
    note = cfg.get("report", {}).get("vault_note", "Claude/Other/Vault Maintenance Report.md")[:-3]
    footer = (f"<a href=\"obsidian://open?vault={quote(vault_name)}&amp;file={quote(note)}\" style=\"color:{ACCENT};\">"
              f"Full maintenance report in Obsidian</a> &nbsp;&middot;&nbsp; "
              f"<a href=\"{home_url(cfg)}\" style=\"color:{ACCENT};\">Vault home page</a> (Tailscale; bookmark it: it "
              "works even when this email doesn't arrive)<br>"
              "Unanswered questions leave their notes in the Inbox; they are asked again the next morning.")
    what = []
    if qs or tags or dups or qq or hqa:
        what.append(f"{len(qs) + len(tags) + len(dups) + len(qq) + len(hqa)} to answer")
    if held or others or rem_needs:
        what.append(f"{len(held) + len(others) + len(rem_needs)} to look at")
    subject = f"{subj_tag} Obsidian Vault Update: {', '.join(what) or 'all clear'}"
    return subject, shell("Obsidian Vault Update", fmt_day(day), rows, footer), n


def backlog(cfg, log=print):
    """What is still waiting in each Remember source (2026-10-01), shown at the top of the daily email:
    {"dropbox": [(folder, n)], "outlook": [(folder, n)], "youtube": n or None, "errors": [...]}.
    Dropbox: files present per subfolder (sidecar .txt notes and files you chose to keep are not counted).
    Outlook: items in Inbox\\Remember and each subfolder. YouTube: videos on the Remember playlist."""
    import sys
    import vqueue as Q
    out = {"dropbox": [], "outlook": [], "youtube": None, "errors": []}
    q = Q.qcfg(cfg)
    try:
        import collect_folder as CF
        root = Path((q.get("dropbox_path") or "").strip() or "\0")
        kept = {(it.get("source_ref") or {}).get("path") for it in Q.Store(cfg).all()
                if it.get("source") == "folder" and it.get("status") == "skipped"}
        counts = {}
        for p, rel in CF.files_in(root):
            if CF.is_sidecar(p) or rel in kept:
                continue
            top = rel.split("/")[0] if "/" in rel else "(top level)"
            counts[top] = counts.get(top, 0) + 1
        order = sorted(q["route_folders"], key=str.lower) + ["(top level)"]
        out["dropbox"] = [(k, counts.get(k, 0)) for k in order] + sorted((k, v) for k, v in counts.items() if k not in order)
    except Exception as ex:  # noqa: BLE001
        out["errors"].append(f"OneDrive dropbox: {type(ex).__name__}: {ex}")
    try:
        import collect_mail as M
        import intake as I
        if C.mail_on(cfg):   # mail off: no Outlook counts, and no error for it
            s, base = I.graph(cfg)
            out["outlook"] = [(name, int(total or 0)) for _, name, _, total in
                              M.mail_folders(s, base, (cfg.get("intake") or {}).get("remember_folder", "Remember"), q["route_folders"])]
    except Exception as ex:  # noqa: BLE001
        out["errors"].append(f"Outlook: {type(ex).__name__}: {ex}")
    try:
        if (cfg.get("remember") or {}).get("enabled") is False:   # YouTube Remember turned off: no count
            raise LookupError("off")
        sys.path.insert(0, str(C.LIB))
        from remember import youtube as Y
        yt = Y.service()
        pid = Y.find_playlist(yt, (cfg.get("remember") or {}).get("playlist", "Remember"))
        if pid:
            r = yt.playlists().list(part="contentDetails", id=pid).execute()
            out["youtube"] = int(r["items"][0]["contentDetails"]["itemCount"])
    except LookupError:
        pass
    except Exception as ex:  # noqa: BLE001
        out["errors"].append(f"YouTube: {type(ex).__name__}: {ex}")
    for x in out["errors"]:
        log(f"backlog count: {x}")
    return out


def backlog_card(b):
    """'Waiting to be processed' rows for the top of the email."""
    if not b:
        return ""

    def parts(rows):
        return " &nbsp;&middot;&nbsp; ".join(f"{e(k)} <b>{n}</b>" for k, n in rows)
    drop_total = sum(n for _, n in b.get("dropbox", []))
    out_total = sum(n for _, n in b.get("outlook", []))
    rows = []
    if b.get("dropbox"):
        rows.append(("OneDrive Remember", f"<b>{drop_total}</b> &nbsp;({parts(b['dropbox'])})"))
    if b.get("outlook"):
        sub = [(k.split("/", 1)[1] if "/" in k else "Remember itself", n) for k, n in b["outlook"]]
        rows.append(("Outlook Remember", f"<b>{out_total}</b> &nbsp;({parts(sub)})"))
    if b.get("youtube") is not None:
        rows.append(("YouTube Remember", f"<b>{b['youtube']}</b> video{'s' if b['youtube'] != 1 else ''}"))
    for x in b.get("errors", []):
        rows.append(("Couldn't count", e(x.split(':')[0]) + f" (see the {C.pc_name()} log)"))
    if not rows:
        return ""
    trs = "".join(f"<tr><td style=\"padding:2px 12px 2px 0;color:#616161;white-space:nowrap;vertical-align:top;\">{e(k)}</td>"
                  f"<td style=\"padding:2px 0;\">{v}</td></tr>" for k, v in rows)
    return (f"<div style=\"font-size:10pt;color:#424242;padding-top:10px;\"><div style=\"font-weight:bold;padding-bottom:2px;\">"
            f"Still waiting to be processed</div><table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
            f"style=\"font-size:10pt;font-family:{FONT};\">{trs}</table></div>")


def usage_card(cfg, day, prop, f):
    """'Claude usage' block for the top of the email (2026-10-06): where the week stands, what tonight
    would run and its estimated share, the week's projected finish, warnings, and the tonight buttons."""
    if not f:
        return ""
    goal = f.get("goal", 90)
    rows = []
    if f.get("ok"):
        reset = f.get("reset", "")[:16].replace("T", " ")
        rows.append(("This week", f"<b>{f['week_pct']:.0f}%</b> used &nbsp;&middot;&nbsp; session {f['session_pct']:.0f}% "
                                  f"&nbsp;&middot;&nbsp; resets {e(reset)} ({f['nights_left']} night{'s' if f['nights_left'] != 1 else ''} left)"))
        bk = f.get("tonight_books") or []
        plan = []
        if bk:
            plan.append(f"{len(bk)} book{'s' if len(bk) != 1 else ''} ("
                        + ", ".join(f"{e(t[:40])} ~{s:.1f}%" for t, s in bk[:4]) + (", …" if len(bk) > 4 else "") + ")")
        if f.get("tonight_cheap"):
            plan.append(f"{f['tonight_cheap']} analys{'es' if f['tonight_cheap'] != 1 else 'is'}")
        rows.append(("Tonight", (" + ".join(plan) or "nothing queued") +
                     f" &nbsp;&rarr; about <b>{f['tonight_share']:.1f}%</b> of the week (allowance {f['allowance']:.1f}%"
                     + (f", <b>{e(__import__('usage_plan').LABEL[f['mode']])}</b>" if f.get("mode") != "normal" else "") + ")"))
        rows.append(("Week's finish", f"about <b>{f['projected']:.0f}%</b> projected (goal {goal}%); queue holds ~{f['work_total']:.0f}%"
                                      f" of a week's work"))
    else:
        rows.append(("This week", f"usage couldn't be read ({e(f.get('why') or '?')}); tonight uses the fixed limits"))
    for w in f.get("windows") or []:
        rows.append(("Extra window", f"<b>{e(w['label'])}</b>" + (f" &mdash; {e(w['warning'])}" if w.get("warning") else "")))
    for w in f.get("warnings", []):
        rows.append(("Note", e(w)))
    trs = "".join(f"<tr><td style=\"padding:2px 12px 2px 0;color:#616161;white-space:nowrap;vertical-align:top;\">{e(k)}</td>"
                  f"<td style=\"padding:2px 0;\">{v}</td></tr>" for k, v in rows)
    tok = (prop or {}).get("web_token", "")
    btns = ""
    if tok:
        base = f"{web_base(cfg)}/organize/{day}/usage?t={tok}&mode="
        btns = buttons(button(base + "more", "Use more tonight", False), button(base + "light", "Go light tonight", False),
                       button(base + "normal", "Normal", False))
        wbase = f"{web_base(cfg)}/organize/{day}/usage?t={tok}&window="
        btns += ("<div style=\"padding-top:6px;font-size:10pt;\">Also use an extra 5-hour window: "
                 f"<a href=\"{e(wbase)}02:00&amp;hours=5&amp;pct=90\" style=\"color:{ACCENT};font-weight:bold;\">2&ndash;7 AM at 90%</a> &middot; "
                 f"<a href=\"{e(wbase)}02:00&amp;hours=5&amp;pct=80\" style=\"color:{ACCENT};font-weight:bold;\">2&ndash;7 AM at 80%</a> &middot; "
                 f"<a href=\"{e(web_base(cfg))}/organize/{day}/queue?t={e(tok)}\" style=\"color:{ACCENT};\">other times</a></div>")
        btns += (f"<div style=\"padding-top:6px;\"><a href=\"{e(web_base(cfg))}/organize/{day}/queue?t={e(tok)}\" "
                 f"style=\"color:{ACCENT};font-weight:bold;\">Manage the intake queue</a> "
                 "<span style=\"color:#8a8886;font-size:9pt;\">(order, do next, hold, release, change route)</span></div>")
    hint = ("Or reply <b>more tonight</b> / <b>light tonight</b>. Go light if you expect to need Claude yourself "
            "tomorrow; the choice lasts one night.")
    return (f"<div style=\"font-size:10pt;color:#424242;padding-top:12px;\"><div style=\"font-weight:bold;padding-bottom:2px;\">"
            f"Claude usage</div><table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
            f"style=\"font-size:10pt;font-family:{FONT};\">{trs}</table>{btns}"
            f"<div style=\"font-size:9pt;color:#8a8886;padding-top:4px;\">{hint}</div></div>")


def send_daily(cfg, log, day, prop, dry=False):
    held = load_held()
    nightly = load_nightly(day)
    remember = load_remember(day)
    try:
        bl = backlog(cfg, log)
    except Exception as ex:  # noqa: BLE001  the counts are a convenience: never let them stop the email
        log(f"backlog counts skipped: {type(ex).__name__}: {ex}")
        bl = None
    try:   # Claude usage block (2026-10-06): a convenience, never stops the email
        import usage_plan
        fc = usage_plan.forecast(cfg)
    except Exception as ex:  # noqa: BLE001
        log(f"usage forecast skipped: {type(ex).__name__}: {ex}")
        fc = None
    subject, body, n = build_daily(cfg, day, prop, held, nightly, remember, bl, fc)
    (C.APP / "reports" / f"update-{day}.html").write_text(body, encoding="utf-8")
    if n == 0 and not (nightly or {}).get("failures") and not (fc or {}).get("ok"):   # the usage block is daily news (2026-10-06)
        log("daily update: nothing needs you; no email")
        return None
    if dry:
        log(f"daily update (dry run) written: {subject}")
        return None
    if (cfg.get("organize") or {}).get("email_enabled") is False:   # home page switch (2026-11-12)
        log(f"daily update not emailed (email is off; questions on the home page): {subject}")
        return None
    code = C.send_mail(cfg, subject, body)
    log(f"daily update emailed ({code}): {subject}")
    return code
