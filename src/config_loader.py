"""Loads and validates config.yaml. The only file that reads config from disk."""

from pathlib import Path

import yaml

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
    "paths",
)
REQUIRED_GRAPH_KEYS = ("tenant_id", "client_id", "client_secret")
PATH_KEYS = ("logs", "temp", "excel")

logger = get_logger(__name__)


def load_config(config_path: Path | None = None) -> dict:
    """Read config.yaml, validate it, and resolve relative paths.

    Paths under `paths` are resolved against the project root, not the working
    directory, so DALYN behaves the same when launched by Task Scheduler as it
    does from a terminal. Absolute paths (a network drive, for example) are
    left alone.

    Args:
        config_path: Override for the config file location. Tests use this.

    Returns:
        The config dict, with `paths` values replaced by absolute Path objects.

    Raises:
        SystemProblem: If the file is missing, unparseable, or incomplete.
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

    logger.info(
        "Config loaded from %s (dry_run=%s, mailboxes=%s)",
        path,
        config["dry_run"],
        len(config["mailboxes"]),
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

    graph = config["graph"]
    if not isinstance(graph, dict):
        raise SystemProblem("Config key 'graph' must be a mapping.")

    blank = [key for key in REQUIRED_GRAPH_KEYS if not graph.get(key)]
    if blank:
        raise SystemProblem(
            f"Config at {path} has empty graph credentials: {', '.join(blank)}"
        )

    mailboxes = config["mailboxes"]
    if not isinstance(mailboxes, list) or not mailboxes:
        raise SystemProblem("Config key 'mailboxes' must be a non-empty list.")

    for mailbox in mailboxes:
        if not isinstance(mailbox, str) or "@" not in mailbox:
            raise SystemProblem(
                f"Not a valid SMTP address in 'mailboxes': {mailbox!r}. "
                "Aliases and display names will not work."
            )

    for key in ("days_back", "max_messages"):
        value = config[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SystemProblem(f"Config key '{key}' must be a positive integer.")

    for key in ("dry_run", "newest_first"):
        if not isinstance(config[key], bool):
            raise SystemProblem(f"Config key '{key}' must be true or false.")

    paths = config["paths"]
    if not isinstance(paths, dict):
        raise SystemProblem("Config key 'paths' must be a mapping.")

    missing_paths = [key for key in PATH_KEYS if not paths.get(key)]
    if missing_paths:
        raise SystemProblem(f"Config 'paths' is missing: {', '.join(missing_paths)}")


def _resolve_paths(config: dict) -> None:
    """Turn `paths` values into absolute Paths, anchored at the project root."""
    resolved = {}

    for key, value in config["paths"].items():
        candidate = Path(value)
        resolved[key] = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate

    config["paths"] = resolved