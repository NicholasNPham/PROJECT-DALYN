"""Data structures passed between DALYN's modules, the outcome names, and the tag wording.

No logic lives here. These are the shapes that graph_client, ocr, classifier and
stac hand back and forth.
"""

from dataclasses import dataclass, field
from datetime import datetime


class ReviewTag:
    """Exact wording of the Outlook categories DALYN applies.

    Staff see these strings in the message list, so they are defined once here
    rather than typed inline wherever a tag gets applied.

    Every one starts with PREFIX. That is how DALYN tells its own categories
    from the ones staff set, both when replacing its tags on a re-run and when
    skip_tagged decides an email was already handled. Never add a tag without
    the prefix.

    An uploaded email gets one of the three STAC outcomes, plus NO_RULE if any
    attachment went in under the review pair. A Manual Review email gets one
    tag per distinct reason it was held back.

    Colors tell staff what to do: red, DALYN has it or has filed it, so
    nobody touches it; green, a person needs to look. Staff work the Inbox
    alongside DALYN, so the color is the whole instruction. Filed mail leaves
    the Inbox, so red there is nearly always an email DALYN is working on.

    Which tags mean "still in progress" is in_progress(), never read from the
    color: red covers both in progress and filed (Nick, 9 Oct 2026).
    """

    PREFIX = "DALYN: "

    # Graph's names for Outlook's category palette.
    RED_COLOR = "preset0"
    GREEN_COLOR = "preset4"

    # In progress, in the order an email passes through them. The whole batch is
    # tagged QUEUED before DALYN starts on any of it, so staff know which
    # emails to leave alone. PROCESSING replaces it on the one email being
    # worked, and SAVING replaces that just before Save is pressed.
    #
    # After a crash, only SAVING needs a person: up to then nothing is on the
    # case, so a leftover QUEUED or PROCESSING email is simply done again.
    QUEUED = "DALYN: Queued"
    PROCESSING = "DALYN: Processing"
    SAVING = "DALYN: Saving"

    # Found still carrying SAVING on a later pass: a run died around the
    # Save click, so the document may or may not be on the case. Held for a
    # person, who checks STAC and clears the tag to let DALYN take it again.
    INTERRUPTED = "DALYN: Interrupted"

    # Uploaded. Which one depends on how far the stac switches let it go, so
    # a tag never says Filed when Save was never pressed.
    FILED = "DALYN: Filed"
    READY_TO_SAVE = "DALYN: Ready to save"
    REHEARSED = "DALYN: Rehearsed"
    NO_RULE = "DALYN: No rule matched"

    # Manual Review, decided before STAC.
    NO_ATTACHMENTS = "DALYN: No attachments"
    CANT_READ = "DALYN: Can't read"
    NO_UCN = "DALYN: No case number"
    UCN_CONFLICT = "DALYN: Case number conflict"
    OTHER_COUNTY = "DALYN: Other county"

    # Manual Review, decided by STAC. MAY_BE_SAVED is the one a person must
    # check inside STAC before doing anything, because the document may
    # already be on the case.
    #
    # No commas or semicolons in any tag: Outlook uses them to separate
    # categories, and Graph rejects the name with a 400. The first tagging
    # run on 7 Oct 2026 stopped on "Check STAC, may be saved".
    STAC_FAILED = "DALYN: STAC could not file"
    MAY_BE_SAVED = "DALYN: Check STAC first"

    @classmethod
    def all(cls) -> list[str]:
        """Return every tag, for creating them in a mailbox's master list."""
        return [
            value
            for name, value in vars(cls).items()
            if name.isupper()
            and name != "PREFIX"
            and isinstance(value, str)
            and value.startswith(cls.PREFIX)
        ]

    @classmethod
    def in_progress(cls) -> frozenset[str]:
        """The tags meaning DALYN still has the email: Queued, Processing, Saving.

        A run that stopped can leave any of these behind, and such an email has
        to come back through skip_tagged to be finished or held. Listed by name
        so a color change can never strand one.
        """
        return frozenset({cls.QUEUED, cls.PROCESSING, cls.SAVING})

    @classmethod
    def colors(cls) -> dict[str, str]:
        """Return {tag: Graph color} for every tag.

        Green unless listed otherwise, so a tag added later without a color
        decision shows as one a person must look at, not as done.
        """
        red = cls.in_progress() | {cls.FILED, cls.NO_RULE}
        return {tag: cls.RED_COLOR if tag in red else cls.GREEN_COLOR for tag in cls.all()}

    @classmethod
    def is_dalyn(cls, category: str) -> bool:
        """True when an Outlook category is one of DALYN's own."""
        return category.startswith(cls.PREFIX)


class Outcome:
    """What production DALYN would have done with one attachment.

    Precedence, highest first. An attachment gets exactly one outcome:
        TOO_LARGE, NON_PDF, UNREADABLE   no text, nothing to classify
        NO_UCN                           nowhere to enter it, even if classified
        UCN_OTHER_COUNTY                 the case number is not Polk, Highlands
                                         or Hardee
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

    INTERRUPTED is email-level too: an earlier run died partway through this
    email with Save on, so it is held for a person without being read.

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
    INTERRUPTED = "INTERRUPTED"


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