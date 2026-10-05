"""Reads DALYN's secrets from Windows Credential Manager. The only file that does.

config.yaml holds identifiers (tenant id, client id, STAC url). The values that
grant access live in Credential Manager instead, written there once per machine
by set_credentials.py:

    DALYN / graph_client_secret   the Entra app's client secret Value
    DALYN / stac_username         the STAC service account
    DALYN / stac_password         its password

Credential Manager encrypts per Windows user. Entries stored while signed in as
one account cannot be read by another, so a scheduled task running as SYSTEM or
a service account sees none of them. The error raised here names the account
that looked, so that failure reads as what it is rather than as an expired
secret.

This is storage, not an access boundary. Anything running as the same Windows
user, including a shell, can read these back exactly as DALYN does.
"""

import getpass

import keyring
from keyring.errors import KeyringError

from exceptions import SystemProblem
from logger import get_logger

SERVICE = "DALYN"

# Key in Credential Manager -> what it is, for prompts and error messages.
# set_credentials.py walks this, so adding a secret here is the whole change.
CREDENTIALS = {
    "graph_client_secret": "Entra app client secret (the Value, not the Secret ID)",
    "stac_username": "STAC username",
    "stac_password": "STAC password",
}

# Backends that would quietly undo the point of moving secrets out of
# config.yaml: one writes them to a plaintext file, the other stores nothing.
_REFUSED_BACKENDS = ("PlaintextKeyring", "fail.Keyring", "null.Keyring")

logger = get_logger(__name__)


def load_credentials() -> dict[str, str]:
    """Return every DALYN secret, keyed as in CREDENTIALS.

    All keys are checked before raising, so a machine missing two secrets is
    told about both at once rather than one run at a time.

    Returns:
        {key: value} for every key in CREDENTIALS. Values are returned exactly
        as stored: a password with a leading space keeps it.

    Raises:
        SystemProblem: If the keyring backend is unusable or unsafe, or if any
            secret is missing or blank.
    """
    _check_backend()

    found: dict[str, str] = {}
    missing: list[str] = []

    for key in CREDENTIALS:
        try:
            value = keyring.get_password(SERVICE, key)
        except KeyringError as error:
            raise SystemProblem(
                f"Could not read {SERVICE}/{key} from Credential Manager: {error}"
            ) from error

        if value is None or not value.strip():
            missing.append(key)
        else:
            found[key] = value

    if missing:
        user = getpass.getuser()
        raise SystemProblem(
            f"Credential Manager has no {', '.join(f'{SERVICE}/{k}' for k in missing)} "
            f"for Windows user '{user}'. Run set_credentials.py while signed in as "
            f"'{user}'. If these were stored under a different account, such as "
            "when DALYN runs from Task Scheduler as another user, they exist but "
            "are invisible to this one: Credential Manager is per user."
        )

    # Never the values. Only that they were found.
    logger.debug("Loaded %s credentials from %s", len(found), _backend_name())
    return found


def _check_backend() -> None:
    """Refuse a keyring backend that stores nothing or stores in plaintext.

    On Windows the default is the Credential Manager backend. A plaintext one
    only appears if keyrings.alt is installed and configured, and if it ever
    were, the secrets would be back in a file on disk with nothing to show for
    it.
    """
    name = _backend_name()
    if any(refused in name for refused in _REFUSED_BACKENDS):
        raise SystemProblem(
            f"keyring is using {name}, which either stores nothing or stores "
            "secrets in a plaintext file. DALYN expects Windows Credential "
            "Manager (WinVaultKeyring). Check for keyrings.alt or a "
            "keyringrc.cfg overriding the default."
        )


def _backend_name() -> str:
    backend = keyring.get_keyring()
    return f"{type(backend).__module__}.{type(backend).__qualname__}"