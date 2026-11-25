#!/usr/bin/env python3
"""Phase 1 tests for the intake queue, on a throwaway vault with a fake Graph mailbox and a stubbed
claude -p. Never touches the real vault, queue, reports or Outlook.

  python setup\\queue\\test_queue.py
"""
import base64
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

APPDIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPDIR / "jobs"))
# intake.py imports organize (and it imports update_email); the queue doesn't use them
for name in ("organize", "update_email"):
    m = types.ModuleType(name)
    m.has_pending = lambda: False
    m.has_unapplied = lambda: False                 # saved answers awaiting a retry (2026-11-23)
    m.catch_up_send = lambda *a, **k: None
    sys.modules.setdefault(name, m)

import common as C  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="vq-test-"))
VAULT, LOCAL = TMP / "vault", TMP / "local"
(VAULT / "Inbox").mkdir(parents=True)
(VAULT / ".obsidian").mkdir()
(TMP / "reports").mkdir()
(TMP / "config").mkdir()
(TMP / "dropbox").mkdir()
REAL_APP = C.APP
CFG = {
    "owner": {"name": "Pat", "id": "human:pat", "note_label": "Pat's note"}, "pc_name": "TESTPC",
    "vault_path": str(VAULT), "local_root": str(LOCAL), "timezone": "America/Chicago", "mailbox": "you@example.com",
    "lock_stale_minutes": 120,
    "intake": {"enabled": False, "batch": 20, "remember_folder": "Remember", "confidential_domains": ["lawfirm.example"],
               "signature_markers": ["Pat Example"], "verbatim_max_words": 2000},
    "queue": {"enabled": True, "files_enabled": True, "dropbox_path": str(TMP / "dropbox")},
}
for sub in ("logs", "work", "snapshots", "queue"):
    (LOCAL / sub).mkdir(parents=True, exist_ok=True)
C.APP = TMP
import os  # noqa: E402
os.chdir(TMP)   # never run with the app folder as the current folder (2026-09-28 incident)                     # items and reports go to the temp folder; lib paths (C.VL) stay real
C.load_config = lambda: json.loads(json.dumps(CFG))
os.environ.update(VAULT_OWNER_NAME="Pat", VAULT_OWNER_ID="human:pat", VAULT_OWNER_NOTE_LABEL="Pat's note")   # lib scripts

import vqueue as Q  # noqa: E402
import claude_cli  # noqa: E402
import linkjob  # noqa: E402
import intake as I  # noqa: E402
import queue_run as R  # noqa: E402

CALLS = []
LIMIT = {"on": False}


def fake_ask(instructions, material, cwd, timeout=300, **kw):
    CALLS.append(instructions[:30])
    if LIMIT["on"]:
        return None, {"error": "Claude AI usage limit reached|1759100000"}
    if "one sentence" in instructions.lower():
        return "A test description.", {"ms": 5, "cost_usd": 0.001}
    return "Summary paragraph.\n\n## Key facts\n- fact", {"ms": 5, "cost_usd": 0.01}


claude_cli.ask = fake_ask
LINKS = []


def fake_save_url(cfg, log, url, doug_note="", captured_via=None, dry=False, **kw):   # html, title_hint
    LINKS.append((url, captured_via, dry))
    if dry:
        return {"ok": True, "dry": True, "would": "write a Web Clipping note"}
    if "youtube.com" in url:  # linkjob hands YouTube to the Remember job (2026-09-28)
        return {"ok": True, "queued": True, "category": "video", "note": "YouTube abc queued for tonight's Remember job"}
    return {"ok": True, "note": "Inbox/Example article.md", "category": "article"}


linkjob.save_url = fake_save_url


def msg(mid, subject, text, sender="friend@example.com", atts=False, received="2026-09-28T10:00:00Z"):
    return {"id": mid, "subject": subject, "from": {"emailAddress": {"address": sender}},
            "toRecipients": [{"emailAddress": {"address": "you@example.com"}}], "ccRecipients": [],
            "receivedDateTime": received, "internetMessageId": f"<{mid}@test>", "webLink": f"https://outlook/{mid}",
            "body": {"contentType": "text", "content": text}, "hasAttachments": atts}


LONG = " ".join(["word"] * 2500)
BOX = {
    "REM": [msg("m1", "Lunch notes", "We agreed the lease renewal is due November 1 and the rent goes to 4,200 a month "
                "starting in January. Please keep this for the file along with the other items we discussed today."),
            msg("m2", "Remember", "https://example.com/article"),
            msg("m3", "Client matter", "Privileged draft attached for review of the matter.", sender="a@lawfirm.example"),
            msg("m4", "Long newsletter", LONG, received="2026-09-28T09:00:00Z")],
    "BOOKS": [msg("m5", "Book", "for the vault", atts=True)],
}
ATTS = {"m5": [{"id": "a1", "name": "Policy Book.pdf", "size": 4_000_000, "isInline": False, "contentType": "application/pdf",
                "@odata.type": "#microsoft.graph.fileAttachment"},
               {"id": "a2", "name": "logo.png", "size": 3000, "isInline": False, "contentType": "image/png",
                "@odata.type": "#microsoft.graph.fileAttachment"}]}
MOVES = []


class Resp:
    def __init__(self, data=None, status=200, content=b""):
        self._d, self.status_code, self.content = data, status, content
        self.ok = status < 400

    def json(self):
        return self._d

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class FakeSession:
    def get(self, url, params=None, timeout=None):
        if url.endswith("/mailFolders/inbox/childFolders"):
            return Resp({"value": [{"id": "REM", "displayName": "Remember", "totalItemCount": len(BOX["REM"])}]})
        if url.endswith("/mailFolders/REM/childFolders"):
            return Resp({"value": [{"id": "BOOKS", "displayName": "Books", "totalItemCount": len(BOX["BOOKS"])}]})
        for f in BOX:
            if url.endswith(f"/mailFolders/{f}/messages"):
                return Resp({"value": list(BOX[f])})
        if "/attachments/" in url:
            return Resp({"contentBytes": base64.b64encode(b"%PDF-1.4 fake").decode()})
        if url.endswith("/attachments"):
            mid = url.split("/messages/")[1].split("/")[0]
            return Resp({"value": ATTS.get(mid, [])})
        return Resp(status=404)

    def post(self, url, json=None, timeout=None):
        mid = url.split("/messages/")[1].split("/")[0]
        MOVES.append(mid)
        for f in BOX:
            BOX[f] = [m for m in BOX[f] if m["id"] != mid]
        return Resp({}, 201)


I.graph = lambda cfg: (FakeSession(), "https://graph/users/me")
FAILS = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def items():
    return Q.Store(C.load_config()).all()


def by_title(t):
    return [x for x in items() if x.get("title") == t]


try:
    cfg = C.load_config()
    # A. ordering rules and manual overrides
    st = Q.Store(cfg, TMP / "rules-items", TMP / "rules-payload")
    a = st.create(source="mail", title="mail", status="ready"); a["status"] = "ready"
    b = st.create(source="folder", title="book", route="book"); b["status"] = "ready"
    c = st.create(source="folder", title="recipe", route="recipe"); c["status"] = "ready"
    d = st.create(source="folder", title="held by hand", route="recipe"); d["status"] = "ready"; d["manual"]["hold"] = True
    order = [x["title"] for x in Q.ordered([a, b, c, d], cfg)]
    check(order == ["mail", "recipe", "book"], f"rules order mail > default > book, hold excluded ({order})")
    b["manual"]["priority"] = 1000
    check(Q.ordered([a, b, c], cfg)[0]["title"] == "book", "manual priority beats the rules")
    check(Q.priority(c, Q.qcfg(cfg)["rules"], C.now(cfg))[1] == "default", "priority explains its rule")
    old = st.create(source="folder", title="old", route="article"); old["status"] = "ready"
    old["added"] = "2026-01-01T00:00:00-06:00"
    rules = [{"match": {"older_than_days": 30}, "priority": 200}, {"match": {}, "priority": 50}]
    check(Q.priority(old, rules, C.now(cfg))[0] == 200, "older_than_days rule")

    # A2. the 2026-09-28 incident: a missing, relative or app/vault dropbox path must never be collected
    import collect_folder as CF
    for bad in ("", ".", str(APPDIR), str(APPDIR / "jobs"), str(APPDIR.parent), str(VAULT)):
        c2 = json.loads(json.dumps(CFG))
        c2["queue"]["dropbox_path"] = bad
        rows, new = CF.collect(c2, print, Q.Store(c2, TMP / "guard-items", TMP / "guard-payload"), True)
        check(not new, f"dropbox path {bad!r} collects nothing")

    # B. dry run: nothing saved, nothing moved, no model calls
    rc = R.main(["--dry-run"])
    check(rc == 0 and not items() and not MOVES and not CALLS, "dry run saves, moves and calls nothing")
    check(any((TMP / "reports").glob("* queue (dry run).md")), "dry-run report written")
    LINKS.clear()

    # C. live run
    rc = R.main(["--live"])
    check(rc == 0, "live run completes")
    notes = sorted(p.name for p in (VAULT / "Inbox").glob("*.md"))
    check(any("Lunch notes" in n for n in notes), "short email -> verbatim Email note")
    ln = next(p for p in (VAULT / "Inbox").glob("*Long newsletter*.md"))
    txt = ln.read_text(encoding="utf-8")
    check("content: \"summary\"" in txt and "Summary paragraph" in txt, "long email -> summary Email note")
    check("type: \"Email\"" in txt and "generated:" in txt and "description: \"A test description.\"" in txt, "Email note has OKF keys")
    check(LINKS and LINKS[-1][1] == "mid:m2@test" and not LINKS[-1][2], "link email -> url-to-vault with mid")
    m3 = by_title("Client matter")[0]
    check(m3["status"] == "held" and m3["needs"] == "you", "confidential sender held for the owner")
    box = by_title("Book")[0]
    kids = Q.Store(cfg).children(box)
    check(box["route"] == "container" and len(kids) == 1, "attachment email -> container with one child (logo skipped)")
    check(kids and kids[0]["route"] == "book" and kids[0]["route_from"] == "folder", "child takes the subfolder's route")
    check(kids and (LOCAL / "queue" / "payload" / kids[0]["id"] / "Policy Book.pdf").exists(), "attachment captured locally")
    check(kids and kids[0]["doug_note"] == "for the vault", "short body becomes the owner's note on the attachment")
    check(sorted(MOVES) == ["m1", "m2", "m4"], f"only saved emails moved ({sorted(MOVES)})")
    check("m5" not in MOVES and "m3" not in MOVES, "container with open attachment and held email stay")
    check(not list((TMP / "queue" / "items").glob("*.tmp")), "no temp files left")

    # D. second run: nothing collected twice
    n_before = len(items())
    R.main(["--live"])
    check(len(items()) == n_before, "second run adds no duplicates")

    # E. child finished by hand -> container closes, email removed
    Q.main(["resolved", kids[0]["id"], "Inbox/Policy Book.md"])
    R.main(["--live", "--no-collect"])
    box = by_title("Book")[0]
    check(box["status"] == "done" and "m5" in MOVES, "container closes when its attachment is done; email removed")

    # F. The owner deletes the held email -> item cancelled
    BOX["REM"] = [m for m in BOX["REM"] if m["id"] != "m3"]
    R.main(["--live"])
    check(by_title("Client matter")[0]["status"] == "cancelled", "email gone from Remember -> item cancelled")

    # F2. YouTube link: queued for the Remember job, counts as done, email removed, no note path recorded
    BOX["REM"].append(msg("m7", "Remember", "https://www.youtube.com/watch?v=abcdefghijk"))
    R.main(["--live"])
    yt = [x for x in items() if (x.get("url") or "").startswith("https://www.youtube.com")]
    check(yt and yt[0]["status"] == "done" and yt[0]["result"]["notes"] == [] and "m7" in MOVES,
          "YouTube link -> queued for the Remember job, email removed, no fake note path")

    # G. usage limit: item waits, not failed
    BOX["REM"].append(msg("m6", "Another long one", LONG))
    LIMIT["on"] = True
    R.main(["--live"])
    m6 = by_title("Another long one")[0]
    check(m6["status"] == "ready" and m6["attempts"] == 0 and "m6" not in MOVES, "usage limit -> waits for a later run")
    LIMIT["on"] = False
    R.main(["--live"])
    check(by_title("Another long one")[0]["status"] == "done" and "m6" in MOVES, "next run finishes it")

    # I. held.json bridge (switch-over)
    HJ = TMP / "queue" / "held.json"

    def hj():
        return json.loads(HJ.read_text(encoding="utf-8")) if HJ.exists() else {}
    BOX["REM"].append(msg("L1", "Remember", "https://www.washingtonpost.com/x"))
    HJ.write_text(json.dumps({
        "L1": {"subject": "Remember", "url": "https://www.washingtonpost.com/x", "why": "bot wall", "needs": "browser",
               "status": "held", "doug_note": "", "internet_message_id": "<L1@test>", "headless_tries": 2},
        "L2": {"subject": "gone", "url": "https://example.com/gone", "needs": "browser", "status": "held"},
        "web-x": {"subject": "(shared from phone)", "url": "https://a.example", "status": "saved", "note": "Inbox/A.md"},
        "web-y": {"subject": "(shared from phone)", "url": "https://b.example", "status": "held", "needs": "browser"}}),
        encoding="utf-8")
    R.main(["--live"])
    h = hj()
    l1 = [x for x in items() if (x.get("source_ref") or {}).get("message_id") == "L1"]
    check(len(l1) == 1 and l1[0]["status"] == "held" and l1[0]["needs"] == "browser", "legacy held entry adopted, not re-captured")
    check("L1" in h and h["L1"].get("queue_id") == l1[0]["id"] and h["L1"].get("headless_tries") == 2,
          "adopted entry keeps the headless retry count")
    check("L2" not in h, "entry whose email is gone is dropped (stale fix)")
    check("web-x" not in h and "web-y" in h, "saved phone share dropped; held phone share left alone")
    check("L1" not in MOVES, "held email stays in Remember")
    h["L1"].update({"status": "saved", "note": "Resources/WaPo.md", "saved_by": "headless browser"})
    HJ.write_text(json.dumps(h), encoding="utf-8")
    R.main(["--live"])
    l1 = [x for x in items() if (x.get("source_ref") or {}).get("message_id") == "L1"][0]
    check(l1["status"] == "done" and l1["result"]["notes"] == ["Resources/WaPo.md"] and "L1" in MOVES,
          "saved by the headless retry -> item done, email removed")
    check("L1" not in hj(), "entry dropped once the email is gone")

    BOX["REM"].append(msg("m9", "Firm memo", "Please review the attached draft for our meeting next week.", sender="b@lawfirm.example"))
    R.main(["--live"])
    m9 = by_title("Firm memo")[0]
    check("m9" in hj() and hj()["m9"]["needs"] == "you" and hj()["m9"]["queue_id"] == m9["id"],
          "held queue item appears in held.json for the morning email")
    Q.main(["release", m9["id"]])
    R.main(["--live"])
    check(by_title("Firm memo")[0]["status"] == "done" and "m9" in MOVES and "m9" not in hj(),
          "released by you -> filed, email removed, entry dropped")

    # J. intake.py hands Remember to the queue when queue.enabled
    CFG["intake"]["enabled"] = True
    BOX["REM"].append(msg("m10", "Via intake", "A short note about the hangar lease renewal terms we agreed on today."))
    sys.argv = ["intake.py", "--force"]
    rc = I.main()
    check(rc == 0 and by_title("Via intake") and by_title("Via intake")[0]["status"] == "done" and "m10" in MOVES,
          "intake.py -> queue_run when queue.enabled")

    # H. CLI and the switch-over guard
    Q.main(["hold", m6["id"], "testing"])
    check(by_title("Another long one")[0]["manual"]["hold"], "CLI hold")
    CFG["intake"]["enabled"], CFG["queue"]["enabled"] = True, False
    check(R.main(["--live"]) == 2, "live run refused while intake.py is live")
finally:
    C.APP = REAL_APP
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
