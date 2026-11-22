"""The owner's corrections to names and filing (2026-10-10). Two ways in, both handled each night:

1. He renames or moves a note himself in Obsidian (Obsidian updates the links). detect() compares the vault with
   the state saved at the end of the previous run, matches notes by their text, and skips changes Claude made
   (queue/renames.jsonl from vault_names, the `a` -> `b` moves in log.md from apply_plan). For each of his:
     - a move: the new folder's tag is added (old tags kept; no tag for "Other" folders), and the move is written
       to queue/filing-choices.jsonl as an override, so organize's filing suggestions learn from it;
     - a rename: the old name goes into `aliases`, any [[old]] links left behind are rewritten, and the rename is
       written to queue/naming-choices.jsonl, which organize's prompt uses as naming examples;
     - when two or more of his moves send the same kind of note (shared tag or type) from one folder to another,
       the morning email suggests making it a standing rule.
2. He describes a change in a `fix:` property ("file under Economics", "title should be X (Hayek 1973)").
   fixes() sends each one to Claude (one short call), applies the answer (rename with links rewritten, move with
   folder tag, approved properties), removes `fix`, and logs it; if it can't be done safely, `fix` stays and
   `fix_status` says why. Notes in Inbox/ and Clippings/ wait until they are filed.

save_state() runs at the very end of nightly, after Claude's own renames and moves.
"""
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import vault_names as VN  # noqa: E402

sys.path.insert(0, str(C.VL))
from vaultlib import RESERVED  # noqa: E402

NAMES = C.APP / "queue" / "naming-choices.jsonl"
CHOICES = C.APP / "queue" / "filing-choices.jsonl"
WAIT_TOP = {"Inbox", "Clippings"}
NOT_BY_HAND_TOP = {"_Obsidian Internal", "Claude"}   # archives, hubs, reports: moved by jobs, not by hand
MAX_FIXES = 20


def state_path(cfg):
    return Path(cfg["local_root"]) / "queue" / "vault-state.json"


def _key(body):
    t = re.sub(r"\s+", " ", re.sub(r"\[\[[^\]]*\]\]", "", body)).strip()[:20000]   # links may change on rename
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16] if len(t) > 40 else None


def scan(vault):
    out = {}
    for rel, p in VN._walk(vault):
        if p.name in RESERVED:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm, end = VN._fm(text)
        body = text[end + 4:] if end else text
        fm = fm or {}
        tags = fm.get("tags") or []
        out[rel] = {"k": _key(body), "type": str(fm.get("type") or ""), "title": str(fm.get("title") or ""),
                    "res": str(fm.get("resource") or ""),
                    "tags": [str(x) for x in (tags if isinstance(tags, list) else [tags])],
                    "description": str(fm.get("description") or "")[:200]}
    return out


def save_state(cfg, log=print):
    sp = state_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"at": C.now(cfg).isoformat(timespec="seconds"), "notes": scan(Path(cfg["vault_path"]))},
                             ensure_ascii=False), encoding="utf-8")


def _claude_moves(vault):
    """(old_rel, new_rel) pairs Claude made: vault_names ledger + apply_plan lines in log.md."""
    pairs, olds = set(), set()
    try:
        for ln in VN.ledger_path().read_text(encoding="utf-8").splitlines():
            if ln.strip():
                x = json.loads(ln)
                pairs.add((x["old"], x["new"]))
                olds.add(x["old"])
    except (OSError, ValueError):
        pass
    try:
        for m in re.finditer(r"`([^`]+\.md)` → `([^`]+\.md)`", (Path(vault) / "log.md").read_text(encoding="utf-8")):
            pairs.add((m.group(1), m.group(2)))
            olds.add(m.group(1))
    except OSError:
        pass
    return pairs, olds


def _folder(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _stem(rel):
    return rel.rsplit("/", 1)[-1][:-3]


def detect(cfg, log, dry=False):
    """The owner's own renames/moves since the last run -> (report lines, needs lines)."""
    vault = Path(cfg["vault_path"])
    try:
        prev = json.loads(state_path(cfg).read_text(encoding="utf-8"))["notes"]
    except (OSError, ValueError, KeyError):
        if not dry:
            save_state(cfg, log)
        return ["first run: vault state saved; corrections are noticed from tomorrow"], []
    cur = scan(vault)
    gone = {r: v for r, v in prev.items() if r not in cur}
    new = {r: v for r, v in cur.items() if r not in prev}
    if not gone or not new:
        return [], []
    by_key = defaultdict(list)
    for r, v in new.items():
        if v["k"]:
            by_key[v["k"]].append(r)
    by_id = defaultdict(list)
    for r, v in new.items():
        if v["title"]:
            by_id[(v["type"], v["title"], v["res"])].append(r)
    claude_pairs, claude_olds = _claude_moves(vault)
    matches = []
    for r, v in gone.items():
        cands = by_key.get(v["k"]) if v["k"] else None
        if not cands and v["title"]:
            cands = by_id.get((v["type"], v["title"], v["res"]))
        if cands and len(cands) == 1:
            matches.append((r, cands[0], cur[cands[0]]))
    lines, needs, plan_items, name_rows, choice_rows, link_moves = [], [], [], [], [], []
    now = C.now(cfg).isoformat(timespec="seconds")
    sys.path.insert(0, str(C.VL))
    from apply_plan import folder_tag
    for old, newr, v in matches:
        if (old, newr) in claude_pairs or old in claude_olds or VN.follow(old) == newr:
            continue
        if old.split("/")[0] in WAIT_TOP | NOT_BY_HAND_TOP or newr.split("/")[0] in NOT_BY_HAND_TOP:
            continue                              # filing out of Inbox, archiving: done by jobs
        moved, renamed = _folder(old) != _folder(newr), _stem(old) != _stem(newr)
        sets = {}
        if moved:
            ft = folder_tag(_folder(newr))
            if ft and ft not in v["tags"]:
                sets["tags"] = v["tags"] + [ft]
            choice_rows.append({"at": now, "title": _stem(newr), "type": v["type"], "tags": v["tags"],
                                "description": v["description"], "suggested": _folder(old), "chosen": _folder(newr),
                                "how": "moved by you in Obsidian"})
            lines.append(f"moved by you: {_stem(newr)}: {_folder(old) or '(root)'} -> {_folder(newr) or '(root)'}"
                         + (f" (tag {ft} added)" if "tags" in sets else ""))
        if renamed:
            name_rows.append({"at": now, "before": _stem(old), "after": _stem(newr), "type": v["type"],
                              "title": v["title"], "how": "renamed by you in Obsidian"})
            link_moves.append((_stem(old), _stem(newr)))
            lines.append(f"renamed by you: {_stem(old)} -> {_stem(newr)}")
        if sets:
            plan_items.append({"path": newr, "set": sets})
    if dry:
        return [("would note " + x) for x in lines], []
    if link_moves:                                # renamed outside Obsidian: links still say the old name
        mapping = {o.lower(): n for o, n in link_moves}
        for rel, p in VN._walk(vault):
            t = p.read_text(encoding="utf-8", errors="replace")
            if "[[" in t:
                nt = VN.rewrite_links(t, mapping)
                if nt != t:
                    p.write_text(nt, encoding="utf-8", newline="\n")
        for o, n in link_moves:                   # old name kept as an alias
            hit = next((r for r in cur if _stem(r) == n), None)
            if hit:
                p = vault / hit
                t = p.read_text(encoding="utf-8")
                t2 = VN._add_alias(t, o)
                if t2 != t:
                    p.write_text(t2, encoding="utf-8", newline="\n")
    if plan_items:
        _apply(cfg, log, plan_items, "Your corrections in Obsidian: folder tags")
    for path, rows in ((CHOICES, choice_rows), (NAMES, name_rows)):
        if rows:
            with path.open("a", encoding="utf-8") as fh:
                fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    needs += rule_suggestions()
    return lines, needs


def rule_suggestions():
    """Two or more of the owner's own moves of the same kind of note between the same folders -> suggest a rule."""
    try:
        rows = [json.loads(x) for x in CHOICES.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    rows = [r for r in rows if str(r.get("how") or "").startswith("moved by ") and str(r.get("how")).endswith(" in Obsidian")]
    try:
        done = set(json.loads((C.APP / "queue" / "rules-suggested.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        done = set()
    cnt = Counter()
    for r in rows:
        for t in (r.get("tags") or []) + ([f"type:{r['type']}"] if r.get("type") else []):
            cnt[(t, r.get("suggested") or "", r.get("chosen") or "")] += 1
    out = []
    for (t, a, b), n in cnt.most_common():
        k = f"{t}|{a}|{b}"
        if n >= 2 and k not in done and a != b:
            what = f"{t[5:]} notes" if t.startswith("type:") else f"notes tagged {t}"
            out.append(f"You moved {n} {what} from {a or 'the vault root'} to {b}. Filing suggestions already learn "
                       f"from this; to make it a standing rule, tell Claude \"always file {what} in {b}\".")
            done.add(k)
    if out:
        (C.APP / "queue" / "rules-suggested.json").write_text(json.dumps(sorted(done)), encoding="utf-8")
    return out[:5]


def name_examples(n=30):
    """The owner's recent renames, for naming prompts."""
    try:
        rows = [json.loads(x) for x in NAMES.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    return [{k: r.get(k) for k in ("before", "after", "type")} for r in rows[-n:]]


def _apply(cfg, log, items, title, dry=False):
    work = Path(cfg["local_root"]) / "work"
    work.mkdir(parents=True, exist_ok=True)
    pj = work / f"corrections-{dt.datetime.now():%H%M%S%f}.json"
    pj.write_text(json.dumps({"actor": "claude/corrections", "log": title, "items": items}, ensure_ascii=False),
                  encoding="utf-8")
    rc, out = C.run(log, C.VL / "apply_plan.py", cfg["vault_path"], pj, *(["--dry-run"] if dry else []))
    return rc, out


# ------------------------------------------------------------------ fix: property
FIX_PROMPT = """«Owner» left a `fix` request on a note in their Obsidian vault. Work out exactly what to change.
stdin JSON: note (path, name, frontmatter, start of text), fix (the request), folders (where notes may go),
tags (the ONLY approved tags), name_rules, past_renames (the owner's own earlier renames, to imitate).
name_rules: a note that summarizes a work (type Book/Video/Podcast/Web Summary, or content: summary) is named
"<work> (Summary)"; analyses "<work> (Analysis)", transcripts "<work> (Transcript)"; <work> is
"Title (Author year)" or "Title (Channel)" when the source is known; title case; no characters \\ / : * ? " < > | # ^ [ ].
The `title` property is the work's own title (no suffix).
You may: rename (rename_to: new note name, no .md), move (move_to: one of folders), set properties (title,
description, tags from the approved list only, type, author, published, aliases, see_also, my_note), unset
properties. Do only what was asked. If the request needs anything else (re-summarizing, re-OCR, deleting,
splitting, a new tag or folder that isn't approved, or you are unsure), do nothing and say why.
Output ONLY JSON: {"rename_to": null, "move_to": null, "set": {}, "unset": [], "done": "one sentence for «owner»
saying what was changed", "cannot": null or "one sentence: what they need to do or decide",
"rule": null or "a standing rule this suggests, in one sentence"}"""
SETTABLE = {"title", "description", "tags", "type", "author", "published", "aliases", "see_also", "my_note"}


def fixes(cfg, log, dry=False):
    """Notes with a `fix` property -> (report lines, needs lines)."""
    vault = Path(cfg["vault_path"])
    todo = []
    for rel, p in VN._walk(vault):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\nfix:" not in text[:8000]:
            continue
        fm, end = VN._fm(text)
        if not fm or not str(fm.get("fix") or "").strip():
            continue
        todo.append((rel, p, fm, text[end + 4:] if end else ""))
    if not todo:
        return [], []
    lines, needs = [], []
    waiting = [r for r, *_ in todo if r.split("/")[0] in WAIT_TOP]
    if waiting:
        lines.append(f"{len(waiting)} fix request(s) on notes in Inbox/Clippings wait until they are filed")
    todo = [t for t in todo if t[0].split("/")[0] not in WAIT_TOP][:MAX_FIXES]
    if dry:
        return lines + [f"would handle fix on {r}: {fm['fix']}" for r, _, fm, _ in todo], []
    import llm
    import organize
    fset, tset = organize.folders(vault), set(organize.tags(vault))
    for rel, p, fm, body in todo:
        fix = str(fm["fix"]).strip()
        rid = "fix-" + hashlib.sha1(f"{rel}|{fix}".encode("utf-8")).hexdigest()[:12]
        small = {k: v for k, v in fm.items() if k not in ("concepts", "concepts_pending", "generated")}
        mat = json.dumps({"note": {"path": rel, "name": p.stem, "frontmatter": small, "text": body.strip()[:1500]},
                          "fix": fix, "folders": fset, "tags": sorted(tset), "past_renames": name_examples()},
                         ensure_ascii=False, default=str)
        ans = llm.run(cfg, "fix", [{"id": rid, "instructions": FIX_PROMPT, "material": mat, "timeout": 180}],
                      cache_dir=Path(cfg["local_root"]) / "work" / "fix-answers", log=log, item=rid)
        a = ans.get(rid) or {}
        m = re.search(r"\{.*\}", a.get("text") or "", re.S)
        try:
            j = json.loads(m.group(0)) if m else None
        except ValueError:
            j = None
        if not j:
            needs.append(f"Couldn't work out the fix on {p.stem} (\"{fix}\"): {a.get('error') or 'no usable answer'}")
            continue
        if j.get("cannot"):
            _apply(cfg, log, [{"path": rel, "set": {"fix_status": f"{C.now(cfg):%Y-%m-%d}: {j['cannot']}"}}],
                   "Fix requests (needs you)")
            needs.append(f"Fix on {p.stem} (\"{fix}\") needs you: {j['cannot']}")
            continue
        sets = {k: v for k, v in (j.get("set") or {}).items() if k in SETTABLE}
        if "tags" in sets:
            bad = [t for t in (sets["tags"] or []) if t not in tset]
            sets["tags"] = [t for t in (sets["tags"] or []) if t in tset]
            if bad:
                needs.append(f"Fix on {p.stem}: tag(s) {', '.join(bad)} aren't approved, so they weren't added")
        move = j.get("move_to") if j.get("move_to") in fset else None
        item = {"path": rel, "set": sets, "unset": ["fix", "fix_status"] + [k for k in (j.get("unset") or []) if k in SETTABLE]}
        if move and move != _folder(rel):
            item["move_to"] = move
        rc, out = _apply(cfg, log, [item], f"Fix requests: {p.stem}")
        if rc:
            needs.append(f"Fix on {p.stem} failed while applying: {out.strip()[-200:]}")
            continue
        now_rel = f"{move}/{p.name}" if item.get("move_to") else rel
        new_name = VN._clean(j.get("rename_to") or "")
        if new_name and new_name != p.stem:
            new_rel = (_folder(now_rel) + "/" if _folder(now_rel) else "") + new_name + ".md"
            if (vault / new_rel).exists():
                needs.append(f"Fix on {p.stem}: a note named \"{new_name}\" already exists, so it wasn't renamed")
            else:
                VN.rename_notes(cfg, vault, [(now_rel, new_rel, p.stem, new_name)])
                with NAMES.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"at": C.now(cfg).isoformat(timespec="seconds"), "before": p.stem,
                                         "after": new_name, "type": str(fm.get("type") or ""),
                                         "title": str(fm.get("title") or ""), "how": f"fix: {fix}"},
                                        ensure_ascii=False) + "\n")
        if item.get("move_to"):
            with CHOICES.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": C.now(cfg).isoformat(timespec="seconds"), "title": new_name or p.stem,
                                     "type": str(fm.get("type") or ""), "tags": fm.get("tags"),
                                     "description": str(fm.get("description") or "")[:200], "suggested": _folder(rel),
                                     "chosen": move, "how": "moved by your fix request"}, ensure_ascii=False) + "\n")
        lines.append(f"fixed {p.stem}: {j.get('done') or fix}")
        if j.get("rule"):
            needs.append(f"From your fix on {new_name or p.stem}: {j['rule']} Tell Claude if you want this as a standing rule.")
    return lines, needs


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--save-state", action="store_true")
    x = ap.parse_args()
    cfg = C.load_config()
    if x.save_state:
        save_state(cfg)
    else:
        for f in (detect, fixes):
            ls, ns = f(cfg, print, x.dry_run)
            print("\n".join(ls + ["NEEDS " + n for n in ns]))
