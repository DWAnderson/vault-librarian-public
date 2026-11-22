"""2026-09-28 test: duplicate questions in the morning email, nightly clipping clean-up, YouTube links to the
Remember job. Throwaway vault and temp queue files only: no email is sent, no web page is fetched, no Claude call.

  python setup\\test_dups_clippings_youtube.py      -> prints RESULT PASS / FAIL
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(APP / "jobs")]
import common as C          # noqa: E402
import organize as O        # noqa: E402
import update_email as U    # noqa: E402
import linkjob as L         # noqa: E402
import clippings as CL      # noqa: E402

T = Path(tempfile.mkdtemp(prefix="vl_dups_"))
V, LOCAL = T / "vault", T / "local"
for d in (".obsidian", "Inbox", "Clippings", "Resources", "Archives"):
    (V / d).mkdir(parents=True)
(LOCAL / "work").mkdir(parents=True)
cfg = {"vault_path": str(V), "local_root": str(LOCAL), "timezone": "America/Chicago", "mailbox": "pat@example.com",
       "notify_to": "pat@example.com", "vault_note": "x"}
sent = []
C.send_mail = lambda cfg_, subj, body, to=None: sent.append((subj, body)) or 202
log = lambda m: None   # noqa: E731
# keep the real queue files untouched
L.REMEMBER_QUEUE = T / "remember.jsonl"
CL.HELD = T / "held.json"
_orig_held = O.held_stub_paths
O.held_stub_paths = lambda: {h["stub_path"] for h in (json.loads(CL.HELD.read_text(encoding="utf-8")) if CL.HELD.exists() else {}).values() if h.get("stub_path")}

G = 'generated: { by: "human:pat", at: "2026-09-20T10:00:00-05:00" }'
LONG = "\n\n".join(f"Paragraph {i} about hangar lease rates and the airport's ground rent schedule for tenants." for i in range(12))


def note(rel, fm, body):
    (V / rel).write_text("---\n" + fm + "\n---\n\n" + body + "\n", encoding="utf-8")


note("Resources/Hangar leases.md", f'type: Reference\ntitle: Hangar leases\ndescription: "d"\n{G}', LONG)
note("Archives/Hangar leases copy.md", f'type: Reference\ndescription: "d"\n{G}\nstatus: deprecated\n'
     'duplicate_of: "[[Inbox/Hangar leases]]"\narchived_from: Inbox', LONG)                  # stale path-qualified keeper
note("Archives/Hangar share stub.md", f'type: Web Clipping\ndescription: "d"\n{G}\nstatus: deprecated\n'
     'duplicate_of: "[[Resources/Hangar leases]]"\narchived_from: Clippings\nsuperseded_stub: true',
     "PExample shared this with you.\n\nFollow on Substack for more posts every week.")
note("Resources/Weekly tennis schedule.md", f'type: Reference\ndescription: "a"\n{G}', "Monday doubles at 6.\n\nThursday drills.")
note("Resources/Weekly tennis schedules.md", f'type: Reference\ndescription: "b"\n{G}', "Club calendar for the fall season, courts 1-4.")
ok = True

# ---------- 1. duplicate questions
dq = O.dup_questions(cfg, log, 5)
print("dup questions:", [(q["id"], q["kind"], q.get("path") or q.get("paths"), q["options"]["a"]["action"]) for q in dq])
arch = {q["path"]: q for q in dq if q["kind"] == "archived"}
pairs = [q for q in dq if q["kind"] == "pair"]
ok &= arch.get("Archives/Hangar leases copy.md", {}).get("keeper") == "Resources/Hangar leases.md"   # found by name
ok &= arch["Archives/Hangar leases copy.md"]["options"]["a"]["action"] == "delete"
ok &= arch["Archives/Hangar share stub.md"]["options"]["a"]["action"] == "delete"                    # stub -> delete
ok &= len(pairs) == 1 and pairs[0]["options"]["a"]["action"] == "notdup"                              # title-only -> not dup
ok &= [q["id"] for q in dq] == [5, 6, 7]

# ---------- 2. the email and the reply parser
prop = {"items": [], "questions": [], "new_tags": [], "dups": dq, "pairs": {}, "day": "2026-09-29", "status": "sent",
        "web_token": "tok"}
nightly = {"status": "needs you", "needs": ["2 archived duplicate(s) to restore, merge or delete",
                                            "1 possible duplicate pair(s) to decide", "1 OKF error(s)"], "health": []}
subj, html, n = U.build_daily(cfg, "2026-09-29", prop, [], nightly)
ok &= "Duplicates to decide" in html and "Hangar share stub" in html and "3 to answer" in subj
ok &= "review archived duplicates" not in html and "OKF error" in html      # answerable now; other needs stay
ans, mode = O.parse_answers("ok", prop)
ok &= mode == "ok" and ans == {5: "a", 6: "a", 7: "a"}
ans2, mode2 = O.parse_answers("5a 6c 7b\n\nFrom: Vault\n> 5b", prop)
ok &= mode2 == "letters" and ans2 == {5: "a", 6: "c", 7: "b"}
print("parse:", ans, mode, "|", ans2, mode2)

# ---------- 3. apply: 5a delete, 6c restore, 7b keep the longer one and archive the other
f = T / "organize-2026-09-29.json"
f.write_text(json.dumps(prop), encoding="utf-8")
summary = O.apply_answers(cfg, log, f, prop, ans2, mode2, "test")
print("apply:", summary)
ok &= not (V / "Archives/Hangar leases copy.md").exists()
ok &= (V / "Clippings/Hangar share stub.md").exists()           # restored to archived_from
kept = [p for p in ("Resources/Weekly tennis schedule.md", "Resources/Weekly tennis schedules.md") if (V / p).exists()]
ok &= len(kept) == 1 and len(list((V / "Archives").glob("Weekly tennis*.md"))) == 1
ok &= sent and "duplicate(s) resolved" in sent[-1][0]
ok &= json.loads(f.read_text(encoding="utf-8"))["status"] == "applied"

# ---------- 4. nightly clipping clean-up: stubs and basics
(V / "Clippings/Hangar share stub.md").unlink()
note("Clippings/2026-09-28_Do I care_.md", 'title: "Do I care?"\nsource: "https://open.substack.com/pub/theargument/p/do-i-care?utm_source=share&r=1tyks"\n'
     'created: "2026-09-28"\ntags:\n  - clippings', "Follow on Substack\n\n### PExample, a subscriber of The Argument, shared this with you.")
note("Clippings/2026-09-28_Paywalled_.md", 'title: "Paywalled"\nsource: "https://www.example.com/p/paywalled?utm_campaign=email-post"\n'
     'created: "2026-09-28"', "PExample, a paid subscriber of Example, shared this with you.")
note("Clippings/Real clipping.md", 'title: "Real clipping"\nsource: "https://news.example.com/story"\ncreated: "2026-09-27"', LONG)
ok &= CL.canonical("https://open.substack.com/pub/theargument/p/do-i-care?utm_source=share&r=1") == "https://theargument.substack.com/p/do-i-care"
ok &= CL.canonical("https://www.example.com/p/paywalled?utm_campaign=email-post") == "https://www.example.com/p/paywalled"
calls = []


def fake_save(cfg_, log_, url, note_="", via=None, dry=False):
    calls.append(url)
    if "theargument" in url:
        (V / "Inbox/Do I care.md").write_text(f'---\ntype: Web Clipping\ndescription: "full"\nresource: "{url}"\n{G}\n---\n\n{LONG}\n', encoding="utf-8")
        return {"ok": True, "note": "Inbox/Do I care.md"}
    return {"ok": False, "needs": "browser", "why": "only 60 words came through (paywall)"}


real_save = CL.linkjob.save_url
CL.linkjob.save_url = fake_save
head, lines = CL.fix_stubs(cfg, log, False)
CL.linkjob.save_url = real_save
print("stubs:", head, lines)
ok &= sorted(calls) == ["https://theargument.substack.com/p/do-i-care", "https://www.example.com/p/paywalled"]
arch_stub = V / "Archives/2026-09-28_Do I care_.md"
ok &= arch_stub.exists() and "superseded_stub: true" in arch_stub.read_text(encoding="utf-8")
ok &= "[[Inbox/Do I care]]" in arch_stub.read_text(encoding="utf-8")
held = json.loads(CL.HELD.read_text(encoding="utf-8"))
ok &= len(held) == 1 and list(held.values())[0]["stub_path"] == "Clippings/2026-09-28_Paywalled_.md"
ok &= "Clippings/2026-09-28_Paywalled_.md" not in [p.relative_to(V).as_posix() for p in O.unfiled(V, 40)]
ok &= "Clippings/Real clipping.md" in [p.relative_to(V).as_posix() for p in O.unfiled(V, 40)]
head2, lines2 = CL.fill_basics(cfg, log, False)
print("basics:", head2, lines2)
rc_ = (V / "Clippings/Real clipping.md").read_text(encoding="utf-8")
ok &= "Web Clipping" in rc_ and "resource:" in rc_ and "generated:" in rc_ and "tool/obsidian-web-clipper" in rc_
# a new stub archived tonight is suggested for delete tomorrow
dq2 = O.dup_questions(cfg, log, 1)
st = [q for q in dq2 if q.get("path") == "Archives/2026-09-28_Do I care_.md"]
ok &= bool(st) and st[0]["options"]["a"]["action"] == "delete"

# ---------- 5. YouTube links go to the Remember job
ok &= L.youtube_id("https://www.youtube.com/watch?v=XhKbq8Jzp0U&t=3s") == "XhKbq8Jzp0U"
ok &= L.youtube_id("https://youtu.be/XhKbq8Jzp0U") == "XhKbq8Jzp0U" and L.youtube_id("https://example.com/watch?v=1") is None
r1 = L.save_url(cfg, log, "https://www.youtube.com/watch?v=AAAAAAAAAAA")
r2 = L.save_url(cfg, log, "https://youtu.be/AAAAAAAAAAA")
q = [json.loads(x) for x in L.REMEMBER_QUEUE.read_text(encoding="utf-8").splitlines() if x.strip()]
ok &= r1.get("queued") and r2.get("queued") and len(q) == 1 and q[0]["video_id"] == "AAAAAAAAAAA"
note("Resources/Inside the lab.md", f'type: Video Summary\ndescription: "v"\nresource: "https://www.youtube.com/watch?v=XhKbq8Jzp0U"\n{G}', "x")
r3 = L.save_url(cfg, log, "https://www.youtube.com/watch?v=XhKbq8Jzp0U")
ok &= r3.get("ok") and r3.get("duplicate")
print("youtube:", r1, r3)

lint = subprocess.run([sys.executable, str(C.VL / "okf_lint.py"), str(V), "--no-orphans"], capture_output=True, text=True)
print(lint.stdout.strip().splitlines()[-3:])
ok &= " 0 errors" in lint.stdout
shutil.rmtree(T, ignore_errors=True)
print("RESULT", "PASS" if ok else "FAIL")
