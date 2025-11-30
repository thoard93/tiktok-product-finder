#!/usr/bin/env python3
"""
Daily Scanner Cron Job
Runs at 1:00 AM UTC daily to scan top brands for NEW products

This script:
1. Gets top 20 brands by GMV from EchoTik
2. Scans 5 pages of products from each brand
3. Saves products with low influencer counts (1-100)
4. Skips products already in database
"""

import os
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
import time

# Database setup
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}
db = SQLAlchemy(app)

# EchoTik API Config
BASE_URL = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')

# Scan settings from environment (with defaults)
NUM_BRANDS = int(os.environ.get('NUM_BRANDS', 20))
PAGES_PER_BRAND = int(os.environ.get('PAGES_PER_BRAND', 5))
MAX_INFLUENCERS = int(os.environ.get('MAX_INFLUENCERS', 100))
MIN_SALES = int(os.environ.get('MIN_SALES', 0))

def get_auth():
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)

# Product model (must match app.py)
class Product(db.Model):
    __tablename__ = 'products'
    product_id = db.Column(db.String(50), primary_key=True)
    product_name = db.Column(db.String(500))
    seller_id = db.Column(db.String(50))
    seller_name = db.Column(db.String(200))
    category = db.Column(db.String(100))
    price = db.Column(db.Float)
    commission_rate = db.Column(db.Float)
    sales = db.Column(db.Integer)
    sales_7d = db.Column(db.Integer)
    sales_30d = db.Column(db.Integer)
    gmv = db.Column(db.Float)
    gmv_30d = db.Column(db.Float)
    influencer_count = db.Column(db.Integer)
    video_count = db.Column(db.Integer)
    video_7d = db.Column(db.Integer)
    video_30d = db.Column(db.Integer)
    live_count = db.Column(db.Integer)
    views_count = db.Column(db.Integer)
    product_rating = db.Column(db.Float)
    review_count = db.Column(db.Integer)
    image_url = db.Column(db.Text)
    cached_image_url = db.Column(db.Text)
    product_status = db.Column(db.String(20), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

def get_top_brands(start_rank=1, count=20):
    """Get top brands by GMV from EchoTik"""
    try:
        response = requests.get(
            f"{BASE_URL}/seller/list",
            params={
                'sort_by': 'total_gmv',
                'sort_order': 'desc',
                'page': (start_rank - 1) // 20 + 1,
                'size': count
            },
            auth=get_auth(),
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 0:
                return data.get('data', [])
        return []
    except Exception as e:
        print(f"Error getting brands: {e}")
        return []

def get_brand_products(seller_id, page=1, size=20):
    """Get products from a specific brand"""
    try:
        response = requests.get(
            f"{BASE_URL}/product/list",
            params={
                'seller_id': seller_id,
                'sort_by': 'total_sale_7d_cnt',
                'sort_order': 'desc',
                'page': page,
                'size': size
            },
            auth=get_auth(),
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 0:
                return data.get('data', [])
        return []
    except Exception as e:
        print(f"Error getting products: {e}")
        return []

def save_product(p, seller_name):
    """Save or update a product in database"""
    try:
        product_id = str(p.get('product_id'))
        existing = Product.query.get(product_id)
        
        if existing:
            return False  # Skip existing products in daily scan
        
        product = Product(
            product_id=product_id,
            product_name=p.get('product_name', '')[:500],
            seller_id=str(p.get('seller_id', '')),
            seller_name=seller_name,
            category=p.get('first_category_name', ''),
            price=float(p.get('spu_avg_price', 0) or 0),
            commission_rate=float(p.get('product_commission_rate', 0) or 0),
            sales=int(p.get('total_sale_cnt', 0) or 0),
            sales_7d=int(p.get('total_sale_7d_cnt', 0) or 0),
            sales_30d=int(p.get('total_sale_30d_cnt', 0) or 0),
            gmv=float(p.get('total_sale_gmv_amt', 0) or 0),
            gmv_30d=float(p.get('total_sale_gmv_30d_amt', 0) or 0),
            influencer_count=int(p.get('total_ifl_cnt', 0) or 0),
            video_count=int(p.get('total_video_cnt', 0) or 0),
            video_7d=int(p.get('total_video_7d_cnt', 0) or 0),
            video_30d=int(p.get('total_video_30d_cnt', 0) or 0),
            live_count=int(p.get('total_live_cnt', 0) or 0),
            views_count=int(p.get('total_views_cnt', 0) or 0),
            product_rating=float(p.get('product_rating', 0) or 0),
            review_count=int(p.get('review_count', 0) or 0),
            image_url=p.get('product_img_url', ''),
            product_status='active',
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        
        db.session.add(product)
        return True
        
    except Exception as e:
        print(f"Error saving product: {e}")
        return False

def run_daily_scan():
    """Main daily scan function"""
    print(f"🚀 Starting daily scan at {datetime.utcnow().isoformat()}")
    print(f"   Settings: {NUM_BRANDS} brands, {PAGES_PER_BRAND} pages each, max {MAX_INFLUENCERS} influencers")
    
    stats = {
        'brands_scanned': 0,
        'products_found': 0,
        'products_saved': 0,
        'products_skipped': 0
    }
    
    with app.app_context():
        # Get top brands
        brands = get_top_brands(start_rank=1, count=NUM_BRANDS)
        print(f"   Found {len(brands)} brands to scan")
        
        for brand in brands:
            seller_id = brand.get('seller_id')
            seller_name = brand.get('seller_name', 'Unknown')
            
            print(f"   Scanning: {seller_name}")
            
            for page in range(1, PAGES_PER_BRAND + 1):
                products = get_brand_products(seller_id, page=page)
                
                for p in products:
                    inf_count = int(p.get('total_ifl_cnt', 0) or 0)
                    sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                    
                    stats['products_found'] += 1
                    
                    # Filter by influencer count and minimum sales
                    if inf_count >= 1 and inf_count <= MAX_INFLUENCERS and sales_7d >= MIN_SALES:
                        if save_product(p, seller_name):
                            stats['products_saved'] += 1
                        else:
                            stats['products_skipped'] += 1
                
                time.sleep(0.5)  # Rate limiting
            
            stats['brands_scanned'] += 1
            db.session.commit()
            time.sleep(1)  # Rate limiting between brands
        
        db.session.commit()
    
    print(f"✅ Daily scan complete!")
    print(f"   Brands scanned: {stats['brands_scanned']}")
    print(f"   Products found: {stats['products_found']}")
    print(f"   New products saved: {stats['products_saved']}")
    print(f"   Existing skipped: {stats['products_skipped']}")
    
    return stats

if __name__ == '__main__':
    if not ECHOTIK_USERNAME or not ECHOTIK_PASSWORD:
        print("❌ Error: ECHOTIK_USERNAME and ECHOTIK_PASSWORD must be set")
        exit(1)
    
    run_daily_scan()
