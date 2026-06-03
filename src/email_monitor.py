# STANDARD LIBRARY
import time
import os

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
                        unread_email = Email( # assign unread_emails with an Email Class Object that parses the data into its parameters
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
        """
        Saves all PDF attachments from an email to the configured temp folder.

        Iterates over all attachments on the given email. Raises ValueError if any
        attachment is not a PDF, signaling the pipeline to route the email to manual
        review. Successfully saved attachments are returned as PDF dataclass objects
        with file_name and file_path populated. UCN and extracted_text are set to
        None and resolved downstream.

        Args:
            email: The Email object whose attachments should be saved.

        Returns:
            A list of PDF objects representing the saved attachments.

        Raises:
            ValueError: If any attachment is not a PDF file.
            ValueError: If there is any duplicate named file attachments.
            LookupError: If the email cannot be located in Outlook by its message ID.
            OSError: If an attachment cannot be saved to the temp folder.
        """
        pdfs = [] # 0. create an empty list

        try:
            mail_item = self.namespace.GetItemFromID(email.message_id) # 1. first get the email object
        except Exception as item_id_error:
            raise LookupError(f"Could not locate Item {email.message_id}. Error: {item_id_error}")

        for attachment in mail_item.Attachments: # 2. for every email com object loop through the attachment com object and validate
            if not attachment.FileName.lower().endswith(".pdf"):
                raise ValueError(f"Attachment is not a PDF file: {attachment.FileName}")

        seen_filenames = set()

        for attachment in mail_item.Attachments: # 3. Loop again and save the attachments to temp folder
                if attachment.FileName in seen_filenames:
                    raise ValueError(f"Attachment {attachment.FileName} is already saved to {email.message_id}")
                else:
                    attachment_file_path = os.path.join(self.config.temp_folder, attachment.FileName)
                    try:
                        attachment.SaveAsFile(attachment_file_path)  # a. save each pdf to temp folder
                        seen_filenames.add(attachment.FileName)

                    except OSError as temp_folder_error:
                        raise OSError(f"Could not save attachment to {self.config.temp_folder}. Error: {temp_folder_error}")
                    pdf = PDF(    # b. make a pdf object
                        file_name=attachment.FileName,
                        file_path= attachment_file_path,
                        ucn=None,
                        extracted_text=None)
                    pdfs.append(pdf) # c. append to results list

        return pdfs # 4. return the result list