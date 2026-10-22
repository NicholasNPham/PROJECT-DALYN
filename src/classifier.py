"""Decides a document's STAC Type and Subtype from the rules spreadsheet.

The rules live in DALYN_Rules.xlsx so staff can maintain them without a code
change. Each rule is a phrase plus the Type and Subtype to file it under.
DALYN walks the rules from the top and the first one that matches wins, so two
document types can never tie and no scores are involved.

A rule matches when some line of the document STARTS WITH its phrase. That is
what separates an order from a motion: a motion's body says "enter an order
withdrawing counsel", but no line of it begins with "order", while a real
order's title line does.

Three phrases are exceptions: ORDER, MOTION and JUDGMENT exist to catch titles
they cannot name, so they only look at the first TITLE_REGION_LINES lines,
where a title can actually be. Everywhere else they do more harm than good.

The bottom row of the sheet is the safety net. It is tried only after every
other rule has had its turn, exact and fuzzy both, so a catch-all phrase like
CERTIFICATE OF SERVICE can never outrank a real title.
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
# Order | Phrase | TYPE | SUBTYPE | Fuzzy? | Notes
COL_PHRASE = 1
COL_TYPE = 2
COL_SUBTYPE = 3
COL_FUZZY = 4

# A fuzzy rule also accepts a near-identical line, for headings OCR mangles on
# scanned documents. Real example: DEMAND FOR DISCOVERY came back as
# "demandfordiscqvery", spaces lost and an o read as a q, which scores 0.944.
#
# 0.90 allows roughly one wrong character in ten. Measured against every line
# of the 62 batch 3 documents, nothing crossed it except true matches.
FUZZY_THRESHOLD = 0.90

# Below this many characters (spaces removed) a phrase is too short to fuzz:
# short words have too many plausible near-misses. YES is ignored on them.
#
# 14, not 15, because NOTICE OF FILING and NOTICE OF TAKING are both exactly
# 14 and both need it. Batch 3's 010_Order is a scanned notice of filing whose
# heading OCR reads as "notce of filing", scoring 0.963, and the old floor of
# 15 silently disabled the only rule that could catch it. ORDER at 5 and
# MOTION at 6 stay exact-only, which is what this floor is really protecting.
MIN_FUZZY_LENGTH = 14

# The catch-all rules exist to recognise a title they cannot name, so they are
# confined to where a title can actually be: the top of the first page, below
# the caption. Everywhere else they are a liability. A memorandum of law is
# nothing but quoted rulings, so "Judgment", "Order" and "Motion" start lines
# throughout it; batch 9's 027_Memorandum Of Law is 61,000 characters and was
# filed COURT/ORDER off a citation on some page in the middle.
#
# Every other rule names a specific document and still matches anywhere, which
# the office requires: a demand for discovery wins wherever it appears.
#
# 35 lines is about one full page. 25 (about double batch 8's
# 002_Motion To Consolidate, six case numbers with the title on line 11)
# still missed titles, and a page keeps the window well short of the body
# text the rule above guards against.
TITLE_REGION_LINES = 35
TITLE_ONLY_PHRASES = frozenset({"order", "motion", "judgment", "memorandum of law"})

# Phrases that may sit anywhere in a title line, not only at its start. Kept
# to the title region, since mid-line anywhere in a document is exactly what
# starts-with exists to avoid. A defense notice of participation at
# depositions in the 14 Oct 2026 test batch put the defendant's name first,
# "defendant <name> notice of participation", so no phrase could start it.
#
# The sheet can ask for the same on any row by starting the phrase with a *,
# as in * MOTION TO WITHDRAW. Added 16 Oct 2026 after two defense filings
# matched nothing: "notice of calendar conflict and unopposed motion to
# continue", and a motion to withdraw headed with the law firm's name.
TITLE_CONTAINS_PHRASES = frozenset({"notice of participation"})
ANYWHERE_IN_TITLE = "*"

# Words that, before a mid-line phrase, mean the document is about that
# filing rather than the filing itself: "order granting defendant s motion to
# continue" is an order, "notice of hearing on motion to continue" a notice of
# hearing, "state s response to motion to continue" a response. Any of them
# earlier in the line stops the mid-line match, and the rest of the sheet
# decides. A line that starts with the phrase is unaffected.
TITLE_CONTAINS_BLOCKERS = frozenset({
    "order", "granting", "denying", "hearing", "response", "objection",
    "opposition", "reply", "withdrawal", "withdraw",
})

# A * in a phrase stands in for the defendant's name, which defense titles
# put in the middle: "defendant <name> s witness list" in the 14 Oct 2026
# test batch. Written in the sheet as DEFENDANT * WITNESS LIST. The gap is
# limited so it cannot swallow real title words: at most four words (first,
# middle, last and a possessive s), and none that begin another document,
# or "motion to strike state s witness list" would file as a witness list.
# Only rows written with a * behave this way; every other row is unchanged.
NAME_GAP = "*"
NAME_GAP_MAX_WORDS = 4
NAME_GAP_STOP_WORDS = frozenset({
    "motion", "response", "objection", "strike", "exclude", "notice", "order",
    "reply", "opposition", "to", "of", "for", "on", "in",
})

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
    title_only: bool
    row: int
    title_contains: bool = False
    name_gap: re.Pattern | None = None


def normalize(text: str) -> str:
    """Flatten text so spreadsheet phrasing and PDF output can be compared.

    Lowercases, straightens curly quotes, turns every other punctuation mark
    into a space, and collapses runs of whitespace. This is why Dalyn does not
    have to care about capitals, apostrophes, commas, colons or double spaces
    when she writes a phrase, and why OCR spacing quirks do not cause misses.

    Note that an apostrophe becomes a space, so DEFENDANT'S normalizes to
    "defendant s" and will NOT match a document that prints DEFENDANTS. Write
    such phrases without the apostrophe and mark them Fuzzy, since the fuzzy
    pass compares with spaces removed and so accepts both spellings.
    """
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
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
        The LAST rule returned is treated as the safety net by classify().

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

            # A leading * and no other: the phrase may come after anything in a
            # title line, such as a law firm's name of any length. normalize
            # has already turned the * into a space.
            anywhere = str(phrase).strip().startswith(ANYWHERE_IN_TITLE) and str(phrase).count("*") == 1
            seen_key = f"{ANYWHERE_IN_TITLE} {normalized}" if anywhere else normalized

            name_gap = None
            if NAME_GAP in str(phrase) and not anywhere:
                name_gap = _name_gap_pattern(str(phrase))
                if name_gap is None:
                    logger.warning(
                        "Rules row %s (%r): a * needs words on both sides and only one *. Skipped.",
                        row_number,
                        phrase,
                    )
                    continue
                # Kept with the * so it never equals, or is taken for a
                # duplicate of, the same words written without a gap.
                normalized = f" {NAME_GAP} ".join(normalize(part) for part in str(phrase).split(NAME_GAP))
                seen_key = normalized

            if seen_key in seen:
                logger.warning(
                    "Rules row %s repeats the phrase from row %s (%r). "
                    "The later row can never match and is ignored.",
                    row_number,
                    seen[seen_key],
                    phrase,
                )
                continue
            seen[seen_key] = row_number

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

            if fuzzy and (name_gap or anywhere):
                # Fuzzy compares a fixed run of characters from the start of
                # a line, which a name of any length breaks.
                logger.warning(
                    "Rules row %s (%r) has a * and is marked Fuzzy. It will be matched exactly only.",
                    row_number,
                    phrase,
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
                    # A * rule is a title with a name in it, so it looks only
                    # where a title can be.
                    title_only=(
                        normalized in TITLE_ONLY_PHRASES
                        or normalized in TITLE_CONTAINS_PHRASES
                        or name_gap is not None
                        or anywhere
                    ),
                    row=row_number,
                    title_contains=anywhere or normalized in TITLE_CONTAINS_PHRASES,
                    name_gap=name_gap,
                )
            )
    finally:
        # read_only workbooks hold the file open until closed explicitly.
        workbook.close()

    if not rules:
        raise SystemProblem(f"Rules sheet at {excel_path} has no rules in it.")

    _warn_on_shadowed_rules(rules)

    nets = [rule.phrase for rule in rules if not rule.document_type]
    logger.info(
        "Loaded %s rules from %s. Safety nets, tried last: %s",
        len(rules),
        excel_path,
        ", ".join(repr(phrase) for phrase in nets) if nets else "none",
    )
    return rules


def classify(rules: list[Rule], document_text: str) -> ClassificationResult:
    """Return the Type and Subtype for one document, or an empty result.

    Three passes, in this order:

        1. Exact, over every rule that files to a real Type.
        2. Fuzzy, over those same rules where Fuzzy? is YES.
        3. The safety-net rules, exact then fuzzy.

    A safety net is any rule with a blank TYPE and SUBTYPE. It is held back
    because a net that competes in pass one is not a net: a phrase that appears
    in the body of nearly every filing would win pass one outright on a scan
    whose heading OCR mangled slightly, and the specific rule that would have
    matched in pass two would never be reached. Batch 3's 029_Notice Of
    Appearance did exactly that, losing at 0.944 to a catch-all.

    A sheet with no blank rules has no safety net and this collapses to the
    ordinary two passes.

    Args:
        rules: Rules from load_rules, already in priority order.
        document_text: The document's extracted text, from ocr.extract_text.

    Returns:
        A ClassificationResult. document_type is None when nothing matched, or
        when the matching rule was left deliberately blank. Either way the
        email goes to Manual Review, and matched_phrase says which happened.
    """
    lines = document_lines(document_text)
    title_lines = lines[:TITLE_REGION_LINES]

    contenders = [rule for rule in rules if rule.document_type]
    safety_nets = [rule for rule in rules if not rule.document_type]

    result = _match_exact(contenders, lines, title_lines) or _match_fuzzy(
        contenders, lines, title_lines
    )
    if result is not None:
        return result

    if safety_nets:
        result = _match_exact(safety_nets, lines, title_lines) or _match_fuzzy(
            safety_nets, lines, title_lines
        )
        if result is not None:
            logger.info(
                "No rule matched a title line. Falling back to rules row %s (%r).",
                result.rule_row,
                result.matched_phrase,
            )
            return result

    return ClassificationResult()


def _match_exact(
    rules: list[Rule], lines: list[str], title_lines: list[str]
) -> ClassificationResult | None:
    """Return the first rule whose phrase starts a line, or None.

    A title-only rule sees just the first TITLE_REGION_LINES lines. A
    title-contains rule may also match mid-line there, on whole words.
    """
    for rule in rules:
        for line in title_lines if rule.title_only else lines:
            if line.startswith(rule.normalized):
                return _result(rule, line, 1.0)
            if rule.title_contains and _contains_as_title(rule.normalized, line):
                return _result(rule, line, 1.0)
            if rule.name_gap and rule.name_gap.match(line):
                return _result(rule, line, 1.0)
    return None


def _contains_as_title(phrase: str, line: str) -> bool:
    """True when phrase sits in line on whole words, with no blocker before it.

    Padded so "notice of participation" cannot match inside a longer word on
    either side. Only the first place it occurs is judged: a later one has
    the same words before it and more.
    """
    padded = f" {line} "
    start = padded.find(f" {phrase} ")
    return start != -1 and not TITLE_CONTAINS_BLOCKERS & set(padded[:start].split())


def _name_gap_pattern(phrase: str) -> re.Pattern | None:
    """Build the match for a phrase with a * standing in for a name.

    DEFENDANT * WITNESS LIST matches a line that starts "defendant", then up
    to NAME_GAP_MAX_WORDS words none of them in NAME_GAP_STOP_WORDS, then
    "witness list". Zero words is allowed, so a title with no name matches.

    Returns:
        The compiled pattern, or None if the phrase does not have exactly
        one * with words on both sides.
    """
    parts = phrase.split(NAME_GAP)
    if len(parts) != 2:
        return None
    before, after = normalize(parts[0]), normalize(parts[1])
    if not before or not after:
        return None
    stop = "|".join(sorted(NAME_GAP_STOP_WORDS))
    word = rf"(?!(?:{stop})\b)[a-z0-9]+"
    return re.compile(rf"{re.escape(before)}(?: {word}){{0,{NAME_GAP_MAX_WORDS}}} {re.escape(after)}")


def _match_fuzzy(
    rules: list[Rule], lines: list[str], title_lines: list[str]
) -> ClassificationResult | None:
    """Return the best near-match for the highest fuzzy rule that has one.

    Priority beats similarity: once a higher rule has any acceptable match, a
    lower rule cannot outrank it on score alone.

    A title-only rule sees just the first TITLE_REGION_LINES lines.
    """
    best: tuple[Rule, str, float] | None = None

    for rule in rules:
        if not rule.fuzzy:
            continue
        for line in title_lines if rule.title_only else lines:
            similarity = _similarity(rule.compressed, compress(line))
            if similarity >= FUZZY_THRESHOLD and (best is None or similarity > best[2]):
                best = (rule, line, similarity)
        if best is not None:
            break

    if best is None:
        return None

    rule, line, similarity = best
    logger.info(
        "Fuzzy match at %.0f%%: rules row %s (%r) matched %r",
        similarity * 100,
        rule.row,
        rule.phrase,
        line,
    )
    return _result(rule, line, similarity)


def _similarity(rule_compressed: str, line_compressed: str) -> float:
    """Compare a rule against the START of a line, both already compressed.

    Only the leading characters of the line are compared, mirroring the exact
    pass. Without that, a long paragraph containing the phrase somewhere in the
    middle would score as a title line.

    A consequence worth knowing when writing rules: because the score is a
    fraction of the phrase's length, a LONGER phrase tolerates more OCR errors,
    not fewer. WAIVER OF DEFENDANT scores 0.82 against a badly scanned heading
    that WAIVER OF DEFENDANTS PRESENCE AT PRETRIAL CONFERENCE scores 0.94 on.
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

    Safety-net rules are skipped: they are meant to be shadowed by everything
    above them.
    """
    contenders = [rule for rule in rules if rule.document_type]

    for position, rule in enumerate(contenders):
        for earlier in contenders[:position]:
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