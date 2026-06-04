# Project-Dalyn----Email-to-STAC-Document-Processing-Pipeline

Automating document intake pipeline for the State Attorney's Office. Monitors two Outlook inboxes, OCRs incoming PDF attachments, classifies them against a phrase-scoring Excel sheet, and enters the results into the STAC web application via Selenium.

---

## What It Does

1. Monitors `email1` and `email2` for incoming emails
2. Saves PDF attachments to a temp folder on disk
3. OCRs each PDF using Tesseract to extract raw text
4. Classifies the document by scoring phrases against an Excel sheet to determine Type and Subtype
5. Logs into STAC and enters the document data automatically
6. Moves processed emails to a completed folder, or a manual review folder if classification fails

---

## Project Structure

```
ProjectDalyn/
├── config/
│   └── config.yaml          # Runtime configuration (accounts, paths, URLs, intervals)
├── data/                    # Persistent data storage
├── logs/                    # Log output
├── temp/                    # Temporary PDF storage during processing
├── src/
│   ├── __init__.py
│   ├── email_monitor.py     # Connects to Outlook via win32com, pulls unread emails
│   ├── ocr.py               # Extracts text from PDFs using PyMuPDF and Tesseract
│   ├── classifier.py        # Scores extracted text against Excel phrase sheet
│   ├── stac.py              # Selenium automation for STAC web application
│   ├── file_manager.py      # Handles temp file creation, cleanup, and archiving
│   ├── logger.py            # Logging setup and helpers
│   ├── models.py            # Dataclasses: Email, PDF, ClassificationResult, Config
│   └── exceptions.py        # Custom exception classes
├── tests/
│   ├── __init__.py
│   ├── test_classifier.py
│   └── test_ocr.py
├── .gitignore
├── key.py                   # Secrets and credentials (not committed to git)
├── key_template.py          # Template for key.py
├── main.py                  # Pipeline entry point
└── README.md
```

---

## Stack

| Purpose | Library |
|---|---|
| Outlook email access | `win32com` |
| PDF text extraction | `PyMuPDF` |
| OCR on scanned PDFs | `Tesseract` via `pytesseract` |
| Excel phrase scoring | `openpyxl` |
| STAC web automation | `Selenium` |
| Configuration | `PyYAML` |

---

## Configuration

Copy `config.yaml` and fill in your values:

```yaml
accounts:
  - 'email1'

outlook_profile: "Outlook"
folder_completed: "Completed"
folder_manual_review: "Manual Review"
excel_path: "C:/path/to/phrases.xlsx"
log_path: "C:/path/to/ProjectDalyn/logs"
polling_interval_minutes: 5
stac_url: "https://your-stac-url.com"
```

---

## Status

| Module | Status |
|---|--|
| `models.py` | Complete |
| `email_monitor.py` | Complete |
| `ocr.py` | Not started |
| `classifier.py` | In progress |
| `stac.py` | Not started |
| `file_manager.py` | Not started |
| `logger.py` | Not started |
| `exceptions.py` | Not started |
| `main.py` | Not started |
