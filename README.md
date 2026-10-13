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
   Inbox. Red means DALYN has it or has filed it, so nobody should touch it;
   filed email moves to Deleted Items. Green names what went wrong
   and the email stays in the Inbox for a person, who can fix the cause and
   clear the label to have DALYN try again.
8. An email whose case is not in the case management system yet, often
   because the case is still being built, is labeled red "Waiting for STAC"
   and stays in the Inbox. DALYN searches for the case again at set times of
   day and files the email once the case appears. After 2 business days it
   goes green for a person. A case number from before last year goes green
   at once, since an old case would already exist and the number is likely
   mistyped.

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
| Tests | `pytest`, 313 tests, no network or live systems |

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
- **Waits, not sleeps, in the browser.** The case management system shows a
  loading spinner while it works, and acting before it clears can leave it
  stuck over the Save button. DALYN waits for the spinner to clear after
  each upload step instead of pausing a fixed time, and when Save cannot be
  pressed the error says what was in the way.

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

**Two machines.** A dev machine reads a test folder inside Deleted Items as a
stand-in for the Inbox, moves filed mail into a second test folder, and files
to the test system. Both folders are made by hand in Outlook and named in
`config.yaml` as `deleteditems/<folder name>`; each must also be on the
allowlist in `src/graph_client.py`. A dedicated live machine reads the Inbox
and files to the live system, and its `config.yaml` is never edited for
testing. Changes are tested on dev, pushed, then pulled on live with
`git pull`.

**Running live.** The live machine runs
`python main.py --watch 180 --hours 07:00-17:30` from the project folder:
a pass every 180 seconds, starting passes only on weekdays from 07:00 to
17:30, and sleeping outside those hours. The window must stay open and the
machine awake, because the browser is a visible window. Each pass takes up
to `max_messages_per_mailbox` emails from each enabled mailbox, oldest
first.

**Rechecking waiting emails.** The first pass at or after each time in
`recheck.times` (live: 09:00, 13:00, 17:00) also searches again for every
email labeled "Waiting for STAC". The first pass after starting always does,
in case a time was missed while DALYN was off. `--hours` must end after the
last recheck time, which is why live stops at 17:30 rather than 17:00.
`recheck.max_business_days` (live: 2) is how long an email waits before it
goes green, counted in weekdays from when it arrived.

**Usage**
```
python main.py --limit 5     # one pass, at most 5 emails from each mailbox
python main.py --watch 120   # a pass every 120 seconds until Ctrl+C
python main.py --watch 180 --hours 07:00-17:30   # same, weekdays 07:00-17:30 only
python main.py --recheck     # one pass that also searches again for every waiting email
python check_types.py        # check the config and the rules sheet against the system's type list
python -m pytest             # tests
```

**Safety switches** in `config.yaml`, each changed one at a time:

| Setting | Controls |
|---|---|
| `source_folder` | `inbox` live, a test folder inside Deleted Items on dev; reading the Inbox with tagging on the test system, or uploading Deleted Items or any folder inside it to live, is refused |
| `mailboxes[].enabled` | Which mailboxes are reachable at all |
| `stac.is_test_instance` | Must match the URL, or DALYN refuses to start |
| `stac.upload_enabled` | Upload, stopping before Save |
| `stac.save_enabled` | Press Save |
| `mailbox_actions.tag_enabled` | Label handled emails in Outlook |
| `mailbox_actions.skip_tagged` | Skip emails already labeled |
| `mailbox_actions.move_enabled` | Move filed emails to `done_folder`; needs `tag_enabled`, and is required when reading the Inbox with Save on |
| `alerts.enabled` | Email one person when a run stops, once per outage, and again when it recovers |
