"""Print pages of a queued PDF's extracted text (read-only).  python page_text.py <item id> <from> <to> [out.txt]"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))
import common as C, vqueue as Q
st = Q.Store(C.load_config())
pages = json.loads((st.payload_root / sys.argv[1] / "pages.json").read_text(encoding="utf-8"))
out = "\n".join(f"=== page {i}\n{pages[i-1]}" for i in range(int(sys.argv[2]), int(sys.argv[3]) + 1))
if len(sys.argv) > 4:
    Path(sys.argv[4]).write_text(out, encoding="utf-8")
else:
    print(out)
