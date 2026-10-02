"""Throwaway: save a sample of attachments to disk for offline OCR development.

Writes real case documents to temp/sample. Confirm temp/ is gitignored,
and delete the folder when done.
"""

import base64
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config_loader import mailbox_addresses
from graph_client import GraphClient

with open("config/config.yaml", encoding="utf-8") as config_file:
    config = yaml.safe_load(config_file)

# Raw yaml, so there is no enabled_mailboxes key: that one is added by
# load_config, which this script does not call. Enabled only, since this pulls
# real case documents out of a live mailbox.
MAILBOXES = mailbox_addresses(config)
if not MAILBOXES:
    sys.exit("No enabled mailboxes in config.yaml.")

graph = config["graph"]
client = GraphClient(
    tenant_id=graph["tenant_id"],
    client_id=graph["client_id"],
    client_secret=graph["client_secret"],
    allowed_mailboxes=MAILBOXES,
)

mailbox = MAILBOXES[0]
out = Path("temp/sample10")

if out.exists():
    sys.exit(f"{out} already exists. Move or delete it first.")
out.mkdir(parents=True)

print(f"Reading {mailbox}")

messages = client.list_messages(
    mailbox=mailbox,
    days_back=config["days_back"],
    max_messages=config["max_messages"],
    newest_first=config["newest_first"],
)

saved = 0
skipped = 0
not_pdf = 0

for index, message in enumerate(messages):
    for attachment in client.get_attachments(mailbox, message["id"]):
        if not attachment["name"].lower().endswith(".pdf"):
            not_pdf += 1
            continue

        if "contentBytes" not in attachment:
            print(f"{index:03d}: {attachment['name']} has no contentBytes, SKIPPED")
            skipped += 1
            continue

        target = out / f"{index:03d}_{attachment['name']}"
        if target.exists():
            target = out / f"{index:03d}_dup{saved}_{attachment['name']}"

        target.write_bytes(base64.b64decode(attachment["contentBytes"]))
        saved += 1

print(f"\n{saved} saved, {skipped} skipped (NO BYTES), {not_pdf} non-PDF, in {out.resolve()}")