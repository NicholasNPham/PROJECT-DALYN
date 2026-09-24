"""All Microsoft Graph calls for DALYN. The only file that talks to Microsoft."""

import msal

from exceptions import GraphAuthError
from logger import get_logger

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

logger = get_logger(__name__)


class GraphClient:
    """Signs in as the DALYN app and makes Graph calls against the monitored mailboxes."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        """Build the MSAL client. Does not contact Microsoft until a token is requested.

        Args:
            tenant_id: Directory (tenant) ID from the app registration.
            client_id: Application (client) ID from the app registration.
            client_secret: The secret Value, not the Secret ID.

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