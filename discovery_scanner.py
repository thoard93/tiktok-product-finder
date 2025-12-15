#!/usr/bin/env python3
"""
Discovery Scanner
Runs broad keyword searches to find trending products, replacing Echotik's Top Brands feed.
"""
import time
import argparse
from datetime import datetime
from apify_service import ApifyService
from app import app, db, Product, check_and_migrate_db, ensure_db_schema

KEYWORDS = [
    "tiktokmademebuyit",
    "trending products",
    "summer finds",
    "kitchen hacks",
    "gadgets",
    "beauty favorites",
    "home decor"
]

def log(msg):
    print(f"[Discovery] {msg}", flush=True)

def run_discovery(max_per_keyword=5):
    # Ensure DB exists
    with app.app_context():
        ensure_db_schema()
        db.create_all()
        check_and_migrate_db()
        
    log("Starting Discovery Scan...")
    
    total_new = 0
    
    for kw in KEYWORDS:
        log(f">> Scanning keyword: '{kw}'")
        try:
            items = ApifyService.search_products(kw, limit=max_per_keyword)
            if isinstance(items, dict) and 'error' in items:
                log(f"   X Error: {items['error']}")
                continue
            
            if not isinstance(items, list): items = []
            
            log(f"   Found {len(items)} raw items.")
            
            saved_count = 0
            with app.app_context():
                for raw_item in items:
                    data = ApifyService.normalize_item(raw_item)
                    if not data: continue
                    
                    pid = data['product_id']
                    
                    # FILTERS
                    # 1. Low Effort
                    if data.get('video_count', 0) <= 1: continue
                    
                    # 2. Saturation
                    if data.get('video_count', 0) > 150:
                         if data.get('sales_30d', 0) < 1000: continue
                    
                    # Upsert
                    p = Product.query.get(pid)
                    is_new = False
                    if not p:
                        p = Product(product_id=pid)
                        p.first_seen = datetime.utcnow()
                        is_new = True
                    
                    p.product_name = data['product_name']
                    p.seller_name = data['seller_name']
                    p.image_url = data['image_url']
                    p.sales = data['sales']
                    p.sales_7d = data['sales_7d']
                    p.sales_30d = data['sales_30d']
                    p.influencer_count = data['influencer_count']
                    p.video_count = data['video_count']
                    p.live_count = data['live_count']
                    p.price = data['price']
                    p.original_price = data['original_price']
                    p.commission_rate = data['commission_rate']
                    p.product_url = f"https://shop.tiktok.com/view/product/{data['raw_id']}?region=US&locale=en"
                    
                    p.scan_type = 'discovery' # Distinct type
                    p.last_updated = datetime.utcnow()
                    
                    db.session.add(p)
                    saved_count += 1
                    if is_new: total_new += 1
                
                try:
                    db.session.commit()
                    log(f"   Saved {saved_count} products.")
                except Exception as e:
                    log(f"   Commit failed: {e}")
                    
        except Exception as e:
            log(f"   Keyword Scan Failed: {e}")
            
        time.sleep(5) # Pause between filtered searches

    log(f"Discovery Complete. {total_new} NEW products added.")

if __name__ == '__main__':
    run_discovery()
