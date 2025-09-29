import os
import requests
import base64
import shutil


def upload_image_to_shopify(image_path: str, gid: str, final_name: str):
    """
    Upload image to Shopify product, replacing all existing images.
    Uses the final_name with spaces preserved for the filename.
    """
    shop_handle = os.getenv("SHOP_NAME")
    access_token = os.getenv("ACCESS_TOKEN")
    api_version = os.getenv("API_VERSION", "2023-10")

    # Extract numeric product ID from gid
    try:
        product_id = gid.rsplit("/", 1)[1]
    except IndexError:
        raise ValueError(f"Invalid GID format: {gid}")

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token
    }

    base_url = f"https://{shop_handle}.myshopify.com/admin/api/{api_version}/products/{product_id}"
    images_url = f"{base_url}/images.json"

    # Step 1: Delete existing images
    try:
        get_resp = requests.get(images_url, headers=headers)
        get_resp.raise_for_status()
        existing_images = get_resp.json().get("images", [])

        for img in existing_images:
            image_id = img["id"]
            delete_url = f"{base_url}/images/{image_id}.json"
            delete_resp = requests.delete(delete_url, headers=headers)
            delete_resp.raise_for_status()
            print(f"Deleted existing image: {image_id}")
    except Exception as e:
        print(f"Warning: Could not delete existing images: {e}")

    # Step 2: Sanitize filename (keep spaces, remove only illegal chars)
    # Remove illegal filename characters but keep spaces
    safe_name = final_name.replace("/", "-").replace("\\", "-")
    safe_name = safe_name.replace(":", "").replace("*", "").replace("?", "")
    safe_name = safe_name.replace('"', "").replace("<", "").replace(">", "").replace("|", "")
    
    ext = os.path.splitext(image_path)[1] or ".jpg"
    shopify_filename = f"{safe_name}{ext}"

    # Step 3: Upload new image
    with open(image_path, "rb") as img_file:
        encoded = base64.b64encode(img_file.read()).decode("utf-8")

    payload = {
        "image": {
            "attachment": encoded,
            "filename": shopify_filename,
            "position": 1
        }
    }

    print(f"Uploading to Shopify as: {shopify_filename}")
    resp = requests.post(images_url, json=payload, headers=headers)
    resp.raise_for_status()

    uploaded_image = resp.json().get("image")
    print(f"Successfully uploaded image ID: {uploaded_image.get('id')}")
    
    return uploaded_image