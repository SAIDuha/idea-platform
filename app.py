from __future__ import annotations
import base64
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
import os

import smtplib
from email.message import EmailMessage

from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename
import google.generativeai as genai

# --- Google Sheets & Drive ---
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# OAuth utilisateur pour Drive
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# ------------ Config globale ------------

# ------------ Config globale ------------

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY manquante. Configure-la dans les variables d'environnement (Render).")

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path("ideas.db")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB

genai.configure(api_key=API_KEY)


PREFERRED_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-flash-latest",
    "gemini-pro-latest",
    "gemini-2.5-flash-preview-05-20",
    "gemini-2.5-flash-lite-preview-06-17",
]


def pick_model() -> str:
    try:
        available = {}
        for m in genai.list_models():
            name = m.name.split("/", 1)[-1]
            methods = set(getattr(m, "supported_generation_methods", []) or [])
            if "generateContent" in methods:
                available[name] = True

        for wanted in PREFERRED_MODELS:
            if wanted in available:
                return wanted
    except Exception:
        pass
    return "gemini-flash-latest"


MODEL_ID = pick_model()

# ------------ Config URL publique & SMTP ------------

PUBLIC_BASE_URL = None  # ex: "https://idea.entreprise.fr"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "said.eljamii@cawe.com"
SMTP_PASS = "bcrvnhkimbyptjzo"
IDEA_TEAM_EMAIL = "said.eljamii@cawe.com"

# ------------ Config Google Sheets / Drive ------------

# Fichier de compte de service (clé JSON téléchargée depuis Google Cloud)
SERVICE_ACCOUNT_FILE = "service_account.json"

# Scopes pour Sheets + Drive
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ID du Google Sheets (partie entre /d/ et /edit dans l’URL)
GSHEET_ID = "1Bet8xflUcVb6lXNR3zW1yRZMRznvun6NEppx9GGl8Wk"

# Nom de l’onglet
GSHEET_SHEET_NAME = "Feuille 1"


def get_google_credentials():
    """
    Récupère les credentials Google depuis :
    1. Variable d'environnement GOOGLE_SERVICE_ACCOUNT (JSON string)
    2. Fichier service_account.json
    """
    import json
    
    # Option 1: Variable d'environnement (recommandé pour Render)
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if service_account_json:
        try:
            service_account_info = json.loads(service_account_json)
            creds = Credentials.from_service_account_info(
                service_account_info, scopes=SCOPES
            )
            print("[INFO] Credentials chargés depuis GOOGLE_SERVICE_ACCOUNT")
            return creds
        except Exception as e:
            print(f"[WARN] Erreur lecture GOOGLE_SERVICE_ACCOUNT: {e}")
    
    # Option 2: Fichier local
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        print("[INFO] Credentials chargés depuis service_account.json")
        return creds
    
    return None


def get_sheets_service():
    """Initialise le client Google Sheets à partir du compte de service."""
    creds = get_google_credentials()
    if not creds:
        raise FileNotFoundError(
            "Credentials Google non trouvés. "
            "Configurez GOOGLE_SERVICE_ACCOUNT (variable d'env) ou service_account.json"
        )
    service = build("sheets", "v4", credentials=creds)
    return service


def append_idea_to_sheet(row: list[str]) -> None:
    """
    Ajoute une ligne dans le Google Sheet.
    row = liste ordonnée correspondant aux colonnes de l’onglet.
    """
    if not GSHEET_ID:
        print("[WARN] GSHEET_ID non configuré, écriture Google Sheets ignorée.")
        return
    try:
        service = get_sheets_service()
        body = {"values": [row]}
        service.spreadsheets().values().append(
            spreadsheetId=GSHEET_ID,
            range=f"{GSHEET_SHEET_NAME}!A:Z",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
    except Exception as e:
        print(f"[WARN] Erreur lors de l’envoi dans Google Sheets : {e}")


# ------------ Google Drive helpers ------------

def get_drive_credentials():
    """
    Récupère les credentials Google Drive depuis :
    1. Variable d'environnement GOOGLE_DRIVE_CREDENTIALS (JSON string)
    2. Fichier credentials_drive.json + token_drive.json (OAuth)
    3. Fallback sur le service account (GOOGLE_SERVICE_ACCOUNT)
    """
    import json
    
    # Option 1: Variable d'environnement GOOGLE_DRIVE_CREDENTIALS
    drive_creds_json = os.environ.get("GOOGLE_DRIVE_CREDENTIALS")
    if drive_creds_json:
        try:
            creds_info = json.loads(drive_creds_json)
            # Si c'est un service account
            if "type" in creds_info and creds_info["type"] == "service_account":
                creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
                print("[INFO] Drive credentials chargés depuis GOOGLE_DRIVE_CREDENTIALS (service account)")
                return creds
            # Si c'est un token OAuth
            elif "refresh_token" in creds_info:
                creds = UserCredentials.from_authorized_user_info(creds_info, SCOPES)
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                print("[INFO] Drive credentials chargés depuis GOOGLE_DRIVE_CREDENTIALS (OAuth)")
                return creds
        except Exception as e:
            print(f"[WARN] Erreur lecture GOOGLE_DRIVE_CREDENTIALS: {e}")
    
    # Option 2: Fichier token_drive.json existant (OAuth)
    token_path = Path("token_drive.json")
    if token_path.exists():
        try:
            creds = UserCredentials.from_authorized_user_file(token_path.as_posix(), SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            print("[INFO] Drive credentials chargés depuis token_drive.json")
            return creds
        except Exception as e:
            print(f"[WARN] Erreur lecture token_drive.json: {e}")
    
    # Option 3: Fichier credentials_drive.json (nécessite OAuth flow - ne marche pas sur serveur)
    if os.path.exists("credentials_drive.json"):
        try:
            flow = InstalledAppFlow.from_client_secrets_file("credentials_drive.json", SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            print("[INFO] Drive credentials obtenus via OAuth flow")
            return creds
        except Exception as e:
            print(f"[WARN] OAuth flow échoué: {e}")
    
    # Option 4: Fallback sur le service account général
    creds = get_google_credentials()
    if creds:
        print("[INFO] Drive utilise le service account général (GOOGLE_SERVICE_ACCOUNT)")
        return creds
    
    return None


def get_drive_service():
    """
    Client Google Drive.
    """
    creds = get_drive_credentials()
    if not creds:
        raise FileNotFoundError(
            "Credentials Drive non trouvés. "
            "Configurez GOOGLE_DRIVE_CREDENTIALS ou GOOGLE_SERVICE_ACCOUNT (variables d'env)"
        )
    service = build("drive", "v3", credentials=creds)
    return service
    return service


DRIVE_PARENT_FOLDER_ID: str | None = None


def get_sheet_parent_folder_id() -> str | None:
    """
    Récupère le dossier parent du Google Sheet.
    Si le Sheet est dans un dossier, on renvoie l'ID de ce dossier.
    Si le Sheet est à la racine du drive, renvoie None.
    """
    global DRIVE_PARENT_FOLDER_ID
    if DRIVE_PARENT_FOLDER_ID is not None:
        return DRIVE_PARENT_FOLDER_ID

    try:
        drive = get_drive_service()
        file_meta = drive.files().get(
            fileId=GSHEET_ID,
            fields="id, name, parents"
        ).execute()
        parents = file_meta.get("parents")
        if parents:
            DRIVE_PARENT_FOLDER_ID = parents[0]
        else:
            DRIVE_PARENT_FOLDER_ID = None
    except Exception as e:
        print(f"[WARN] Impossible de récupérer le dossier parent du Sheet : {e}")
        DRIVE_PARENT_FOLDER_ID = None

    return DRIVE_PARENT_FOLDER_ID


def upload_file_to_drive(local_path: Path, original_name: str) -> tuple[str | None, str | None]:
    """
    Envoie un fichier vers Google Drive dans le même dossier que le Google Sheet.
    Retourne (file_id, web_link) ou (None, None) en cas d'erreur.
    """
    try:
        drive = get_drive_service()
        parent_id = get_sheet_parent_folder_id()

        metadata: dict[str, object] = {"name": original_name}
        if parent_id:
            metadata["parents"] = [parent_id]

        media = MediaFileUpload(local_path.as_posix(), resumable=False)
        created = drive.files().create(
            body=metadata,
            media_body=media,
            fields="id"
        ).execute()

        file_id = created.get("id")
        if not file_id:
            return None, None

        link = f"https://drive.google.com/file/d/{file_id}/view?usp=drivesdk"
        return file_id, link

    except Exception as e:
        print(f"[WARN] Upload vers Google Drive échoué pour {local_path} : {e}")
        return None, None


# ------------ DB & migration légère ------------

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()

        # Schéma cible complet
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ideas (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                idea_code TEXT,
                author_name TEXT,
                country TEXT,
                category TEXT,
                typed_text TEXT,
                audio_path TEXT,
                detected_language TEXT,
                original_text TEXT,
                french_translation TEXT,
                site TEXT,
                service TEXT,
                function_title TEXT,
                professional_email TEXT,
                contact_mode TEXT,
                idea_title TEXT,
                share_types TEXT,
                impact_main TEXT,
                impact_other TEXT,
                source TEXT,
                media_paths TEXT
            );
            """
        )

        cur.execute("PRAGMA table_info(ideas)")
        existing_cols = {row[1] for row in cur.fetchall()}

        desired_extra = {
            "idea_code": "TEXT",
            "author_name": "TEXT",
            "country": "TEXT",
            "category": "TEXT",
            "typed_text": "TEXT",
            "audio_path": "TEXT",
            "detected_language": "TEXT",
            "original_text": "TEXT",
            "french_translation": "TEXT",
            "site": "TEXT",
            "service": "TEXT",
            "function_title": "TEXT",
            "professional_email": "TEXT",
            "contact_mode": "TEXT",
            "idea_title": "TEXT",
            "share_types": "TEXT",
            "impact_main": "TEXT",
            "impact_other": "TEXT",
            "source": "TEXT",
            "media_paths": "TEXT",
        }

        for col, col_type in desired_extra.items():
            if col not in existing_cols:
                cur.execute(f"ALTER TABLE ideas ADD COLUMN {col} {col_type}")

        con.commit()


init_db()

# ------------ Utils JSON / MIME / Mail ------------

JSON_CLEANER = re.compile(r"```(?:json)?\s*|```", re.IGNORECASE)


def force_json(text: str) -> dict:
    cleaned = JSON_CLEANER.sub("", text or "").strip()
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e != -1 and e > s:
        cleaned = cleaned[s: e + 1]
    try:
        return json.loads(cleaned)
    except Exception:
        return {}


def allowed_mime(m: str) -> bool:
    base = (m or "").split(";")[0].strip().lower()
    return base in {
        "audio/webm",
        "audio/ogg",
        "audio/mpeg",
        "audio/mp4",
        "audio/wav",
        "audio/x-wav",
        "audio/3gpp",
        "audio/3gpp2",
    }


def make_abs_url(path: str) -> str:
    path = path or ""
    if PUBLIC_BASE_URL:
        base = PUBLIC_BASE_URL.rstrip("/")
    else:
        base = (request.url_root or "").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def format_email_from_idea(data: dict) -> str:
    def or_dash(v):
        return v if (v is not None and str(v).strip() != "") else "—"

    share_types = ", ".join(data.get("share_types") or []) or "—"
    media_paths = data.get("media_paths") or []
    media_block = "\n".join(f"• {url}" for url in media_paths) or "Aucun média associé"

    body = f"""Bonjour,

Une nouvelle IDEA vient d’être déposée sur la plateforme.

[Identification]
Code IDEA : {or_dash(data.get("idea_code"))}

[Profil]
Nom & prénom : {or_dash(data.get("author_name"))}
Site : {or_dash(data.get("site"))}
Service : {or_dash(data.get("service"))}
Fonction : {or_dash(data.get("function_title"))}

[Contact]
E-mail professionnel : {or_dash(data.get("professional_email"))}
Préférence de contact : {or_dash(data.get("contact_mode"))}

[IDEA]
Titre : {or_dash(data.get("idea_title"))}
Type(s) : {share_types}
Impact principal : {or_dash(data.get("impact_main"))}
Impact précisé : {or_dash(data.get("impact_other"))}

Description (texte saisi) :
{or_dash(data.get("typed_text"))}

Transcription de l’enregistrement
Langue détectée : {or_dash(data.get("detected_language"))}

Texte d'origine :
{or_dash(data.get("original_text"))}

Traduction française :
{or_dash(data.get("french_translation"))}

Médias associés :
{media_block}

---

ID interne de l’IDEA : {or_dash(data.get("_id"))}
Date de création (UTC) : {or_dash(data.get("_created_at"))}

Ceci est un message automatique généré par la plateforme IDEA.
"""
    return body


def send_email_to_idea_team(subject: str, body: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and IDEA_TEAM_EMAIL):
        print("[WARN] SMTP non configuré ; mail non envoyé.")
        return

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = IDEA_TEAM_EMAIL
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def send_email_confirmation_to_user(user_email: str, data: dict):
    """
    E-mail simple de confirmation envoyé à l'utilisateur
    si une adresse e-mail professionnelle est fournie.
    """
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        print("[WARN] SMTP non configuré ; mail utilisateur non envoyé.")
        return

    if not user_email:
        return

    idea_code = data.get("idea_code") or "IDEA"
    idea_title = data.get("idea_title") or "Sans titre"
    author_name = data.get("author_name") or ""

    subject = f"Confirmation de dépôt – {idea_code}"
    body = f"""Bonjour {author_name},

Votre IDEA a bien été enregistrée.

Référence : {idea_code}
Titre : {idea_title}

Merci pour votre contribution.

Ceci est un message automatique.
"""

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = user_email
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"[INFO] Mail de confirmation envoyé à {user_email}")
    except Exception as e:
        print(f"[WARN] Erreur envoi mail confirmation : {e}")


# ------------ Génération du code IDEA ------------

def generate_idea_code(con: sqlite3.Connection, created_dt: datetime) -> str:
    """
    Génère un code de type IDEAyyMMxxxxxx
    - yy : année sur 2 chiffres
    - MM : mois sur 2 chiffres
    - xxxxxx : numéro d’idée sur 6 chiffres, incrémenté à l’intérieur du mois.
    """
    year2 = created_dt.strftime("%y")
    month2 = created_dt.strftime("%m")
    ym = created_dt.strftime("%Y-%m")

    cur = con.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM ideas WHERE substr(created_at, 1, 7) = ?",
        (ym,),
    )
    row = cur.fetchone()
    count = int(row[0]) if row and row[0] is not None else 0
    seq = count + 1

    return f"IDEA{year2}{month2}{seq:06d}"


# ------------ Génération des labels médias pour Google Sheets ------------

def build_media_labels(idea_code: str, media_paths: list[str]) -> list[str]:
    """
    À partir du code idée (ex: IDEA2511000006) et de la liste des chemins médias
    (ex: ['/uploads/xxxx.png', '/uploads/yyyy.mp4']),
    retourne une liste de labels type :
      IDEA2511000006_IMG_1
      IDEA2511000006_IMG_2
      IDEA2511000006_VID_1
      ...
    """
    img_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    vid_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    labels: list[str] = []
    img_count = 0
    vid_count = 0
    other_count = 0

    for p in media_paths:
        suffix = Path(p).suffix.lower()

        if suffix in img_exts:
            img_count += 1
            labels.append(f"{idea_code}_IMG_{img_count}")
        elif suffix in vid_exts:
            vid_count += 1
            labels.append(f"{idea_code}_VID_{vid_count}")
        else:
            other_count += 1
            labels.append(f"{idea_code}_MEDIA_{other_count}")

    return labels


# ------------ Debug / Health ------------

@app.route("/health")
def health():
    return jsonify({"ok": True, "model": MODEL_ID})


@app.route("/api/models")
def list_models():
    out = []
    try:
        for m in genai.list_models():
            out.append(
                {
                    "name": m.name.split("/", 1)[-1],
                    "methods": getattr(m, "supported_generation_methods", []),
                }
            )
        return jsonify({"ok": True, "models": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------ Routes front ------------

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/uploads/<path:filename>")
def get_upload(filename):
    return send_from_directory(str(UPLOAD_DIR), filename, as_attachment=False)


# ------------ Upload médias (images / vidéos) ------------

@app.route("/api/upload_media", methods=["POST"])
def upload_media():
    files = request.files.getlist("media")
    if not files:
        return jsonify({"ok": False, "error": "Aucun média reçu."}), 400

    paths = []
    for f in files:
        if not f.filename:
            continue
        filename = secure_filename(f.filename)
        save_name = f"{uuid.uuid4().hex}-{filename}"
        save_path = UPLOAD_DIR / save_name
        f.save(save_path)
        paths.append(f"/uploads/{save_name}")

    if not paths:
        return jsonify({"ok": False, "error": "Aucun fichier valide."}), 400

    return jsonify({"ok": True, "paths": paths})


# ------------ Transcription / traduction audio ------------

@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    if "audio" not in request.files:
        return jsonify({"ok": False, "error": "Aucun fichier audio reçu (clé 'audio')."}), 400

    f = request.files["audio"]
    filename = secure_filename(f.filename or f"record-{uuid.uuid4().hex}.webm")
    mime_raw = f.mimetype or "application/octet-stream"
    mime = mime_raw.split(";")[0]

    if not allowed_mime(mime_raw):
        return jsonify({"ok": False, "error": f"Type audio non supporté: {mime_raw}"}), 400

    save_name = f"{uuid.uuid4().hex}-{filename}"
    save_path = UPLOAD_DIR / save_name
    f.save(save_path)

    system_prompt = (
        "Tu es un assistant de transcription/traduction. "
        "1) Transcris EXACTEMENT le contenu de l'audio dans sa langue d'origine. "
        "2) Détecte la langue (code ISO ou nom). "
        "3) Fournis une traduction fidèle en français. "
        "4) Génère un titre court et accrocheur (max 10 mots) qui résume l'idée principale, dans la langue d'origine. "
        "5) Génère ce même titre traduit en français. "
        "Réponds STRICTEMENT en JSON:\n"
        "{"
        "  \"language\": \"<code ou nom>\","
        "  \"original_text\": \"<transcription>\","
        "  \"french_translation\": \"<traduction française>\","
        "  \"suggested_title\": \"<titre dans la langue d'origine>\","
        "  \"suggested_title_fr\": \"<titre en français>\""
        "}"
    )

    try:
        model = genai.GenerativeModel(MODEL_ID)

        try:
            with open(save_path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("utf-8")
            resp = model.generate_content(
                [
                    {"text": system_prompt},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
                request_options={"timeout": 25}
            )
        except Exception as e_inline:
            try:
                uploaded = genai.upload_file(save_path.as_posix(), mime_type=mime)
                resp = model.generate_content(
                    [
                        {"text": system_prompt},
                        uploaded,
                    ],
                    request_options={"timeout": 25}
                )
            except Exception as e_upload:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"Echec envoi audio à Gemini: inline={e_inline}, upload={e_upload}",
                            "audio_path": f"/uploads/{save_name}",
                        }
                    ),
                    500,
                )

        data = force_json(getattr(resp, "text", "") or "{}")
        language = (data.get("language") or "").strip()
        original_text = (data.get("original_text") or "").strip()
        french_translation = (data.get("french_translation") or "").strip()
        suggested_title = (data.get("suggested_title") or "").strip()
        suggested_title_fr = (data.get("suggested_title_fr") or "").strip()

        if not (language or original_text or french_translation):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Réponse Gemini vide ou non JSON",
                        "raw": getattr(resp, "text", ""),
                        "candidates": [
                            getattr(c, "finish_reason", None)
                            for c in getattr(resp, "candidates", [])
                        ],
                        "audio_path": f"/uploads/{save_name}",
                    }
                ),
                502,
            )

        return jsonify(
            {
                "ok": True,
                "audio_path": f"/uploads/{save_name}",
                "language": language,
                "original_text": original_text,
                "french_translation": french_translation,
                "suggested_title": suggested_title,
                "suggested_title_fr": suggested_title_fr,
            }
        )

    except Exception as e:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"Transcription/Traduction échouée: {e}",
                    "audio_path": f"/uploads/{save_name}",
                }
            ),
            500,
        )



# ================================================================
# DICTIONNAIRES STATIQUES — traduction instantanée (pas de Gemini)
# ================================================================

_S_VOICE = {
    "fr": {"fr_label":"Français","native_label":"Français","ui":{"title":"Présente-toi à l'oral","intro":"Dans cet enregistrement, indique simplement :","items":["Ton nom.","Ton prénom.","Le site sur lequel tu travailles.","Ton service.","Ta fonction (poste occupé)."],"rec_label":"🎙️ Démarrer l'enregistrement","upload_label":"📁 Importer un audio","notice":"🔒 Ton audio est utilisé uniquement pour générer le texte ci-dessous. Il n'est ni conservé, ni réécouté par une autre personne."}},
    "en": {"fr_label":"Anglais","native_label":"English","ui":{"title":"Introduce yourself verbally","intro":"In this recording, simply state:","items":["Your last name.","Your first name.","The site where you work.","Your department.","Your job title."],"rec_label":"🎙️ Start recording","upload_label":"📁 Import an audio file","notice":"🔒 Your audio is only used to generate the text below. It is neither stored nor listened to by anyone else."}},
    "es": {"fr_label":"Espagnol","native_label":"Español","ui":{"title":"Preséntate oralmente","intro":"En esta grabación, simplemente indica:","items":["Tu apellido.","Tu nombre.","El sitio donde trabajas.","Tu servicio.","Tu función (cargo ocupado)."],"rec_label":"🎙️ Iniciar grabación","upload_label":"📁 Importar un archivo de audio","notice":"🔒 Tu audio se utiliza únicamente para generar el texto a continuación. No se conserva ni lo escucha otra persona."}},
    "de": {"fr_label":"Allemand","native_label":"Deutsch","ui":{"title":"Stelle dich mündlich vor","intro":"Bitte gib in dieser Aufnahme einfach an:","items":["Deinen Nachnamen.","Deinen Vornamen.","Den Standort, an dem du arbeitest.","Deine Abteilung.","Deine Funktion (Stellenbezeichnung)."],"rec_label":"🎙️ Aufnahme starten","upload_label":"📁 Audiodatei importieren","notice":"🔒 Deine Aufnahme wird nur zur Texterkennung verwendet. Sie wird weder gespeichert noch von einer anderen Person angehört."}},
    "it": {"fr_label":"Italien","native_label":"Italiano","ui":{"title":"Presentati oralmente","intro":"In questa registrazione, indica semplicemente:","items":["Il tuo cognome.","Il tuo nome.","Il sito in cui lavori.","Il tuo servizio.","La tua funzione (ruolo ricoperto)."],"rec_label":"🎙️ Avvia la registrazione","upload_label":"📁 Importa un file audio","notice":"🔒 Il tuo audio è utilizzato solo per generare il testo qui sotto. Non viene conservato né ascoltato da un'altra persona."}},
    "pt": {"fr_label":"Portugais","native_label":"Português","ui":{"title":"Apresente-se oralmente","intro":"Nesta gravação, indique simplesmente:","items":["O seu apelido.","O seu nome próprio.","O local onde trabalha.","O seu serviço.","A sua função (cargo ocupado)."],"rec_label":"🎙️ Iniciar gravação","upload_label":"📁 Importar ficheiro de áudio","notice":"🔒 O seu áudio é utilizado apenas para gerar o texto abaixo. Não é conservado nem ouvido por outra pessoa."}},
    "nl": {"fr_label":"Néerlandais","native_label":"Nederlands","ui":{"title":"Stel jezelf mondeling voor","intro":"Geef in deze opname eenvoudig aan:","items":["Je achternaam.","Je voornaam.","De vestiging waar je werkt.","Je afdeling.","Je functie (beklede positie)."],"rec_label":"🎙️ Opname starten","upload_label":"📁 Audiobestand importeren","notice":"🔒 Je audio wordt alleen gebruikt om de onderstaande tekst te genereren. Het wordt niet bewaard en niet beluisterd door een andere persoon."}},
    "pl": {"fr_label":"Polonais","native_label":"Polski","ui":{"title":"Przedstaw się ustnie","intro":"W tym nagraniu podaj po prostu:","items":["Twoje nazwisko.","Twoje imię.","Placówkę, w której pracujesz.","Twój dział.","Twoje stanowisko."],"rec_label":"🎙️ Rozpocznij nagrywanie","upload_label":"📁 Importuj plik audio","notice":"🔒 Twoje nagranie jest używane wyłącznie do wygenerowania poniższego tekstu. Nie jest przechowywane ani odsłuchiwane przez inną osobę."}},
    "ro": {"fr_label":"Roumain","native_label":"Română","ui":{"title":"Prezintă-te oral","intro":"În această înregistrare, indică pur și simplu:","items":["Numele tău.","Prenumele tău.","Locul unde lucrezi.","Serviciul tău.","Funcția ta (postul ocupat)."],"rec_label":"🎙️ Începeți înregistrarea","upload_label":"📁 Importați un fișier audio","notice":"🔒 Înregistrarea dvs. este folosită doar pentru a genera textul de mai jos. Nu este stocată și nu este ascultată de altă persoană."}},
    "ar": {"fr_label":"Arabe","native_label":"العربية","ui":{"title":"قدّم نفسك شفهياً","intro":"في هذا التسجيل، أذكر ببساطة:","items":["اسم العائلة.","الاسم الأول.","الموقع الذي تعمل فيه.","قسمك.","وظيفتك (المنصب الذي تشغله)."],"rec_label":"🎙️ بدء التسجيل","upload_label":"📁 استيراد ملف صوتي","notice":"🔒 يُستخدم تسجيلك فقط لإنشاء النص أدناه. لا يتم حفظه ولا يستمع إليه أي شخص آخر."}},
    "tr": {"fr_label":"Turc","native_label":"Türkçe","ui":{"title":"Kendinizi sözlü olarak tanıtın","intro":"Bu kayıtta yalnızca şunları belirtin:","items":["Soyadınız.","Adınız.","Çalıştığınız tesis.","Bölümünüz.","Göreviniz (üstlendiğiniz pozisyon)."],"rec_label":"🎙️ Kaydı başlat","upload_label":"📁 Ses dosyası içe aktar","notice":"🔒 Sesiniz yalnızca aşağıdaki metni oluşturmak için kullanılır. Saklanmaz ve başka bir kişi tarafından dinlenmez."}},
    "zh": {"fr_label":"Chinois","native_label":"中文","ui":{"title":"请口头介绍自己","intro":"在本录音中，请简单说明：","items":["您的姓氏。","您的名字。","您工作的地点。","您的部门。","您的职位（所担任的岗位）。"],"rec_label":"🎙️ 开始录音","upload_label":"📁 导入音频文件","notice":"🔒 您的录音仅用于生成下方的文字，不会被保存，也不会被他人收听。"}},
    "ja": {"fr_label":"Japonais","native_label":"日本語","ui":{"title":"口頭で自己紹介してください","intro":"この録音では、以下の内容を簡単に述べてください：","items":["苗字。","名前。","勤務地。","所属部署。","役職（担当する業務）。"],"rec_label":"🎙️ 録音を開始","upload_label":"📁 音声ファイルをインポート","notice":"🔒 音声は以下のテキスト生成にのみ使用されます。保存されたり、他の人が聞いたりすることはありません。"}},
    "ko": {"fr_label":"Coréen","native_label":"한국어","ui":{"title":"구두로 자기소개를 해주세요","intro":"이 녹음에서 간단히 다음을 말씀해 주세요:","items":["성(姓).","이름.","근무 사이트.","부서.","직함(담당 직책)."],"rec_label":"🎙️ 녹음 시작","upload_label":"📁 오디오 파일 가져오기","notice":"🔒 귀하의 오디오는 아래 텍스트를 생성하는 데만 사용됩니다. 저장되거나 다른 사람이 듣지 않습니다."}},
    "ru": {"fr_label":"Russe","native_label":"Русский","ui":{"title":"Представьтесь устно","intro":"В этой записи просто укажите:","items":["Вашу фамилию.","Ваше имя.","Место работы.","Ваш отдел.","Вашу должность (занимаемую позицию)."],"rec_label":"🎙️ Начать запись","upload_label":"📁 Импортировать аудиофайл","notice":"🔒 Ваша аудиозапись используется только для создания текста ниже. Она не сохраняется и не прослушивается другим человеком."}},
    "da": {"fr_label":"Danois","native_label":"Dansk","ui":{"title":"Præsenter dig selv mundtligt","intro":"I denne optagelse skal du blot angive:","items":["Dit efternavn.","Dit fornavn.","Det sted, du arbejder.","Din afdeling.","Din funktion (den stilling du varetager)."],"rec_label":"🎙️ Start optagelse","upload_label":"📁 Importer en lydfil","notice":"🔒 Din lyd bruges kun til at generere teksten nedenfor. Den gemmes ikke og lyttes ikke til af en anden person."}},
    "sv": {"fr_label":"Suédois","native_label":"Svenska","ui":{"title":"Presentera dig muntligt","intro":"I den här inspelningen anger du helt enkelt:","items":["Ditt efternamn.","Ditt förnamn.","Den plats där du arbetar.","Din avdelning.","Din funktion (befattning)."],"rec_label":"🎙️ Starta inspelning","upload_label":"📁 Importera en ljudfil","notice":"🔒 Ditt ljud används enbart för att generera texten nedan. Det sparas inte och lyssnas inte på av någon annan."}},
    "no": {"fr_label":"Norvégien","native_label":"Norsk","ui":{"title":"Presenter deg muntlig","intro":"I dette opptaket angir du ganske enkelt:","items":["Etternavnet ditt.","Fornavnet ditt.","Stedet der du jobber.","Avdelingen din.","Funksjonen din (stillingen du har)."],"rec_label":"🎙️ Start opptak","upload_label":"📁 Importer en lydfil","notice":"🔒 Lyden din brukes kun til å generere teksten nedenfor. Den lagres ikke og lyttes ikke til av noen andre."}},
    "fi": {"fr_label":"Finnois","native_label":"Suomi","ui":{"title":"Esittele itsesi suullisesti","intro":"Tässä äänitteessä kerro yksinkertaisesti:","items":["Sukunimesi.","Etunimesi.","Toimipisteesi.","Osastosi.","Toimenkuvasi (tehtävänimike)."],"rec_label":"🎙️ Aloita tallennus","upload_label":"📁 Tuo äänitiedosto","notice":"🔒 Ääntäsi käytetään vain alla olevan tekstin tuottamiseen. Sitä ei tallenneta eikä kukaan muu kuuntele sitä."}},
    "cs": {"fr_label":"Tchèque","native_label":"Čeština","ui":{"title":"Představte se ústně","intro":"V tomto nahrávání jednoduše uveďte:","items":["Vaše příjmení.","Vaše jméno.","Provozovnu, kde pracujete.","Vaše oddělení.","Vaši funkci (zastávanou pozici)."],"rec_label":"🎙️ Spustit nahrávání","upload_label":"📁 Importovat zvukový soubor","notice":"🔒 Váš zvuk je použit pouze pro vytvoření textu níže. Není uchováván ani poslouchán jinou osobou."}},
    "hu": {"fr_label":"Hongrois","native_label":"Magyar","ui":{"title":"Mutatkozzon be szóban","intro":"Ebben a felvételben egyszerűen adja meg:","items":["A vezetéknevét.","A keresztnevét.","A munkahelyét.","Az osztályát.","A beosztását (betöltött pozíció)."],"rec_label":"🎙️ Felvétel indítása","upload_label":"📁 Hangfájl importálása","notice":"🔒 A hangfelvétel kizárólag az alábbi szöveg generálásához kerül felhasználásra. Nem tároljuk, és más személy nem hallgatja meg."}},
    "sk": {"fr_label":"Slovaque","native_label":"Slovenčina","ui":{"title":"Predstavte sa ústne","intro":"V tomto nahrávaní jednoducho uveďte:","items":["Vaše priezvisko.","Vaše meno.","Pracovisko, kde pracujete.","Vaše oddelenie.","Vašu funkciu (zastávanú pozíciu)."],"rec_label":"🎙️ Spustiť nahrávanie","upload_label":"📁 Importovať zvukový súbor","notice":"🔒 Váš zvuk je použitý iba na vytvorenie textu nižšie. Nie je ukladaný ani počúvaný inou osobou."}},
}

_S_PROFILE = {
    "en": {"title":"Let's start with you","intro":"Before we begin, simply tell us <b>who you are</b>, <b>where you work</b> and <b>what your role is</b>.","label_name":"Full name","label_site":"Which site do you work at?","label_service":"Which department do you work in?","label_function":"What is your role?","placeholder_name":"e.g. Marie Dupont","placeholder_site":"Select your site","placeholder_service":"Select your department","placeholder_function":"e.g. Maintenance Technician, Store Manager…","placeholder_other_site":"Enter your site","placeholder_other_service":"Specify your department"},
    "es": {"title":"Empecemos por ti","intro":"Antes de comenzar, indícanos simplemente <b>quién eres</b>, <b>dónde trabajas</b> y <b>cuál es tu función</b>.","label_name":"Nombre y apellidos","label_site":"¿En qué sitio trabajas?","label_service":"¿En qué servicio trabajas?","label_function":"¿Cuál es tu función?","placeholder_name":"Ej.: Marie Dupont","placeholder_site":"Selecciona tu sitio","placeholder_service":"Selecciona tu servicio","placeholder_function":"Ej.: Técnico de mantenimiento, Responsable de tienda…","placeholder_other_site":"Indica tu sitio","placeholder_other_service":"Precisa tu servicio"},
    "de": {"title":"Fangen wir mit dir an","intro":"Bevor wir beginnen, gib uns einfach an, <b>wer du bist</b>, <b>wo du arbeitest</b> und <b>welche Rolle du hast</b>.","label_name":"Vor- und Nachname","label_site":"An welchem Standort arbeitest du?","label_service":"In welcher Abteilung arbeitest du?","label_function":"Was ist deine Funktion?","placeholder_name":"z. B. Marie Dupont","placeholder_site":"Wähle deinen Standort","placeholder_service":"Wähle deine Abteilung","placeholder_function":"z. B. Wartungstechniker, Filialleiter…","placeholder_other_site":"Gib deinen Standort an","placeholder_other_service":"Präzisiere deine Abteilung"},
    "it": {"title":"Iniziamo da te","intro":"Prima di cominciare, indica semplicemente <b>chi sei</b>, <b>dove lavori</b> e <b>qual è il tuo ruolo</b>.","label_name":"Nome e cognome","label_site":"In quale sito lavori?","label_service":"In quale servizio lavori?","label_function":"Qual è la tua funzione?","placeholder_name":"Es.: Marie Dupont","placeholder_site":"Seleziona il tuo sito","placeholder_service":"Seleziona il tuo servizio","placeholder_function":"Es.: Tecnico di manutenzione, Responsabile negozio…","placeholder_other_site":"Indica il tuo sito","placeholder_other_service":"Specifica il tuo servizio"},
    "pt": {"title":"Comecemos por ti","intro":"Antes de começar, indica simplesmente <b>quem és</b>, <b>onde trabalhas</b> e <b>qual é o teu papel</b>.","label_name":"Nome e apelido","label_site":"Em que local trabalhas?","label_service":"Em que serviço trabalhas?","label_function":"Qual é a tua função?","placeholder_name":"Ex.: Marie Dupont","placeholder_site":"Seleciona o teu local","placeholder_service":"Seleciona o teu serviço","placeholder_function":"Ex.: Técnico de manutenção, Responsável de loja…","placeholder_other_site":"Indica o teu local","placeholder_other_service":"Precisa o teu serviço"},
    "nl": {"title":"We beginnen met jou","intro":"Geef ons voor we beginnen aan <b>wie je bent</b>, <b>waar je werkt</b> en <b>wat je rol is</b>.","label_name":"Naam en voornaam","label_site":"Op welke vestiging werk je?","label_service":"In welke afdeling werk je?","label_function":"Wat is jouw functie?","placeholder_name":"bijv. Marie Dupont","placeholder_site":"Selecteer jouw vestiging","placeholder_service":"Selecteer jouw afdeling","placeholder_function":"bijv. Onderhoudstechnicus, Filiaalmanager…","placeholder_other_site":"Geef jouw vestiging aan","placeholder_other_service":"Preciseer jouw afdeling"},
    "pl": {"title":"Zacznijmy od ciebie","intro":"Zanim zaczniemy, po prostu powiedz nam <b>kim jesteś</b>, <b>gdzie pracujesz</b> i <b>jaka jest twoja rola</b>.","label_name":"Imię i nazwisko","label_site":"Na którym stanowisku pracujesz?","label_service":"W jakim dziale pracujesz?","label_function":"Jaka jest twoja funkcja?","placeholder_name":"np. Marie Dupont","placeholder_site":"Wybierz swoje miejsce pracy","placeholder_service":"Wybierz swój dział","placeholder_function":"np. Technik utrzymania ruchu, Kierownik sklepu…","placeholder_other_site":"Podaj swoje miejsce pracy","placeholder_other_service":"Sprecyzuj swój dział"},
    "ro": {"title":"Să începem cu tine","intro":"Înainte de a începe, indică pur și simplu <b>cine ești</b>, <b>unde lucrezi</b> și <b>care este rolul tău</b>.","label_name":"Nume și prenume","label_site":"Pe ce site lucrezi?","label_service":"În ce serviciu lucrezi?","label_function":"Care este funcția ta?","placeholder_name":"Ex.: Marie Dupont","placeholder_site":"Selectează site-ul tău","placeholder_service":"Selectează serviciul tău","placeholder_function":"Ex.: Tehnician de întreținere, Responsabil magazin…","placeholder_other_site":"Indică site-ul tău","placeholder_other_service":"Precizează serviciul tău"},
    "ar": {"title":"لنبدأ بك أنت","intro":"قبل البدء، أخبرنا ببساطة <b>من أنت</b>، <b>أين تعمل</b> و<b>ما هو دورك</b>.","label_name":"الاسم الكامل","label_site":"في أي موقع تعمل؟","label_service":"في أي قسم تعمل؟","label_function":"ما هي وظيفتك؟","placeholder_name":"مثال: محمد علي","placeholder_site":"اختر موقعك","placeholder_service":"اختر قسمك","placeholder_function":"مثال: فني صيانة، مدير متجر…","placeholder_other_site":"أدخل موقعك","placeholder_other_service":"حدد قسمك"},
    "tr": {"title":"Seninle başlayalım","intro":"Başlamadan önce basitçe <b>kim olduğunu</b>, <b>nerede çalıştığını</b> ve <b>rolünün ne olduğunu</b> belirt.","label_name":"Ad ve soyad","label_site":"Hangi tesiste çalışıyorsunuz?","label_service":"Hangi bölümde çalışıyorsunuz?","label_function":"Göreviniz nedir?","placeholder_name":"ör. Marie Dupont","placeholder_site":"Tesisinizi seçin","placeholder_service":"Bölümünüzü seçin","placeholder_function":"ör. Bakım Teknisyeni, Mağaza Müdürü…","placeholder_other_site":"Tesisinizi belirtin","placeholder_other_service":"Bölümünüzü belirtin"},
    "zh": {"title":"从你开始","intro":"在开始之前，请简单说明<b>你是谁</b>、<b>在哪里工作</b>以及<b>你的职位</b>。","label_name":"姓名","label_site":"你在哪个工作地点？","label_service":"你在哪个部门工作？","label_function":"你的职位是什么？","placeholder_name":"例：张伟","placeholder_site":"选择你的工作地点","placeholder_service":"选择你的部门","placeholder_function":"例：维修技术员、商店经理…","placeholder_other_site":"填写你的工作地点","placeholder_other_service":"详细说明你的部门"},
    "ja": {"title":"あなたのことから始めましょう","intro":"始める前に、<b>あなたが誰か</b>、<b>どこで働いているか</b>、<b>あなたの役割</b>を簡単に教えてください。","label_name":"氏名","label_site":"どのサイトで働いていますか？","label_service":"どの部署で働いていますか？","label_function":"あなたの職位は何ですか？","placeholder_name":"例：田中花子","placeholder_site":"勤務地を選択してください","placeholder_service":"部署を選択してください","placeholder_function":"例：保守技術者、店舗責任者…","placeholder_other_site":"勤務地を入力してください","placeholder_other_service":"部署を入力してください"},
    "ko": {"title":"당신부터 시작해요","intro":"시작하기 전에 <b>당신이 누구인지</b>, <b>어디서 일하는지</b>, <b>당신의 역할이 무엇인지</b> 간단히 알려주세요.","label_name":"이름 및 성","label_site":"어느 사이트에서 근무하십니까?","label_service":"어느 부서에서 근무하십니까?","label_function":"귀하의 직함은 무엇입니까?","placeholder_name":"예: 김철수","placeholder_site":"근무지를 선택하세요","placeholder_service":"부서를 선택하세요","placeholder_function":"예: 유지보수 기술자, 매장 관리자…","placeholder_other_site":"근무지를 입력하세요","placeholder_other_service":"부서를 입력하세요"},
    "ru": {"title":"Начнём с тебя","intro":"Прежде чем начать, просто укажи <b>кто ты</b>, <b>где ты работаешь</b> и <b>какова твоя роль</b>.","label_name":"Имя и фамилия","label_site":"На каком объекте ты работаешь?","label_service":"В каком отделе ты работаешь?","label_function":"Какова твоя должность?","placeholder_name":"Напр.: Иван Иванов","placeholder_site":"Выбери свой объект","placeholder_service":"Выбери свой отдел","placeholder_function":"Напр.: Технический специалист, Руководитель магазина…","placeholder_other_site":"Укажи свой объект","placeholder_other_service":"Уточни свой отдел"},
    "da": {"title":"Lad os starte med dig","intro":"Inden vi begynder, skal du blot angive <b>hvem du er</b>, <b>hvor du arbejder</b> og <b>hvad din rolle er</b>.","label_name":"Navn og efternavn","label_site":"Hvilket sted arbejder du på?","label_service":"Hvilken afdeling arbejder du i?","label_function":"Hvad er din funktion?","placeholder_name":"F.eks. Marie Dupont","placeholder_site":"Vælg dit sted","placeholder_service":"Vælg din afdeling","placeholder_function":"F.eks. Vedligeholdelsestekniker, Butikschef…","placeholder_other_site":"Angiv dit sted","placeholder_other_service":"Præciser din afdeling"},
    "sv": {"title":"Låt oss börja med dig","intro":"Innan vi börjar, berätta helt enkelt <b>vem du är</b>, <b>var du arbetar</b> och <b>vilken roll du har</b>.","label_name":"Namn och efternamn","label_site":"På vilken plats arbetar du?","label_service":"På vilken avdelning arbetar du?","label_function":"Vad är din funktion?","placeholder_name":"T.ex. Marie Dupont","placeholder_site":"Välj din plats","placeholder_service":"Välj din avdelning","placeholder_function":"T.ex. Underhållstekniker, Butikschef…","placeholder_other_site":"Ange din plats","placeholder_other_service":"Precisera din avdelning"},
    "no": {"title":"La oss starte med deg","intro":"Før vi begynner, angi ganske enkelt <b>hvem du er</b>, <b>hvor du jobber</b> og <b>hva din rolle er</b>.","label_name":"Navn og etternavn","label_site":"Hvilket sted jobber du på?","label_service":"Hvilken avdeling jobber du i?","label_function":"Hva er din funksjon?","placeholder_name":"F.eks. Marie Dupont","placeholder_site":"Velg ditt sted","placeholder_service":"Velg din avdeling","placeholder_function":"F.eks. Vedlikeholdstekniker, Butikksjef…","placeholder_other_site":"Angi ditt sted","placeholder_other_service":"Presiser din avdeling"},
    "fi": {"title":"Aloitetaan sinusta","intro":"Ennen kuin aloitamme, kerro yksinkertaisesti <b>kuka olet</b>, <b>missä työskentelet</b> ja <b>mikä on roolisi</b>.","label_name":"Etu- ja sukunimi","label_site":"Millä toimipisteellä työskentelet?","label_service":"Millä osastolla työskentelet?","label_function":"Mikä on tehtävänimikkeesi?","placeholder_name":"Esim. Matti Virtanen","placeholder_site":"Valitse toimipisteesi","placeholder_service":"Valitse osastosi","placeholder_function":"Esim. Huoltoteknikko, Myymäläpäällikkö…","placeholder_other_site":"Ilmoita toimipisteesi","placeholder_other_service":"Täsmennä osastosi"},
    "cs": {"title":"Začněme u vás","intro":"Než začneme, jednoduše nám řekněte <b>kdo jste</b>, <b>kde pracujete</b> a <b>jaká je vaše role</b>.","label_name":"Jméno a příjmení","label_site":"Na které provozovně pracujete?","label_service":"V jakém oddělení pracujete?","label_function":"Jaká je vaše funkce?","placeholder_name":"Např. Jan Novák","placeholder_site":"Vyberte svou provozovnu","placeholder_service":"Vyberte své oddělení","placeholder_function":"Např. Technik údržby, Vedoucí prodejny…","placeholder_other_site":"Uveďte svou provozovnu","placeholder_other_service":"Upřesněte své oddělení"},
    "hu": {"title":"Kezdjük veled","intro":"Mielőtt elkezdenénk, egyszerűen mondja el nekünk <b>ki Ön</b>, <b>hol dolgozik</b> és <b>mi a szerepe</b>.","label_name":"Név és keresztnév","label_site":"Melyik telephelyen dolgozik?","label_service":"Melyik osztályon dolgozik?","label_function":"Mi a beosztása?","placeholder_name":"Pl. Kovács János","placeholder_site":"Válassza ki telephelyét","placeholder_service":"Válassza ki osztályát","placeholder_function":"Pl. Karbantartó technikus, Üzletvezető…","placeholder_other_site":"Adja meg telephelyét","placeholder_other_service":"Pontosítsa osztályát"},
    "sk": {"title":"Začnime vami","intro":"Skôr než začneme, jednoducho nám povedzte <b>kto ste</b>, <b>kde pracujete</b> a <b>aká je vaša úloha</b>.","label_name":"Meno a priezvisko","label_site":"Na ktorom pracovisku pracujete?","label_service":"V akom oddelení pracujete?","label_function":"Aká je vaša funkcia?","placeholder_name":"Napr. Ján Novák","placeholder_site":"Vyberte svoje pracovisko","placeholder_service":"Vyberte svoje oddelenie","placeholder_function":"Napr. Technik údržby, Vedúci predajne…","placeholder_other_site":"Uveďte svoje pracovisko","placeholder_other_service":"Upresni svoje oddelenie"},
}

_S_CONTACT = {
    "en": {"section_coords":"Contact details","section_pref":"Contact preference","email_title":"Professional email address","email_label":"If you have a professional email address, enter it below","email_placeholder":"e.g. firstname.lastname@company.com","email_note":"This field is optional, but it helps us follow up on your idea.","pref_title":"How would you like to be contacted?","radio_mail":"Professional email","radio_manager":"Through my manager"},
    "es": {"section_coords":"Datos de contacto","section_pref":"Preferencia de contacto","email_title":"Correo electrónico profesional","email_label":"Si tienes un correo electrónico profesional, anótalo a continuación","email_placeholder":"Ej.: nombre.apellido@empresa.com","email_note":"Este campo es opcional, pero facilita el seguimiento de tu idea.","pref_title":"¿Cómo deseas que te contactemos?","radio_mail":"Correo profesional","radio_manager":"A través de mi responsable"},
    "de": {"section_coords":"Kontaktdaten","section_pref":"Kontaktpräferenz","email_title":"Berufliche E-Mail-Adresse","email_label":"Wenn du eine berufliche E-Mail-Adresse hast, trage sie unten ein","email_placeholder":"z. B. vorname.nachname@unternehmen.de","email_note":"Dieses Feld ist optional, erleichtert aber die Nachverfolgung deiner Idee.","pref_title":"Wie möchtest du kontaktiert werden?","radio_mail":"Berufliche E-Mail","radio_manager":"Über meinen Vorgesetzten"},
    "it": {"section_coords":"Recapiti","section_pref":"Preferenza di contatto","email_title":"Indirizzo email professionale","email_label":"Se hai un indirizzo email professionale, annotalo qui sotto","email_placeholder":"Es.: nome.cognome@azienda.com","email_note":"Questo campo è facoltativo, ma facilita il monitoraggio della tua idea.","pref_title":"Come desideri essere ricontattato/a?","radio_mail":"Email professionale","radio_manager":"Tramite il mio responsabile"},
    "pt": {"section_coords":"Dados de contacto","section_pref":"Preferência de contacto","email_title":"Endereço de email profissional","email_label":"Se tens um endereço de email profissional, indica-o abaixo","email_placeholder":"Ex.: nome.apelido@empresa.com","email_note":"Este campo é facultativo, mas facilita o acompanhamento da tua ideia.","pref_title":"Como deseja ser contactado/a?","radio_mail":"Email profissional","radio_manager":"Através do meu responsável"},
    "nl": {"section_coords":"Contactgegevens","section_pref":"Contactvoorkeur","email_title":"Professioneel e-mailadres","email_label":"Als je een professioneel e-mailadres hebt, vul het hieronder in","email_placeholder":"bijv. voornaam.achternaam@bedrijf.com","email_note":"Dit veld is optioneel, maar het vergemakkelijkt de opvolging van jouw idee.","pref_title":"Hoe wil je gecontacteerd worden?","radio_mail":"Professionele e-mail","radio_manager":"Via mijn leidinggevende"},
    "pl": {"section_coords":"Dane kontaktowe","section_pref":"Preferencje kontaktu","email_title":"Służbowy adres e-mail","email_label":"Jeśli masz służbowy adres e-mail, wpisz go poniżej","email_placeholder":"np. imie.nazwisko@firma.com","email_note":"To pole jest opcjonalne, ale ułatwia śledzenie Twojego pomysłu.","pref_title":"Jak chciałbyś/chciałabyś być kontaktowany/a?","radio_mail":"Służbowy e-mail","radio_manager":"Przez mojego przełożonego"},
    "ro": {"section_coords":"Date de contact","section_pref":"Preferință de contact","email_title":"Adresă de email profesională","email_label":"Dacă ai o adresă de email profesională, notează-o mai jos","email_placeholder":"Ex.: prenume.nume@companie.com","email_note":"Acest câmp este opțional, dar facilitează urmărirea ideii tale.","pref_title":"Cum dorești să fii recontactat(ă)?","radio_mail":"Email profesional","radio_manager":"Prin intermediul managerului meu"},
    "ar": {"section_coords":"بيانات الاتصال","section_pref":"تفضيل الاتصال","email_title":"عنوان البريد الإلكتروني المهني","email_label":"إذا كان لديك عنوان بريد إلكتروني مهني، أدخله أدناه","email_placeholder":"مثال: firstname.lastname@company.com","email_note":"هذا الحقل اختياري، لكنه يسهّل متابعة فكرتك.","pref_title":"كيف تفضل أن يتم التواصل معك؟","radio_mail":"البريد الإلكتروني المهني","radio_manager":"عن طريق مديري"},
    "tr": {"section_coords":"İletişim bilgileri","section_pref":"İletişim tercihi","email_title":"Profesyonel e-posta adresi","email_label":"Profesyonel bir e-posta adresiniz varsa aşağıya girin","email_placeholder":"ör. ad.soyad@sirket.com","email_note":"Bu alan isteğe bağlıdır, ancak fikrinizin takibini kolaylaştırır.","pref_title":"Nasıl iletişime geçilmesini tercih edersiniz?","radio_mail":"Profesyonel e-posta","radio_manager":"Yöneticim aracılığıyla"},
    "zh": {"section_coords":"联系方式","section_pref":"联系偏好","email_title":"职业邮箱地址","email_label":"如果你有职业邮箱，请在下方填写","email_placeholder":"例：firstname.lastname@company.com","email_note":"此字段为选填，但有助于跟进你的建议。","pref_title":"你希望通过哪种方式被联系？","radio_mail":"职业邮箱","radio_manager":"通过我的上级"},
    "ja": {"section_coords":"連絡先","section_pref":"連絡先の希望","email_title":"業務用メールアドレス","email_label":"業務用メールアドレスをお持ちの場合は、以下に入力してください","email_placeholder":"例：firstname.lastname@company.com","email_note":"このフィールドは任意ですが、アイデアのフォローアップに役立ちます。","pref_title":"どのような方法でご連絡を希望しますか？","radio_mail":"業務用メール","radio_manager":"上司を通じて"},
    "ko": {"section_coords":"연락처 정보","section_pref":"연락 방법 선호","email_title":"업무용 이메일 주소","email_label":"업무용 이메일 주소가 있으면 아래에 입력하세요","email_placeholder":"예: firstname.lastname@company.com","email_note":"이 필드는 선택 사항이지만 아이디어 추적에 도움이 됩니다.","pref_title":"어떻게 연락받기를 원하십니까?","radio_mail":"업무용 이메일","radio_manager":"상사를 통해"},
    "ru": {"section_coords":"Контактные данные","section_pref":"Предпочтительный способ связи","email_title":"Рабочий адрес электронной почты","email_label":"Если у тебя есть рабочий email, укажи его ниже","email_placeholder":"Напр.: имя.фамилия@компания.com","email_note":"Это поле необязательное, но помогает отследить твою идею.","pref_title":"Как ты предпочитаешь, чтобы с тобой связались?","radio_mail":"Рабочий email","radio_manager":"Через моего руководителя"},
    "da": {"section_coords":"Kontaktoplysninger","section_pref":"Kontaktpræference","email_title":"Professionel e-mailadresse","email_label":"Hvis du har en professionel e-mailadresse, noter den nedenfor","email_placeholder":"F.eks. fornavn.efternavn@virksomhed.com","email_note":"Dette felt er valgfrit, men det letter opfølgningen af din idé.","pref_title":"Hvordan ønsker du at blive kontaktet?","radio_mail":"Professionel e-mail","radio_manager":"Via min leder"},
    "sv": {"section_coords":"Kontaktuppgifter","section_pref":"Kontaktpreferens","email_title":"Professionell e-postadress","email_label":"Om du har en professionell e-postadress, ange den nedan","email_placeholder":"t.ex. fornamn.efternamn@foretag.com","email_note":"Det här fältet är valfritt men underlättar uppföljningen av din idé.","pref_title":"Hur vill du bli kontaktad?","radio_mail":"Professionell e-post","radio_manager":"Via min chef"},
    "no": {"section_coords":"Kontaktinformasjon","section_pref":"Kontaktpreferanse","email_title":"Profesjonell e-postadresse","email_label":"Hvis du har en profesjonell e-postadresse, noter den nedenfor","email_placeholder":"F.eks. fornavn.etternavn@bedrift.com","email_note":"Dette feltet er valgfritt, men det letter oppfølgingen av ideen din.","pref_title":"Hvordan ønsker du å bli kontaktet?","radio_mail":"Profesjonell e-post","radio_manager":"Via min leder"},
    "fi": {"section_coords":"Yhteystiedot","section_pref":"Yhteydenottotapa","email_title":"Työsähköpostiosoite","email_label":"Jos sinulla on työsähköpostiosoite, kirjoita se alle","email_placeholder":"esim. etunimi.sukunimi@yritys.com","email_note":"Tämä kenttä on vapaaehtoinen, mutta se helpottaa ideasi seurantaa.","pref_title":"Miten haluaisit, että sinuun otetaan yhteyttä?","radio_mail":"Työsähköposti","radio_manager":"Esimieheni kautta"},
    "cs": {"section_coords":"Kontaktní údaje","section_pref":"Preferovaný způsob kontaktu","email_title":"Pracovní e-mailová adresa","email_label":"Pokud máte pracovní e-mailovou adresu, zapište ji níže","email_placeholder":"Např. jmeno.prijmeni@firma.com","email_note":"Toto pole je volitelné, ale usnadňuje sledování vašeho nápadu.","pref_title":"Jak chcete být kontaktováni?","radio_mail":"Pracovní e-mail","radio_manager":"Prostřednictvím mého vedoucího"},
    "hu": {"section_coords":"Elérhetőség","section_pref":"Kapcsolatfelvételi preferencia","email_title":"Munkahelyi e-mail cím","email_label":"Ha rendelkezik munkahelyi e-mail címmel, adja meg alább","email_placeholder":"pl. nev.vezeteknev@ceg.com","email_note":"Ez a mező nem kötelező, de megkönnyíti az ötlet nyomon követését.","pref_title":"Hogyan szeretné, hogy felvegyük Önnel a kapcsolatot?","radio_mail":"Munkahelyi e-mail","radio_manager":"A felettesem útján"},
    "sk": {"section_coords":"Kontaktné údaje","section_pref":"Preferovaný spôsob kontaktu","email_title":"Pracovná e-mailová adresa","email_label":"Ak máte pracovnú e-mailovú adresu, uveďte ju nižšie","email_placeholder":"Napr. meno.priezvisko@firma.com","email_note":"Toto pole je voliteľné, ale uľahčuje sledovanie vášho nápadu.","pref_title":"Akým spôsobom chcete byť kontaktovaný/á?","radio_mail":"Pracovný e-mail","radio_manager":"Prostredníctvom môjho nadriadeného"},
}

_S_IDEA = {
    "en": {"panel_title":"Your idea","panel_intro":"A few elements are enough: the goal is to understand your context, your need and the expected impact.","label_type":"Type of contribution","check_difficulty":"A difficulty","check_improvement":"An improvement","check_innovation":"An innovation","label_title":"Title of your IDEA","placeholder_title":"e.g. Photo reform","label_description":"Description (optional if audio)","placeholder_description":"Describe your idea, your need, your insight…","label_impact":"What main impact would your idea have?","impact_options":{"placeholder":"Select the main impact","ergonomie":"Working conditions / Ergonomics","environnement":"Sustainability / Environment","efficacite":"Time saving / Efficiency","productivite":"Productivity","energie":"Energy saving","securite":"Safety","autre":"Other (specify)"},"label_recording":"Voice recording","btn_rec":"🎙️ Start recording","btn_upload":"📁 Import audio","btn_tone":"🔊 Test sound","label_media":"Illustrations (optional)","label_photos":"Photos / videos","btn_capture":"📷 Take a photo / video","btn_media_upload":"📁 Import from your device","btn_back":"◀ Previous","preview_title":"Preview & translation","preview_intro":"This panel will update as soon as you record or import audio. You can check the understood text before submitting your IDEA.","preview_orig_label":"🗣️ Original text","preview_fr_label":"🇫🇷 French translation","helper_text":"Check quickly: you can then finalize and submit your idea. In case of error, you can correct the text or make a new recording."},
    "es": {"panel_title":"Tu idea","panel_intro":"Bastan unos pocos elementos: el objetivo es comprender tu contexto, tu necesidad y el impacto esperado.","label_type":"Tipo de contribución","check_difficulty":"Una dificultad","check_improvement":"Una mejora","check_innovation":"Una innovación","label_title":"Título de tu IDEA","placeholder_title":"Ej.: Reforma fotográfica","label_description":"Descripción (opcional si hay audio)","placeholder_description":"Describe tu idea, tu necesidad, tu perspectiva…","label_impact":"¿Qué impacto principal tendría tu idea?","impact_options":{"placeholder":"Selecciona el impacto principal","ergonomie":"Condiciones de trabajo / Ergonomía","environnement":"Desarrollo sostenible / Medio ambiente","efficacite":"Ahorro de tiempo / Eficiencia","productivite":"Productividad","energie":"Ahorro de energía","securite":"Seguridad","autre":"Otro (especificar)"},"label_recording":"Grabación de voz","btn_rec":"🎙️ Iniciar grabación","btn_upload":"📁 Importar audio","btn_tone":"🔊 Probar sonido","label_media":"Ilustraciones (opcional)","label_photos":"Fotos / vídeos","btn_capture":"📷 Tomar una foto / vídeo","btn_media_upload":"📁 Importar desde tu dispositivo","btn_back":"◀ Anterior","preview_title":"Vista previa y traducción","preview_intro":"Este panel se actualizará en cuanto grabes o importes un audio. Puedes verificar el texto comprendido antes de enviar tu IDEA.","preview_orig_label":"🗣️ Texto original","preview_fr_label":"🇫🇷 Traducción al francés","helper_text":"Comprueba rápidamente: luego podrás finalizar y enviar tu idea. En caso de error, podrás corregir el texto o hacer una nueva grabación."},
    "de": {"panel_title":"Deine Idee","panel_intro":"Ein paar Elemente reichen: Das Ziel ist, deinen Kontext, deinen Bedarf und die erwarteten Auswirkungen zu verstehen.","label_type":"Beitragstyp","check_difficulty":"Eine Schwierigkeit","check_improvement":"Eine Verbesserung","check_innovation":"Eine Innovation","label_title":"Titel deiner IDEE","placeholder_title":"z. B. Foto-Reform","label_description":"Beschreibung (optional bei Audio)","placeholder_description":"Beschreibe deine Idee, deinen Bedarf, deinen Einblick…","label_impact":"Welche Hauptauswirkung hätte deine Idee?","impact_options":{"placeholder":"Wähle die Hauptauswirkung","ergonomie":"Arbeitsbedingungen / Ergonomie","environnement":"Nachhaltigkeit / Umwelt","efficacite":"Zeitersparnis / Effizienz","productivite":"Produktivität","energie":"Energieeinsparung","securite":"Sicherheit","autre":"Andere (angeben)"},"label_recording":"Sprachaufnahme","btn_rec":"🎙️ Aufnahme starten","btn_upload":"📁 Audio importieren","btn_tone":"🔊 Ton testen","label_media":"Illustrationen (optional)","label_photos":"Fotos / Videos","btn_capture":"📷 Foto / Video aufnehmen","btn_media_upload":"📁 Vom Gerät importieren","btn_back":"◀ Zurück","preview_title":"Vorschau & Übersetzung","preview_intro":"Dieses Fenster wird aktualisiert, sobald du eine Aufnahme machst oder Audio importierst.","preview_orig_label":"🗣️ Originaltext","preview_fr_label":"🇫🇷 Französische Übersetzung","helper_text":"Überprüfe kurz: Du kannst dann deine Idee abschließen und absenden."},
    "it": {"panel_title":"La tua idea","panel_intro":"Bastano pochi elementi: l'obiettivo è capire il tuo contesto, il tuo bisogno e l'impatto atteso.","label_type":"Tipo di contributo","check_difficulty":"Una difficoltà","check_improvement":"Un miglioramento","check_innovation":"Un'innovazione","label_title":"Titolo della tua IDEA","placeholder_title":"Es.: Riforma fotografica","label_description":"Descrizione (opzionale se c'è audio)","placeholder_description":"Descrivi la tua idea, il tuo bisogno, la tua intuizione…","label_impact":"Quale impatto principale avrebbe la tua idea?","impact_options":{"placeholder":"Seleziona l'impatto principale","ergonomie":"Condizioni di lavoro / Ergonomia","environnement":"Sviluppo sostenibile / Ambiente","efficacite":"Risparmio di tempo / Efficienza","productivite":"Produttività","energie":"Risparmio energetico","securite":"Sicurezza","autre":"Altro (specificare)"},"label_recording":"Registrazione vocale","btn_rec":"🎙️ Avvia registrazione","btn_upload":"📁 Importa audio","btn_tone":"🔊 Testa il suono","label_media":"Illustrazioni (facoltativo)","label_photos":"Foto / video","btn_capture":"📷 Scatta una foto / video","btn_media_upload":"📁 Importa dal tuo dispositivo","btn_back":"◀ Precedente","preview_title":"Anteprima e traduzione","preview_intro":"Questo pannello si aggiornerà non appena registri o importi un audio.","preview_orig_label":"🗣️ Testo originale","preview_fr_label":"🇫🇷 Traduzione in francese","helper_text":"Controlla rapidamente: potrai poi finalizzare e inviare la tua idea."},
    "pt": {"panel_title":"A tua ideia","panel_intro":"Bastam alguns elementos: o objetivo é compreender o teu contexto, a tua necessidade e o impacto esperado.","label_type":"Tipo de contribuição","check_difficulty":"Uma dificuldade","check_improvement":"Uma melhoria","check_innovation":"Uma inovação","label_title":"Título da tua IDEIA","placeholder_title":"Ex.: Reforma fotográfica","label_description":"Descrição (opcional se áudio)","placeholder_description":"Descreve a tua ideia, a tua necessidade, o teu insight…","label_impact":"Que impacto principal teria a tua ideia?","impact_options":{"placeholder":"Seleciona o impacto principal","ergonomie":"Condições de trabalho / Ergonomia","environnement":"Desenvolvimento sustentável / Ambiente","efficacite":"Ganho de tempo / Eficiência","productivite":"Produtividade","energie":"Economia de energia","securite":"Segurança","autre":"Outro (especificar)"},"label_recording":"Gravação de voz","btn_rec":"🎙️ Iniciar gravação","btn_upload":"📁 Importar áudio","btn_tone":"🔊 Testar som","label_media":"Ilustrações (facultativo)","label_photos":"Fotos / vídeos","btn_capture":"📷 Tirar uma foto / vídeo","btn_media_upload":"📁 Importar do teu dispositivo","btn_back":"◀ Anterior","preview_title":"Pré-visualização e tradução","preview_intro":"Este painel será atualizado assim que gravares ou importares um áudio.","preview_orig_label":"🗣️ Texto original","preview_fr_label":"🇫🇷 Tradução francesa","helper_text":"Verifica rapidamente: depois podes finalizar e enviar a tua ideia."},
    "nl": {"panel_title":"Jouw idee","panel_intro":"Een paar elementen zijn voldoende: het doel is jouw context, jouw behoefte en de verwachte impact te begrijpen.","label_type":"Type bijdrage","check_difficulty":"Een moeilijkheid","check_improvement":"Een verbetering","check_innovation":"Een innovatie","label_title":"Titel van jouw IDEE","placeholder_title":"bijv. Fotoreform","label_description":"Beschrijving (optioneel bij audio)","placeholder_description":"Beschrijf jouw idee, jouw behoefte, jouw inzicht…","label_impact":"Welke hoofdimpact zou jouw idee hebben?","impact_options":{"placeholder":"Selecteer de hoofdimpact","ergonomie":"Werkomstandigheden / Ergonomie","environnement":"Duurzaamheid / Milieu","efficacite":"Tijdbesparing / Efficiëntie","productivite":"Productiviteit","energie":"Energiebesparing","securite":"Veiligheid","autre":"Ander (specificeer)"},"label_recording":"Spraakopname","btn_rec":"🎙️ Opname starten","btn_upload":"📁 Audio importeren","btn_tone":"🔊 Geluid testen","label_media":"Illustraties (optioneel)","label_photos":"Foto's / video's","btn_capture":"📷 Een foto / video nemen","btn_media_upload":"📁 Importeren van jouw apparaat","btn_back":"◀ Vorige","preview_title":"Voorbeeld & vertaling","preview_intro":"Dit paneel wordt bijgewerkt zodra je opneemt of audio importeert.","preview_orig_label":"🗣️ Originele tekst","preview_fr_label":"🇫🇷 Franse vertaling","helper_text":"Controleer snel: je kunt je idee daarna afronden en indienen."},
    "pl": {"panel_title":"Twój pomysł","panel_intro":"Wystarczy kilka elementów: celem jest zrozumienie Twojego kontekstu, potrzeby i oczekiwanego wpływu.","label_type":"Rodzaj wkładu","check_difficulty":"Trudność","check_improvement":"Usprawnienie","check_innovation":"Innowacja","label_title":"Tytuł Twojego POMYSŁU","placeholder_title":"np. Reforma fotograficzna","label_description":"Opis (opcjonalny przy audio)","placeholder_description":"Opisz swój pomysł, potrzebę, spostrzeżenie…","label_impact":"Jaki główny wpływ miałby Twój pomysł?","impact_options":{"placeholder":"Wybierz główny wpływ","ergonomie":"Warunki pracy / Ergonomia","environnement":"Zrównoważony rozwój / Środowisko","efficacite":"Oszczędność czasu / Efektywność","productivite":"Produktywność","energie":"Oszczędność energii","securite":"Bezpieczeństwo","autre":"Inne (podaj)"},"label_recording":"Nagranie głosowe","btn_rec":"🎙️ Rozpocznij nagrywanie","btn_upload":"📁 Importuj audio","btn_tone":"🔊 Testuj dźwięk","label_media":"Ilustracje (opcjonalnie)","label_photos":"Zdjęcia / filmy","btn_capture":"📷 Zrób zdjęcie / wideo","btn_media_upload":"📁 Importuj z urządzenia","btn_back":"◀ Poprzedni","preview_title":"Podgląd i tłumaczenie","preview_intro":"Ten panel zaktualizuje się po nagraniu lub zaimportowaniu audio.","preview_orig_label":"🗣️ Tekst oryginalny","preview_fr_label":"🇫🇷 Tłumaczenie na francuski","helper_text":"Sprawdź szybko: następnie możesz sfinalizować i przesłać swój pomysł."},
    "ro": {"panel_title":"Ideea ta","panel_intro":"Câteva elemente sunt suficiente: scopul este să înțelegem contextul, nevoia și impactul așteptat.","label_type":"Tip de contribuție","check_difficulty":"O dificultate","check_improvement":"O îmbunătățire","check_innovation":"O inovație","label_title":"Titlul IDEII tale","placeholder_title":"Ex.: Reformă fotografică","label_description":"Descriere (opțional dacă există audio)","placeholder_description":"Descrie ideea ta, nevoia ta, perspectiva ta…","label_impact":"Ce impact principal ar avea ideea ta?","impact_options":{"placeholder":"Selectează impactul principal","ergonomie":"Condiții de muncă / Ergonomie","environnement":"Dezvoltare durabilă / Mediu","efficacite":"Economie de timp / Eficiență","productivite":"Productivitate","energie":"Economie de energie","securite":"Siguranță","autre":"Altul (precizați)"},"label_recording":"Înregistrare vocală","btn_rec":"🎙️ Începeți înregistrarea","btn_upload":"📁 Importați audio","btn_tone":"🔊 Testați sunetul","label_media":"Ilustrații (opțional)","label_photos":"Fotografii / videoclipuri","btn_capture":"📷 Faceți o fotografie / videoclip","btn_media_upload":"📁 Importați de pe dispozitivul dvs.","btn_back":"◀ Anterior","preview_title":"Previzualizare și traducere","preview_intro":"Acest panou se va actualiza de îndată ce înregistrați sau importați un audio.","preview_orig_label":"🗣️ Text original","preview_fr_label":"🇫🇷 Traducere în franceză","helper_text":"Verificați rapid: puteți apoi finaliza și trimite ideea dvs."},
    "ar": {"panel_title":"فكرتك","panel_intro":"عدد قليل من العناصر كافٍ: الهدف هو فهم سياقك واحتياجك والأثر المتوقع.","label_type":"نوع المساهمة","check_difficulty":"صعوبة","check_improvement":"تحسين","check_innovation":"ابتكار","label_title":"عنوان فكرتك","placeholder_title":"مثال: إصلاح الصور","label_description":"الوصف (اختياري إذا كان هناك صوت)","placeholder_description":"صِف فكرتك، احتياجك، رؤيتك…","label_impact":"ما الأثر الرئيسي الذي ستحدثه فكرتك؟","impact_options":{"placeholder":"اختر الأثر الرئيسي","ergonomie":"ظروف العمل / الإرغونوميا","environnement":"التنمية المستدامة / البيئة","efficacite":"توفير الوقت / الكفاءة","productivite":"الإنتاجية","energie":"توفير الطاقة","securite":"السلامة","autre":"أخرى (حدد)"},"label_recording":"التسجيل الصوتي","btn_rec":"🎙️ بدء التسجيل","btn_upload":"📁 استيراد صوت","btn_tone":"🔊 اختبار الصوت","label_media":"صور توضيحية (اختياري)","label_photos":"صور / مقاطع فيديو","btn_capture":"📷 التقاط صورة / فيديو","btn_media_upload":"📁 استيراد من جهازك","btn_back":"◀ السابق","preview_title":"معاينة وترجمة","preview_intro":"سيتم تحديث هذا اللوح بمجرد تسجيل أو استيراد صوت.","preview_orig_label":"🗣️ النص الأصلي","preview_fr_label":"🇫🇷 الترجمة إلى الفرنسية","helper_text":"تحقق بسرعة: يمكنك بعد ذلك إنهاء وإرسال فكرتك."},
    "tr": {"panel_title":"Fikriniz","panel_intro":"Birkaç unsur yeterlidir: amaç bağlamınızı, ihtiyacınızı ve beklenen etkiyi anlamaktır.","label_type":"Katkı türü","check_difficulty":"Bir güçlük","check_improvement":"Bir iyileştirme","check_innovation":"Bir inovasyon","label_title":"FİKRİNİZİN başlığı","placeholder_title":"ör. Fotoğraf reformu","label_description":"Açıklama (ses varsa isteğe bağlı)","placeholder_description":"Fikrinizi, ihtiyacınızı, görüşünüzü açıklayın…","label_impact":"Fikrinizin ana etkisi ne olur?","impact_options":{"placeholder":"Ana etkiyi seçin","ergonomie":"Çalışma koşulları / Ergonomi","environnement":"Sürdürülebilir kalkınma / Çevre","efficacite":"Zaman tasarrufu / Verimlilik","productivite":"Üretkenlik","energie":"Enerji tasarrufu","securite":"Güvenlik","autre":"Diğer (belirtin)"},"label_recording":"Sesli kayıt","btn_rec":"🎙️ Kaydı başlat","btn_upload":"📁 Ses içe aktar","btn_tone":"🔊 Sesi test et","label_media":"İllüstrasyonlar (isteğe bağlı)","label_photos":"Fotoğraflar / videolar","btn_capture":"📷 Fotoğraf / video çek","btn_media_upload":"📁 Cihazınızdan içe aktar","btn_back":"◀ Önceki","preview_title":"Önizleme ve çeviri","preview_intro":"Bu panel ses kaydettiğinizde veya içe aktardığınızda güncellenecektir.","preview_orig_label":"🗣️ Orijinal metin","preview_fr_label":"🇫🇷 Fransızca çeviri","helper_text":"Hızlıca kontrol edin: ardından fikrinizi tamamlayıp gönderebilirsiniz."},
    "zh": {"panel_title":"你的想法","panel_intro":"几个要素就够了：目标是了解你的背景、需求和预期影响。","label_type":"贡献类型","check_difficulty":"一个困难","check_improvement":"一项改进","check_innovation":"一项创新","label_title":"你的IDEA标题","placeholder_title":"例：照片改革","label_description":"描述（有音频时可选）","placeholder_description":"描述你的想法、需求、见解…","label_impact":"你的想法会有什么主要影响？","impact_options":{"placeholder":"选择主要影响","ergonomie":"工作条件 / 人体工程学","environnement":"可持续发展 / 环境","efficacite":"节省时间 / 效率","productivite":"生产力","energie":"节能","securite":"安全","autre":"其他（请说明）"},"label_recording":"语音录制","btn_rec":"🎙️ 开始录音","btn_upload":"📁 导入音频","btn_tone":"🔊 测试声音","label_media":"插图（可选）","label_photos":"照片 / 视频","btn_capture":"📷 拍照 / 录视频","btn_media_upload":"📁 从设备导入","btn_back":"◀ 上一步","preview_title":"预览与翻译","preview_intro":"录音或导入音频后，此面板将更新。","preview_orig_label":"🗣️ 原文","preview_fr_label":"🇫🇷 法语翻译","helper_text":"快速检查：然后你可以完成并提交你的想法。"},
    "ja": {"panel_title":"あなたのアイデア","panel_intro":"いくつかの要素で十分です：目標は、あなたのコンテキスト、ニーズ、期待される影響を理解することです。","label_type":"貢献の種類","check_difficulty":"困難","check_improvement":"改善","check_innovation":"イノベーション","label_title":"あなたのIDEAのタイトル","placeholder_title":"例：写真改革","label_description":"説明（音声がある場合は任意）","placeholder_description":"あなたのアイデア、ニーズ、洞察を説明してください…","label_impact":"あなたのアイデアはどのような主な影響を与えますか？","impact_options":{"placeholder":"主な影響を選択してください","ergonomie":"労働条件 / 人間工学","environnement":"持続可能な開発 / 環境","efficacite":"時間の節約 / 効率","productivite":"生産性","energie":"省エネ","securite":"安全","autre":"その他（詳細を記入）"},"label_recording":"音声録音","btn_rec":"🎙️ 録音を開始","btn_upload":"📁 音声をインポート","btn_tone":"🔊 音をテスト","label_media":"イラスト（任意）","label_photos":"写真 / 動画","btn_capture":"📷 写真 / 動画を撮る","btn_media_upload":"📁 デバイスからインポート","btn_back":"◀ 前へ","preview_title":"プレビューと翻訳","preview_intro":"このパネルは、録音または音声をインポートするとすぐに更新されます。","preview_orig_label":"🗣️ 原文","preview_fr_label":"🇫🇷 フランス語翻訳","helper_text":"すばやく確認してください：その後、アイデアを完成させて送信できます。"},
    "ko": {"panel_title":"당신의 아이디어","panel_intro":"몇 가지 요소면 충분합니다: 목표는 당신의 맥락, 필요 및 예상 영향을 이해하는 것입니다.","label_type":"기여 유형","check_difficulty":"어려움","check_improvement":"개선","check_innovation":"혁신","label_title":"IDEA 제목","placeholder_title":"예: 사진 개혁","label_description":"설명 (오디오가 있는 경우 선택 사항)","placeholder_description":"아이디어, 필요, 통찰력을 설명하세요…","label_impact":"당신의 아이디어는 어떤 주요 영향을 미칠까요?","impact_options":{"placeholder":"주요 영향을 선택하세요","ergonomie":"근무 조건 / 인체공학","environnement":"지속 가능한 개발 / 환경","efficacite":"시간 절약 / 효율성","productivite":"생산성","energie":"에너지 절약","securite":"안전","autre":"기타 (지정)"},"label_recording":"음성 녹음","btn_rec":"🎙️ 녹음 시작","btn_upload":"📁 오디오 가져오기","btn_tone":"🔊 사운드 테스트","label_media":"일러스트 (선택 사항)","label_photos":"사진 / 비디오","btn_capture":"📷 사진 / 비디오 찍기","btn_media_upload":"📁 기기에서 가져오기","btn_back":"◀ 이전","preview_title":"미리보기 및 번역","preview_intro":"이 패널은 녹음하거나 오디오를 가져오는 즉시 업데이트됩니다.","preview_orig_label":"🗣️ 원문","preview_fr_label":"🇫🇷 프랑스어 번역","helper_text":"빠르게 확인하세요: 그런 다음 아이디어를 완성하고 제출할 수 있습니다."},
    "ru": {"panel_title":"Твоя идея","panel_intro":"Нескольких элементов достаточно: цель — понять твой контекст, потребность и ожидаемый эффект.","label_type":"Тип вклада","check_difficulty":"Трудность","check_improvement":"Улучшение","check_innovation":"Инновация","label_title":"Название твоей ИДЕИ","placeholder_title":"Напр.: Фото-реформа","label_description":"Описание (необязательно при наличии аудио)","placeholder_description":"Опиши свою идею, потребность, инсайт…","label_impact":"Какое основное влияние окажет твоя идея?","impact_options":{"placeholder":"Выбери основное влияние","ergonomie":"Условия труда / Эргономика","environnement":"Устойчивое развитие / Окружающая среда","efficacite":"Экономия времени / Эффективность","productivite":"Производительность","energie":"Экономия энергии","securite":"Безопасность","autre":"Другое (укажи)"},"label_recording":"Голосовая запись","btn_rec":"🎙️ Начать запись","btn_upload":"📁 Импортировать аудио","btn_tone":"🔊 Тест звука","label_media":"Иллюстрации (необязательно)","label_photos":"Фото / видео","btn_capture":"📷 Сделать фото / видео","btn_media_upload":"📁 Импортировать с устройства","btn_back":"◀ Назад","preview_title":"Предварительный просмотр и перевод","preview_intro":"Эта панель обновится, как только ты запишешь или импортируешь аудио.","preview_orig_label":"🗣️ Исходный текст","preview_fr_label":"🇫🇷 Перевод на французский","helper_text":"Проверь быстро: затем ты сможешь завершить и отправить свою идею."},
    "da": {"panel_title":"Din idé","panel_intro":"Et par elementer er nok: målet er at forstå din kontekst, dit behov og den forventede effekt.","label_type":"Bidragstype","check_difficulty":"En vanskelighed","check_improvement":"En forbedring","check_innovation":"En innovation","label_title":"Titel på din IDÉ","placeholder_title":"F.eks. Fotoreform","label_description":"Beskrivelse (valgfrit ved audio)","placeholder_description":"Beskriv din idé, dit behov, din indsigt…","label_impact":"Hvilken hovedeffekt ville din idé have?","impact_options":{"placeholder":"Vælg den primære effekt","ergonomie":"Arbejdsforhold / Ergonomi","environnement":"Bæredygtighed / Miljø","efficacite":"Tidsbesparelse / Effektivitet","productivite":"Produktivitet","energie":"Energibesparelse","securite":"Sikkerhed","autre":"Andet (angiv)"},"label_recording":"Stemmeoptag","btn_rec":"🎙️ Start optagelse","btn_upload":"📁 Importer lyd","btn_tone":"🔊 Test lyd","label_media":"Illustrationer (valgfrit)","label_photos":"Fotos / videoer","btn_capture":"📷 Tag et foto / video","btn_media_upload":"📁 Importer fra din enhed","btn_back":"◀ Forrige","preview_title":"Forhåndsvisning og oversættelse","preview_intro":"Dette panel opdateres, så snart du optager eller importerer lyd.","preview_orig_label":"🗣️ Originaltekst","preview_fr_label":"🇫🇷 Fransk oversættelse","helper_text":"Tjek hurtigt: du kan derefter færdiggøre og indsende din idé."},
    "sv": {"panel_title":"Din idé","panel_intro":"Några element räcker: målet är att förstå ditt sammanhang, ditt behov och den förväntade effekten.","label_type":"Typ av bidrag","check_difficulty":"En svårighet","check_improvement":"En förbättring","check_innovation":"En innovation","label_title":"Titel på din IDÉ","placeholder_title":"T.ex. Fotoreform","label_description":"Beskrivning (valfritt vid ljud)","placeholder_description":"Beskriv din idé, ditt behov, din insikt…","label_impact":"Vilken huvudeffekt skulle din idé ha?","impact_options":{"placeholder":"Välj den primära effekten","ergonomie":"Arbetsförhållanden / Ergonomi","environnement":"Hållbar utveckling / Miljö","efficacite":"Tidsbesparings / Effektivitet","productivite":"Produktivitet","energie":"Energibesparing","securite":"Säkerhet","autre":"Annat (specificera)"},"label_recording":"Röstinspelning","btn_rec":"🎙️ Starta inspelning","btn_upload":"📁 Importera ljud","btn_tone":"🔊 Testa ljud","label_media":"Illustrationer (valfritt)","label_photos":"Foton / videor","btn_capture":"📷 Ta ett foto / video","btn_media_upload":"📁 Importera från din enhet","btn_back":"◀ Föregående","preview_title":"Förhandsvisning och översättning","preview_intro":"Den här panelen uppdateras när du spelar in eller importerar ljud.","preview_orig_label":"🗣️ Originaltext","preview_fr_label":"🇫🇷 Fransk översättning","helper_text":"Kontrollera snabbt: du kan sedan slutföra och skicka din idé."},
    "no": {"panel_title":"Din idé","panel_intro":"Noen få elementer er nok: målet er å forstå konteksten, behovet og forventet effekt.","label_type":"Bidragstype","check_difficulty":"En vanskelighet","check_improvement":"En forbedring","check_innovation":"En innovasjon","label_title":"Tittel på din IDÉ","placeholder_title":"F.eks. Fotoreform","label_description":"Beskrivelse (valgfritt ved lyd)","placeholder_description":"Beskriv ideen din, behovet ditt, innsikten din…","label_impact":"Hvilken hovedeffekt ville ideen din ha?","impact_options":{"placeholder":"Velg den primære effekten","ergonomie":"Arbeidsforhold / Ergonomi","environnement":"Bærekraft / Miljø","efficacite":"Tidsbesparelse / Effektivitet","productivite":"Produktivitet","energie":"Energibesparelse","securite":"Sikkerhet","autre":"Annet (spesifiser)"},"label_recording":"Stemmeopptag","btn_rec":"🎙️ Start opptak","btn_upload":"📁 Importer lyd","btn_tone":"🔊 Test lyd","label_media":"Illustrasjoner (valgfritt)","label_photos":"Bilder / videoer","btn_capture":"📷 Ta et bilde / video","btn_media_upload":"📁 Importer fra enheten din","btn_back":"◀ Forrige","preview_title":"Forhåndsvisning og oversettelse","preview_intro":"Dette panelet oppdateres så snart du tar opp eller importerer lyd.","preview_orig_label":"🗣️ Originaltekst","preview_fr_label":"🇫🇷 Fransk oversettelse","helper_text":"Sjekk raskt: du kan deretter fullføre og sende inn ideen din."},
    "fi": {"panel_title":"Ideasi","panel_intro":"Muutama elementti riittää: tavoitteena on ymmärtää kontekstisi, tarpeesi ja odotettu vaikutus.","label_type":"Panoksen tyyppi","check_difficulty":"Vaikeus","check_improvement":"Parannus","check_innovation":"Innovaatio","label_title":"IDEASi otsikko","placeholder_title":"Esim. Valokuvausuudistus","label_description":"Kuvaus (valinnainen äänellä)","placeholder_description":"Kuvaile ideasi, tarpeesi, näkemyksesi…","label_impact":"Mikä olisi ideasi pääasiallinen vaikutus?","impact_options":{"placeholder":"Valitse ensisijainen vaikutus","ergonomie":"Työolot / Ergonomia","environnement":"Kestävä kehitys / Ympäristö","efficacite":"Ajansäästö / Tehokkuus","productivite":"Tuottavuus","energie":"Energiansäästö","securite":"Turvallisuus","autre":"Muu (täsmennä)"},"label_recording":"Äänitys","btn_rec":"🎙️ Aloita tallennus","btn_upload":"📁 Tuo ääni","btn_tone":"🔊 Testaa ääni","label_media":"Kuvitukset (valinnainen)","label_photos":"Kuvat / videot","btn_capture":"📷 Ota kuva / video","btn_media_upload":"📁 Tuo laitteeltasi","btn_back":"◀ Edellinen","preview_title":"Esikatselu ja käännös","preview_intro":"Tämä paneeli päivittyy heti, kun tallennat tai tuot ääntä.","preview_orig_label":"🗣️ Alkuperäinen teksti","preview_fr_label":"🇫🇷 Ranskankielinen käännös","helper_text":"Tarkista nopeasti: voit sitten viimeistellä ja lähettää ideasi."},
    "cs": {"panel_title":"Váš nápad","panel_intro":"Stačí několik prvků: cílem je pochopit váš kontext, vaši potřebu a očekávaný dopad.","label_type":"Typ příspěvku","check_difficulty":"Obtíž","check_improvement":"Zlepšení","check_innovation":"Inovace","label_title":"Název vašeho NÁPADU","placeholder_title":"Např. Fotoreforma","label_description":"Popis (volitelné při zvuku)","placeholder_description":"Popište svůj nápad, potřebu, pohled…","label_impact":"Jaký hlavní dopad by měl váš nápad?","impact_options":{"placeholder":"Vyberte hlavní dopad","ergonomie":"Pracovní podmínky / Ergonomie","environnement":"Udržitelný rozvoj / Životní prostředí","efficacite":"Úspora času / Efektivita","productivite":"Produktivita","energie":"Úspora energie","securite":"Bezpečnost","autre":"Jiné (upřesněte)"},"label_recording":"Hlasový záznam","btn_rec":"🎙️ Spustit nahrávání","btn_upload":"📁 Importovat zvuk","btn_tone":"🔊 Testovat zvuk","label_media":"Ilustrace (volitelné)","label_photos":"Fotografie / videa","btn_capture":"📷 Pořídit fotografii / video","btn_media_upload":"📁 Importovat ze zařízení","btn_back":"◀ Předchozí","preview_title":"Náhled a překlad","preview_intro":"Tento panel se aktualizuje, jakmile nahrajete nebo importujete zvuk.","preview_orig_label":"🗣️ Původní text","preview_fr_label":"🇫🇷 Překlad do francouzštiny","helper_text":"Rychle zkontrolujte: poté můžete svůj nápad dokončit a odeslat."},
    "hu": {"panel_title":"Az ötlete","panel_intro":"Néhány elem elegendő: a cél az Ön kontextusának, szükségletének és a várható hatásnak a megértése.","label_type":"Hozzájárulás típusa","check_difficulty":"Nehézség","check_improvement":"Fejlesztés","check_innovation":"Innováció","label_title":"ÖTLETE címe","placeholder_title":"Pl. Fotóreform","label_description":"Leírás (opcionális hangfelvétel esetén)","placeholder_description":"Írja le ötletét, szükségletét, meglátását…","label_impact":"Milyen fő hatása lenne az ötletének?","impact_options":{"placeholder":"Válassza ki a fő hatást","ergonomie":"Munkakörülmények / Ergonómia","environnement":"Fenntartható fejlődés / Környezet","efficacite":"Időmegtakarítás / Hatékonyság","productivite":"Termelékenység","energie":"Energiamegtakarítás","securite":"Biztonság","autre":"Egyéb (pontosítsa)"},"label_recording":"Hangfelvétel","btn_rec":"🎙️ Felvétel indítása","btn_upload":"📁 Hang importálása","btn_tone":"🔊 Hang tesztelése","label_media":"Illusztrációk (opcionális)","label_photos":"Fotók / videók","btn_capture":"📷 Fotó / videó készítése","btn_media_upload":"📁 Importálás eszközéről","btn_back":"◀ Előző","preview_title":"Előnézet és fordítás","preview_intro":"Ez a panel frissül, mihelyt felveszik vagy importálják a hangot.","preview_orig_label":"🗣️ Eredeti szöveg","preview_fr_label":"🇫🇷 Francia fordítás","helper_text":"Ellenőrizze gyorsan: ezután befejezheti és elküldheti ötletét."},
    "sk": {"panel_title":"Váš nápad","panel_intro":"Stačí niekoľko prvkov: cieľom je pochopiť váš kontext, vašu potrebu a očakávaný dopad.","label_type":"Typ príspevku","check_difficulty":"Ťažkosť","check_improvement":"Zlepšenie","check_innovation":"Inovácia","label_title":"Názov vášho NÁPADU","placeholder_title":"Napr. Fotoreforma","label_description":"Popis (voliteľné pri zvuku)","placeholder_description":"Opíšte svoj nápad, potrebu, pohľad…","label_impact":"Aký hlavný dopad by mal váš nápad?","impact_options":{"placeholder":"Vyberte hlavný dopad","ergonomie":"Pracovné podmienky / Ergonómia","environnement":"Udržateľný rozvoj / Životné prostredie","efficacite":"Úspora času / Efektivita","productivite":"Produktivita","energie":"Úspora energie","securite":"Bezpečnosť","autre":"Iné (upresni)"},"label_recording":"Hlasový záznam","btn_rec":"🎙️ Spustiť nahrávanie","btn_upload":"📁 Importovať zvuk","btn_tone":"🔊 Testovať zvuk","label_media":"Ilustrácie (voliteľné)","label_photos":"Fotografie / videá","btn_capture":"📷 Odfotiť / natočiť video","btn_media_upload":"📁 Importovať zo zariadenia","btn_back":"◀ Predchádzajúci","preview_title":"Náhľad a preklad","preview_intro":"Tento panel sa aktualizuje hneď, ako nahráte alebo importujete zvuk.","preview_orig_label":"🗣️ Pôvodný text","preview_fr_label":"🇫🇷 Preklad do francúzštiny","helper_text":"Rýchlo skontrolujte: potom môžete dokončiť a odeslať váš nápad."},
}

# Caches Gemini (langues rares, évite les appels répétés)
_CACHE_VOICE:   dict = {}
_CACHE_PROFILE: dict = {}
_CACHE_CONTACT: dict = {}
_CACHE_IDEA:    dict = {}

# ------------ /api/voice_lang ------------

@app.route("/api/voice_lang", methods=["POST"])
def voice_lang():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400

    language_field = (data.get("language") or "").strip()
    original_text  = (data.get("original_text") or "").strip()
    french_translation = (data.get("french_translation") or "").strip()

    # 1. Dictionnaire statique (instantané)
    lang_code = language_field.lower()[:2] if language_field else ""
    entry = _S_VOICE.get(lang_code)
    if not entry and language_field:
        lf = language_field.lower()
        for k, v in _S_VOICE.items():
            if v["fr_label"].lower() == lf or v["native_label"].lower() == lf:
                entry, lang_code = v, k
                break
    if entry:
        return jsonify({"ok": True, "code": lang_code,
                        "fr_label": entry["fr_label"],
                        "native_label": entry["native_label"],
                        "ui": entry["ui"]})

    # 2. Cache Gemini
    ck = lang_code or language_field.lower()
    if ck in _CACHE_VOICE:
        return jsonify({"ok": True, **_CACHE_VOICE[ck]})

    # 3. Fallback Gemini (langues rares), timeout 25s
    pb = ""
    if original_text:      pb += f'Texte : """{original_text}"""\n'
    if french_translation: pb += f'Trad FR : """{french_translation}"""\n'
    prompt = f"""Tu identifies la langue (language_field="{language_field}") {pb}
et traduis : title="Présente-toi à l'oral", intro="Dans cet enregistrement, indique simplement :",
items=["Ton nom.","Ton prénom.","Le site sur lequel tu travailles.","Ton service.","Ta fonction (poste occupé)."],
rec_label="🎙️ Démarrer l'enregistrement", upload_label="📁 Importer un audio",
notice="🔒 Ton audio est utilisé uniquement pour générer le texte ci-dessous."
Conserve les emojis. JSON UNIQUEMENT :
{{"code":"xx","fr_label":"…","native_label":"…","ui":{{"title":"…","intro":"…","items":["…","…","…","…","…"],"rec_label":"🎙️ …","upload_label":"📁 …","notice":"🔒 …"}}}}"""
    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt, request_options={"timeout": 25})
        p = force_json(getattr(resp, "text", "") or "{}")
        code = (p.get("code") or "").strip() or "und"
        ui = p.get("ui") or {}
        items = ui.get("items") or []
        if not isinstance(items, list): items = []
        result = {"code": code, "fr_label": (p.get("fr_label") or "").strip() or "langue inconnue",
                  "native_label": (p.get("native_label") or "").strip() or "?",
                  "ui": {"title": ui.get("title") or code, "intro": ui.get("intro") or "",
                         "items": items,
                         "rec_label": ui.get("rec_label") or "🎙️ Démarrer l'enregistrement",
                         "upload_label": ui.get("upload_label") or "📁 Importer un audio",
                         "notice": ui.get("notice") or ""}}
        _CACHE_VOICE[code] = result
        if ck != code: _CACHE_VOICE[ck] = result
        return jsonify({"ok": True, **result})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": f"Détection de langue vocale échouée : {e}"}), 500


# ------------ /api/analyze_profile ------------

@app.route("/api/analyze_profile", methods=["POST"])
def analyze_profile():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400

    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Texte vide."}), 400

    prompt = f"""
Tu es un assistant pour une plateforme interne appelée IDEA.

À partir du texte ci-dessous, tu dois :

1) Extraire les informations (sinon null) :
   - name
   - site
   - service
   - function_title

2) Construire "missing" = liste des champs null.

3) Construire "hints" = message d’aide en français pour chaque champ manquant.

Réponds STRICTEMENT :

{{
  "profile": {{
    "name": "... ou null",
    "site": "... ou null",
    "service": "... ou null",
    "function_title": "... ou null"
  }},
  "missing": ["name", "site", ...],
    "hints": {{
    "name": "message si manquant",
    "site": "...",
    "service": "...",
    "function_title": "..."
  }}
}}

Texte à analyser :
\"\"\"{text}\"\"\""""

    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt, request_options={"timeout": 90})

        raw = getattr(resp, "text", "") or "{}"
        parsed = force_json(raw)

        profile = parsed.get("profile") or {}
        profile_struct = {
            "name": profile.get("name"),
            "site": profile.get("site"),
            "service": profile.get("service"),
            "function_title": profile.get("function_title"),
        }

        missing = parsed.get("missing")
        if not isinstance(missing, list):
            missing = [k for k, v in profile_struct.items() if not v]

        hints = parsed.get("hints")
        if not isinstance(hints, dict):
            hints = {}

        default_hints = {
            "name": "Je n’ai pas bien compris ton nom, merci de le préciser ici.",
            "site": "Je n’ai pas bien compris ton site, merci de le sélectionner ou le préciser.",
            "service": "Je n’ai pas bien compris ton service, merci de le préciser.",
            "function_title": "Je n’ai pas bien compris ta fonction, merci de la préciser.",
        }

        clean_hints = {}
        for key in ["name", "site", "service", "function_title"]:
            if key in missing:
                msg = hints.get(key) or default_hints.get(key)
                clean_hints[key] = msg

        return jsonify(
            {
                "ok": True,
                "profile": profile_struct,
                "missing": missing,
                "hints": clean_hints,
            }
        )

    except Exception as e:
        return jsonify({"ok": False, "error": f"Analyse profil échouée : {e}"}), 500

@app.route("/api/profile_lang", methods=["POST"])
def profile_lang():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400
    lc = (data.get("language") or "").strip()
    if not lc or lc == "fr":
        return jsonify({"ok": True, "ui": {}})
    if lc in _S_PROFILE:
        return jsonify({"ok": True, "ui": _S_PROFILE[lc]})
    if lc in _CACHE_PROFILE:
        return jsonify({"ok": True, "ui": _CACHE_PROFILE[lc]})
    # Fallback Gemini
    prompt = f"""Traduis du français vers la langue ISO "{lc}" (tutoiement si possible, balises <b> conservées).
Textes : title_fr="On démarre par toi", intro_fr="Avant de commencer, indique simplement <b>qui tu es</b>, <b>où tu travailles</b> et <b>quel est ton rôle</b>.", label_name_fr="Nom et prénom", label_site_fr="Sur quel site travailles-tu ?", label_service_fr="Dans quel service travailles-tu ?", label_function_fr="Quelle est ta fonction ?", placeholder_name_fr="Ex : Marie Dupont", placeholder_site_fr="Sélectionne ton site", placeholder_service_fr="Sélectionne ton service", placeholder_function_fr="Ex : Technicien de maintenance, Responsable magasin…", placeholder_other_site_fr="Indique ton site", placeholder_other_service_fr="Précise ton service"
JSON UNIQUEMENT : {{"title":"…","intro":"…","label_name":"…","label_site":"…","label_service":"…","label_function":"…","placeholder_name":"…","placeholder_site":"…","placeholder_service":"…","placeholder_function":"…","placeholder_other_site":"…","placeholder_other_service":"…"}}"""
    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt, request_options={"timeout": 25})
        parsed = force_json(getattr(resp, "text", "") or "{}")
        _CACHE_PROFILE[lc] = parsed
        return jsonify({"ok": True, "ui": parsed})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Traduction profil échouée : {e}"}), 500


@app.route("/api/contact_lang", methods=["POST"])
def contact_lang():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400
    lc = (data.get("language") or "").strip()
    if not lc or lc == "fr":
        return jsonify({"ok": True, "ui": {}})
    if lc in _S_CONTACT:
        return jsonify({"ok": True, "ui": _S_CONTACT[lc]})
    if lc in _CACHE_CONTACT:
        return jsonify({"ok": True, "ui": _CACHE_CONTACT[lc]})
    # Fallback Gemini
    prompt = f"""Traduis du français vers la langue ISO "{lc}" (tutoiement si possible).
Textes : section_coords_fr="Coordonnées", section_pref_fr="Préférence de contact", email_title_fr="Adresse mail professionnelle", email_label_fr="Si tu as une adresse mail professionnelle, note-la ci-dessous", email_placeholder_fr="Ex : prenom.nom@entreprise.com", email_note_fr="Ce champ est facultatif, mais il facilite le suivi de ton idée.", pref_title_fr="Comment souhaites-tu être recontacté(e) ?", radio_mail_fr="Mail professionnel", radio_manager_fr="Par l'intermédiaire de mon responsable"
JSON UNIQUEMENT : {{"section_coords":"…","section_pref":"…","email_title":"…","email_label":"…","email_placeholder":"…","email_note":"…","pref_title":"…","radio_mail":"…","radio_manager":"…"}}"""
    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt, request_options={"timeout": 25})
        parsed = force_json(getattr(resp, "text", "") or "{}")
        _CACHE_CONTACT[lc] = parsed
        return jsonify({"ok": True, "ui": parsed})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Traduction contact échouée : {e}"}), 500


@app.route("/api/idea_lang", methods=["POST"])
def idea_lang():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400
    lc = (data.get("language") or "").strip()
    if not lc or lc == "fr":
        return jsonify({"ok": True, "ui": {}})
    if lc in _S_IDEA:
        return jsonify({"ok": True, "ui": _S_IDEA[lc]})
    if lc in _CACHE_IDEA:
        return jsonify({"ok": True, "ui": _CACHE_IDEA[lc]})
    # Fallback Gemini
    prompt = f"""Traduis du français vers la langue ISO "{lc}" (tutoiement si possible, conserve les emojis).
Textes FR : panel_title="Contenu de ton idée", panel_intro="Quelques éléments suffisent : l'objectif est de comprendre ton contexte, ton besoin et l'impact attendu.", label_type="Type de contribution", check_difficulty="Une difficulté", check_improvement="Une amélioration", check_innovation="Une innovation", label_title="Titre de ton IDEA", placeholder_title="Ex : Photo réforme", label_description="Description (optionnel si audio)", placeholder_description="Décris ton idée, ton besoin, ton insight…", label_impact="Quel impact principal aurait ton idée ?", impact_placeholder="Sélectionne l'impact principal", impact_ergonomie="Condition de travail / Ergonomie", impact_environnement="Développement durable / Environnement", impact_efficacite="Gain de temps / Efficacité", impact_productivite="Productivité", impact_energie="Économie d'énergie", impact_securite="Sécurité", impact_autre="Autre (préciser)", label_recording="Enregistrement vocal", btn_rec="🎙️ Démarrer l'enregistrement", btn_upload="📁 Importer un audio", btn_tone="🔊 Tester le son", label_media="Illustrations (facultatif)", label_photos="Photos / vidéos", btn_capture="📷 Prendre une photo / vidéo", btn_media_upload="📁 Importer depuis ton appareil", btn_back="◀ Précédent", preview_title="Aperçu & traduction", preview_intro="Ce panneau se mettra à jour dès que tu enregistres ou importes un audio.", preview_orig_label="🗣️ Texte d'origine", preview_fr_label="🇫🇷 Traduction française", helper_text="Vérifie rapidement : tu pourras ensuite finaliser et envoyer ton idée."
JSON UNIQUEMENT : {{"panel_title":"…","panel_intro":"…","label_type":"…","check_difficulty":"…","check_improvement":"…","check_innovation":"…","label_title":"…","placeholder_title":"…","label_description":"…","placeholder_description":"…","label_impact":"…","impact_options":{{"placeholder":"…","ergonomie":"…","environnement":"…","efficacite":"…","productivite":"…","energie":"…","securite":"…","autre":"…"}},"label_recording":"…","btn_rec":"🎙️ …","btn_upload":"📁 …","btn_tone":"🔊 …","label_media":"…","label_photos":"…","btn_capture":"📷 …","btn_media_upload":"📁 …","btn_back":"◀ …","preview_title":"…","preview_intro":"…","preview_orig_label":"🗣️ …","preview_fr_label":"🇫🇷 …","helper_text":"…"}}"""
    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt, request_options={"timeout": 25})
        parsed = force_json(getattr(resp, "text", "") or "{}")
        _CACHE_IDEA[lc] = parsed
        return jsonify({"ok": True, "ui": parsed})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Traduction idea échouée : {e}"}), 500


# ------------ Submit final ------------

@app.route("/api/submit", methods=["POST"])
def submit():
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400

    author_name = payload.get("author_name") or None
    site = payload.get("site") or None
    service = payload.get("service") or None
    function_title = payload.get("function_title") or None
    professional_email = payload.get("professional_email") or None
    contact_mode = payload.get("contact_mode") or None

    typed_text = payload.get("typed_text") or None
    audio_path = payload.get("audio_path") or None
    detected_language = payload.get("detected_language") or None
    original_text = payload.get("original_text") or None
    french_translation = payload.get("french_translation") or None

    idea_title = payload.get("idea_title") or None
    share_types = payload.get("share_types") or []
    impact_main = payload.get("impact_main") or None
    impact_other = payload.get("impact_other") or None
    media_paths = payload.get("media_paths") or []

    source = payload.get("source") or "web_form"

    share_types_json = json.dumps(share_types, ensure_ascii=False)
    media_paths_json = json.dumps(media_paths, ensure_ascii=False)

    idea_id = uuid.uuid4().hex
    created_dt = datetime.now(timezone.utc)
    created_at = created_dt.isoformat(timespec="seconds")

    # Enregistrement + génération du code dans la même connexion
    with sqlite3.connect(DB_PATH) as con:
        idea_code = generate_idea_code(con, created_dt)

        con.execute(
            """
            INSERT INTO ideas (
                id,
                created_at,
                idea_code,
                author_name,
                country,
                category,
                typed_text,
                audio_path,
                detected_language,
                original_text,
                french_translation,
                site,
                service,
                function_title,
                professional_email,
                contact_mode,
                idea_title,
                share_types,
                impact_main,
                impact_other,
                source,
                media_paths
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idea_id,
                created_at,
                idea_code,
                author_name,
                None,  # country
                None,  # category
                typed_text,
                audio_path,
                detected_language,
                original_text,
                french_translation,
                site,
                service,
                function_title,
                professional_email,
                contact_mode,
                idea_title,
                share_types_json,
                impact_main,
                impact_other,
                source,
                media_paths_json,
            ),
        )
        con.commit()

    # Génère les labels fonctionnels des médias (IDEAxxxx_IMG_1, IDEAxxxx_VID_1, ...)
    media_labels = build_media_labels(idea_code, media_paths)

    # Upload des médias sur Google Drive dans le même dossier que le Google Sheet
    # puis suppression des fichiers locaux
    drive_links: list[str] = []

    for p, media_label in zip(media_paths, media_labels):
        try:
            # p est de type "/uploads/xxxx-xxx.png"
            rel = p.lstrip("/")  # "uploads/xxxx-xxx.png"
            local_path = Path(rel)
            if not local_path.exists():
                # fallback : on essaie via UPLOAD_DIR
                local_path = UPLOAD_DIR / Path(p).name

            if local_path.exists():
                ext = Path(p).suffix.lower()  # ".png", ".jpg", ".mp4", etc.
                drive_name = f"{media_label}{ext}" if ext else media_label

                _, link = upload_file_to_drive(local_path, original_name=drive_name)
                if link:
                    drive_links.append(link)
                    # suppression locale après upload réussi
                    try:
                        os.remove(local_path.as_posix())
                    except Exception as e_rm:
                        print(f"[WARN] Impossible de supprimer le fichier local {local_path} : {e_rm}")
                else:
                    drive_links.append("")
            else:
                print(f"[WARN] Fichier local introuvable pour upload Drive : {p}")
                drive_links.append("")
        except Exception as e:
            print(f"[WARN] Erreur lors du traitement du média {p} : {e}")
            drive_links.append("")

    # Les liens utilisés dans l'email et dans Google Sheets sont les liens Drive
    abs_media_paths = drive_links

    email_data = {
        "idea_code": idea_code,
        "author_name": author_name,
        "site": site,
        "service": service,
        "function_title": function_title,
        "professional_email": professional_email,
        "contact_mode": contact_mode,
        "idea_title": idea_title,
        "share_types": share_types,
        "impact_main": impact_main,
        "impact_other": impact_other,
        "typed_text": typed_text,
        "detected_language": detected_language,
        "original_text": original_text,
        "french_translation": french_translation,
        "media_paths": abs_media_paths,
        "_id": idea_id,
        "_created_at": created_at,
    }

    # Pousser dans Google Sheets : une ligne par idée
    try:
        row = [
            idea_code,                       # A - Code idée
            created_at,                      # B - Date/heure (UTC)
            author_name or "",               # C - Nom & Prénom
            site or "",                      # D - Site
            service or "",                   # E - Service
            function_title or "",            # F - Fonction
            professional_email or "",        # G - E-mail professionnel
            contact_mode or "",              # H - Préférence de contact
            idea_title or "",                # I - Titre
            ", ".join(share_types) if share_types else "",  # J - Type(s)
            impact_main or "",               # K - Impact principal
            impact_other or "",              # L - Impact précisé
            typed_text or "",                # M - Description (texte saisi)
            detected_language or "",         # N - Langue détectée
            original_text or "",             # O - Texte d'origine
            french_translation or "",        # P - Traduction française
            "; ".join(abs_media_paths),      # Q - URLs médias (Drive)
            idea_id,                         # R - ID interne
            "; ".join(media_labels),         # S - Codes médias (IMG_x / VID_x)
        ]
        append_idea_to_sheet(row)
    except Exception as e:
        print(f"[WARN] Impossible d’écrire dans Google Sheets : {e}")

    # Envoi de l'e-mail avec URLs Drive cliquables (équipe IDEA)
    try:
        subject = f"Nouvelle IDEA {idea_code} – « {idea_title or 'Sans titre'} » – {author_name or 'Auteur inconnu'}"
        body = format_email_from_idea(email_data)
        send_email_to_idea_team(subject, body)
    except Exception as e:
        print(f"[WARN] Erreur d'envoi d'e-mail IDEA : {e}")

    # Envoi de l'e-mail de confirmation à l'utilisateur (si e-mail fourni)
    try:
        if professional_email:
            send_email_confirmation_to_user(professional_email, email_data)
    except Exception as e:
        print(f"[WARN] Erreur d'envoi d'e-mail de confirmation utilisateur : {e}")

    return jsonify(
        {
            "ok": True,
            "id": idea_id,
            "created_at": created_at,
            "idea_code": idea_code,
        }
    )


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)