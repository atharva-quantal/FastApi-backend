import os
import pickle
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

# Pickle file path for persistent storage
FOLDER_STRUCTURE_FILE = "drive_folder_structure.pkl"

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
# Pickle file helpers
# -------------------------------
def save_folder_structure(structure: Dict[str, str]) -> None:
    """Save folder structure to pickle file."""
    try:
        with open(FOLDER_STRUCTURE_FILE, 'wb') as f:
            pickle.dump(structure, f)
        print(f"✅ Folder structure saved to {FOLDER_STRUCTURE_FILE}")
    except Exception as e:
        print(f"❌ Failed to save folder structure: {e}")


def load_folder_structure() -> Optional[Dict[str, str]]:
    """Load folder structure from pickle file."""
    try:
        if os.path.exists(FOLDER_STRUCTURE_FILE):
            with open(FOLDER_STRUCTURE_FILE, 'rb') as f:
                structure = pickle.load(f)
            print(f"✅ Folder structure loaded from {FOLDER_STRUCTURE_FILE}")
            return structure
    except Exception as e:
        print(f"❌ Failed to load folder structure: {e}")
    return None


def verify_folder_structure(service, structure: Dict[str, str]) -> bool:
    """Verify that all folders in the structure still exist in Google Drive."""
    try:
        def check_folder_exists(folder_id: str) -> bool:
            try:
                service.files().get(fileId=folder_id, fields="id, trashed").execute()
                return True
            except Exception:
                return False

        # Check root folder
        if not check_folder_exists(structure.get("root", "")):
            return False

        # Check main folders
        for key in ["input", "output", "upload"]:
            if not check_folder_exists(structure.get(key, "")):
                return False

        # Check NHR folders
        nhr_structure = structure.get("nhr", {})
        if not check_folder_exists(nhr_structure.get("root", "")):
            return False

        for nhr_key in ["search_failed", "ocr_failed", "manual_rejection", "others"]:
            if not check_folder_exists(nhr_structure.get(nhr_key, "")):
                return False

        return True
    except Exception as e:
        print(f"❌ Error verifying folder structure: {e}")
        return False


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
        print(f"✅ Found existing folder: {name} (ID: {items[0]['id']})")
        return items[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent:
        metadata["parents"] = [parent]

    folder = service.files().create(body=metadata, fields="id").execute()
    print(f"✅ Created new folder: {name} (ID: {folder['id']})")
    return folder["id"]


def _get_or_create_structure(service) -> Dict[str, str]:
    """Rebuild Drive folder structure and save to pickle file."""
    print("🔄 Creating/verifying Drive folder structure...")
    
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

    structure = {
        "root": root_id,
        "input": input_id,
        "output": output_id,
        "upload": upload_id,
        "nhr": {"root": nhr_id, **nhr_structure}
    }

    # Save structure to pickle file
    save_folder_structure(structure)
    
    print("✅ Drive folder structure created and saved successfully!")
    return structure


def get_folder_structure(service) -> Dict[str, str]:
    """Get folder structure - load from pickle or create new one."""
    global DRIVE_STRUCTURE
    
    # Try to load from pickle file first
    if not DRIVE_STRUCTURE:
        saved_structure = load_folder_structure()
        
        if saved_structure and verify_folder_structure(service, saved_structure):
            print("✅ Using saved folder structure")
            DRIVE_STRUCTURE = saved_structure
        else:
            print("🔄 Saved structure invalid or missing, creating new one...")
            DRIVE_STRUCTURE = _get_or_create_structure(service)
    
    return DRIVE_STRUCTURE


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

        # Get or create folders with persistent storage
        DRIVE_STRUCTURE = get_folder_structure(service)
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
        "linked": "default" in USER_TOKENS,
        "folder_structure_cached": bool(DRIVE_STRUCTURE),
        "pickle_file_exists": os.path.exists(FOLDER_STRUCTURE_FILE)
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

    # ✅ Get folder structure (will load from pickle or create new one)
    global DRIVE_STRUCTURE
    DRIVE_STRUCTURE = get_folder_structure(service)

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
                print(f"✅ Deleted local file: {file_path}")
            except Exception as e:
                print(f"⚠️ Warning: Could not delete {file_path} -> {e}")

    return {
        "message": f"Uploaded {len(uploaded)} files to {target} and cleaned up local copies",
        "files": uploaded,
    }


# -------------------------------
# Additional utility functions
# -------------------------------
def reset_folder_structure() -> Dict[str, str]:
    """Reset folder structure - delete pickle file and clear cache."""
    global DRIVE_STRUCTURE
    
    try:
        if os.path.exists(FOLDER_STRUCTURE_FILE):
            os.remove(FOLDER_STRUCTURE_FILE)
            print(f"✅ Deleted {FOLDER_STRUCTURE_FILE}")
        
        DRIVE_STRUCTURE = {}
        return {"message": "Folder structure reset. Will be recreated on next operation."}
    
    except Exception as e:
        return {"error": f"Failed to reset folder structure: {e}"}


def get_folder_info() -> Dict[str, object]:
    """Get information about current folder structure."""
    return {
        "structure": DRIVE_STRUCTURE,
        "pickle_file_exists": os.path.exists(FOLDER_STRUCTURE_FILE),
        "pickle_file_path": os.path.abspath(FOLDER_STRUCTURE_FILE) if os.path.exists(FOLDER_STRUCTURE_FILE) else None
    }


# Load folder structure on module import (if pickle file exists)
try:
    saved_structure = load_folder_structure()
    if saved_structure:
        DRIVE_STRUCTURE = saved_structure
        if DRIVE_STRUCTURE.get("root"):
            DRIVE_FOLDER_ID = DRIVE_STRUCTURE["root"]
except Exception as e:
    print(f"⚠️ Could not load folder structure on startup: {e}")