"""Nightly retry of held links in a headless browser on this PC (called by nightly.py; added 2026-09-28).

Links that url_probe couldn't read (sites that refuse scripts, pages built by JavaScript) wait in
queue\\held.json with needs: browser. This step opens each one once in headless Chromium (Playwright) with
NO sign-ins - it never logs in, never fills a form. If the full text comes through, the note is written
with the normal url-to-vault routine (linkjob.save_url) and the entry is marked saved, exactly as a laptop
"process the vault queue" session would; intake then removes the Remember email. Paywalled / sign-in
pages are left for the laptop (the owner's decision, 2026-09-28). An entry that fails MAX_TRIES nights is no
longer retried here (headless_gave_up) and waits for the laptop.

One-time install (the owner's own PowerShell on this PC; package installs don't work from a Claude session):
  & "$env:LOCALAPPDATA\\Programs\\VaultLibrarian\\runtime\\Scripts\\python.exe" -m pip install playwright
  & "$env:LOCALAPPDATA\\Programs\\VaultLibrarian\\runtime\\Scripts\\python.exe" -m playwright install chromium
Without Playwright the step reports "not installed" and changes nothing.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import linkjob  # noqa: E402

HELD = C.APP / "queue" / "held.json"
MAX_TRIES = 3
WAIT_MS = 4000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")
PAYWALL = re.compile(r"(this post is for paid subscribers|subscribe to (continue|keep) reading|keep reading with a|"
                     r"already a (paid )?subscriber\?|become a (paid )?(subscriber|member) to|"
                     r"to continue reading,? (please )?(subscribe|sign in|log in)|create a free account to continue|"
                     r"sign in to (continue|read)|access denied|you don't have permission to access|"
                     r"verify you are (a )?human|are you a robot|enable javascript and cookies)", re.I)

# Same compact page as the url-to-vault skill's browser fallback (url-to-vault/scripts/page_extract.js on the
# laptop; publish.ps1 copies only *.py, so this copy is separate): citation metadata + main text blocks.
# If you change one, consider the same change in the other.
EXTRACT = r"""() => {
 const keep=/^(og:(title|site_name|type|url|description)|article:(published_time|modified_time|section)|author|description|citation_|dc\.|prism\.|parsely-(author|pub-date|title)|sailthru\.author)/i;
 const h=[...document.querySelectorAll('title,head link[rel=canonical],script[type="application/ld+json"],head meta')]
   .filter(e=>e.tagName!=='META'||keep.test(e.getAttribute('property')||e.getAttribute('name')||''))
   .map(e=>e.tagName==='SCRIPT'?'<script type="application/ld+json">'+e.textContent.slice(0,4000)+'</script>':e.outerHTML).join('\n');
 const m=document.querySelector('article')||document.querySelector('main')||document.body;
 const blocks=[...m.querySelectorAll('h1,h2,h3,p,li,blockquote')].map(e=>[e.tagName,e.textContent.trim().replace(/\s+/g,' ')])
   .filter(([t,x])=>x && (/^H/.test(t) || x.split(' ').length>=6));
 const esc=x=>x.replace(/&/g,'&amp;').replace(/</g,'&lt;');
 return {html:'<html lang="'+(document.documentElement.lang||'')+'"><head>'+h+'</head><body><article>'
   +blocks.map(([t,x])=>'<'+(t==='LI'?'p':t.toLowerCase())+'>'+esc(x)+'</'+(t==='LI'?'p':t.toLowerCase())+'>').join('\n')+'</article></body></html>',
   text:(document.body.innerText||'').slice(0,200000), title:document.title};
}"""


def load_held():
    try:
        return json.loads(HELD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def captured_via(key, h):
    if key.startswith("web-"):
        return "share:phone"
    if key.startswith("stub-"):
        return "clipper-stub"
    mid = (h.get("internet_message_id") or "").strip("<>")
    return f"mid:{mid}" if mid else "headless-retry"


def clean(url):
    """Drop tracking parameters (utm_*, r=, fbclid...) so the site doesn't redirect mid-load."""
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not re.match(r"^(utm_|r$|fbclid$|gclid$|mc_|ref$|source$)", k, re.I)]
    return urlunparse(p._replace(query=urlencode(q)))


def fetch(page, url):
    """-> (html or None, why). Never signs in: just loads the page and reads it."""
    url = clean(url)
    resp = None
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:  # noqa: BLE001  a redirect can interrupt goto; read wherever the page ended up
        page.wait_for_timeout(WAIT_MS)
        if not page.url.startswith("http"):
            return None, f"headless browser error: {type(e).__name__}: {str(e)[:120]}"
    try:
        page.wait_for_timeout(WAIT_MS)
        got = page.evaluate(EXTRACT)
    except Exception as e:  # noqa: BLE001
        return None, f"headless browser error: {type(e).__name__}: {str(e)[:120]}"
    status = resp.status if resp else 0
    if status >= 400:
        return None, f"HTTP {status} in the headless browser"
    if PAYWALL.search(got.get("text", "")[-6000:]) or PAYWALL.search(got.get("title", "")):
        return None, "paywall, sign-in or bot check in the headless browser"
    return got["html"], ""


def retry(cfg, log, dry):
    """-> (summary line, detail lines). The caller holds the vault lock."""
    held = load_held()
    todo = {}
    for k, h in held.items():
        if h.get("status") == "held" and h.get("needs") == "browser" and h.get("url") and not h.get("headless_gave_up"):
            todo.setdefault(h["url"], []).append(k)
    if not todo:
        return "no links waiting for a browser", []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return (f"{len(todo)} link(s) waiting, but Playwright is not installed on {C.pc_name()} (see jobs\\browser_retry.py "
                f"for the two install commands); left for the laptop"), []
    work = Path(cfg["local_root"]) / "work" / "links"
    work.mkdir(parents=True, exist_ok=True)
    lines, updates, archive = [], {}, []
    now = C.now(cfg).isoformat(timespec="seconds")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 2000})
        page = ctx.new_page()
        for url, keys in todo.items():
            h0 = held[keys[0]]
            name = h0.get("title") or url
            html, why = fetch(page, url)
            res = {}
            if html:
                hp = work / f"h-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}.html"
                hp.write_text(html, encoding="utf-8")
                res = linkjob.save_url(cfg, log, url, h0.get("doug_note", ""), captured_via(keys[0], h0), dry, html=str(hp))
                if not res.get("ok"):
                    why = str(res.get("why") or "not readable")
            if res.get("ok") and res.get("dry"):
                lines.append(f"{name}: would save ({res.get('would')})")
                continue
            if res.get("ok"):
                note = str(res.get("note") or "")
                for k in keys:
                    updates[k] = {"status": "saved", "note": note, "saved_at": now, "saved_by": f"{C.pc_name()} headless browser"}
                    stub = held[k].get("stub_path")
                    if stub:
                        keep = note[:-3] if note.endswith(".md") else note
                        archive.append({"path": stub, "move_to": "Archives", "add_generated": True,
                                        "generated_by": "tool/obsidian-web-clipper",
                                        "set": {"type": "Web Clipping", "status": "deprecated", "duplicate_of": f"[[{keep}]]",
                                                "archived_from": "Clippings", "superseded_stub": True}})
                lines.append(f"{name}: {'already in the vault as' if res.get('duplicate') else 'saved as'} {note}")
            else:
                for k in keys:
                    tries = int(held[k].get("headless_tries") or 0) + 1
                    u = {"headless_tries": tries, "headless_why": why}
                    if tries >= MAX_TRIES:
                        u["headless_gave_up"] = True
                    updates[k] = u
                lines.append(f"{name}: left for the laptop ({why})")
        browser.close()
    if dry:
        return f"{len(todo)} link(s) tried in the headless browser (dry run: nothing saved)", lines
    if archive:
        pj = Path(cfg["local_root"]) / "work" / "nightly-headless-stubs.json"
        pj.write_text(json.dumps({"actor": "process:vault-nightly", "log": "Stubs replaced by headless-browser saves",
                                  "items": archive}, ensure_ascii=False, indent=1), encoding="utf-8")
        rc, out = C.run(log, C.VL / "apply_plan.py", Path(cfg["vault_path"]), pj)
        if rc:
            lines.append("apply_plan problem archiving stubs: " + (out.strip().splitlines() or ["?"])[-1])
    if updates:   # re-read just before writing: intake and the vault service also write held.json
        fresh = load_held()
        for k, u in updates.items():
            if k in fresh:
                fresh[k].update(u)
        HELD.write_text(json.dumps(fresh, ensure_ascii=False, indent=1), encoding="utf-8")
    saved = len({u['note'] for u in updates.values() if u.get('status') == 'saved'})
    return f"{len(todo)} link(s) tried in the headless browser: {saved} saved, {len(todo) - saved} left for the laptop", lines
