"""All Microsoft Graph calls for DALYN. The only file that talks to Microsoft."""

import time
from datetime import datetime, timedelta, timezone

import msal
import requests

from exceptions import GraphAuthError, SystemProblem
from logger import get_logger

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

ALLOWED_FOLDERS = frozenset({"deleteditems"})
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 300

logger = get_logger(__name__)


class GraphClient:
    """Signs in as the DALYN app and makes Graph calls against the monitored mailboxes."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        allowed_mailboxes: list[str],
    ) -> None:
        """Build the MSAL client. Does not contact Microsoft until a token is requested.

        Args:
            tenant_id: Directory (tenant) ID from the app registration.
            client_id: Application (client) ID from the app registration.
            client_secret: The secret Value, not the Secret ID.
            allowed_mailboxes: SMTP addresses DALYN is permitted to touch.

        Raises:
            GraphAuthError: If MSAL rejects the credentials as malformed.
        """
        try:
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=client_id,
                authority=f"https://login.microsoftonline.com/{tenant_id}",
                client_credential=client_secret,
            )
        except ValueError as error:
            raise GraphAuthError(f"Could not set up MSAL client: {error}") from error

        self._session = requests.Session()
        self._allowed_mailboxes = frozenset(
            mailbox.strip().lower() for mailbox in allowed_mailboxes
        )

        logger.debug("MSAL client built for tenant %s", tenant_id)

    def _get_token(self) -> str:
        """Return a valid access token, reusing the cached one until it nears expiry.

        Returns:
            The bearer token string for the Authorization header.

        Raises:
            GraphAuthError: If Microsoft refuses to issue a token.
        """
        result = self._msal_app.acquire_token_for_client(scopes=GRAPH_SCOPE)

        if "access_token" in result:
            return result["access_token"]

        error_code = result.get("error", "unknown_error")
        description = result.get("error_description", "no description")
        logger.error("Token request failed: %s", error_code)
        raise GraphAuthError(f"Token request failed: {error_code}: {description}")

    def _mailbox_url(self, mailbox: str, path: str) -> str:
        """Build a Graph URL for a mailbox, refusing anything outside the allowlist.

        This is the only place in DALYN that builds a /users/ URL. The app
        credential currently has tenant-wide mail access, so this check is the
        sole thing keeping DALYN inside its three mailboxes. Do not bypass it.

        Args:
            mailbox: SMTP address of the target mailbox.
            path: Graph path below the user, e.g. "mailFolders/deleteditems/messages".

        Returns:
            Fully qualified Graph URL.

        Raises:
            SystemProblem: If the mailbox or folder is not on the allowlist.
        """
        normalized = mailbox.strip().lower()
        if normalized not in self._allowed_mailboxes:
            raise SystemProblem(f"Refusing request: mailbox not on allowlist: {mailbox}")

        if path.startswith("mailFolders/"):
            folder = path.split("/")[1].lower()
            if folder not in ALLOWED_FOLDERS:
                raise SystemProblem(
                    f"Refusing request: folder not on allowlist: {folder}"
                )

        return f"{GRAPH_BASE_URL}/users/{normalized}/{path}"

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make an authenticated Graph call, retrying on throttling and outages.

        Args:
            method: HTTP verb, e.g. "GET" or "PATCH".
            url: Full Graph URL, normally from _mailbox_url.
            **kwargs: Passed through to requests, e.g. params, json.

        Returns:
            The successful Response. Callers parse it themselves.

        Raises:
            GraphAuthError: On 401, meaning the credential is bad or revoked.
            SystemProblem: On any other non-success status, or if retries run out.
            requests.RequestException: On network failure, meaning try next pass.
        """
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            headers.update(kwargs.pop("headers", {}))

            response = self._session.request(
                method, url, headers=headers, timeout=30, **kwargs
            )

            if response.ok:
                return response

            if response.status_code == 401:
                raise GraphAuthError(f"Graph rejected the token: {response.text[:200]}")

            if response.status_code in (429, 503):
                wait = self._retry_delay(response, attempt)
                logger.warning(
                    "Graph returned %s, waiting %ss (attempt %s of %s)",
                    response.status_code,
                    wait,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            raise SystemProblem(
                f"Graph {method} failed with {response.status_code}: "
                f"{response.text[:200]}"
            )

        raise SystemProblem(
            f"Graph {method} still throttled after {MAX_RETRIES} attempts"
        )

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> int:
        """Decide how long to wait before retrying a throttled request.

        Honors Retry-After when Microsoft sends one, otherwise backs off
        exponentially. Capped so a bad header cannot stall DALYN for hours.

        Args:
            response: The throttled response, checked for a Retry-After header.
            attempt: Zero-based retry count, used for exponential backoff.

        Returns:
            Seconds to sleep before retrying.
        """
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(int(retry_after), MAX_BACKOFF_SECONDS)
            except ValueError:
                logger.warning("Unparseable Retry-After header: %r", retry_after)

        return min(2**attempt, MAX_BACKOFF_SECONDS)

    def list_messages(
            self,
            mailbox: str,
            days_back: int,
            max_messages: int,
            newest_first: bool = True,
    ) -> list[dict]:
        """Return recent messages from Deleted Items that carry attachments.

        Deliberately single-page. The dry run is bounded by max_messages so a
        reviewer can check every decision by hand, and paging past that ceiling
        would defeat the point. Paging arrives with the polling loop.

        Args:
            mailbox: SMTP address of the mailbox to read.
            days_back: Only consider mail received within this many days.
            max_messages: Hard ceiling on how many messages to return.
            newest_first: Sort order. True for dry runs, False for production.

        Returns:
            Message dicts with id, receivedDateTime, and hasAttachments.
            Possibly fewer than max_messages once attachment-less mail is dropped.

        Raises:
            SystemProblem: If the mailbox or folder is not on the allowlist,
                or if Graph returns an unrecoverable error.
            GraphAuthError: If the credential is rejected.
        """
        cutoff = (
                datetime.now(timezone.utc) - timedelta(days=days_back)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        direction = "desc" if newest_first else "asc"

        url = self._mailbox_url(mailbox, "mailFolders/deleteditems/messages")
        params = {
            "$filter": f"receivedDateTime ge {cutoff}",
            "$orderby": f"receivedDateTime {direction}",
            "$select": "id,receivedDateTime,hasAttachments,subject,bodyPreview",
            "$top": max_messages,
        }

        response = self._request("GET", url, params=params)
        messages = response.json().get("value", [])

        with_attachments = [m for m in messages if m.get("hasAttachments")]

        logger.info(
            "Listed %s messages from %s (last %s days), %s with attachments",
            len(messages),
            mailbox,
            days_back,
            len(with_attachments),
        )

        return with_attachments

    def get_attachments(self, mailbox: str, message_id: str) -> list[dict]:
        """Return file attachments for one message, with their bytes.

        Inline images and item attachments (forwarded emails, contacts) are
        dropped. Only real files come back.

        Args:
            mailbox: SMTP address of the mailbox holding the message.
            message_id: Graph message ID from list_messages.

        Returns:
            Dicts with name, contentType, size, and contentBytes (base64 str).

        Raises:
            SystemProblem: If the mailbox is not on the allowlist, or if Graph
                returns an unrecoverable error.
            GraphAuthError: If the credential is rejected.
        """
        url = self._mailbox_url(mailbox, f"messages/{message_id}/attachments")

        response = self._request("GET", url)
        attachments = response.json().get("value", [])

        files = [
            attachment
            for attachment in attachments
            if attachment.get("@odata.type") == "#microsoft.graph.fileAttachment"
               and not attachment.get("isInline")
        ]

        logger.debug(
            "Message %s: %s attachments, %s usable files",
            message_id,
            len(attachments),
            len(files),
        )

        return files