#!/usr/bin/env python3
"""Tests for concept analysis (plan: 2026-09-28 Concept analysis and links - plan.md, phase A), on a
throwaway vault with a stubbed claude -p. Never touches the real vault, queue, reports or Outlook.

  python setup\\queue\\test_analysis.py
"""
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

APPDIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPDIR / "jobs"))
import common as C  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="an-test-"))
VAULT, LOCAL = TMP / "vault", TMP / "local"
for d in ("Resources", "Concepts", "Inbox", "Personal Notes", ".obsidian"):
    (VAULT / d).mkdir(parents=True)
for d in ("reports", "config", "dropbox", "queue"):
    (TMP / d).mkdir()
for sub in ("logs", "work", "snapshots", "queue"):
    (LOCAL / sub).mkdir(parents=True, exist_ok=True)
NOW = dt.datetime.now().astimezone()
CFG = {
    "owner": {"name": "Pat", "id": "human:pat", "note_label": "Pat's note"}, "pc_name": "TESTPC",
    "vault_path": str(VAULT), "local_root": str(LOCAL), "timezone": "America/Chicago", "mailbox": "you@example.com",
    "lock_stale_minutes": 120,
    "intake": {"enabled": False, "batch": 20, "remember_folder": "Remember", "confidential_domains": ["lawfirm.example"],
               "signature_markers": ["Pat Example"], "verbatim_max_words": 2000},
    "queue": {"enabled": True, "files_enabled": True, "dropbox_path": str(TMP / "dropbox"), "confirm_over_usd": 0.25},
    "analysis": {"auto": True, "since": (NOW - dt.timedelta(hours=1)).isoformat(timespec="seconds")},
}
C.APP = TMP
os.chdir(TMP)   # never with the app folder as the current folder (2026-09-28 incident)
C.load_config = lambda: json.loads(json.dumps(CFG))
os.environ.update(VAULT_OWNER_NAME="Pat", VAULT_OWNER_ID="human:pat", VAULT_OWNER_NOTE_LABEL="Pat's note")   # lib scripts
C.send_mail = lambda *a, **k: None

import vqueue as Q  # noqa: E402
import claude_cli  # noqa: E402
import queue_run as R  # noqa: E402
import analyze as AN  # noqa: E402

GEN_AT = NOW.isoformat(timespec="seconds")
OLD_AT = (NOW - dt.timedelta(days=30)).isoformat(timespec="seconds")
WORDS = " ".join(["Markets coordinate dispersed knowledge through prices that no planner could gather."] * 30)


def note(rel, ntype, body, at=GEN_AT, **extra):
    fm = {"type": ntype, "title": Path(rel).stem, "description": extra.pop("description", f"About {Path(rel).stem}."),
          "generated": {"by": "tool/test", "at": at}}
    fm.update(extra)
    import yaml
    p = VAULT / rel
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body + "\n", encoding="utf-8")


note("Resources/Hayek on knowledge.md", "Article", WORDS, tags=["economics"],
     description="Hayek argues prices carry knowledge no central planner can hold.")
note("Resources/Munger on public choice.md", "Video Summary", "## Summary\n\n" + WORDS + "\n\n## Transcript\n\n[[Munger on public choice - Transcript]]",
     tags=["economics"], description="Mike Munger on public choice theory and politics without romance.")
note("Resources/Munger on public choice - Transcript.md", "Transcript",
     "Summary: [[Munger on public choice]]\n\n00:01 TRANSCRIPT-MARKER politicians respond to incentives " + WORDS)
note("Resources/Airbnb regulation.md", "Web Clipping", WORDS, at=OLD_AT, tags=["economics"],
     description="How one group captured short-term rental regulation.")
note("Resources/Old article.md", "Article", WORDS, at=OLD_AT, description="An old article before the switch-on time.")
note("Resources/Short.md", "Reference", "Too short to analyze.")
note("Inbox/Fresh clip.md", "Web Clipping", WORDS)
note("Personal Notes/My thoughts on prices.md", "Personal Note", "Prices and knowledge and planners.",
     description="My notes on prices, markets and knowledge.")
note("Concepts/Regulatory capture.md", "Concept", "When regulators come to serve the regulated industry.",
     aliases=["Agency capture"], description="When regulators come to serve the industry they regulate.")

CALLS = []
ANSWER = {
    "description": "Argues that prices coordinate dispersed knowledge.",
    "in_brief": "An essay arguing that markets use knowledge no planner has.",
    "argues": True,
    "argument": {"thesis": "Central planning fails for want of knowledge", "thesis_cite": "p. 1",
                 "thesis_quote": "Markets coordinate dispersed knowledge through prices that no planner could gather.",
                 "premises": [{"claim": "Knowledge is dispersed", "evidence": ["local circumstances"], "cite": "p. 2",
                               "quote": "the invisible hand of the planner decides everything"}],
                 "objections": [{"objection": "Computers could gather it", "reply": "tacit knowledge can't be sent", "cite": ""}],
                 "conclusion": "Leave coordination to prices"},
    "concepts": [
        {"name": "Agency capture", "new": False, "use": "cited as a risk of planning", "cite": "p. 3"},
        {"name": "Dispersed knowledge", "new": True, "use": "the central idea", "cite": "p. 1",
         "definition": "Knowledge of particular circumstances held by many people. No one holds it all.", "why_new": "not in register"},
        {"name": "Public choice theory", "new": True, "use": "mentioned", "cite": "",
         "definition": "Economics applied to politics.", "why_new": "none"},
        {"name": "Capture of regulators", "new": True, "use": "aside", "cite": "",
         "definition": "Regulators serving firms.", "why_new": "none"}],
    "claims": [{"claim": "Prices summarize information", "cite": "p. 4"}],
    "related": [{"note": "Airbnb regulation", "relation": "applies", "why": "a capture case"},
                {"note": "Not a candidate", "relation": "agrees", "why": "x"},
                {"note": "My thoughts on prices", "relation": "loves", "why": "bad relation"},
                {"note": "My thoughts on prices", "relation": "agrees", "why": "prices and knowledge"}],
}


def fake_ask(instructions, material, cwd, timeout=300, model=None, **kw):
    CALLS.append((instructions[:40], material))
    if instructions.startswith("You analyze one item"):
        ans = json.loads(json.dumps(ANSWER))
        if material.startswith("ITEM: Munger"):
            ans["concepts"].append({"name": "Politics without romance", "new": True, "use": "the talk's frame", "cite": "",
                                    "quote": "", "definition": "Analyzing politicians as self-interested.", "why_new": "x"})
            ans["related"].append({"note": "Hayek on knowledge", "relation": "disagrees", "why": "knowledge vs incentives"})
        if material.startswith("ITEM: Hayek"):
            ans["related"].append({"note": "Munger on public choice", "relation": "disagrees", "why": "knowledge vs incentives"})
        if "PAT'S NOTES" in material:
            ans["your_notes"] = "Pat thinks the capture story is overstated."
        return "Here it is:\n" + json.dumps(ans), {"ms": 5, "cost_usd": 0.12, "usage": {"input_tokens": 9000}, "models": ["test"]}
    return "A test description.", {"ms": 5, "cost_usd": 0.001}


claude_cli.ask = fake_ask
FAILS = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def fm_body(rel):
    sys.path.insert(0, str(C.VL))
    from vaultlib import split_frontmatter
    fm, _, body = split_frontmatter((VAULT / rel).read_text(encoding="utf-8"))
    return fm, body


def run():
    return R.main(["--live", "--routes", "cheap"])


# ---- 1. collect + analyze
run()
store = Q.Store(CFG)
items = [i for i in store.all() if Q.route_of(i) == "analyze"]
paths = sorted(i["source_ref"]["path"] for i in items)
check(paths == ["Resources/Hayek on knowledge.md", "Resources/Munger on public choice.md", "Resources/Short.md"],
      f"collect picks new filed notes of the scope types only (got {paths})")
an = "Resources/Hayek on knowledge - Analysis.md"
check((VAULT / an).exists(), "analysis note written beside the item")
fm, body = fm_body(an)
check(fm.get("type") == "Analysis" and fm.get("status") == "draft" and fm["generated"]["by"] == "claude/claude-code-cli",
      "analysis frontmatter: type, draft, generated")
check(fm.get("analysis_of") == "[[Hayek on knowledge]]" and fm.get("argues") is True, "analysis_of and argues")
check(fm.get("concepts") == ["Regulatory capture"], f"alias maps to the register concept (concepts={fm.get('concepts')})")
check(fm.get("concepts_pending") == ["Dispersed knowledge", "Public choice theory", "Capture of regulators"],
      f"new concepts pending ({fm.get('concepts_pending')})")
check("- [[Regulatory capture]] — cited as a risk of planning (p. 3)" in body, "existing concept linked with use and cite")
check("`Dispersed knowledge` *(new concept, awaiting approval)*" in body, "pending concept shown as plain text")
check("**Thesis:** Central planning fails for want of knowledge (p. 1: " in body and "Objections answered" in body, "argument map")
check("- **applies** — [[Airbnb regulation]]" in body and "- **agrees** — [[My thoughts on prices]]" in body, "related links kept")
check("Not a candidate" not in body and "loves" not in body, "related links outside the candidates or with bad relations dropped")
check("tags" not in fm, "analysis carries no tags (hubs stay clean)")
mat = [m for i, m in CALLS if i.startswith("You analyze one item") and "Munger" in m.split("\n", 1)[0]]
check(mat and "TRANSCRIPT-MARKER" in mat[0], "a video summary's transcript is part of the material")
check(mat and "Regulatory capture (also: Agency capture)" in mat[0] and "Airbnb regulation" in mat[0],
      "register and candidates are in the material")
short = next(i for i in store.all() if i["source_ref"]["path"] == "Resources/Short.md")
check(short["status"] == "skipped", f"too-short note skipped ({short['status']})")
cfm, cbody = fm_body("Concepts/Regulatory capture.md")
check("## Discussed in" in cbody and "[[Hayek on knowledge - Analysis|Hayek on knowledge]] — cited as a risk of planning" in cbody,
      "concept note lists the analysis under Discussed in")
pend = json.loads((TMP / "queue" / "concepts.json").read_text(encoding="utf-8"))
dk = pend["pending"].get("Dispersed knowledge") or {}
check(len(dk.get("analyses") or []) == 2, f"pending concept tracks both analyses ({dk.get('analyses')})")
check((pend["pending"].get("Capture of regulators") or {}).get("merge_into") == "Regulatory capture",
      "a near-duplicate concept is offered as a merge")
n_calls = len([c for c in CALLS if c[0].startswith("You analyze")])
run()
check(len([c for c in CALLS if c[0].startswith("You analyze")]) == n_calls, "a second run spends nothing (nothing new)")

# ---- 2. the 06:00 questions and answers
import organize  # noqa: E402
qs = organize.queue_questions(CFG, print, 1)
cq = {q["concept"]: q for q in qs if q.get("kind") == "concept"}
check(set(cq) == {"Dispersed knowledge", "Public choice theory", "Capture of regulators"},
      f"(B) concepts used by two items are asked; one used once waits ({sorted(cq)})")
check("Politics without romance" in json.loads((TMP / "queue" / "concepts.json").read_text(encoding="utf-8"))["pending"],
      "(B) the single-use concept stays pending")
# (A) quotes
fm0, body0 = fm_body(an)
check("(p. 1: \u201cMarkets coordinate dispersed knowledge through prices that no planner could gather.\u201d)" in body0,
      "(A) a real quote is shown with its cite")
check("Knowledge is dispersed (p. 2) *(unverified)*" in body0 and fm0.get("quotes_unverified", 0) >= 1,
      "(A) an invented quote is marked unverified and counted")
# (C) Munger disagrees with Hayek: Hayek's analysis (Claude's note) hears about it; The owner-owned notes never do
mbody0 = fm_body("Resources/Munger on public choice - Analysis.md")[1]
check(("- [[Munger on public choice]] disagrees with this: knowledge vs incentives" in body0)
      != ("- [[Hayek on knowledge]] disagrees with this: knowledge vs incentives" in mbody0),
      "(C) the older item's analysis (only) gets a later-items line")
check("Later items" not in (VAULT / "Personal Notes/My thoughts on prices.md").read_text(encoding="utf-8"),
      "(C) the owner's own notes are never touched")
check("b" in cq["Capture of regulators"]["options"] and "b" not in cq["Dispersed knowledge"]["options"],
      "merge option only where a close concept exists")
prop = {"questions": [], "new_tags": [], "dups": [], "queue": qs}
ans, mode = organize.parse_answers(f"{cq['Dispersed knowledge']['id']}a {cq['Public choice theory']['id']}c "
                                   f"{cq['Capture of regulators']['id']}b", prop)
lines = organize.apply_queue(CFG, print, prop, ans)
print("   ", lines)
check((VAULT / "Concepts/Dispersed knowledge.md").exists(), "approved concept note created")
dfm, dbody = fm_body("Concepts/Dispersed knowledge.md")
check(dfm.get("type") == "Concept" and dfm.get("description") == "Knowledge of particular circumstances held by many people.",
      "concept note frontmatter (description = first sentence)")
check(dbody.count("- [[") >= 2 and "Munger on public choice - Analysis|" in dbody, "new concept lists both analyses")
check("### Where your sources disagree" in dbody and "disagrees with [[" in dbody, "(D) concept page shows where sources disagree")
fm, body = fm_body(an)
check("- [[Dispersed knowledge]] — the central idea (p. 1)" in body, "approved concept now linked in the analysis")
check("- Public choice theory — mentioned" in body, "rejected concept left as plain text")
check("[[Regulatory capture|Capture of regulators]]" in body, "merged concept links to the existing one")
check(fm.get("concepts") == ["Regulatory capture", "Dispersed knowledge"] and "concepts_pending" not in fm,
      f"frontmatter concepts updated ({fm.get('concepts')}, {fm.get('concepts_pending')})")
rfm, _ = fm_body("Concepts/Regulatory capture.md")
check("Capture of regulators" in rfm.get("aliases", []), "merged name kept as an alias")
pend = json.loads((TMP / "queue" / "concepts.json").read_text(encoding="utf-8"))
check(set(pend["pending"]) == {"Politics without romance"} and pend["rejected"] == ["Public choice theory"],
      "answered concepts leave the pending list; rejection remembered")

# ---- 3. lint: no errors, no dead links
rc, out = C.run(print, C.VL / "okf_lint.py", VAULT, "--no-orphans")
print("   ", out.strip()[-900:])
check(rc == 0, "okf_lint: 0 errors")
bad = [ln for ln in out.splitlines() if "unresolved" in ln.lower() and not ln.split(":")[0].strip().split()[-1].endswith("index.md")]
check(not bad, f"no unresolved links in notes (index warnings come from the partial test vault) {bad}")

# ---- 4. redo, hand-edit guard, force, backup
print("   ", Q.main(["analyze", "Hayek on knowledge"]))
it = [i for i in Q.Store(CFG).all() if i["source_ref"].get("path") == "Resources/Hayek on knowledge.md"]
check(len(it) == 1, "analyze without --redo doesn't queue a note that has an analysis")
Q.main(["analyze", "Hayek on knowledge", "--redo"])
p = VAULT / an
p.write_text(p.read_text(encoding="utf-8").replace("## In brief", "## In brief\n\nOWNER'S OWN LINE"), encoding="utf-8")
run()
held = [i for i in Q.Store(CFG).all() if i["source_ref"].get("mode") == "redo"][0]
check(held["status"] == "held" and "edited by hand" in (held.get("why") or ""), f"hand-edited analysis not replaced ({held['status']})")
check("OWNER'S OWN LINE" in p.read_text(encoding="utf-8"), "the owner's edit untouched")
Q.main(["cancel", held["id"]])
Q.main(["analyze", "Hayek on knowledge", "--redo", "--force"])
run()
check("OWNER'S OWN LINE" not in p.read_text(encoding="utf-8"), "--force replaces it")
_bk = [b.read_text(encoding="utf-8") for b in (VAULT / ".analysis-backups").rglob("*.md")]
check(("disagrees with this" in _bk[0]) == ("disagrees with this" in p.read_text(encoding="utf-8")),
      "(C) a redo keeps the later-items lines")
bk = list((VAULT / ".analysis-backups").rglob("*.md"))
check(len(bk) == 1 and "OWNER'S OWN LINE" in bk[0].read_text(encoding="utf-8"), "old analysis backed up")

# ---- 5. targets and the cost gate
Q.main(["analyze", "tag:economics"])
queued = [i["source_ref"]["path"] for i in Q.Store(CFG).all() if i["status"] == "ready" and Q.route_of(i) == "analyze"]
check(queued == ["Resources/Airbnb regulation.md"], f"tag: target queues only notes without an analysis ({queued})")
CFG["queue"]["confirm_over_usd"] = 0.0001
Q.main(["analyze", "Old article"])
for i in Q.Store(CFG).all():                           # a queued request counts as released; test the gate on a fresh one
    if i["source_ref"].get("path") == "Resources/Old article.md":
        i["manual"]["released"] = False
        Q.Store(CFG).save(i)
run()
st = {i["source_ref"]["path"]: i for i in Q.Store(CFG).all() if Q.route_of(i) == "analyze"}
check(st["Resources/Old article.md"]["status"] == "held" and st["Resources/Old article.md"]["needs"] == "confirm",
      "over confirm_over_usd: held to confirm")
qs = organize.queue_questions(CFG, print, 1)
q = next(q for q in qs if q.get("item") == st["Resources/Old article.md"]["id"])
check(q["text"].startswith("Analyze it?"), "the email asks 'Analyze it?'")
organize.apply_queue(CFG, print, {"queue": [q]}, {q["id"]: "a"})
check(Q.Store(CFG).get(q["item"])["status"] == "ready", "approved: ready for the next run")
CFG["queue"]["confirm_over_usd"] = 0.25

# ---- 5b. (E) weekly concept check -> "Same concept?" -> merge
import yaml  # noqa: E402
(VAULT / "Concepts/Regulatory captures.md").write_text("---\n" + yaml.safe_dump({"type": "Concept", "title": "Regulatory captures",
    "description": "Dup.", "generated": {"by": "tool/test", "at": GEN_AT}}) + "---\n\nDup.\n", encoding="utf-8")
rc, out = C.run(print, C.VL / "concept_lint.py", VAULT, "--pending", TMP / "queue" / "concepts.json", "--json", TMP / "cl.json")
res = json.loads((TMP / "cl.json").read_text(encoding="utf-8"))
check(rc == 0 and [(d["keep"], d["merge"]) for d in res["duplicates"]] == [("Regulatory capture", "Regulatory captures")],
      f"(E) likely duplicate found, keeper = the more-used one ({res['duplicates']})")
check("Regulatory captures" in res["unused"], "(E) unused concept reported")
pend = AN.load_pending(); pend["dup_pairs"] = res["duplicates"]; AN.save_pending(pend)
qs = organize.queue_questions(CFG, print, 1)
dq = [q for q in qs if q.get("kind") == "concept_dup"]
check(len(dq) == 1 and dq[0]["options"]["a"]["action"] == "dup_merge", "(E) 'Same concept?' asked")
print("   ", organize.apply_queue(CFG, print, {"queue": dq}, {dq[0]["id"]: "a"}))
check(not (VAULT / "Concepts/Regulatory captures.md").exists() and (VAULT / "Archives/Regulatory captures.md").exists(),
      "(E) merged concept moved to Archives")
afm, _ = fm_body("Archives/Regulatory captures.md")
check(afm.get("duplicate_of") == "[[Regulatory capture]]" and afm.get("status") == "deprecated", "(E) archived as a duplicate")
rfm, _ = fm_body("Concepts/Regulatory capture.md")
check("Regulatory captures" in rfm.get("aliases", []), "(E) old name kept as an alias")
check(not AN.load_pending().get("dup_pairs"), "(E) question cleared")
rc, out = C.run(print, C.VL / "okf_lint.py", VAULT, "--no-orphans")
check(rc == 0, "okf_lint still 0 errors after A-E")

# ---- 5c. The owner's notes: a "## My notes" section and a linked note of his are part of the material; changes trigger a redo
CFG["analysis"]["notes_settle_minutes"] = 0
hp = VAULT / "Resources/Airbnb regulation.md"
hp.write_text(hp.read_text(encoding="utf-8") + "\n## My notes\n\nOWNER-MARKER I think the capture story is overstated.\n", encoding="utf-8")
note("Personal Notes/Airbnb thoughts.md", "Personal Note", "LINKED-MARKER see [[Airbnb regulation]] for the case.",
     description="My take on the Airbnb case.")
n_before = len(CALLS)
run()
mats = [m for i, m in CALLS[n_before:] if i.startswith("You analyze one item") and m.startswith("ITEM: Airbnb")]
check(mats and "OWNER-MARKER" in mats[-1].split("ITEM TEXT:")[0] and "OWNER-MARKER" not in mats[-1].split("ITEM TEXT:")[1],
      "the owner's '## My notes' go to the model as their notes, not as item text")
check(mats and "LINKED-MARKER" in mats[-1], "a note of his that links to the item is included")
afm, abody = fm_body("Resources/Airbnb regulation - Analysis.md")
check(afm.get("notes_hash") and "## Your notes" in abody and "[[Airbnb thoughts]]" in abody, "analysis has a Your notes section")
n_before = len(CALLS)
run()
check(len(CALLS) == n_before, "no redo while his notes are unchanged")
Q.main(["analyze", "Syncthing like reference"]) if False else None
note("Personal Notes/How-to ref.md", "Reference", WORDS)
rows, _ = AN.collect(CFG, print, Q.Store(CFG), True)
check(not any(r[0] == "How-to ref" for r in rows), "Reference outside Resources isn't analyzed automatically")

# ---- 5d. The owner's properties: my_note and see_also feed the analysis; new items get them, empty and last
hp = VAULT / "Resources/Airbnb regulation.md"
hp.write_text(hp.read_text(encoding="utf-8").replace("---\n", '---\nmy_note: "TAKE-MARKER overstated"\nsee_also: ["[[Old article]]"]\n', 1),
              encoding="utf-8")
n_before = len(CALLS)
run()
mats = [m for i, m in CALLS[n_before:] if i.startswith("You analyze one item") and m.startswith("ITEM: Airbnb")]
check(mats and "TAKE-MARKER" in mats[-1].split("ITEM TEXT:")[0] and "He links this item to: [[Old article]]" in mats[-1],
      "my_note and see_also reach the model as the owner's notes")
check(mats and "\"Old article\" [Article] (owner's see_also)" in mats[-1], "see_also notes are always candidates, marked as his")
_, ab = fm_body("Resources/Airbnb regulation - Analysis.md")
check("[[Old article]]" in ab.split("## Related")[1], "a see_also note always appears under Related")
spec = [{"id": "p1", "type": "Article", "title": "Props test", "description": "A test.", "fields": {}, "resource": None,
         "body": "Some text.", "doug_note": "", "original": None, "original_name": None, "generated_by": "process:file-intake"}]
(TMP / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
rc, out = C.run(print, C.VL / "file_to_note.py", VAULT, TMP / "spec.json")
pt = (VAULT / "Inbox/Props test.md").read_text(encoding="utf-8") if (VAULT / "Inbox/Props test.md").exists() else ""
fml = pt.split("---")[1].strip().splitlines() if pt else []
check(fml[-2:] == ["see_also: []", 'my_note: ""'], f"file_to_note: see_also and my_note last and empty ({fml[-3:]})")
sys.path.insert(0, str(C.UV))
import url_to_note  # noqa: E402
check(url_to_note.owner_props("Web Clipping") == ["see_also: []", 'my_note: ""'] and url_to_note.owner_props("Transcript") == [],
      "url_to_note: added to items, not to transcripts")
sys.path.insert(0, str(C.LIB))
from remember import vault_writer as VWR  # noqa: E402
fv = VWR.frontmatter({"type": "Video Summary", "title": "x", "see_also": ["[[Kept]]"]})
check(fv.strip().splitlines()[-2:] == ['my_note: ""', "---"] and '"[[Kept]]"' in fv, "Remember notes: his see_also kept, my_note added")
check("see_also" not in VWR.frontmatter({"type": "Transcript", "title": "x"}), "transcripts don't get them")
rc, out = C.run(print, C.VL / "okf_lint.py", VAULT, "--paths", "Inbox/Props test.md", "Resources/Airbnb regulation.md")
check(rc == 0, "the new properties pass the OKF check")

# ---- 6. the daily email renders the new sections
import update_email as U  # noqa: E402
Q.Store(CFG)  # fresh
rec = AN.recent(CFG)
check(any(r["title"] == "Hayek on knowledge" and r["related"] for r in rec), "recent analyses for the email")
Q.main(["analyze", "Personal Notes/My thoughts on prices.md"])   # on request: any type
check(any(i["source_ref"]["path"] == "Personal Notes/My thoughts on prices.md" for i in Q.Store(CFG).all()),
      "on request, other note types can be analyzed")
AN.save_pending({"pending": {"Test concept": {"definition": "A test.", "first_from": "X", "analyses": ["X - Analysis", "Y - Analysis"], "merge_into": None,
                                              "added": GEN_AT}}, "rejected": []})
prop = {"questions": [], "new_tags": [], "dups": [], "queue": organize.queue_questions(CFG, print, 1), "items": [],
        "web_token": "t"}
subj, html_, n = U.build_daily(CFG, NOW.strftime("%Y-%m-%d"), prop, [], None)
check("New concept?" in html_ and "New analyses" in html_ and "Hayek on knowledge" in html_, "email shows new concepts and analyses")

shutil.rmtree(TMP, ignore_errors=True)
print("\nALL PASSED" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
