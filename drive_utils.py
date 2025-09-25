import os
from typing import Dict, List, Optional

import google_auth_oauthlib.flow
import googleapiclient.discovery
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from fastapi.responses import RedirectResponse

# -------------------------------
# Global store (replace with DB if multi-user)
# -------------------------------
USER_TOKENS: Dict[str, object] = {}
DRIVE_FOLDER_ID: Optional[str] = None
DRIVE_STRUCTURE: Dict[str, str] = {}  # will hold all folder IDs

# -------------------------------
# Load env vars (MUST exist in Render & local .env)
# -------------------------------
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", "wineocr-project")
REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://fastapi-backend-4wqb.onrender.com/drive-callback"
)

# Add frontend URL for redirecting after OAuth
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://david-f-frontend.vercel.app")

if not CLIENT_ID or not CLIENT_SECRET:
    raise ValueError("❌ Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET in environment variables")

CLIENT_CONFIG = {
    "web": {
        "client_id": CLIENT_ID,
        "project_id": PROJECT_ID,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": CLIENT_SECRET,
        "redirect_uris": "https://david-f-frontend.vercel.app",
    }
}

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile"
]

# -------------------------------
# Helpers
# -------------------------------
def _ensure_folder(service, name: str, parent: Optional[str] = None) -> str:
    """Ensure folder exists; create if not. Returns folder ID."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent:
        query += f" and '{parent}' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get("files", [])

    if items:
        return items[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent:
        metadata["parents"] = [parent]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def _get_or_create_structure(service) -> Dict[str, str]:
    """Rebuild Drive folder structure (auto-heal for empty DRIVE_STRUCTURE)."""
    root_id = _ensure_folder(service, "WineOCR_Processed")

    input_id = _ensure_folder(service, "Input", root_id)
    output_id = _ensure_folder(service, "Output", root_id)
    upload_id = _ensure_folder(service, "Upload", root_id)
    nhr_id = _ensure_folder(service, "NHR", root_id)

    nhr_structure = {
        "search_failed": _ensure_folder(service, "search_failed", nhr_id),
        "ocr_failed": _ensure_folder(service, "ocr_failed", nhr_id),
        "manual_rejection": _ensure_folder(service, "manual_rejection", nhr_id),
        "others": _ensure_folder(service, "others", nhr_id),
    }

    return {
        "root": root_id,
        "input": input_id,
        "output": output_id,
        "upload": upload_id,
        "nhr": {"root": nhr_id, **nhr_structure}
    }


# -------------------------------
# Drive Init (Step 1)
# -------------------------------
def init_drive() -> Dict[str, str]:
    """Generate Google OAuth consent URL for Drive sign-in."""
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES
    )
    flow.redirect_uri = REDIRECT_URI

    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return {"url": auth_url, "state": state}


# -------------------------------
# Drive Callback (Step 2)
# -------------------------------
def oauth_callback(code: str, state: str):
    """Exchange code for tokens and create necessary folders, then redirect."""
    global DRIVE_FOLDER_ID, DRIVE_STRUCTURE

    try:
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            CLIENT_CONFIG, scopes=SCOPES, state=state
        )
        flow.redirect_uri = REDIRECT_URI
        flow.fetch_token(code=code)

        credentials = flow.credentials
        USER_TOKENS["default"] = credentials

        service = build("drive", "v3", credentials=credentials)

        # Create folders
        DRIVE_STRUCTURE = _get_or_create_structure(service)
        DRIVE_FOLDER_ID = DRIVE_STRUCTURE["root"]

        # ✅ Redirect back to frontend with success
        return RedirectResponse(url=f"{FRONTEND_URL}?drive_connected=success")

    except Exception as e:
        return RedirectResponse(url=f"{FRONTEND_URL}?drive_error={str(e)}")


# -------------------------------
# Drive Status
# -------------------------------
def is_drive_ready() -> Dict[str, object]:
    """Check if Drive is linked and folder is ready."""
    return {
        "linked": "default" in USER_TOKENS
    }


# -------------------------------
# Upload to Drive (Step 3)
# -------------------------------
def upload_to_drive(
    local_dir: str = "processed",
    rename_map: Dict[str, str] = None,
    target: str = "output"  # e.g. "input", "output", "upload", "nhr.search_failed"
) -> Dict[str, List[Dict[str, str]]]:
    """Upload all files in local_dir to the correct Drive subfolder and delete them locally after upload."""
    creds = USER_TOKENS.get("default")
    if not creds:
        return {"error": "Drive not initialized. Call /init-drive first."}

    service = build("drive", "v3", credentials=creds)

    # ✅ Auto-heal if DRIVE_STRUCTURE is empty
    global DRIVE_STRUCTURE
    if not DRIVE_STRUCTURE:
        DRIVE_STRUCTURE = _get_or_create_structure(service)

    # Resolve folder ID
    folder_id = None
    if target.startswith("nhr."):
        _, sub = target.split(".", 1)
        folder_id = DRIVE_STRUCTURE["nhr"].get(sub)
    else:
        folder_id = DRIVE_STRUCTURE.get(target)

    if not folder_id:
        return {"error": f"Invalid target folder: {target}"}

    uploaded = []
    if os.path.exists(local_dir):
        for file in os.listdir(local_dir):
            file_path = os.path.join(local_dir, file)
            if not os.path.isfile(file_path):
                continue

            new_name = rename_map.get(file, file) if rename_map else file

            media = MediaFileUpload(file_path, mimetype="image/jpeg")
            drive_file = service.files().create(
                body={"name": new_name, "parents": [folder_id]},
                media_body=media,
                fields="id, name, webViewLink"
            ).execute()

            uploaded.append(drive_file)

            # ✅ Delete file locally after successful upload
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Warning: Could not delete {file_path} -> {e}")

    return {
        "message": f"Uploaded {len(uploaded)} files to {target} and cleaned up local copies",
        "files": uploaded,
    }
