from __future__ import annotations
import os
import re
import time
from typing import Optional, Dict, Tuple

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Zakresy – pełny dostęp do Drive + możliwość edycji w Docs/Sheets
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ==========
# Autoryzacja
# ==========
def get_creds():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    def do_full_auth():
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        # pierwsza autoryzacja zwróci refresh token; zapisujemy
        new_creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(new_creds.to_json())
        return new_creds

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # zapisujemy odświeżone dane na wszelki wypadek
                with open("token.json", "w") as f:
                    f.write(creds.to_json())
            except RefreshError:
                # token revoked/expired: czyścimy i robimy pełną autoryzację
                try:
                    os.remove("token.json")
                except FileNotFoundError:
                    pass
                creds = do_full_auth()
        else:
            creds = do_full_auth()

    return creds

# ====================
# Helpery do Drive API
# ====================
def with_retries(func, *args, **kwargs):
    """Prosty retry z backoff na 403/429/5xx."""
    max_attempts = 6
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            status = getattr(e, "status_code", None) or getattr(e, "resp", {}).status if hasattr(e, "resp") else None
            if status in (403, 429, 500, 502, 503, 504):
                if attempt == max_attempts:
                    raise
                time.sleep(delay)
                delay *= 2
            else:
                raise

def extract_id_from_url(url_or_id: str) -> str:
    """Obsługa: pełny URL lub czyste ID."""
    s = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_\-]{20,}", s):
        return s
    # /folders/<id>
    m = re.search(r"/folders/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    # /file/d/<id> (jeśli wskaże folder przez "plik" / skrót)
    m = re.search(r"/file/d/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    # ?id=<id>
    m = re.search(r"[?&]id=([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    raise ValueError("Nie rozpoznano ID z podanego linku/tekstu.")

# =====================
# Twoje dotychczasowe I/O
# =====================
def create_folder(drive, name: str, parent_id: Optional[str] = None) -> dict:
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    return with_retries(
        drive.files().create,
        body=body,
        fields="id,name,webViewLink,parents",
        supportsAllDrives=True,
    ).execute()

def set_anyone_with_link_permission(drive, file_id: str, role: str = "reader"):
    # role: "reader" (podgląd), "commenter" (komentowanie), "writer" (edycja)
    perm = {"type": "anyone", "role": role, "allowFileDiscovery": False}
    with_retries(drive.permissions().create, fileId=file_id, body=perm, supportsAllDrives=True).execute()

def create_google_doc(drive, docs, name: str, folder_id: str, text: str) -> dict:
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [folder_id],
    }
    doc_file = with_retries(
        drive.files().create, body=meta, fields="id,name,webViewLink", supportsAllDrives=True
    ).execute()
    doc_id = doc_file["id"]

    requests = [{"insertText": {"location": {"index": 1}, "text": text}}]
    with_retries(docs.documents().batchUpdate, documentId=doc_id, body={"requests": requests}).execute()
    return doc_file

def create_google_sheet_and_write(drive, sheets, name: str, folder_id: str) -> dict:
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [folder_id],
    }
    sheet_file = with_retries(
        drive.files().create, body=meta, fields="id,name,webViewLink", supportsAllDrives=True
    ).execute()
    sheet_id = sheet_file["id"]

    with_retries(
        sheets.spreadsheets().values().update,
        spreadsheetId=sheet_id,
        range="A1",
        valueInputOption="RAW",
        body={"values": [["Hello from sheet"]]},
    ).execute()
    return sheet_file

# =========================
# NOWOŚĆ: rekurencyjne klonowanie
# =========================
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

def get_file(drive, file_id: str) -> dict:
    return with_retries(
        drive.files().get,
        fileId=file_id,
        fields="id,name,mimeType,parents,shortcutDetails,driveId",
        supportsAllDrives=True,
    ).execute()

def list_children(drive, parent_id: str):
    """Iterator po dzieciach folderu (obsługa paginacji)."""
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
    """
    Kopiuje pojedynczy plik (nie-folder).
    - Google Docs/Sheets/Slides/…: files.copy tworzy natywną kopię.
    - Pliki binarne (PDF, JPG, ZIP…): files.copy tworzy duplikat.
    - Shortcut: kopiujemy *cel* skrótu.
    """
    mime = src_file["mimeType"]
    name = src_file["name"]

    # Shortcut -> kopiuj cel (jeśli dostępny)
    if mime == SHORTCUT_MIME:
        target_id = src_file.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            # Brak celu – pomiń lub skopiuj jako zwykły metadany plik (tu: pomijamy)
            return {}
        # Pobierz realny plik i kopiuj jego treść
        real_src = get_file(drive, target_id)
        return copy_single_file(drive, real_src | {"name": name}, dst_parent_id)

    body = {"name": name, "parents": [dst_parent_id]}
    return with_retries(
        drive.files().copy,
        fileId=src_file["id"],
        body=body,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()

def clone_folder_tree(drive, src_folder_id: str, dst_parent_id: Optional[str]) -> Tuple[str, str]:
    """
    Rekurencyjnie klonuje *folder źródłowy* w nowe miejsce.
    Zwraca (new_folder_id, new_folder_webViewLink).
    """
    src = get_file(drive, src_folder_id)
    if src["mimeType"] != FOLDER_MIME:
        raise ValueError("Podane ID/URL wskazuje na plik, a nie folder. Podaj folder (root) do sklonowania.")

    # Utwórz folder docelowy
    dst_folder = create_folder(drive, f"Kopia: {src['name']} (link-only)", parent_id=dst_parent_id)
    dst_folder_id = dst_folder["id"]

    # Rekurencyjnie kopiuj dzieci
    for child in list_children(drive, src_folder_id):
        if child["mimeType"] == FOLDER_MIME:
            # utwórz podfolder i rekurencja
            sub_dst = create_folder(drive, child["name"], parent_id=dst_folder_id)
            # rekurencja na wnętrzu
            clone_folder_tree_into(drive, child["id"], sub_dst["id"])
        else:
            copy_single_file(drive, child, dst_folder_id)

    return dst_folder_id, dst_folder.get("webViewLink")

def clone_folder_tree_into(drive, src_folder_id: str, dst_folder_id: str):
    """Jak wyżej, ale zakłada, że folder docelowy już istnieje (używane w rekurencji)."""
    for child in list_children(drive, src_folder_id):
        if child["mimeType"] == FOLDER_MIME:
            sub_dst = create_folder(drive, child["name"], parent_id=dst_folder_id)
            clone_folder_tree_into(drive, child["id"], sub_dst["id"])
        else:
            copy_single_file(drive, child, dst_folder_id)

def copy_disk(drive, source_link_or_id: str, anyone_role: str = "reader") -> dict:
    """
    Klonuje cały *folder źródłowy* (traktowany jako „dysk”) do nowego folderu w My Drive.
    Nadaje uprawnienie „anyone with link” na folderze docelowym.
    Zwraca metadane nowego folderu (id, name, webViewLink).
    """
    src_id = extract_id_from_url(source_link_or_id)
    new_root_id, new_root_link = clone_folder_tree(drive, src_id, dst_parent_id=None)

    # anyone with link
    set_anyone_with_link_permission(drive, new_root_id, role=anyone_role)

    # zwrot metadanych
    new_folder = with_retries(
        drive.files().get, fileId=new_root_id, fields="id,name,webViewLink", supportsAllDrives=True
    ).execute()
    return new_folder

# =====
# main()
# =====
def main():
    # jeśli masz stary token z testu readonly, usuń go przed odpaleniem
    # if os.path.exists("token.json"): os.remove("token.json")

    creds = get_creds()
    drive = build("drive", "v3", credentials=creds)
    # docs = build("docs", "v1", credentials=creds)
    # sheets = build("sheets", "v4", credentials=creds)
    #
    # # 1) Folder
    # folder = create_folder(drive, "Publiczny folder (link-only)")
    # folder_id = folder["id"]
    # print(f"Utworzono folder: {folder['name']} → {folder.get('webViewLink')}")
    #
    # # 2) Uprawnienia „każdy z linkiem”
    # set_anyone_with_link_permission(drive, folder_id, role="reader")
    # print("Nadano uprawnienia: każdy z linkiem (reader) dla folderu.")
    #
    # # 3) Dokument Google „Hello world”
    # doc = create_google_doc(drive, docs, "Mój dokument", folder_id, "Hello world")
    # print(f"Utworzono Dokument: {doc['name']} → {doc.get('webViewLink')}")
    #
    # # 4) Arkusz Google z wpisem w A1
    # sheet = create_google_sheet_and_write(drive, sheets, "Mój arkusz", folder_id)
    # print(f"Utworzono Arkusz: {sheet['name']} → {sheet.get('webViewLink')}")

    # 5) NOWOŚĆ: klonowanie innego „dysku” (folderu-źródła)
    #    Podmień poniższą zmienną na link/ID folderu, który chcesz sklonować:
    SOURCE_FOLDER_LINK_OR_ID = "https://drive.google.com/drive/folders/1Qcw5tzmE69ZoSXKmGE0HC0K5L2ss7BmJ"  # np. "https://drive.google.com/drive/folders/<ID>" lub samo "<ID>"

    if SOURCE_FOLDER_LINK_OR_ID:
        cloned = copy_disk(drive, SOURCE_FOLDER_LINK_OR_ID, anyone_role="reader")
        print(f"\nSklonowano! Nowy folder: {cloned['name']} → {cloned.get('webViewLink')}")

    print("\nGotowe! Pliki są w folderze z dostępem dla każdego, kto ma link.")

if __name__ == "__main__":
    main()
