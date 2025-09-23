from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from typing import List
import os
import shutil
import time
import json

from ocr import process_image 
from graphql import get_shopify_data
from compare_products import compare
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



app = FastAPI(title="Wine OCR + Matching API")

# Directories
UPLOAD_DIR = "uploads"
PROCESSED_DIR = "processed"
CACHE_FILE = "ocr_results.json"
COMPARE_FILE = "compare_results.json"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)


@app.get("/")
def root():
    return {"message": "Wine OCR + Matching API is running 🚀"}


@app.post("/upload-images")
async def upload_images(files: List[UploadFile] = File(...)):
    """
    Upload multiple images at once and store them in UPLOAD_DIR.
    """
    try:
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
    """
    Run OCR on uploaded images in batches of 10,
    rename them using the formatted label,
    and store results in CACHE_FILE.
    """
    try:
        files = os.listdir(UPLOAD_DIR)
        results = []

        for i in range(0, len(files), 10):
            batch = files[i:i + 10]
            for file_name in batch:
                file_path = os.path.join(UPLOAD_DIR, file_name)
                result = process_image(file_path, PROCESSED_DIR)
                results.append(result)

            # Add 5s pause between batches to avoid rate limits
            time.sleep(5)

        # Save all OCR results
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        return {"message": f"OCR completed for {len(results)} images.", "results": results}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/compare-batch")
def compare_batch():
    """
    Run BM25 + Gemini on all OCR results (from CACHE_FILE).
    Return top 3 matches per image.
    """
    try:
        if not os.path.exists(CACHE_FILE):
            return JSONResponse(status_code=400, content={"error": "No OCR results found. Run /process-ocr first."})

        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            ocr_results = json.load(f)

        products = get_shopify_data()
        all_matches = []

        for result in ocr_results:
            formatted_text = result["formatted_name"]
            matches = compare(formatted_text, products)  # BM25 + Gemini
            all_matches.append({
                "image": result["new_filename"],
                "matches": matches
            })

        
        with open(COMPARE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_matches, f, indent=2, ensure_ascii=False)

        return {"message": f"Comparison finished for {len(all_matches)} images.", "results": all_matches}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
