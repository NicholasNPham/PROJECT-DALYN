"""Decides a document's STAC Type and Subtype from the rules spreadsheet.

The rules live in DALYN_Rules.xlsx so staff can maintain them without a code
change. Each rule is a phrase plus the Type and Subtype to file it under.
DALYN walks the rules from the top and the first one that matches wins, so two
document types can never tie and no scores are involved.

A rule matches when some line of the document STARTS WITH its phrase. That is
what separates an order from a motion: a motion's body says "enter an order
withdrawing counsel", but no line of it begins with "order", while a real
order's title line does.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from difflib import SequenceMatcher

from exceptions import SystemProblem
from logger import get_logger
from models import ClassificationResult

RULES_SHEET = "Rules"

# Column positions on the Rules sheet, zero-based.
# Order | Phrase | TYPE | SUBTYPE | Notes
COL_PHRASE = 1
COL_TYPE = 2
COL_SUBTYPE = 3
COL_FUZZY = 4

# A fuzzy rule also accepts a near-identical line, for headings OCR mangles on
# scanned documents. Real example: DEMAND FOR DISCOVERY came back as
# "demandfordiscqvery", spaces lost and an o read as a q, which scores 0.944.
#
# 0.90 allows roughly one wrong character in ten. Measured against every line
# of the 11 sample documents, nothing crossed it except true matches.
FUZZY_THRESHOLD = 0.90

# Below this many characters (spaces removed) a phrase is too short to fuzz:
# short words have too many plausible near-misses. YES is ignored on them.
MIN_FUZZY_LENGTH = 15

logger = get_logger(__name__)


@dataclass(frozen=True)
class Rule:
    """One row of the Rules sheet, with its phrase pre-normalized."""

    phrase: str
    normalized: str
    compressed: str
    document_type: str | None
    document_subtype: str | None
    fuzzy: bool
    row: int


def normalize(text: str) -> str:
    """Flatten text so spreadsheet phrasing and PDF output can be compared.

    Lowercases, straightens curly quotes, turns every other punctuation mark
    into a space, and collapses runs of whitespace. This is why Dalyn does not
    have to care about capitals, apostrophes, commas, colons or double spaces
    when she writes a phrase, and why OCR spacing quirks do not cause misses.
    """
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def compress(text: str) -> str:
    """Normalize and then strip spaces, so lost spacing cannot break a match.

    OCR routinely runs an underlined heading together. Comparing compressed
    forms makes "demandfordiscovery" and "demand for discovery" identical.
    """
    return normalize(text).replace(" ", "")


def document_lines(document_text: str) -> list[str]:
    """Return the document's non-empty lines, normalized, in order.

    Line breaks are kept deliberately. Matching on line starts is the whole
    mechanism, so joining the document into one blob would break it.
    """
    return [line for line in (normalize(raw) for raw in document_text.splitlines()) if line]


def load_rules(excel_path: Path) -> list[Rule]:
    """Read the Rules sheet, top to bottom. Sheet row order is the priority.

    Args:
        excel_path: Path to DALYN_Rules.xlsx.

    Returns:
        Rules in the order they will be tried. A rule with a blank Type and
        Subtype is kept: it means "recognized, but send it to a person".

    Raises:
        SystemProblem: If the file is missing, unreadable, has no Rules sheet,
            or contains no usable rules.
    """
    try:
        workbook = load_workbook(excel_path, read_only=True, data_only=True)
    except FileNotFoundError as error:
        raise SystemProblem(f"Rules sheet not found at {excel_path}") from error
    except (InvalidFileException, OSError) as error:
        raise SystemProblem(f"Rules sheet at {excel_path} is unreadable: {error}") from error

    try:
        if RULES_SHEET not in workbook.sheetnames:
            raise SystemProblem(
                f"Rules sheet at {excel_path} has no '{RULES_SHEET}' tab. "
                f"Found: {', '.join(workbook.sheetnames)}"
            )

        rules: list[Rule] = []
        seen: dict[str, int] = {}

        for row_number, row in enumerate(
            workbook[RULES_SHEET].iter_rows(min_row=2, values_only=True), start=2
        ):
            phrase = row[COL_PHRASE] if len(row) > COL_PHRASE else None
            if phrase is None or not str(phrase).strip():
                continue

            normalized = normalize(str(phrase))
            if not normalized:
                logger.warning(
                    "Rules row %s: phrase %r is only punctuation, skipped", row_number, phrase
                )
                continue

            if normalized in seen:
                logger.warning(
                    "Rules row %s repeats the phrase from row %s (%r). "
                    "The later row can never match and is ignored.",
                    row_number,
                    seen[normalized],
                    phrase,
                )
                continue
            seen[normalized] = row_number

            fuzzy = _cell(row, COL_FUZZY) == "YES"
            compressed = normalized.replace(" ", "")

            if fuzzy and len(compressed) < MIN_FUZZY_LENGTH:
                logger.warning(
                    "Rules row %s (%r) is marked Fuzzy but is too short (%s characters). "
                    "It will be matched exactly only.",
                    row_number,
                    phrase,
                    len(compressed),
                )
                fuzzy = False

            document_type = _cell(row, COL_TYPE)
            document_subtype = _cell(row, COL_SUBTYPE)

            if bool(document_type) != bool(document_subtype):
                raise SystemProblem(
                    f"Rules row {row_number} ({phrase!r}) has a TYPE or a SUBTYPE but "
                    "not both. Fill in both, or leave both blank to route to Manual Review."
                )

            rules.append(
                Rule(
                    phrase=str(phrase).strip(),
                    normalized=normalized,
                    compressed=compressed,
                    document_type=document_type,
                    document_subtype=document_subtype,
                    fuzzy=fuzzy,
                    row=row_number,
                )
            )
    finally:
        # read_only workbooks hold the file open until closed explicitly.
        workbook.close()

    if not rules:
        raise SystemProblem(f"Rules sheet at {excel_path} has no rules in it.")

    _warn_on_shadowed_rules(rules)

    logger.info("Loaded %s rules from %s", len(rules), excel_path)
    return rules


def classify(rules: list[Rule], document_text: str) -> ClassificationResult:
    """Return the Type and Subtype for one document, or an empty result.

    Args:
        rules: Rules from load_rules, already in priority order.
        document_text: The document's extracted text, from ocr.extract_text.

    Returns:
        A ClassificationResult. document_type is None when nothing matched, or
        when the matching rule was left deliberately blank. Either way the
        email goes to Manual Review, and matched_phrase says which happened.
    """
    lines = document_lines(document_text)

    # Pass one: exact. Every rule gets a clean shot before any fuzzing happens,
    # so a document DALYN already reads correctly is never touched by pass two.
    for rule in rules:
        for line in lines:
            if line.startswith(rule.normalized):
                return _result(rule, line, 1.0)

    # Pass two: the rules Dalyn marked Fuzzy, still in priority order.
    best: tuple[Rule, str, float] | None = None

    for rule in rules:
        if not rule.fuzzy:
            continue
        for line in lines:
            similarity = _similarity(rule.compressed, compress(line))
            if similarity >= FUZZY_THRESHOLD and (best is None or similarity > best[2]):
                best = (rule, line, similarity)
        if best is not None:
            # Priority beats similarity: once a higher rule has any acceptable
            # match, a lower rule cannot outrank it on score alone.
            break

    if best is not None:
        rule, line, similarity = best
        logger.info(
            "Fuzzy match at %.0f%%: rules row %s (%r) matched %r",
            similarity * 100,
            rule.row,
            rule.phrase,
            line,
        )
        return _result(rule, line, similarity)

    return ClassificationResult()


def _similarity(rule_compressed: str, line_compressed: str) -> float:
    """Compare a rule against the START of a line, both already compressed.

    Only the leading characters of the line are compared, mirroring the exact
    pass. Without that, a long paragraph containing the phrase somewhere in the
    middle would score as a title line.
    """
    if not line_compressed:
        return 0.0
    head = line_compressed[: len(rule_compressed)]
    return SequenceMatcher(None, rule_compressed, head).ratio()


def _result(rule: Rule, line: str, similarity: float) -> ClassificationResult:
    """Build the result for a rule that matched."""
    return ClassificationResult(
        document_type=rule.document_type,
        document_subtype=rule.document_subtype,
        matched_phrase=rule.phrase,
        matched_line=line,
        rule_row=rule.row,
        similarity=similarity,
    )

def _cell(row: tuple, index: int) -> str | None:
    """Return a trimmed, upper-cased cell value, or None if it is blank."""
    if len(row) <= index or row[index] is None:
        return None
    value = str(row[index]).strip().upper()
    return value or None


def _warn_on_shadowed_rules(rules: list[Rule]) -> None:
    """Warn when a rule can never fire because a shorter one sits above it.

    'DEMAND FOR DISCOVERY' above 'DEMAND FOR DISCOVERY AND INSPECTION' means the
    second never runs. The sheet still loads, because the fix is Dalyn's to make.
    """
    for position, rule in enumerate(rules):
        for earlier in rules[:position]:
            if rule.normalized.startswith(earlier.normalized):
                logger.warning(
                    "Rules row %s (%r) can never match: row %s (%r) sits above it and "
                    "catches the same lines. Move the longer phrase above the shorter one.",
                    rule.row,
                    rule.phrase,
                    earlier.row,
                    earlier.phrase,
                )
                break