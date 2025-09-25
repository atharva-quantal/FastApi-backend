import os
import requests
import base64
import shutil


def upload_image_to_shopify(image_path: str, gid: str, final_name: str):
    """
    Rename locally with final_name and upload to Shopify product,
    replacing all existing ones.
    """
    shop_handle = os.getenv("SHOP_NAME")
    access_token = os.getenv("ACCESS_TOKEN")
    api_version = os.getenv("API_VERSION", "2023-10")  # default to latest stable

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
    get_resp = requests.get(images_url, headers=headers)
    get_resp.raise_for_status()
    existing_images = get_resp.json().get("images", [])

    for img in existing_images:
        image_id = img["id"]
        delete_url = f"{base_url}/images/{image_id}.json"
        requests.delete(delete_url, headers=headers).raise_for_status()

    # Step 2: Build safe filename (no local overwrite)
    ext = os.path.splitext(image_path)[1] or ".jpg"
    safe_name = final_name.replace(" ", "_").replace("/", "-")
    renamed_path = os.path.join(os.path.dirname(image_path), f"{safe_name}{ext}")

    # Create a temporary renamed copy
    if image_path != renamed_path:
        shutil.copy(image_path, renamed_path)

    # Step 3: Upload new image
    with open(renamed_path, "rb") as img_file:
        encoded = base64.b64encode(img_file.read()).decode("utf-8")

    payload = {
        "image": {
            "attachment": encoded,
            "filename": os.path.basename(renamed_path),
            "position": 1
        }
    }

    resp = requests.post(images_url, json=payload, headers=headers)
    resp.raise_for_status()

    return resp.json().get("image")
