"""Checks that OCRmyPDF works on this PC: makes an image-only PDF, OCRs it, reads the text back.
Run with the Vault Librarian runtime's python. Writes only to a temp folder."""
import subprocess, sys, tempfile
from pathlib import Path
import pymupdf

tmp = Path(tempfile.mkdtemp(prefix="ocr-check-"))
doc = pymupdf.open()
page = doc.new_page()
page.insert_text((72, 120), "Owner's Manual Model No. XR-200 dishwasher", fontsize=20)
pix = page.get_pixmap(dpi=200)
img = pymupdf.open()
p2 = img.new_page(width=page.rect.width, height=page.rect.height)
p2.insert_image(p2.rect, pixmap=pix)
src, out = tmp / "scan.pdf", tmp / "scan-ocr.pdf"
img.save(src)
print("text before OCR:", repr(pymupdf.open(src)[0].get_text().strip()))
for extra in ([], ["--output-type", "pdf"]):
    r = subprocess.run([sys.executable, "-m", "ocrmypdf", "--force-ocr", *extra, str(src), str(out)],
                       capture_output=True, text=True)
    print("ocrmypdf", extra, "exit", r.returncode, (r.stderr or "").strip()[-600:])
    if r.returncode == 0:
        print("text after OCR:", repr(pymupdf.open(out)[0].get_text().strip()))
