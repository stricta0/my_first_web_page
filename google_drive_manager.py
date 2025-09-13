from __future__ import annotations
import os
import re
import time
from typing import Optional, Tuple, Iterable, Dict, Any

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Scopes – przy kopiowaniu natywnych plików Google warto mieć Docs/Sheets
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
PLACEHOLDER_TOKEN = "IMIE_NAZWISKO"  # dokładnie taki ciąg podmieniamy

# ----------------------------
# Autoryzacja (użyteczne do testów CLI)
# ----------------------------
def get_creds() -> Credentials:
    """
    Lokalna autoryzacja do testów (credentials.json -> token.json).
    W produkcji (Streamlit) przekaż gotowe creds zewnętrznie.
    """
    creds: Optional[Credentials] = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    def do_full_auth() -> Credentials:
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        new_creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(new_creds.to_json())
        return new_creds

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open("token.json", "w") as f:
                    f.write(creds.to_json())
            except RefreshError:
                try:
                    os.remove("token.json")
                except FileNotFoundError:
                    pass
                creds = do_full_auth()
        else:
            creds = do_full_auth()
    return creds


def build_drive(creds: Credentials):
    return build("drive", "v3", credentials=creds)

# ----------------------------
# Retry helper
# ----------------------------
def with_retries(func, *args, **kwargs):
    """
    Prosty retry z wykładniczym backoffem na 403/429/5xx.
    """
    max_attempts = 6
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            status = getattr(e, "status_code", None)
            if status is None and hasattr(e, "resp") and hasattr(e.resp, "status"):
                status = e.resp.status
            if status in (403, 429, 500, 502, 503, 504):
                if attempt == max_attempts:
                    raise
                time.sleep(delay)
                delay *= 2
            else:
                raise

# ----------------------------
# Utils
# ----------------------------
def extract_id_from_url(url_or_id: str) -> str:
    """
    Obsługuje: pełny URL do folderu/plików lub czyste ID.
    """
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


def _rename_with_placeholder(name: str, full_name: Optional[str]) -> str:
    if full_name and PLACEHOLDER_TOKEN in name:
        return name.replace(PLACEHOLDER_TOKEN, full_name)
    return name


def _render_root_name(template: Optional[str], src_name: str, full_name: Optional[str]) -> str:
    """
    Generuje nazwę root-folderu na podstawie:
    - template (jeśli podany) z podmianą IMIE_NAZWISKO
    - albo nazwy źródła (z podmianą IMIE_NAZWISKO) gdy template = None
    """
    if template:
        return template.replace(PLACEHOLDER_TOKEN, full_name or PLACEHOLDER_TOKEN)
    return _rename_with_placeholder(src_name, full_name)

# ----------------------------
# Drive primitives
# ----------------------------
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
    """
    role: "reader" | "commenter" | "writer"
    """
    perm = {"type": "anyone", "role": role, "allowFileDiscovery": False}
    with_retries(
        drive.permissions().create,
        fileId=file_id,
        body=perm,
        supportsAllDrives=True
    ).execute()


# NEW: opcjonalna blokada możliwości dalszego udostępniania przez edytorów
def set_writers_can_share(drive, file_id: str, allow: bool):  # NEW
    with_retries(
        drive.files().update,
        fileId=file_id,
        body={"writersCanShare": allow},
        supportsAllDrives=True
    ).execute()


def get_file(drive, file_id: str) -> dict:
    return with_retries(
        drive.files().get,
        fileId=file_id,
        fields="id,name,mimeType,parents,shortcutDetails",
        supportsAllDrives=True,
    ).execute()


def list_children(drive, parent_id: str) -> Iterable[Dict[str, Any]]:
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


def copy_single_file(drive, src_file: dict, dst_parent_id: str, full_name: Optional[str]) -> dict:
    """
    Kopiuje pojedynczy plik (nie-folder). Dla skrótu kopiuje *cel*,
    zachowując nazwę skrótu (po podmianie PLACEHOLDER_TOKEN -> full_name).
    """
    mime = src_file["mimeType"]
    desired_name = _rename_with_placeholder(src_file["name"], full_name)

    if mime == SHORTCUT_MIME:
        target_id = src_file.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            return {}
        real_src = get_file(drive, target_id)
        # narzuć nazwę po podmianie placeholdera
        real_src = {**real_src, "name": desired_name}
        return copy_single_file(drive, real_src, dst_parent_id, full_name)

    body = {"name": desired_name, "parents": [dst_parent_id]}
    return with_retries(
        drive.files().copy,
        fileId=src_file["id"],
        body=body,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()


def clone_folder_tree(
    drive,
    src_folder_id: str,
    dst_parent_id: Optional[str],
    full_name: Optional[str],
    root_name_template: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Rekurencyjnie klonuje folder źródłowy, podmieniając PLACEHOLDER_TOKEN w nazwach.
    Zwraca (new_folder_id, new_folder_webViewLink).
    """
    src = get_file(drive, src_folder_id)
    if src["mimeType"] != FOLDER_MIME:
        raise ValueError("Podane ID/URL wskazuje na plik, a nie folder.")

    # nazwa roota:
    dst_name = _render_root_name(root_name_template, src["name"], full_name)
    dst_folder = create_folder(drive, dst_name, parent_id=dst_parent_id)
    dst_folder_id = dst_folder["id"]

    # dzieci:
    for child in list_children(drive, src_folder_id):
        if child["mimeType"] == FOLDER_MIME:
            sub_name = _rename_with_placeholder(child["name"], full_name)
            sub_dst = create_folder(drive, sub_name, parent_id=dst_folder_id)
            clone_folder_tree_into(drive, child["id"], sub_dst["id"], full_name)
        else:
            copy_single_file(drive, child, dst_folder_id, full_name)

    return dst_folder_id, dst_folder.get("webViewLink")


def clone_folder_tree_into(drive, src_folder_id: str, dst_folder_id: str, full_name: Optional[str]):
    for child in list_children(drive, src_folder_id):
        if child["mimeType"] == FOLDER_MIME:
            sub_name = _rename_with_placeholder(child["name"], full_name)
            sub_dst = create_folder(drive, sub_name, parent_id=dst_folder_id)
            clone_folder_tree_into(drive, child["id"], sub_dst["id"], full_name)
        else:
            copy_single_file(drive, child, dst_folder_id, full_name)


def copy_disk(
    drive,
    source_link_or_id: str,
    full_name: Optional[str] = None,
    anyone_role: Optional[str] = "reader",   # NEW: Optional -> można wyłączyć „anyone” podając None
    root_name_template: Optional[str] = None,
    lock_editors_sharing: bool = False,      # NEW: writersCanShare=False gdy True
    dst_parent_id: Optional[str] = None,     # NEW: ID folderu docelowego (np. na My Drive konta-bota)
) -> dict:
    """
    Klonuje cały folder (traktowany jako „dysk”), podmienia PLACEHOLDER_TOKEN w nazwach
    na `full_name`, (opcjonalnie) nadaje uprawnienia 'anyone with link',
    (opcjonalnie) blokuje możliwość dalszego udostępniania przez edytorów,
    zwraca metadane nowego folderu.
    Jeśli `root_name_template` jest podany, nazwa roota będzie z niego wyrenderowana.
    Jeśli `dst_parent_id` jest podany – nowy root zostanie utworzony *we wskazanym folderze*.
    """
    src_id = extract_id_from_url(source_link_or_id)
    new_root_id, _ = clone_folder_tree(
        drive,
        src_id,
        dst_parent_id=dst_parent_id,  # <-- ważne: tworzymy kopię we wskazanym folderze
        full_name=full_name.strip() if isinstance(full_name, str) else full_name,
        root_name_template=root_name_template,
    )

    # 1) (opcjonalnie) „anyone with link” (reader/commenter/writer)
    if anyone_role:
        set_anyone_with_link_permission(drive, new_root_id, role=anyone_role)

    # 2) (opcjonalnie) zablokuj share przez edytorów (writersCanShare=false)
    if lock_editors_sharing:
        # zakładamy, że masz helper set_writers_can_share(drive, file_id, allow: bool)
        set_writers_can_share(drive, new_root_id, allow=False)

    # zwrot metadanych
    return with_retries(
        drive.files().get,
        fileId=new_root_id,
        fields="id,name,webViewLink",
        supportsAllDrives=True
    ).execute()

# ----------------------------
# Narzędzia testowe / walidacja
# ----------------------------
def find_items_with_placeholder(drive, root_folder_id: str) -> Iterable[Dict[str, str]]:
    """
    Zwraca generator słowników {id, name, mimeType} dla elementów,
    których nazwa nadal zawiera PLACEHOLDER_TOKEN (do sanity-checku po klonowaniu).
    """
    def _walk(folder_id: str):
        for child in list_children(drive, folder_id):
            if child["mimeType"] == FOLDER_MIME:
                if PLACEHOLDER_TOKEN in child["name"]:
                    yield {"id": child["id"], "name": child["name"], "mimeType": child["mimeType"]}
                yield from _walk(child["id"])
            else:
                if PLACEHOLDER_TOKEN in child["name"]:
                    yield {"id": child["id"], "name": child["name"], "mimeType": child["mimeType"]}

    yield from _walk(root_folder_id)

# ----------------------------
# CLI demo (opcjonalne do lokalnych testów)
# ----------------------------
if __name__ == "__main__":
    # Przykład użycia lokalnie:
    # python google_drive_manager.py
    creds = get_creds()
    drive = build_drive(creds)

    SOURCE = "https://drive.google.com/drive/folders/1Qcw5tzmE69ZoSXKmGE0HC0K5L2ss7BmJ"
    FULL_NAME = "Jan Kowalski"  # <- podmień podczas testu
    ROOT_TEMPLATE = "IMIE_NAZWISKO matura informatyka IT"  # lub None

    cloned = copy_disk(
        drive,
        SOURCE,
        full_name=FULL_NAME,
        anyone_role="writer",           # <- każdy z linkiem może edytować
        root_name_template=ROOT_TEMPLATE,
        lock_editors_sharing=True       # <- edytorzy nie mogą dalej udostępniać
    )
    print(f"Sklonowano: {cloned['name']} → {cloned.get('webViewLink')}")
