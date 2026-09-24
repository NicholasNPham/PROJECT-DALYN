"""Throwaway: save a sample of attachments to disk for offline OCR development.

Writes real case documents to temp/sample. Confirm temp/ is gitignored,
and delete the folder when done.
"""

import base64
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))
from graph_client import GraphClient

with open("config/config.yaml", encoding="utf-8") as config_file:
    config = yaml.safe_load(config_file)

graph = config["graph"]
client = GraphClient(
    tenant_id=graph["tenant_id"],
    client_id=graph["client_id"],
    client_secret=graph["client_secret"],
    allowed_mailboxes=config["mailboxes"],
)

mailbox = config["mailboxes"][0]
out = Path("temp/sample")
out.mkdir(parents=True, exist_ok=True)

messages = client.list_messages(
    mailbox=mailbox,
    days_back=config["days_back"],
    max_messages=config["max_messages"],
    newest_first=config["newest_first"],
)

saved = 0
skipped = 0

for index, message in enumerate(messages):
    for attachment in client.get_attachments(mailbox, message["id"]):
        if "contentBytes" not in attachment:
            print(f"{index:03d}: {attachment['name']} too large, skipped")
            skipped += 1
            continue
        target = out / f"{index:03d}_{attachment['name']}"
        target.write_bytes(base64.b64decode(attachment["contentBytes"]))
        saved += 1

print(f"\n{saved} saved, {skipped} skipped, in {out.resolve()}")