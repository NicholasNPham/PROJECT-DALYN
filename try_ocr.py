"""Throwaway: print every line DALYN reads from one PDF, numbered.

For seeing whether a title that is on the page made it into the text at
all, past the 25 lines diag.py shows. Reads the PDF only; touches no mailbox
and no browser.

Usage: python try_ocr.py <path-to-pdf> [--ocr]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
import ocr  # noqa: E402
from classifier import document_lines  # noqa: E402
from config_loader import load_config  # noqa: E402
from exceptions import SystemProblem  # noqa: E402


def main() -> None:
    """OCR or read the one PDF named on the command line and print its lines."""
    args = sys.argv[1:]
    # diag.py retries with OCR when the text layer matches no rule, so the
    # flag shows what that retry read.
    force_ocr = "--ocr" in args
    args = [arg for arg in args if arg != "--ocr"]
    if len(args) != 1:
        sys.exit("Usage: python try_ocr.py <path-to-pdf> [--ocr]")
    pdf = Path(args[0])
    if not pdf.is_file():
        sys.exit(f"No such file: {pdf}")

    try:
        config = load_config()
        # Same Tesseract the real run uses. Without this pytesseract only
        # looks on PATH, which is where it was not found.
        ocr.configure_tesseract(config["paths"].get("tesseract"))
    except SystemProblem as error:
        sys.exit(f"Setup problem: {error}")

    start = time.perf_counter()
    text, source = ocr.extract_text(pdf.read_bytes(), pdf.name, force_ocr=force_ocr)
    lines = document_lines(text)
    print(f"{pdf.name}: source={source}, {len(text)} chars, {len(lines)} lines, "
          f"{time.perf_counter() - start:.1f}s\n")
    # Normalized, as the classifier sees them, so a missing title shows as
    # missing here exactly when it is missing for the rules.
    for number, line in enumerate(lines):
        print(f"{number:4} {line!r}")


if __name__ == "__main__":
    main()
