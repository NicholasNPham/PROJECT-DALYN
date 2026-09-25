"""Classify a folder of PDFs and write a review report. Not part of the pipeline.

Usage:
    python review_batch.py <folder of PDFs> [output csv]

Produces one row per PDF: what DALYN read, what it classified it as, and which
rule decided. A reviewer checks the Type and Subtype columns against what the
document actually is.

The rules sheet comes from config.yaml (paths.excel), so this runs against the
same sheet the real pipeline will use, wherever it lives.
"""

import csv
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import classifier  # noqa: E402
import ocr  # noqa: E402
import ucn  # noqa: E402
from config_loader import load_config  # noqa: E402
from exceptions import DocumentProblem  # noqa: E402
from logger import setup_logging  # noqa: E402

COLUMNS = (
    "file",
    "ucn",
    "text_source",
    "char_count",
    "type",
    "subtype",
    "matched_phrase",
    "matched_line",
    "rule_row",
    "match",
    "outcome",
    "reason",
)


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "temp" / "sample"
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else PROJECT_ROOT / "review_report.csv"

    config = load_config()
    setup_logging(config["paths"]["logs"])

    rules_path = config["paths"]["excel"]
    rules = classifier.load_rules(rules_path)

    # Print which sheet was used and when it last changed. When a reviewer says
    # "everything came out wrong on Tuesday", this is how you tell whether the
    # sheet was edited that morning.
    modified = datetime.fromtimestamp(rules_path.stat().st_mtime)
    print(f"Rules sheet: {rules_path}")
    print(f"{len(rules)} rules, last modified {modified:%Y-%m-%d %H:%M}")

    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs in {folder}")
        return 1

    rows = [_review_one(pdf, rules) for pdf in pdfs]

    with open(out_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    _print_table(rows)

    classified = sum(1 for row in rows if row["outcome"] == "CLASSIFIED")
    fuzzy = sum(1 for row in rows if str(row["match"]).startswith("FUZZY"))
    print(f"\n{classified} classified, {len(rows) - classified} to Manual Review")
    if fuzzy:
        print(f"{fuzzy} matched fuzzily and need a hand check")
    print(f"Report: {out_path}")
    return 0


def _review_one(pdf: Path, rules: list) -> dict:
    """Read, classify and summarize one PDF. Never raises; failures become rows."""
    row = dict.fromkeys(COLUMNS, "")
    row["file"] = pdf.name

    try:
        text, source = ocr.extract_text(pdf.read_bytes(), pdf.name)
    except DocumentProblem as error:
        row["outcome"] = "MANUAL REVIEW"
        row["reason"] = f"Could not read the PDF: {error}"
        return row

    row["text_source"] = source
    row["char_count"] = len(text)
    row["ucn"] = ucn.find_ucn(text) or ""

    result = classifier.classify(rules, text)
    row["matched_phrase"] = result.matched_phrase or ""
    row["matched_line"] = (result.matched_line or "")[:80]
    row["rule_row"] = result.rule_row or ""

    if result.matched_phrase:
        row["match"] = "EXACT" if not result.is_fuzzy else f"FUZZY {result.similarity:.0%}"

    if result.is_classified:
        row["type"] = result.document_type
        row["subtype"] = result.document_subtype
        row["outcome"] = "CLASSIFIED"
    elif result.matched_phrase:
        row["outcome"] = "MANUAL REVIEW"
        row["reason"] = f"Rule row {result.rule_row} matched but has no Type or Subtype"
    else:
        row["outcome"] = "MANUAL REVIEW"
        row["reason"] = "No rule matched. Add a rule for this document's title line."

    if result.is_fuzzy:
        row["reason"] = (
            f"OCR text did not match exactly ({result.similarity:.0%}). "
            "Check this one by hand."
        )
    elif row["outcome"] == "CLASSIFIED" and not row["ucn"]:
        row["reason"] = "Classified, but no UCN in the document"

    return row


def _print_table(rows: list[dict]) -> None:
    print(f"\n{'FILE':44} {'TYPE':10} {'SUBTYPE':9} {'SRC':8} {'MATCH':10} {'MATCHED ON':24} OUTCOME")
    print("-" * 132)
    for row in rows:
        print(
            f"{row['file'][:44]:44} {str(row['type'])[:10]:10} {str(row['subtype'])[:9]:9} "
            f"{str(row['text_source']):8} {str(row['match']):10} "
            f"{str(row['matched_phrase'])[:24]:24} {row['outcome']}"
        )


if __name__ == "__main__":
    raise SystemExit(main())