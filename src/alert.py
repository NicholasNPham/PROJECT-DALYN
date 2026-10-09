"""Emails a person when a DALYN run stops, and again when runs work again.

Live DALYN runs unattended from Task Scheduler every 15 minutes, so a run
that stops is otherwise only visible in the log. One email per outage, not
one per run: a small state file in the logs folder records that the "stopped"
email went out, and the next successful run sends "running again" and clears
it.

The email carries the time, the machine and the kind of error, never the
error text. A SystemProblem message can name a case number, and this goes
to an inbox outside DALYN's logs.

Nothing here raises. An alert that cannot be sent is logged, and the error it
was reporting is still the one the run ends with.
"""

import json
import platform
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from graph_client import GraphClient
from logger import get_logger

STATE_FILE_NAME = "alert_state.json"

# (subject, body) -> None. Raises if the email could not be sent.
Sender = Callable[[str, str], None]

logger = get_logger(__name__)


def report_failure(config: dict, error: BaseException, send: Sender | None = None) -> None:
    """Send the "stopped" email, unless one already went out for this outage.

    Args:
        config: From load_config.
        error: What stopped the run. Only its type name is sent.
        send: Replaces the Graph sender. Tests use this.
    """
    if not config["alerts"]["enabled"]:
        return

    path = _state_path(config)
    state = _read_state(path)
    if state.get("failing"):
        logger.info("Alert already sent for this outage (since %s), not sending again", state.get("since"))
        return

    now = datetime.now()
    body = (
        f"A DALYN run stopped at {now:%Y-%m-%d %H:%M} on {platform.node()}.\n"
        f"Error type: {type(error).__name__}.\n\n"
        "The reason is in logs/dalyn.log on that machine. Runs keep starting on "
        "schedule; no further email is sent until one succeeds."
    )
    # Only recorded once sent, so an email that failed to go is tried again
    # on the next failed run.
    if _send(config, "DALYN stopped", body, send):
        _write_state(path, {"failing": True, "since": f"{now:%Y-%m-%d %H:%M}"})


def report_success(config: dict, send: Sender | None = None) -> None:
    """Send the "running again" email if the last alert said DALYN had stopped.

    Args:
        config: From load_config.
        send: Replaces the Graph sender. Tests use this.
    """
    if not config["alerts"]["enabled"]:
        return

    path = _state_path(config)
    state = _read_state(path)
    if not state.get("failing"):
        return

    body = (
        f"DALYN ran successfully at {datetime.now():%Y-%m-%d %H:%M} on {platform.node()}, "
        f"after stopping at {state.get('since', 'an unknown time')}."
    )
    if _send(config, "DALYN running again", body, send):
        path.unlink(missing_ok=True)


def _send(config: dict, subject: str, body: str, send: Sender | None) -> bool:
    """Send one alert email. True if it went, False if it could not.

    Catches everything on purpose: the run is already ending in an error,
    and a second one from here must not take its place.
    """
    try:
        (send or _graph_sender(config))(subject, body)
    except Exception as error:  # noqa: BLE001 - any failure to send is only logged
        logger.error("Could not send the %r alert email: %s", subject, type(error).__name__)
        return False

    logger.info("Sent the %r alert email", subject)
    return True


def _graph_sender(config: dict) -> Sender:
    """Build a sender that goes through Graph from alerts.send_from.

    A client of its own with no readable mailboxes: it may send from the one
    alert address and do nothing else.
    """
    graph = config["graph"]
    alerts = config["alerts"]
    client = GraphClient(
        tenant_id=graph["tenant_id"],
        client_id=graph["client_id"],
        client_secret=graph["client_secret"],
        allowed_mailboxes=[],
        alert_sender=alerts["send_from"],
    )
    return lambda subject, body: client.send_mail(alerts["send_from"], alerts["send_to"], subject, body)


def _state_path(config: dict) -> Path:
    """Where the "stopped email already sent" record lives."""
    return Path(config["paths"]["logs"]) / STATE_FILE_NAME


def _read_state(path: Path) -> dict:
    """Read the alert state. A missing or unreadable file means not failing.

    Unreadable reads as not failing because the cost is one extra email,
    where the other way round could silence every alert from then on.
    """
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("Could not read %s, treating it as no alert sent", path)
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(path: Path, state: dict) -> None:
    """Record the alert state. A failure to write is logged, not raised."""
    try:
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as error:
        logger.error("Could not write %s: %s", path, type(error).__name__)
