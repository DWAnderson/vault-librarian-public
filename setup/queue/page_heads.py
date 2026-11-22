"""Print the first line of each page of a queued PDF (read-only).  python page_heads.py <item id> [from] [to]"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))
import common as C, vqueue as Q
cfg = C.load_config(); st = Q.Store(cfg)
pages = json.loads((st.payload_root / sys.argv[1] / "pages.json").read_text(encoding="utf-8"))
a = int(sys.argv[2]) if len(sys.argv) > 2 else 1; b = int(sys.argv[3]) if len(sys.argv) > 3 else len(pages)
for i in range(a, b + 1):
    t = pages[i - 1]
    blocks = t.split("\n\n")
    print(i, len(t.split()), "|", " || ".join(x[:60] for x in blocks[:2]))
