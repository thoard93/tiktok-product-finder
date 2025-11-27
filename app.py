"""
TikTok Product Finder - Brand Hunter
Updated: Sort by Influencer Count DESCENDING

Strategy Change:
- OLD: Sort by 30-day sales desc → filter 1-100 influencers
- NEW: Sort by influencer count desc → filter target range (e.g. 50-300)

This finds products that PROVEN affiliates are promoting (high influencer count)
but still have room for more affiliates (not 1000+ saturated).
"""

import os
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
import time

app = Flask(__name__, static_folder='pwa')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///products.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Fix Render's postgres:// URL
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

db = SQLAlchemy(app)

# EchoTik API Config
BASE_URL = "https://open.echotik.live/api/v2"
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')

# =============================================================================
# DATABASE MODELS
# =============================================================================

class Brand(db.Model):
    """Tracked brands/sellers for Brand Hunter"""
    __tablename__ = 'brands'
    
    seller_id = db.Column(db.String(50), primary_key=True)
    seller_name = db.Column(db.String(255))
    gmv = db.Column(db.Float, default=0)
    products_count = db.Column(db.Integer, default=0)
    influencer_count = db.Column(db.Integer, default=0)
    is_followed = db.Column(db.Boolean, default=False)
    last_scanned = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'seller_id': self.seller_id,
            'seller_name': self.seller_name,
            'gmv': self.gmv,
            'products_count': self.products_count,
            'influencer_count': self.influencer_count,
            'is_followed': self.is_followed,
            'last_scanned': self.last_scanned.isoformat() if self.last_scanned else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

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
    scan_type = db.Column(db.String(50), default='general')  # 'brand_hunter' or 'general'
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

def get_auth_token():
    """Get authentication token from EchoTik"""
    try:
        response = requests.post(
            f"{BASE_URL}/login",
            json={
                "username": ECHOTIK_USERNAME,
                "password": ECHOTIK_PASSWORD
            },
            timeout=10
        )
        data = response.json()
        if data.get('code') == 0:
            return data.get('data', {}).get('token')
        else:
            print(f"Auth error: {data}")
            return None
    except Exception as e:
        print(f"Auth exception: {e}")
        return None

def get_seller_products(token, seller_id, page=1, page_size=10, sort_field=5, sort_type=1):
    """
    Get products from a seller/shop
    
    Sort Fields (seller_product_sort_field):
        1 = Total Sales
        2 = 7-day GMV
        3 = Total GMV
        4 = 30-day Sales
        5 = Influencers  <-- DEFAULT NOW
    
    Sort Type (seller_product_sort_type):
        1 = Descending (highest first) <-- DEFAULT
        2 = Ascending (lowest first)
    """
    try:
        response = requests.post(
            f"{BASE_URL}/seller/product/list",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "seller_id": seller_id,
                "page_number": page,
                "page_size": page_size,
                "region": "US",
                "seller_product_sort_field": sort_field,  # 5 = Influencers
                "seller_product_sort_type": sort_type      # 1 = Descending
            },
            timeout=30
        )
        data = response.json()
        if data.get('code') == 0:
            return data.get('data', {})
        else:
            print(f"Seller products error: {data}")
            return None
    except Exception as e:
        print(f"Seller products exception: {e}")
        return None

def get_product_influencer_count(token, product_id):
    """Get accurate influencer count for a product"""
    try:
        response = requests.post(
            f"{BASE_URL}/product/author/list",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "page_number": 1,
                "page_size": 1,
                "product_id": product_id,
                "region": "US"
            },
            timeout=10
        )
        data = response.json()
        if data.get('code') == 0:
            return data.get('data', {}).get('total', 0)
        return 0
    except Exception as e:
        print(f"Influencer count error: {e}")
        return 0

def discover_brands(token, page=1, sort_by='gmv'):
    """Discover top brands/sellers from EchoTik"""
    try:
        # Sort field for seller list
        sort_field = 3  # GMV
        if sort_by == 'products':
            sort_field = 4
        elif sort_by == 'influencers':
            sort_field = 5
            
        response = requests.post(
            f"{BASE_URL}/seller/list",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "page_number": page,
                "page_size": 20,
                "region": "US",
                "seller_sort_field": sort_field,
                "seller_sort_type": 1  # Descending
            },
            timeout=30
        )
        data = response.json()
        if data.get('code') == 0:
            return data.get('data', {}).get('list', [])
        return []
    except Exception as e:
        print(f"Discover brands error: {e}")
        return []

# =============================================================================
# BRAND HUNTER API ROUTES
# =============================================================================

@app.route('/api/brands', methods=['GET'])
def list_brands():
    """List all tracked brands"""
    followed_only = request.args.get('followed', 'false').lower() == 'true'
    
    query = Brand.query
    if followed_only:
        query = query.filter_by(is_followed=True)
    
    brands = query.order_by(Brand.gmv.desc()).all()
    return jsonify({
        'brands': [b.to_dict() for b in brands],
        'total': len(brands)
    })

@app.route('/api/brands/discover', methods=['GET'])
def discover_brands_route():
    """Discover new brands from EchoTik"""
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort', 'gmv')
    
    token = get_auth_token()
    if not token:
        return jsonify({'error': 'Authentication failed'}), 500
    
    brands_data = discover_brands(token, page, sort_by)
    
    added = 0
    for b in brands_data:
        seller_id = b.get('seller_id', '')
        if not seller_id:
            continue
            
        existing = Brand.query.get(seller_id)
        if not existing:
            brand = Brand(
                seller_id=seller_id,
                seller_name=b.get('seller_name', 'Unknown'),
                gmv=float(b.get('gmv', 0) or 0),
                products_count=int(b.get('product_num', 0) or 0),
                influencer_count=int(b.get('author_num', 0) or 0),
                is_followed=False
            )
            db.session.add(brand)
            added += 1
        else:
            # Update existing
            existing.gmv = float(b.get('gmv', 0) or 0)
            existing.products_count = int(b.get('product_num', 0) or 0)
            existing.influencer_count = int(b.get('author_num', 0) or 0)
    
    db.session.commit()
    
    return jsonify({
        'discovered': len(brands_data),
        'added': added,
        'brands': brands_data
    })

@app.route('/api/brands/<seller_id>/follow', methods=['POST'])
def follow_brand(seller_id):
    """Follow a brand to include in scans"""
    brand = Brand.query.get(seller_id)
    if not brand:
        return jsonify({'error': 'Brand not found'}), 404
    
    brand.is_followed = True
    db.session.commit()
    
    return jsonify({'success': True, 'brand': brand.to_dict()})

@app.route('/api/brands/<seller_id>/unfollow', methods=['POST'])
def unfollow_brand(seller_id):
    """Unfollow a brand"""
    brand = Brand.query.get(seller_id)
    if not brand:
        return jsonify({'error': 'Brand not found'}), 404
    
    brand.is_followed = False
    db.session.commit()
    
    return jsonify({'success': True, 'brand': brand.to_dict()})

@app.route('/api/brands/stats', methods=['GET'])
def brand_stats():
    """Get Brand Hunter statistics"""
    total_brands = Brand.query.count()
    followed_brands = Brand.query.filter_by(is_followed=True).count()
    total_products = Product.query.filter_by(scan_type='brand_hunter').count()
    
    # Get competition breakdown
    untapped = Product.query.filter(
        Product.scan_type == 'brand_hunter',
        Product.influencer_count >= 1,
        Product.influencer_count <= 50
    ).count()
    
    low_comp = Product.query.filter(
        Product.scan_type == 'brand_hunter',
        Product.influencer_count >= 51,
        Product.influencer_count <= 100
    ).count()
    
    medium_comp = Product.query.filter(
        Product.scan_type == 'brand_hunter',
        Product.influencer_count >= 101,
        Product.influencer_count <= 200
    ).count()
    
    high_comp = Product.query.filter(
        Product.scan_type == 'brand_hunter',
        Product.influencer_count >= 201,
        Product.influencer_count <= 500
    ).count()
    
    return jsonify({
        'total_brands': total_brands,
        'followed_brands': followed_brands,
        'total_products': total_products,
        'competition_breakdown': {
            'untapped_1_50': untapped,
            'low_51_100': low_comp,
            'medium_101_200': medium_comp,
            'high_201_500': high_comp
        }
    })

@app.route('/api/brands/scan-next', methods=['GET'])
def scan_next_brand():
    """
    Scan the next brand in queue
    
    NEW STRATEGY: Sort by INFLUENCER COUNT DESCENDING
    - Shows products with most affiliates first
    - Filter for target range (e.g. 50-300 influencers)
    - These are proven products with room for more affiliates
    
    Parameters:
        pages: Number of pages to scan (default: 10)
        min_influencers: Minimum influencer count (default: 50)
        max_influencers: Maximum influencer count (default: 300)
        min_sales: Minimum 30d sales required (default: 1)
        start_page: Which page to start from (default: 1)
    """
    pages = request.args.get('pages', 10, type=int)
    min_influencers = request.args.get('min_influencers', 50, type=int)
    max_influencers = request.args.get('max_influencers', 300, type=int)
    min_sales = request.args.get('min_sales', 1, type=int)
    start_page = request.args.get('start_page', 1, type=int)
    
    # Get next brand to scan (oldest scanned or never scanned)
    brand = Brand.query.filter_by(is_followed=True).order_by(
        Brand.last_scanned.asc().nullsfirst()
    ).first()
    
    if not brand:
        return jsonify({'error': 'No followed brands to scan'}), 404
    
    token = get_auth_token()
    if not token:
        return jsonify({'error': 'Authentication failed'}), 500
    
    products_found = []
    products_saved = 0
    
    for page in range(start_page, start_page + pages):
        print(f"Scanning {brand.seller_name} page {page}...")
        
        # Sort by influencers descending (field 5, type 1)
        data = get_seller_products(
            token, 
            brand.seller_id, 
            page=page, 
            page_size=10,
            sort_field=5,   # Influencers
            sort_type=1     # Descending
        )
        
        if not data or not data.get('list'):
            print(f"  No more products at page {page}")
            break
        
        products = data.get('list', [])
        
        for p in products:
            product_id = p.get('product_id', '')
            if not product_id:
                continue
            
            # Get influencer count from the API response
            influencer_count = int(p.get('author_cnt', 0) or p.get('total_ifl_cnt', 0) or 0)
            sales_30d = int(p.get('sold_cnt_30d', 0) or 0)
            
            # Filter: Must be in target influencer range AND have sales
            if influencer_count < min_influencers or influencer_count > max_influencers:
                continue
            if sales_30d < min_sales:
                continue
            
            # This is a gem! Save it
            existing = Product.query.get(product_id)
            if existing:
                # Update existing
                existing.influencer_count = influencer_count
                existing.sales_30d = sales_30d
                existing.last_updated = datetime.utcnow()
            else:
                # Create new
                product = Product(
                    product_id=product_id,
                    product_name=p.get('product_name', ''),
                    seller_id=brand.seller_id,
                    seller_name=brand.seller_name,
                    gmv=float(p.get('gmv', 0) or 0),
                    gmv_30d=float(p.get('gmv_30d', 0) or 0),
                    sales=int(p.get('sold_cnt', 0) or 0),
                    sales_30d=sales_30d,
                    influencer_count=influencer_count,
                    commission_rate=float(p.get('commission_rate', 0) or 0),
                    price=float(p.get('price', 0) or 0) / 100,  # Convert cents to dollars
                    image_url=p.get('image_url', ''),
                    scan_type='brand_hunter'
                )
                db.session.add(product)
                products_saved += 1
            
            products_found.append({
                'product_id': product_id,
                'product_name': p.get('product_name', ''),
                'influencer_count': influencer_count,
                'sales_30d': sales_30d
            })
        
        # Small delay between pages
        time.sleep(0.5)
    
    # Update brand's last_scanned timestamp
    brand.last_scanned = datetime.utcnow()
    db.session.commit()
    
    return jsonify({
        'brand': brand.to_dict(),
        'pages_scanned': pages,
        'products_found': len(products_found),
        'products_saved': products_saved,
        'filter_settings': {
            'min_influencers': min_influencers,
            'max_influencers': max_influencers,
            'min_sales': min_sales,
            'sort': 'influencers_descending'
        },
        'products': products_found[:20]  # Return first 20 for preview
    })

@app.route('/api/brands/<seller_id>/deep-scan', methods=['GET'])
def deep_scan_brand(seller_id):
    """
    Deep scan a specific brand (for large catalogs like QVC)
    Sorted by influencer count descending
    """
    pages = request.args.get('pages', 100, type=int)
    start_page = request.args.get('start_page', 1, type=int)
    min_influencers = request.args.get('min_influencers', 50, type=int)
    max_influencers = request.args.get('max_influencers', 300, type=int)
    min_sales = request.args.get('min_sales', 1, type=int)
    
    brand = Brand.query.get(seller_id)
    if not brand:
        return jsonify({'error': 'Brand not found'}), 404
    
    token = get_auth_token()
    if not token:
        return jsonify({'error': 'Authentication failed'}), 500
    
    products_found = []
    products_saved = 0
    
    for page in range(start_page, start_page + pages):
        if page % 10 == 0:
            print(f"Deep scanning {brand.seller_name} page {page}...")
        
        data = get_seller_products(
            token, 
            brand.seller_id, 
            page=page, 
            page_size=10,
            sort_field=5,   # Influencers
            sort_type=1     # Descending
        )
        
        if not data or not data.get('list'):
            print(f"  No more products at page {page}")
            break
        
        products = data.get('list', [])
        
        for p in products:
            product_id = p.get('product_id', '')
            if not product_id:
                continue
            
            influencer_count = int(p.get('author_cnt', 0) or p.get('total_ifl_cnt', 0) or 0)
            sales_30d = int(p.get('sold_cnt_30d', 0) or 0)
            
            # Once we hit products below min_influencers, we can stop
            # (since sorted by influencers desc, remaining will be lower)
            if influencer_count < min_influencers:
                print(f"  Hit min influencer threshold at page {page}")
                break
            
            # Skip if above max
            if influencer_count > max_influencers:
                continue
            
            if sales_30d < min_sales:
                continue
            
            existing = Product.query.get(product_id)
            if existing:
                existing.influencer_count = influencer_count
                existing.sales_30d = sales_30d
                existing.last_updated = datetime.utcnow()
            else:
                product = Product(
                    product_id=product_id,
                    product_name=p.get('product_name', ''),
                    seller_id=brand.seller_id,
                    seller_name=brand.seller_name,
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
            
            products_found.append({
                'product_id': product_id,
                'product_name': p.get('product_name', ''),
                'influencer_count': influencer_count,
                'sales_30d': sales_30d
            })
        
        # Check if we should stop early
        if data.get('list') and len(data.get('list', [])) > 0:
            last_product = data['list'][-1]
            last_influencers = int(last_product.get('author_cnt', 0) or 0)
            if last_influencers < min_influencers:
                print(f"  All remaining products below threshold, stopping")
                break
        
        time.sleep(0.3)
    
    brand.last_scanned = datetime.utcnow()
    db.session.commit()
    
    return jsonify({
        'brand': brand.to_dict(),
        'pages_scanned': page - start_page + 1,
        'products_found': len(products_found),
        'products_saved': products_saved,
        'filter_settings': {
            'min_influencers': min_influencers,
            'max_influencers': max_influencers,
            'min_sales': min_sales,
            'sort': 'influencers_descending'
        }
    })

@app.route('/api/brands/products', methods=['GET'])
def get_brand_products():
    """Get all products from Brand Hunter"""
    min_influencers = request.args.get('min_influencers', 1, type=int)
    max_influencers = request.args.get('max_influencers', 500, type=int)
    limit = request.args.get('limit', 100, type=int)
    
    products = Product.query.filter(
        Product.scan_type == 'brand_hunter',
        Product.influencer_count >= min_influencers,
        Product.influencer_count <= max_influencers
    ).order_by(Product.influencer_count.desc()).limit(limit).all()
    
    return jsonify({
        'products': [p.to_dict() for p in products],
        'total': len(products),
        'filters': {
            'min_influencers': min_influencers,
            'max_influencers': max_influencers
        }
    })

# =============================================================================
# PWA / STATIC FILES
# =============================================================================

@app.route('/')
def index():
    return send_from_directory('pwa', 'brand_hunter.html')

@app.route('/brand-hunter')
def brand_hunter():
    return send_from_directory('pwa', 'brand_hunter.html')

@app.route('/pwa/<path:filename>')
def pwa_files(filename):
    return send_from_directory('pwa', filename)

# =============================================================================
# IMAGE PROXY
# =============================================================================

@app.route('/api/image-proxy/<product_id>')
def image_proxy(product_id):
    """Proxy product images to avoid CORS issues"""
    product = Product.query.get(product_id)
    if not product or not product.image_url:
        return '', 404
    
    try:
        response = requests.get(product.image_url, timeout=10)
        return response.content, 200, {'Content-Type': response.headers.get('Content-Type', 'image/jpeg')}
    except:
        return '', 404

# =============================================================================
# DATABASE INIT
# =============================================================================

@app.route('/api/init-db', methods=['POST'])
def init_database():
    """Initialize database tables"""
    db.create_all()
    return jsonify({'success': True, 'message': 'Database initialized'})

# Create tables on startup
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
