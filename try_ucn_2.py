"""Throwaway: compare UCN hit rate from document vs subject vs bodyPreview."""

import base64
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "src")
from graph_client import GraphClient
from ocr import extract_text
from ucn import find_ucn

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
messages = client.list_messages(
    mailbox=mailbox,
    days_back=config["days_back"],
    max_messages=config["max_messages"],
    newest_first=config["newest_first"],
)

counts = {"document": 0, "subject": 0, "body": 0, "none": 0, "disagree": 0}

for index, message in enumerate(messages):
    subject = message.get("subject", "")
    body = message.get("bodyPreview", "")

    doc_ucn = None
    for attachment in client.get_attachments(mailbox, message["id"]):
        if "contentBytes" not in attachment:
            continue
        try:
            text = extract_text(base64.b64decode(attachment["contentBytes"]), attachment["name"])
            doc_ucn = find_ucn(text)
        except Exception as error:
            print(f"{index:03d}: {type(error).__name__}: {error}")
        break

    subject_ucn = find_ucn(subject)
    body_ucn = find_ucn(body)

    counts["document"] += bool(doc_ucn)
    counts["subject"] += bool(subject_ucn)
    counts["body"] += bool(body_ucn)
    if not any((doc_ucn, subject_ucn, body_ucn)):
        counts["none"] += 1

    found = {u for u in (doc_ucn, subject_ucn, body_ucn) if u}
    if len(found) > 1:
        counts["disagree"] += 1
        print(f"{index:03d}: DISAGREE doc={doc_ucn} subj={subject_ucn} body={body_ucn}")

print(f"\nof {len(messages)} messages:")
for key, value in counts.items():
    print(f"  {key}: {value}")