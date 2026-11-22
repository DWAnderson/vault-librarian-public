"""2026-09-28 test: held links retried in a headless browser (jobs\\browser_retry.py). Throwaway vault, temp queue
file and a local web server only: one page builds its text with JavaScript (should be saved), one is a paywall
(must be left for the laptop). No sign-in, no email, no Claude call. Needs Playwright + Chromium installed.

  python setup\\test_browser_retry.py      -> prints RESULT PASS / FAIL
"""
import functools
import http.server
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(APP / "jobs")]
import common as C          # noqa: E402
import linkjob as L         # noqa: E402
import browser_retry as BR  # noqa: E402

T = Path(tempfile.mkdtemp(prefix="vl_headless_"))
V, LOCAL, WEB = T / "vault", T / "local", T / "web"
for d in (V / ".obsidian", V / "Inbox", V / "Clippings", V / "Archives", LOCAL / "work", WEB):
    d.mkdir(parents=True)
cfg = {"vault_path": str(V), "local_root": str(LOCAL), "timezone": "America/Chicago"}
log = lambda m: None   # noqa: E731
L.claude_cli.ask = lambda *a, **k: (None, {"error": "no claude in tests"})
BR.HELD = T / "held.json"

text = " ".join(f"Sentence {i} explains the hangar ground lease escalation clause and what tenants at small airports "
                f"should negotiate." for i in range(40))
(WEB / "js.html").write_text(
    '<html lang="en"><head><title>Hangar leases explained</title><meta property="og:type" content="article">'
    '<meta property="og:title" content="Hangar leases explained"><meta name="author" content="Jane Pilot">'
    '<meta property="article:published_time" content="2026-09-20"><meta property="og:site_name" content="Test Aviation News">'
    f'</head><body><main id=m></main><script>const t={json.dumps(text)};const a=document.createElement("article");'
    'a.innerHTML="<h1>Hangar leases explained</h1>"+t.split(". ").map(s=>"<p>"+s+".</p>").join("");'
    'document.getElementById("m").appendChild(a);</script></body></html>', encoding="utf-8")
(WEB / "pay.html").write_text(
    '<html><head><title>Paid post</title><meta property="og:type" content="article"></head><body><article><h1>Paid post</h1><p>'
    + "Teaser text about something interesting that goes on for a while here. " * 40
    + '</p><p>This post is for paid subscribers. Already a paid subscriber? Sign in</p></article></body></html>', encoding="utf-8")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
(V / "Clippings/js stub.md").write_text('---\ntitle: "js"\nsource: "x"\n---\n\nshared this with you\n', encoding="utf-8")
BR.HELD.write_text(json.dumps({
    "MSGID1": {"subject": "Remember", "url": f"{base}/js.html", "needs": "browser", "status": "held", "doug_note": "",
               "internet_message_id": "<abc@example.com>"},
    "stub-1": {"subject": "(Web Clipper stub)", "url": f"{base}/js.html", "needs": "browser", "status": "held",
               "stub_path": "Clippings/js stub.md"},
    "web-2": {"subject": "(shared from phone)", "url": f"{base}/pay.html", "needs": "browser", "status": "held"},
    "OTHER": {"subject": "Remember", "url": "https://example.com/x", "needs": "you", "status": "held"}}), encoding="utf-8")

ok = True
head, lines = BR.retry(cfg, log, False)
print(head)
print("\n".join(lines))
held = json.loads(BR.HELD.read_text(encoding="utf-8"))
ok &= held["MSGID1"]["status"] == "saved" and held["stub-1"]["status"] == "saved"
note = V / held["MSGID1"]["note"]
ok &= note.exists() and "Sentence 39" in note.read_text(encoding="utf-8") and "abc@example.com" in note.read_text(encoding="utf-8")
ok &= (V / "Archives/js stub.md").exists() and "superseded_stub: true" in (V / "Archives/js stub.md").read_text(encoding="utf-8")
ok &= held["web-2"]["status"] == "held" and held["web-2"].get("headless_tries") == 1 and "paywall" in held["web-2"]["headless_why"]
ok &= held["OTHER"] == {"subject": "Remember", "url": "https://example.com/x", "needs": "you", "status": "held"}
for _ in range(2):          # gives up after MAX_TRIES nights
    BR.retry(cfg, log, False)
held = json.loads(BR.HELD.read_text(encoding="utf-8"))
ok &= held["web-2"].get("headless_gave_up") is True and held["web-2"]["status"] == "held"
head3, _ = BR.retry(cfg, log, False)
ok &= head3 == "no links waiting for a browser"
srv.shutdown()
shutil.rmtree(T, ignore_errors=True)
print("RESULT", "PASS" if ok else "FAIL")
