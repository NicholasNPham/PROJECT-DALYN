"""Store DALYN's secrets in Windows Credential Manager. Run once per machine.

    python set_credentials.py            set or replace each secret
    python set_credentials.py --status   show which are set, never the values
    python set_credentials.py --clear    delete all of them

Run it signed in as the Windows account DALYN will run as. Credential Manager
is per user: secrets stored here as one account are invisible to another,
including to a scheduled task running as a different user.

Nothing typed here is echoed, logged, or written anywhere but Credential
Manager. Secrets are typed twice, because a hidden prompt hides typos too.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import keyring  # noqa: E402
from keyring.errors import KeyringError, PasswordDeleteError  # noqa: E402

import credential  # noqa: E402
from exceptions import SystemProblem  # noqa: E402

# Keys whose value is shown while typing. Everything else is a secret.
VISIBLE_KEYS = frozenset({"stac_username"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Store DALYN secrets in Credential Manager.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Show which secrets are set.")
    mode.add_argument("--clear", action="store_true", help="Delete every DALYN secret.")
    args = parser.parse_args()

    try:
        # Reaches into credential's private check on purpose: refusing a
        # plaintext backend matters as much when writing as when reading.
        credential._check_backend()
    except SystemProblem as error:
        print(error, file=sys.stderr)
        return 1

    print(f"Windows user: {getpass.getuser()}")
    print(f"Backend:      {credential._backend_name()}\n")

    if args.clear:
        return _clear()

    _print_status()
    if args.status:
        return 0

    print("\nEnter leaves a secret that is already set unchanged.\n")
    for key, label in credential.CREDENTIALS.items():
        try:
            _set_one(key, label)
        except KeyboardInterrupt:
            print("\nStopped. Secrets entered before this point were saved.")
            return 1
        except KeyringError as error:
            print(f"Could not write {credential.SERVICE}/{key}: {error}", file=sys.stderr)
            return 1

    print()
    _print_status()
    # Non-zero while anything is still missing, so a setup script or a person
    # skimming the output cannot mistake a half-done run for a finished one.
    return 0 if all(_is_set(key) for key in credential.CREDENTIALS) else 1


def _is_set(key: str) -> bool:
    value = keyring.get_password(credential.SERVICE, key)
    return bool(value and value.strip())


def _print_status() -> None:
    for key in credential.CREDENTIALS:
        state = "set" if _is_set(key) else "NOT SET"
        print(f"  {credential.SERVICE}/{key:<22} {state}")


def _set_one(key: str, label: str) -> None:
    """Prompt for one value, store it, and read it back to confirm."""
    already = _is_set(key)
    suffix = " [Enter to keep]" if already else ""

    if key in VISIBLE_KEYS:
        value = input(f"{label}{suffix}: ").strip()
    else:
        value = getpass.getpass(f"{label}{suffix}: ")
        if value and getpass.getpass("  again to confirm: ") != value:
            print("  Did not match. Not saved; run again for this one.")
            return

    if not value:
        print("  Kept." if already else "  Skipped. Still NOT SET.")
        return

    # Secrets are never stripped, since a real one can contain spaces. But a
    # paste that dragged in whitespace is the likelier explanation, so ask.
    if value != value.strip():
        answer = input("  Starts or ends with whitespace. Save exactly as typed? [y/N]: ")
        if answer.strip().lower() != "y":
            print("  Not saved.")
            return

    keyring.set_password(credential.SERVICE, key, value)

    if keyring.get_password(credential.SERVICE, key) != value:
        raise KeyringError(f"read-back of {credential.SERVICE}/{key} did not match what was written")
    print("  Saved.")


def _clear() -> int:
    answer = input(f"Delete every {credential.SERVICE} secret for this user? Type DELETE: ")
    if answer != "DELETE":
        print("Nothing deleted.")
        return 1

    for key in credential.CREDENTIALS:
        try:
            keyring.delete_password(credential.SERVICE, key)
            print(f"  {credential.SERVICE}/{key:<22} deleted")
        except PasswordDeleteError:
            print(f"  {credential.SERVICE}/{key:<22} was not set")
    return 0


if __name__ == "__main__":
    sys.exit(main())