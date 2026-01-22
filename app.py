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


def get_sheets_service():
    """Initialise le client Google Sheets à partir du compte de service."""
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
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


# ------------ Google Drive helpers (OAuth utilisateur) ------------

def get_drive_service():
    """
    Client Google Drive basé sur TON compte Google (OAuth utilisateur),
    en utilisant credentials_drive.json + token_drive.json.
    """
    creds = None
    token_path = Path("token_drive.json")

    # 1) On tente de recharger un token existant
    if token_path.exists():
        creds = UserCredentials.from_authorized_user_file(
            token_path.as_posix(), SCOPES
        )

    # 2) Si pas de creds ou invalides → flow OAuth
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Première autorisation : ouvre un navigateur pour te connecter à ton compte Google
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials_drive.json", SCOPES
            )
            creds = flow.run_local_server(port=0)

        # 3) On sauvegarde le token pour les prochaines fois
        with open(token_path, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    # 4) Construction du service Drive
    service = build("drive", "v3", credentials=creds)
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
                ]
            )
        except Exception as e_inline:
            try:
                uploaded = genai.upload_file(save_path.as_posix(), mime_type=mime)
                resp = model.generate_content(
                    [
                        {"text": system_prompt},
                        uploaded,
                    ]
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


# ------------ /api/voice_lang ------------

@app.route("/api/voice_lang", methods=["POST"])
def voice_lang():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400

    language_field = (data.get("language") or "").strip()
    original_text = (data.get("original_text") or "").strip()
    french_translation = (data.get("french_translation") or "").strip()

    phrase_block = ""
    if original_text:
        phrase_block += f'Texte reconnu (langue d\'origine) : """{original_text}"""\\n'
    if french_translation:
        phrase_block += f'Traduction française : """{french_translation}"""\\n'

    phrase_comment = ""
    if not phrase_block:
        phrase_comment = (
            "Aucun exemple de phrase n'est fourni, base-toi uniquement sur language_field."
        )

    prompt = f"""
Tu es un assistant pour une plateforme interne appelée IDEA.

On te donne :
- un champ "language_field" (code ou nom de langue),
- éventuellement une phrase où la personne dit dans quelle langue elle veut parler.

language_field = "{language_field}"
{phrase_block}

{phrase_comment}

OBJECTIF 1 — Identification de la langue
----------------------------------------
1. Identifie la langue voulue :
   - "code" : code ISO 639-1 (2 lettres) si possible, sinon "und".
   - "fr_label" : nom de la langue en français.
   - "native_label" : nom de la langue dans cette langue elle-même.

Par exemple :
- Si la personne dit "I want to speak in English" -> code "en", fr_label "Anglais", native_label "English".
- Si la personne dit "Prefiero hablar en español" -> code "es", fr_label "Espagnol", native_label "Español".


OBJECTIF 2 — TRADUIRE un bloc français existant
-----------------------------------------------
Tu dois PARTIR des textes français suivants, qui décrivent l'étape
« Présente-toi à l’oral », et en donner l'équivalent dans la langue détectée,
sans changer le sens ni la structure.

Bloc français de référence :

- title_fr  : "Présente-toi à l’oral"
- intro_fr  : "Dans cet enregistrement, indique simplement :"
- items_fr  :
  1. "Ton nom et prénom."
  2. "Le site sur lequel tu travailles."
  3. "Ton service."
  4. "Ta fonction (poste occupé)."
- rec_label_fr    : "🎙️ Démarrer l’enregistrement"
- upload_label_fr : "📁 Importer un audio"
- notice_fr       : "🔒 Ton audio est utilisé uniquement pour générer le texte ci-dessous.
                     Il n’est ni conservé, ni réécouté par une autre personne."

Consignes très importantes :
- Tu NE dois PAS inventer d'autres textes (pas de message du type
  "Welcome to the IDEA platform" ou "Thank you for using this app").
- Tu DOIS fournir une traduction FIDÈLE de ces phrases dans la langue détectée.
- Tu conserves les emojis (🎙️, 📁, 🔒) au début des textes et tu traduis le reste.
- Le style doit rester simple, clair et poli.


FORMAT DE RÉPONSE
-----------------
Tu réponds STRICTEMENT avec CE JSON unique :

{{
  "code": "xx",
  "fr_label": "nom de la langue en français",
  "native_label": "nom de la langue dans cette langue",
  "ui": {{
    "title": "traduction de title_fr dans la langue détectée",
    "intro": "traduction de intro_fr",
    "items": [
      "traduction de l’item 1",
      "traduction de l’item 2",
      "traduction de l’item 3",
      "traduction de l’item 4"
    ],
    "rec_label": "traduction de rec_label_fr, emoji conservé",
    "upload_label": "traduction de upload_label_fr, emoji conservé",
    "notice": "traduction de notice_fr, emoji conservé"
  }}
}}

Aucun texte en dehors de ce JSON.
"""

    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt)
        raw = getattr(resp, "text", "") or "{}"
        parsed = force_json(raw)

        code = (parsed.get("code") or "").strip() or "und"
        fr_label = (parsed.get("fr_label") or "").strip() or "langue inconnue"
        native_label = (parsed.get("native_label") or "").strip() or fr_label
        ui = parsed.get("ui") or {}

        items = ui.get("items") or []
        if not isinstance(items, list):
            items = []

        return jsonify(
            {
                "ok": True,
                "code": code,
                "fr_label": fr_label,
                "native_label": native_label,
                "ui": {
                    "title": ui.get("title") or fr_label,
                    "intro": ui.get("intro") or "",
                    "items": items,
                    "rec_label": ui.get("rec_label") or "🎙️ Démarrer l’enregistrement",
                    "upload_label": ui.get("upload_label") or "📁 Importer un audio",
                    "notice": ui.get("notice") or "",
                },
            }
        )

    except Exception as e:
        return jsonify(
            {"ok": False, "error": f"Détection de langue vocale échouée : {e}"}
        ), 500


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
        resp = model.generate_content(prompt)

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
    """
    Traduit les labels du formulaire de profil dans la langue demandée.
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400

    language_code = (data.get("language") or "").strip()
    if not language_code or language_code == "fr":
        return jsonify({"ok": True, "ui": {}})

    prompt = f"""
Tu es un assistant de traduction pour une plateforme interne appelée IDEA.

Tu dois traduire les textes suivants du français vers la langue avec le code ISO "{language_code}".

Textes à traduire :

- title_fr: "On démarre par toi"
- intro_fr: "Avant de commencer, indique simplement <b>qui tu es</b>, <b>où tu travailles</b> et <b>quel est ton rôle</b>."
- label_name_fr: "Nom et prénom"
- label_site_fr: "Sur quel site travailles-tu ?"
- label_service_fr: "Dans quel service travailles-tu ?"
- label_function_fr: "Quelle est ta fonction ?"
- placeholder_name_fr: "Ex : Marie Dupont"
- placeholder_site_fr: "Sélectionne ton site"
- placeholder_service_fr: "Sélectionne ton service"
- placeholder_function_fr: "Ex : Technicien de maintenance, Responsable magasin…"
- placeholder_other_site_fr: "Indique ton site"
- placeholder_other_service_fr: "Précise ton service"

Consignes :
- Fournis une traduction FIDÈLE dans la langue cible.
- Conserve les balises HTML (<b>).
- Le style doit rester simple, clair et poli.

Réponds STRICTEMENT avec ce JSON :

{{
  "title": "traduction de title_fr",
  "intro": "traduction de intro_fr (avec les balises <b>)",
  "label_name": "traduction de label_name_fr",
  "label_site": "traduction de label_site_fr",
  "label_service": "traduction de label_service_fr",
  "label_function": "traduction de label_function_fr",
  "placeholder_name": "traduction de placeholder_name_fr",
  "placeholder_site": "traduction de placeholder_site_fr",
  "placeholder_service": "traduction de placeholder_service_fr",
  "placeholder_function": "traduction de placeholder_function_fr",
  "placeholder_other_site": "traduction de placeholder_other_site_fr",
  "placeholder_other_service": "traduction de placeholder_other_service_fr"
}}

Aucun texte en dehors de ce JSON.
"""

    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt)
        raw = getattr(resp, "text", "") or "{}"
        parsed = force_json(raw)

        return jsonify({"ok": True, "ui": parsed})

    except Exception as e:
        return jsonify({"ok": False, "error": f"Traduction profil échouée : {e}"}), 500



@app.route("/api/contact_lang", methods=["POST"])
def contact_lang():
    """
    Traduit les labels du formulaire de coordonnées dans la langue demandée.
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400

    language_code = (data.get("language") or "").strip()
    if not language_code or language_code == "fr":
        return jsonify({"ok": True, "ui": {}})

    prompt = f"""
Tu es un assistant de traduction pour une plateforme interne appelée IDEA.

Tu dois traduire les textes suivants du français vers la langue avec le code ISO "{language_code}".

Textes à traduire :

- section_coords_fr: "Coordonnées"
- section_pref_fr: "Préférence de contact"
- email_title_fr: "Adresse mail professionnelle"
- email_label_fr: "Si tu as une adresse mail professionnelle, note-la ci-dessous"
- email_placeholder_fr: "Ex : prenom.nom@entreprise.com"
- email_note_fr: "Ce champ est facultatif, mais il facilite le suivi de ton idée."
- pref_title_fr: "Comment souhaites-tu être recontacté(e) ?"
- radio_mail_fr: "Mail professionnel"
- radio_manager_fr: "Par l'intermédiaire de mon responsable"

Consignes :
- Fournis une traduction FIDÈLE dans la langue cible.
- Le style doit rester simple, clair et poli.
- Utilise le tutoiement si la langue le permet.

Réponds STRICTEMENT avec ce JSON :

{{
  "section_coords": "traduction de section_coords_fr",
  "section_pref": "traduction de section_pref_fr",
  "email_title": "traduction de email_title_fr",
  "email_label": "traduction de email_label_fr",
  "email_placeholder": "traduction de email_placeholder_fr",
  "email_note": "traduction de email_note_fr",
  "pref_title": "traduction de pref_title_fr",
  "radio_mail": "traduction de radio_mail_fr",
  "radio_manager": "traduction de radio_manager_fr"
}}

Aucun texte en dehors de ce JSON.
"""

    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt)
        raw = getattr(resp, "text", "") or "{}"
        parsed = force_json(raw)

        return jsonify({"ok": True, "ui": parsed})

    except Exception as e:
        return jsonify({"ok": False, "error": f"Traduction contact échouée : {e}"}), 500


@app.route("/api/idea_lang", methods=["POST"])
def idea_lang():
    """
    Traduit les labels du formulaire d'idée dans la langue demandée.
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON invalide."}), 400

    language_code = (data.get("language") or "").strip()
    if not language_code or language_code == "fr":
        return jsonify({"ok": True, "ui": {}})

    prompt = f"""
Tu es un assistant de traduction pour une plateforme interne appelée IDEA.

Tu dois traduire les textes suivants du français vers la langue avec le code ISO "{language_code}".

Textes à traduire :

SECTION PRINCIPALE :
- panel_title_fr: "Contenu de ton idée"
- panel_intro_fr: "Quelques éléments suffisent : l'objectif est de comprendre ton contexte, ton besoin et l'impact attendu."

TYPE DE CONTRIBUTION :
- label_type_fr: "Type de contribution"
- check_difficulty_fr: "Une difficulté"
- check_improvement_fr: "Une amélioration"
- check_innovation_fr: "Une innovation"

TITRE ET DESCRIPTION :
- label_title_fr: "Titre de ton IDEA"
- placeholder_title_fr: "Ex : Photo réforme"
- label_description_fr: "Description (optionnel si audio)"
- placeholder_description_fr: "Décris ton idée, ton besoin, ton insight…"

IMPACT :
- label_impact_fr: "Quel impact principal aurait ton idée ?"
- impact_placeholder_fr: "Sélectionne l'impact principal"
- impact_ergonomie_fr: "Condition de travail / Ergonomie"
- impact_environnement_fr: "Développement durable / Environnement"
- impact_efficacite_fr: "Gain de temps / Efficacité"
- impact_productivite_fr: "Productivité"
- impact_energie_fr: "Économie d'énergie"
- impact_securite_fr: "Sécurité"
- impact_autre_fr: "Autre (préciser)"

ENREGISTREMENT :
- label_recording_fr: "Enregistrement vocal"
- btn_rec_fr: "🎙️ Démarrer l'enregistrement"
- btn_upload_fr: "📁 Importer un audio"
- btn_tone_fr: "🔊 Tester le son"

MÉDIAS :
- label_media_fr: "Illustrations (facultatif)"
- label_photos_fr: "Photos / vidéos"
- btn_capture_fr: "📷 Prendre une photo / vidéo"
- btn_media_upload_fr: "📁 Importer depuis ton appareil"

NAVIGATION :
- btn_back_fr: "◀ Précédent"

APERÇU :
- preview_title_fr: "Aperçu & traduction"
- preview_intro_fr: "Ce panneau se mettra à jour dès que tu enregistres ou importes un audio. Tu peux vérifier le texte compris avant d'envoyer ton IDEA."
- preview_orig_label_fr: "🗣️ Texte d'origine"
- preview_fr_label_fr: "🇫🇷 Traduction française"
- helper_text_fr: "Vérifie rapidement : tu pourras ensuite finaliser et envoyer ton idée. En cas d'erreur, tu pourras corriger le texte ou refaire un enregistrement."

Consignes :
- Fournis une traduction FIDÈLE dans la langue cible.
- Conserve les emojis (🎙️, 📁, 🔊, 📷, ◀, 🗣️, 🇫🇷).
- Le style doit rester simple, clair et poli.
- Utilise le tutoiement si la langue le permet.

Réponds STRICTEMENT avec ce JSON :

{{
  "panel_title": "traduction",
  "panel_intro": "traduction",
  "label_type": "traduction",
  "check_difficulty": "traduction",
  "check_improvement": "traduction",
  "check_innovation": "traduction",
  "label_title": "traduction",
  "placeholder_title": "traduction",
  "label_description": "traduction",
  "placeholder_description": "traduction",
  "label_impact": "traduction",
  "impact_options": {{
    "placeholder": "traduction",
    "ergonomie": "traduction",
    "environnement": "traduction",
    "efficacite": "traduction",
    "productivite": "traduction",
    "energie": "traduction",
    "securite": "traduction",
    "autre": "traduction"
  }},
  "label_recording": "traduction",
  "btn_rec": "traduction avec emoji",
  "btn_upload": "traduction avec emoji",
  "btn_tone": "traduction avec emoji",
  "label_media": "traduction",
  "label_photos": "traduction",
  "btn_capture": "traduction avec emoji",
  "btn_media_upload": "traduction avec emoji",
  "btn_back": "traduction avec emoji",
  "preview_title": "traduction",
  "preview_intro": "traduction",
  "preview_orig_label": "traduction avec emoji",
  "preview_fr_label": "traduction avec emoji",
  "helper_text": "traduction"
}}

Aucun texte en dehors de ce JSON.
"""

    try:
        model = genai.GenerativeModel(MODEL_ID)
        resp = model.generate_content(prompt)
        raw = getattr(resp, "text", "") or "{}"
        parsed = force_json(raw)

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