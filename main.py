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
from exceptions import DocumentProblem, GraphAuthError, SystemProblem  # noqa: E402
from graph_client import GraphClient  # noqa: E402
from logger import get_logger, setup_logging  # noqa: E402

logger = get_logger("main")

# Reviewer CSV, one row per attachment. The reviewer joins to STAC on ucn.
# Nothing in these columns may come from subject or body text except the
# UCNs themselves. matched_line is deliberately left out: title lines can
# carry party names, and diag.py shows the line when someone needs it.
CSV_COLUMNS = (
    "mailbox",
    "message_id",
    "received_utc",
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
)

# A real PDF starts with %PDF-. The spec tolerates junk before the header,
# so look in the first KB rather than at byte 0. Content-Type from Graph is
# not trusted: senders' mail clients label PDFs as octet-stream constantly.
PDF_MAGIC = b"%PDF-"
PDF_HEADER_WINDOW = 1024

# Anything bigger is skipped rather than OCR'd. Arbitrary; revisit once the
# dry run shows what real filings weigh.
MAX_PDF_BYTES = 25 * 1024 * 1024


class Outcome:
    """What production DALYN would have done with one attachment.

    Precedence, highest first. An attachment gets exactly one outcome:
        TOO_LARGE, NON_PDF, UNREADABLE   no text, nothing to classify
        NO_UCN                           nowhere to enter it, even if classified
        NO_MATCH                         no rule matched, or only a safety net did
        WOULD_ENTER                      classified and a UCN

    classified_type/subtype are filled whenever the classifier produced them,
    even if NO_UCN wins, so every readable PDF gets its classification checked.
    """

    WOULD_ENTER = "WOULD_ENTER"
    NO_UCN = "NO_UCN"
    NO_MATCH = "NO_MATCH"
    UNREADABLE = "UNREADABLE"
    NON_PDF = "NON_PDF"
    TOO_LARGE = "TOO_LARGE"
    NO_FILES = "NO_FILES"


class TextSource:
    EMBEDDED = "embedded"
    OCR = "ocr"


EXIT_OK = 0
EXIT_SYSTEM_PROBLEM = 1
EXIT_CONFIG_PROBLEM = 2

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

    The document UCN prefers the retry text but falls back to the embedded
    text, since forced OCR reads only the first FORCE_OCR_PAGE_LIMIT pages.

    Returns:
        (text, text_source, ClassificationResult, retried, document_ucn)

    Raises:
        DocumentProblem: If the first extraction cannot produce usable text.
        SystemProblem: If Tesseract is unavailable.
    """
    text, source = ocr.extract_text(pdf_bytes, filename)
    result = classifier.classify(rules, text)
    document_ucn = ucn.find_ucn(text)

    if result.matched_phrase or source != TextSource.EMBEDDED:
        return text, source, result, False, document_ucn

    try:
        text, source = ocr.extract_text(pdf_bytes, filename, force_ocr=True)
    except DocumentProblem as error:
        logger.info("%s: OCR retry failed, keeping embedded result (%s)", filename, error)
        return text, source, result, True, document_ucn

    result = classifier.classify(rules, text)
    document_ucn = ucn.find_ucn(text) or document_ucn
    return text, source, result, True, document_ucn


def _finish(row: dict, outcome: str, reason: str = "") -> dict:
    row["outcome"] = outcome
    row["reason"] = reason
    return row


def _process_attachment(
    attachment: dict,
    rules: list,
    base_row: dict,
    subject_ucn: str | None,
    body_ucn: str | None,
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
    row["ucn_body"] = body_ucn or ""

    # Filled now so early-exit rows (NON_PDF, TOO_LARGE, UNREADABLE) still
    # carry a UCN for the reviewer to join on. The document UCN can only
    # fill it later if subject and body both came up empty.
    if subject_ucn or body_ucn:
        row["ucn"] = subject_ucn or body_ucn
        row["ucn_source"] = "subject" if subject_ucn else "body"

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
        text, source, result, retried, document_ucn = _read_and_classify(data, name, rules)
    except DocumentProblem as error:
        return _finish(row, Outcome.UNREADABLE, f"Could not read the PDF: {error}")

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

    # Each source searched on its own so the reviewer can see disagreement.
    # The chosen UCN follows ucn.find_ucn's precedence: subject, body, document.
    row["ucn_document"] = document_ucn or ""

    for source_name, found in (
        ("subject", subject_ucn),
        ("body", body_ucn),
        ("document", document_ucn),
    ):
        if found:
            row["ucn"] = found
            row["ucn_source"] = source_name
            break

    distinct = {found for found in (subject_ucn, body_ucn, document_ucn) if found}
    row["ucn_conflict"] = "YES" if len(distinct) > 1 else ""

    if not row["ucn"]:
        return _finish(row, Outcome.NO_UCN, "No UCN in subject, body preview or document")

    if not result.is_classified:
        if result.matched_phrase:
            reason = f"Only safety-net row {result.rule_row} matched, no Type or Subtype"
        else:
            reason = "No rule matched"
        return _finish(row, Outcome.NO_MATCH, reason)

    notes = []
    if result.is_fuzzy:
        notes.append("Fuzzy match, check by hand")
    if row["ucn_conflict"]:
        notes.append("UCN sources disagree")
    return _finish(row, Outcome.WOULD_ENTER, "; ".join(notes))


def _process_message(client: GraphClient, mailbox: str, message: dict, rules: list) -> list[dict]:
    """Return one CSV row per attachment, or a single NO_FILES row.

    Subject and bodyPreview are read here only to pull UCNs out of them, and
    are then dropped. They never reach the log or the CSV.

    SystemProblem from Graph is not caught. That includes a 404 on a message
    that vanished, until graph_client gets a GONE path; in a 50-message dry
    run against Deleted Items that is rare enough to accept failing loudly.
    """
    message_id = message["id"]
    received = message.get("receivedDateTime", "")

    subject_ucn = ucn.find_ucn("", subject=message.get("subject") or "")
    body_ucn = ucn.find_ucn("", body=message.get("bodyPreview") or "")

    base_row = {"mailbox": mailbox, "message_id": message_id, "received_utc": received}

    attachments = client.get_attachments(mailbox, message_id)

    if not attachments:
        # hasAttachments was true (list_messages filters on it) but nothing
        # usable came back. Usually an item attachment: a forwarded email
        # carrying the PDF, which get_attachments does not open yet.
        row = dict.fromkeys(CSV_COLUMNS, "")
        row.update(base_row)
        row["ucn_subject"] = subject_ucn or ""
        row["ucn_body"] = body_ucn or ""
        row["ucn"] = subject_ucn or body_ucn or ""
        row["ucn_source"] = "subject" if subject_ucn else ("body" if body_ucn else "")
        logger.info(
            "%s: no file attachments, likely a forwarded item | subject ucn %s | body ucn %s",
            received,
            subject_ucn or "-",
            body_ucn or "-",
        )
        return [_finish(row, Outcome.NO_FILES, "No file attachments; item attachments not read yet")]

    rows = [
        _process_attachment(attachment, rules, base_row, subject_ucn, body_ucn)
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

    outcomes = {row["outcome"] for row in rows}
    if len(rows) > 1 and Outcome.WOULD_ENTER in outcomes and len(outcomes) > 1:
        logger.info("%s: mixed outcomes across %s attachments (would be PARTIAL)", received, len(rows))

    return rows


def _write_csv(rows: list[dict], path: Path) -> None:
    # utf-8-sig so Excel opens it without mangling anything non-ASCII.
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
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

    order = "newest first" if config["newest_first"] else "oldest first"
    logger.info("Dry run: %s message(s) from %s, %s", len(messages), mailbox, order)

    rows: list[dict] = []
    try:
        for number, message in enumerate(messages, start=1):
            logger.info(
                "Message %s of %s, received %s",
                number,
                len(messages),
                message.get("receivedDateTime"),
            )
            rows.extend(_process_message(client, mailbox, message, rules))
    finally:
        # Written even on a crash, so a failure at message 40 still leaves
        # 39 messages of results to look at.
        _write_csv(rows, csv_path)
        logger.info("Wrote %s row(s) to %s", len(rows), csv_path)

    counts = Counter(row["outcome"] for row in rows)
    logger.info("Outcomes across %s attachment row(s):", len(rows))
    for outcome, count in counts.most_common():
        logger.info("  %-12s %s", outcome, count)

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