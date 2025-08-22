import os
import base64
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

class EmailSender:
    def __init__(self, creds_file="credentials.json", token_file="token_email.json"):
        self.creds_file = creds_file
        self.token_file = token_file
        self.creds = self._get_creds()
        self.service = build("gmail", "v1", credentials=self.creds)

    def _get_creds(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        def do_full_auth():
            flow = InstalledAppFlow.from_client_secrets_file(self.creds_file, SCOPES)
            new_creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as f:
                f.write(new_creds.to_json())
            return new_creds

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(self.token_file, "w") as f:
                        f.write(creds.to_json())
                except RefreshError:
                    try:
                        os.remove(self.token_file)
                    except FileNotFoundError:
                        pass
                    creds = do_full_auth()
            else:
                creds = do_full_auth()

        return creds

    def send_email(self, email_adres: str, title: str, text: str):
        """Wyślij wiadomość email na wskazany adres"""
        message = MIMEText(text)
        message["to"] = email_adres
        message["subject"] = title

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body = {"raw": raw}

        sent_message = (
            self.service.users().messages().send(userId="me", body=body).execute()
        )
        print(f"Wysłano wiadomość do {email_adres}, id: {sent_message['id']}")
        return sent_message
