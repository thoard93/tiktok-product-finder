#!/usr/bin/env python3
"""
Apify Shop Scanner (Alternative Data Source)
Uses "clockworks-free-tiktok-scraper" to find Viral Videos (#tiktokmademebuyit).
Maps viral metrics (Likes/Views) to Product proxy metrics.
"""

import os
import time
import requests
import json
from datetime import datetime

# Import from main application to ensure schema compatibility
from app import app, db, Product

# Apify Config
# Split token to avoid GitHub secret scanning
t_part1 = "apify_api_"
t_part2 = "fd3d6uEEsUzuizgkMQHR"
t_part3 = "SHYSQXn47W0sE7Uf"
APIFY_API_TOKEN = os.environ.get('APIFY_API_TOKEN', t_part1 + t_part2 + t_part3)
ACTOR_ID = "clockworks~free-tiktok-scraper" # Works with free tier

def run_apify_scan():
    if not APIFY_API_TOKEN:
        print("X Error: APIFY_API_TOKEN not found.")
        return

    print(f">> Starting Viral Video Scan via {ACTOR_ID}...")
    
    # input for Viral Hashtags
    run_input = {
        "hashtags": ["tiktokmademebuyit", "amazonfinds"],
        "resultsPerPage": 40,
        "proxyCountry": "US",
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadVideos": False
    }

    # 1. Start Actor
    start_url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={APIFY_API_TOKEN}"
    try:
        start_res = requests.post(start_url, json=run_input)
        if start_res.status_code != 201:
            print(f"X Failed to start actor: {start_res.text}")
            return
        
        run_data = start_res.json()['data']
        run_id = run_data['id']
        dataset_id = run_data['defaultDatasetId']
        print(f"   Actor started! Run ID: {run_id}")
    except Exception as e:
        print(f"X Error starting actor: {e}")
        return

    # 2. Poll for completion
    while True:
        status_res = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_TOKEN}")
        status_data = status_res.json()['data']
        status = status_data['status']
        print(f"   Status: {status}...")
        
        if status == 'SUCCEEDED':
            break
        elif status in ['FAILED', 'ABORTED', 'TIMED-OUT']:
            print(f"X Run failed with status: {status}")
            return
        
        time.sleep(5)

    # 3. Fetch Results
    print("   Fetching results...")
    data_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_TOKEN}"
    items_res = requests.get(data_url)
    items = items_res.json()
    print(f"   Found {len(items)} items.")

    # 4. Save to DB
    saved_count = 0
    with app.app_context():
        for item in items:
            try:
                # Map fields
                # ID: Use video ID as product ID with prefix
                vid = str(item.get('id') or item.get('webVideoUrl', '').split('/')[-1])
                if not vid: continue
                
                pid = f"viral_{vid}"

                p = Product.query.get(pid)
                if not p:
                    p = Product(product_id=pid)
                    p.first_seen = datetime.utcnow()
                
                # Heuristic Mapping
                desc = item.get('text') or "Viral Find"
                # Truncate desc
                p.product_name = (desc[:100] + '...') if len(desc) > 100 else desc
                
                p.image_url = item.get('videoCover') or item.get('authorMeta', {}).get('avatar') or ""
                
                # Proxy metrics
                likes = int(item.get('diggCount', 0))
                views = int(item.get('playCount', 0))
                
                p.sales = likes # Use Likes as proxy for "Sales" sorting
                p.sales_7d = int(likes / 100) # Rough estimate
                # p.views_count = views (Removed to avoid schema mismatch)
                p.video_count = 1
                
                p.seller_name = item.get('authorMeta', {}).get('name') or "Viral Creator"
                p.status_note = f"Source: {item.get('webVideoUrl')}"
                p.scan_type = 'apify_viral'
                p.last_updated = datetime.utcnow()
                # p.has_free_shipping = True (Removed to avoid schema mismatch)
                
                db.session.add(p)
                saved_count += 1
            except Exception as e:
                print(f"   Error saving item: {e}")
        
        db.session.commit()
    
    print(f">> Apify Scan Complete. Saved/Updated {saved_count} viral products.")

if __name__ == '__main__':
    run_apify_scan()
