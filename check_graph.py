"""Throwaway check: token acquisition and mailbox address validation.

NOTE: the app currently has tenant-wide mail access. This script cannot
prove scoping, because nothing is scoped. It proves auth works and that
the three configured addresses resolve to the mailboxes we expect.

Keep until the Exchange scope is applied, then re-run to verify cutover.
"""

import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))
from graph_client import GRAPH_BASE_URL, GraphClient

with open("config/config.yaml", encoding="utf-8") as config_file:
    config = yaml.safe_load(config_file)

graph = config["graph"]
client = GraphClient(
    tenant_id=graph["tenant_id"],
    client_id=graph["client_id"],
    client_secret=graph["client_secret"],
    allowed_mailboxes=config["mailboxes"],
)

print("Allowlist:", sorted(client._allowed_mailboxes))
print()

token = client._get_token()
headers = {"Authorization": f"Bearer {token}"}
print("Token acquired.\n")

OUT_OF_SCOPE = "npham@sao10.com"


def check(mailbox: str) -> None:
    """Resolve the mailbox, then confirm Deleted Items is reachable."""
    user = requests.get(
        f"{GRAPH_BASE_URL}/users/{mailbox}", headers=headers, timeout=30
    )
    if user.status_code == 404:
        print(f"{mailbox}: 404 ADDRESS DOES NOT EXIST")
        return

    display_name = user.json().get("displayName", "?") if user.ok else "?"

    folder = requests.get(
        f"{GRAPH_BASE_URL}/users/{mailbox}/mailFolders/deleteditems",
        headers=headers,
        timeout=30,
    )
    if folder.ok:
        count = folder.json().get("totalItemCount", "?")
        print(f"{mailbox}: 200  [{display_name}]  DeletedItems={count} items")
    else:
        try:
            code = folder.json()["error"]["code"]
        except (ValueError, KeyError):
            code = "no error body"
        print(f"{mailbox}: {folder.status_code} ({code})  [{display_name}]")


print("--- Configured mailboxes (all should be 200) ---")
for mailbox in config["mailboxes"]:
    check(mailbox)

print("\n--- Out-of-scope control ---")
check(OUT_OF_SCOPE)
print(
    "200 above is EXPECTED while access is tenant-wide.\n"
    "After the Exchange scope is applied it must become 403."
)

# --- Dry run listing test, temporary ---
messages = client.list_messages(
    mailbox=config["mailboxes"][0],
    days_back=config["days_back"],
    max_messages=config["max_messages"],
    newest_first=config["newest_first"],
)

print(f"\n{len(messages)} messages with attachments:")
for message in messages[:5]:
    print(f"  {message['receivedDateTime']}  {message['id'][:40]}...")

if messages:
    files = client.get_attachments(config["mailboxes"][0], messages[0]["id"])
    print(f"\nFirst message has {len(files)} usable file(s):")
    for attachment in files:
        size_kb = attachment.get("size", 0) // 1024
        has_bytes = "contentBytes" in attachment
        print(f"  {attachment['name']}  {size_kb}KB  bytes_inline={has_bytes}")
