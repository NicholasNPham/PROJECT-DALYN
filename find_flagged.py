"""Throwaway: pull emails back out of the mailbox from a dry-run CSV and save them.

    python find_flagged.py logs\\decisions_20261001_095115.csv
    python find_flagged.py logs\\...csv --rule 64
    python find_flagged.py logs\\...csv --name motion --name "cover letter"
    python find_flagged.py logs\\...csv --time 12:26 --time 13:13
    python find_flagged.py logs\\...csv --outcome PLS_RVW,NO_UCN
    python find_flagged.py logs\\...csv --email 16,41
    python find_flagged.py logs\\...csv --all

With no filter it picks the emails worth a second look: any outcome other
than WOULD_ENTER, any UCN conflict, any UCN that came from the document
alone. The filters are for chasing something specific, such as every
document the ORDER catch-all claimed.

Filters match if ANY attachment on the email matches, and the whole email
is then saved. Several filters together mean OR, not AND.

For each email this prints the subject, sender and current folder to the
CONSOLE ONLY (never to a file, since subjects carry defendant names), and
saves into temp/flagged/:
    NN_<label>.txt   what DALYN decided, no subject or body text
    NN_email.html    the email itself, opens in any browser
    NN_email.eml     the raw email, for when Outlook cooperates
    NN_<file>.pdf    each attachment, for diag.py

A CSV can cover several mailboxes, and each email is looked up in its own.
If a mailbox in the CSV has since been disabled in config, this fails with
"Refusing request: mailbox not on allowlist" rather than anything about
flags: re-enable it, or use the config that produced the CSV.

temp/ holds real case documents. Confirm it is gitignored, and delete
temp/flagged when done.

Reaches into GraphClient's private helpers because graph_client has no
single-message lookup yet. Fine for a throwaway; do not copy this pattern
into DALYN proper.
"""

import argparse
import base64
import csv
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config_loader import load_config  # noqa: E402
from exceptions import MessageGone  # noqa: E402
from graph_client import GraphClient  # noqa: E402

parser = argparse.ArgumentParser(description="Pull emails from a dry-run CSV back out of the mailbox.")
parser.add_argument("csv_path", help="A logs/decisions_*.csv from a dry run.")
parser.add_argument("--all", action="store_true", help="Every email in the CSV.")
parser.add_argument("--rule", action="append", default=[], metavar="N",
                    help="Rule row that classified it, e.g. 64. Repeatable, or comma separated.")
parser.add_argument("--outcome", action="append", default=[], metavar="NAME",
                    help="Attachment outcome, e.g. PLS_RVW. Repeatable, or comma separated.")
parser.add_argument("--name", action="append", default=[], metavar="TEXT",
                    help="Text anywhere in the attachment filename, case insensitive.")
parser.add_argument("--time", action="append", default=[], metavar="HH:MM",
                    help="Text anywhere in the received time, which is UTC as printed in the CSV.")
parser.add_argument("--email", action="append", default=[], metavar="N",
                    help="email_number from the CSV. Repeatable, or comma separated.")
args = parser.parse_args()


def split_all(values: list[str]) -> set[str]:
    """Accept --rule 64 --rule 65 and --rule 64,65 as the same thing."""
    return {part.strip() for value in values for part in value.split(",") if part.strip()}


rule_rows = split_all(args.rule)
outcomes = {value.upper() for value in split_all(args.outcome)}
email_numbers = split_all(args.email)
names = [value.lower() for value in split_all(args.name)]
times = list(split_all(args.time))
filtering = bool(rule_rows or outcomes or email_numbers or names or times)


def wanted(row: dict) -> bool:
    """Does this one attachment row match what was asked for?"""
    if args.all:
        return True

    if filtering:
        # Any filter matching is enough. They are alternatives, not conditions
        # to satisfy together.
        return (
            row.get("rule_row", "") in rule_rows
            or row["outcome"].upper() in outcomes
            or row.get("email_number", "") in email_numbers
            or any(text in row["attachment_name"].lower() for text in names)
            or any(text in row["received_utc"] for text in times)
        )

    # Default: anything that did not cleanly enter, or whose case number is
    # worth a second look.
    return (
        row["outcome"] != "WOULD_ENTER"
        or row["ucn_conflict"] == "YES"
        or row["ucn_source"] == "document"
    )


config = load_config()
graph = config["graph"]
client = GraphClient(
    tenant_id=graph["tenant_id"],
    client_id=graph["client_id"],
    client_secret=graph["client_secret"],
    allowed_mailboxes=config["enabled_mailboxes"],
)

with open(args.csv_path, encoding="utf-8-sig", newline="") as handle:
    rows = list(csv.DictReader(handle))

selected_ids = {row["message_id"] for row in rows if wanted(row)}

# Every row of a selected email, not just the matching ones, so the summary
# shows everything DALYN decided for that email.
selected = {}
for row in rows:
    if row["message_id"] in selected_ids:
        selected.setdefault(row["message_id"], []).append(row)

if not selected:
    sys.exit("Nothing matched in that CSV.")

# Each email is looked up in the mailbox it came from. A CSV can now cover
# several, and a Graph message id only means anything against its own mailbox.
deleted_items_ids: dict[str, str] = {}


def deleted_items_id_for(mailbox: str) -> str:
    """Deleted Items folder id for one mailbox, fetched once per mailbox."""
    if mailbox not in deleted_items_ids:
        deleted_items_ids[mailbox] = client._request(
            "GET",
            client._mailbox_url(mailbox, "mailFolders/deleteditems"),
            params={"$select": "id"},
        ).json()["id"]
    return deleted_items_ids[mailbox]


out = Path("temp/flagged")
out.mkdir(parents=True, exist_ok=True)

SUMMARY_FIELDS = (
    "attachment_name", "outcome", "reason",
    "classified_type", "classified_subtype", "match", "matched_phrase", "rule_row",
    "ucn", "ucn_source", "ucn_subject", "ucn_body", "ucn_document", "ucn_conflict",
    "text_source", "ocr_retry", "char_count", "size_bytes",
)


def label_for(message_rows: list[dict]) -> str:
    """Short name for the filename, saying why this email is here.

    Normally the outcomes that are not WOULD_ENTER, plus UCN warnings. When
    everything entered cleanly, which only happens under a filter, the rule
    rows instead, so a --rule 64 run gives WOULD_ENTER_rule64.
    """
    parts = []
    for row in message_rows:
        if row["outcome"] != "WOULD_ENTER":
            parts.append(row["outcome"])
        if row["ucn_conflict"] == "YES":
            parts.append("UCN_CONFLICT")
        if row["ucn_source"] == "document":
            parts.append("DOC_ONLY_UCN")

    if not parts:
        parts.append("WOULD_ENTER")
        parts += [f"rule{row['rule_row']}" for row in message_rows if row.get("rule_row")]

    return "_".join(dict.fromkeys(parts))


def write_summary(number: int, label: str, message_rows: list[dict], status: str) -> None:
    """What DALYN decided for each attachment. No subject or body text."""
    lines = [
        f"Email {number:02d}: {label}",
        f"Received (UTC): {message_rows[0]['received_utc']}",
        f"Mailbox:        {message_rows[0]['mailbox']}",
        f"Status now:     {status}",
        f"Message ID:     {message_rows[0]['message_id']}",
        "",
    ]
    for row in message_rows:
        lines.append("-" * 60)
        width = max(len(field) for field in SUMMARY_FIELDS)
        for field in SUMMARY_FIELDS:
            lines.append(f"{field.ljust(width)}  {row.get(field) or '-'}")
    lines.append("")
    path = out / f"{number:02d}_{label}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"     summary: {path}")


def _addresses(recipients: list | None) -> str:
    return ", ".join(
        entry.get("emailAddress", {}).get("address", "?") for entry in recipients or []
    ) or "-"


def render_html(number: int, label: str, message: dict, where: str, saved: list[Path]) -> str:
    """A self-contained page of one email, for viewing in a browser.

    The body is the sender's own HTML, shown as-is. The CSP blocks scripts and
    every remote load, so opening it cannot run anything or ping a tracking
    pixel; remote images simply do not appear.
    """
    body = message.get("body") or {}
    content = body.get("content") or ""
    if body.get("contentType", "").lower() != "html":
        content = f"<pre style='white-space:pre-wrap'>{html.escape(content)}</pre>"

    esc = html.escape
    links = "".join(
        f"<li><a href='{esc(path.name)}'>{esc(path.name)}</a></li>" for path in saved
    ) or "<li>none</li>"
    sender = (message.get("from") or {}).get("emailAddress", {})

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>{number:02d} {esc(label)}</title>
<style>
 body {{ font-family: Segoe UI, Arial, sans-serif; margin: 0; background: #f3f3f3; }}
 .head {{ background: #fff; border-bottom: 1px solid #ccc; padding: 16px 24px; }}
 .head table {{ border-collapse: collapse; }}
 .head td {{ padding: 2px 12px 2px 0; vertical-align: top; }}
 .head td:first-child {{ color: #666; }}
 .tag {{ display: inline-block; background: #b3261e; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 13px; }}
 .mail {{ background: #fff; margin: 16px 24px; padding: 16px; border: 1px solid #ddd; }}
</style></head><body>
<div class="head">
 <p><span class="tag">{number:02d} {esc(label)}</span> &nbsp; {esc(where)}</p>
 <table>
  <tr><td>Subject</td><td><b>{esc(message.get("subject") or "(no subject)")}</b></td></tr>
  <tr><td>From</td><td>{esc(sender.get("name") or "")} &lt;{esc(sender.get("address") or "?")}&gt;</td></tr>
  <tr><td>To</td><td>{esc(_addresses(message.get("toRecipients")))}</td></tr>
  <tr><td>Cc</td><td>{esc(_addresses(message.get("ccRecipients")))}</td></tr>
  <tr><td>Received</td><td>{esc(message.get("receivedDateTime") or "")} (UTC)</td></tr>
  <tr><td>Attachments</td><td><ul style="margin:0;padding-left:18px">{links}</ul></td></tr>
 </table>
</div>
<div class="mail">{content}</div>
</body></html>
"""


for number, (message_id, message_rows) in enumerate(selected.items(), start=1):
    first = message_rows[0]
    mailbox = first["mailbox"]
    label = label_for(message_rows)
    print(f"\n[{number:02d}] {label}, {mailbox}, received {first['received_utc']} UTC")
    for row in message_rows:
        print(f"     {row['attachment_name']}: {row['outcome']} "
              f"{row['classified_type'] or '-'}/{row['classified_subtype'] or '-'} "
              f"rule {row.get('rule_row') or '-'} ({row.get('matched_phrase') or 'no match'})")

    try:
        message = client._request(
            "GET",
            client._mailbox_url(mailbox, f"messages/{message_id}"),
            gone_on_404=True,
            params={"$select": "subject,from,toRecipients,ccRecipients,receivedDateTime,body,parentFolderId"},
        ).json()
    except MessageGone:
        print("     GONE: no longer in the mailbox (purged, or moved and re-keyed)")
        write_summary(number, label, message_rows, "GONE, no longer in the mailbox")
        continue

    sender = message.get("from", {}).get("emailAddress", {}).get("address", "?")
    where = (
        "Deleted Items"
        if message.get("parentFolderId") == deleted_items_id_for(mailbox)
        else "MOVED out of Deleted Items"
    )
    print(f"     subject: {message.get('subject')}")
    print(f"     from:    {sender}")
    print(f"     folder:  {where}")
    write_summary(number, label, message_rows, where)

    # Whole message as MIME: body, headers and every attachment, including
    # forwarded-item attachments that get_attachments drops. Opens in Outlook.
    eml = client._request("GET", client._mailbox_url(mailbox, f"messages/{message_id}/$value"))
    eml_path = out / f"{number:02d}_email.eml"
    eml_path.write_bytes(eml.content)
    print(f"     email:   {eml_path}")

    saved = []
    for attachment in client.get_attachments(mailbox, message_id):
        if not attachment.get("contentBytes"):
            continue
        target = out / f"{number:02d}_{attachment['name'].strip()}"
        target.write_bytes(base64.b64decode(attachment["contentBytes"]))
        saved.append(target)
        print(f"     saved:   {target}")

    html_path = out / f"{number:02d}_email.html"
    html_path.write_text(render_html(number, label, message, where, saved), encoding="utf-8")
    print(f"     view:    {html_path}")

print(f"\n{len(selected)} email(s). PDFs in {out.resolve()}")
print(f"Next: python diag.py {out}")