"""Show how longdoc would split a queued book (read-only; no model calls).  python show_plan.py <item id>"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))
import common as C, vqueue as Q, longdoc
cfg = C.load_config(); st = Q.Store(cfg); it = st.load(sys.argv[1])
pay = st.payload_root / it["id"]
print("payload exists:", pay.exists())
doc = json.loads((pay / "extract.json").read_text(encoding="utf-8")); doc["text"] = (pay / "text.md").read_text(encoding="utf-8")
print("title:", doc.get("title"), "| author:", doc.get("author"), "| bookmarks:", len(doc.get("chapters") or []))
parts, skipped, hp = longdoc.plan(st, it, doc)
for p in parts: print(f"{p['id']} {longdoc.span(p, hp):>14} {p['words']:>7,}  {p['title'][:70]}")
print("skipped:", "; ".join(f"{p['title']} ({longdoc.span(p, hp)}, {p['words']:,} w)" for p in skipped))
print("words in parts:", sum(p['words'] for p in parts))
