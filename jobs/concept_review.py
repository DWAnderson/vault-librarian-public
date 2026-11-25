#!/usr/bin/env python3
"""Keep-or-drop review of concepts only one item uses (2026-11-23, the owner).

A new concept is asked about once a second item uses it (analysis.ask_after_uses; a book's chapter analyses
count as one item since 2026-11-23). Most never get a second
use: 559 of 855 pending concepts on 2026-11-23. They are never asked about and pile up. Dropping them all by age
would lose the novel ones, so each night Claude (route concept_review, pinned to Opus) judges a batch on one
question, the owner's (2026-11-23): is this an interesting idea, or an interesting take on an idea, that is not
common knowledge? It judges the idea itself, not how it compares with the existing concepts (the concept merge
does that).

  keep   interesting and not common knowledge: worth a concept note even though one item uses it so far
  drop   common knowledge, a plain fact or description of the one item, or not interesting: plain text

An experiment: if the suggestions don't prove useful, turn it off (analysis.concept_review.enabled).

Nothing changes on Claude's word. Each verdict becomes a "Keep this concept?" question in the morning email, with
Claude's suggestion as option a. At least `ask_per_day` of them are asked each day, apart from the other
questions' 15-question cap, until the backlog is gone.

Run by Nightly (under the vault lock) after the concept merge. Settings: config analysis.concept_review.

    python jobs\\concept_review.py            show what tonight's batch would be (no Claude call)
    python jobs\\concept_review.py --run      judge one batch now (one Claude call)
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import analyze as A  # noqa: E402

DEFAULTS = {"enabled": True, "min_age_days": 30, "per_night": 20, "ask_per_day": 10, "backlog_max": 30}

PROMPT = """You help «owner» decide which ideas in «owner's» Obsidian vault deserve a note of their own. \
«owner_about» Each concept on stdin was proposed by the analysis of one item (an article, book, video or podcast) \
and no other item has used it since. Judge each one on its own merits, as a well-read, curious generalist would: \
is it an INTERESTING IDEA, or an INTERESTING TAKE on an idea, that is NOT COMMON KNOWLEDGE?
- "keep": yes. A surprising argument, a non-obvious mechanism, an unusual framing or distinction, a little-known \
finding or term worth remembering, or a fresh angle on a familiar idea.
- "drop": no. Common knowledge or a textbook commonplace an educated reader already knows, a plain fact or \
description of the one item, a label for something obvious, or simply not interesting.
Don't keep an idea only because it is correct, important or famous; keep it because it is worth being reminded of. \
Use the definition and the line from the analysis to see what the item actually said.
Return ONLY one JSON object: {"concepts": [{"n": <number>, "verdict": "keep" | "drop", "why": "one short sentence \
saying what is (or isn't) interesting about it"}]} with one entry per concept."""


def rcfg(cfg):
    d = dict(DEFAULTS)
    d.update(((cfg.get("analysis") or {}).get("concept_review")) or {})
    return d


def _single_use(cfg, p):
    return A.concept_items(p) < int(A.acfg(cfg)["ask_after_uses"]) and not p.get("ask")


def waiting(cfg, pend=None):
    """Reviewed, unanswered one-item concepts, oldest first: [(name, p)]."""
    pend = pend or A.load_pending()
    return sorted(((n, p) for n, p in pend["pending"].items()
                   if _single_use(cfg, p) and (p.get("review") or {}).get("verdict")
                   and (p.get("review") or {}).get("answer") != "wait"),
                  key=lambda kv: kv[1].get("added") or "")


def batch(cfg, pend=None):
    """Tonight's concepts to judge: one-item concepts at least min_age_days old, never reviewed, oldest first."""
    r = rcfg(cfg)
    pend = pend or A.load_pending()
    cut = (C.now(cfg) - dt.timedelta(days=int(r["min_age_days"]))).isoformat(timespec="seconds")
    todo = sorted(((n, p) for n, p in pend["pending"].items()
                   if _single_use(cfg, p) and not p.get("review") and (p.get("added") or "") <= cut),
                  key=lambda kv: kv[1].get("added") or "")
    room = max(0, int(r["backlog_max"]) - len(waiting(cfg, pend)))
    return todo[:min(int(r["per_night"]), room)], len(todo)


def _usage_line(vault, notes, analysis, name):
    """The line of the analysis note that uses the concept (what the item said about it)."""
    n = notes.get(analysis)
    if not n:
        return ""
    try:
        for ln in (vault / n["rel"]).read_text(encoding="utf-8").splitlines():
            if f"`{name}`" in ln:
                return re.sub(r"\s+", " ", ln.replace(A.PENDING_MARK, "")).strip()[:400]
    except OSError:
        pass
    return ""


def run(cfg, log=print, dry=False):
    """Judge one batch. Returns a one-line summary, or None when there is nothing to do."""
    r = rcfg(cfg)
    if not r.get("enabled"):
        return None
    pend = A.load_pending()
    todo, total = batch(cfg, pend)
    if not todo:
        return None if not total else f"{total} one-item concept(s) waiting; {len(waiting(cfg, pend))} reviews still unanswered, none added tonight"
    if dry:
        return f"would review {len(todo)} of {total} one-item concept(s): " + ", ".join(n for n, _ in todo[:10])
    vault = Path(cfg["vault_path"])
    notes = A.scan(vault)
    lines = ["PROPOSED CONCEPTS TO JUDGE:"]
    for i, (name, p) in enumerate(todo, 1):
        an = (p.get("analyses") or [""])[0]
        lines.append(f"{i}. {name}\n   definition: {p.get('definition') or '(none)'}\n   proposed in: {p.get('first_from') or an}"
                     + (f"\n   used there as: {_usage_line(vault, notes, an, name)}" if an else ""))
    import claude_cli
    work = Path(cfg["local_root"]) / "work"
    text, info = claude_cli.ask(PROMPT, "\n".join(lines), work, timeout=900, route="concept_review")
    if not text:
        return f"concept review: Claude call failed ({info.get('error')}); tried again tomorrow night"
    m = re.search(r"\{.*\}", text, re.S)
    try:
        verdicts = json.loads(m.group(0)).get("concepts") or []
    except Exception:  # noqa: BLE001
        return "concept review: the answer was not the JSON asked for; tried again tomorrow night"
    model = ",".join(info.get("models") or []) or claude_cli.model_for("concept_review", cfg) or "?"
    at = C.now(cfg).isoformat(timespec="seconds")
    pend = A.load_pending()           # re-read: nothing else should have changed it, but never write over a change
    counts = {"keep": 0, "drop": 0}
    for v in verdicts:
        try:
            name = todo[int(v.get("n")) - 1][0]
        except (TypeError, ValueError, IndexError):
            continue
        p = pend["pending"].get(name)
        verdict = str(v.get("verdict") or "").lower()
        if not p or verdict not in counts:
            continue
        p["review"] = {"verdict": verdict, "why": str(v.get("why") or "")[:300], "by": model, "at": at}
        counts[verdict] += 1
    A.save_pending(pend)
    n = sum(counts.values())
    return (f"reviewed {n} one-item concept(s) ({counts['keep']} keep, {counts['drop']} drop suggested); "
            f"{max(0, total - n)} left to review")


def run_if_due(cfg, log=print, dry=False):
    line = run(cfg, log, dry)
    if line:
        log("concept review: " + line)
    return line


def questions(cfg, first_id):
    """Morning "Keep this concept?" questions, Claude's suggestion as option a (organize.queue_questions)."""
    r = rcfg(cfg)
    if not r.get("enabled"):
        return []
    today = C.now(cfg).strftime("%Y-%m-%d")
    later = int(A.acfg(cfg).get("later_days") or 7)
    out, i = [], first_id
    for name, p in waiting(cfg):
        if (p.get("snooze_until") or "") > today:
            continue
        rv = p["review"]
        add = {"label": "Keep it: add it as a concept note", "action": "concept_add"}
        drop = {"label": "Drop it: keep it as plain text (not proposed again)", "action": "concept_reject"}
        wait = {"label": "Leave it pending until another item uses it", "action": "concept_wait"}
        # four options at most: the answer page and email replies take letters a-d
        opts = [drop, add, wait] if rv["verdict"] == "drop" else [add, drop, wait]
        opts.append({"label": f"Not now (ask again in {later} days)", "action": "concept_later"})
        word = {"keep": "keep it (interesting, not common knowledge)",
                "drop": "drop it (common knowledge or not interesting)"}.get(rv["verdict"], rv["verdict"])
        out.append({"id": i, "kind": "concept_review", "item": f"concept:{name}", "title": name, "concept": name,
                    "text": (p.get("definition") or "(no definition)") + f"  Claude suggests: {word}. {rv.get('why') or ''}".rstrip(),
                    "where": f"Linked to 1 item, \"{p.get('first_from')}\""
                             + (f" ({len(p.get('analyses') or [])} chapter analyses)" if len(p.get("analyses") or []) > 1 else "")
                             + f"; proposed {(p.get('added') or '?')[:10]}",
                    "size": "", "why": rv.get("why") or "", "options": dict(zip("abcdef", opts))})
        i += 1
        if len(out) >= int(r["ask_per_day"]):
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="judge one batch now (one Claude call)")
    a = ap.parse_args()
    cfg = C.load_config()
    log = C.Log(cfg, "concept-review")
    print(run(cfg, log, dry=not a.run) or "nothing to review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
