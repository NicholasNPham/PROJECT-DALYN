"""Throwaway: check UCN extraction hit rate against the sample folder."""

import sys
from pathlib import Path

sys.path.insert(0, "src")
from ocr import extract_text
from ucn import find_ucn

found = 0
missing = []

for pdf in sorted(Path("temp/sample").glob("*.pdf")):
    try:
        ucn = find_ucn(extract_text(pdf.read_bytes(), pdf.name))
    except Exception as error:
        print(f"{pdf.name}: {type(error).__name__}: {error}")
        continue

    if ucn:
        found += 1
        print(f"{pdf.name}: {ucn}")
    else:
        missing.append(pdf.name)
        print(f"{pdf.name}: NO UCN")

total = found + len(missing)
print(f"\n{found} of {total} documents had a UCN")

if missing:
    print("\nNo UCN found in:")
    for name in missing:
        print(f"  {name}")