import json
import re
import io
from pathlib import Path

import streamlit as st
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
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
                "anyone_role": "writer",
                "lock_editors_sharing": True
            },
            "email": {
                "subject": "Dysk do korepetycji z IT",
                "body_md": "# Cześć, [IMIE_NAZWISKO]!\n\n[**Otwórz folder**]([LINK_DO_GOOGLE_DRIVE])"
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
    auth_mode = (CFG.get("google_drive", {}).get("auth") or "oauth").lower()
    if auth_mode == "sa" and "gcp_sa_drive" in st.secrets:
        sa_val = st.secrets["gcp_sa_drive"]
        info = json.loads(sa_val) if isinstance(sa_val, str) else dict(sa_val)
        return ServiceAccountCredentials.from_service_account_info(info, scopes=SCOPES_DRIVE)

    # OAuth na koncie-bocie (domyślne)
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

def load_email_md_from_disk_or_cfg() -> str:
    """Najpierw spróbuj wczytać templates/email.md, a jeśli brak — weź z configu."""
    p = Path("templates/email.md")
    if p.exists():
        return p.read_text(encoding="utf-8")
    return CFG.get("email", {}).get("body_md") or CFG.get("email", {}).get("body") or \
           "Cześć!\n[LINK_DO_GOOGLE_DRIVE]"

def render_email_body_from_md(md_template: str, link: str, full_name: str) -> tuple[str, str]:
    """
    Buduje treść e-maila na podstawie Markdowna (md_template) z placeholderami:
    [LINK_DO_GOOGLE_DRIVE], [IMIE_NAZWISKO] / IMIE_NAZWISKO oraz [ACCENT].
    Zwraca (plain_text_md, html_email).
    """
    import re
    import markdown2

    # ---- BRAND / CONFIG ----
    brand = CFG.get("brand", {}) if isinstance(CFG, dict) else {}
    brand_name = brand.get("name")  # jeśli None/"" → nagłówek nie będzie renderowany
    accent = brand.get("accent", "#0ea5e9")
    footer = brand.get("footer", "")
    grad_style = (brand.get("gradient_style") or "vibrant").lower()
    page_bg_solid = brand.get("page_bg") or "#FFF7ED"  # stałe, kremowe tło poza kartą

    # ---- GRADIENTY ----
    page_gradients = {
        "vibrant": "linear-gradient(135deg,#bae6fd 0%,#7dd3fc 25%,#60a5fa 55%,#a78bfa 100%)",
        "pastel":  "linear-gradient(135deg,#ebf4ff 0%,#e0f2fe 50%,#f5f3ff 100%)",
        "sunset":  "linear-gradient(135deg,#fecaca 0%,#fda4af 35%,#f0abfc 70%,#c4b5fd 100%)",
    }
    top_strip_gradients = {
        "vibrant": "linear-gradient(90deg,#0ea5e9,#22d3ee,#6366f1,#a855f7)",
        "pastel":  "linear-gradient(90deg,#93c5fd,#a5f3fc,#c7d2fe,#f0abfc)",
        "sunset":  "linear-gradient(90deg,#fb7185,#f59e0b,#ec4899,#8b5cf6)",
    }
    page_grad = page_gradients.get(grad_style, page_gradients["vibrant"])
    top_strip = top_strip_gradients.get(grad_style, top_strip_gradients["vibrant"])

    # ---- PODMIANA PLACEHOLDERÓW ----
    md = (
        md_template
        .replace("[LINK_DO_GOOGLE_DRIVE]", link)
        .replace("[IMIE_NAZWISKO]", full_name)
        .replace("IMIE_NAZWISKO", full_name)
        .replace("[ACCENT]", accent)
    )

    # ---- DZIELENIE NA CZĘŚĆ NAD I POD '---' ----
    parts = re.split(r'^\s*(?:-{3,}|_{3,}|\*{3,})\s*$', md, maxsplit=1, flags=re.MULTILINE)
    if len(parts) == 2:
        md_top, md_bottom = parts
    else:
        md_top, md_bottom = md, ""

    html_top = markdown2.markdown(md_top, extras=["break-on-newline", "fenced-code-blocks", "tables"])
    html_bottom = markdown2.markdown(md_bottom, extras=["break-on-newline", "fenced-code-blocks", "tables"]) if md_bottom else ""

    # ---- OPCJONALNY NAGŁÓWEK MARKI ----
    brand_html = (
        f"<div style='font-size:18px;margin-bottom:16px;'>"
        f"<strong style='color:{accent};'>{brand_name}</strong>"
        f"</div>"
        if brand_name else ""
    )

    # ---- HTML CAŁOŚCI ----
    html = f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:{page_bg_solid};">
    <div style="background:{page_bg_solid};padding:24px 0;">
      <div style="max-width:640px;margin:0 auto;padding:0 24px;">
        <div style="background:#ffffff;border-radius:16px;box-shadow:0 6px 20px rgba(2,6,23,.10);
                    border:1px solid #e5e7eb;overflow:hidden;">
          <div style="height:10px;background:{top_strip};"></div>

          <!-- SEKCJA Z GRADIENTEM (nad '---') -->
          <div style="background:{page_grad};padding:24px;">
            <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial,sans-serif;
                        line-height:1.6;color:#0f172a;">
              {brand_html}
              <div>{html_top}</div>
            </div>
            <div style="height:1px;background:rgba(15,23,42,.22);margin-top:14px;"></div>
          </div>

          <!-- SEKCJA BIAŁA (po '---') -->
          {"<div style='padding:24px;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Helvetica Neue,Arial,sans-serif;line-height:1.6;color:#0f172a;'>" + html_bottom + "</div>" if html_bottom else ""}
        </div>

        <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,'Helvetica Neue',Arial,sans-serif;
                    color:#64748b;font-size:12px;margin-top:12px;text-align:center;">
          {footer}
        </div>
      </div>
    </div>
  </body>
</html>"""

    # plain-text → zwracamy Markdown z podmienionymi placeholderami
    return md, html




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

if st.button("🔎 Test: czy SA widzi folder źródłowy?"):
    try:
        drive_creds = get_drive_creds()
        drive = build_drive(drive_creds)
        from google_drive_manager import extract_id_from_url, get_file
        src = st.secrets.get("source_folder")
        src_id = extract_id_from_url(src)
        meta = get_file(drive, src_id)  # 404 jeśli brak dostępu
        st.success(f"OK: SA widzi „{meta.get('name')}” (ID: {meta.get('id')})")
    except Exception as e:
        st.error("SA nadal nie ma dostępu do folderu. Upewnij się, że udostępniasz **folder** (albo dodaj SA do Dysku współdzielonego).")
        st.code(st.secrets.get("gcp_sa_drive").get("client_email") if isinstance(st.secrets.get("gcp_sa_drive"), dict) else "drive-bot@…", language="text")
        with st.expander("Szczegóły błędu"):
            st.exception(e)


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
            anyone_role = CFG.get("google_drive", {}).get("anyone_role", "writer")  # "reader"/"commenter"/"writer"/None
            lock_share = CFG.get("google_drive", {}).get("lock_editors_sharing", True)
            dest_parent = CFG.get("google_drive", {}).get("destination_parent")
            cloned = copy_disk(
                drive,
                source_folder,
                full_name=full_name.strip(),
                anyone_role=anyone_role,
                root_name_template=name_template,
                lock_editors_sharing=lock_share,
                dst_parent_id=dest_parent
            )
            link = cloned.get("webViewLink")
            folder_name = cloned.get("name", "Nowy folder")
            if not link:
                raise RuntimeError("Nie uzyskano linku do sklonowanego folderu.")

            status.info("✉️ Przygotowywanie wiadomości e-mail…")
            progress.progress(85)
            subject = CFG.get("email", {}).get("subject", "Twoje materiały – link do Dysku Google")
            body_md = load_email_md_from_disk_or_cfg()
            text_body, html_body = render_email_body_from_md(body_md, link, full_name.strip())

            if gmail_creds_available():
                status.info("🚀 Wysyłanie wiadomości e-mail…")
                progress.progress(95)
                gmail_creds = get_gmail_creds()
                msg_id = send_email_gmail_multipart(
                    gmail_creds,
                    to_addr=email.strip(),
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body
                )

                progress.progress(100)
                status.success("Gotowe! Wysłaliśmy wiadomość z linkiem.")
                result.success(
                    f"✅ **{folder_name}** — [Otwórz sklonowany folder]({link})\n\n"
                    f"📩 Wiadomość wysłana na **{email.strip()}** (ID: `{msg_id}`)"
                )
            else:
                progress.progress(100)
                status.warning("Brak konfiguracji wysyłki e-mail (token_gmail). Poniżej podgląd wiadomości do ręcznego wysyłania.")
                result.markdown(f"✅ **{folder_name}** — [Otwórz sklonowany folder]({link})")

                st.subheader("Podgląd wiadomości (Markdown → HTML)")
                st.text_area("Wariant plain-text (zastępczy):", text_body, height=160)
                components.html(html_body, height=420, scrolling=True)

                st.download_button("📥 Pobierz treść e-maila (.txt)",
                                   data=io.BytesIO(text_body.encode("utf-8")),
                                   file_name="wiadomosc.txt", mime="text/plain")
                st.download_button("📥 Pobierz treść e-maila (.html)",
                                   data=io.BytesIO(html_body.encode("utf-8")),
                                   file_name="wiadomosc.html", mime="text/html")

        except Exception as e:
            status.empty()
            progress.empty()
            st.error("Coś poszło nie tak podczas tworzenia kopii lub wysyłki e-maila.")
            with st.expander("Pokaż szczegóły błędu"):
                st.exception(e)
