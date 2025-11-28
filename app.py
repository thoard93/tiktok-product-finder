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
from datetime import datetime
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
            'image_url': self.image_url,
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
        brands: Number of top brands to scan (default: 5)
        pages_per_brand: Pages to scan per brand (default: 10)
        min_influencers: Minimum influencer count (default: 1)
        max_influencers: Maximum influencer count (default: 100)
        min_sales: Minimum total sales (default: 10)
    """
    try:
        num_brands = request.args.get('brands', 5, type=int)
        pages_per_brand = request.args.get('pages_per_brand', 10, type=int)
        min_influencers = request.args.get('min_influencers', 1, type=int)
        max_influencers = request.args.get('max_influencers', 100, type=int)
        min_sales = request.args.get('min_sales', 10, type=int)
        
        # Get top brands
        print(f"Fetching top {num_brands} brands...")
        brands = get_top_brands(page=1)
        
        if not brands:
            return jsonify({'error': 'Failed to fetch brands - check EchoTik credentials'}), 500
        
        brands = brands[:num_brands]
        
        results = {
            'brands_scanned': [],
            'total_products_found': 0,
            'total_products_saved': 0,
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
                    
                    # Filter: Must be in target influencer range AND have recent sales
                    if influencer_count < min_influencers or influencer_count > max_influencers:
                        continue
                    if sales_7d < min_sales:  # Filter by 7-day sales, not total
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
                            scan_type='brand_hunter'
                        )
                        db.session.add(product)
                        brand_result['products_saved'] += 1
                
                time.sleep(0.3)
            
            results['brands_scanned'].append(brand_result)
            results['total_products_found'] += brand_result['products_found']
            results['total_products_saved'] += brand_result['products_saved']
            
            print(f"  ✅ Scanned: {brand_result['products_scanned']}, Found: {brand_result['products_found']}, Saved: {brand_result['products_saved']}")
        
        db.session.commit()
        
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
    """Get all saved products"""
    min_influencers = request.args.get('min_influencers', 1, type=int)
    max_influencers = request.args.get('max_influencers', 500, type=int)
    limit = request.args.get('limit', 200, type=int)
    
    products = Product.query.filter(
        Product.influencer_count >= min_influencers,
        Product.influencer_count <= max_influencers
    ).order_by(Product.sales.desc()).limit(limit).all()
    
    return jsonify({
        'products': [p.to_dict() for p in products],
        'total': len(products)
    })

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

@app.route('/api/clear', methods=['POST'])
def clear_products():
    """Clear all products"""
    Product.query.delete()
    db.session.commit()
    return jsonify({'success': True, 'message': 'All products cleared'})

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
    """Proxy product images using EchoTik's batch cover download API"""
    product = Product.query.get(product_id)
    if not product or not product.image_url:
        return '', 404
    
    image_url = product.image_url
    
    # If URL is from EchoTik's image server, try to get signed URL
    if image_url and 'echosell-images' in image_url:
        try:
            response = requests.get(
                f"{BASE_URL}/batch/cover/download",
                params={'cover_urls': image_url},
                auth=get_auth(),
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('code') == 0 and data.get('data'):
                    for item in data['data']:
                        if isinstance(item, dict):
                            for orig_url, signed_url in item.items():
                                if signed_url and signed_url.startswith('http'):
                                    image_url = signed_url
                                    break
        except Exception as e:
            print(f"Image API error: {e}")
    
    # Fetch the image
    try:
        response = requests.get(image_url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.tiktok.com/'
        })
        if response.status_code == 200:
            return response.content, 200, {
                'Content-Type': response.headers.get('Content-Type', 'image/jpeg'),
                'Cache-Control': 'public, max-age=3600'
            }
    except:
        pass
    
    return '', 404

@app.route('/api/init-db', methods=['POST', 'GET'])
def init_database():
    """Initialize database tables and add any missing columns"""
    try:
        db.create_all()
        
        # Try to add sales_7d column if it doesn't exist
        try:
            db.session.execute(db.text('ALTER TABLE products ADD COLUMN sales_7d INTEGER DEFAULT 0'))
            db.session.commit()
            return jsonify({'success': True, 'message': 'Database initialized and sales_7d column added'})
        except Exception as e:
            db.session.rollback()
            # Column probably already exists
            return jsonify({'success': True, 'message': 'Database initialized (sales_7d column already exists)'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
