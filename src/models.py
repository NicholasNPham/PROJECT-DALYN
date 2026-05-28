from dataclasses import dataclass, field

@dataclass
class Email:
    """
    Represents an incoming email retrieved from a monitored Outlook account.

    Attributes:
        message_id: Unique Outlook EntryID for the mail item.
        account: The monitored account that received the email (e.g. felonypolk@sao10.com).
        sender: Email address of the sender.
        body: Plain text body of the email, or None if empty.
        subject: Subject line of the email, or None if missing.
        ucn: Unified case number parsed from the email content, or None if not found.
        attachments: List of file paths to saved PDF attachments.
    """
    message_id: str
    account: str
    sender: str
    body: str | None
    subject : str | None
    ucn: str | None
    attachments: list[str] = field(default_factory=list) # this creates a new list object every time a new email object instantiates

@dataclass
class PDF:
    """
    Represents a PDF attachment extracted from a monitored email.

    Attributes:
        file_name: The filename of the PDF (e.g. motion_to_dismiss.pdf).
        file_path: Absolute path to the saved PDF on disk.
        ucn: Unified case number parsed from the document, or None if not found.
        extracted_text: Raw text extracted via OCR, or None if OCR has not run yet.
    """
    file_name: str
    file_path: str
    ucn: str | None
    extracted_text: str | None
