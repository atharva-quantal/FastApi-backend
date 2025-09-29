import os
import pickle
from typing import Dict, List, Optional
import hashlib
import shutil

import google_auth_oauthlib.flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from fastapi.responses import RedirectResponse
from google.auth.exceptions import RefreshError
import traceback
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# -------------------------------
# Multi-user storage
# -------------------------------
USER_TOKENS: Dict[str, object] = {}
USER_DRIVE_STRUCTURES: Dict[str, Dict[str, str]] = {}

PICKLE_BASE_DIR = "user_data"
os.makedirs(PICKLE_BASE_DIR, exist_ok=True)

# -------------------------------
# Environment variables
# -------------------------------
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", "wineocr-project")
REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://fastapi-backend-4wqb.onrender.com/drive-callback"
)
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
# User identification
# -------------------------------
def get_user_id_from_credentials(credentials) -> str:
    """Generate a consistent user ID from credentials."""
    try:
        service = build("oauth2", "v2", credentials=credentials)
        user_info = service.userinfo().get().execute()
        email = user_info.get("email", "")
        if email:
            return hashlib.sha256(email.encode()).hexdigest()[:16]
    except Exception as e:
        logger.warning(f"Could not get user info: {e}")
    
    token = getattr(credentials, 'token', '')
    if token:
        return hashlib.sha256(str(token).encode()).hexdigest()[:16]
    
    import time
    return hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]


def get_user_pickle_path(user_id: str) -> str:
    """Get the pickle file path for a specific user."""
    return os.path.join(PICKLE_BASE_DIR, f"user_{user_id}_structure.pkl")


# -------------------------------
# Pickle file helpers
# -------------------------------
def save_folder_structure(user_id: str, structure: Dict[str, str]) -> None:
    """Save folder structure to user-specific pickle file."""
    try:
        pickle_path = get_user_pickle_path(user_id)
        with open(pickle_path, 'wb') as f:
            pickle.dump(structure, f)
        logger.info(f"Folder structure saved for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to save folder structure for user {user_id}: {e}")


def load_folder_structure(user_id: str) -> Optional[Dict[str, str]]:
    """Load folder structure from user-specific pickle file."""
    try:
        pickle_path = get_user_pickle_path(user_id)
        if os.path.exists(pickle_path):
            with open(pickle_path, 'rb') as f:
                structure = pickle.load(f)
            logger.info(f"Folder structure loaded for user {user_id}")
            return structure
    except Exception as e:
        logger.error(f"Failed to load folder structure for user {user_id}: {e}")
    return None


def verify_folder_structure(service, structure: Dict[str, str]) -> bool:
    """Verify that all folders in the structure still exist in Google Drive."""
    try:
        def check_folder_exists(folder_id: str) -> bool:
            try:
                result = service.files().get(fileId=folder_id, fields="id, trashed").execute()
                return not result.get("trashed", False)
            except Exception:
                return False

        if not check_folder_exists(structure.get("root", "")):
            return False

        for key in ["input", "output", "upload"]:
            if not check_folder_exists(structure.get(key, "")):
                return False

        nhr_structure = structure.get("nhr", {})
        if not isinstance(nhr_structure, dict):
            return False
            
        if not check_folder_exists(nhr_structure.get("root", "")):
            return False

        for nhr_key in ["search_failed", "ocr_failed", "manual_rejection", "others"]:
            if not check_folder_exists(nhr_structure.get(nhr_key, "")):
                return False

        return True
    except Exception as e:
        logger.error(f"Error verifying folder structure: {e}")
        return False


# -------------------------------
# Local folder management
# -------------------------------
def ensure_local_folders(base_dir: str = "processed") -> None:
    """Ensure all required local folders exist."""
    folders = [
        os.path.join(base_dir, "input"),
        os.path.join(base_dir, "output"),
        os.path.join(base_dir, "upload"),
        os.path.join(base_dir, "nhr", "search_failed"),
        os.path.join(base_dir, "nhr", "ocr_failed"),
        os.path.join(base_dir, "nhr", "manual_rejection"),
        os.path.join(base_dir, "nhr", "others")
    ]
    
    for folder in folders:
        os.makedirs(folder, exist_ok=True)


def move_file_to_folders(file_path: str, new_name: str, target_folders: List[str], base_dir: str = "processed") -> List[str]:
    """Copy/move file to multiple target folders. Returns list of destination paths."""
    moved_paths = []
    
    if not os.path.exists(file_path):
        logger.error(f"Source file not found: {file_path}")
        return moved_paths
    
    for i, folder in enumerate(target_folders):
        target_path = os.path.join(base_dir, folder, new_name)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        
        # Move on last folder, copy for others
        is_last = (i == len(target_folders) - 1)
        
        try:
            if is_last and os.path.exists(file_path):
                shutil.move(file_path, target_path)
                logger.info(f"Moved file to: {target_path}")
            else:
                shutil.copy2(file_path, target_path)
                logger.info(f"Copied file to: {target_path}")
            
            moved_paths.append(target_path)
        except Exception as e:
            logger.error(f"Failed to move/copy to {target_path}: {e}")
    
    return moved_paths


# -------------------------------
# Drive folder management
# -------------------------------
def _ensure_folder(service, name: str, parent: Optional[str] = None) -> str:
    """Ensure folder exists in Drive; create if not. Returns folder ID."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent:
        query += f" and '{parent}' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get("files", [])

    if items:
        logger.info(f"Found existing folder: {name} (ID: {items[0]['id']})")
        return items[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent:
        metadata["parents"] = [parent]

    folder = service.files().create(body=metadata, fields="id").execute()
    logger.info(f"Created new folder: {name} (ID: {folder['id']})")
    return folder["id"]


def refresh_credentials_if_needed(creds):
    """Refresh credentials if they're expired."""
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh()
            return True
        except RefreshError as e:
            logger.error(f"Failed to refresh credentials: {e}")
            return False
    return True


def _get_or_create_structure(service, user_id: str) -> Dict[str, str]:
    """Build Drive folder structure and save to pickle file."""
    logger.info(f"Creating/verifying Drive folder structure for user {user_id}")
    
    root_id = _ensure_folder(service, "WineOCR_Processed")
    input_id = _ensure_folder(service, "Input", root_id)
    output_id = _ensure_folder(service, "Output", root_id)
    upload_id = _ensure_folder(service, "Upload", root_id)
    nhr_id = _ensure_folder(service, "NHR", root_id)

    nhr_structure = {
        "root": nhr_id,
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
        "nhr": nhr_structure
    }

    save_folder_structure(user_id, structure)
    logger.info(f"Drive folder structure created for user {user_id}")
    return structure


def get_folder_structure(service, user_id: str) -> Dict[str, str]:
    """Get folder structure for user - load from pickle or create new."""
    if user_id not in USER_DRIVE_STRUCTURES:
        saved_structure = load_folder_structure(user_id)
        
        if saved_structure and verify_folder_structure(service, saved_structure):
            logger.info(f"Using saved folder structure for user {user_id}")
            USER_DRIVE_STRUCTURES[user_id] = saved_structure
        else:
            logger.info(f"Creating new folder structure for user {user_id}")
            USER_DRIVE_STRUCTURES[user_id] = _get_or_create_structure(service, user_id)
    
    return USER_DRIVE_STRUCTURES[user_id]


# -------------------------------
# OAuth functions
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


def oauth_callback(code: str, state: str):
    """Exchange code for tokens, create folders, then redirect to frontend."""
    try:
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            CLIENT_CONFIG, scopes=SCOPES, state=state
        )
        flow.redirect_uri = REDIRECT_URI
        flow.fetch_token(code=code)

        credentials = flow.credentials
        user_id = get_user_id_from_credentials(credentials)
        USER_TOKENS[user_id] = credentials
        
        service = build("drive", "v3", credentials=credentials)
        structure = get_folder_structure(service, user_id)

        logger.info(f"Drive connected successfully for user {user_id}")
        return RedirectResponse(url=f"{FRONTEND_URL}?drive_connected=success&user_id={user_id}")

    except Exception as e:
        logger.error(f"Error during Drive callback: {traceback.format_exc()}")
        return RedirectResponse(url=f"{FRONTEND_URL}?drive_error={str(e)}")


def is_drive_ready(user_id: str = None) -> Dict[str, object]:
    """Check if Drive is linked and ready for a specific user."""
    if not user_id:
        return {
            "linked": False,
            "error": "No user_id provided",
            "total_users": len(USER_TOKENS)
        }
    
    creds = USER_TOKENS.get(user_id)
    if not creds:
        return {
            "linked": False,
            "folder_structure_cached": user_id in USER_DRIVE_STRUCTURES,
            "pickle_file_exists": os.path.exists(get_user_pickle_path(user_id)),
            "user_id": user_id
        }
    
    valid = True
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh()
        except RefreshError:
            valid = False
    elif creds.expired:
        valid = False
    
    return {
        "linked": valid,
        "folder_structure_cached": user_id in USER_DRIVE_STRUCTURES,
        "pickle_file_exists": os.path.exists(get_user_pickle_path(user_id)),
        "user_id": user_id
    }


# -------------------------------
# Drive upload function
# -------------------------------
def upload_to_drive(user_id: str, local_dir: str, target_folders: List[str]) -> Dict[str, object]:
    """
    Upload files from local folder structure to Google Drive.
    Looks for files in local_dir/<target_folder>/ and uploads to corresponding Drive folder.
    """
    try:
        creds = USER_TOKENS.get(user_id)
        if not creds:
            return {"error": f"Drive not initialized for user {user_id}"}

        if not refresh_credentials_if_needed(creds):
            return {"error": "Failed to refresh expired credentials"}

        service = build("drive", "v3", credentials=creds)
        structure = get_folder_structure(service, user_id)

        all_uploaded = []
        errors = []

        for target_folder in target_folders:
            # Resolve Drive folder ID
            if target_folder.startswith("nhr/"):
                nhr_reason = target_folder.split("/")[1]
                drive_folder_id = structure["nhr"].get(nhr_reason)
            else:
                drive_folder_id = structure.get(target_folder)

            if not drive_folder_id:
                errors.append(f"Invalid target folder: {target_folder}")
                continue

            # Local folder path
            local_folder_path = os.path.join(local_dir, target_folder)
            
            if not os.path.exists(local_folder_path):
                logger.warning(f"Local folder not found: {local_folder_path}")
                continue

            # Upload all files from this folder
            for filename in os.listdir(local_folder_path):
                file_path = os.path.join(local_folder_path, filename)
                
                if not os.path.isfile(file_path):
                    continue

                try:
                    media = MediaFileUpload(file_path, mimetype="image/jpeg")
                    drive_file = service.files().create(
                        body={"name": filename, "parents": [drive_folder_id]},
                        media_body=media,
                        fields="id, name, webViewLink"
                    ).execute()

                    all_uploaded.append({
                        "filename": filename,
                        "target": target_folder,
                        "drive_id": drive_file.get("id"),
                        "web_view_link": drive_file.get("webViewLink")
                    })

                    # Clean up local file after successful upload
                    os.remove(file_path)
                    logger.info(f"Uploaded and deleted: {filename} -> {target_folder}")

                except Exception as e:
                    error_msg = f"Failed to upload {filename} to {target_folder}: {str(e)}"
                    logger.error(error_msg)
                    errors.append(error_msg)

        result = {
            "message": f"Uploaded {len(all_uploaded)} files across {len(target_folders)} folders",
            "uploaded_files": all_uploaded,
            "user_id": user_id,
            "success_count": len(all_uploaded)
        }

        if errors:
            result["errors"] = errors
            result["error_count"] = len(errors)

        return result

    except Exception as e:
        error_msg = f"Upload failed for user {user_id}: {str(e)}"
        logger.error(f"{error_msg}\n{traceback.format_exc()}")
        return {"error": error_msg}


# -------------------------------
# Utility functions
# -------------------------------
def reset_folder_structure(user_id: str) -> Dict[str, str]:
    """Reset folder structure for a specific user."""
    try:
        pickle_path = get_user_pickle_path(user_id)
        if os.path.exists(pickle_path):
            os.remove(pickle_path)
            logger.info(f"Deleted pickle file for user {user_id}")
        
        if user_id in USER_DRIVE_STRUCTURES:
            del USER_DRIVE_STRUCTURES[user_id]
        
        return {"message": f"Folder structure reset for user {user_id}"}
    except Exception as e:
        return {"error": f"Failed to reset folder structure: {e}"}


def debug_drive_structure():
    """Return debug info about drive structure."""
    return {
        "total_users": len(USER_TOKENS),
        "user_tokens": list(USER_TOKENS.keys()),
        "user_tokens_valid": {k: (not v.expired if v else False) for k, v in USER_TOKENS.items()},
        "user_structures_cached": list(USER_DRIVE_STRUCTURES.keys()),
        "pickle_files": [f for f in os.listdir(PICKLE_BASE_DIR) if f.startswith("user_") and f.endswith(".pkl")]
    }