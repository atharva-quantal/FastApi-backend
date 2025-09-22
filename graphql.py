import requests
import os
import json
from dotenv import load_dotenv
from time import time

load_dotenv()

CACHE_FILE = "cache/shopify_products.json"

def get_shopify_data(force_refresh=False):
    # If not forcing a refresh, try to load from the local cache first
    if not force_refresh and os.path.exists(CACHE_FILE):
        print("Loading Shopify data from local cache...")
        try:
            with open(CACHE_FILE, 'r') as f:
                product_dict = json.load(f)
            print(f"Loaded {len(product_dict)} products from cache.")
            return product_dict
        except (json.JSONDecodeError, IOError) as e:
            print(f"Cache file is corrupted or unreadable ({e}), fetching from API.")

    print("Fetching Shopify data from API...")
    # Credentials for store
    shop_handle = os.getenv("SHOP_NAME")
    access_token = os.getenv("ACCESS_TOKEN")
    api_version  = os.getenv("API_VERSION")

    url = f"https://{shop_handle}.myshopify.com/admin/api/{api_version}/graphql.json"

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token
    }

    product_dict = {}
    has_next_page = True
    after_cursor = None
    page = 1

    while has_next_page:
        print(f"Fetching page {page}...")

        # GraphQL query with pagination (up to 250 per page)
        query = f"""
        {{
          products(first: 250{f', after: "{after_cursor}"' if after_cursor else ''}) {{
            pageInfo {{
              hasNextPage
            }}
            edges {{
              cursor
              node {{
                id
                title
                descriptionHtml
              }}
            }}
          }}
        }}
        """

        try:
            response = requests.post(url, json={"query": query}, headers=headers)
            response.raise_for_status()
        except requests.HTTPError as err:
            print(f"HTTP error: {err}")
            print(f"→ Check that '{shop_handle}.myshopify.com' is correct and the store is active.")
            return None
        except requests.RequestException as err:
            print(f"Request failed: {err}")
            return None

        data = response.json()

        if "errors" in data:
            print("GraphQL errors:", data["errors"])
            return None

        products_data = data["data"]["products"]
        edges = products_data["edges"]

        for edge in edges:
            node = edge["node"]
            product_dict[node["id"]] = node["title"]

        has_next_page = products_data["pageInfo"]["hasNextPage"]
        if has_next_page:
            after_cursor = edges[-1]["cursor"]
            page += 1

    print(f"Finished fetching {len(product_dict)} products. Caching data locally...")
    
    # Change 2: Ensure the 'cache' directory exists before writing the file
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    
    # Save the fetched data to the cache file
    with open(CACHE_FILE, 'w') as f:
        json.dump(product_dict, f, indent=4)

    return product_dict
