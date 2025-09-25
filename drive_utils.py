# drive_utils.py
import os
from typing import Dict, List

import google_auth_oauthlib.flow
import googleapiclient.discovery
from googleapiclient.http import MediaFileUpload

# -------------------------------
# Global store (replace with DB if multi-user)
# -------------------------------
USER_TOKENS: Dict[str, object] = {}
DRIVE_FOLDER_ID: str = None

# -------------------------------
# Client configuration
# -------------------------------
CLIENT_CONFIG = {
    "web": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "project_id": os.getenv("GOOGLE_PROJECT_ID"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "redirect_uris": [os.getenv("GOOGLE_REDIRECT_URI")]
    }
}

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# -------------------------------
# Drive Init (Step 1)
# -------------------------------
def init_drive() -> Dict[str, str]:
    """Generate Google OAuth consent URL for Drive sign-in."""
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES
    )
    flow.redirect_uri = CLIENT_CONFIG["web"]["redirect_uris"][0]

    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true"
    )
    return {"auth_url": auth_url, "state": state}

# -------------------------------
# Drive Callback (Step 2)
# -------------------------------
def oauth_callback(code: str, state: str) -> Dict[str, str]:
    """Exchange code for tokens and create necessary folders."""
    global DRIVE_FOLDER_ID

    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES, state=state
    )
    flow.redirect_uri = CLIENT_CONFIG["web"]["redirect_uris"][0]
    flow.fetch_token(code=code)

    credentials = flow.credentials
    USER_TOKENS["default"] = credentials

    # Create folders if not already present
    service = googleapiclient.discovery.build("drive", "v3", credentials=credentials)

    folder_name = "WineOCR_Processed"
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get("files", [])

    if items:
        DRIVE_FOLDER_ID = items[0]["id"]
    else:
        folder_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder"
        }
        folder = service.files().create(body=folder_metadata, fields="id").execute()
        DRIVE_FOLDER_ID = folder["id"]

    return {
        "message": f"Signed in successfully. Folder ready: {folder_name}",
        "folder_id": DRIVE_FOLDER_ID
    }

# -------------------------------
# Upload to Drive (Step 3)
# -------------------------------
def upload_to_drive(
    local_dir: str = "processed",
    rename_map: Dict[str, str] = None
) -> Dict[str, List[Dict[str, str]]]:
    """
    Upload all files in local_dir to the Drive folder.
    If rename_map is provided, filenames will be renamed before upload.
    Example rename_map: {"wine1.jpg": "2012_Chateau_Test.jpg"}
    """
    creds = USER_TOKENS.get("default")
    if not creds:
        return {"error": "Drive not initialized. Call /init-drive first."}

    if not DRIVE_FOLDER_ID:
        return {"error": "Drive folder not set. Complete OAuth callback first."}

    service = googleapiclient.discovery.build("drive", "v3", credentials=creds)

    uploaded = []
    for file in os.listdir(local_dir):
        file_path = os.path.join(local_dir, file)

        # Use mapped name if available
        new_name = rename_map.get(file, file) if rename_map else file

        media = MediaFileUpload(file_path, mimetype="image/jpeg")
        drive_file = service.files().create(
            body={"name": new_name, "parents": [DRIVE_FOLDER_ID]},
            media_body=media,
            fields="id, name"
        ).execute()
        uploaded.append(drive_file)

    return {
        "message": f"Uploaded {len(uploaded)} files",
        "files": uploaded
    }
