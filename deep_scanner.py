"""
Brand Hunter Deep Scanner
Runs as a cron job to scan all pages (up to 200) from top brands.

Strategy:
- Scans pages 1-200 from each brand (where the gems hide)
- Sorts by 7-day sales descending
- Filters for 1-100 influencers (low competition)
- Runs in batches to avoid memory issues

Schedule: Daily at 1:00 AM UTC (after EchoTik updates at UTC 0)

Usage:
  python deep_scanner.py                  # Full scan
  python deep_scanner.py --brands 5       # Scan 5 brands
  python deep_scanner.py --start-page 100 # Start from page 100
"""

import os
import sys
import time
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
import argparse

# Database setup
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
db_url = os.environ.get('DATABASE_URL', 'sqlite:///products.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# EchoTik API
BASE_URL = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')

def get_auth():
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)

# Product model (must match app.py)
class Product(db.Model):
    __tablename__ = 'products'
    product_id = db.Column(db.String(50), primary_key=True)
    product_name = db.Column(db.String(500))
    seller_id = db.Column(db.String(50))
    seller_name = db.Column(db.String(255))
    gmv = db.Column(db.Float, default=0)
    gmv_30d = db.Column(db.Float, default=0)
    sales = db.Column(db.Integer, default=0)
    sales_7d = db.Column(db.Integer, default=0)
    sales_30d = db.Column(db.Integer, default=0)
    influencer_count = db.Column(db.Integer, default=0)
    commission_rate = db.Column(db.Float, default=0)
    price = db.Column(db.Float, default=0)
    image_url = db.Column(db.Text)
    scan_type = db.Column(db.String(50), default='brand_hunter')
    first_seen = db.Column(db.DateTime, default=datetime.utcnow)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

def parse_cover_url(raw):
    """Extract clean URL from cover_url"""
    import json
    if not raw:
        return None
    if isinstance(raw, str):
        if raw.startswith('['):
            try:
                urls = json.loads(raw)
                if urls and isinstance(urls, list) and len(urls) > 0:
                    urls.sort(key=lambda x: x.get('index', 0) if isinstance(x, dict) else 0)
                    return urls[0].get('url') if isinstance(urls[0], dict) else urls[0]
            except:
                return raw if raw.startswith('http') else None
        elif raw.startswith('http'):
            return raw
    return None

def get_top_brands(num_brands=10):
    """Get top brands by GMV"""
    all_brands = []
    pages_needed = (num_brands + 9) // 10  # 10 per page
    
    for page in range(1, pages_needed + 1):
        try:
            response = requests.get(
                f"{BASE_URL}/seller/list",
                params={
                    "page_num": page,
                    "page_size": 10,
                    "region": "US",
                    "seller_sort_field": 2,  # GMV
                    "sort_type": 1           # Descending
                },
                auth=get_auth(),
                timeout=30
            )
            data = response.json()
            if data.get('code') == 0:
                all_brands.extend(data.get('data', []))
            time.sleep(0.3)
        except Exception as e:
            print(f"Error fetching brands page {page}: {e}")
    
    return all_brands[:num_brands]

def get_seller_products(seller_id, page=1):
    """Get products from seller sorted by 7-day sales"""
    try:
        response = requests.get(
            f"{BASE_URL}/seller/product/list",
            params={
                "seller_id": seller_id,
                "page_num": page,
                "page_size": 10,
                "seller_product_sort_field": 4,  # 7-day sales
                "sort_type": 1                    # Descending
            },
            auth=get_auth(),
            timeout=30
        )
        data = response.json()
        if data.get('code') == 0:
            return data.get('data', [])
        return []
    except Exception as e:
        print(f"Error fetching products: {e}")
        return []

def deep_scan(num_brands=10, max_pages=200, start_page=1, 
              min_influencers=1, max_influencers=100, min_sales_7d=1):
    """
    Deep scan brands for hidden gems.
    
    Args:
        num_brands: Number of top brands to scan
        max_pages: Maximum pages per brand (up to 200)
        start_page: Starting page (useful for scanning later pages)
        min_influencers: Minimum influencer count filter
        max_influencers: Maximum influencer count filter
        min_sales_7d: Minimum 7-day sales filter
    """
    print(f"\n{'='*60}")
    print(f"BRAND HUNTER DEEP SCAN")
    print(f"{'='*60}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Brands: {num_brands} | Pages: {start_page}-{max_pages}")
    print(f"Filters: {min_influencers}-{max_influencers} influencers, {min_sales_7d}+ 7d sales")
    print(f"{'='*60}\n")
    
    with app.app_context():
        db.create_all()
        
        # Try to add sales_7d column if missing
        try:
            db.session.execute(db.text('ALTER TABLE products ADD COLUMN sales_7d INTEGER DEFAULT 0'))
            db.session.commit()
            print("Added sales_7d column")
        except:
            db.session.rollback()
        
        brands = get_top_brands(num_brands)
        print(f"Found {len(brands)} brands to scan\n")
        
        total_found = 0
        total_saved = 0
        
        for i, brand in enumerate(brands, 1):
            seller_id = brand.get('seller_id', '')
            seller_name = brand.get('seller_name', 'Unknown')
            
            if not seller_id:
                continue
            
            print(f"\n[{i}/{len(brands)}] 📦 {seller_name}")
            print(f"    Scanning pages {start_page} to {max_pages}...")
            
            brand_found = 0
            brand_saved = 0
            empty_pages = 0
            
            for page in range(start_page, max_pages + 1):
                if page % 20 == 0:
                    print(f"    Page {page}...")
                
                products = get_seller_products(seller_id, page)
                
                if not products:
                    empty_pages += 1
                    if empty_pages >= 5:  # Stop after 5 empty pages
                        print(f"    No more products at page {page}")
                        break
                    continue
                
                empty_pages = 0  # Reset counter
                
                for p in products:
                    product_id = p.get('product_id', '')
                    if not product_id:
                        continue
                    
                    influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
                    sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                    total_sales = int(p.get('total_sale_cnt', 0) or 0)
                    sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
                    
                    # Apply filters
                    if influencer_count < min_influencers or influencer_count > max_influencers:
                        continue
                    if sales_7d < min_sales_7d:
                        continue
                    
                    brand_found += 1
                    image_url = parse_cover_url(p.get('cover_url', ''))
                    
                    # Save to database
                    existing = Product.query.get(product_id)
                    if existing:
                        existing.influencer_count = influencer_count
                        existing.sales = total_sales
                        existing.sales_7d = sales_7d
                        existing.sales_30d = sales_30d
                        existing.last_updated = datetime.utcnow()
                    else:
                        product = Product(
                            product_id=product_id,
                            product_name=p.get('product_name', ''),
                            seller_id=seller_id,
                            seller_name=seller_name,
                            gmv=float(p.get('total_sale_gmv_amt', 0) or 0),
                            gmv_30d=float(p.get('total_sale_gmv_30d_amt', 0) or 0),
                            sales=total_sales,
                            sales_7d=sales_7d,
                            sales_30d=sales_30d,
                            influencer_count=influencer_count,
                            commission_rate=float(p.get('product_commission_rate', 0) or 0),
                            price=float(p.get('spu_avg_price', 0) or 0),
                            image_url=image_url,
                            scan_type='deep_scan'
                        )
                        db.session.add(product)
                        brand_saved += 1
                
                # Rate limiting
                time.sleep(0.3)
            
            db.session.commit()
            total_found += brand_found
            total_saved += brand_saved
            
            print(f"    ✅ Found: {brand_found}, New: {brand_saved}")
        
        print(f"\n{'='*60}")
        print(f"SCAN COMPLETE")
        print(f"{'='*60}")
        print(f"Total products found: {total_found}")
        print(f"New products saved: {total_saved}")
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")
        
        return {'found': total_found, 'saved': total_saved}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Brand Hunter Deep Scanner')
    parser.add_argument('--brands', type=int, default=10, help='Number of brands to scan')
    parser.add_argument('--max-pages', type=int, default=200, help='Max pages per brand')
    parser.add_argument('--start-page', type=int, default=1, help='Starting page')
    parser.add_argument('--min-influencers', type=int, default=1, help='Min influencer filter')
    parser.add_argument('--max-influencers', type=int, default=100, help='Max influencer filter')
    parser.add_argument('--min-sales', type=int, default=1, help='Min 7-day sales filter')
    
    args = parser.parse_args()
    
    deep_scan(
        num_brands=args.brands,
        max_pages=args.max_pages,
        start_page=args.start_page,
        min_influencers=args.min_influencers,
        max_influencers=args.max_influencers,
        min_sales_7d=args.min_sales
    )
