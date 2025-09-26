from fastapi import FastAPI, UploadFile, File, Query, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
import os
import shutil
import time
import json
import tempfile

from drive_utils import (
    USER_TOKENS, USER_DRIVE_STRUCTURES, init_drive, oauth_callback,
    upload_file_to_drive, is_drive_ready, debug_drive_structure,
    get_user_id_from_credentials
)

from ocr import process_image
from graphql import get_shopify_data
from compare_products import compare
from shopify_upload import upload_image_to_shopify

app = FastAPI(title="Wine OCR + Matching API")

# -------------------------------
# CORS setup
# -------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://david-f-frontend.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------
# Constants (cache only, no staging dirs)
# -------------------------------
CACHE_FILE = "ocr_results.json"
COMPARE_FILE = "compare_results.json"


# -------------------------------
# Drive Endpoints
# -------------------------------
@app.post("/init-drive")
def init_drive_endpoint():
    try:
        return init_drive()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/drive-callback")
def drive_callback_endpoint(code: str = Query(...), state: str = Query(...)):
    try:
        return oauth_callback(code, state)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/drive-status")
def drive_status_endpoint(user_id: Optional[str] = Query(None)):
    try:
        if not user_id:
            return JSONResponse(status_code=400, content={"error": "user_id required"})
        status = is_drive_ready(user_id)
        return {**status, "debug": debug_drive_structure()}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/finalize-upload")
def finalize_upload_endpoint(
    selections: List[dict], 
    user_id: Optional[str] = Query(None)
):
    """
    Upload user-selected files directly to Drive.
    Expected format:
    [
      {
        "image": "original.jpg",
        "selected_name": "Renamed Wine",
        "target": "output" | "nhr",
        "nhr_reason": "search_failed" | "ocr_failed" | "manual_rejection" | "others"
      }
    ]
    """
    try:
        if not user_id:
            return JSONResponse(status_code=400, content={"error": "user_id required"})

        if not is_drive_ready(user_id).get("linked"):
            return JSONResponse(status_code=400, content={"error": "Drive not connected"})

        if not os.path.exists(COMPARE_FILE):
            return JSONResponse(status_code=400, content={"error": "No compare results found"})

        with open(COMPARE_FILE, "r", encoding="utf-8") as f:
            compare_results = json.load(f)

        results_lookup = {r["image"]: r for r in compare_results}

        upload_results = []

        for selection in selections:
            image_name = selection.get("image")
            selected_name = selection.get("selected_name")
            target = selection.get("target", "output")
            nhr_reason = selection.get("nhr_reason")

            if not (image_name and selected_name):
                continue

            # Locate file from OCR step (temporary /tmp storage)
            temp_path = os.path.join(tempfile.gettempdir(), image_name)
            if not os.path.exists(temp_path):
                upload_results.append({"error": f"File {image_name} not found"})
                continue

            # Clean rename
            final_name = f"{selected_name.replace(' ', '_').replace('/', '_').replace('\\', '_')}.jpg"

            # Upload logic
            if target == "nhr" and nhr_reason:
                target_folders = [f"nhr/{nhr_reason}"]
            else:
                target_folders = ["input", "output", "upload"]

            for folder in target_folders:
                try:
                    result = upload_file_to_drive(
                        user_id=user_id,
                        local_path=temp_path,
                        final_name=final_name if folder != "input" else image_name,
                        target_folder=folder
                    )
                    upload_results.append({
                        "image": image_name,
                        "folder": folder,
                        "drive_result": result
                    })
                except Exception as e:
                    upload_results.append({
                        "image": image_name,
                        "folder": folder,
                        "error": str(e)
                    })

            # Cleanup temp
            try:
                os.remove(temp_path)
            except:
                pass

        return {
            "message": f"Processed {len(selections)} selections",
            "results": upload_results,
            "user_id": user_id
        }

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return JSONResponse(status_code=500, content={"error": str(e)})


# -------------------------------
# Upload & OCR
# -------------------------------
@app.post("/upload-images")
async def upload_images(files: List[UploadFile] = File(...)):
    """Upload multiple images temporarily for OCR."""
    try:
        saved_files = []
        for file in files:
            temp_path = os.path.join(tempfile.gettempdir(), file.filename)
            with open(temp_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            saved_files.append(file.filename)
        return {"message": f"Uploaded {len(saved_files)} images.", "files": saved_files}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/process-ocr")
def process_ocr():
    """Run OCR on uploaded images stored in /tmp."""
    try:
        files = [f for f in os.listdir(tempfile.gettempdir()) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
        results = []

        for i in range(0, len(files), 10):
            batch = files[i:i+10]
            batch_results = []
            for file_name in batch:
                file_path = os.path.join(tempfile.gettempdir(), file_name)
                result = process_image(file_path, output_dir=tempfile.gettempdir())
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
    """Run comparison on OCR results."""
    try:
        if not os.path.exists(CACHE_FILE):
            return JSONResponse(status_code=400, content={"error": "No OCR results"})

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
            all_matches.append({"image": image_file, "matches": matches})

        with open(COMPARE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_matches, f, indent=2, ensure_ascii=False)

        return {"message": f"Comparison finished for {len(all_matches)} images.", "results": all_matches}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# -------------------------------
# Shopify Upload
# -------------------------------
@app.post("/upload-to-shopify-batch")
def upload_to_shopify_batch(selections: List[dict]):
    """
    Upload images to Shopify.
    [
      {"image": "file.jpg", "selected_name": "Product", "gid": "gid://..."}
    ]
    """
    try:
        if not selections:
            return JSONResponse(status_code=400, content={"error": "No selections"})

        uploaded_files, errors = [], []

        for selection in selections:
            image_name = selection.get("image")
            selected_name = selection.get("selected_name")
            gid = selection.get("gid")

            if not all([image_name, selected_name, gid]):
                errors.append(f"Invalid selection: {selection}")
                continue

            # Locate temp file
            possible_paths = [
                os.path.join(tempfile.gettempdir(), f"{selected_name.replace(' ', '_')}.jpg"),
                os.path.join(tempfile.gettempdir(), image_name)
            ]
            image_path = next((p for p in possible_paths if os.path.exists(p)), None)
            if not image_path:
                errors.append(f"File not found for {image_name}")
                continue

            try:
                result = upload_image_to_shopify(image_path, gid, selected_name)
                uploaded_files.append({
                    "original": image_name,
                    "selected_name": selected_name,
                    "gid": gid,
                    "shopify_result": result
                })
            except Exception as err:
                errors.append(f"Failed {image_name}: {str(err)}")

        response = {
            "message": f"Shopify upload complete. {len(uploaded_files)} succeeded.",
            "uploaded": uploaded_files
        }
        if errors:
            response["errors"] = errors
        return response
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# -------------------------------
# Debug Routes
# -------------------------------
@app.get("/debug-drive-structure")
def debug_drive_structure_endpoint():
    return debug_drive_structure()
