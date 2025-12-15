#!/usr/bin/env python3
"""
Apify Shop Scanner (Refactored)
Uses generic ApifyService to fetch data.
Handles Discovery Mode scanning with Smart Filters.
"""
import os
import time
import argparse
from datetime import datetime
from apify_service import ApifyService

def log(msg):
    print(f"[Scanner] {msg}", flush=True)

def scan_target(TARGET_ID, MAX_PRODUCTS, LIMIT_PER_RUN=10):
    # LAZY IMPORT to avoid Circular Dependency with app.py
    from app import app, db, Product
    
    saved_ids = []  # Track saved IDs
    total_saved = 0
    page = 1
    
    # Cleanup Broad Scan
    if not TARGET_ID:
        with app.app_context():
            deleted = Product.query.filter(Product.scan_type == 'apify_shop').delete()
            db.session.commit()
            if deleted > 0: log(f">> Cleaned up {deleted} old products for discovery scan.")

    while total_saved < MAX_PRODUCTS:
        log(f"--- Batch {page} (Saved: {total_saved}/{MAX_PRODUCTS}) ---")
        
        # 1. Fetch Items via Service
        if TARGET_ID:
            if any(c.isalpha() for c in TARGET_ID):
                 log(f">> Keyword Search: '{TARGET_ID}'")
                 res = ApifyService.search_products(TARGET_ID, limit=3)
            else:
                 log(f">> ID Lookup: {TARGET_ID}")
                 # Updated signature expects single ID
                 res = ApifyService.get_product_details(TARGET_ID)
        else:
             # Broad Discovery
             log(">> Broad Discovery Search: 'trending products'")
             res = ApifyService.search_products("trending products", limit=LIMIT_PER_RUN)

        if isinstance(res, dict) and 'error' in res:
             log(f"X Service Error: {res['error']}")
             break
        
        items = res if isinstance(res, list) else []
        log(f"   Service returned {len(items)} items")

        if not items:
             if TARGET_ID: 
                 log("   Target not found.")
                 break
             log("   No more items found.")
             break

        # 2. Process & Save
        batch_saved = 0
        with app.app_context():
            for raw_item in items:
                try:
                    data = ApifyService.normalize_item(raw_item)
                    if not data: continue
                    
                    pid = data['product_id']
                    
                    # SAFETY CHECK: Reject "Unknown" garbage (failed scrape)
                    if data.get('product_name') == 'Unknown':
                        log(f"   [SKIP] Rejected 'Unknown' garbage data for {pid}")
                        continue
                    
                    # ---------------------------------------------------------
                    # SMART FILTERS (Discovery Mode Only)
                    # ---------------------------------------------------------
                    if not TARGET_ID:
                        vid_count = data.get('video_count', 0)
                        sales_30 = data.get('sales_30d', 0)
                        
                        # 1. Low Effort: >1 video required
                        if vid_count <= 1:
                            log(f"   [FILTER] Skip {pid}: Low Vids ({vid_count})")
                            continue
                        
                        # 2. Saturation: <150 videos unless highly active
                        if vid_count > 150:
                            if sales_30 < 1000 and data.get('sales', 0) < 5000:
                                log(f"   [FILTER] Skip {pid}: Saturated ({vid_count} vids) & Low Sales")
                                continue
                    # ---------------------------------------------------------
                    
                    # DB Upsert
                    p = Product.query.get(pid)
                    if not p:
                        p = Product(product_id=pid)
                        p.first_seen = datetime.utcnow()
                    
                    # Update all fields
                    p.product_name = data['product_name']
                    # FIXED: Default to 'Unknown Shop' to prevent NULL filter exclusion in dashboard
                    p.seller_name = data.get('shop_name') or data.get('seller_name') or 'Unknown Shop'
                    p.image_url = data['image_url']
                    p.sales = data['sales']
                    p.sales_7d = data['sales_7d']
                    p.sales_30d = data['sales_30d']
                    p.influencer_count = data['influencer_count']
                    p.video_count = data['video_count']
                    p.live_count = data.get('stock') or data.get('live_count', 0) # Mapped to stock in service
                    p.price = data['price']
                    p.original_price = data.get('original_price', 0)
                    p.commission_rate = data.get('commission_rate', 0)

                    # ---------------------------------------------------------
                    # VIDEO RESCUE: If video_count is missing (Excavator limitation), search generic
                    # ---------------------------------------------------------
                    if TARGET_ID and p.video_count <= 2: # Excavator often returns 0
                        log(f"   [RESCUE] Missing Video Count ({p.video_count}). Searching Pratik Dani...")
                        try:
                            # Search by truncated title (max 50 chars to avoid noise)
                            search_q = data['product_name'][:50]
                            rescue_res = ApifyService.search_products(search_q, limit=1)
                            
                            if rescue_res and len(rescue_res) > 0:
                                rescue_data = ApifyService.normalize_item(rescue_res[0])
                                if rescue_data:
                                    # Verify it's roughly the same product (title similarity?)
                                    # For now, trust the first result if sales > 0
                                    log(f"   [RESCUE] Found Match! Videos: {rescue_data['video_count']}, Sales: {rescue_data['sales']}")
                                    
                                    # Update stats if valid
                                    if rescue_data['video_count'] > p.video_count:
                                        p.video_count = rescue_data['video_count']
                                    
                                    if rescue_data['influencer_count'] > 0:
                                        p.influencer_count = rescue_data['influencer_count']
                                        
                                    # Update sales if higher (Pratik is often more up to date on stats)
                                    if rescue_data['sales'] > p.sales:
                                        p.sales = rescue_data['sales']
                        except Exception as e:
                            log(f"   [RESCUE] Failed: {e}")
                    # ---------------------------------------------------------

                    # Use product_id (was raw_id)
                    p.product_url = f"https://shop.tiktok.com/view/product/{data['product_id']}?region=US&locale=en"
                    
                    p.scan_type = 'apify_shop'
                    p.last_updated = datetime.utcnow()
                    
                    db.session.add(p)
                    batch_saved += 1
                    saved_ids.append(pid) # Track ID
                    
                    if batch_saved <= 1:
                        print(f"   Saving: {p.product_name[:30]}... (${p.price})")
                        
                except Exception as e:
                    log(f"Error saving item: {e}")
            
            try:
                db.session.commit()
                log(f"   Saved {batch_saved} products (post-filter).")
            except Exception as e:
                log(f"   DB Commit failed: {e}")

        total_saved += batch_saved
        if TARGET_ID: break # One pass for specific target
        
        if total_saved >= MAX_PRODUCTS: 
            break
        
        page += 1
        time.sleep(2) # Brief pause
        
    return saved_ids

def run_apify_scan():
    # LAZY IMPORT here too
    from app import app, db, Product
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--max_products', type=int, default=50)
    parser.add_argument('--product_id', type=str, default=None, help="Specific Product ID/Keyword to scan")
    parser.add_argument('--refresh_all', action='store_true', help="Refreshes all Shop products")
    args, unknown = parser.parse_known_args()
    
    if args.refresh_all:
        log(">> Bulk Refresh Mode (Using DB Targets)")
        with app.app_context():
            products = Product.query.filter(Product.scan_type.in_(['apify_shop', 'imported'])).all()
            for p in products:
                target = p.product_name if (p.product_name and "Unknown" not in p.product_name) else p.product_id.replace('shop_','')
                scan_target(target, 1)
                time.sleep(2)
    elif args.product_id:
        scan_target(args.product_id, 1)
    else:
        scan_target(None, args.max_products)

if __name__ == '__main__':
    run_apify_scan()
