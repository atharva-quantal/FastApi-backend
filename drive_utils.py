import os
import pickle
from typing import Dict, List

import google_auth_oauthlib.flow
import googleapiclient.discovery
from googleapiclient.http import MediaFileUpload
from fastapi.responses import RedirectResponse

# ============================================================
# Globals
# ============================================================
USER_TOKENS: Dict[str, object] = {}
DRIVE_STRUCTURE: Dict[str, object] = {}
PERSIST_FILE = "drive_state.pkl"

# Google OAuth setup
CLIENT_CONFIG = {
    "web": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "project_id": "wine-ocr",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "redirect_uris": [os.getenv("REDIRECT_URI")],
    }
}
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
REDIRECT_URI = os.getenv("REDIRECT_URI")
FRONTEND_URL = os.getenv("FRONTEND_URL")


# ============================================================
# Persistence Helpers
# ============================================================
def save_drive_state():
    """Save credentials + drive structure into pickle."""
    try:
        with open(PERSIST_FILE, "wb") as f:
            pickle.dump(
                {"user_tokens": USER_TOKENS, "drive_structure": DRIVE_STRUCTURE}, f
            )
        print("💾 Drive state saved to pickle")
    except Exception as e:
        print(f"⚠️ Failed to save drive state: {e}")


def load_drive_state():
    """Load credentials + drive structure if available."""
    global USER_TOKENS, DRIVE_STRUCTURE
    if os.path.exists(PERSIST_FILE):
        try:
            with open(PERSIST_FILE, "rb") as f:
                state = pickle.load(f)
                USER_TOKENS = state.get("user_tokens", {})
                DRIVE_STRUCTURE = state.get("drive_structure", {})
                print("✅ Drive state restored from pickle")
        except Exception as e:
            print(f"⚠️ Failed to load drive state: {e}")
    else:
        print("ℹ️ No saved drive state found")


# ============================================================
# Folder Utilities
# ============================================================
def _ensure_folder(service, name: str, parent: str = None) -> str:
    """Create or retrieve a Google Drive folder."""
    query = f"mimeType='application/vnd.google-apps.folder' and name='{name}'"
    if parent:
        query += f" and '{parent}' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    file_metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        file_metadata["parents"] = [parent]

    folder = service.files().create(body=file_metadata, fields="id").execute()
    return folder["id"]


# ============================================================
# OAuth Callback
# ============================================================
def oauth_callback(code: str, state: str):
    """Exchange code for tokens and create necessary folders."""
    global DRIVE_STRUCTURE

    try:
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            CLIENT_CONFIG, scopes=SCOPES, state=state
        )
        flow.redirect_uri = REDIRECT_URI
        flow.fetch_token(code=code)

        credentials = flow.credentials
        USER_TOKENS["default"] = credentials

        service = googleapiclient.discovery.build("drive", "v3", credentials=credentials)

        # Main folder
        root_id = _ensure_folder(service, "WineOCR_Processed")

        # Subfolders
        input_id = _ensure_folder(service, "Input", root_id)
        output_id = _ensure_folder(service, "Output", root_id)
        upload_id = _ensure_folder(service, "Upload", root_id)
        nhr_id = _ensure_folder(service, "NHR", root_id)

        # NHR subfolders
        nhr_structure = {
            "search_failed": _ensure_folder(service, "search_failed", nhr_id),
            "ocr_failed": _ensure_folder(service, "ocr_failed", nhr_id),
            "manual_rejection": _ensure_folder(service, "manual_rejection", nhr_id),
            "others": _ensure_folder(service, "others", nhr_id),
        }

        DRIVE_STRUCTURE = {
            "root": root_id,
            "input": input_id,
            "output": output_id,
            "upload": upload_id,
            "nhr": {"root": nhr_id, **nhr_structure},
        }

        # 💾 Save everything
        save_drive_state()

        # Redirect user to frontend
        return RedirectResponse(f"{FRONTEND_URL}?drive_connected=success")

    except Exception as e:
        return RedirectResponse(f"{FRONTEND_URL}?drive_error={str(e)}")


# ============================================================
# Upload Files
# ============================================================
def upload_to_drive(
    local_dir: str = "processed",
    rename_map: Dict[str, str] = None,
    target: str = "output",  # e.g. "input", "output", "upload", "nhr.search_failed"
) -> Dict[str, List[Dict[str, str]]]:
    """Upload all files in local_dir to the correct Drive subfolder and delete them locally after upload."""
    creds = USER_TOKENS.get("default")
    if not creds:
        return {"error": "Drive not initialized. Call /init-drive first."}

    if not DRIVE_STRUCTURE:
        return {"error": "Drive folder structure not set. Complete OAuth callback first."}

    service = googleapiclient.discovery.build("drive", "v3", credentials=creds)

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
            drive_file = (
                service.files()
                .create(
                    body={"name": new_name, "parents": [folder_id]},
                    media_body=media,
                    fields="id, name",
                )
                .execute()
            )

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


# ============================================================
# Load state on import
# ============================================================
load_drive_state()
