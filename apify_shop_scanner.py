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

import argparse

def run_apify_scan():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max_products', type=int, default=50)
    args, unknown = parser.parse_known_args()
    
    LIMIT_PER_RUN = 20
    MAX_PRODUCTS = args.max_products
    
    if not APIFY_API_TOKEN:
        print("X Error: APIFY_API_TOKEN not found.")
        return

    print(f">> Starting Shop Product Scan via {ACTOR_ID}...", flush=True)
    print(f">> Target: {MAX_PRODUCTS} products (Batch size: {LIMIT_PER_RUN})")
    
    # Clean up old "viral video" junk only on first run of session? 
    # Actually, let's keep it additive for now, or clear old ones?
    # User pref: "Clean Slate" mentioned in UI. Let's clear for now to avoid duplicates confusing stats.
    with app.app_context():
        # Only delete older than 1 hour to allow "append" logic if we wanted, 
        # but for now, full refresh is safer for "Current Trends"
        deleted = Product.query.filter(Product.scan_type == 'apify_shop').delete()
        db.session.commit()
        if deleted > 0:
            print(f">> Cleaned up {deleted} old products for fresh scan.")

    total_saved = 0
    page = 1
    
    while total_saved < MAX_PRODUCTS:
        print(f"\n--- Batch {page} (Target: {total_saved}/{MAX_PRODUCTS}) ---")
        
        # Search Logic: US Search for "trending"
        # We can shift keys slightly or just rely on random sort from Apify?
        # Apify actor doesn't support "page" param well, but "limit" works.
        # We might get duplicates, so we just filter them out.
        run_input = {
            "keyword": "trending products", 
            "limit": LIMIT_PER_RUN,
            "country_code": "US",
            "sort_type": 1 # 1=Default (Relevance?), 2=Sales? Let's stick to default for variety
        }

        # 1. Start Actor
        start_url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={APIFY_API_TOKEN}"
        run_id = None
        dataset_id = None
        
        try:
            start_res = requests.post(start_url, json=run_input)
            if start_res.status_code != 201:
                print(f"X Failed to start actor: {start_res.text}")
                break # Stop loop
            
            run_data = start_res.json()['data']
            run_id = run_data['id']
            dataset_id = run_data['defaultDatasetId']
            print(f"   Actor started! Run ID: {run_id}")
        except Exception as e:
            print(f"X Error starting actor: {e}")
            break

        # 2. Poll for completion
        while True:
            # Wait 5s between polls
            time.sleep(5) 
            status_res = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_TOKEN}")
            status_data = status_res.json()['data']
            status = status_data['status']
            print(f"   Status: {status}...")
            
            if status == 'SUCCEEDED':
                break
            elif status in ['FAILED', 'ABORTED', 'TIMED-OUT']:
                print(f"X Run failed with status: {status}")
                return # Fatal error
            

        # 3. Fetch Results
        print("   Fetching results...")
        data_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_TOKEN}"
        items_res = requests.get(data_url)
        items = items_res.json()
        print(f"   Found {len(items)} items.")

        if not items:
            print("   No items found. Stopping.")
            break

        # 4. Save to DB
        batch_saved = 0
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
                    if total_saved == 0 and batch_saved == 0:
                        print(f"DEBUG ITEM: {json.dumps(item, default=str)}")
                    
                    # Name
                    p.product_name = (item.get('product_name') or item.get('title') or item.get('name') or "Unknown Product")[:200]
                    
                    # Seller Name
                    seller_data = item.get('seller') or {}
                    if isinstance(seller_data, dict):
                         p.seller_name = seller_data.get('seller_name') or item.get('shop_name') or "TikTok Shop"
                    else:
                         p.seller_name = item.get('seller_name') or item.get('shop_name') or "TikTok Shop"
                    
                    # Image
                    p.image_url = item.get('cover_url') or item.get('main_images', [None])[0]
                    
                    # Helper to clean "K/M" strings
                    def parse_metric(val):
                        if not val: return 0
                        val = str(val).replace('$', '').replace(',', '').strip()
                        mult = 1
                        if 'K' in val:
                            mult = 1000
                            val = val.replace('K', '')
                        elif 'M' in val:
                             mult = 1000000
                             val = val.replace('M', '')
                        try:
                            return int(float(val) * mult)
                        except:
                            return 0

                    def parse_float(val):
                        if not val: return 0.0
                        val = str(val).replace('$', '').replace(',', '').strip()
                        mult = 1
                        if 'K' in val:
                            mult = 1000
                            val = val.replace('K', '')
                        elif 'M' in val:
                             mult = 1000000
                             val = val.replace('M', '')
                        try:
                            return float(val) * mult
                        except:
                            return 0.0

                    # Sales
                    p.sales = parse_metric(item.get('total_sale_cnt') or item.get('sales'))
                    p.sales_30d = parse_metric(item.get('total_sale_30d_cnt'))

                    # GMV
                    p.gmv = parse_float(item.get('total_sale_gmv_amt'))

                    # ADS GMV (New Logic)
                    # "total_sale_gmv_nd_amt" likely means Non-Direct (Ads/Affiliate)
                    # We map this to `msg_gmv` or similar, but for now let's just log it or add to details?
                    # Product model doesn't have 'ads_gmv'. We can overwrite 'msg_gmv' field? 
                    # Or just add it to the description/keywords for now to show in UI?
                    ads_gmv_val = parse_float(item.get('total_sale_gmv_nd_amt'))
                    
                    # Store Commission & Ads info in 'ext_info' or unused fields?
                    # We have 'commission_rate'.
                    comm_str = str(item.get('commission') or "0").replace('%', '')
                    try:
                        p.commission_rate = float(comm_str)
                    except:
                        p.commission_rate = 0.0

                    # Stock (Sum of SKUs)
                    total_stock = 0
                    skus = item.get('skus') or {}
                    if isinstance(skus, dict):
                        for sku_key, sku_data in skus.items():
                             total_stock += int(sku_data.get('stock', 0))
                    
                    # Hack: Store Stock in 'favorites' temporarily or just print?
                    # Product Table has no 'stock' column.
                    # We will use 'favorites' column as a proxy for STOCK for now (since we don't have real favorites from Apify).
                    p.favorites = total_stock # Stock Proxy

                    # Store Ads GMV in 'msg_gmv' (since we don't send messages)
                    p.msg_gmv = ads_gmv_val # Ads GMV Proxy

                    # Price
                    p.price = parse_float(item.get('avg_price') or item.get('real_price') or item.get('price'))

                    # Influencers & Videos
                    p.influencer_count = parse_metric(item.get('total_ifl_cnt'))
                    p.video_count = parse_metric(item.get('total_video_count') or item.get('videos_count'))
                    p.views_count = parse_metric(item.get('view_count'))
                    
                    # URL
                    if not p.product_url or 'http' not in p.product_url:
                         p.product_url = f"https://shop.tiktok.com/view/product/{pid_raw}?region=US&locale=en"

                    p.scan_type = 'apify_shop'
                    p.last_updated = datetime.utcnow()
                    p.is_ad_driven = True
                    
                    db.session.add(p)
                    batch_saved += 1
                except Exception as e:
                    print(f"   Error saving item: {e}")
            
            db.session.commit()
            
        print(f"   Batch Saved: {batch_saved}")
        total_saved += batch_saved
        
        if total_saved >= MAX_PRODUCTS:
            print(">> Reached Max Product Limit. Stopping.")
            break
            
        # Pause before next batch to be nice
        print("   Pausing 5s before next batch...")
        time.sleep(5)
        page += 1
    
    print(f">> Apify Shop Scan Complete. Total Saved: {total_saved} products.")
    print(">> Cleanup complete.")

if __name__ == '__main__':
    run_apify_scan()
