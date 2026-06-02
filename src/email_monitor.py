# STANDARD LIBRARY
import time

# THIRD-PARTY IMPORTS
import win32com.client

# LOCAL IMPORTS
from models import Config, Email, PDF

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

    def get_unread_emails(self) -> list[Email]:
        """
        Retrieves all unread emails from each monitored Outlook inbox.

        Iterates over all configured accounts, locates the matching Outlook store,
        and collects unread messages using the Restrict filter. Each message is
        mapped to an Email dataclass. UCN is set to None and resolved downstream.
        Attachments are not processed here and default to an empty list.

        Returns:
            A list of Email objects representing all unread messages across all
            monitored accounts. Returns an empty list if no unread messages are found.
        """
        emails = []

        for account in self.config.accounts: # for each account in a list of accounts
            for store in self.namespace.Stores: # for each store in stores; stores = outlook object model
                if store.DisplayName == account: # If the store name matches the account name
                    inbox = store.GetDefaultFolder(6) # assigning inbox with the return of getDefaultFolder(6) inbox
                    messages = inbox.Items # assigning messages with everything inside that inbox
                    unread_messages = messages.Restrict("[Unread] = True") # assigning unread_messages with anything that is unread in the inbox folder
                    for unread_message in unread_messages: # for each unread_messages in the collection of unread_messages
                        unread_email = Email( # assign unread_emails with a Email Class Object that parses the data into its parameters
                            message_id=unread_message.EntryID,
                            account=account,
                            sender=unread_message.SenderEmailAddress,
                            body=unread_message.Body,
                            subject=unread_message.Subject,
                            ucn=None,
                            attachments=[])

                        emails.append(unread_email) # appends the email class object to the results list

                    break # break to the next account not store

        return emails

    def save_attachments(self, email: Email) -> list[PDF]:
        pass