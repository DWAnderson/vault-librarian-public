#!/usr/bin/env python3
"""Regular combining of concepts that substantially overlap (2026-11-13).

Run by Nightly (02:30, under the vault lock) when due: every `analysis.concept_merge.every_days` days (0 = off;
also on the home page). Steps:
  1. candidates  name rules (concept_lint.why_same: same name apart from plural/case, one inside the other, very
                 similar spelling, mostly the same words) and meaning (local embeddings of name + description,
                 cosine >= `similarity`), over approved and pending concepts; pairs the owner said are different skipped
  2. confirm     Claude calls (route concept_merge; Opus since 2026-11-23), `batch_pairs` pairs per call, over the
                 top `max_pairs` meaning pairs: which
                 substantially overlap, and which name to keep. Name-rule pairs need no call.
  3. act         pending + pending  -> combined now (auto_merge_pending): one keeps the other's analyses and its name
                                       as an alias; analysis notes' pending marks are renamed
                 pending + approved -> the pending one is asked about with "Same as the existing concept ..."
                 approved + approved -> a "Same concept?" question in the morning email (as the weekly check did)
State and the last result: queue\\concept-merge.json. Cost: local embeddings are free; the Claude call is ~5-15K
tokens per call (Opus since 2026-11-23; was Sonnet).

    python jobs\\concept_merge.py               candidates and Claude's verdicts, changes nothing
    python jobs\\concept_merge.py --apply       act on them now (takes the vault lock)
    python jobs\\concept_merge.py --no-claude   candidates only, no Claude call
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

DEFAULTS = {"every_days": 7, "every_days_backlog": 3, "similarity": 0.83, "max_pairs": 60, "batch_pairs": 50,
            "auto_merge_pending": True}
STATE = C.APP / "queue" / "concept-merge.json"

PROMPT = """You review pairs of concepts from «owner's» Obsidian vault. «owner_about» Each concept is an idea, \
doctrine, theory, mechanism, method or term of art that several notes can share. For each numbered pair on stdin, \
decide whether the two SUBSTANTIALLY OVERLAP: the same idea under different names, or one is only a restatement or \
trivially narrower form of the other, so that keeping both would split notes that belong together. Ideas that are \
merely related (cause and effect, opposites, a concept and an application of it, two distinct mechanisms in one \
field) are NOT the same.
Return ONLY one JSON object: {"pairs": [{"n": <pair number>, "same": true or false, "keep": "the exact name of the \
two that is the clearer, more general label (only when same)", "why": "a few words"}]} with one entry per pair."""


def mcfg(cfg):
    d = dict(DEFAULTS)
    d.update(((cfg.get("analysis") or {}).get("concept_merge")) or {})
    return d


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def due(cfg):
    m = mcfg(cfg)
    days = int(m.get("every_days") or 0)
    if days <= 0:
        return False
    st = load_state()
    # backlog (2026-11-23): while the last run confirmed pairs or left candidates unchecked, run every
    # every_days_backlog days; once a run finds nothing new, back to every_days
    if st.get("confirmed") or st.get("unchecked"):
        days = min(days, int(m.get("every_days_backlog") or days))
    last = st.get("last_run")
    if not last:
        return True
    try:
        return C.now(cfg) - dt.datetime.fromisoformat(last) >= dt.timedelta(days=days) - dt.timedelta(hours=2)
    except ValueError:
        return True


# ---------------------------------------------------------------- 1. candidates
def _why_same():
    if str(C.VL) not in sys.path:
        sys.path.insert(0, str(C.VL))
    from concept_lint import why_same
    return why_same


def concepts(cfg):
    """[{name, aliases, description, pending, uses}] for approved and pending concepts."""
    vault = Path(cfg["vault_path"])
    notes = A.scan(vault)
    pend = A.load_pending()["pending"]
    out = []
    for c in A.register(vault, notes):
        p = pend.get(c["name"]) if c["pending"] else None
        out.append({**c, "description": (p.get("definition") if p else c["description"]) or "",
                    "uses": len((p or {}).get("analyses") or []) if p else None})
    return out, notes


def candidates(cfg, cons, log=print):
    m = mcfg(cfg)
    apart = {tuple(sorted(x)) for x in A.load_pending().get("not_duplicates") or []}
    why_same = _why_same()
    pairs = {}
    for i, a in enumerate(cons):          # name rules: cheap checks first, spelling ratio only for near-equal lengths
        for b in cons[i + 1:]:
            key = tuple(sorted((a["name"], b["name"])))
            if key in apart:
                continue
            why = why_same(a["name"], b["name"])
            if why:
                pairs[key] = {"a": a, "b": b, "why": why, "source": "name", "score": 1.0}
    try:
        import related
        texts = [f"{c['name']}. {c['description'][:300]}" for c in cons]
        vec = related.embed(cfg, texts)
        sim = vec @ vec.T
        thr = float(m["similarity"])
        import numpy as np
        ii, jj = np.where(np.triu(sim, 1) >= thr)
        for i, j in zip(ii.tolist(), jj.tolist()):
            key = tuple(sorted((cons[i]["name"], cons[j]["name"])))
            if key in apart or key in pairs:
                continue
            pairs[key] = {"a": cons[i], "b": cons[j], "why": f"similar meaning ({sim[i, j]:.2f})", "source": "meaning",
                          "score": float(sim[i, j])}
    except Exception as e:  # noqa: BLE001
        log(f"concept merge: meaning check skipped ({type(e).__name__}: {e})")
    return sorted(pairs.values(), key=lambda p: -p["score"])


# ---------------------------------------------------------------- 2. confirm
def _status(c):
    return "pending" if c["pending"] else "approved"


def confirm(cfg, pairs, log=print):
    """Ask Claude about the meaning pairs (top max_pairs). -> list of confirmed pairs with 'keep'."""
    import claude_cli
    m = mcfg(cfg)
    both_ok = [p for p in pairs if p["source"] == "name" and not p["a"]["pending"] and not p["b"]["pending"]]
    rest = [p for p in pairs if p not in both_ok]
    rest.sort(key=lambda p: (p["source"] != "name", -p["score"]))
    ask = rest[:int(m["max_pairs"])]      # the rest waits for the next run
    out = []
    for p in both_ok:   # approved + approved by name rule: only ever a question for the owner, as the weekly check did
        a, b = p["a"], p["b"]
        keep = a if len(a["name"]) <= len(b["name"]) else b
        out.append({**p, "keep": keep["name"], "merge": (b if keep is a else a)["name"]})
    if not ask:
        return out, None
    work = Path(cfg["local_root"]) / "work"
    errs = []
    size = max(1, int(m.get("batch_pairs") or 50))   # several calls of batch_pairs each (2026-11-23: up to 150 pairs)
    for b0 in range(0, len(ask), size):
        chunk = ask[b0:b0 + size]
        lines = []
        for i, p in enumerate(chunk, 1):
            lines.append(f"PAIR {i}:\n  A: {p['a']['name']} [{_status(p['a'])}] - {p['a']['description'][:300]}\n"
                         f"  B: {p['b']['name']} [{_status(p['b'])}] - {p['b']['description'][:300]}")
        text, info = claude_cli.ask(PROMPT, "\n\n".join(lines), work, timeout=900, route="concept_merge")
        if not text:
            errs.append(f"Claude call failed: {info.get('error')}")
            break                                         # the rest waits for the next run
        mm = re.search(r"\{.*\}", text, re.S)
        try:
            verdicts = json.loads(mm.group(0)).get("pairs") or []
        except Exception:  # noqa: BLE001
            errs.append("Claude's answer was not the JSON asked for")
            continue
        for v in verdicts:
            try:
                p = chunk[int(v.get("n")) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            if not v.get("same"):
                continue
            names = {p["a"]["name"], p["b"]["name"]}
            keep = v.get("keep") if v.get("keep") in names else p["a"]["name"]
            out.append({**p, "keep": keep, "merge": (names - {keep}).pop(),
                        "why": f"{p['why']}; {v.get('why') or ''}".strip("; ")})
    return out, "; ".join(errs) or None


# ---------------------------------------------------------------- 3. act
def merge_pending(cfg, notes, pend, keep, merge):
    """Combine pending concept `merge` into pending `keep`: analyses, aliases, the marks in analysis notes."""
    pk, pm = pend["pending"][keep], pend["pending"].pop(merge)
    for an in pm.get("analyses") or []:
        if an not in pk.setdefault("analyses", []):
            pk["analyses"].append(an)
    al = pk.setdefault("aliases", [])
    for x in [merge] + (pm.get("aliases") or []):
        if x not in al and x != keep:
            al.append(x)
    old, new = f"`{merge}`{A.PENDING_MARK}", f"`{keep}`{A.PENDING_MARK}"

    def fn(fm, body):
        body = body.replace(old, new)
        cp = [keep if x == merge else x for x in (fm.get("concepts_pending") or [])]
        fm["concepts_pending"] = list(dict.fromkeys(cp))
        return fm, body
    A._sub_in_analyses(Path(cfg["vault_path"]), notes, pm.get("analyses") or [], fn)


def act(cfg, log, found, notes, dry):
    m = mcfg(cfg)
    pend = A.load_pending()
    gone, done, asked, dups = {}, [], [], []

    def cur(n):
        while n in gone:
            n = gone[n]
        return n
    have = {(d["keep"], d["merge"]) for d in pend.get("dup_pairs") or []}
    for f in found:
        k, mg = cur(f["keep"]), cur(f["merge"])
        if k == mg:
            continue
        kp, mp = k in pend["pending"], mg in pend["pending"]
        if kp and mp:
            if m.get("auto_merge_pending"):
                if not dry:
                    merge_pending(cfg, notes, pend, k, mg)
                gone[mg] = k
                done.append(f"{mg} -> {k}")
            else:
                asked.append(f"{mg} = {k}? (not combined: automatic combining is off)")
        elif kp != mp:                       # one pending, one approved: ask with the merge option
            pn, an = (k, mg) if kp else (mg, k)
            if not dry:
                pend["pending"][pn]["merge_into"] = an
                pend["pending"][pn]["ask"] = True
            asked.append(f"{pn} -> {an}?")
        else:                                # both approved: "Same concept?" in the morning email
            if (k, mg) not in have and (mg, k) not in have:
                dups.append({"keep": k, "merge": mg, "why": f["why"]})
    if not dry:
        pend.setdefault("dup_pairs", []).extend(dups)
        A.save_pending(pend)
    return done, asked, dups


def run(cfg, log=print, dry=True, use_claude=True):
    cons, notes = concepts(cfg)
    pairs = candidates(cfg, cons, log)
    if use_claude:
        found, err = confirm(cfg, pairs, log)
    else:
        found, err = [], "no Claude check (--no-claude): candidates only"
    done, asked, dups = act(cfg, log, found, notes, dry)
    st = load_state()
    summary = (f"{len(cons)} concepts, {len(pairs)} candidate pairs, {len(found)} confirmed: "
               f"{len(done)} pending combined, {len(asked)} pending asked about, {len(dups)} 'Same concept?' questions"
               + (f"; {err}" if err else "") + (" (dry run)" if dry else ""))
    if not dry:
        meaning = [p for p in pairs if not (p["source"] == "name" and not p["a"]["pending"] and not p["b"]["pending"])]
        st.update({"confirmed": len(found), "unchecked": max(0, len(meaning) - int(mcfg(cfg)["max_pairs"]))})
        st.update({"last_run": C.now(cfg).isoformat(timespec="seconds"), "last": summary,
                   "combined": done, "asked": asked, "dup_pairs": [f"{d['merge']} -> {d['keep']}" for d in dups]})
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    log("concept merge: " + summary)
    return summary, pairs, found, done, asked, dups


def run_if_due(cfg, log, dry):
    """Nightly hook (vault lock held by the caller). -> report line, or None when not due."""
    if not due(cfg):
        return None
    summary, *_ = run(cfg, log, dry=dry)
    if not dry:
        A.refresh_after_concepts(cfg, log)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-claude", action="store_true")
    ap.add_argument("--show", type=int, default=40, help="candidate pairs to print")
    a = ap.parse_args()
    cfg = C.load_config()
    lock = None
    if a.apply:
        lock = C.VaultLock(cfg, "concept merge")
        ok, note = lock.acquire()
        if not ok:
            print(f"vault is locked: {note}")
            return 1
    try:
        summary, pairs, found, done, asked, dups = run(cfg, print, dry=not a.apply, use_claude=not a.no_claude)
        print(f"\nTop candidate pairs ({len(pairs)}):")
        for p in pairs[:a.show]:
            print(f"  {p['score']:.2f} {p['source']:7} {p['a']['name']} [{_status(p['a'])}] ~ {p['b']['name']} [{_status(p['b'])}]")
        for title, xs in (("Pending combined", done), ("Pending asked about", asked),
                          ("Same concept? questions", [f"{d['merge']} -> {d['keep']}" for d in dups])):
            print(f"\n{title} ({len(xs)}):" + "".join(f"\n  {x}" for x in xs[:60]))
    finally:
        if lock:
            lock.release()
            if a.apply:
                A.refresh_after_concepts(cfg, print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
