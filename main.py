from fastapi import FastAPI, UploadFile, File, Query, Header
from fastapi.responses import JSONResponse
from typing import List, Optional
import os
import shutil
import time
import json
from fastapi import FastAPI, APIRouter
from drive_utils import USER_TOKENS, USER_DRIVE_STRUCTURES

from ocr import process_image
from graphql import get_shopify_data
from compare_products import compare
from shopify_upload import upload_image_to_shopify
from fastapi.middleware.cors import CORSMiddleware
from drive_utils import (
    init_drive, oauth_callback, upload_to_drive, upload_single_file_to_drive, 
    is_drive_ready, debug_drive_structure, get_user_id_from_credentials
)

app = FastAPI(title="Wine OCR + Matching API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://david-f-frontend.vercel.app",  # add prod frontend when deployed
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
# Helper function to get user_id from headers
# -------------------------------
def get_user_id_from_headers(x_user_id: Optional[str] = Header(None)) -> Optional[str]:
    """Extract user ID from headers."""
    return x_user_id


# -------------------------------
# 🚀 Google Drive Endpoints
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
                # Old format - convert to new format
                candidates = matches_data
                original_text = ""
                validated_gid = ""
                need_human_review = False
            else:
                # New format
                candidates = matches_data.get("candidates", [])
                original_text = matches_data.get("orig", "")
                validated_gid = matches_data.get("validated_gid", "")
                need_human_review = matches_data.get("need_human_review", False)
            
            if not candidates:
                continue
            
            # Extract just the names and scores for frontend
            options = []
            for candidate in candidates:
                # Handle both old format (name) and new format (text)
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


@app.post("/upload-drive")
def upload_drive_endpoint(user_id: Optional[str] = Query(None)):
    """
    Upload processed & renamed images (after compare step) to Google Drive for a specific user.
    Uses the validated_gid or first candidate by default.
    """
    try:
        if not user_id:
            return JSONResponse(
                status_code=400,
                content={"error": "user_id parameter is required"}
            )
        
        # Check if Drive is ready for this user
        drive_status = is_drive_ready(user_id)
        if not drive_status.get("linked", False):
            return JSONResponse(
                status_code=400,
                content={"error": f"Google Drive not connected for user {user_id} or credentials expired. Please run /init-drive first."}
            )
        
        if not os.path.exists(COMPARE_FILE):
            return JSONResponse(
                status_code=400,
                content={"error": "No compare results found. Run /compare-batch first."}
            )

        with open(COMPARE_FILE, "r", encoding="utf-8") as f:
            compare_results = json.load(f)

        files_to_upload = []
        upload_errors = []
        
        for result in compare_results:
            image_name = result.get("image")
            matches_data = result.get("matches", {})

            # Handle both old and new formats
            if isinstance(matches_data, list):
                # Old format
                candidates = matches_data
                validated_gid = None
            else:
                # New format
                candidates = matches_data.get("candidates", [])
                validated_gid = matches_data.get("validated_gid")

            if not candidates:
                print(f"Skipping {image_name} - no candidates found")
                continue

            # Use the validated_gid if available, otherwise take the first candidate
            selected_candidate = None
            
            if validated_gid:
                # Find the candidate with the validated GID
                for candidate in candidates:
                    if candidate.get("gid") == validated_gid:
                        selected_candidate = candidate
                        break
            
            # If no validated candidate found, use the first (highest scored) candidate
            if not selected_candidate and candidates:
                selected_candidate = candidates[0]
            
            if not selected_candidate:
                print(f"Skipping {image_name} - no valid candidate found")
                continue

            # Extract the product name from the candidate
            product_name = selected_candidate.get("text") or selected_candidate.get("name", "")
            if not product_name:
                print(f"Warning: No name found in candidate: {selected_candidate}")
                # Use GID as fallback
                gid = selected_candidate.get("gid", "")
                if gid:
                    product_name = f"product_{gid.split('/')[-1]}"
                else:
                    product_name = f"unknown_product_{image_name}"
            
            final_name = product_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            print(f"Selected product: {product_name} (GID: {selected_candidate.get('gid')})")
            
            processed_path = os.path.join(PROCESSED_DIR, image_name)
            if not os.path.exists(processed_path):
                upload_errors.append(f"File not found: {processed_path}")
                continue

            try:
                new_filename = f"{final_name}.jpg"
                new_path = os.path.join(PROCESSED_DIR, new_filename)

                # rename locally before upload (if not already renamed)
                if processed_path != new_path:
                    if os.path.exists(new_path):
                        os.remove(new_path)  # Remove if exists
                    os.rename(processed_path, new_path)

                with open(new_path, "rb") as f:
                    file_bytes = f.read()

                print(f"Uploading {new_filename} to Drive for user {user_id}...")
                file_id = upload_single_file_to_drive(user_id, file_bytes, new_filename, target="output")
                
                files_to_upload.append({
                    "local": new_filename, 
                    "drive_id": file_id,
                    "original": image_name,
                    "final_name": final_name,
                    "selected_gid": selected_candidate.get("gid"),
                    "product_name": product_name
                })
                print(f"Successfully uploaded {new_filename} with ID: {file_id}")
                
            except Exception as upload_error:
                error_msg = f"Failed to upload {image_name}: {str(upload_error)}"
                print(error_msg)
                upload_errors.append(error_msg)

        response_data = {
            "message": f"Upload process completed for user {user_id}. {len(files_to_upload)} files uploaded successfully.",
            "files": files_to_upload,
            "user_id": user_id
        }
        
        if upload_errors:
            response_data["errors"] = upload_errors
            response_data["error_count"] = len(upload_errors)

        if not files_to_upload and not upload_errors:
            return {"message": "No images to upload (no candidates found)", "user_id": user_id}
        
        # Return success even if some files failed (partial success)
        return response_data

    except Exception as e:
        print(f"Upload drive endpoint error: {str(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/upload-drive-selected")
def upload_drive_selected_endpoint(selections: List[dict], user_id: Optional[str] = Query(None)):
    """
    Upload images with user-selected names and target folders to Google Drive for a specific user.
    Expected format: [{"image": "file.jpg", "selected_name": "Product Name", "target": "output", "nhr_reason": "search_failed"}]
    """
    try:
        if not user_id:
            return JSONResponse(
                status_code=400,
                content={"error": "user_id parameter is required"}
            )
        
        # Check if Drive is ready for this user
        drive_status = is_drive_ready(user_id)
        if not drive_status.get("linked", False):
            return JSONResponse(
                status_code=400,
                content={"error": f"Google Drive not connected for user {user_id} or credentials expired. Please run /init-drive first."}
            )
        
        if not os.path.exists(COMPARE_FILE):
            return JSONResponse(
                status_code=400,
                content={"error": "No compare results found. Run /compare-batch first."}
            )

        with open(COMPARE_FILE, "r", encoding="utf-8") as f:
            compare_results = json.load(f)

        # Create a lookup for quick access
        results_lookup = {result["image"]: result for result in compare_results}
        
        files_to_upload = []
        upload_errors = []
        
        for selection in selections:
            image_name = selection.get("image")
            selected_name = selection.get("selected_name")
            target = selection.get("target", "output")  # default to output
            nhr_reason = selection.get("nhr_reason")  # for NHR uploads
            
            if not image_name or not selected_name:
                upload_errors.append(f"Invalid selection: {selection}")
                continue
                
            if image_name not in results_lookup:
                upload_errors.append(f"Image {image_name} not found in compare results")
                continue
            
            # Determine target folder
            if target == "nhr" and nhr_reason:
                target_folder = f"nhr.{nhr_reason}"
            else:
                target_folder = target
            
            # Clean the selected name for filename
            final_name = selected_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            
            processed_path = os.path.join(PROCESSED_DIR, image_name)
            if not os.path.exists(processed_path):
                upload_errors.append(f"File not found: {processed_path}")
                continue

            try:
                new_filename = f"{final_name}.jpg"
                new_path = os.path.join(PROCESSED_DIR, new_filename)

                # rename locally before upload (if not already renamed)
                if processed_path != new_path:
                    if os.path.exists(new_path):
                        os.remove(new_path)  # Remove if exists
                    os.rename(processed_path, new_path)

                with open(new_path, "rb") as f:
                    file_bytes = f.read()

                print(f"Uploading user-selected {new_filename} to Drive folder: {target_folder} for user {user_id}")
                file_id = upload_single_file_to_drive(user_id, file_bytes, new_filename, target=target_folder)
                
                files_to_upload.append({
                    "local": new_filename, 
                    "drive_id": file_id,
                    "original": image_name,
                    "final_name": final_name,
                    "selected_name": selected_name,
                    "target": target_folder
                })
                print(f"Successfully uploaded {new_filename} with ID: {file_id}")
                
            except Exception as upload_error:
                error_msg = f"Failed to upload {image_name}: {str(upload_error)}"
                print(error_msg)
                upload_errors.append(error_msg)

        response_data = {
            "message": f"Upload process completed for user {user_id}. {len(files_to_upload)} files uploaded successfully.",
            "files": files_to_upload,
            "user_id": user_id
        }
        
        if upload_errors:
            response_data["errors"] = upload_errors
            response_data["error_count"] = len(upload_errors)

        return response_data

    except Exception as e:
        print(f"Upload drive selected endpoint error: {str(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": str(e)})


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


def clear_file(path: str):
    """Delete a file if it exists."""
    if os.path.exists(path):
        os.remove(path)


@app.on_event("startup")
def startup_event():
    """Ensure clean state when backend starts."""
    clear_folder(UPLOAD_DIR)
    clear_folder(PROCESSED_DIR)
    clear_file(CACHE_FILE)
    clear_file(COMPARE_FILE)
    print("✅ Cleared uploads/, processed/, and cache files at startup")


# -------------------------------
# Root
# -------------------------------
@app.get("/")
def root():
    return {"message": "Wine OCR + Matching API is running 🚀"}


# -------------------------------
# Upload images
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


# -------------------------------
# Process OCR
# -------------------------------
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

                # ✅ Ensure required keys exist
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


# -------------------------------
# Compare batch
# -------------------------------
@app.post("/compare-batch")
def compare_batch():
    """Run BM25 + Gemini on all OCR results (from CACHE_FILE)."""
    try:
        if not os.path.exists(CACHE_FILE):
            return JSONResponse(status_code=400, content={"error": "No OCR results found. Run /process-ocr first."})

        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            ocr_results = json.load(f)

        products = get_shopify_data()
        all_matches = []

        for result in ocr_results:
            formatted_text = result.get("formatted_name", "")
            image_file = result.get("new_filename") or result.get("original_filename")

            if not formatted_text:
                continue  # skip empty OCR results

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


# -------------------------------
# Upload to Shopify
# -------------------------------
@app.post("/upload-to-shopify")
def upload_to_shopify_api(gid: str, filename: str, final_name: str):
    """Upload a processed image to Shopify by product GID."""
    try:
        image_path = os.path.join(PROCESSED_DIR, filename)
        if not os.path.exists(image_path):
            return JSONResponse(status_code=404, content={"error": f"File {filename} not found in processed dir"})

        result = upload_image_to_shopify(image_path, gid, final_name)
        return {"message": "Image uploaded successfully", "image": result}

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

            # Clean the selected name for filename
            final_name = selected_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            
            # Look for the renamed file first, then original
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


# -------------------------------
# Refresh Shopify Cache
# -------------------------------
@app.post("/refresh-shopify-cache")
def refresh_shopify_cache():
    """
    Force refresh Shopify product data and overwrite cache file.
    """
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