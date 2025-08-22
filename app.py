import json
import re
import time
from typing import Optional, Tuple

import streamlit as st

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# DWA ZESTAWY ZAKRESÓW: osobno do Drive i do Gmail
SCOPES_DRIVE = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]
SCOPES_GMAIL = ["https://www.googleapis.com/auth/gmail.send"]

def get_drive_creds():
    data = json.loads(st.secrets["token_drive"])
    creds = Credentials.from_authorized_user_info(data, SCOPES_DRIVE)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

def get_gmail_creds():
    data = json.loads(st.secrets["token_gmail"])
    creds = Credentials.from_authorized_user_info(data, SCOPES_GMAIL)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# ===== Helpery Drive (minimalnie uproszczone)
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

def with_retries(func, *args, **kwargs):
    max_attempts = 6
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            status = getattr(e, "status_code", None) or (e.resp.status if hasattr(e, "resp") else None)
            if status in (403, 429, 500, 502, 503, 504):
                if attempt == max_attempts:
                    raise
                time.sleep(delay)
                delay *= 2
            else:
                raise

def extract_id_from_url(url_or_id: str) -> str:
    s = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_\-]{20,}", s):
        return s
    m = re.search(r"/folders/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"/file/d/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    raise ValueError("Nie rozpoznano ID z podanego linku/tekstu.")

def create_folder(drive, name: str, parent_id: Optional[str] = None) -> dict:
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    return with_retries(
        drive.files().create,
        body=body,
        fields="id,name,webViewLink,parents",
        supportsAllDrives=True,
    ).execute()

def set_anyone_with_link_permission(drive, file_id: str, role: str = "reader"):
    perm = {"type": "anyone", "role": role, "allowFileDiscovery": False}
    with_retries(
        drive.permissions().create,
        fileId=file_id,
        body=perm,
        supportsAllDrives=True
    ).execute()

def get_file(drive, file_id: str) -> dict:
    return with_retries(
        drive.files().get,
        fileId=file_id,
        fields="id,name,mimeType,parents,shortcutDetails,driveId",
        supportsAllDrives=True,
    ).execute()

def list_children(drive, parent_id: str):
    page_token = None
    while True:
        resp = with_retries(
            drive.files().list,
            q=f"'{parent_id}' in parents and trashed = false",
            fields="nextPageToken, files(id,name,mimeType,shortcutDetails)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageToken=page_token,
            pageSize=1000,
        ).execute()
        for f in resp.get("files", []):
            yield f
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

def copy_single_file(drive, src_file: dict, dst_parent_id: str) -> dict:
    mime = src_file["mimeType"]
    name = src_file["name"]

    if mime == SHORTCUT_MIME:
        target_id = src_file.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            return {}
        real_src = get_file(drive, target_id)
        real_src["name"] = name
        return copy_single_file(drive, real_src, dst_parent_id)

    body = {"name": name, "parents": [dst_parent_id]}
    return with_retries(
        drive.files().copy,
        fileId=src_file["id"],
        body=body,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()

def clone_folder_tree(drive, src_folder_id: str, dst_parent_id: Optional[str]) -> Tuple[str, str]:
    src = get_file(drive, src_folder_id)
    if src["mimeType"] != FOLDER_MIME:
        raise ValueError("Podane ID/URL wskazuje na plik, a nie folder. Podaj folder do sklonowania.")

    dst_folder = create_folder(drive, f"Kopia: {src['name']} (link-only)", parent_id=dst_parent_id)
    dst_folder_id = dst_folder["id"]

    for child in list_children(drive, src_folder_id):
        if child["mimeType"] == FOLDER_MIME:
            sub_dst = create_folder(drive, child["name"], parent_id=dst_folder_id)
            clone_folder_tree_into(drive, child["id"], sub_dst["id"])
        else:
            copy_single_file(drive, child, dst_folder_id)

    return dst_folder_id, dst_folder.get("webViewLink")

def clone_folder_tree_into(drive, src_folder_id: str, dst_folder_id: str):
    for child in list_children(drive, src_folder_id):
        if child["mimeType"] == FOLDER_MIME:
            sub_dst = create_folder(drive, child["name"], parent_id=dst_folder_id)
            clone_folder_tree_into(drive, child["id"], sub_dst["id"])
        else:
            copy_single_file(drive, child, dst_folder_id)

def copy_disk_and_make_public_link(creds: Credentials) -> str:
    drive = build("drive", "v3", credentials=creds)
    src = st.secrets.get("source_folder")
    src_id = extract_id_from_url(src)
    new_root_id, new_root_link = clone_folder_tree(drive, src_id, dst_parent_id=None)
    set_anyone_with_link_permission(drive, new_root_id, role="reader")
    return new_root_link

# ===== Wysyłka e‑mail przez Gmail API
import base64
from email.mime.text import MIMEText

def send_email_gmail(creds: Credentials, to_addr: str, subject: str, body_text: str):
    gmail = build("gmail", "v1", credentials=creds)
    msg = MIMEText(body_text)
    msg["to"] = to_addr
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"raw": raw}
    sent = gmail.users().messages().send(userId="me", body=body).execute()
    return sent.get("id")

# ====== UI Streamlit
st.set_page_config(page_title="Zapis przez e‑mail", page_icon="✉️", layout="centered")

st.title("Podaj swój adres e‑mail, aby się zapisać")
st.write("Po kliknięciu **Zatwierdź** sklonujemy folder na Dysku Google i wyślemy do Ciebie wiadomość z linkiem.")

email_adres = st.text_input("E‑mail:")
if st.button("Zatwierdź", type="primary"):
    if not email_adres or "@" not in email_adres:
        st.error("Podaj poprawny adres e‑mail.")
    else:
        dodatkowa = ""
        try:
            with st.spinner("Przygotowuję Twoje materiały..."):
                # 1) Drive: klonowanie folderu i publiczny link
                drive_creds = get_drive_creds()
                public_link = copy_disk_and_make_public_link(drive_creds)
                dodatkowa = f"link do dysku: {public_link}"
                # 2) Wczytanie szablonu e‑maila i wstawienie linku
                with open("tresc_emaila.txt", "r", encoding="utf-8") as f:
                    template = f.read()
                body = template.replace("[LINK_DO_GOOGLE_DRIVE]", public_link)
                dodatkowa += "\nprzed wiadomoscia"
                # 3) Gmail: wysyłka wiadomości
                gmail_creds = get_gmail_creds()
                msg_id = send_email_gmail(
                    gmail_creds,
                    to_addr=email_adres,
                    subject="Twoje materiały – link do Dysku Google",
                    body_text=body,
                )

            st.success(f"Wiadomość została wysłana na adres: {email_adres}")
            st.caption(f"(ID wiadomości: {msg_id})")

        except Exception as e:
            st.error(f"Coś poszło nie tak: {e}, \n dodatek: {dodatkowa}")
            st.stop()

