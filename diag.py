"""Throwaway: show exactly what the classifier sees, and why it decided what it did.

Usage:
    python diag.py <file-or-folder>            every rule that came close
    python diag.py <file-or-folder> notice     only rules containing "notice"
    python diag.py <file-or-folder> --lines    also dump every normalized line

Answers the three questions that keep coming up:
    Did it read the document, or read garbage and not notice?
    Which rule won, and on which line?
    For a rule that should have won, how close did it get?
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, "src")

import classifier
from config_loader import load_config
from exceptions import DalynError
import ocr
from ocr import extract_text

# How many near-miss rules to list per document, best first.
TOP_NEAR_MISSES = 6


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    target = Path(sys.argv[1])
    arguments = sys.argv[2:]
    show_lines = "--lines" in arguments
    keyword = next((a.lower() for a in arguments if not a.startswith("-")), None)

    if not target.exists():
        print(f"Not found: {target}")
        return 2

    # A folder path passed to read_bytes fails as PermissionError on Windows,
    # which sends you hunting for a permissions problem that is not there.
    pdfs = sorted(target.glob("*.pdf")) if target.is_dir() else [target]

    if not pdfs:
        print(f"No PDFs in {target}")
        return 2

    rules = classifier.load_rules(load_config()["paths"]["excel"])
    print(f"{len(rules)} rules loaded. Safety net is row {rules[-1].row} ({rules[-1].phrase!r}).")

    for pdf in pdfs:
        print()
        print("=" * 100)
        print(pdf.name)
        print("=" * 100)
        try:
            report(pdf, rules, keyword, show_lines)
        except DalynError as error:
            print(f"  {type(error).__name__}: {error}")

    return 0


def report(pdf: Path, rules: list, keyword: str | None, show_lines: bool) -> None:
    """Print the extraction, the decision, and the near misses for one PDF."""
    text, source = extract_text(pdf.read_bytes(), pdf.name)
    lines = classifier.document_lines(text)

    words = [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 1]
    raw_lines = sum(1 for line in text.splitlines() if line.strip())
    rate = sum(1 for w in words if w in ocr.COMMON_WORDS) / max(len(words), 1)
    per_line = len(words) / max(raw_lines, 1)

    print(f"  source={source}  chars={len(text)}  lines={len(lines)}")
    print(
        f"  readability: {len(words)} words, {rate:.3f} real-word rate "
        f"(floor {ocr.MIN_COMMON_WORD_RATE}), {per_line:.2f} words/line "
        f"(floor {ocr.MIN_WORDS_PER_LINE})"
    )

    if source == "embedded":
        print("  (read the PDF's own text layer, OCR never ran)")
    else:
        print("  (no usable text layer, this went through OCR)")

    print("\n  first 12 lines:")
    for index, line in enumerate(lines[:12]):
        print(f"    {index:3} {line[:92]!r}")

    if show_lines and len(lines) > 12:
        print("\n  remaining lines:")
        for index, line in enumerate(lines[12:], start=12):
            print(f"    {index:3} {line[:92]!r}")

    result = classifier.classify(rules, text)

    print("\n  DECISION:")
    if result.matched_phrase is None:
        print("    no rule matched, Manual Review")
    else:
        how = "EXACT" if result.similarity == 1.0 else f"FUZZY {result.similarity * 100:.0f}%"
        verdict = (
            f"{result.document_type}/{result.document_subtype}"
            if result.is_classified
            else "blank, routes to a person"
        )
        print(f"    {verdict}")
        print(f"    rules row {result.rule_row}  {result.matched_phrase!r}  {how}")
        print(f"    on line: {result.matched_line!r}")

    print("\n  how close every other rule got (best line per rule):")
    scored = []
    for rule in rules:
        best_line, best_score = "", 0.0
        for line in lines:
            if line.startswith(rule.normalized):
                best_line, best_score = line, 1.0
                break
            score = classifier._similarity(rule.compressed, classifier.compress(line))
            if score > best_score:
                best_line, best_score = line, score
        scored.append((best_score, rule, best_line))

    if keyword:
        scored = [s for s in scored if keyword in s[1].normalized]

    scored.sort(key=lambda s: s[0], reverse=True)

    for score, rule, line in scored[:TOP_NEAR_MISSES]:
        if score == 1.0:
            verdict = "exact match"
        elif not rule.fuzzy:
            verdict = f"would need Fuzzy? = YES ({len(rule.compressed)} chars)"
        elif score >= classifier.FUZZY_THRESHOLD:
            verdict = "over the fuzzy threshold"
        else:
            verdict = f"under the {classifier.FUZZY_THRESHOLD:.2f} threshold"
        print(f"    {score:.3f}  row {rule.row:3} {rule.phrase[:40]:42} {verdict}")
        print(f"           vs {line[:86]!r}")


if __name__ == "__main__":
    sys.exit(main())