from fastapi import FastAPI, UploadFile, File, Query, Header
from fastapi.responses import JSONResponse
from typing import List, Optional
import os
import shutil
import time
import json
from fastapi import FastAPI, APIRouter
from drive_utils import (
    USER_TOKENS, USER_DRIVE_STRUCTURES, init_drive, oauth_callback,
    upload_to_drive, is_drive_ready, debug_drive_structure,
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

# Ensure all required folders exist
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
# Helper function to get user_id from headers
# -------------------------------
def get_user_id_from_headers(x_user_id: Optional[str] = Header(None)) -> Optional[str]:
    """Extract user ID from headers."""
    return x_user_id


# -------------------------------
# Drive Endpoints
# -------------------------------
@app.post("/init-drive")
def init_drive_endpoint():
    """Start Google Drive sign-in and return OAuth URL."""
    try:
        return init_drive()
    except Exception as e:
        print(f"Init drive error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/drive-callback")
def drive_callback_endpoint(code: str = Query(...), state: str = Query(...)):
    """Handle Google Drive OAuth callback."""
    try:
        return oauth_callback(code, state)
    except Exception as e:
        print(f"Drive callback error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/drive-status")
def drive_status_endpoint(user_id: Optional[str] = Query(None)):
    """Check if Drive is linked and ready for a specific user."""
    try:
        if not user_id:
            return JSONResponse(
                status_code=400,
                content={"error": "user_id parameter is required"}
            )
        
        status = is_drive_ready(user_id)
        debug_info = debug_drive_structure()
        return {**status, "debug": debug_info}
    except Exception as e:
        print(f"Drive status error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/upload-drive-selected")
def upload_drive_selected_endpoint(selections: List[dict], user_id: Optional[str] = Query(None)):
    """
    Upload images with user-selected names and target folders to Google Drive.
    Expected format: [{"image": "file.jpg", "selected_name": "Product Name", "target": "output", "nhr_reason": "search_failed"}]
    """
    try:
        if not user_id:
            return JSONResponse(
                status_code=400,
                content={"error": "user_id parameter is required"}
            )
        
        # Check Drive connection
        drive_status = is_drive_ready(user_id)
        if not drive_status.get("linked", False):
            return JSONResponse(
                status_code=400,
                content={"error": f"Drive not connected for user {user_id}. Please connect Drive first."}
            )
        
        # Load compare results for file info
        if not os.path.exists(COMPARE_FILE):
            return JSONResponse(
                status_code=400,
                content={"error": "No compare results found. Run comparison first."}
            )

        with open(COMPARE_FILE, "r", encoding="utf-8") as f:
            compare_results = json.load(f)
        
        results_lookup = {result["image"]: result for result in compare_results}
        
        # Track which files go to which folders
        files_by_folder = {
            "input": [],
            "output": [],
            "upload": [],
            "nhr/search_failed": [],
            "nhr/ocr_failed": [],
            "nhr/manual_rejection": [],
            "nhr/others": []
        }
        
        # Process each selection and move files to appropriate folders
        for selection in selections:
            image_name = selection.get("image")
            selected_name = selection.get("selected_name")
            target = selection.get("target", "output")
            nhr_reason = selection.get("nhr_reason")
            
            if not all([image_name, selected_name]):
                continue
            
            # Clean filename
            final_name = f"{selected_name.replace(' ', '_').replace('/', '_').replace('\\', '_')}.jpg"
            
            # Determine target folders based on selection
            if target == "nhr" and nhr_reason:
                target_folders = [f"nhr/{nhr_reason}"]
            else:
                target_folders = ["output", "upload"]  # Valid matches go to both
            
            # Move file to appropriate folders
            source_path = os.path.join(PROCESSED_DIR, image_name)
            if os.path.exists(source_path):
                try:
                    moved_paths = move_file_to_folders(
                        source_path,
                        final_name,
                        target_folders,
                        PROCESSED_DIR
                    )
                    
                    # Track files by folder for upload
                    for folder in target_folders:
                        files_by_folder[folder].append({
                            "original": image_name,
                            "final_name": final_name,
                            "path": moved_paths[target_folders.index(folder)]
                        })
                except Exception as e:
                    print(f"❌ Error moving {image_name}: {e}")
        
        # Upload files from each folder
        upload_folders = [folder for folder, files in files_by_folder.items() if files]
        
        if not upload_folders:
            return {"message": "No files to upload", "user_id": user_id}
        
        print(f"📂 Preparing to upload for user {user_id}. Folders: {upload_folders}")
        for folder, files in files_by_folder.items():
            if files:
                print(f"   └── {folder}: {[f['final_name'] for f in files]}")
        
        # Perform batch upload for all folders
        upload_result = upload_to_drive(
            user_id=user_id,
            local_dir=PROCESSED_DIR,
            target_folders=upload_folders
        )
        
        print(f"✅ Upload finished for user {user_id}")
        
        return {
            "message": f"Upload complete. Processed {len(selections)} selections across {len(upload_folders)} folders.",
            "upload_result": upload_result,
            "user_id": user_id
        }

    except Exception as e:
        print(f"🚨 Upload error: {str(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
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

            time.sleep(5)  # rate limiting

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
            
            # Clean name for filename
            final_name = selected_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            
            # Look for both original and renamed files
            possible_paths = [
                os.path.join(PROCESSED_DIR, f"{final_name}.jpg"),
                os.path.join(PROCESSED_DIR, image_name)
            ]
            
            image_path = None
            for path in possible_paths:
                if os.path.exists(path):
                    image_path = path
                    break
            
            if not image_path:
                upload_errors.append(f"File not found for {image_name} (searched: {possible_paths})")
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



# -----------------------------------------
# Alias endpoint for backward compatibility
# -----------------------------------------
@app.post("/upload-to-drive")
def upload_to_drive_alias(
    selections: List[dict],
    user_id: Optional[str] = Query(None)
):
    """
    Alias for /upload-drive-selected so existing frontend calls keep working.
    """
    return upload_drive_selected_endpoint(selections, user_id)

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
        
        # Return just the first few results to see the structure
        sample_results = compare_results[:2] if len(compare_results) > 2 else compare_results
        
        return {
            "total_results": len(compare_results),
            "sample_results": sample_results,
            "first_match_structure": compare_results[0]["matches"] if compare_results else None
        }
    except Exception as e:
        return {"error": str(e)}

# Mount router
app.include_router(router)