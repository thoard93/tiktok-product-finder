
import os
import requests
import json
import time

# Use the token from environment or hardcoded (same as scanner)
t_part1 = "apify_api_"
t_part2 = "fd3d6uEEsUzuizgkMQHR"
t_part3 = "SHYSQXn47W0sE7Uf"
APIFY_API_TOKEN = os.environ.get('APIFY_API_TOKEN', t_part1 + t_part2 + t_part3)

# Product to Test
TARGET_ID = "1729406693411230691"
TARGET_URL = f"https://shop.tiktok.com/view/product/{TARGET_ID}?region=US&locale=en"

def run_test(actor_id, input_data):
    print(f"\n--- Testing Actor: {actor_id} ---")
    print(f"Input: {json.dumps(input_data)}")
    
    url = f"https://api.apify.com/v2/acts/{actor_id}/runs?token={APIFY_API_TOKEN}"
    try:
        res = requests.post(url, json=input_data)
        if res.status_code != 201:
            print(f"X Failed to start: {res.text}")
            return
            
        run_id = res.json()['data']['id']
        dataset_id = res.json()['data']['defaultDatasetId']
        print(f"Started Run: {run_id}")
        
        while True:
            time.sleep(3)
            status_res = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_TOKEN}")
            status = status_res.json()['data']['status']
            print(f"Status: {status}")
            if status in ['SUCCEEDED', 'FAILED', 'ABORTED', 'TIMED-OUT']:
                break
        
        if status == 'SUCCEEDED':
            items = requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_TOKEN}").json()
            print(f"Found {len(items)} items.")
            if items:
                print(f"Sample Item Keys: {list(items[0].keys())}")
        else:
            print("Run Failed.")
            
    except Exception as e:
        print(f"Exception: {e}")

# Test 1: Barrierefix with productUrls
run_test("barrierefix~tiktok-shop-scraper", {
    "productUrls": [TARGET_URL],
    "maxItems": 1
})

# Test 2: Barrierefix with startUrls (common alternative)
run_test("barrierefix~tiktok-shop-scraper", {
    "startUrls": [{"url": TARGET_URL}],
    "maxItems": 1
})

# Test 3: Novi with keyword (Retry)
run_test("novi~tiktok-shop-scraper", {
    "keyword": TARGET_ID,
    "limit": 1
})
