# DALYN: email to case-file document intake

## 1. What problem it solves

A prosecutor's office receives a steady stream of court filings by email, and
each one has to be identified and filed on the right case by hand. DALYN does
that filing automatically and hands a person only the emails it cannot be
sure about.

## 2. How it works

1. DALYN checks the case mailboxes for new emails with attachments.
2. It reads each PDF, using OCR when the document is a scan.
3. It works out what kind of document it is from a spreadsheet of rules that
   staff maintain themselves, with no code changes.
4. It finds the case number in the email or document, and checks that they
   agree.
5. It opens the case in the case management system and uploads the document
   under the right type.
6. Anything uncertain, such as a missing or conflicting case number, an
   unreadable scan, or a defendant name that does not match, goes to a person
   instead. DALYN never guesses.
7. Each email is labeled in Outlook by color, since staff work the same
   Inbox. Yellow means DALYN has it and nobody should touch it. Red means
   filed, and the email moves to Deleted Items. Green names what went wrong
   and the email stays in the Inbox for a person, who can fix the cause and
   clear the label to have DALYN try again.

It runs in stages, each switched on deliberately: read only, upload without
saving, then save.

## 3. Tech used

| Purpose | Tool |
|---|---|
| Language | Python 3.14 |
| Reading and labeling mail | Microsoft Graph API (`msal`, `requests`) |
| PDF text | `pypdf`, `PyMuPDF` |
| OCR for scans | Tesseract via `pytesseract`, `opencv` for cleanup |
| Rules spreadsheet | `openpyxl` |
| Case management web app | `selenium` |
| Secrets | Windows Credential Manager via `keyring` |
| Config | `PyYAML`, validated at startup with no defaults |
| Tests | `pytest`, 255 tests, no network or live systems |

Design choices worth knowing:
- **All or nothing per email.** Either every attachment is filed or none are,
  so a person never has to work out what DALYN already did.
- **Two kinds of error.** A problem with one document sends that email to
  review and the run continues. A problem with DALYN or a service stops the
  run.
- **Allowlists in code.** Graph calls are limited to the enabled mailboxes
  and the configured folder, and the test-instance flag must match the URL.
- **No personal data in output.** Logs and the review CSV hold case numbers
  and outcomes only, never names, subjects or email bodies.

## 4. Results

From a dry run on 6 Oct 2026 against the test case management system,
stopping before Save:

| Measure | Result |
|---|---|
| Emails handled end to end with no person | 18 of 25 (72%) |
| Emails sent to review | 7 of 25 (28%): 3 forwarded with no file, 2 cases missing from the test system, 1 non-PDF, 1 defendant mismatch |
| Attachments matched to a specific rule | 30 of 39 (77%) |
| Attachments with no rule, filed for review on the right case | 8 of 39 (21%) |
| Case number problems | 0 of 39 |
| Uploads that reached Save | 35 of 38 (92%), all 3 failures expected |
| New county formats: case number read correctly | 4 of 4 |

## 5. How to run it

**Setup**
1. Install Python 3.14 and run `pip install -r requirements.txt`.
2. Install the Tesseract binary and put it on PATH, or set `paths.tesseract`.
3. Copy `config/config.example.yaml` to `config/config.yaml` and fill it in.
4. Run `python set_credentials.py` to store the Graph secret and the
   case management login in Windows Credential Manager.

The Graph app registration needs `Mail.ReadWrite` and `MailboxSettings.ReadWrite`,
plus `Mail.Send` for alert emails.

**Two machines.** A dev machine reads Deleted Items as a stand-in for the
Inbox and files to the test system. A dedicated live machine reads the Inbox
and files to the live system, and its `config.yaml` is never edited for
testing. Changes are tested on dev, pushed, then pulled on live with
`git pull`.

**Running live.** Windows Task Scheduler runs `python main.py` from the
project folder every 15 minutes, with "Do not start a new instance" so runs
never overlap, and "Run only when user is logged on" because the browser is
a visible window. Each run takes up to `max_messages_per_mailbox` emails
from each enabled mailbox, oldest first.

**Usage**
```
python main.py --limit 5     # one pass, at most 5 emails from each mailbox
python main.py --watch 120   # a pass every 120 seconds until Ctrl+C
python check_types.py        # check the config and the rules sheet against the system's type list
python -m pytest             # tests
```

**Safety switches** in `config.yaml`, each changed one at a time:

| Setting | Controls |
|---|---|
| `source_folder` | `inbox` live, `deleteditems` on dev; reading the Inbox with tagging on the test system, or uploading Deleted Items to live, is refused |
| `mailboxes[].enabled` | Which mailboxes are reachable at all |
| `stac.is_test_instance` | Must match the URL, or DALYN refuses to start |
| `stac.upload_enabled` | Upload, stopping before Save |
| `stac.save_enabled` | Press Save |
| `mailbox_actions.tag_enabled` | Label handled emails in Outlook |
| `mailbox_actions.skip_tagged` | Skip emails already labeled |
| `mailbox_actions.move_enabled` | Move filed emails to `done_folder`; needs `tag_enabled`, and is required when reading the Inbox with Save on |
| `alerts.enabled` | Email one person when a run stops, once per outage, and again when it recovers |
