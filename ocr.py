import os
import io
import time
from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai
from prompt import prompts
from fastapi import UploadFile
from typing import List, Dict, Union

load_dotenv()

API_KEY = os.getenv("GEMINI_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_KEY not found in environment variables.")
genai.configure(api_key=API_KEY)


def _load_image(file_or_image) -> Image.Image:
    """
    Normalize input into a PIL Image.
    Accepts UploadFile, path (str), bytes, or PIL.Image.Image.
    """
    if isinstance(file_or_image, UploadFile):
        return Image.open(io.BytesIO(file_or_image.file.read()))

    if isinstance(file_or_image, str):  # path
        return Image.open(file_or_image)

    if isinstance(file_or_image, bytes):
        return Image.open(io.BytesIO(file_or_image))

    if isinstance(file_or_image, Image.Image):
        return file_or_image

    raise ValueError("Unsupported input type for OCR.")


def _extract_text(image: Image.Image) -> str:
    """Extract raw text from an image using Gemini."""
    try:
        model = genai.GenerativeModel(model_name="gemini-2.0-flash")
        response = model.generate_content([image, prompts["text_prompt"]])
        return response.text.strip() if response and response.text else ""
    except Exception as e:
        return f"[OCR Error: {str(e)}]"


def _categorize_text(ocr_text: str, original_filename: str = None) -> str:
    """Categorize OCR text into a structured format (clean wine label string)."""
    try:
        model = genai.GenerativeModel(model_name="gemini-2.0-flash")

        if ocr_text.strip() == "No valid label found.":
            if original_filename:
                base, ext = os.path.splitext(original_filename)
                return f"no-label-{base}{ext}"
            return "no-label"

        response = model.generate_content([
            f"{prompts['format_prompt']} Below is the text extracted from the bottle:\n{ocr_text}"
        ])
        return response.text.strip() if response and response.text else ""
    except Exception as e:
        return f"[Categorization Error: {str(e)}]"


def process_image(file_or_image, original_filename: str, output_dir: str = "processed") -> Dict[str, str]:
    """
    High-level function:
      - Runs OCR
      - Categorizes/cleans text
      - Renames & saves image
    Returns dict with {original_filename, new_filename, formatted_name}
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        
        image = _load_image(file_or_image)

        
        ocr_text = _extract_text(image)

        
        formatted_text = _categorize_text(ocr_text, original_filename)

        
        base_name = os.path.splitext(original_filename)[0]
        ext = os.path.splitext(original_filename)[1] or ".jpg"
        safe_name = formatted_text.replace(" ", "_").replace("/", "-")
        new_filename = f"{safe_name}{ext}"
        output_path = os.path.join(output_dir, new_filename)
        image.save(output_path)

        return {
            "original_filename": original_filename,
            "new_filename": new_filename,
            "formatted_name": formatted_text
        }

    except Exception as e:
        return {
            "original_filename": original_filename,
            "error": str(e)
        }


def process_images_in_batches(
    files: List[Union[str, UploadFile]],
    batch_size: int = 10,
    delay: int = 5,
    output_dir: str = "processed"
) -> List[Dict[str, str]]:
    """
    Process multiple images in batches.
    Each batch of size N is followed by a delay (to avoid API rate limits).
    """
    results = []
    total = len(files)

    for i in range(0, total, batch_size):
        batch = files[i:i + batch_size]
        for file in batch:
            filename = file.filename if isinstance(file, UploadFile) else os.path.basename(file)
            result = process_image(file, filename, output_dir)
            results.append(result)

        if i + batch_size < total:
            time.sleep(delay)

    return results
