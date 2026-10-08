"""DALYN entry point.

One pass over the source folder of every enabled mailbox, up to a batch per
mailbox. Reads, OCRs, classifies, takes each email as far into STAC as the
stac switches allow, and logs. With mailbox_actions.tag_enabled it also puts
"DALYN: ..." Outlook categories on each email: yellow while DALYN has it, red
when filed, green when a person needs to look. With move_enabled, filed mail
moves to the done folder and everything else stays where it is.

Two outputs per run:
    logs/dalyn.log                   what happened, for Nick
    logs/decisions_<run stamp>.csv   one row per attachment, for the reviewer

Subject and body are read in memory for UCN extraction and, on Highlands and
Hardee cases, for finding STAC's defendant by name. They are never written to
either output because they carry defendant names.
"""

import argparse
import base64
import binascii
import csv
import shutil
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
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
from models import EmailDecision, Outcome, ReviewTag, StacResult  # noqa: E402
from tagging import (  # noqa: E402
    mark_processing,
    mark_queued,
    mark_saving,
    move_email,
    needs_handling,
    tag_email,
    was_interrupted,
)
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

# Counties this office files in. ucn.py reads Highlands and Hardee in their
# own AXMX long form and their short forms, so a case number from any other
# county is a cited case or a misread, and goes to a person.
#   53 Polk, 25 Hardee, 28 Highlands
SUPPORTED_COUNTY_CODES = frozenset({"53", "25", "28"})

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


class EmailType:
    SINGLE = "SINGLE"
    MULTIPLE = "MULTIPLE"
    NONE = "NONE"


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


def _read_and_classify(pdf_bytes: bytes, label: str, rules: list):
    """Extract text, classify, and find the document UCN. Mirrors review_batch.py.

    If the embedded text matched no rule at all, retry once through forced
    OCR and keep that result. Safety-net hits are not retried, because the
    batch 10 numbers were measured without that.

    label names the attachment in OCR's log lines and errors. It is a
    position such as "attachment 1 of 2", never the filename: senders name
    files after the defendant.

    Document UCNs are collected from both the embedded text and, if it ran,
    the OCR retry, since forced OCR reads only the first FORCE_OCR_PAGE_LIMIT
    pages and the embedded layer may have a corrupt caption.

    Returns:
        (text, text_source, ClassificationResult, retried, document_ucns,
        document_refs)
        document_ucns is every distinct full UCN found, in order.
        document_refs is every Highlands or Hardee case found, in any form.

    Raises:
        DocumentProblem: If the first extraction cannot produce usable text.
        SystemProblem: If Tesseract is unavailable.
    """
    text, source = ocr.extract_text(pdf_bytes, label)
    result = classifier.classify(rules, text)
    document_ucns = ucn.find_all(text)
    document_refs = ucn.find_document_refs(text, document_ucns)

    if result.matched_phrase or source != TextSource.EMBEDDED:
        return text, source, result, False, document_ucns, document_refs

    try:
        text, source = ocr.extract_text(pdf_bytes, label, force_ocr=True)
    except DocumentProblem as error:
        logger.info("%s: OCR retry failed, keeping embedded result (%s)", label, error)
        return text, source, result, True, document_ucns, document_refs

    result = classifier.classify(rules, text)
    retry_ucns = ucn.find_all(text)
    document_ucns = list(dict.fromkeys(retry_ucns + document_ucns))
    document_refs = list(dict.fromkeys(ucn.find_document_refs(text, retry_ucns) + document_refs))
    return text, source, result, True, document_ucns, document_refs


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
    label: str,
) -> dict:
    """Build one CSV row for one attachment. DocumentProblems become rows.

    label is the attachment's position, used in place of its filename
    anywhere the attachment is logged.

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
    email_choice, _ = ucn.choose_ucn(subject_ucn, body_ucns, [])
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
        text, source, result, retried, document_ucns, document_refs = _read_and_classify(
            data, label, rules
        )
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

    chosen, conflict = ucn.choose_ucn(subject_ucn, body_ucns, document_ucns, document_refs)
    row["ucn_document"] = " ".join(
        ucn.document_labels(document_ucns, document_refs, chosen[0] if chosen else None)
    )
    row["ucn_conflict"] = "YES" if conflict else ""
    if chosen:
        row["ucn"], row["ucn_source"] = chosen
    else:
        row["ucn"] = row["ucn_source"] = ""

    if conflict:
        return _finish(row, Outcome.UCN_CONFLICT, conflict)

    if not row["ucn"]:
        if document_refs:
            return _finish(
                row,
                Outcome.NO_UCN,
                "No UCN on the email, and the document names its case only in a "
                "short form with no county, so there is nothing to search by",
            )
        return _finish(row, Outcome.NO_UCN, "No UCN in subject, body or document")

    county = row["ucn"][:2]
    if county not in SUPPORTED_COUNTY_CODES:
        return _finish(
            row,
            Outcome.UCN_OTHER_COUNTY,
            f"Case number starts {county}, not Polk, Highlands or Hardee.",
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


def _email_row(base_row: dict, subject_ucn: str | None, body_ucns: list[str]) -> dict:
    """One row standing for a whole email, for NO_FILES, GONE and INTERRUPTED."""
    row = dict.fromkeys(CSV_COLUMNS, "")
    row.update(base_row)
    row["ucn_subject"] = subject_ucn or ""
    row["ucn_body"] = " ".join(body_ucns)
    chosen, _ = ucn.choose_ucn(subject_ucn, body_ucns, [])
    if chosen:
        row["ucn"], row["ucn_source"] = chosen
    return row


def _interrupted_rows(mailbox: str, message: dict) -> list[dict]:
    """The single INTERRUPTED row for an email held without being read.

    The case number comes from the subject and body only, which are already
    in hand. It is what the person needs to look the case up in STAC, and
    reading the attachments would mean doing the very work being held back.
    """
    subject, body_text = _email_text(message)
    base_row = {
        "mailbox": mailbox,
        "message_id": message["id"],
        "received_utc": message.get("receivedDateTime", ""),
    }
    row = _email_row(base_row, ucn.find_ucn("", subject=subject), ucn.find_all(body_text))
    return [_finish(row, Outcome.INTERRUPTED, "Still tagged Saving from an earlier run")]


def _email_text(message: dict) -> tuple[str, str]:
    """Return (subject, body) of a message. Held in memory only, never logged.

    Full body as plain text (list_messages asks for it). bodyPreview, the
    first 255 characters, only as a fallback if the body did not come back.
    """
    subject = message.get("subject") or ""
    body = (message.get("body") or {}).get("content") or message.get("bodyPreview") or ""
    return subject, body


def _process_message(
    client: GraphClient, mailbox: str, message: dict, rules: list, source_folder_id: str
) -> list[dict]:
    """Return one CSV row per attachment, or a single NO_FILES or GONE row.

    Subject and body are read here only to pull UCNs out of them, and
    are then dropped. They never reach the log or the CSV. _enter_in_stac
    reads them again from the message for the name check.

    The message is checked to still be in the source folder before anything
    is read. Anyone with access to the mailbox can move or delete mail while
    a pass is running; such a message is GONE and skipped, never an error.
    Other Graph failures (SystemProblem) are not caught and stop the pass.
    """
    message_id = message["id"]
    received = message.get("receivedDateTime", "")

    subject, body_text = _email_text(message)
    subject_ucn = ucn.find_ucn("", subject=subject)
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

    labels = [f"attachment {position} of {len(attachments)}" for position in range(1, len(attachments) + 1)]
    rows = [
        _process_attachment(attachment, rules, base_row, subject_ucn, body_ucns, label)
        for attachment, label in zip(attachments, labels)
    ]

    for row, label in zip(rows, labels):
        logger.info(
            "%s | %s | %s %s/%s | ucn %s (from %s) | subject %s | body %s | document %s",
            received,
            label,
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
    elif outcomes == [Outcome.INTERRUPTED]:
        email_type, decision, reason = (
            EmailType.NONE,
            EmailDecision.MANUAL_REVIEW,
            "Interrupted around Save on an earlier run; check STAC, then clear the tag",
        )
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


def _enter_in_stac(
    runner,
    rows: list[dict],
    work_dir: Path,
    email_number: int,
    message: dict,
    before_save: Callable[[], bool] | None = None,
) -> bool:
    """Put one email's documents into STAC and record the result on each row.

    Only called for an email already decided UPLOAD, so every enterable
    attachment goes or none does.

    The message is passed for its subject and body, which the Highlands and
    Hardee name check searches for STAC's defendant.

    before_save is asked right before each Save click. When it says no, a
    person moved the email mid-way, and it is recorded GONE: it is theirs
    now, and nothing of it was saved. If part of it was already saved, STAC
    raises PartiallyEntered instead and the email goes to a person.

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
    subject, body = _email_text(message)

    try:
        runner.enter_email(ucn_value, documents, document_text, subject, body, before_save=before_save)
    except MessageGone as error:
        for row in rows:
            row["email_decision"] = EmailDecision.GONE
            row["email_reason"] = f"Moved by a person while DALYN worked on it, not saved: {error}"
        logger.warning("Email %s: %s. Not saved.", email_number, error)
        # Not STAC's failure, so it does not count towards giving up.
        return True
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


def _log_mailbox_table(
    mailboxes: list[str], emails: list[dict], listed: Counter, moved: Counter
) -> None:
    """Log one line per mailbox: listed, upload, review, moved, gone.

    Counts emails, not attachments. Upload means the email went (or, before
    Save is on, would go) into STAC whole; review means a person has it. A
    mailbox that listed nothing still gets its row of zeros, which is the
    point: an empty folder and a broken one look different here.

    Args:
        mailboxes: Enabled mailboxes, in config order.
        emails: The first row of each email, carrying the email decision.
        listed: Emails listed per mailbox this pass.
        moved: Emails moved to the done folder per mailbox this pass.
    """
    decisions = Counter((row["mailbox"], row["email_decision"]) for row in emails)
    logger.info("By mailbox (emails):             listed upload review  moved   gone")
    for address in mailboxes:
        logger.info(
            "  %-30s %6s %6s %6s %6s %6s",
            address,
            listed.get(address, 0),
            decisions.get((address, EmailDecision.UPLOAD), 0),
            decisions.get((address, EmailDecision.MANUAL_REVIEW), 0),
            moved.get(address, 0),
            decisions.get((address, EmailDecision.GONE), 0),
        )


def run_dry_run(config: dict, limit: int | None = None) -> int:
    """One pass over the source folder of every enabled mailbox, in file order.

    Mailboxes are read one at a time to completion, each up to its own batch
    size. The messages are not merged into one list: nothing is gained by
    interleaving them.

    Args:
        config: From load_config.
        limit: Tighten max_messages_per_mailbox for this pass. Per mailbox
            too, so --limit 1 is one email from each enabled mailbox.

    Raises:
        SystemProblem: Rules sheet, Graph or Tesseract failure, or five
            consecutive STAC failures. Whatever rows were built before the
            failure are still written to the CSV.
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
        allowed_mailboxes=config["enabled_mailboxes"],
    )

    # Per mailbox, not shared. A shared budget was worked in list order, so a
    # busy first mailbox used all of it and the others went unread for as
    # long as it stayed busy. --limit tightens the same number rather than
    # introducing a second one.
    batch_size = config["max_messages_per_mailbox"]
    if limit is not None:
        batch_size = min(batch_size, limit)

    order = "newest first" if config["newest_first"] else "oldest first"
    logger.info(
        "Dry run: up to %s message(s) from each of %s, %s",
        batch_size,
        ", ".join(config["enabled_mailboxes"]),
        order,
    )

    work_dir = config["paths"]["temp"] / f"stac_{stamp}"

    actions = config["mailbox_actions"]
    tagged = 0
    moved = 0
    # Per mailbox, for the end-of-pass table. The rows cannot give these:
    # an empty folder leaves no rows, and moving is not recorded in them.
    listed_by_mailbox: Counter = Counter()
    moved_by_mailbox: Counter = Counter()
    # Emails that left the folder mid-run, by number. Listed at the end so a
    # person can check whoever took them filed them.
    gone: list[int] = []

    rows: list[dict] = []
    stac_failures = 0
    # Continuous across the whole pass, not reset per mailbox. Two emails both
    # numbered 1 would write colliding file names into work_dir, since the name
    # is built from the number, and would be ambiguous to talk about from the
    # CSV afterwards.
    email_number = 0

    try:
        # One browser for the whole pass, all mailboxes. Opened before the
        # first message so a bad password or an unreachable STAC stops the run
        # immediately rather than after fifty documents have been OCR'd for
        # nothing.
        with StacRunner(config) as runner:
            for mailbox in config["enabled_mailboxes"]:
                messages = client.list_messages(
                    mailbox,
                    days_back=config["days_back"],
                    max_messages=batch_size,
                    newest_first=config["newest_first"],
                    # Skipped in the listing rather than here, so handled
                    # mail does not use up the batch. needs_handling lets
                    # interrupted emails through for the check below.
                    keep=needs_handling if actions["skip_tagged"] else None,
                )
                source_folder_id = client.get_folder_id(mailbox, config["source_folder"])
                logger.info("%s: %s message(s) with attachments", mailbox, len(messages))
                listed_by_mailbox[mailbox] = len(messages)

                # Looked up before the first email, so a missing done folder
                # stops the pass before any work is done.
                done_folder_id = (
                    client.get_move_target_id(mailbox, actions["done_folder"])
                    if actions["move_enabled"]
                    else None
                )

                if actions["tag_enabled"]:
                    # Before the first email, so a missing MailboxSettings
                    # permission stops the pass before any work is done.
                    client.ensure_categories(mailbox, ReviewTag.colors())
                    # The whole batch goes yellow before DALYN starts on any
                    # of it, so staff sharing the folder know to leave it.
                    mark_queued(client, mailbox, messages)

                for position, message in enumerate(messages, start=1):
                    interrupted = actions["tag_enabled"] and was_interrupted(message)

                    email_number += 1
                    logger.info(
                        "%s message %s of %s (email %s), received %s",
                        mailbox,
                        position,
                        len(messages),
                        email_number,
                        message.get("receivedDateTime"),
                    )

                    if interrupted:
                        # The run that died was around the Save click and
                        # may have pressed it. Doing it again could file the
                        # document twice, so a person checks STAC first.
                        # Nothing is downloaded, and it stays where it is.
                        email_rows = _interrupted_rows(mailbox, message)
                        logger.warning(_decide_email(email_rows, email_number))
                        rows.extend(email_rows)
                        if tag_email(client, mailbox, message, email_rows):
                            tagged += 1
                        continue

                    before_save = None
                    if actions["tag_enabled"]:
                        mark_processing(client, mailbox, message, email_number)
                        # Bound now, called by STAC just before each Save.
                        # Checks a person has not taken the email, then
                        # marks it Saving.
                        before_save = lambda message=message, number=email_number: mark_saving(  # noqa: E731
                            client, mailbox, message, source_folder_id, number
                        )

                    email_rows = _process_message(
                        client, mailbox, message, rules, source_folder_id
                    )
                    summary = _decide_email(email_rows, email_number)

                    if email_rows[0]["email_decision"] == EmailDecision.UPLOAD:
                        if _enter_in_stac(runner, email_rows, work_dir, email_number, message, before_save):
                            stac_failures = 0
                        else:
                            stac_failures += 1
                        # The STAC result can turn an UPLOAD into a Manual
                        # Review, so the summary is re-read afterwards rather
                        # than before.
                        summary = _decide_email(email_rows, email_number, keep_decision=True)

                    logger.info(summary)
                    rows.extend(email_rows)

                    if actions["tag_enabled"] and tag_email(client, mailbox, message, email_rows):
                        tagged += 1

                    if email_rows[0]["email_decision"] == EmailDecision.GONE:
                        gone.append(email_number)

                    # Whether or not tag_email changed anything: an email
                    # tagged on an earlier pass still needs moving.
                    if done_folder_id and move_email(
                        client, mailbox, message, email_rows, done_folder_id, source_folder_id
                    ):
                        moved += 1
                        moved_by_mailbox[mailbox] += 1

                    if stac_failures >= MAX_CONSECUTIVE_STAC_FAILURES:
                        raise SystemProblem(
                            f"{stac_failures} emails in a row failed inside STAC. "
                            "Stopping rather than working through the rest of the "
                            "mailboxes against something that is not answering."
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

    # With one mailbox this repeats the totals above. With several it is the
    # only place an empty folder shows apart from one where everything failed.
    if len(config["enabled_mailboxes"]) > 1:
        _log_mailbox_table(config["enabled_mailboxes"], emails, listed_by_mailbox, moved_by_mailbox)

    if actions["tag_enabled"]:
        logger.info("Tagged: %s email(s)", tagged)
    if actions["move_enabled"]:
        logger.info("Moved: %s to the done folder", moved)

    if gone:
        # Numbers only, matching the CSV. Whoever took these has them now;
        # this is the prompt to check they were filed, not left.
        logger.warning(
            "%s email(s) left the folder mid-run: email %s. Check whoever took them filed them.",
            len(gone),
            ", ".join(str(number) for number in gone),
        )

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

    Unless mailbox_actions.skip_tagged is on, an email still in the source
    folder is read again every pass. With it on, an email DALYN has tagged is
    left alone. move_enabled takes filed mail out of the folder; mail for a
    person stays, tagged green. The rules
    sheet is reloaded each pass, so a sheet edit shows up on the next pass
    without restarting.

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
        help="Process at most N messages from each enabled mailbox.",
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