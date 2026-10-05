"""Loads and validates config.yaml. The only file that reads config from disk.

Secrets are not in config.yaml. They come from Windows Credential Manager via
credential.py and are attached to the config here, after validation, so the
rest of DALYN reads config["graph"]["client_secret"] and config["stac"]
["username"] / ["password"] exactly as before and never knows where they came
from.
"""

from pathlib import Path

import yaml

from credential import load_credentials
from exceptions import SystemProblem
from logger import get_logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

REQUIRED_KEYS = (
    "graph",
    "mailboxes",
    "source_folder",
    "dry_run",
    "days_back",
    "max_messages",
    "newest_first",
    "stac",
    "paths",
)
REQUIRED_GRAPH_KEYS = ("tenant_id", "client_id")
REQUIRED_STAC_KEYS = ("url",)

# Settings that used to live in config.yaml and now come from Credential
# Manager. Their presence in the file is refused outright, whatever the value,
# so an old config.yaml still carrying a live secret cannot go on working
# quietly, and a copy of it handed to someone else is not a copy of the secret.
MOVED_TO_CREDENTIAL_MANAGER = {
    "graph": ("client_secret",),
    "stac": ("username", "password"),
}
PATH_KEYS = ("logs", "temp", "excel", "stac_types")
MAILBOX_KEYS = ("address", "enabled")

# A url is treated as a test instance only if one of these appears in it.
# Deliberately crude: the point is that a plain production url cannot be
# left in place while is_test_instance still says true.
TEST_URL_MARKERS = ("test", "uat", "stage", "staging", "dev")

# Where an attachment goes when it has a usable case number but matched no
# rule. Both halves are the same string, read off STAC's own matrix dialog on
# 1 Oct 2026: Type "PLS RVW" (IMAGE NEEDS TO BE REVIEWED), Subtype "PLS RVW"
# (Please Review.). Not Type PLS with Subtype RVW, which is what it was
# modelled as until a run printed the real rows.
LIVE_REVIEW_TYPE = "PLS RVW"
LIVE_REVIEW_SUBTYPE = "PLS RVW"

logger = get_logger(__name__)


def load_config(config_path: Path | None = None, with_credentials: bool = True) -> dict:
    """Read config.yaml, validate it, resolve relative paths, and attach secrets.

    Paths under `paths` are resolved against the project root, not the working
    directory, so DALYN behaves the same when launched by Task Scheduler as it
    does from a terminal. Absolute paths (a network drive, for example) are
    left alone.

    Args:
        config_path: Override for the config file location. Tests use this.
        with_credentials: False skips Credential Manager entirely, for tools
            that never sign in to anything (check_types, diag, review_batch).
            The secret keys are then simply absent from the returned config.

    Returns:
        The config dict, with `paths` values replaced by absolute Path objects
        and, unless with_credentials is False, graph.client_secret,
        stac.username and stac.password filled from Credential Manager.

    Raises:
        SystemProblem: If the file is missing, unparseable, incomplete, still
            holds a secret, or Credential Manager is missing one.
    """
    path = config_path or DEFAULT_CONFIG_PATH

    try:
        with open(path, encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
    except FileNotFoundError as error:
        raise SystemProblem(
            f"Config not found at {path}. Copy config.example.yaml and fill it in."
        ) from error
    except yaml.YAMLError as error:
        raise SystemProblem(f"Config at {path} is not valid YAML: {error}") from error

    if not isinstance(config, dict):
        raise SystemProblem(f"Config at {path} is empty or not a mapping.")

    _validate(config, path)
    _resolve_paths(config)
    if with_credentials:
        _attach_credentials(config)

    stac = config["stac"]
    logger.info(
        "Config loaded from %s (dry_run=%s, mailboxes=%s of %s enabled: %s, "
        "stac=%s, test=%s, upload=%s, save=%s, secrets=%s)",
        path,
        config["dry_run"],
        len(config["enabled_mailboxes"]),
        len(config["mailboxes"]),
        ", ".join(config["enabled_mailboxes"]),
        stac["url"],
        stac.get("is_test_instance", True),
        stac.get("upload_enabled", False),
        stac.get("save_enabled", False),
        "Credential Manager" if with_credentials else "not loaded",
    )

    return config


def _validate(config: dict, path: Path) -> None:
    """Fail at startup on a bad config rather than halfway through a run.

    Raises:
        SystemProblem: If a required key is missing or a value is unusable.
    """
    missing = [key for key in REQUIRED_KEYS if key not in config]
    if missing:
        raise SystemProblem(f"Config at {path} is missing keys: {', '.join(missing)}")

    _refuse_stored_secrets(config, path)

    graph = config["graph"]
    if not isinstance(graph, dict):
        raise SystemProblem("Config key 'graph' must be a mapping.")

    blank = [key for key in REQUIRED_GRAPH_KEYS if not graph.get(key)]
    if blank:
        raise SystemProblem(
            f"Config at {path} has empty graph settings: {', '.join(blank)}"
        )

    _validate_mailboxes(config, path)

    for key in ("days_back", "max_messages"):
        value = config[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SystemProblem(f"Config key '{key}' must be a positive integer.")

    for key in ("dry_run", "newest_first"):
        if not isinstance(config[key], bool):
            raise SystemProblem(f"Config key '{key}' must be true or false.")

    stac = config["stac"]
    if not isinstance(stac, dict):
        raise SystemProblem("Config key 'stac' must be a mapping.")

    blank_stac = [key for key in REQUIRED_STAC_KEYS if not stac.get(key)]
    if blank_stac:
        raise SystemProblem(
            f"Config at {path} has empty stac settings: {', '.join(blank_stac)}"
        )

    if stac.get("save_enabled") and not stac.get("upload_enabled"):
        raise SystemProblem(
            "Config has stac.save_enabled true but stac.upload_enabled false. "
            "There would be nothing uploaded to save. Set upload_enabled true, "
            "or save_enabled false."
        )

    url = str(stac["url"]).strip()
    if not url.lower().startswith("https://"):
        raise SystemProblem(f"Config 'stac.url' must be https, got {url!r}.")

    for key in ("is_test_instance", "upload_enabled", "save_enabled", "fresh_browser"):
        if key in stac and not isinstance(stac[key], bool):
            raise SystemProblem(f"Config key 'stac.{key}' must be true or false.")

    # Refuse the combination that does the damage: a url that is clearly not
    # a test instance, while the config still claims it is. Someone pasting
    # the live url into a test config should be stopped here, not after the
    # first document is filed on a real case.
    if stac.get("is_test_instance", True) and not any(
        marker in url.lower() for marker in TEST_URL_MARKERS
    ):
        raise SystemProblem(
            f"Config says stac.is_test_instance is true, but {url!r} does not look "
            f"like a test instance (expected one of {', '.join(TEST_URL_MARKERS)} in "
            "the address). Fix the url, or set is_test_instance to false on purpose."
        )

    for key in ("wait_timeout", "upload_timeout", "max_attempts"):
        value = stac.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            raise SystemProblem(f"Config key 'stac.{key}' must be a positive integer.")

    pause = stac.get("action_pause")
    if pause is not None and (isinstance(pause, bool) or not isinstance(pause, (int, float)) or pause < 0):
        raise SystemProblem("Config key 'stac.action_pause' must be 0 or more seconds.")

    _validate_review_pair(stac)

    paths = config["paths"]
    if not isinstance(paths, dict):
        raise SystemProblem("Config key 'paths' must be a mapping.")

    missing_paths = [key for key in PATH_KEYS if not paths.get(key)]
    if missing_paths:
        raise SystemProblem(f"Config 'paths' is missing: {', '.join(missing_paths)}")


def _refuse_stored_secrets(config: dict, path: Path) -> None:
    """Stop if config.yaml still holds anything that now lives in Credential Manager.

    Checked for presence, not value: a placeholder like PASTE-SECRET-VALUE is
    refused too, so config.example.yaml and every real config.yaml end up with
    the keys gone rather than blanked, and there is one shape to compare.

    Raises:
        SystemProblem: Naming every offending key at once.
    """
    found = [
        f"{section}.{key}"
        for section, keys in MOVED_TO_CREDENTIAL_MANAGER.items()
        if isinstance(config.get(section), dict)
        for key in keys
        if key in config[section]
    ]
    if found:
        raise SystemProblem(
            f"Config at {path} still contains {', '.join(found)}. These now live "
            "in Windows Credential Manager: store them with set_credentials.py, "
            "then delete those lines from config.yaml."
        )


def _attach_credentials(config: dict) -> None:
    """Fill the secrets in from Credential Manager, after validation has passed.

    After validation on purpose: a malformed config.yaml is reported as that,
    without first touching Credential Manager.
    """
    secrets = load_credentials()
    config["graph"]["client_secret"] = secrets["graph_client_secret"]
    config["stac"]["username"] = secrets["stac_username"]
    config["stac"]["password"] = secrets["stac_password"]


def _validate_mailboxes(config: dict, path: Path) -> None:
    """Normalize the mailbox list and derive the allowlist from the enabled ones.

    Each entry is a mapping with an `address` and an `enabled` flag, so a
    mailbox can sit in the file while switched off. Only the enabled addresses
    are handed to GraphClient, which makes a disabled mailbox unreachable
    rather than merely unvisited. That distinction carries weight while the app
    registration still holds tenant-wide mail access: this list is the only
    thing confining it, so "not in the loop" is not good enough for juvenile.

    Adds `enabled_mailboxes` to the config: the addresses to read, in file
    order.

    Raises:
        SystemProblem: If the list is empty, an entry is the wrong shape, an
            address is duplicated, or nothing is enabled.
    """
    mailboxes = config["mailboxes"]
    if not isinstance(mailboxes, list) or not mailboxes:
        raise SystemProblem("Config key 'mailboxes' must be a non-empty list.")

    normalized: list[dict] = []
    seen: set[str] = set()

    for index, entry in enumerate(mailboxes, start=1):
        if not isinstance(entry, dict):
            raise SystemProblem(
                f"Config 'mailboxes' entry {index} is {entry!r}. Each entry must be "
                "a mapping with 'address' and 'enabled'. See config.example.yaml."
            )

        unknown = sorted(set(entry) - set(MAILBOX_KEYS))
        if unknown:
            raise SystemProblem(
                f"Config 'mailboxes' entry {index} has unexpected keys: "
                f"{', '.join(unknown)}. Only 'address' and 'enabled' are read, so a "
                "typo here would be a setting that silently does nothing."
            )

        address = entry.get("address")
        if not isinstance(address, str) or "@" not in address:
            raise SystemProblem(
                f"Config 'mailboxes' entry {index} has address {address!r}. A full "
                "SMTP address is required; aliases and display names will not work."
            )

        enabled = entry.get("enabled")
        if not isinstance(enabled, bool):
            raise SystemProblem(
                f"Config 'mailboxes' entry {index} ({address}) must set 'enabled' to "
                "true or false. It has no default on purpose: a mailbox that is "
                "neither clearly on nor clearly off is the one that gets read by "
                "accident."
            )

        address = address.strip().lower()
        if address in seen:
            raise SystemProblem(
                f"Config 'mailboxes' lists {address} more than once. Two entries for "
                "one mailbox leaves the enabled flag ambiguous."
            )
        seen.add(address)

        normalized.append({"address": address, "enabled": enabled})

    enabled_addresses = [entry["address"] for entry in normalized if entry["enabled"]]
    if not enabled_addresses:
        raise SystemProblem(
            f"Config at {path} has every mailbox disabled. A pass over nothing "
            "finishes clean and looks like success, so this is an error instead."
        )

    config["mailboxes"] = normalized
    config["enabled_mailboxes"] = enabled_addresses


def _validate_review_pair(stac: dict) -> None:
    """Check the Type/Subtype that unclassified attachments are filed under.

    Live STAC has PLS/RVW. Test STAC does not, which is why this is config at
    all. The one rule worth enforcing: anything other than PLS/RVW is a
    stand-in for testing, and a stand-in on a live instance would quietly file
    real filings under a made-up code that nobody reviews. So a different pair
    is only allowed while is_test_instance is true.

    Raises:
        SystemProblem: If only one half of the pair is set, if either is not a
            string, or if a stand-in pair is configured against live STAC.
    """
    document_type = stac.get("review_type")
    subtype = stac.get("review_subtype")

    if document_type is None and subtype is None:
        stac["review_type"] = LIVE_REVIEW_TYPE
        stac["review_subtype"] = LIVE_REVIEW_SUBTYPE
        return

    # Half a pair files everything under the wrong code, so it is an error
    # rather than something to fill in with a default.
    if document_type is None or subtype is None:
        raise SystemProblem(
            "Config has only one of stac.review_type and stac.review_subtype. "
            "Set both, or neither to use "
            f"{LIVE_REVIEW_TYPE}/{LIVE_REVIEW_SUBTYPE}."
        )

    for key, value in (("review_type", document_type), ("review_subtype", subtype)):
        if not isinstance(value, str) or not value.strip():
            raise SystemProblem(f"Config key 'stac.{key}' must be a non-empty string.")

    document_type = document_type.strip().upper()
    subtype = subtype.strip().upper()
    stac["review_type"] = document_type
    stac["review_subtype"] = subtype

    is_live = not stac.get("is_test_instance", True)
    is_stand_in = (document_type, subtype) != (LIVE_REVIEW_TYPE, LIVE_REVIEW_SUBTYPE)

    if is_live and is_stand_in:
        raise SystemProblem(
            "Config sets stac.review_type/stac.review_subtype to "
            f"{document_type}/{subtype} on a live instance. Only "
            f"{LIVE_REVIEW_TYPE}/{LIVE_REVIEW_SUBTYPE} may be used against live "
            "STAC; a stand-in code would file real documents where nobody looks "
            "for them."
        )


def _resolve_paths(config: dict) -> None:
    """Turn `paths` values into absolute Paths, anchored at the project root."""
    resolved = {}

    for key, value in config["paths"].items():
        # A blank optional path means "not set". Without this it becomes
        # Path(""), which resolves to the project root and then looks like a
        # real setting to everything downstream.
        if value is None or not str(value).strip():
            resolved[key] = None
            continue
        candidate = Path(str(value).strip())
        resolved[key] = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate

    config["paths"] = resolved


def mailbox_addresses(config: dict, enabled_only: bool = True) -> list[str]:
    """Pull mailbox addresses out of a raw or validated config.

    For the hand-run scripts, which read config.yaml with yaml.safe_load and
    never call load_config, so they have no `enabled_mailboxes` key. DALYN
    itself should use config["enabled_mailboxes"].

    Args:
        config: Parsed config.yaml, validated or not.
        enabled_only: False returns every listed address regardless of its
            flag. Only check_graph.py wants this, because Exchange RBAC
            scoping is server-side and has to be verifiable on a mailbox that
            is switched off here.
    """
    entries = config.get("mailboxes") or []
    return [
        str(entry["address"]).strip().lower()
        for entry in entries
        if isinstance(entry, dict) and entry.get("address")
        and (not enabled_only or entry.get("enabled") is True)
    ]