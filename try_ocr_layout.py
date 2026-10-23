"""Throwaway: which OCR setting finds a title that DALYN's OCR drops?

Page 1 only, read several ways: with and without the underline cleanup, and
under Tesseract layout modes 3 (DALYN's), 4, 6 and 11. For each, prints only
the lines containing a word you give (default "motion"), so body text and
names stay off the screen. Reads the PDF only; no mailbox, no browser.

Usage: python try_ocr_layout.py <path-to-pdf> [word ...]
"""

import sys
from pathlib import Path

import cv2
import numpy
import pymupdf
import pytesseract

sys.path.insert(0, str(Path(__file__).parent / "src"))
import ocr  # noqa: E402
from classifier import document_lines  # noqa: E402
from config_loader import load_config  # noqa: E402
from exceptions import SystemProblem  # noqa: E402

LAYOUT_MODES = ("3", "4", "6", "11")


def render_page_one(pdf: Path) -> numpy.ndarray:
    """Page 1 as an RGB array at DALYN's OCR resolution, before any cleanup."""
    with pymupdf.open(pdf) as document:
        pixmap = document[0].get_pixmap(dpi=ocr.OCR_DPI)
    image = numpy.frombuffer(pixmap.samples, dtype=numpy.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
    if pixmap.n == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
    if pixmap.n == 1:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    return image.copy()


def painted_out(raw: numpy.ndarray, cleaned: numpy.ndarray) -> str:
    """Where the underline cleanup whitened the page, as bands of height.

    A tall band in the top third would mean a shaded or boxed title was
    erased along with the underlines.
    """
    changed = numpy.any(raw != cleaned, axis=2)
    rows = numpy.flatnonzero(changed.any(axis=1))
    if rows.size == 0:
        return "nothing"
    height = raw.shape[0]
    bands, start, last = [], rows[0], rows[0]
    for row in rows[1:]:
        if row - last > 5:
            bands.append((start, last))
            start = row
        last = row
    bands.append((start, last))
    return ", ".join(f"{a / height:.0%}-{b / height:.0%} down ({b - a + 1}px tall)" for a, b in bands)


def main() -> None:
    """Run each variant on page 1 and print the matching lines."""
    if len(sys.argv) < 2:
        sys.exit("Usage: python try_ocr_layout.py <path-to-pdf> [word ...]")
    pdf = Path(sys.argv[1])
    words = [word.lower() for word in sys.argv[2:]] or ["motion"]
    if not pdf.is_file():
        sys.exit(f"No such file: {pdf}")

    try:
        ocr.configure_tesseract(load_config()["paths"].get("tesseract"))
    except SystemProblem as error:
        sys.exit(f"Setup problem: {error}")

    raw = render_page_one(pdf)
    # DALYN's own cleanup, so "cleaned" is exactly what the real run OCRs.
    with pymupdf.open(pdf) as document:
        cleaned = ocr._page_image(document[0].get_pixmap(dpi=ocr.OCR_DPI), pdf.name, 1)

    print(f"Underline cleanup whitened: {painted_out(raw, cleaned)}\n")

    for image_label, image in (("cleaned (DALYN)", cleaned), ("no cleanup", raw)):
        for mode in LAYOUT_MODES:
            text = pytesseract.image_to_string(image, config=f"--psm {mode}")
            lines = document_lines(text)
            hits = [(number, line) for number, line in enumerate(lines) if any(word in line for word in words)]
            print(f"{image_label}, psm {mode}: {len(lines)} lines, {len(hits)} with {words}")
            for number, line in hits:
                print(f"    {number:3} {line!r}")


if __name__ == "__main__":
    main()
