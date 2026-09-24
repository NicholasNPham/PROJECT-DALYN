"""Data structures passed between DALYN's modules, plus the Manual Review tag wording.

No logic lives here. These are the shapes that graph_client, ocr, classifier and
stac hand back and forth.
"""

from dataclasses import dataclass, field
from datetime import datetime


class ReviewTag:
    """Exact wording of the Outlook categories DALYN applies.

    Staff see these strings in the message list, so they are defined once here
    rather than typed inline wherever a tag gets applied.
    """

    PROCESSING = "DALYN: Processing"
    INTERRUPTED = "DALYN: Interrupted"
    NO_ATTACHMENTS = "DALYN: No attachments"
    NON_PDF_ATTACHMENT = "DALYN: Non-PDF attachment"
    NO_UCN = "DALYN: No UCN"
    NO_MATCH = "DALYN: No match"
    TIE = "DALYN: Tie"
    LOW_MARGIN = "DALYN: Low margin"
    PARTIAL = "DALYN: Partial"
    STAC_ERROR = "DALYN: STAC error"


@dataclass
class ClassificationResult:
    """The outcome of scoring one PDF's text against the phrase sheet.

    A result with document_type set to None means no winner: either nothing
    matched or two types tied. Either way the email goes to Manual Review.
    """

    document_type: str | None = None
    document_subtype: str | None = None
    score: int = 0
    matched_phrases: list[str] = field(default_factory=list)
    runner_up_score: int = 0


@dataclass
class PDF:
    """One PDF attachment, carried through download, OCR, classification and STAC."""

    filename: str
    local_path: str
    attachment_id: str
    fingerprint: str = ""
    extracted_text: str = ""
    classification: ClassificationResult | None = None
    entered_into_stac: bool = False


@dataclass
class Email:
    """One message pulled from a monitored mailbox.

    message_id is Graph's identifier for the message and is what every later
    Graph call (tag, move, fetch attachments) refers back to. mailbox is the
    SMTP address it came from, since DALYN watches three.
    """

    message_id: str
    mailbox: str
    sender: str
    received: datetime
    has_attachments: bool
    body: str = ""
    ucn: str | None = None
    pdfs: list[PDF] = field(default_factory=list)