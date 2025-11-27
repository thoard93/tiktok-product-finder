"""
TikTok Product Finder - Brand Hunter (Simplified)
Scans TOP BRANDS directly - no follow workflow needed

Strategy: 
- Get top brands by GMV from EchoTik
- Scan their products sorted by INFLUENCER COUNT DESCENDING
- Filter for target range (50-300 influencers)
- Save hidden gems automatically
"""

import os
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_sqlalchemy import SQLAlchemy
import time

app = Flask(__name__, static_folder='pwa')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///products.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Fix Render's postgres:// URL
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

db = SQLAlchemy(app)

# EchoTik API Config
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
    sales_30d = db.Column(db.Integer, default=0)
    influencer_count = db.Column(db.Integer, default=0)
    commission_rate = db.Column(db.Float, default=0)
    price = db.Column(db.Float, default=0)
    image_url = db.Column(db.String(500))
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
            'sales_30d': self.sales_30d,
            'influencer_count': self.influencer_count,
            'commission_rate': self.commission_rate,
            'price': self.price,
            'image_url': self.image_url,
            'scan_type': self.scan_type,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None
        }

# =============================================================================
# ECHOTIK API HELPERS
# =============================================================================

def get_auth():
    """Get HTTPBasicAuth for EchoTik API v3"""
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)

def get_top_brands(page=1):
    """Get top brands/sellers sorted by GMV"""
    try:
        url = f"{BASE_URL}/seller/list"
        params = {
            "page_num": page,
            "page_size": 20,
            "region": "US",
            "seller_sort_field": 3,  # GMV
            "seller_sort_type": 1    # Descending
        }
        print(f"Calling API: {url} with params: {params}")
        response = requests.get(
            url,
            auth=get_auth(),
            params=params,
            timeout=30
        )
        print(f"Response status: {response.status_code}")
        data = response.json()
        if data.get('code') == 0:
            brands = data.get('data', {}).get('list', [])
            print(f"Got {len(brands)} brands")
            return brands
        print(f"Get brands error: {data}")
        return []
    except Exception as e:
        print(f"Get brands exception: {e}")
        return []

def get_seller_products(seller_id, page=1, page_size=10):
    """
    Get products from a seller sorted by INFLUENCER COUNT DESCENDING

    Sort Fields (seller_product_sort_field):
        1 = Total Sales
        2 = 7-day GMV
        3 = Total GMV
        4 = 30-day Sales
        5 = Influencers  <-- USING THIS

    Sort Type:
        1 = Descending (highest first) <-- USING THIS
        2 = Ascending
    """
    try:
        response = requests.get(
            f"{BASE_URL}/seller/product/list",
            auth=get_auth(),
            params={
                "seller_id": seller_id,
                "page_num": page,
                "page_size": page_size,
                "region": "US",
                "seller_product_sort_field": 5,  # INFLUENCERS
                "seller_product_sort_type": 1    # DESCENDING
            },
            timeout=30
        )
        data = response.json()
        if data.get('code') == 0:
            return data.get('data', {})
        print(f"Seller products error for {seller_id}: {data}")
        return None
    except Exception as e:
        print(f"Seller products exception: {e}")
        return None

# =============================================================================
# MAIN SCANNING ENDPOINTS
# =============================================================================

@app.route('/api/scan', methods=['GET'])
def scan_top_brands():
    """
    Main scanning endpoint - scans top brands for hidden gems
    
    Parameters:
        brands: Number of top brands to scan (default: 5)
        pages_per_brand: Pages to scan per brand (default: 10)
        min_influencers: Minimum influencer count (default: 50)
        max_influencers: Maximum influencer count (default: 300)
        min_sales: Minimum 30d sales (default: 1)
    """
    num_brands = request.args.get('brands', 5, type=int)
    pages_per_brand = request.args.get('pages_per_brand', 10, type=int)
    min_influencers = request.args.get('min_influencers', 1, type=int)
    max_influencers = request.args.get('max_influencers', 100, type=int)
    min_sales = request.args.get('min_sales', 1, type=int)

    # Get top brands
    print(f"Fetching top {num_brands} brands...")
    brands = get_top_brands(page=1)
    
    if not brands:
        return jsonify({'error': 'Failed to fetch brands'}), 500
    
    brands = brands[:num_brands]
    
    results = {
        'brands_scanned': [],
        'total_products_found': 0,
        'total_products_saved': 0,
        'filter_settings': {
            'min_influencers': min_influencers,
            'max_influencers': max_influencers,
            'min_sales': min_sales,
            'sort': 'influencers_descending'
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
            'products_found': 0,
            'products_saved': 0
        }
        
        hit_threshold = False
        
        for page in range(1, pages_per_brand + 1):
            data = get_seller_products(seller_id, page=page)
            
            if not data or not data.get('list'):
                print(f"  No more products at page {page}")
                break
            
            products = data.get('list', [])
            
            for p in products:
                product_id = p.get('product_id', '')
                if not product_id:
                    continue
                
                # Get influencer count
                influencer_count = int(p.get('author_cnt', 0) or p.get('total_ifl_cnt', 0) or 0)
                sales_30d = int(p.get('sold_cnt_30d', 0) or 0)
                
                # Early exit: if we're below min_influencers and sorted desc, stop
                if influencer_count < min_influencers:
                    print(f"  Hit min threshold ({influencer_count} < {min_influencers}), moving to next brand")
                    hit_threshold = True
                    break
                
                # Skip if above max
                if influencer_count > max_influencers:
                    continue
                
                # Check sales
                if sales_30d < min_sales:
                    continue
                
                brand_result['products_found'] += 1
                
                # Save to database
                existing = Product.query.get(product_id)
                if existing:
                    existing.influencer_count = influencer_count
                    existing.sales_30d = sales_30d
                    existing.last_updated = datetime.utcnow()
                else:
                    product = Product(
                        product_id=product_id,
                        product_name=p.get('product_name', ''),
                        seller_id=seller_id,
                        seller_name=seller_name,
                        gmv=float(p.get('gmv', 0) or 0),
                        gmv_30d=float(p.get('gmv_30d', 0) or 0),
                        sales=int(p.get('sold_cnt', 0) or 0),
                        sales_30d=sales_30d,
                        influencer_count=influencer_count,
                        commission_rate=float(p.get('commission_rate', 0) or 0),
                        price=float(p.get('price', 0) or 0) / 100,
                        image_url=p.get('image_url', ''),
                        scan_type='brand_hunter'
                    )
                    db.session.add(product)
                    brand_result['products_saved'] += 1
            
            if hit_threshold:
                break
            
            time.sleep(0.3)
        
        results['brands_scanned'].append(brand_result)
        results['total_products_found'] += brand_result['products_found']
        results['total_products_saved'] += brand_result['products_saved']
        
        print(f"  ✅ Found: {brand_result['products_found']}, Saved: {brand_result['products_saved']}")
    
    db.session.commit()
    
    return jsonify(results)

@app.route('/api/scan-brand/<seller_id>', methods=['GET'])
def scan_single_brand(seller_id):
    """
    Deep scan a specific brand by seller_id
    """
    pages = request.args.get('pages', 50, type=int)
    min_influencers = request.args.get('min_influencers', 1, type=int)
    max_influencers = request.args.get('max_influencers', 100, type=int)
    min_sales = request.args.get('min_sales', 1, type=int)

    products_found = 0
    products_saved = 0
    seller_name = "Unknown"
    pages_actually_scanned = 0
    
    for page in range(1, pages + 1):
        pages_actually_scanned = page
        
        if page % 10 == 0:
            print(f"Scanning page {page}...")
        
        data = get_seller_products(seller_id, page=page)
        
        if not data or not data.get('list'):
            break
        
        products = data.get('list', [])
        
        for p in products:
            product_id = p.get('product_id', '')
            if not product_id:
                continue
            
            if seller_name == "Unknown":
                seller_name = p.get('seller_name', 'Unknown')
            
            influencer_count = int(p.get('author_cnt', 0) or p.get('total_ifl_cnt', 0) or 0)
            sales_30d = int(p.get('sold_cnt_30d', 0) or 0)
            
            # Stop if below threshold (sorted desc)
            if influencer_count < min_influencers:
                db.session.commit()
                return jsonify({
                    'seller_id': seller_id,
                    'seller_name': seller_name,
                    'pages_scanned': pages_actually_scanned,
                    'products_found': products_found,
                    'products_saved': products_saved,
                    'stopped_reason': f'Hit min threshold at {influencer_count} influencers'
                })
            
            if influencer_count > max_influencers:
                continue
            
            if sales_30d < min_sales:
                continue
            
            products_found += 1
            
            existing = Product.query.get(product_id)
            if not existing:
                product = Product(
                    product_id=product_id,
                    product_name=p.get('product_name', ''),
                    seller_id=seller_id,
                    seller_name=seller_name,
                    gmv=float(p.get('gmv', 0) or 0),
                    gmv_30d=float(p.get('gmv_30d', 0) or 0),
                    sales=int(p.get('sold_cnt', 0) or 0),
                    sales_30d=sales_30d,
                    influencer_count=influencer_count,
                    commission_rate=float(p.get('commission_rate', 0) or 0),
                    price=float(p.get('price', 0) or 0) / 100,
                    image_url=p.get('image_url', ''),
                    scan_type='brand_hunter'
                )
                db.session.add(product)
                products_saved += 1
        
        time.sleep(0.3)
    
    db.session.commit()
    
    return jsonify({
        'seller_id': seller_id,
        'seller_name': seller_name,
        'pages_scanned': pages_actually_scanned,
        'products_found': products_found,
        'products_saved': products_saved
    })

@app.route('/api/brands/list', methods=['GET'])
def list_top_brands():
    """Get list of top brands from EchoTik"""
    page = request.args.get('page', 1, type=int)

    brands = get_top_brands(page=page)
    
    return jsonify({
        'brands': [{
            'seller_id': b.get('seller_id'),
            'seller_name': b.get('seller_name'),
            'gmv': b.get('gmv', 0),
            'products_count': b.get('product_num', 0),
            'influencer_count': b.get('author_num', 0)
        } for b in brands],
        'page': page
    })

@app.route('/api/debug', methods=['GET'])
def debug_api():
    """Debug endpoint to see raw EchoTik API response"""
    try:
        url = f"{BASE_URL}/seller/list"
        params = {
            "page_num": 1,
            "page_size": 20,
            "region": "US",
            "seller_sort_field": 3,
            "seller_sort_type": 1
        }
        response = requests.get(
            url,
            auth=get_auth(),
            params=params,
            timeout=30
        )
        return jsonify({
            'debug_info': {
                'url': url,
                'params': params,
                'auth_username': ECHOTIK_USERNAME[:3] + '***' if ECHOTIK_USERNAME else 'NOT SET',
                'auth_password_set': bool(ECHOTIK_PASSWORD)
            },
            'response': {
                'status_code': response.status_code,
                'raw_data': response.json()
            }
        })
    except Exception as e:
        return jsonify({
            'error': str(e),
            'debug_info': {
                'url': f"{BASE_URL}/seller/list",
                'auth_username': ECHOTIK_USERNAME[:3] + '***' if ECHOTIK_USERNAME else 'NOT SET',
                'auth_password_set': bool(ECHOTIK_PASSWORD)
            }
        }), 500

# =============================================================================
# PRODUCTS ENDPOINTS
# =============================================================================

@app.route('/api/products', methods=['GET'])
def get_products():
    """Get all saved products"""
    min_influencers = request.args.get('min_influencers', 1, type=int)
    max_influencers = request.args.get('max_influencers', 500, type=int)
    limit = request.args.get('limit', 200, type=int)
    
    products = Product.query.filter(
        Product.influencer_count >= min_influencers,
        Product.influencer_count <= max_influencers
    ).order_by(Product.influencer_count.desc()).limit(limit).all()
    
    return jsonify({
        'products': [p.to_dict() for p in products],
        'total': len(products)
    })

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get scanning statistics"""
    total = Product.query.count()
    
    # New ranges for 1-100 strategy
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

@app.route('/api/clear', methods=['POST'])
def clear_products():
    """Clear all products"""
    Product.query.delete()
    db.session.commit()
    return jsonify({'success': True, 'message': 'All products cleared'})

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
    """Proxy product images"""
    product = Product.query.get(product_id)
    if not product or not product.image_url:
        return '', 404
    
    try:
        response = requests.get(product.image_url, timeout=10)
        return response.content, 200, {'Content-Type': response.headers.get('Content-Type', 'image/jpeg')}
    except:
        return '', 404

@app.route('/api/init-db', methods=['POST'])
def init_database():
    """Initialize database tables"""
    db.create_all()
    return jsonify({'success': True, 'message': 'Database initialized'})

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
