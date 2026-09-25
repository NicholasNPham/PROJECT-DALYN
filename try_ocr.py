import sys
import time
from pathlib import Path

sys.path.insert(0, "src")
from ocr import extract_text

for pdf in sorted(Path("temp/sample").glob("*.pdf")):
    start = time.perf_counter()
    try:
        text = extract_text(pdf.read_bytes(), pdf.name)
        elapsed = time.perf_counter() - start
        print(f"{pdf.name}: {len(text)} chars in {elapsed:.2f}s")
    except Exception as error:
        print(f"{pdf.name}: {type(error).__name__}: {error}")

text = extract_text(Path("temp/sample/012_Notice Of Appearance.pdf").read_bytes(), "012")
print(text[:800])