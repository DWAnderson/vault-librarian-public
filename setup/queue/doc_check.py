"""Checks that old Word .doc files convert through Word (COM) on this PC. Temp folder only."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))
import extract as X  # noqa: E402
import pythoncom  # noqa: E402
import win32com.client  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="doc-check-"))
pythoncom.CoInitialize()
w = win32com.client.DispatchEx("Word.Application")
w.Visible, w.DisplayAlerts = False, 0
try:
    d = w.Documents.Add()
    d.Content.Text = "Hangar lease memo\rThe rent escalates 3% each year."
    d.SaveAs2(str(tmp / "old.doc"), FileFormat=0)   # Word 97-2003 .doc
    d.Close(False)
finally:
    w.Quit()
res = X.extract(tmp / "old.doc", tmp / "work")
print("words:", res["words"], "| text:", res["text"][:80].replace("\n", " / "))
print("RESULT", "PASS" if "rent escalates" in res["text"] else "FAIL")
