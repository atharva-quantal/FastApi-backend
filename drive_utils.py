import os
import pickle
from typing import Dict, List, Optional
import hashlib
import json

import google_auth_oauthlib.flow
import googleapiclient.discovery
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload
from fastapi.responses import RedirectResponse
from google.auth.exceptions import RefreshError
import traceback

# -------------------------------
# Multi-user storage (replace with proper DB in production)
# -------------------------------
USER_TOKENS: Dict[str, object] = {}  # user_id -> credentials
USER_DRIVE_STRUCTURES: Dict[str, Dict[str, str]] = {}  # user_id -> folder structure

# Base directory for user-specific pickle files
PICKLE_BASE_DIR = "user_data"
os.makedirs(PICKLE_BASE_DIR, exist_ok=True)

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
# User identification helpers
# -------------------------------
def get_user_id_from_credentials(credentials) -> str:
    """Generate a consistent user ID from credentials."""
    try:
        # Use the token to get user info
        service = build("oauth2", "v2", credentials=credentials)
        user_info = service.userinfo().get().execute()
        email = user_info.get("email", "")
        if email:
            # Create a hash of the email for consistent user ID
            return hashlib.sha256(email.encode()).hexdigest()[:16]
    except Exception as e:
        print(f"Warning: Could not get user info: {e}")
    
    # Fallback: use token hash
    token = getattr(credentials, 'token', '')
    if token:
        return hashlib.sha256(str(token).encode()).hexdigest()[:16]
    
    # Last resort: random ID (not ideal for persistence)
    import time
    return hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]


def get_user_pickle_path(user_id: str) -> str:
    """Get the pickle file path for a specific user."""
    return os.path.join(PICKLE_BASE_DIR, f"user_{user_id}_structure.pkl")


# -------------------------------
# Pickle file helpers (now user-specific)
# -------------------------------
def save_folder_structure(user_id: str, structure: Dict[str, str]) -> None:
    """Save folder structure to user-specific pickle file."""
    try:
        pickle_path = get_user_pickle_path(user_id)
        with open(pickle_path, 'wb') as f:
            pickle.dump(structure, f)
        print(f"✅ Folder structure saved for user {user_id} to {pickle_path}")
    except Exception as e:
        print(f"❌ Failed to save folder structure for user {user_id}: {e}")


def load_folder_structure(user_id: str) -> Optional[Dict[str, str]]:
    """Load folder structure from user-specific pickle file."""
    try:
        pickle_path = get_user_pickle_path(user_id)
        if os.path.exists(pickle_path):
            with open(pickle_path, 'rb') as f:
                structure = pickle.load(f)
            print(f"✅ Folder structure loaded for user {user_id} from {pickle_path}")
            return structure
    except Exception as e:
        print(f"❌ Failed to load folder structure for user {user_id}: {e}")
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

        # Check root folder
        if not check_folder_exists(structure.get("root", "")):
            return False

        # Check main folders
        for key in ["input", "output", "upload"]:
            if not check_folder_exists(structure.get(key, "")):
                return False

        # Check NHR folders
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


def refresh_credentials_if_needed(creds):
    """Refresh credentials if they're expired"""
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh()
            return True
        except RefreshError as e:
            print(f"Failed to refresh credentials: {e}")
            return False
    return True


def _get_or_create_structure(service, user_id: str) -> Dict[str, str]:
    """Rebuild Drive folder structure and save to user-specific pickle file."""
    print(f"🔄 Creating/verifying Drive folder structure for user {user_id}...")
    
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

    # Save structure to user-specific pickle file
    save_folder_structure(user_id, structure)
    
    print(f"✅ Drive folder structure created and saved successfully for user {user_id}!")
    return structure


def get_folder_structure(service, user_id: str) -> Dict[str, str]:
    """Get folder structure for specific user - load from pickle or create new one."""
    
    # Try to load from user-specific pickle file first
    if user_id not in USER_DRIVE_STRUCTURES:
        saved_structure = load_folder_structure(user_id)
        
        if saved_structure and verify_folder_structure(service, saved_structure):
            print(f"✅ Using saved folder structure for user {user_id}")
            USER_DRIVE_STRUCTURES[user_id] = saved_structure
        else:
            print(f"🔄 Saved structure invalid or missing for user {user_id}, creating new one...")
            USER_DRIVE_STRUCTURES[user_id] = _get_or_create_structure(service, user_id)
    
    return USER_DRIVE_STRUCTURES[user_id]


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
    """Exchange code for tokens, create folders, then redirect to frontend."""
    try:
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            CLIENT_CONFIG, scopes=SCOPES, state=state
        )
        flow.redirect_uri = REDIRECT_URI
        flow.fetch_token(code=code)

        credentials = flow.credentials
        
        # Get user ID from credentials
        user_id = get_user_id_from_credentials(credentials)
        
        # Store credentials for this user
        USER_TOKENS[user_id] = credentials
        
        service = build("drive", "v3", credentials=credentials)

        # ✅ Create / verify folder structure for this specific user
        print(f"🔄 Creating/Verifying Drive folder structure for user {user_id}...")
        structure = get_folder_structure(service, user_id)

        # 🔎 Debug logs
        print(f"✅ Drive connected successfully for user {user_id}!")
        print("📂 Folder structure:")
        for key, val in structure.items():
            if isinstance(val, dict):
                print(f"  {key}:")
                for sub, sub_id in val.items():
                    print(f"    - {sub}: {sub_id}")
            else:
                print(f"  {key}: {val}")

        # ✅ Redirect back to frontend with success and user_id
        return RedirectResponse(url=f"{FRONTEND_URL}?drive_connected=success&user_id={user_id}")

    except Exception as e:
        import traceback
        print("❌ Error during Drive callback:")
        print(traceback.format_exc())
        return RedirectResponse(url=f"{FRONTEND_URL}?drive_error={str(e)}")


# -------------------------------
# Drive Status
# -------------------------------
def is_drive_ready(user_id: str = None) -> Dict[str, object]:
    """Check if Drive is linked and folder is ready for a specific user."""
    if not user_id:
        # Return status for all users if no specific user_id provided
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
    
    # Check if credentials are valid or can be refreshed
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
# Upload to Drive (Enhanced for user selections)
# -------------------------------
def upload_to_drive(
    user_id: str,
    local_dir: str = "processed",
    rename_map: Dict[str, str] = None,
    target: str = "output"  # e.g. "input", "output", "upload", "nhr.search_failed"
) -> Dict[str, List[Dict[str, str]]]:
    """Upload all files in local_dir to the correct Drive subfolder and delete them locally after upload."""
    creds = USER_TOKENS.get(user_id)
    if not creds:
        return {"error": f"Drive not initialized for user {user_id}. Call /init-drive first."}

    if not refresh_credentials_if_needed(creds):
        return {"error": "Failed to refresh expired credentials"}

    service = build("drive", "v3", credentials=creds)

    # ✅ Get folder structure for this user
    structure = get_folder_structure(service, user_id)

    # Resolve folder ID
    folder_id = None
    if target.startswith("nhr."):
        _, sub = target.split(".", 1)
        folder_id = structure["nhr"].get(sub)
    else:
        folder_id = structure.get(target)

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


def upload_single_file_to_drive(user_id: str, file_bytes: bytes, filename: str, target: str = "output") -> str:
    """Upload a single file (bytes) to Drive for a specific user. Returns file ID or raises exception."""
    try:
        creds = USER_TOKENS.get(user_id)
        if not creds:
            raise Exception(f"Drive not initialized for user {user_id}. Call /init-drive first.")

        if not refresh_credentials_if_needed(creds):
            raise Exception("Failed to refresh expired credentials")

        service = build("drive", "v3", credentials=creds)

        # Get folder structure for this user
        structure = get_folder_structure(service, user_id)

        # Resolve folder ID
        folder_id = None
        if target.startswith("nhr."):
            _, sub = target.split(".", 1)
            folder_id = structure["nhr"].get(sub)
        else:
            folder_id = structure.get(target)

        if not folder_id:
            raise Exception(f"Invalid target folder: {target}")

        # Determine appropriate MIME type
        mime_type = "application/octet-stream"
        if filename.lower().endswith(('.jpg', '.jpeg')):
            mime_type = "image/jpeg"
        elif filename.lower().endswith('.png'):
            mime_type = "image/png"

        file_metadata = {"name": filename, "parents": [folder_id]}
        media = MediaInMemoryUpload(file_bytes, mimetype=mime_type)

        print(f"[Drive Upload] User {user_id}: Uploading {filename} to {target}")
        print(f"[Drive Upload] Target folder_id: {folder_id}")
        print(f"[Drive Upload] File size: {len(file_bytes)} bytes")

        uploaded_file = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id,name,parents,webViewLink")
            .execute()
        )

        print(f"[Drive Upload] Success for user {user_id}: {uploaded_file}")
        return uploaded_file.get("id")

    except Exception as e:
        error_msg = f"Drive upload failed for user {user_id}, file {filename}: {str(e)}"
        print(f"[Drive Upload] ERROR: {error_msg}")
        print(f"[Drive Upload] Traceback: {traceback.format_exc()}")
        raise Exception(error_msg)


# -------------------------------
# Additional utility functions
# -------------------------------
def reset_folder_structure(user_id: str) -> Dict[str, str]:
    """Reset folder structure for a specific user - delete pickle file and clear cache."""
    try:
        pickle_path = get_user_pickle_path(user_id)
        if os.path.exists(pickle_path):
            os.remove(pickle_path)
            print(f"✅ Deleted {pickle_path}")
        
        if user_id in USER_DRIVE_STRUCTURES:
            del USER_DRIVE_STRUCTURES[user_id]
        
        return {"message": f"Folder structure reset for user {user_id}. Will be recreated on next operation."}
    
    except Exception as e:
        return {"error": f"Failed to reset folder structure for user {user_id}: {e}"}


def get_folder_info(user_id: str) -> Dict[str, object]:
    """Get information about current folder structure for a specific user."""
    pickle_path = get_user_pickle_path(user_id)
    return {
        "structure": USER_DRIVE_STRUCTURES.get(user_id, {}),
        "pickle_file_exists": os.path.exists(pickle_path),
        "pickle_file_path": os.path.abspath(pickle_path) if os.path.exists(pickle_path) else None,
        "user_id": user_id
    }


def debug_drive_structure():
    """Return debug info about drive structure"""
    return {
        "total_users": len(USER_TOKENS),
        "user_tokens": list(USER_TOKENS.keys()),
        "user_tokens_valid": {k: (not v.expired if v else False) for k, v in USER_TOKENS.items()},
        "user_structures_cached": list(USER_DRIVE_STRUCTURES.keys()),
        "pickle_files": [f for f in os.listdir(PICKLE_BASE_DIR) if f.startswith("user_") and f.endswith(".pkl")]
    }


# -------------------------------
# Session management helpers
# -------------------------------
def get_current_user_id(request_headers: dict = None) -> Optional[str]:
    """Extract user ID from request headers or return None."""
    # In a real application, you'd implement proper session management
    # For now, we'll rely on the frontend to pass user_id
    if request_headers:
        return request_headers.get("X-User-ID")
    return None


def cleanup_expired_users():
    """Clean up expired user sessions (call this periodically)."""
    expired_users = []
    for user_id, creds in USER_TOKENS.items():
        if creds.expired and not creds.refresh_token:
            expired_users.append(user_id)
    
    for user_id in expired_users:
        print(f"Cleaning up expired user: {user_id}")
        USER_TOKENS.pop(user_id, None)
        USER_DRIVE_STRUCTURES.pop(user_id, None)