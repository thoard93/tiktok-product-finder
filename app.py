"""
TikTok Product Finder - Brand Hunter (Simplified)
Scans TOP BRANDS directly - no follow workflow needed

Strategy: 
- Get top brands by GMV from EchoTik
- Scan their products sorted by 7-DAY SALES DESCENDING
- Filter for low influencer count (1-100)
- Save hidden gems automatically

Why 7-day sales descending:
- Products with recent momentum, not legacy sellers
- Lower influencer counts than all-time bestsellers
- Better use of limited pages - active products first

API Reference (EchoTik v3):
- seller/list: seller_sort_field 1=sales, 2=gmv, 3=avg_price | sort_type 0=asc, 1=desc
- seller/product/list: seller_product_sort_field 1=total_sale_cnt, 2=gmv, 3=avg_price, 4=7d_sales, 5=7d_gmv | sort_type 0=asc, 1=desc
"""

import os
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_sqlalchemy import SQLAlchemy
import time
import json

app = Flask(__name__, static_folder='pwa')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///products.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Fix Render's postgres:// URL
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

# Connection pool settings to handle Render's connection drops
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,  # Test connection before using
    'pool_recycle': 300,    # Recycle connections every 5 minutes
    'pool_size': 5,
    'max_overflow': 10,
    'pool_timeout': 30,
}

db = SQLAlchemy(app)

# EchoTik API Config - v3 API with HTTPBasicAuth
BASE_URL = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')

# =============================================================================
# DATABASE MODELS
# =============================================================================

class Product(db.Model):
    """Products found by scanner"""
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
    cached_image_url = db.Column(db.Text)  # Signed URL that works
    image_cached_at = db.Column(db.DateTime)  # When cache was created
    
    # Video/Live stats from EchoTik
    video_count = db.Column(db.Integer, default=0)
    video_7d = db.Column(db.Integer, default=0)
    video_30d = db.Column(db.Integer, default=0)
    live_count = db.Column(db.Integer, default=0)
    views_count = db.Column(db.Integer, default=0)
    product_rating = db.Column(db.Float, default=0)
    review_count = db.Column(db.Integer, default=0)
    
    # User features
    is_favorite = db.Column(db.Boolean, default=False)
    
    scan_type = db.Column(db.String(50), default='brand_hunter')
    first_seen = db.Column(db.DateTime, default=datetime.utcnow)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'product_id': self.product_id,
            'product_name': self.product_name,
            'seller_id': self.seller_id,
            'seller_name': self.seller_name,
            'gmv': self.gmv,
            'gmv_30d': self.gmv_30d,
            'sales': self.sales,
            'sales_7d': self.sales_7d,
            'sales_30d': self.sales_30d,
            'influencer_count': self.influencer_count,
            'commission_rate': self.commission_rate,
            'price': self.price,
            'image_url': self.cached_image_url or self.image_url,  # Prefer cached
            'cached_image_url': self.cached_image_url,
            'video_count': self.video_count,
            'video_7d': self.video_7d,
            'video_30d': self.video_30d,
            'live_count': self.live_count,
            'views_count': self.views_count,
            'product_rating': self.product_rating,
            'review_count': self.review_count,
            'is_favorite': self.is_favorite,
            'scan_type': self.scan_type,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None
        }

# =============================================================================
# ECHOTIK API HELPERS - v3 API with HTTPBasicAuth
# =============================================================================

def get_auth():
    """Get HTTPBasicAuth object for EchoTik API"""
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)

def parse_cover_url(raw):
    """Extract clean URL from cover_url which may be a JSON array string."""
    if not raw:
        return None
    if isinstance(raw, str):
        if raw.startswith('['):
            try:
                urls = json.loads(raw)
                if urls and isinstance(urls, list) and len(urls) > 0:
                    # Sort by index and get first
                    urls.sort(key=lambda x: x.get('index', 0) if isinstance(x, dict) else 0)
                    return urls[0].get('url') if isinstance(urls[0], dict) else urls[0]
            except json.JSONDecodeError:
                return raw if raw.startswith('http') else None
        elif raw.startswith('http'):
            return raw
    elif isinstance(raw, list) and len(raw) > 0:
        return raw[0].get('url') if isinstance(raw[0], dict) else raw[0]
    return None

def get_cached_image_urls(cover_urls):
    """
    Call EchoTik's batch cover download API to get signed URLs.
    
    Args:
        cover_urls: List of original cover URLs (max 10 per call)
    
    Returns:
        Dict mapping original URL -> signed URL
    """
    if not cover_urls:
        return {}
    
    # Filter for valid EchoTik URLs
    valid_urls = [url for url in cover_urls if url and 'echosell-images' in str(url)]
    
    if not valid_urls:
        return {}
    
    # Max 10 URLs per request
    url_string = ','.join(valid_urls[:10])
    
    try:
        response = requests.get(
            f"{BASE_URL}/batch/cover/download",
            params={'cover_urls': url_string},
            auth=get_auth(),
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 0 and data.get('data'):
                result = {}
                for item in data['data']:
                    if isinstance(item, dict):
                        for orig_url, signed_url in item.items():
                            if signed_url and signed_url.startswith('http'):
                                result[orig_url] = signed_url
                return result
        
        return {}
        
    except Exception as e:
        print(f"EchoTik image API exception: {e}")
        return {}

def get_top_brands(page=1):
    """
    Get top brands/sellers sorted by GMV
    
    seller_sort_field: 1=total_sale_cnt, 2=total_sale_gmv_amt, 3=spu_avg_price
    sort_type: 0=asc, 1=desc
    """
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
        print(f"Seller list response code: {data.get('code')}, count: {len(data.get('data', []))}")
        if data.get('code') == 0:
            return data.get('data', [])
        print(f"Get brands error: {data}")
        return []
    except Exception as e:
        print(f"Get brands exception: {e}")
        return []

def get_seller_products(seller_id, page=1, page_size=10):
    """
    Get products from a seller sorted by 7-DAY SALES DESCENDING
    Then we filter for low influencer count (1-100) after fetching
    
    seller_product_sort_field:
        1 = total_sale_cnt (Total Sales)
        2 = total_sale_gmv_amt (Total GMV)
        3 = spu_avg_price (Avg Price)
        4 = total_sale_7d_cnt (7-day Sales) <-- USING THIS
        5 = total_sale_gmv_7d_amt (7-day GMV)
    
    sort_type: 0=asc, 1=desc
    
    Why 7-day sales descending:
    - Shows products with RECENT momentum (not legacy sellers)
    - Products hot now have lower influencer counts than all-time bestsellers
    - Better use of limited pages - active products first, not dead inventory
    
    NOTE: No influencer sort option - we filter by total_ifl_cnt after fetching
    """
    try:
        response = requests.get(
            f"{BASE_URL}/seller/product/list",
            params={
                "seller_id": seller_id,
                "page_num": page,
                "page_size": page_size,
                "seller_product_sort_field": 4,  # 7-day Sales
                "sort_type": 1                    # Descending
            },
            auth=get_auth(),
            timeout=30
        )
        data = response.json()
        if data.get('code') == 0:
            return data.get('data', [])
        print(f"Seller products error for {seller_id}: {data}")
        return []
    except Exception as e:
        print(f"Seller products exception: {e}")
        return []

# =============================================================================
# MAIN SCANNING ENDPOINTS
# =============================================================================

@app.route('/api/scan', methods=['GET'])
def scan_top_brands():
    """
    Main scanning endpoint - scans top brands for hidden gems
    
    Strategy: Get products sorted by sales, filter for low influencer count
    
    Parameters:
        brands: Number of brands to scan (default: 5)
        start_rank: Starting brand rank (default: 1, meaning top brand)
        pages_per_brand: Pages to scan per brand (default: 10)
        min_influencers: Minimum influencer count (default: 1)
        max_influencers: Maximum influencer count (default: 100)
        min_sales: Minimum 7-day sales (default: 0)
    """
    try:
        num_brands = request.args.get('brands', 5, type=int)
        start_rank = request.args.get('start_rank', 1, type=int)
        pages_per_brand = request.args.get('pages_per_brand', 10, type=int)
        min_influencers = request.args.get('min_influencers', 1, type=int)
        max_influencers = request.args.get('max_influencers', 100, type=int)
        min_sales = request.args.get('min_sales', 0, type=int)
        
        # Calculate which pages of brands to fetch
        # EchoTik returns 10 brands per page
        start_page = (start_rank - 1) // 10 + 1
        start_offset = (start_rank - 1) % 10
        
        # Get brands from the right pages
        all_brands = []
        pages_needed = ((start_offset + num_brands - 1) // 10) + 1
        
        for page in range(start_page, start_page + pages_needed):
            brands_page = get_top_brands(page=page)
            if brands_page:
                all_brands.extend(brands_page)
            time.sleep(0.2)
        
        # Slice to get exactly the brands we want
        brands = all_brands[start_offset:start_offset + num_brands]
        
        if not brands:
            return jsonify({'error': 'Failed to fetch brands - check EchoTik credentials'}), 500
        
        results = {
            'brands_scanned': [],
            'total_products_found': 0,
            'total_products_saved': 0,
            'scan_info': {
                'brand_ranks': f"{start_rank}-{start_rank + len(brands) - 1}",
                'pages_per_brand': pages_per_brand
            },
            'filter_settings': {
                'min_influencers': min_influencers,
                'max_influencers': max_influencers,
                'min_sales_7d': min_sales,
                'sort': '7_day_sales_descending',
                'note': 'Products sorted by 7-day sales (recent momentum), filtered by influencer count'
            }
        }
        
        for brand in brands:
            seller_id = brand.get('seller_id', '')
            seller_name = brand.get('seller_name', 'Unknown')
            
            if not seller_id:
                continue
            
            print(f"\n📦 Scanning: {seller_name}")
            
            brand_result = {
                'seller_id': seller_id,
                'seller_name': seller_name,
                'products_scanned': 0,
                'products_found': 0,
                'products_saved': 0
            }
            
            for page in range(1, pages_per_brand + 1):
                products = get_seller_products(seller_id, page=page)
                
                if not products:
                    print(f"  No more products at page {page}")
                    break
                
                brand_result['products_scanned'] += len(products)
                
                for p in products:
                    product_id = p.get('product_id', '')
                    if not product_id:
                        continue
                    
                    # Get influencer count and sales
                    influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
                    total_sales = int(p.get('total_sale_cnt', 0) or 0)
                    sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                    sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
                    
                    # Get commission and video stats
                    commission_rate = float(p.get('product_commission_rate', 0) or 0)
                    video_count = int(p.get('total_video_cnt', 0) or 0)
                    video_7d = int(p.get('total_video_7d_cnt', 0) or 0)
                    video_30d = int(p.get('total_video_30d_cnt', 0) or 0)
                    live_count = int(p.get('total_live_cnt', 0) or 0)
                    views_count = int(p.get('total_views_cnt', 0) or 0)
                    
                    # Filter: Must be in target influencer range AND have recent sales
                    if influencer_count < min_influencers or influencer_count > max_influencers:
                        continue
                    if sales_7d < min_sales:  # Filter by 7-day sales, not total
                        continue
                    
                    # SKIP products with 0% commission - not available for affiliates
                    if commission_rate <= 0:
                        continue
                    
                    brand_result['products_found'] += 1
                    
                    # Parse image URL
                    image_url = parse_cover_url(p.get('cover_url', ''))
                    
                    # Save to database
                    existing = Product.query.get(product_id)
                    if existing:
                        existing.influencer_count = influencer_count
                        existing.sales = total_sales
                        existing.sales_30d = sales_30d
                        existing.sales_7d = sales_7d
                        existing.commission_rate = commission_rate
                        existing.video_count = video_count
                        existing.video_7d = video_7d
                        existing.video_30d = video_30d
                        existing.live_count = live_count
                        existing.views_count = views_count
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
                            commission_rate=commission_rate,
                            price=float(p.get('spu_avg_price', 0) or 0),
                            image_url=image_url,
                            video_count=video_count,
                            video_7d=video_7d,
                            video_30d=video_30d,
                            live_count=live_count,
                            views_count=views_count,
                            scan_type='brand_hunter'
                        )
                        db.session.add(product)
                        brand_result['products_saved'] += 1
                
                time.sleep(0.3)
            
            # Commit after each brand to avoid losing progress
            db.session.commit()
            
            results['brands_scanned'].append(brand_result)
            results['total_products_found'] += brand_result['products_found']
            results['total_products_saved'] += brand_result['products_saved']
            
            print(f"  ✅ Scanned: {brand_result['products_scanned']}, Found: {brand_result['products_found']}, Saved: {brand_result['products_saved']}")
        
        return jsonify(results)
    
    except Exception as e:
        import traceback
        db.session.rollback()
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

@app.route('/api/scan-pages/<seller_id>', methods=['GET'])
def scan_page_range(seller_id):
    """
    Scan a specific page range from a seller.
    Useful for getting deep pages (100-200) where gems hide.
    
    Parameters:
        start: Starting page (default: 1)
        end: Ending page (default: 50)
        max_influencers: Max influencer filter (default: 100)
        min_sales: Min 7-day sales (default: 0)
    """
    try:
        start_page = request.args.get('start', 1, type=int)
        end_page = request.args.get('end', 50, type=int)
        min_influencers = request.args.get('min_influencers', 1, type=int)
        max_influencers = request.args.get('max_influencers', 100, type=int)
        min_sales = request.args.get('min_sales', 0, type=int)
        
        products_scanned = 0
        products_found = 0
        products_saved = 0
        seller_name = "Unknown"
        
        for page in range(start_page, end_page + 1):
            products = get_seller_products(seller_id, page=page)
            
            if not products:
                continue
            
            for p in products:
                products_scanned += 1
                product_id = p.get('product_id', '')
                if not product_id:
                    continue
                
                if seller_name == "Unknown":
                    seller_name = p.get('seller_name', 'Unknown') or "Unknown"
                
                influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
                sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                total_sales = int(p.get('total_sale_cnt', 0) or 0)
                sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
                
                if influencer_count < min_influencers or influencer_count > max_influencers:
                    continue
                if sales_7d < min_sales:
                    continue
                
                products_found += 1
                image_url = parse_cover_url(p.get('cover_url', ''))
                
                existing = Product.query.get(product_id)
                if not existing:
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
                        scan_type='page_range'
                    )
                    db.session.add(product)
                    products_saved += 1
            
            time.sleep(0.2)
        
        db.session.commit()
        
        return jsonify({
            'seller_id': seller_id,
            'seller_name': seller_name,
            'pages_scanned': f"{start_page}-{end_page}",
            'products_scanned': products_scanned,
            'products_found': products_found,
            'products_saved': products_saved
        })
    
    except Exception as e:
        import traceback
        db.session.rollback()
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/scan-brand/<seller_id>', methods=['GET'])
def scan_single_brand(seller_id):
    """Deep scan a specific brand by seller_id"""
    pages = request.args.get('pages', 50, type=int)
    min_influencers = request.args.get('min_influencers', 1, type=int)
    max_influencers = request.args.get('max_influencers', 100, type=int)
    min_sales = request.args.get('min_sales', 10, type=int)
    
    products_scanned = 0
    products_found = 0
    products_saved = 0
    seller_name = "Unknown"
    
    for page in range(1, pages + 1):
        if page % 10 == 0:
            print(f"Scanning page {page}...")
        
        products = get_seller_products(seller_id, page=page)
        
        if not products:
            break
        
        for p in products:
            products_scanned += 1
            product_id = p.get('product_id', '')
            if not product_id:
                continue
            
            if seller_name == "Unknown":
                seller_name = p.get('seller_name', 'Unknown') or "Unknown"
            
            influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
            total_sales = int(p.get('total_sale_cnt', 0) or 0)
            sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
            sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
            
            if influencer_count < min_influencers or influencer_count > max_influencers:
                continue
            if sales_7d < min_sales:  # Filter by 7-day sales
                continue
            
            products_found += 1
            image_url = parse_cover_url(p.get('cover_url', ''))
            
            existing = Product.query.get(product_id)
            if not existing:
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
                    scan_type='brand_hunter'
                )
                db.session.add(product)
                products_saved += 1
        
        time.sleep(0.3)
    
    db.session.commit()
    
    return jsonify({
        'seller_id': seller_id,
        'seller_name': seller_name,
        'pages_scanned': page,
        'products_scanned': products_scanned,
        'products_found': products_found,
        'products_saved': products_saved
    })

@app.route('/api/brands/list', methods=['GET'])
def list_top_brands():
    """Get list of top brands from EchoTik"""
    page = request.args.get('page', 1, type=int)
    
    brands = get_top_brands(page=page)
    
    if not brands:
        return jsonify({'error': 'Failed to fetch brands', 'brands': []}), 500
    
    return jsonify({
        'brands': [{
            'seller_id': b.get('seller_id'),
            'seller_name': b.get('seller_name'),
            'gmv': b.get('total_sale_gmv_amt', 0),
            'products_count': b.get('total_product_cnt', 0),
            'influencer_count': b.get('total_ifl_cnt', 0),
            'total_sales': b.get('total_sale_cnt', 0)
        } for b in brands],
        'page': page,
        'count': len(brands)
    })

# =============================================================================
# PRODUCTS ENDPOINTS
# =============================================================================

@app.route('/api/products', methods=['GET'])
def get_products():
    """Get all saved products with filtering options"""
    min_influencers = request.args.get('min_influencers', 1, type=int)
    max_influencers = request.args.get('max_influencers', 500, type=int)
    limit = request.args.get('limit', 500, type=int)
    
    # Date filter: today, yesterday, 7days, all
    date_filter = request.args.get('date', 'all')
    
    # Brand/seller search
    brand_search = request.args.get('brand', '').strip()
    
    # Favorites only
    favorites_only = request.args.get('favorites', 'false').lower() == 'true'
    
    # Build query
    query = Product.query.filter(
        Product.influencer_count >= min_influencers,
        Product.influencer_count <= max_influencers
    )
    
    # Apply date filter
    now = datetime.utcnow()
    if date_filter == 'today':
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(Product.first_seen >= start_of_day)
    elif date_filter == 'yesterday':
        start_of_yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_yesterday = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(Product.first_seen >= start_of_yesterday, Product.first_seen < end_of_yesterday)
    elif date_filter == '7days':
        week_ago = now - timedelta(days=7)
        query = query.filter(Product.first_seen >= week_ago)
    
    # Apply brand search
    if brand_search:
        query = query.filter(Product.seller_name.ilike(f'%{brand_search}%'))
    
    # Apply favorites filter
    if favorites_only:
        query = query.filter(Product.is_favorite == True)
    
    products = query.order_by(Product.sales_7d.desc()).limit(limit).all()
    
    return jsonify({
        'products': [p.to_dict() for p in products],
        'total': len(products),
        'filters': {
            'date': date_filter,
            'brand': brand_search,
            'favorites_only': favorites_only
        }
    })

@app.route('/product')
def product_detail_page():
    """Product detail page - serve from pwa folder"""
    return send_from_directory('pwa', 'product_detail.html')


@app.route('/api/product/<product_id>')
def get_product_detail(product_id):
    """Get detailed info for a single product"""
    try:
        product = Product.query.get(product_id)
        
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        
        # Use cached image if available, otherwise fall back to proxy
        image_url = product.cached_image_url or f'/api/image-proxy/{product_id}'
        
        data = {
            'product_id': product.product_id,
            'product_name': product.product_name or '',
            'seller_id': product.seller_id,
            'seller_name': product.seller_name or 'Unknown',
            
            # Sales data
            'gmv': float(product.gmv or 0),
            'gmv_30d': float(product.gmv_30d or 0),
            'sales': int(product.sales or 0),
            'sales_7d': int(product.sales_7d or 0),
            'sales_30d': int(product.sales_30d or 0),
            
            # Commission
            'commission_rate': float(product.commission_rate or 0),
            
            # Competition
            'influencer_count': int(product.influencer_count or 0),
            
            # Product info
            'price': float(product.price or 0),
            
            # Video/Live stats
            'video_count': int(product.video_count or 0),
            'video_7d': int(product.video_7d or 0),
            'video_30d': int(product.video_30d or 0),
            'live_count': int(product.live_count or 0),
            'views_count': int(product.views_count or 0),
            'product_rating': float(product.product_rating or 0),
            'review_count': int(product.review_count or 0),
            
            # Favorites
            'is_favorite': product.is_favorite or False,
            
            # Media - use cached URL for instant loading
            'image_url': image_url,
            'cached_image_url': image_url,
            
            # Links
            'tiktok_url': f'https://www.tiktok.com/shop/product/{product.product_id}',
            'affiliate_url': f'https://affiliate.tiktok.com/product/{product.product_id}',
            
            # Timestamps
            'first_seen': product.first_seen.isoformat() if product.first_seen else None,
            'last_updated': product.last_updated.isoformat() if product.last_updated else None,
        }
        
        return jsonify({'success': True, 'product': data})
        
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get scanning statistics"""
    total = Product.query.count()
    
    # Ranges for 1-100 strategy
    untapped = Product.query.filter(
        Product.influencer_count >= 1,
        Product.influencer_count <= 10
    ).count()
    
    low = Product.query.filter(
        Product.influencer_count >= 11,
        Product.influencer_count <= 30
    ).count()
    
    medium = Product.query.filter(
        Product.influencer_count >= 31,
        Product.influencer_count <= 60
    ).count()
    
    good = Product.query.filter(
        Product.influencer_count >= 61,
        Product.influencer_count <= 100
    ).count()
    
    # Get unique brands
    brands = db.session.query(Product.seller_name).distinct().count()
    
    return jsonify({
        'total_products': total,
        'unique_brands': brands,
        'breakdown': {
            'untapped_1_10': untapped,
            'low_11_30': low,
            'medium_31_60': medium,
            'good_61_100': good
        }
    })


@app.route('/api/refresh-images', methods=['POST', 'GET'])
def refresh_images():
    """
    Batch refresh cached image URLs for products.
    Call this after scanning to get working image URLs.
    """
    try:
        batch_size = request.args.get('batch', 50, type=int)
        
        # Get products with image_url but no cached_image_url (or old cache)
        products = Product.query.filter(
            Product.image_url.isnot(None),
            Product.image_url != '',
            db.or_(
                Product.cached_image_url.is_(None),
                Product.cached_image_url == ''
            )
        ).limit(batch_size).all()
        
        if not products:
            return jsonify({'success': True, 'message': 'No images need refreshing', 'refreshed': 0})
        
        # Group by batches of 10 (API limit)
        refreshed = 0
        for i in range(0, len(products), 10):
            batch = products[i:i+10]
            
            # Get original URLs
            url_to_product = {}
            for p in batch:
                parsed_url = parse_cover_url(p.image_url)
                if parsed_url:
                    url_to_product[parsed_url] = p
            
            if not url_to_product:
                continue
            
            # Get signed URLs
            signed_urls = get_cached_image_urls(list(url_to_product.keys()))
            
            # Update products
            for orig_url, signed_url in signed_urls.items():
                if orig_url in url_to_product:
                    product = url_to_product[orig_url]
                    product.cached_image_url = signed_url
                    product.image_cached_at = datetime.utcnow()
                    refreshed += 1
            
            db.session.commit()
            time.sleep(0.3)  # Rate limiting
        
        return jsonify({
            'success': True,
            'message': f'Refreshed {refreshed} images',
            'refreshed': refreshed,
            'remaining': Product.query.filter(
                Product.image_url.isnot(None),
                Product.cached_image_url.is_(None)
            ).count()
        })
        
    except Exception as e:
        import traceback
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/clear', methods=['POST'])
def clear_products():
    """Clear all products"""
    Product.query.delete()
    db.session.commit()
    return jsonify({'success': True, 'message': 'All products cleared'})


@app.route('/api/favorite/<product_id>', methods=['POST'])
def toggle_favorite(product_id):
    """Toggle favorite status for a product"""
    try:
        product = Product.query.get(product_id)
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        
        product.is_favorite = not product.is_favorite
        db.session.commit()
        
        return jsonify({
            'success': True,
            'product_id': product_id,
            'is_favorite': product.is_favorite
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/favorites', methods=['GET'])
def get_favorites():
    """Get all favorited products"""
    try:
        products = Product.query.filter_by(is_favorite=True).order_by(Product.sales_7d.desc()).all()
        return jsonify({
            'success': True,
            'products': [p.to_dict() for p in products],
            'count': len(products)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/brands', methods=['GET'])
def get_brands():
    """Get list of unique brands/sellers"""
    try:
        brands = db.session.query(
            Product.seller_id,
            Product.seller_name,
            db.func.count(Product.product_id).label('product_count')
        ).group_by(Product.seller_id, Product.seller_name).order_by(db.desc('product_count')).all()
        
        return jsonify({
            'success': True,
            'brands': [{'seller_id': b.seller_id, 'seller_name': b.seller_name, 'product_count': b.product_count} for b in brands]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cleanup', methods=['POST', 'GET'])
def cleanup_products():
    """
    Remove products that aren't affiliate-eligible:
    - 0% commission (not available for affiliates)
    """
    try:
        # Count before cleanup
        total_before = Product.query.count()
        
        # Delete products with 0 commission
        deleted = Product.query.filter(
            db.or_(Product.commission_rate == 0, Product.commission_rate.is_(None))
        ).delete(synchronize_session=False)
        
        db.session.commit()
        
        total_after = Product.query.count()
        
        return jsonify({
            'success': True,
            'message': f'Cleaned up {deleted} products with 0% commission',
            'before': total_before,
            'after': total_after,
            'removed': deleted
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/refresh-product/<product_id>', methods=['POST'])
def refresh_product_data(product_id):
    """Fetch fresh data for a product from EchoTik's product detail API"""
    try:
        product = Product.query.get(product_id)
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        
        # Call EchoTik product detail API
        response = requests.get(
            f"{BASE_URL}/product/detail",
            params={'product_ids': product_id},
            auth=get_auth(),
            timeout=30
        )
        
        if response.status_code != 200:
            return jsonify({'success': False, 'error': f'API returned {response.status_code}'}), 500
        
        data = response.json()
        if data.get('code') != 0 or not data.get('data'):
            return jsonify({'success': False, 'error': 'No data returned from API'}), 500
        
        p = data['data'][0]
        
        # Update product with fresh data
        product.sales = int(p.get('total_sale_cnt', 0) or 0)
        product.sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
        product.sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
        product.gmv = float(p.get('total_sale_gmv_amt', 0) or 0)
        product.gmv_30d = float(p.get('total_sale_gmv_30d_amt', 0) or 0)
        product.influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
        product.commission_rate = float(p.get('product_commission_rate', 0) or 0)
        product.price = float(p.get('spu_avg_price', 0) or 0)
        
        # Video/Live stats
        product.video_count = int(p.get('total_video_cnt', 0) or 0)
        product.video_7d = int(p.get('total_video_7d_cnt', 0) or 0)
        product.video_30d = int(p.get('total_video_30d_cnt', 0) or 0)
        product.live_count = int(p.get('total_live_cnt', 0) or 0)
        product.views_count = int(p.get('total_views_cnt', 0) or 0)
        product.product_rating = float(p.get('product_rating', 0) or 0)
        product.review_count = int(p.get('review_count', 0) or 0)
        
        product.last_updated = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Product data refreshed',
            'product': product.to_dict()
        })
        
    except Exception as e:
        import traceback
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500

# =============================================================================
# DEBUG ENDPOINT
# =============================================================================

@app.route('/api/debug', methods=['GET'])
def debug_api():
    """Debug endpoint to test EchoTik connection and see raw response"""
    try:
        response = requests.get(
            f"{BASE_URL}/seller/list",
            params={
                "page_num": 1, 
                "page_size": 2, 
                "region": "US",
                "seller_sort_field": 2,
                "sort_type": 1
            },
            auth=get_auth(),
            timeout=10
        )
        data = response.json()
        
        return jsonify({
            'debug_info': {
                'url': f"{BASE_URL}/seller/list",
                'params': {"page_num": 1, "page_size": 2, "region": "US", "seller_sort_field": 2, "sort_type": 1},
                'username_set': bool(ECHOTIK_USERNAME),
                'username_preview': ECHOTIK_USERNAME[:3] + '***' if ECHOTIK_USERNAME else 'NOT SET',
                'password_set': bool(ECHOTIK_PASSWORD)
            },
            'response': {
                'status_code': response.status_code,
                'api_code': data.get('code'),
                'message': data.get('message'),
                'brands_count': len(data.get('data', [])),
                'raw_data': data
            }
        })
    except Exception as e:
        return jsonify({
            'debug_info': {
                'username_set': bool(ECHOTIK_USERNAME),
                'password_set': bool(ECHOTIK_PASSWORD)
            },
            'error': str(e)
        })

# =============================================================================
# PWA / STATIC FILES
# =============================================================================

@app.route('/')
def index():
    return send_from_directory('pwa', 'brand_hunter.html')

@app.route('/pwa/<path:filename>')
def pwa_files(filename):
    return send_from_directory('pwa', filename)

@app.route('/api/image-proxy/<product_id>')
def image_proxy(product_id):
    """Proxy product images - fast version without EchoTik API call"""
    product = Product.query.get(product_id)
    if not product or not product.image_url:
        return '', 404
    
    image_url = product.image_url
    
    # Try to fetch the image directly (works for some URLs)
    try:
        response = requests.get(image_url, timeout=5, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.tiktok.com/'
        })
        if response.status_code == 200:
            return response.content, 200, {
                'Content-Type': response.headers.get('Content-Type', 'image/jpeg'),
                'Cache-Control': 'public, max-age=86400'
            }
    except:
        pass
    
    # If direct fetch fails, return 404 (frontend will show placeholder)
    return '', 404

@app.route('/api/init-db', methods=['POST', 'GET'])
def init_database():
    """Initialize database tables and add any missing columns"""
    try:
        db.create_all()
        
        # Try to add missing columns
        columns_to_add = [
            ('sales_7d', 'INTEGER DEFAULT 0'),
            ('cached_image_url', 'TEXT'),
            ('image_cached_at', 'TIMESTAMP'),
            ('video_count', 'INTEGER DEFAULT 0'),
            ('video_7d', 'INTEGER DEFAULT 0'),
            ('video_30d', 'INTEGER DEFAULT 0'),
            ('live_count', 'INTEGER DEFAULT 0'),
            ('views_count', 'INTEGER DEFAULT 0'),
            ('product_rating', 'FLOAT DEFAULT 0'),
            ('review_count', 'INTEGER DEFAULT 0'),
            ('is_favorite', 'BOOLEAN DEFAULT FALSE'),
        ]
        
        added = []
        for col_name, col_type in columns_to_add:
            try:
                db.session.execute(db.text(f'ALTER TABLE products ADD COLUMN {col_name} {col_type}'))
                db.session.commit()
                added.append(col_name)
            except Exception as e:
                db.session.rollback()
                # Column probably already exists
        
        return jsonify({
            'success': True, 
            'message': f'Database initialized. Added columns: {added if added else "none (already exist)"}'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
