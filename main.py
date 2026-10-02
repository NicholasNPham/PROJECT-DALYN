"""DALYN entry point.

First iteration: one dry-run pass over Deleted Items of the configured
mailboxes. Reads, OCRs, classifies, logs, exits. Enters nothing into STAC,
tags nothing, moves nothing.

Two outputs per run:
    logs/dalyn.log                   what happened, for Nick
    logs/decisions_<run stamp>.csv   one row per attachment, for the reviewer

Subject and body are read in memory for UCN extraction only. They are never
written to either output because they carry defendant names.
"""

import argparse
import base64
import binascii
import csv
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# src/ modules import each other flat (from exceptions import ...), so the
# path insert above has to run before any of these.
import classifier  # noqa: E402
import ocr  # noqa: E402
import ucn  # noqa: E402
from config_loader import load_config  # noqa: E402
from exceptions import DocumentProblem, GraphAuthError, MessageGone, SystemProblem  # noqa: E402
from graph_client import GraphClient  # noqa: E402
from logger import get_logger, setup_logging  # noqa: E402
from stac import PartiallyEntered, SaveMayHaveHappened, StacRunner  # noqa: E402

logger = get_logger("main")

# Reviewer CSV, one row per attachment. The reviewer joins to STAC on ucn.
# Nothing in these columns may come from subject or body text except the
# UCNs themselves. ucn_body and ucn_document can hold several UCNs,
# space separated, when the text names more than one case. matched_line is deliberately left out: title lines can
# carry party names, and diag.py shows the line when someone needs it.
CSV_COLUMNS = (
    "mailbox",
    "message_id",
    "received_utc",
    "email_number",
    "email_type",
    "attachment",
    "email_decision",
    "email_reason",
    "attachment_name",
    "size_bytes",
    "ucn",
    "ucn_source",
    "ucn_subject",
    "ucn_body",
    "ucn_document",
    "ucn_conflict",
    "text_source",
    "ocr_retry",
    "char_count",
    "classified_type",
    "classified_subtype",
    "match",
    "matched_phrase",
    "rule_row",
    "outcome",
    "reason",
    "stac_result",
    "stac_reason",
)

# A real PDF starts with %PDF-. The spec tolerates junk before the header,
# so look in the first KB rather than at byte 0. Content-Type from Graph is
# not trusted: senders' mail clients label PDFs as octet-stream constantly.
PDF_MAGIC = b"%PDF-"
PDF_HEADER_WINDOW = 1024

# TEMPORARY, 1 Oct 2026. UCN_PATTERN in ucn.py ends in exactly two letters,
# but Hardee uses three-letter division codes, so a Hardee number comes back
# with its last letter missing: 25-2026-MM-000000-A000-XXW reads as
# ...A000XX. That is a WRONG case number, not a missing one, and nothing
# downstream can tell the difference. Until the pattern is fixed, anything
# whose county code is not Polk goes to a person instead.
#   53 Polk, 25 Hardee, 28 Highlands
# Remove this and the UCN_OTHER_COUNTY outcome once ucn.py handles three-
# letter divisions.
SUPPORTED_COUNTY_CODES = frozenset({"53"})

# Where any attachment goes that has a usable case number but matched no
# rule. Cover letters and similar one-off correspondence vary too much to
# write rules for, so they are entered on the case and sorted out inside STAC
# rather than handed back to a person in Outlook.
#
# Both halves are the string "PLS RVW", read off STAC's matrix dialog on
# 1 Oct 2026. It is one Type whose Subtype has the same name, not Type PLS
# with Subtype RVW. _apply_review_pair can override both from config, and
# config_loader will not let an override reach live STAC.
PLS_RVW_TYPE = "PLS RVW"
PLS_RVW_SUBTYPE = "PLS RVW"


def _apply_review_pair(config: dict) -> None:
    """Point the no-rule Type/Subtype at whatever this instance actually has.

    Module-level rather than threaded through every caller on purpose. Five
    functions reference this pair for one line of text each, and passing
    config into all of them to carry two strings is a worse trade than two
    globals written once before any message is read.
    """
    global PLS_RVW_TYPE, PLS_RVW_SUBTYPE
    stac = config.get("stac", {})
    PLS_RVW_TYPE = stac.get("review_type") or PLS_RVW_TYPE
    PLS_RVW_SUBTYPE = stac.get("review_subtype") or PLS_RVW_SUBTYPE

# Anything bigger is skipped rather than OCR'd. Arbitrary; revisit once the
# dry run shows what real filings weigh.
MAX_PDF_BYTES = 25 * 1024 * 1024


class Outcome:
    """What production DALYN would have done with one attachment.

    Precedence, highest first. An attachment gets exactly one outcome:
        TOO_LARGE, NON_PDF, UNREADABLE   no text, nothing to classify
        NO_UCN                           nowhere to enter it, even if classified
        UCN_OTHER_COUNTY                 the case number is not Polk, so it cannot
                                         be trusted until the Hardee pattern in
                                         ucn.py is fixed (temporary)
        UCN_CONFLICT                     the email and the document name different
                                         cases, or a document with no email UCN
                                         names several
        PLS_RVW                          no rule matched, but it has a usable case
                                         number, so it is entered under PLS/RVW
                                         for a person to sort out inside STAC
        WOULD_ENTER                      classified and a UCN

    GONE is email-level, like NO_FILES: the email left the source folder
    after it was listed (moved, deleted or purged by a person), so it was
    skipped without reading. Nobody needs to act on it; whoever moved it
    has it.

    classified_type/subtype are filled whenever the classifier produced them,
    even if NO_UCN wins, so every readable PDF gets its classification checked.
    """

    WOULD_ENTER = "WOULD_ENTER"
    NO_UCN = "NO_UCN"
    UCN_CONFLICT = "UCN_CONFLICT"
    UCN_OTHER_COUNTY = "UCN_OTHER_COUNTY"
    PLS_RVW = "PLS_RVW"
    UNREADABLE = "UNREADABLE"
    NON_PDF = "NON_PDF"
    TOO_LARGE = "TOO_LARGE"
    NO_FILES = "NO_FILES"
    GONE = "GONE"


class EmailDecision:
    """What production DALYN would do with the whole email.

    All-or-nothing, for now: an email is uploaded only if every attachment
    on it is WOULD_ENTER. If any one is not, the whole email goes to Manual
    Review and nothing on it is entered, so a person never has to work out
    which attachments DALYN already put in STAC. Switching to Partial is a
    change to _decide_email alone.
    """

    UPLOAD = "UPLOAD"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    GONE = "GONE"


class EmailType:
    SINGLE = "SINGLE"
    MULTIPLE = "MULTIPLE"
    NONE = "NONE"


class StacResult:
    """What STAC did with one attachment.

    ENTERED and REHEARSED both mean everything worked. The difference is only
    whether stac.save_enabled was on.
    """

    ENTERED = "ENTERED"
    REACHED_SAVE = "REACHED_SAVE"
    REHEARSED = "REHEARSED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    NOT_ATTEMPTED = ""


class TextSource:
    EMBEDDED = "embedded"
    OCR = "ocr"


EXIT_OK = 0
EXIT_SYSTEM_PROBLEM = 1
EXIT_CONFIG_PROBLEM = 2

# How many emails in a row may fail inside STAC before the pass gives up.
# One email with an odd page should not end a run; STAC being down should.
# Reset by any email that gets through.
MAX_CONSECUTIVE_STAC_FAILURES = 5

# --watch: stop after this many passes in a row fail with a SystemProblem.
# One Graph hiccup should not end a watch; three in a row means something
# is actually down.
MAX_CONSECUTIVE_FAILURES = 3
MIN_WATCH_SECONDS = 10


def _is_pdf(data: bytes) -> bool:
    return PDF_MAGIC in data[:PDF_HEADER_WINDOW]


def _read_and_classify(pdf_bytes: bytes, filename: str, rules: list):
    """Extract text, classify, and find the document UCN. Mirrors review_batch.py.

    If the embedded text matched no rule at all, retry once through forced
    OCR and keep that result. Safety-net hits are not retried, because the
    batch 10 numbers were measured without that.

    Document UCNs are collected from both the embedded text and, if it ran,
    the OCR retry, since forced OCR reads only the first FORCE_OCR_PAGE_LIMIT
    pages and the embedded layer may have a corrupt caption.

    Returns:
        (text, text_source, ClassificationResult, retried, document_ucns)
        document_ucns is every distinct UCN found, in order.

    Raises:
        DocumentProblem: If the first extraction cannot produce usable text.
        SystemProblem: If Tesseract is unavailable.
    """
    text, source = ocr.extract_text(pdf_bytes, filename)
    result = classifier.classify(rules, text)
    document_ucns = ucn.find_all(text)

    if result.matched_phrase or source != TextSource.EMBEDDED:
        return text, source, result, False, document_ucns

    try:
        text, source = ocr.extract_text(pdf_bytes, filename, force_ocr=True)
    except DocumentProblem as error:
        logger.info("%s: OCR retry failed, keeping embedded result (%s)", filename, error)
        return text, source, result, True, document_ucns

    result = classifier.classify(rules, text)
    document_ucns = list(dict.fromkeys(ucn.find_all(text) + document_ucns))
    return text, source, result, True, document_ucns


def _finish(row: dict, outcome: str, reason: str = "") -> dict:
    row["outcome"] = outcome
    row["reason"] = reason
    return row


def _process_attachment(
    attachment: dict,
    rules: list,
    base_row: dict,
    subject_ucn: str | None,
    body_ucns: list[str],
) -> dict:
    """Build one CSV row for one attachment. DocumentProblems become rows.

    SystemProblem is not caught here. A missing Tesseract is not a fact about
    this attachment and must stop the run.
    """
    row = dict.fromkeys(CSV_COLUMNS, "")
    row.update(base_row)

    name = attachment.get("name") or "(unnamed)"
    size = attachment.get("size") or 0
    row["attachment_name"] = name
    row["size_bytes"] = size
    row["ucn_subject"] = subject_ucn or ""
    row["ucn_body"] = " ".join(body_ucns)

    # Filled now so early-exit rows (NON_PDF, TOO_LARGE, UNREADABLE) still
    # carry a UCN for the reviewer to join on, when the email names one case.
    email_choice, _ = _choose_ucn(subject_ucn, body_ucns, [])
    if email_choice:
        row["ucn"], row["ucn_source"] = email_choice

    if size > MAX_PDF_BYTES:
        return _finish(row, Outcome.TOO_LARGE, f"Over {MAX_PDF_BYTES // 1024 // 1024} MB")

    encoded = attachment.get("contentBytes")
    if not encoded:
        return _finish(row, Outcome.UNREADABLE, "Graph returned no content for this attachment")

    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return _finish(row, Outcome.UNREADABLE, "Attachment content is not valid base64")

    if not _is_pdf(data):
        content_type = attachment.get("contentType") or "unknown"
        return _finish(row, Outcome.NON_PDF, f"No PDF header (Graph says {content_type})")

    try:
        text, source, result, retried, document_ucns = _read_and_classify(data, name, rules)
    except DocumentProblem as error:
        return _finish(row, Outcome.UNREADABLE, f"Could not read the PDF: {error}")

    # Carried out of band so STAC can write the file to disk later. Stripped
    # before the CSV is written, since these are megabytes of PDF.
    row["_bytes"] = data
    row["_text"] = text

    row["text_source"] = source
    row["ocr_retry"] = "YES" if retried else ""
    row["char_count"] = len(text)
    row["matched_phrase"] = result.matched_phrase or ""
    row["rule_row"] = result.rule_row or ""
    if result.matched_phrase:
        row["match"] = "FUZZY %.0f%%" % (result.similarity * 100) if result.is_fuzzy else "EXACT"
    if result.is_classified:
        row["classified_type"] = result.document_type
        row["classified_subtype"] = result.document_subtype

    chosen, conflict = _choose_ucn(subject_ucn, body_ucns, document_ucns)
    row["ucn_document"] = " ".join(document_ucns)
    row["ucn_conflict"] = "YES" if conflict else ""
    if chosen:
        row["ucn"], row["ucn_source"] = chosen
    else:
        row["ucn"] = row["ucn_source"] = ""

    if conflict:
        return _finish(row, Outcome.UCN_CONFLICT, conflict)

    if not row["ucn"]:
        return _finish(row, Outcome.NO_UCN, "No UCN in subject, body or document")

    county = row["ucn"][:2]
    if county not in SUPPORTED_COUNTY_CODES:
        return _finish(
            row,
            Outcome.UCN_OTHER_COUNTY,
            f"Case number starts {county}, not Polk. The pattern drops the last "
            "letter of a three-letter division, so this number may be wrong.",
        )

    if not result.is_classified:
        # It has a case number, so it can go on the right case. What it is
        # gets decided by a person inside STAC.
        if result.matched_phrase:
            reason = f"Only safety-net row {result.rule_row} matched, no Type or Subtype"
        else:
            reason = "No rule matched"
        row["classified_type"] = PLS_RVW_TYPE
        row["classified_subtype"] = PLS_RVW_SUBTYPE
        return _finish(
            row,
            Outcome.PLS_RVW,
            f"{reason}; entering under {PLS_RVW_TYPE}/{PLS_RVW_SUBTYPE} for review in STAC",
        )

    notes = []
    if result.is_fuzzy:
        notes.append("Fuzzy match, check by hand")
    if row["ucn_source"] == "document":
        notes.append("UCN from the document only")
    return _finish(row, Outcome.WOULD_ENTER, "; ".join(notes))


def _choose_ucn(
    subject_ucn: str | None, body_ucns: list[str], document_ucns: list[str]
) -> tuple[tuple[str, str] | None, str]:
    """Pick the UCN to enter under, or say why none can be trusted.

    Rules, agreed 1 Oct 2026:
        email UCN and document agree        use it
        email UCN, document has none        use the email UCN
        no email UCN, document has one      use the document UCN
        email UCN not in the document       conflict, Manual Review
        no email UCN, document has several  conflict, Manual Review
        nothing anywhere                    no UCN

    "Email UCN" is the subject's. If the subject has none, the body's, but
    only when the body names exactly one case: a full body often carries a
    reply chain or forwarded history that mentions other cases, so a body
    naming several is a conflict. When the subject has a UCN the body is
    recorded but decides nothing, for the same reason.

    A document that cites other cases still agrees if the email's UCN is
    anywhere in it.

    Returns:
        ((ucn, source) or None, conflict reason or "").
    """
    if subject_ucn:
        email_ucn, email_source = subject_ucn, "subject"
    elif len(body_ucns) == 1:
        email_ucn, email_source = body_ucns[0], "body"
    elif len(body_ucns) > 1:
        return None, (
            f"No UCN in the subject and the body names {len(body_ucns)} cases: "
            f"{', '.join(body_ucns)}"
        )
    else:
        email_ucn, email_source = None, ""

    if email_ucn:
        if document_ucns and email_ucn not in document_ucns:
            return None, (
                f"Email names {email_ucn}, document names "
                f"{', '.join(document_ucns)}"
            )
        return (email_ucn, email_source), ""

    if len(document_ucns) > 1:
        return None, (
            f"No UCN on the email and the document names {len(document_ucns)} cases: "
            f"{', '.join(document_ucns)}"
        )

    if document_ucns:
        return (document_ucns[0], "document"), ""

    return None, ""


def _email_row(base_row: dict, subject_ucn: str | None, body_ucns: list[str]) -> dict:
    """One row standing for a whole email, for NO_FILES and GONE."""
    row = dict.fromkeys(CSV_COLUMNS, "")
    row.update(base_row)
    row["ucn_subject"] = subject_ucn or ""
    row["ucn_body"] = " ".join(body_ucns)
    chosen, _ = _choose_ucn(subject_ucn, body_ucns, [])
    if chosen:
        row["ucn"], row["ucn_source"] = chosen
    return row


def _process_message(
    client: GraphClient, mailbox: str, message: dict, rules: list, source_folder_id: str
) -> list[dict]:
    """Return one CSV row per attachment, or a single NO_FILES or GONE row.

    Subject and body are read here only to pull UCNs out of them, and
    are then dropped. They never reach the log or the CSV.

    The message is checked to still be in the source folder before anything
    is read. Anyone with access to the mailbox can move or delete mail while
    a pass is running; such a message is GONE and skipped, never an error.
    Other Graph failures (SystemProblem) are not caught and stop the pass.
    """
    message_id = message["id"]
    received = message.get("receivedDateTime", "")

    subject_ucn = ucn.find_ucn("", subject=message.get("subject") or "")
    # Full body as plain text (list_messages asks for it). bodyPreview, the
    # first 255 characters, only as a fallback if the body did not come back.
    body_text = (message.get("body") or {}).get("content") or message.get("bodyPreview") or ""
    body_ucns = ucn.find_all(body_text)

    base_row = {"mailbox": mailbox, "message_id": message_id, "received_utc": received}

    try:
        current_folder = client.get_parent_folder_id(mailbox, message_id)
        if current_folder != source_folder_id:
            raise MessageGone("moved to another folder")
        attachments = client.get_attachments(mailbox, message_id)
    except MessageGone as error:
        logger.info("%s: GONE, skipped (%s)", received, error)
        row = _email_row(base_row, subject_ucn, body_ucns)
        return [_finish(row, Outcome.GONE, f"Left the folder before DALYN read it: {error}")]

    if not attachments:
        # hasAttachments was true (list_messages filters on it) but nothing
        # usable came back. Usually an item attachment: a forwarded email
        # carrying the PDF, which get_attachments does not open yet.
        row = _email_row(base_row, subject_ucn, body_ucns)
        logger.info(
            "%s: no file attachments, likely a forwarded item | subject ucn %s | body ucn %s",
            received,
            subject_ucn or "-",
            " ".join(body_ucns) or "-",
        )
        return [_finish(row, Outcome.NO_FILES, "No file attachments; item attachments not read yet")]

    rows = [
        _process_attachment(attachment, rules, base_row, subject_ucn, body_ucns)
        for attachment in attachments
    ]

    for row in rows:
        logger.info(
            "%s | %s | %s %s/%s | ucn %s (from %s) | subject %s | body %s | document %s",
            received,
            row["attachment_name"],
            row["outcome"],
            row["classified_type"] or "-",
            row["classified_subtype"] or "-",
            row["ucn"] or "-",
            row["ucn_source"] or "none",
            row["ucn_subject"] or "-",
            row["ucn_body"] or "-",
            row["ucn_document"] or "-",
        )

    return rows


# Outcomes that would be entered into STAC.
ENTERABLE = frozenset({Outcome.WOULD_ENTER, Outcome.PLS_RVW})


def _decide_email(rows: list[dict], email_number: int, keep_decision: bool = False) -> str:
    """Stamp the email-level decision onto every row of one email.

    Returns the one-line summary that gets logged for the email.
    """
    if keep_decision:
        # STAC has already had its say and may have downgraded the email.
        # Re-deciding from the attachment outcomes would undo that.
        decision = rows[0]["email_decision"]
        reason = rows[0]["email_reason"]
        total = len(rows) if rows[0]["attachment"] else 0
        label = (
            f"{rows[0]['email_type']} ({total} attachment{'s' if total != 1 else ''})"
            if total
            else rows[0]["email_type"]
        )
        return f"Email {email_number}: {label} -> {decision}. {reason}"

    outcomes = [row["outcome"] for row in rows]

    if outcomes == [Outcome.GONE]:
        email_type, decision, reason = EmailType.NONE, EmailDecision.GONE, "Left the folder before it was read"
    elif outcomes == [Outcome.NO_FILES]:
        email_type, decision, reason = EmailType.NONE, EmailDecision.MANUAL_REVIEW, "No file attachments"
    else:
        email_type = EmailType.SINGLE if len(rows) == 1 else EmailType.MULTIPLE
        blocking = [row for row in rows if row["outcome"] not in ENTERABLE]
        if not blocking:
            decision = EmailDecision.UPLOAD
            routed = sum(1 for row in rows if row["outcome"] == Outcome.PLS_RVW)
            if len(rows) == 1:
                reason = "Attachment would enter"
                if routed:
                    reason += f" under {PLS_RVW_TYPE}/{PLS_RVW_SUBTYPE}"
            else:
                reason = (
                    "Both attachments would enter" if len(rows) == 2
                    else f"All {len(rows)} attachments would enter"
                )
                if routed:
                    reason += f" ({routed} under {PLS_RVW_TYPE}/{PLS_RVW_SUBTYPE})"
        else:
            decision = EmailDecision.MANUAL_REVIEW
            counts = Counter(row["outcome"] for row in blocking)
            held = ", ".join(f"{count} {outcome}" for outcome, count in counts.most_common())
            if len(rows) == 1:
                reason = f"Attachment is {rows[0]['outcome']}"
            else:
                reason = f"{len(rows) - len(blocking)} of {len(rows)} would enter; held back by {held}"

    total = len(rows) if email_type != EmailType.NONE else 0
    for position, row in enumerate(rows, start=1):
        row["email_number"] = email_number
        row["email_type"] = email_type
        row["attachment"] = f"{position} of {total}" if total else ""
        row["email_decision"] = decision
        row["email_reason"] = reason

    label = f"{email_type} ({total} attachment{'s' if total != 1 else ''})" if total else email_type
    return f"Email {email_number}: {label} -> {decision}. {reason}"


def _stac_documents(rows: list[dict], work_dir: Path, email_number: int) -> list:
    """Write this email's enterable attachments to disk for Selenium.

    Selenium uploads from a file path, so the bytes held in memory have to
    land somewhere first. Files are named with the email number so two
    attachments called Order.pdf on different emails cannot collide.

    Returns:
        (path, document_type, subtype) per attachment, in email order.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    documents = []

    for position, row in enumerate(rows, start=1):
        if row["outcome"] not in ENTERABLE:
            continue
        target = work_dir / f"{email_number:04d}_{position}_{row['attachment_name'].strip()}"
        target.write_bytes(row["_bytes"])
        documents.append((target, row["classified_type"], row["classified_subtype"]))

    return documents


def _enter_in_stac(runner, rows: list[dict], work_dir: Path, email_number: int) -> bool:
    """Put one email's documents into STAC and record the result on each row.

    Only called for an email already decided UPLOAD, so every enterable
    attachment goes or none does.

    A STAC failure is recorded against this email and the pass carries on.
    One email with a page that will not settle should not end a run, and
    stopping would leave the rest of the Inbox untouched with no record of
    why. The caller counts consecutive failures and stops when STAC is
    plainly down rather than merely awkward.

    Returns:
        True when the email got through, False when it did not.
    """
    documents = _stac_documents(rows, work_dir, email_number)
    if not documents:
        return True

    enterable = [row for row in rows if row["outcome"] in ENTERABLE]
    ucn_value = enterable[0]["ucn"]
    # Any one document's text will do for the defendant check; the longest is
    # the most likely to carry a readable caption.
    document_text = max((row.get("_text") or "" for row in enterable), key=len)

    try:
        runner.enter_email(ucn_value, documents, document_text)
    except SaveMayHaveHappened as error:
        _mark_stac(enterable, StacResult.UNKNOWN, str(error))
        logger.error("Email %s: %s", email_number, error)
        return False
    except PartiallyEntered as error:
        _mark_stac(enterable, StacResult.FAILED, str(error))
        logger.error("Email %s: %s", email_number, error)
        return False
    except DocumentProblem as error:
        _mark_stac(enterable, StacResult.FAILED, str(error))
        logger.info("Email %s: not filed, %s", email_number, error)
        # The document's own problem, not STAC's. It does not count towards
        # giving up, because the next email may be perfectly fine.
        return True
    except SystemProblem as error:
        _mark_stac(enterable, StacResult.FAILED, f"STAC failed: {error}")
        logger.error("Email %s: STAC failed, moving on. %s", email_number, error)
        return False
    else:
        session = runner.session
        if session.save_enabled:
            done = StacResult.ENTERED
        elif session.upload_enabled:
            done = StacResult.REACHED_SAVE
        else:
            done = StacResult.REHEARSED
        _mark_stac(enterable, done, "")
        return True


def _mark_stac(rows: list[dict], result: str, reason: str) -> None:
    """Record the STAC outcome on every row, and downgrade the email if it failed."""
    for row in rows:
        row["stac_result"] = result
        row["stac_reason"] = reason

    if result in (StacResult.FAILED, StacResult.UNKNOWN):
        for row in rows:
            row["email_decision"] = EmailDecision.MANUAL_REVIEW
            row["email_reason"] = reason


def _write_csv(rows: list[dict], path: Path) -> None:
    # utf-8-sig so Excel opens it without mangling anything non-ASCII.
    # extrasaction drops the _bytes and _text keys, which are megabytes of
    # PDF and the document's full text, neither of which belongs in a CSV.
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_dry_run(config: dict, limit: int | None = None) -> int:
    """One pass over Deleted Items of the first configured mailbox.

    Args:
        config: From load_config.
        limit: Process only the first N messages after sorting and after
            attachment-less mail is dropped. With newest_first, --limit 1
            is the newest email that has attachments.

    Raises:
        SystemProblem: Rules sheet, Graph or Tesseract failure. Whatever
            rows were built before the failure are still written.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = config["paths"]["logs"] / f"decisions_{stamp}.csv"

    rules_path = config["paths"]["excel"]
    rules = classifier.load_rules(rules_path)
    modified = datetime.fromtimestamp(rules_path.stat().st_mtime)
    logger.info(
        "Rules sheet %s: %s rules, last modified %s",
        rules_path,
        len(rules),
        f"{modified:%Y-%m-%d %H:%M}",
    )

    graph = config["graph"]
    client = GraphClient(
        tenant_id=graph["tenant_id"],
        client_id=graph["client_id"],
        client_secret=graph["client_secret"],
        allowed_mailboxes=config["mailboxes"],
    )

    # First iteration reads one mailbox. The allowlist can hold all three;
    # the first entry is the one read, so keep felony Polk at the top.
    mailbox = config["mailboxes"][0]

    messages = client.list_messages(
        mailbox,
        days_back=config["days_back"],
        max_messages=config["max_messages"],
        newest_first=config["newest_first"],
    )
    if limit is not None:
        messages = messages[:limit]

    source_folder_id = client.get_folder_id(mailbox, config["source_folder"])

    order = "newest first" if config["newest_first"] else "oldest first"
    logger.info("Dry run: %s message(s) from %s, %s", len(messages), mailbox, order)

    work_dir = config["paths"]["temp"] / f"stac_{stamp}"

    rows: list[dict] = []
    stac_failures = 0
    try:
        # One browser for the whole pass. Opened before the first message so a
        # bad password or an unreachable STAC stops the run immediately rather
        # than after fifty documents have been OCR'd for nothing.
        with StacRunner(config) as runner:
            for number, message in enumerate(messages, start=1):
                logger.info(
                    "Message %s of %s, received %s",
                    number,
                    len(messages),
                    message.get("receivedDateTime"),
                )
                email_rows = _process_message(client, mailbox, message, rules, source_folder_id)
                summary = _decide_email(email_rows, number)

                if email_rows[0]["email_decision"] == EmailDecision.UPLOAD:
                    if _enter_in_stac(runner, email_rows, work_dir, number):
                        stac_failures = 0
                    else:
                        stac_failures += 1
                    # The STAC result can turn an UPLOAD into a Manual Review,
                    # so the summary is re-read afterwards rather than before.
                    summary = _decide_email(email_rows, number, keep_decision=True)

                logger.info(summary)
                rows.extend(email_rows)

                if stac_failures >= MAX_CONSECUTIVE_STAC_FAILURES:
                    raise SystemProblem(
                        f"{stac_failures} emails in a row failed inside STAC. Stopping "
                        "rather than working through the rest of the mailbox against "
                        "something that is not answering."
                    )
    finally:
        # Real case documents. Gone whether the pass finished or crashed.
        shutil.rmtree(work_dir, ignore_errors=True)
        # Written even on a crash, so a failure at message 40 still leaves
        # 39 messages of results to look at.
        _write_csv(rows, csv_path)
        logger.info("Wrote %s row(s) to %s", len(rows), csv_path)

    # One entry per email: its first row carries the email-level fields.
    emails = [row for row in rows if not row["attachment"] or row["attachment"].startswith("1 of ")]
    email_counts = Counter((row["email_type"], row["email_decision"]) for row in emails)
    logger.info("Emails: %s", len(emails))
    for email_type in (EmailType.SINGLE, EmailType.MULTIPLE, EmailType.NONE):
        for decision in (EmailDecision.UPLOAD, EmailDecision.MANUAL_REVIEW, EmailDecision.GONE):
            count = email_counts.get((email_type, decision))
            if count:
                logger.info("  %-8s %-13s %s", email_type, decision, count)

    counts = Counter(row["outcome"] for row in rows)
    logger.info("Attachments: %s", len(rows))
    for outcome, count in counts.most_common():
        logger.info("  %-12s %s", outcome, count)

    stac_counts = Counter(row["stac_result"] for row in rows if row["stac_result"])
    if stac_counts:
        logger.info("STAC:")
        for result, count in stac_counts.most_common():
            logger.info("  %-12s %s", result, count)

    # The sheet only improves if someone looks at what fell through it.
    # Nothing flags these in production, so say it here.
    routed = counts.get(Outcome.PLS_RVW, 0)
    if routed:
        logger.info(
            "%s attachment(s) matched no rule and would go in under %s/%s. "
            "Check them in the CSV for titles worth a rule.",
            routed,
            PLS_RVW_TYPE,
            PLS_RVW_SUBTYPE,
        )

    return EXIT_OK


def _watch(config: dict, limit: int | None, interval: int) -> int:
    """Run a dry-run pass every `interval` seconds until Ctrl+C.

    Nothing is moved, so the same newest email is read again every pass
    until a newer one lands. The rules sheet is reloaded each pass, so a
    sheet edit shows up on the next pass without restarting.

    A failed credential stops the watch at once. Any other SystemProblem is
    logged and retried next pass, up to MAX_CONSECUTIVE_FAILURES in a row.
    """
    failures = 0
    pass_number = 0
    logger.info("Watching: one pass every %s seconds. Ctrl+C to stop.", interval)

    try:
        while True:
            pass_number += 1
            logger.info("===== Pass %s =====", pass_number)
            try:
                run_dry_run(config, limit=limit)
                failures = 0
            except GraphAuthError:
                raise
            except SystemProblem as error:
                failures += 1
                logger.error("Pass %s failed (%s in a row): %s", pass_number, failures, error)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error("Stopping after %s failed passes in a row", failures)
                    return EXIT_SYSTEM_PROBLEM

            logger.info("Next pass in %s seconds", interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Watch stopped by user after %s pass(es)", pass_number)
        return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DALYN dry run over Deleted Items.")
    parser.add_argument(
        "--limit",
        type=int,
        help="Process only the first N messages (newest first in a dry run).",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        const=120,
        type=int,
        metavar="SECONDS",
        help="Repeat a pass every SECONDS (default 120) until Ctrl+C.",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.watch is not None and args.watch < MIN_WATCH_SECONDS:
        parser.error(f"--watch must be at least {MIN_WATCH_SECONDS} seconds")

    # Logging is not set up yet, because the log folder comes from config.
    try:
        config = load_config()
    except SystemProblem as error:
        print(f"Config problem: {error}", file=sys.stderr)
        return EXIT_CONFIG_PROBLEM

    setup_logging(config["paths"]["logs"])
    _apply_review_pair(config)

    if (PLS_RVW_TYPE, PLS_RVW_SUBTYPE) != ("PLS RVW", "PLS RVW"):
        logger.warning(
            "Unclassified attachments will be filed under %s/%s, not "
            "'PLS RVW'/'PLS RVW'. This is a stand-in, set in config.",
            PLS_RVW_TYPE,
            PLS_RVW_SUBTYPE,
        )

    if not config["dry_run"]:
        logger.error(
            "dry_run is false in config.yaml, but this build can only dry run. "
            "Refusing to start rather than let anyone think it went live."
        )
        return EXIT_CONFIG_PROBLEM

    try:
        ocr.configure_tesseract(config["paths"].get("tesseract"))
        if args.watch is None:
            return run_dry_run(config, limit=args.limit)
        return _watch(config, args.limit, args.watch)
    except GraphAuthError as error:
        logger.error("Graph sign-in failed. Check the client secret has not expired. %s", error)
        return EXIT_SYSTEM_PROBLEM
    except SystemProblem as error:
        logger.error("Stopped: %s", error)
        return EXIT_SYSTEM_PROBLEM
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return EXIT_SYSTEM_PROBLEM
    except Exception:
        logger.exception("Unexpected error. This is a bug, not a document problem.")
        return EXIT_SYSTEM_PROBLEM


if __name__ == "__main__":
    sys.exit(main())