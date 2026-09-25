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

import base64
import csv
import sys
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
# Nothing in these columns may come from subject or body text.
CSV_COLUMNS = (
    "message_id",
    "received_utc",
    "attachment_name",
    "ucn",
    "ucn_source",
    "text_source",
    "char_count",
    "classified_type",
    "classified_subtype",
    "score",
    "matched_phrases",
    "outcome",
    "reason",
)

# A real PDF starts with %PDF-. The spec tolerates junk before the header,
# so look in the first KB rather than at byte 0. Content-Type from Graph is
# not trusted: senders' mail clients label PDFs as octet-stream constantly.
PDF_MAGIC = b"%PDF-"
PDF_HEADER_WINDOW = 1024


class Outcome:
    """What production DALYN would have done with one attachment.

    Precedence, highest first. An attachment gets exactly one outcome:
        TOO_LARGE, NON_PDF, UNREADABLE   no text, nothing to classify
        NO_UCN                           nowhere to enter it, even if classified
        TIE, NO_MATCH                    classifier gave no winner
        WOULD_ENTER                      clean winner and a UCN

    classified_type/subtype are filled whenever the classifier ran, even if
    a higher-precedence outcome wins, so every readable PDF gets its
    classification checked.
    """

    WOULD_ENTER = "WOULD_ENTER"
    NO_UCN = "NO_UCN"
    TIE = "TIE"
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