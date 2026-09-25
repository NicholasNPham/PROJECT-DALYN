"""Throwaway: find OCR settings that read this document's title lines correctly."""

import sys
from pathlib import Path

import easyocr
import pymupdf

pdf = Path(sys.argv[1])
reader = easyocr.Reader(["en"], gpu=False, verbose=False)

VARIANTS = [
    ("dpi 300 baseline",   dict(dpi=300, gray=False), {}),
    ("dpi 400",            dict(dpi=400, gray=False), {}),
    ("dpi 600",            dict(dpi=600, gray=False), {}),
    ("dpi 400 grayscale",  dict(dpi=400, gray=True),  {}),
    ("dpi 300 mag 2",      dict(dpi=300, gray=False), dict(mag_ratio=2.0)),
    ("dpi 300 loose boxes", dict(dpi=300, gray=False),
        dict(width_ths=0.3, text_threshold=0.6, low_text=0.3)),
    ("dpi 400 mag 2 loose", dict(dpi=400, gray=True),
        dict(mag_ratio=2.0, width_ths=0.3, text_threshold=0.6, low_text=0.3)),
]

WANT = ("notice of appearance", "demand for discovery")

for label, render, params in VARIANTS:
    document = pymupdf.open(pdf)
    with document:
        page = document[0]
        pixmap = (
            page.get_pixmap(dpi=render["dpi"], colorspace=pymupdf.csGRAY)
            if render["gray"]
            else page.get_pixmap(dpi=render["dpi"])
        )
        lines = reader.readtext(pixmap.tobytes("png"), detail=0, paragraph=False, **params)

    clean = [" ".join(l.lower().split()) for l in lines]
    found = [w for w in WANT if any(c.startswith(w) for c in clean)]
    print(f"\n=== {label}: {len(found)}/2 title lines correct")
    for line in clean:
        if "appear" in line or "demand" in line or "discov" in line:
            print(f"    {line!r}")