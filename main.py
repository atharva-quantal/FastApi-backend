from fastapi import FastAPI, UploadFile, File, Query, Header, Body
from fastapi.responses import JSONResponse
from typing import List, Optional
import os
import shutil
import time
import json
from fastapi import APIRouter
from pydantic import BaseModel
from drive_utils import (
    USER_TOKENS, USER_DRIVE_STRUCTURES, init_auth, oauth_callback,
    upload_to_drive, is_authenticated, debug_drive_structure,
    get_user_id_from_credentials, ensure_local_folders, move_file_to_folders
)

from ocr import process_image
from graphql import get_shopify_data
from compare_products import compare
from shopify_upload import upload_image_to_shopify
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Wine OCR + Matching API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://david-f-frontend.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Directories & cache files
UPLOAD_DIR = "uploads"
PROCESSED_DIR = "processed"
CACHE_FILE = "ocr_results.json"
COMPARE_FILE = "compare_results.json"


# -------------------------------
# Request models
# -------------------------------
class DriveSelection(BaseModel):
    image: str
    selected_name: str
    target: Optional[str] = "output"
    nhr_reason: Optional[str] = None


class DriveUploadRequest(BaseModel):
    user_id: str
    selections: List[DriveSelection]


# -------------------------------
# Utility functions
# -------------------------------
def clear_folder(folder: str):
    """Delete all files inside a folder, keep folder."""
    if os.path.exists(folder):
        for f in os.listdir(folder):
            file_path = os.path.join(folder, f)
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
    else:
        os.makedirs(folder, exist_ok=True)


# -------------------------------
# Startup
# -------------------------------
@app.on_event("startup")
def startup_event():
    """Ensure clean state and folder structure when backend starts."""
    for folder in [UPLOAD_DIR, PROCESSED_DIR]:
        if os.path.exists(folder):
            shutil.rmtree(folder)
        os.makedirs(folder)
    
    ensure_local_folders(PROCESSED_DIR)
    
    for cache_file in [CACHE_FILE, COMPARE_FILE]:
        if os.path.exists(cache_file):
            os.remove(cache_file)
    
    print("✅ Initialized clean folder structure and removed cache files")


# -------------------------------
# UNIFIED Authentication Endpoints (Google + Drive)
# -------------------------------
@app.post("/auth/init")
def init_auth_endpoint():
    """
    Start unified Google + Drive authentication.
    Returns OAuth URL that handles both Google sign-in and Drive access.
    """
    try:
        return init_auth()
    except Exception as e:
        print(f"Init auth error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/auth/callback")
def auth_callback_endpoint(code: str = Query(...), state: str = Query(...)):
    """
    Handle unified OAuth callback.
    Creates user session, sets up Drive folders, and redirects to frontend.
    """
    try:
        return oauth_callback(code, state)
    except Exception as e:
        print(f"Auth callback error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/auth/status")
def auth_status_endpoint(user_id: Optional[str] = Query(None)):
    """
    Check if user is authenticated and Drive is ready.
    Returns unified status for both Google authentication and Drive connection.
    """
    try:
        if not user_id:
            return JSONResponse(
                status_code=400,
                content={"error": "user_id parameter is required"}
            )
        
        status = is_authenticated(user_id)
        debug_info = debug_drive_structure()
        return {**status, "debug": debug_info}
    except Exception as e:
        print(f"Auth status error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# -------------------------------
# DEPRECATED Endpoints (kept for backward compatibility)
# -------------------------------
@app.post("/init-drive")
def init_drive_endpoint():
    """
    DEPRECATED: Use /auth/init instead.
    This endpoint is kept for backward compatibility.
    """
    print("⚠️ Warning: /init-drive is deprecated. Use /auth/init instead.")
    try:
        return init_auth()
    except Exception as e:
        print(f"Init drive error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/drive-callback")
def drive_callback_endpoint(code: str = Query(...), state: str = Query(...)):
    """
    DEPRECATED: Use /auth/callback instead.
    This endpoint is kept for backward compatibility.
    """
    print("⚠️ Warning: /drive-callback is deprecated. Use /auth/callback instead.")
    try:
        return oauth_callback(code, state)
    except Exception as e:
        print(f"Drive callback error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/drive-status")
def drive_status_endpoint(user_id: Optional[str] = Query(None)):
    """
    DEPRECATED: Use /auth/status instead.
    This endpoint is kept for backward compatibility.
    """
    print("⚠️ Warning: /drive-status is deprecated. Use /auth/status instead.")
    try:
        if not user_id:
            return JSONResponse(
                status_code=400,
                content={"error": "user_id parameter is required"}
            )
        
        status = is_authenticated(user_id)
        debug_info = debug_drive_structure()
        return {**status, "debug": debug_info}
    except Exception as e:
        print(f"Drive status error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# -------------------------------
# Drive Upload Endpoint
# -------------------------------
@app.post("/upload-to-drive")
def upload_to_drive_endpoint(payload: DriveUploadRequest = Body(...)):
    """
    Upload images with user-selected names to Google Drive.
    This should be called AFTER OCR and comparison are complete.
    
    File organization logic:
    - input/ → Original user-uploaded filename (from compare results)
    - output/ & upload/ → User-selected renamed file (for normal flow)
    - nhr/<reason>/ → Original user-uploaded filename (for NHR flow)
    """
    try:
        user_id = payload.user_id
        selections = payload.selections

        # Check authentication and Drive connection
        auth_status = is_authenticated(user_id)
        if not auth_status.get("authenticated", False):
            return JSONResponse(
                status_code=400,
                content={"error": f"User not authenticated: {user_id}. Please sign in first."}
            )

        # Verify compare results exist
        if not os.path.exists(COMPARE_FILE):
            return JSONResponse(
                status_code=400,
                content={"error": "No comparison results found. Please run OCR and comparison first."}
            )

        # Load compare results to get mapping of OCR filenames to original filenames
        with open(COMPARE_FILE, "r", encoding="utf-8") as f:
            compare_results = json.load(f)
        
        # Create mapping: OCR filename -> original filename
        ocr_to_original = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                ocr_results = json.load(f)
                for result in ocr_results:
                    new_filename = result.get("new_filename")
                    original_filename = result.get("original_filename")
                    if new_filename and original_filename:
                        ocr_to_original[new_filename] = original_filename

        files_organized = []
        target_folders_set = set()
        errors = []

        for selection in selections:
            ocr_filename = selection.image  # This is the OCR-generated filename
            selected_name = selection.selected_name  # User's product name choice
            target = selection.target or "output"
            nhr_reason = selection.nhr_reason

            if not all([ocr_filename, selected_name]):
                errors.append(f"Skipping incomplete selection: {selection}")
                continue

            source_path = os.path.join(PROCESSED_DIR, ocr_filename)
            
            if not os.path.exists(source_path):
                errors.append(f"Source file not found: {source_path}")
                continue

            # Get original filename for input folder
            original_filename = ocr_to_original.get(ocr_filename, ocr_filename)

            # Sanitize user-selected name for filename (keep spaces, only remove path separators)
            safe_name = selected_name.replace("/", "-").replace("\\", "-")
            
            # Get file extension from source file
            _, source_ext = os.path.splitext(source_path)
            if not source_ext:
                source_ext = ".jpg"  # Default to .jpg if no extension
            
            # Remove extension from safe_name if it already has one to avoid double extensions
            if safe_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
                safe_name = os.path.splitext(safe_name)[0]
            
            renamed_file = f"{safe_name}{source_ext}"

            # Prepare all target folders and filenames for this file
            folders_to_create = []
            
            # Always include input with original filename
            folders_to_create.append(("input", original_filename))
            
            # Handle target-specific logic
            if target == "nhr" and nhr_reason:
                # NHR case: add nhr/<reason>/ with original filename
                nhr_folder = f"nhr/{nhr_reason}"
                folders_to_create.append((nhr_folder, original_filename))
            else:
                # Normal case: add output/ and upload/ with renamed file
                folders_to_create.append(("output", renamed_file))
                folders_to_create.append(("upload", renamed_file))

            # Process all copies for this file
            for folder, filename in folders_to_create:
                # Create target directory if it doesn't exist
                target_dir = os.path.join(PROCESSED_DIR, folder)
                os.makedirs(target_dir, exist_ok=True)
                
                # Copy file to target
                target_path = os.path.join(target_dir, filename)
                shutil.copy2(source_path, target_path)
                
                target_folders_set.add(folder)
                
                # Record the operation
                file_info = {
                    "ocr_filename": ocr_filename,
                    "original_filename": original_filename,
                    "target": folder,
                    "filename": filename
                }
                
                if folder not in ["input"] and target != "nhr":
                    file_info["renamed_to"] = selected_name
                elif "nhr" in folder:
                    file_info["user_selected"] = selected_name
                    file_info["nhr_reason"] = nhr_reason
                    
                files_organized.append(file_info)

        # Upload all organized files to Drive
        if not target_folders_set:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "No files were organized for upload",
                    "errors": errors
                }
            )

        print(f"Target folders to upload: {target_folders_set}")
        upload_result = upload_to_drive(
            user_id=user_id,
            local_dir=PROCESSED_DIR,
            target_folders=list(target_folders_set)
        )

        response = {
            "message": f"Upload complete. Processed {len(selections)} selections.",
            "files_organized": files_organized,
            "upload_result": upload_result,
            "user_id": user_id
        }

        if errors:
            response["errors"] = errors
            response["error_count"] = len(errors)

        return response

    except Exception as e:
        import traceback
        print(f"🚨 Upload error: {e}")
        print(traceback.format_exc())
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/get-compare-results")
def get_compare_results():
    """Get compare results formatted for frontend display"""
    try:
        if not os.path.exists(COMPARE_FILE):
            return JSONResponse(
                status_code=400,
                content={"error": "No compare results found. Run /compare-batch first."}
            )
        
        with open(COMPARE_FILE, "r", encoding="utf-8") as f:
            compare_results = json.load(f)
        
        # Format results for frontend
        frontend_results = []
        for result in compare_results:
            image_name = result.get("image")
            matches_data = result.get("matches", {})
            
            # Handle both old and new formats
            if isinstance(matches_data, list):
                candidates = matches_data
                original_text = ""
                validated_gid = ""
                need_human_review = False
            else:
                candidates = matches_data.get("candidates", [])
                original_text = matches_data.get("orig", "")
                validated_gid = matches_data.get("validated_gid", "")
                need_human_review = matches_data.get("need_human_review", False)
            
            if not candidates:
                continue
            
            options = []
            for candidate in candidates:
                name = candidate.get("text") or candidate.get("name", "")
                options.append({
                    "name": name,
                    "score": candidate.get("score", 0),
                    "reason": candidate.get("reason", ""),
                    "gid": candidate.get("gid", "")
                })
            
            frontend_results.append({
                "image": image_name,
                "original_text": original_text,
                "options": options,
                "validated_gid": validated_gid,
                "need_human_review": need_human_review
            })
        
        return {
            "results": frontend_results,
            "total_images": len(frontend_results)
        }
        
    except Exception as e:
        print(f"Get compare results error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# -------------------------------
# Upload & Process Endpoints
# -------------------------------
@app.post("/upload-images")
async def upload_images(files: List[UploadFile] = File(...)):
    """Upload multiple images and clear old ones first."""
    try:
        clear_folder(UPLOAD_DIR)

        saved_files = []
        for file in files:
            file_path = os.path.join(UPLOAD_DIR, file.filename)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            saved_files.append(file.filename)

        return {"message": f"Uploaded {len(saved_files)} images.", "files": saved_files}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/process-ocr")
def process_ocr():
    """Run OCR on uploaded images in batches of 10."""
    try:
        files = os.listdir(UPLOAD_DIR)
        results = []

        for i in range(0, len(files), 10):
            batch = files[i:i + 10]
            batch_results = []

            for file_name in batch:
                file_path = os.path.join(UPLOAD_DIR, file_name)
                result = process_image(file_path, output_dir=PROCESSED_DIR)

                batch_results.append({
                    "original_filename": result.get("original_filename", file_name),
                    "new_filename": result.get("new_filename", file_name),
                    "formatted_name": result.get("formatted_name", "")
                })

            results.extend(batch_results)

            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)

            time.sleep(5)

        return {"message": f"OCR completed for {len(results)} images.", "results": results}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/compare-batch")
def compare_batch():
    """Run comparison on all OCR results."""
    try:
        if not os.path.exists(CACHE_FILE):
            return JSONResponse(status_code=400, content={"error": "No OCR results found. Run OCR first."})

        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            ocr_results = json.load(f)

        products = get_shopify_data()
        all_matches = []

        for result in ocr_results:
            formatted_text = result.get("formatted_name", "")
            image_file = result.get("new_filename") or result.get("original_filename")

            if not formatted_text:
                continue

            matches = compare(formatted_text, products)
            all_matches.append({
                "image": image_file,
                "matches": matches
            })

        with open(COMPARE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_matches, f, indent=2, ensure_ascii=False)

        return {"message": f"Comparison finished for {len(all_matches)} images.", "results": all_matches}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/upload-to-shopify-batch")
def upload_to_shopify_batch(selections: List[dict]):
    """
    Upload multiple images to Shopify based on user selections.
    Expected format: [{"image": "file.jpg", "selected_name": "Product Name", "gid": "gid://..."}]
    """
    try:
        if not selections:
            return JSONResponse(status_code=400, content={"error": "No selections provided"})

        uploaded_files = []
        upload_errors = []

        for selection in selections:
            image_name = selection.get("image")
            selected_name = selection.get("selected_name")
            gid = selection.get("gid")
            
            if not all([image_name, selected_name, gid]):
                upload_errors.append(f"Invalid selection (missing required fields): {selection}")
                continue
            
            safe_name = selected_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            
            # Look in output/ and processed/ folders
            possible_paths = [
                os.path.join(PROCESSED_DIR, "output", f"{safe_name}.jpg"),
                os.path.join(PROCESSED_DIR, "upload", f"{safe_name}.jpg"),
                os.path.join(PROCESSED_DIR, image_name)
            ]
            
            image_path = None
            for path in possible_paths:
                if os.path.exists(path):
                    image_path = path
                    break
            
            if not image_path:
                upload_errors.append(f"File not found for {image_name}")
                continue

            try:
                print(f"Uploading {image_name} to Shopify with name: {selected_name}")
                result = upload_image_to_shopify(image_path, gid, selected_name)
                
                uploaded_files.append({
                    "original": image_name,
                    "selected_name": selected_name,
                    "gid": gid,
                    "shopify_result": result
                })
                print(f"Successfully uploaded {image_name} to Shopify")
                
            except Exception as upload_error:
                error_msg = f"Failed to upload {image_name} to Shopify: {str(upload_error)}"
                print(error_msg)
                upload_errors.append(error_msg)

        response_data = {
            "message": f"Shopify upload completed. {len(uploaded_files)} files uploaded successfully.",
            "uploaded": uploaded_files
        }
        
        if upload_errors:
            response_data["errors"] = upload_errors
            response_data["error_count"] = len(upload_errors)

        return response_data

    except Exception as e:
        print(f"Upload to Shopify batch error: {str(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/refresh-shopify-cache")
def refresh_shopify_cache():
    """Force refresh Shopify product data and overwrite cache file."""
    try:
        products = get_shopify_data(force_refresh=True)
        if not products:
            return JSONResponse(status_code=500, content={"error": "Failed to refresh Shopify data"})
        
        return {
            "message": f"Refreshed cache with {len(products)} products.",
            "cache_file": "cache/shopify_products.json"
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# -------------------------------
# Root endpoint
# -------------------------------
@app.get("/")
def root():
    return {"message": "Wine OCR + Matching API is running"}


# -------------------------------
# Debug routes
# -------------------------------
router = APIRouter()

@router.get("/debug-drive-structure")
def debug_drive_structure_endpoint():
    """Debug Drive folder structure in production."""
    return debug_drive_structure()


@router.get("/debug-compare-results")
def debug_compare_results():
    """Debug: Show the structure of compare results"""
    try:
        if not os.path.exists(COMPARE_FILE):
            return {"error": "No compare results found. Run /compare-batch first."}
        
        with open(COMPARE_FILE, "r", encoding="utf-8") as f:
            compare_results = json.load(f)
        
        sample_results = compare_results[:2] if len(compare_results) > 2 else compare_results
        
        return {
            "total_results": len(compare_results),
            "sample_results": sample_results,
            "first_match_structure": compare_results[0]["matches"] if compare_results else None
        }
    except Exception as e:
        return {"error": str(e)}


app.include_router(router)