from fastapi import FastAPI, UploadFile, File, Query
from fastapi.responses import JSONResponse
from typing import List
import os
import shutil
import time
import json
from fastapi import FastAPI, APIRouter
from drive_utils import USER_TOKENS, DRIVE_STRUCTURE

from ocr import process_image
from graphql import get_shopify_data
from compare_products import compare
from shopify_upload import upload_image_to_shopify
from fastapi.middleware.cors import CORSMiddleware
from drive_utils import init_drive, oauth_callback, upload_to_drive, is_drive_ready

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
# 🚀 Google Drive Endpoints
# -------------------------------
@app.post("/init-drive")
def init_drive_endpoint():
    """Start Google Drive sign-in and return OAuth URL."""
    try:
        return init_drive()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/drive-callback")
def drive_callback_endpoint(code: str = Query(...), state: str = Query(...)):
    """Handle Google Drive OAuth callback."""
    try:
        return oauth_callback(code, state)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/drive-status")
def drive_status_endpoint():
    """Check if Drive is linked and ready."""
    try:
        return is_drive_ready()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/upload-drive")
def upload_drive_endpoint():
    """
    Upload processed & renamed images (after compare step) to Google Drive.
    """
    try:
        if not os.path.exists(COMPARE_FILE):
            return JSONResponse(status_code=400, content={"error": "No compare results found. Run /compare-batch first."})

        with open(COMPARE_FILE, "r", encoding="utf-8") as f:
            compare_results = json.load(f)

        files_to_upload = []
        for result in compare_results:
            image_name = result.get("image")
            matches = result.get("matches", [])

            if not matches:
                continue

            # ✅ Take best match (index 0) as final name
            final_name = matches[0]["name"].replace(" ", "_")

            processed_path = os.path.join(PROCESSED_DIR, image_name)
            if os.path.exists(processed_path):
                new_filename = f"{final_name}.jpg"
                new_path = os.path.join(PROCESSED_DIR, new_filename)

                # rename locally before upload
                os.rename(processed_path, new_path)

                files_to_upload.append(new_path)

        if not files_to_upload:
            return {"message": "No images to upload (no matches found)"}

        drive_response = upload_to_drive(files_to_upload)
        return {"message": "Uploaded images to Drive", "files": drive_response}

    except Exception as e:
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
def debug_drive_structure():
    """TEMP: Debug Drive folder structure in production."""
    return {
        "user_tokens": list(USER_TOKENS.keys()),
        "drive_structure": DRIVE_STRUCTURE,
    }

# Mount router
app.include_router(router)