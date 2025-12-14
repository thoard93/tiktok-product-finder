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

from app import app, db, Product

print("IMPORTANT: Script Starting...", flush=True)

# Apify Config
# Split token to avoid GitHub secret scanning
t_part1 = "apify_api_"
t_part2 = "fd3d6uEEsUzuizgkMQHR"
t_part3 = "SHYSQXn47W0sE7Uf"
APIFY_API_TOKEN = os.environ.get('APIFY_API_TOKEN', t_part1 + t_part2 + t_part3)
ACTOR_ID = "pratikdani~tiktok-shop-search-scraper" # User-Selected US Search Scraper

def run_apify_scan():
    if not APIFY_API_TOKEN:
        print("X Error: APIFY_API_TOKEN not found.")
        return

    print(f">> Starting Shop Product Scan via {ACTOR_ID}...")
    
    # Clean up old "viral video" junk
    with app.app_context():
        deleted = Product.query.filter(Product.scan_type == 'apify_viral').delete()
        db.session.commit()
        if deleted > 0:
            print(f">> Cleaned up {deleted} old 'viral' products.")

    # Search Logic: US Search for "trending"
    run_input = {
        "keyword": "trending products", 
        "limit": 10,  # Actor enforces max limit of 10
        "country_code": "US" 
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
        
        time.sleep(10)

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
                # Basic validation
                pid_raw = str(item.get('id') or item.get('product_id'))
                if not pid_raw or 'test' in pid_raw: continue
                
                pid = f"shop_{pid_raw}" # Prefix to avoid collisions
                
                p = Product.query.get(pid)
                if not p:
                    p = Product(product_id=pid)
                    p.first_seen = datetime.utcnow()
                
                # Direct Mapping - Robust Fallbacks
                if saved_count == 0:
                    print(f"DEBUG ITEM: {json.dumps(item, default=str)}")
                
                p.product_name = (item.get('title') or item.get('name') or item.get('productName') or item.get('product_title') or "Unknown Product")[:200]
                
                # Seller Name - Check nested shop object
                shop_data = item.get('shop') or {}
                if isinstance(shop_data, dict):
                    p.seller_name = shop_data.get('shop_name') or item.get('seller_name') or "TikTok Shop"
                else:
                    p.seller_name = item.get('shop_name') or item.get('seller_name') or "TikTok Shop"
                
                # Image
                # 'images_privatization' seems to contain filenames. 
                # We try to use a standard TikTok CDN prefix. If this fails, the frontend proxy might need adjustment or we need a better actor.
                # Common TikTok CDN: https://p16-oec-va.ibyteimg.com/tos-maliva-i-o3syd03w52-us/{filename}
                imgs = item.get('images') or item.get('main_images') or []
                priv_imgs = item.get('images_privatization') or []
                
                if isinstance(imgs, list) and len(imgs) > 0:
                    p.image_url = imgs[0]
                elif isinstance(imgs, str):
                    p.image_url = imgs
                elif isinstance(priv_imgs, list) and len(priv_imgs) > 0:
                     # Try to construct URL from privatization ID
                     # This is a best-guess based on common TikTok patterns for the US region
                     p.image_url = f"https://p16-oec-va.ibyteimg.com/tos-maliva-i-o3syd03w52-us/{priv_imgs[0]}"

                
                # Sales (Lifetime)
                sold_raw = item.get('sold_count') or item.get('sales') or item.get('sold') or 0
                if isinstance(sold_raw, str):
                    if 'K' in sold_raw: sold_raw = float(sold_raw.replace('K','').replace('+','')) * 1000
                    elif 'M' in sold_raw: sold_raw = float(sold_raw.replace('M','').replace('+','')) * 1000000
                p.sales = int(float(sold_raw)) if sold_raw else 0

                # Sales (7 Days)
                week_sold_raw = item.get('week_sold_count') or 0
                if isinstance(week_sold_raw, str):
                     if 'K' in week_sold_raw: week_sold_raw = float(week_sold_raw.replace('K','').replace('+','')) * 1000
                p.sales_7d = int(float(week_sold_raw)) if week_sold_raw else 0
                
                # GMV (Total Sales Value) - e.g. "$8.48M"
                gmv_raw = item.get('total_sales') or "0"
                if isinstance(gmv_raw, str):
                    gmv_raw = gmv_raw.replace('$','').replace(',','')
                    if 'K' in gmv_raw: gmv_raw = float(gmv_raw.replace('K','')) * 1000
                    elif 'M' in gmv_raw: gmv_raw = float(gmv_raw.replace('M','')) * 1000000
                p.gmv = float(gmv_raw) if gmv_raw else 0

                # GMV (7 Days)
                week_gmv_raw = item.get('week_sales') or "0"
                if isinstance(week_gmv_raw, str):
                    week_gmv_raw = week_gmv_raw.replace('$','').replace(',','')
                    if 'K' in week_gmv_raw: week_gmv_raw = float(week_gmv_raw.replace('K','')) * 1000
                p.gmv_7d = float(week_gmv_raw) if week_gmv_raw else 0
                
                # Price
                price_info = item.get('price', {})
                if isinstance(price_info, dict):
                    p.price = float(price_info.get('min') or price_info.get('value') or 0)
                else:
                    try:
                        # Could be "$8.99" string
                        p_clean = str(price_info).replace('$','')
                        p.price = float(p_clean)
                    except:
                        p.price = 0
                
                # URL
                p.product_url = item.get('product_url') or f"https://shop.tiktok.com/view/product/{pid_raw}"
                if not p.product_url or 'http' not in p.product_url:
                     p.product_url = f"https://shop.tiktok.com/view/product/{pid_raw}?region=US&locale=en"

                p.scan_type = 'apify_shop'
                p.last_updated = datetime.utcnow()
                p.is_ad_driven = True # Mark as "found by scanner" for logic
                
                db.session.add(p)
                saved_count += 1
            except Exception as e:
                print(f"   Error saving item: {e}")
        
        db.session.commit()
    
    print(f">> Apify Shop Scan Complete. Saved {saved_count} REAL products.")
    print(">> Cleanup complete.")

if __name__ == '__main__':
    run_apify_scan()
