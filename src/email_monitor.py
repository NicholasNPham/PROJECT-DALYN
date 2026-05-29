# STANDARD LIBRARY
import time

# THIRD-PARTY IMPORTS
import win32com.client

# LOCAL IMPORTS
from models import Config

class EmailMonitor:
    def __init__(self, config: Config) -> None:
        """
        Initializes the EmailMonitor and establishes a connection to Outlook via win32com.

        Attempts to connect up to three times with a short delay between attempts.
        Raises ConnectionError if all attempts fail.

        Args:
            config: The application Config object containing Outlook profile and account settings.

        Raises:
            ConnectionError: If Outlook cannot be reached after three attempts.
        """
        self.config = config

        for attempt in range(self.config.outlook_retry_attempts):
            try:
                self.outlook = win32com.client.Dispatch("Outlook.Application")
                self.namespace = self.outlook.GetNamespace("MAPI")
                break
            except Exception as connection_error:
                time.sleep(self.config.outlook_retry_delay_seconds)
                print(f"Connection Error: {connection_error}. Retrying... (Attempt {attempt + 1})")
        else:
            print(f"Failed to connect to Outlook after {self.config.outlook_retry_attempts} attempts.")
            raise ConnectionError(f"Failed to connect to Outlook after {self.config.outlook_retry_attempts} attempts.")
