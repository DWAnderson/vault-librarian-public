#!/usr/bin/env python3
"""Compare Related candidates: the retired note-level index (related.py, embeddings.npz) against the passage index
(vault_search.related, with the owner's my_note). For the most recently analyzed items, lists each method's top 8 and
marks the notes the analysis actually chose as Related. No Claude usage; read-only.

  python related_compare.py [--n 15]        -> reports\\related-compare-<date>.md

Plan: 2026-10-08 Vault search - plan.md, phase 4 (2026-10-12).
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import vault_search as VS  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=15)
    a = ap.parse_args(argv)
    cfg = C.load_config()
    import analyze
    import related
    vault = Path(cfg["vault_path"])
    notes = analyze.scan(vault)
    con = VS.connect(cfg)
    rows = con.execute("SELECT path, analysis_of FROM notes WHERE type='Analysis' AND analysis_of != '' "
                       "ORDER BY mtime DESC LIMIT ?", (a.n,)).fetchall()
    out = [f"# Related candidates: note index vs passage index ({C.now(cfg):%Y-%m-%d %H:%M})", "",
           "For each recently analyzed item: the top 8 candidates from each method. **Bold** = chosen as Related by "
           "the analysis (which saw the old method's candidates, so the count favours it). Only notes an analysis "
           "can offer are listed. Both lists also merge with the word-overlap list in the real run.", ""]
    hits = {"old": 0, "new": 0}
    for apath, item in rows:
        n = notes.get(item)
        if not n:
            continue
        text = (vault / apath).read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(?ms)^## Related\s*$(.*?)(?=^## |\Z)", text)
        chosen = set(re.findall(r"\[\[([^\]|#]+)", m.group(1))) if m else set()
        try:
            _fm, body = analyze._body(vault, n["rel"])
        except OSError:
            body = ""
        old = related.neighbours(cfg, n["name"], related.note_text(n["title"], n["description"], n["tags"], body), 40)
        byrel = {x["rel"]: x["name"] for x in notes.values()}
        pool = {m["rel"] for m in notes.values() if m["type"] not in analyze.NOT_RELATED_TYPES
                and m["top"] not in ("Claude", "Archives", "Templates", "attachments", "_Obsidian Internal")
                and not analyze.VN.is_derived(m["name"]) and m["name"] != n["name"]}
        old = [x for x in old if notes.get(x) and notes[x]["rel"] in pool][:8]
        new = [byrel.get(p, Path(p).stem) for p, _s in VS.related(cfg, n["rel"], 40, con=con) if p in pool][:8]
        hits["old"] += len(chosen & set(old))
        hits["new"] += len(chosen & set(new))
        fmt = lambda xs: "; ".join(f"**{x}**" if x in chosen else x for x in xs) or "(none)"  # noqa: E731
        out += [f"## {item}", "", f"- note index: {fmt(old)}", f"- passage index: {fmt(new)}", ""]
    con.close()
    out.insert(4, f"Chosen Related notes found in the top 8: note index {hits['old']}, passage index {hits['new']}.\n")
    rep = C.APP / "reports" / f"related-compare-{C.now(cfg):%Y-%m-%d}.md"
    rep.write_text("\n".join(out), encoding="utf-8")
    print(f"{rep.name}: note index {hits['old']}, passage index {hits['new']} chosen notes in the top 8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
