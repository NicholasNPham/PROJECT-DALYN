from dataclasses import dataclass, field
from pathlib import Path

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

@dataclass
class ClassificationResult:
    """
    Represents the result of classifying a PDF document against the Excel phrase sheet.

    Attributes:
        document_type: The broad category of the document (e.g. DEMAND, DISCOVERY), or None if unclassified.
        document_subtype: The specific subcategory of the document, or None if unclassified.
        score: Confidence score from the phrase matching algorithm.
        matched_phrases: List of matched phrases
    """
    document_type: str | None = None
    document_subtype: str | None = None
    score: int = 0
    matched_phrases: list[str] = field(default_factory=list)

@dataclass
class Config:
    """
    Represents the application configuration loaded from config.yaml at startup.

    Attributes:
        accounts: List of Outlook email addresses to monitor (e.g. felonypolk@sao10.com).
        outlook_profile: Name of the Windows Outlook profile used by win32com to open Outlook.
        folder_completed: Name of the Outlook folder to move successfully processed emails into.
        folder_manual_review: Name of the Outlook folder to move emails that failed classification into.
        temp_folder:  Absolute path to the temporary directory where PDF attachments are saved during processing.
        excel_path: Absolute path to the Excel phrase scoring sheet used by the classifier.
        log_path: Absolute path to the directory where log files are written.
        polling_interval_minutes: How often the pipeline checks for new emails, in whole minutes.
        stac_url: Base URL of the STAC web application used by the Selenium automation.
        outlook_retry_attempts: Number of times to retry connecting to Outlook before giving up
        outlook_retry_delay_seconds: Number of seconds to wait between each Outlook connection retry
        email_move_error_color: Color of the error when it can not move to one of the finished folders
    """
    accounts: list[str]
    outlook_profile: str
    folder_completed: str
    folder_manual_review: str
    temp_folder: str
    excel_path: Path
    log_path: str
    polling_interval_minutes: int
    stac_url: str
    outlook_retry_attempts: int
    outlook_retry_delay_seconds: int
    email_move_error_color: str