import json
import re
import io
from pathlib import Path

import streamlit as st
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from google_drive_manager import copy_disk, build_drive

# --- SCOPES & REGEX ---
SCOPES_DRIVE = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]
SCOPES_GMAIL = ["https://www.googleapis.com/auth/gmail.send"]

EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

# --- CONFIG ---
@st.cache_resource
def load_config():
    p = Path("config.json")
    if not p.exists():
        # sensowne defaulty, gdy config.json brak
        return {
            "google_drive": {
                "root_name_template": "IMIE_NAZWISKO matura informatyka IT",
                "anyone_role": "writer",           # domyślnie każdy z linkiem może edytować
                "lock_editors_sharing": True       # blokuje udostępnianie przez edytorów
            },
            "email": {
                "subject": "Dysk do korepetycji z IT",
                "body_md": "# Cześć, [IMIE_NAZWISKO]!\n\n[**Otwórz folder**]([LINK_DO_GOOGLE_DRIVE])\n\n---\nJeśli link nie działa, skopiuj ten adres:\n\n```\n[LINK_DO_GOOGLE_DRIVE]\n```\n\nPozdrawiam,\nZespół korepetycji IT"
            },
            "brand": {
                "accent": "#0ea5e9",
                "footer": "© 2025 Korepetycje IT • W razie pytań odpisz na tego maila."
            }
        }
    return json.loads(p.read_text(encoding="utf-8"))

CFG = load_config()

# --- CREDS ---
def get_drive_creds() -> Credentials:
    data = json.loads(st.secrets["token_drive"])
    creds = Credentials.from_authorized_user_info(data, SCOPES_DRIVE)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

def gmail_creds_available() -> bool:
    try:
        _ = st.secrets["token_gmail"]
        return True
    except Exception:
        return False

def get_gmail_creds() -> Credentials:
    data = json.loads(st.secrets["token_gmail"])
    creds = Credentials.from_authorized_user_info(data, SCOPES_GMAIL)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# --- EMAIL (Markdown -> HTML, multipart) ---
import base64
import markdown2
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from googleapiclient.discovery import build as gbuild
import streamlit.components.v1 as components

def render_email_body_from_md(md_template: str, link: str, full_name: str) -> tuple[str, str]:
    # podmiana placeholderów
    md = (md_template
          .replace("[LINK_DO_GOOGLE_DRIVE]", link)
          .replace("[IMIE_NAZWISKO]", full_name)
          .replace("IMIE_NAZWISKO", full_name))
    html_core = markdown2.markdown(md, extras=["break-on-newline", "fenced-code-blocks", "tables"])
    accent = CFG.get("brand", {}).get("accent", "#0ea5e9")
    footer = CFG.get("brand", {}).get("footer", "")
    html = f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f6f7f9;">
    <div style="max-width:640px;margin:0 auto;padding:24px;">
      <div style="background:#ffffff;border-radius:12px;padding:24px;
                  box-shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);">
        <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial,sans-serif;
                    line-height:1.5;color:#111827;">
          <div style="font-size:18px;margin-bottom:16px;">
            <strong style="color:{accent};">Korepetycje IT</strong>
          </div>
          <div>{html_core}</div>
        </div>
      </div>
      <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial,sans-serif;
                  color:#6b7280;font-size:12px;margin-top:12px;text-align:center;">
        {footer}
      </div>
    </div>
  </body>
</html>"""
    # plain text fallback = markdown bez renderowania (po podmianie placeholderów)
    text = md
    return text, html

def send_email_gmail_multipart(creds: Credentials, to_addr: str, subject: str, text_body: str, html_body: str) -> str:
    service = gbuild("gmail", "v1", credentials=creds)
    msg = MIMEMultipart("alternative")
    msg["to"] = to_addr
    msg["subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"raw": raw}
    sent = service.users().messages().send(userId="me", body=body).execute()
    return sent.get("id")

# --- Helpers ---
def valid_email(s: str) -> bool:
    return bool(s and EMAIL_REGEX.match(s.strip()))

def valid_full_name(s: str) -> bool:
    s = (s or "").strip()
    return len(s) >= 3 and (" " in s or "-" in s)

# --- UI ---
st.set_page_config(page_title="Kopia materiałów", page_icon="📁", layout="centered")
st.title("Uzyskaj swoją kopię materiałów 📁")
st.write("Wpisz **imię i nazwisko** oraz **adres e-mail**. Skopiujemy folder i wyślemy Ci link.")

with st.form("copy_form", clear_on_submit=False):
    full_name = st.text_input("Imię i nazwisko", placeholder="np. Jan Kowalski")
    email = st.text_input("E-mail", placeholder="np. jan.kowalski@example.com")
    submitted = st.form_submit_button("Zatwierdź", type="primary")

status = st.empty()
progress = st.progress(0)
result = st.empty()

if submitted:
    if not valid_full_name(full_name):
        st.error("Podaj poprawne imię i nazwisko (np. „Jan Kowalski”).")
    elif not valid_email(email):
        st.error("Podaj poprawny adres e-mail (np. jan.kowalski@example.com).")
    else:
        try:
            status.info("🔐 Uzyskiwanie dostępu do Dysku Google…")
            progress.progress(10)
            drive_creds = get_drive_creds()
            drive = build_drive(drive_creds)

            status.info("🧭 Sprawdzanie konfiguracji źródła…")
            progress.progress(30)
            source_folder = st.secrets.get("source_folder")
            if not source_folder:
                raise RuntimeError("Brak konfiguracji: `source_folder` w secrets.")

            status.info("📦 Klonowanie folderu i ustawianie udostępniania…")
            progress.progress(70)

            # nazwa z configu + polityka udostępniania
            name_template = CFG.get("google_drive", {}).get("root_name_template")
            anyone_role = CFG.get("google_drive", {}).get("anyone_role", "writer")  # może być "reader"/"commenter"/"writer"/None
            lock_share = CFG.get("google_drive", {}).get("lock_editors_sharing", True)

            cloned = copy_disk(
                drive,
                source_folder,
                full_name=full_name.strip(),
                anyone_role=anyone_role,
                root_name_template=name_template,
                lock_editors_sharing=lock_share
            )
            link = cloned.get("webViewLink")
            folder_name = cloned.get("name", "Nowy folder")
            if not link:
                raise RuntimeError("Nie uzyskano linku do sklonowanego folderu.")

            status.info("✉️ Przygotowywanie wiadomości e-mail…")
            progress.progress(85)
            subject = CFG.get("email", {}).get("subject", "Twoje materiały – link do Dysku Google")
            # obsługujemy body_md; jeśli ktoś ma stare 'body', też zadziała:
            body_md = CFG.get("email", {}).get("body_md") or CFG.get("email", {}).get("body") or "Cześć!\n[LINK_DO_GOOGLE_DRIVE]"
            text_body, html_body = render_email_body_from_md(body_md, link, full_name.strip())

            if gmail_creds_available():
                status.info("🚀 Wysyłanie wiadomości e-mail…")
                progress.progress(95)
                gmail_creds = get_gmail_creds()
                msg_id = send_email_gmail_multipart(gmail_creds, to_addr=email.strip(), subject=subject, text_body=text_body, html_body=html_body)

                progress.progress(100)
                status.success("Gotowe! Wysłaliśmy wiadomość z linkiem.")
                result.success(
                    f"✅ **{folder_name}** — [Otwórz sklonowany folder]({link})\n\n"
                    f"📩 Wiadomość wysłana na **{email.strip()}** (ID: `{msg_id}`)"
                )
            else:
                progress.progress(100)
                status.warning("Brak konfiguracji wysyłki e-mail (token_gmail). Poniżej podgląd wiadomości do ręcznego wysłania.")
                result.markdown(f"✅ **{folder_name}** — [Otwórz sklonowany folder]({link})")

                st.subheader("Podgląd wiadomości (Markdown → HTML)")
                st.text_area("Wariant plain-text (zastępczy):", text_body, height=160)
                # szybki podgląd HTML w osadzonym iframe
                components.html(html_body, height=420, scrolling=True)

                # przyciski pobierania
                st.download_button("📥 Pobierz treść e-maila (.txt)", data=io.BytesIO(text_body.encode("utf-8")), file_name="wiadomosc.txt", mime="text/plain")
                st.download_button("📥 Pobierz treść e-maila (.html)", data=io.BytesIO(html_body.encode("utf-8")), file_name="wiadomosc.html", mime="text/html")

        except Exception as e:
            status.empty()
            progress.empty()
            st.error("Coś poszło nie tak podczas tworzenia kopii lub wysyłki e-maila.")
            with st.expander("Pokaż szczegóły błędu"):
                st.exception(e)
