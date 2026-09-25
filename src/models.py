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
    """The outcome of matching one document against the rules sheet.

    document_type is None when no rule matched, or when the rule that matched
    was left deliberately blank on the sheet. Both mean Manual Review;
    matched_phrase tells the reviewer which of the two happened.
    """

    document_type: str | None = None
    document_subtype: str | None = None
    matched_phrase: str | None = None
    matched_line: str | None = None
    rule_row: int | None = None
    similarity: float = 0.0

    @property
    def is_fuzzy(self) -> bool:
        """True when this matched a mangled OCR line rather than the exact phrase."""
        return bool(self.matched_phrase) and self.similarity < 1.0

    @property
    def is_classified(self) -> bool:
        """True only when the document has both a Type and a Subtype for STAC."""
        return bool(self.document_type and self.document_subtype)


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