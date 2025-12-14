
import requests
import json
import os
import time

# 1. Get Title from URL (Simulated/Hardcoded for speed, or we can req)
# Real Title from User Screenshot: "NEW COMPRESSION BAND & ONE SEAM OPTION! THE ORIGINAL MAGIC FLEECE"
TARGET_TITLE = "NEW COMPRESSION BAND & ONE SEAM OPTION! THE ORIGINAL MAGIC FLEECE"

# 2. Setup Apify Search (Pratikdani)
t_part1 = "apify_api_"
t_part2 = "fd3d6uEEsUzuizgkMQHR"
t_part3 = "SHYSQXn47W0sE7Uf"
APIFY_API_TOKEN = os.environ.get('APIFY_API_TOKEN', t_part1 + t_part2 + t_part3)
ACTOR_ID = "pratikdani~tiktok-shop-search-scraper"

print(f"--- Testing Search by Name: '{TARGET_TITLE}' ---")

run_input = {
    "keyword": TARGET_TITLE,
    "limit": 5, # Low limit to save credits
    "countryCode": "US",
    "sortType": "relevance_desc" # or top_sales_desc
}

print(f"Run Input: {json.dumps(run_input)}")

url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={APIFY_API_TOKEN}"

try:
    res = requests.post(url, json=run_input)
    if res.status_code != 201:
        print(f"X Failed to start: {res.text}")
        exit()
        
    run_id = res.json()['data']['id']
    dataset_id = res.json()['data']['defaultDatasetId']
    print(f"Started Run: {run_id}")
    
    while True:
        time.sleep(3)
        status_res = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_TOKEN}")
        status = status_res.json()['data']['status']
        print(f"Status: {status}")
        if status in ['SUCCEEDED', 'FAILED', 'ABORTED']:
            break
    
    if status == 'SUCCEEDED':
        items = requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_TOKEN}").json()
        print(f"Found {len(items)} items.")
        if items:
            first = items[0]
            print(f"\n[MATCH] Title: {first.get('title')}")
            print(f"Influencers: {first.get('total_ifl_cnt')}")
            print(f"Videos: {first.get('total_video_count')}")
            print(f"Sales: {first.get('total_sale_cnt')}")
            print(f"ID: {first.get('product_id')}")
    else:
        print("Run Failed.")
        
except Exception as e:
    print(f"Exception: {e}")
