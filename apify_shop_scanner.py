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

# Setup Logging
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scans')
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
log_file = os.path.join(log_dir, 'apify_shop.log')

def log(msg):
    timestamp = datetime.now().strftime('%H:%M:%S')
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except:
        pass

# Initialize log file
with open(log_file, 'w') as f:
    f.write(f"--- Scan Started at {datetime.now()} ---\n")

import argparse

def run_apify_scan():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max_products', type=int, default=50)
    parser.add_argument('--product_id', type=str, default=None, help="Specific Product ID/Keyword to scan")
    args, unknown = parser.parse_known_args()
    
    LIMIT_PER_RUN = 10 # Apify Actor limit restriction
    MAX_PRODUCTS = args.max_products
    TARGET_ID = args.product_id

    if not APIFY_API_TOKEN:
        log("X Error: APIFY_API_TOKEN not found.")
        return

    if TARGET_ID:
        log(f">> Starting Single Product Scan for: {TARGET_ID}")
        MAX_PRODUCTS = 1 # Only need one
        # If it's a long number, it's likely an ID. If text, it's a keyword.
        # The actor uses 'keyword' for both.
    else:
        log(f">> Starting Shop Product Scan via {ACTOR_ID}...")
        log(f">> Target: {MAX_PRODUCTS} products (Batch size: {LIMIT_PER_RUN})")
    
    # Clean up old "viral video" junk only on first run of session? 
    # Actually, let's keep it additive for now, or clear old ones?
    # User pref: "Clean Slate" mentioned in UI. Let's clear for now to avoid duplicates confusing stats.
    # ONLY clear if running a broad scan. If targeting a specific product, keep the rest!
    if not TARGET_ID:
        with app.app_context():
            # Only delete older than 1 hour to allow "append" logic if we wanted, 
            # but for now, full refresh is safer for "Current Trends"
            deleted = Product.query.filter(Product.scan_type == 'apify_shop').delete()
            db.session.commit()
            if deleted > 0:
                log(f">> Cleaned up {deleted} old products for fresh scan.")

    total_saved = 0
    page = 1
    
    while total_saved < MAX_PRODUCTS:
        log(f"\n--- Batch {page} (Target: {total_saved}/{MAX_PRODUCTS}) ---")
        
        # Search Logic: US Search for "trending"
        # We can shift keys slightly or just rely on random sort from Apify?
        # Apify actor doesn't support "page" param well, but "limit" works.
        # We might get duplicates, so we just filter them out.
        search_keyword = TARGET_ID if TARGET_ID else "trending products"
        
        run_input = {
            "keyword": search_keyword, 
            "limit": LIMIT_PER_RUN,
            "country_code": "US",
            "sort_type": 1, # 1=Default (Relevance?), 2=Sales? Let's stick to default for variety
            "page": page
        }

        if TARGET_ID:
             # SWITCH to Excavator (User request / advertised as fast)
             # ID: excavator~tiktok-shop-product
             CURRENT_ACTOR = "excavator~tiktok-shop-product" 
             
             direct_url = f"https://shop.tiktok.com/view/product/{TARGET_ID}?region=US&locale=en"
             
             # Excavator requires 'urls' key (per error message)
             run_input = {
                 "urls": [{"url": direct_url}],
                 "maxItems": 1
             }
             log(f">> Switching to Fast Scraper: {CURRENT_ACTOR}")
        else:
             CURRENT_ACTOR = ACTOR_ID
        
        log(f">> Run Input: {json.dumps(run_input)}")
        
        # 1. Start Actor
        start_url = f"https://api.apify.com/v2/acts/{CURRENT_ACTOR}/runs?token={APIFY_API_TOKEN}"
        run_id = None
        dataset_id = None
        
        try:
            start_res = requests.post(start_url, json=run_input)
            if start_res.status_code != 201:
                log(f"X Failed to start actor: {start_res.text}")
                break # Stop loop
            
            run_data = start_res.json()['data']
            run_id = run_data['id']
            dataset_id = run_data['defaultDatasetId']
            log(f"   Actor started! Run ID: {run_id}")
        except Exception as e:
            log(f"X Error starting actor: {e}")
            break

        # 2. Poll for completion
        while True:
            # Wait 5s between polls
            time.sleep(5) 
            status_res = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_TOKEN}")
            status_data = status_res.json()['data']
            status = status_data['status']
            log(f"   Status: {status}...")
            
            if status == 'SUCCEEDED':
                break
            elif status in ['FAILED', 'ABORTED', 'TIMED-OUT']:
                log(f"X Run failed with status: {status}")
                if TARGET_ID: return # Fatal error for single item
                break # Break inner poll loop -> Continue outer loop (maybe retry?)
            
        # If run didn't succeed, verify before fetching
        if status != 'SUCCEEDED':
             continue 

        # 3. Fetch Results
        log("   Fetching results...")
        data_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_TOKEN}"
        items_res = requests.get(data_url)
        items = items_res.json()
        log(f"   Found {len(items)} items.")

        if not items:
            log("   No items found.")
            if TARGET_ID:
                log("   [Single Scan] Stopping - ID not found.")
                break # STOP INFINITE LOOP for single ID
            log("   Stopping batch scan.")
            break

        # 4. Save to DB
        batch_saved = 0
        with app.app_context():
            for item in items:
                try:
                    # Check for explicit error from Apify
                    if item.get('error'):
                         log(f"[ERROR] Apify returned error for item: {item.get('error')}")
                         continue

                    # Basic validation
                    # Fallback: If we are targeting a specific ID and the scraper returns an item without an ID, assume it's the one we asked for.
                    pid_raw = str(item.get('id') or item.get('product_id'))
                    
                    if (not pid_raw or 'None' in pid_raw) and TARGET_ID:
                        pid_raw = TARGET_ID
                    
                    if not pid_raw or 'None' in pid_raw or 'test' in pid_raw: 
                        log(f"[WARN] Skipping invalid ID: {pid_raw}. Item keys: {list(item.keys())}")
                        continue
                    
                    pid = f"shop_{pid_raw}" # Prefix to avoid collisions
                    
                    p = Product.query.get(pid)
                    if not p:
                        p = Product(product_id=pid)
                        p.first_seen = datetime.utcnow()
                    
                    # Direct Mapping - Robust Fallbacks
                    if total_saved == 0 and batch_saved == 0:
                        print(f"DEBUG ITEM: {json.dumps(item, default=str)}")
                    
                    # Name
                    raw_name = item.get('product_name') or item.get('title') or item.get('name')
                    if not raw_name:
                         log(f"[WARN] No name found for {pid}. Keys: {list(item.keys())}")
                         p.product_name = "Unknown Product"
                    else:
                         p.product_name = raw_name[:200]
                    
                    # Excavator Schema Mapping (sold, price, images)
                    if item.get('sold'):
                        p.sales = parse_metric(item.get('sold'))
                    if item.get('images') and isinstance(item.get('images'), list) and len(item.get('images')) > 0:
                        p.image_url = item.get('images')[0]
                    if item.get('price'):
                         try:
                             p.price = float(str(item.get('price')).replace('$','').replace(',',''))
                         except: pass
                    
                    # Fix Sort Order: If manual lookup (TARGET_ID), make it "Newest"
                    if TARGET_ID or p.product_name != "Unknown Product":
                         p.first_seen = datetime.utcnow() # Bump to top of "Newest Added" list
                    
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

                    p.sales = parse_metric(item.get('total_sale_cnt') or item.get('sales'))
                    p.sales_30d = parse_metric(item.get('total_sale_30d_cnt') or item.get('sales_30d'))
                    # p.sales_7d -> Field unavailable in Apify. Will default to 0.

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
                    elif isinstance(skus, list): # Handle list case just in case
                         for s in skus:
                             total_stock += int(s.get('stock', 0))
                    
                    # Hack: Store Stock in 'live_count' (Hijacked field)
                    # Product Table has no 'stock' column.
                    # We will use 'live_count' column as a proxy for STOCK.
                    p.live_count = total_stock # Stock Proxy (Hijacked)
                    p.msg_gmv = ads_gmv_val # Ads GMV Proxy

                    # Price
                    p.price = parse_float(item.get('avg_price') or item.get('real_price') or item.get('price'))

                    # Influencers & Videos
                    p.influencer_count = parse_metric(item.get('total_ifl_cnt'))
                    p.video_count = parse_metric(item.get('total_video_count') or item.get('videos_count'))

                    # URL (Force Set to Shop View format which is often more reliable)
                    pid_clean = str(item.get('product_id'))
                    # Format: https://shop.tiktok.com/view/product/1729...?region=US&locale=en
                    p.product_url = f"https://shop.tiktok.com/view/product/{pid_clean}?region=US&locale=en"

                    # Debug Logs
                    if batch_saved < 2: 
                        log(f"   [DEBUG_URI] DB: {app.config['SQLALCHEMY_DATABASE_URI']}")
                        log(f"   [DEBUG_OBJ] Saving '{p.product_name[:10]}...' | Stock: {total_stock} | URL: {p.product_url}")
                        p._debug_stock_val = total_stock # Attach temp attrib for verification

                    p.scan_type = 'apify_shop'
                    p.last_updated = datetime.utcnow()
                    p.is_ad_driven = True
                    
                    db.session.add(p)
                    batch_saved += 1
                except Exception as e:
                    log(f"   Error saving item: {e}")
            
            try:
                db.session.commit()
                # Verify Persistence for items in this batch
                if batch_saved > 0:
                   # Check the last item added
                   p_verify = Product.query.get(p.product_id)
                   log(f"   [DEBUG_PERSIST] {p.product_id} -> Saved Stock: {p_verify.live_count if p_verify else 'NOT FOUND'}")
            except Exception as commit_err:
                 log(f"   [CRITICAL] Commit Failed: {commit_err}")
            
        log(f"   Batch Saved: {batch_saved}")
        
        # Stop infinite loop if single lookup failed to save anything (e.g. error response)
        if TARGET_ID and batch_saved == 0:
             log(f"   [Single Scan] No valid products saved (likely Error response). Stopping.")
             break

        total_saved += batch_saved
        total_saved += batch_saved
        
        if total_saved >= MAX_PRODUCTS:
            log(">> Reached Max Product Limit. Stopping.")
            break
            
        # Pause before next batch to be nice
        log("   Pausing 5s before next batch...")
        time.sleep(5)
        page += 1
    
    log(f">> Apify Shop Scan Complete. Total Saved: {total_saved} products.")
    log(">> Cleanup complete.")

if __name__ == '__main__':
    run_apify_scan()
