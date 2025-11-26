"""
TikTok Shop Product Finder - Enhanced Version 3.0
Fixes display issues + adds new features

New Features:
- Momentum Score calculation
- Trend detection (rising/stable/falling)
- Influencer count filtering (finally working!)
- Category filtering
- Competition analysis
- Smart sorting options
"""

from flask import Flask, render_template, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text, func, desc, asc
from decimal import Decimal
import os
from dotenv import load_dotenv
import json
from datetime import datetime, timedelta
import time
import requests
from requests.auth import HTTPBasicAuth


def extract_image_url(data):
    """Extract clean image URL from string or JSON array"""
    if not data:
        return ''
    if isinstance(data, str):
        if data.startswith('http'):
            return data
        if data.startswith('['):
            try:
                import json
                arr = json.loads(data)
                if arr:
                    arr.sort(key=lambda x: x.get('index', 0))
                    return arr[0].get('url', '')
            except:
                pass
    if isinstance(data, list) and data:
        data.sort(key=lambda x: x.get('index', 0))
        return data[0].get('url', '')
    return ''


load_dotenv()

app = Flask(__name__)
CORS(app)

# Database configuration
db_url = os.getenv('DATABASE_URL', '')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ============================================================================
# ENHANCED DATABASE MODEL - Now captures more API fields
# ============================================================================

class Product(db.Model):
    __tablename__ = 'product'
    
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(50), unique=True)
    product_name = db.Column(db.String(500))
    
    # GMV & Sales - now with time ranges
    gmv = db.Column(db.Numeric(15, 2))  # total_sale_gmv_amt
    gmv_7d = db.Column(db.Numeric(15, 2))  # NEW: 7-day GMV
    gmv_30d = db.Column(db.Numeric(15, 2))  # NEW: 30-day GMV
    
    sales = db.Column(db.Integer)  # total_sale_cnt
    sales_7d = db.Column(db.Integer)  # NEW: 7-day sales
    sales_30d = db.Column(db.Integer)  # NEW: 30-day sales
    
    # Commission
    commission_rate = db.Column(db.Numeric(5, 2))
    potential_earnings = db.Column(db.Numeric(15, 2))
    
    # NEW: Influencer & Competition data
    influencer_count = db.Column(db.Integer)  # total_ifl_cnt - THE KEY FIELD!
    video_count = db.Column(db.Integer)  # total_video_cnt
    live_count = db.Column(db.Integer)  # total_live_cnt
    
    # NEW: Product metrics
    price = db.Column(db.Numeric(10, 2))
    rating = db.Column(db.Numeric(3, 2))  # product_rating
    review_count = db.Column(db.Integer)
    
    # NEW: Trend & Status
    sales_trend = db.Column(db.Integer)  # 0=stable, 1=up, 2=down
    listing_date = db.Column(db.String(20))  # first_crawl_dt
    is_delisted = db.Column(db.Boolean, default=False)
    free_shipping = db.Column(db.Boolean, default=False)
    discount = db.Column(db.String(20))
    
    # NEW: Category info
    category_id = db.Column(db.String(20))
    category_l2_id = db.Column(db.String(20))
    category_l3_id = db.Column(db.String(20))
    
    # Seller info
    seller_id = db.Column(db.String(50))
    seller_name = db.Column(db.String(200))
    
    # Media
    image_url = db.Column(db.Text)
    
    # Image Caching - for EchoTik batch cover download API
    cached_image_url = db.Column(db.Text)           # Temporary accessible URL from EchoTik
    image_cached_at = db.Column(db.DateTime)        # When the cached URL was fetched
    
    # Scan tracking
    scan_id = db.Column(db.Integer, db.ForeignKey('product_scan.id'))
    scan_type = db.Column(db.String(50))  # 'brand_hunter', 'general', 'category'
    
    # NEW: Calculated fields (updated on scan)
    momentum_score = db.Column(db.Numeric(5, 2))  # Our custom score!
    competition_level = db.Column(db.String(20))  # 'low', 'medium', 'high'
    opportunity_score = db.Column(db.Numeric(5, 2))  # Combined ranking
    
    # Time tracking for daily scans
    first_seen = db.Column(db.DateTime, default=datetime.utcnow)  # When product was first discovered
    total_influencers = db.Column(db.Integer)  # Alias for influencer_count
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def calculate_momentum(self):
        """Calculate momentum score based on 7d vs 30d performance"""
        if not self.sales_30d or self.sales_30d == 0:
            return 0
        
        # 7-day sales as percentage of 30-day (ideally >23% = accelerating)
        weekly_ratio = (self.sales_7d or 0) / self.sales_30d
        
        # Score: >30% of monthly in 7 days = hot, <15% = cooling
        if weekly_ratio > 0.35:
            return 100  # 🔥 On fire
        elif weekly_ratio > 0.28:
            return 80   # 📈 Accelerating
        elif weekly_ratio > 0.23:
            return 60   # ✅ Healthy
        elif weekly_ratio > 0.15:
            return 40   # ⚠️ Slowing
        else:
            return 20   # ❄️ Cooling
    
    def get_competition_level(self):
        """Determine competition level based on influencer count"""
        count = self.influencer_count or 0
        if count <= 3:
            return 'untapped'      # 🏆 Golden opportunity
        elif count <= 10:
            return 'low'           # ✅ Easy entry
        elif count <= 30:
            return 'medium'        # ⚡ Competitive but doable
        elif count <= 50:
            return 'high'          # ⚠️ Crowded
        else:
            return 'saturated'     # ❌ Very hard to rank
    
    def to_dict(self):
        """Safe serialization with field mapping"""
        def safe_float(val):
            if val is None:
                return None
            if isinstance(val, Decimal):
                return float(val)
            return val
        
        def safe_int(val):
            return int(val) if val is not None else None
        
        # Calculate dynamic fields
        momentum = self.calculate_momentum()
        competition = self.get_competition_level()
        
        return {
            'id': self.id,
            'product_id': self.product_id,
            'product_name': self.product_name or 'Unknown Product',
            'title': self.product_name or 'Unknown Product',  # Alias for JS compatibility
            
            # Sales data
            'gmv': safe_float(self.gmv) or 0,
            'gmv_7d': safe_float(self.gmv_7d),
            'gmv_30d': safe_float(self.gmv_30d),
            'sales': safe_int(self.sales) or 0,
            'sales_7d': safe_int(self.sales_7d),
            'sales_30d': safe_int(self.sales_30d),
            
            # Commission & Earnings
            'commission_rate': safe_float(self.commission_rate) or 0,
            'potential_earnings': safe_float(self.potential_earnings) or 0,
            
            # Competition metrics
            'influencer_count': safe_int(self.influencer_count) or 0,
            'total_ifl_cnt': safe_int(self.influencer_count) or 0,  # API field name
            'video_count': safe_int(self.video_count) or 0,
            'live_count': safe_int(self.live_count) or 0,
            
            # Product metrics
            'price': safe_float(self.price),
            'rating': safe_float(self.rating),
            'review_count': safe_int(self.review_count),
            
            # Trend & Status
            'sales_trend': self.sales_trend,
            'trend_label': {0: 'stable', 1: 'rising', 2: 'falling'}.get(self.sales_trend, 'unknown'),
            'listing_date': self.listing_date,
            'is_delisted': self.is_delisted or False,
            'free_shipping': self.free_shipping or False,
            'discount': self.discount,
            
            # Categories
            'category_id': self.category_id,
            'category_l2_id': self.category_l2_id,
            'category_l3_id': self.category_l3_id,
            
            # Seller
            'seller_id': self.seller_id,
            'seller_name': self.seller_name or 'Unknown Seller',
            
            # Media
            'image_url': self.image_url or '',
            'images': self.image_url or '',  # Alias
            'cached_image_url': self.cached_image_url or '',  # Accessible signed URL
            'image_cached_at': self.image_cached_at.isoformat() if self.image_cached_at else None,
            
            # Scan info
            'scan_type': self.scan_type,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            
            # Calculated scores
            'momentum_score': momentum,
            'momentum_label': self._get_momentum_label(momentum),
            'competition_level': competition,
            'opportunity_score': safe_float(self.opportunity_score),
        }
    
    def _get_momentum_label(self, score):
        if score >= 80:
            return '🔥 Hot'
        elif score >= 60:
            return '📈 Rising'
        elif score >= 40:
            return '✅ Stable'
        else:
            return '❄️ Cooling'


class ProductScan(db.Model):
    __tablename__ = 'product_scan'
    
    id = db.Column(db.Integer, primary_key=True)
    scan_date = db.Column(db.DateTime, default=datetime.utcnow)
    total_products_scanned = db.Column(db.Integer)
    total_qualified = db.Column(db.Integer)
    scan_type = db.Column(db.String(50))
    region = db.Column(db.String(10), default='US')
    filters_used = db.Column(db.Text)  # JSON string of filters
    
    products = db.relationship('Product', backref='scan', lazy=True)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def parse_cover_url(raw):
    """Extract clean URL from cover_url which may be a JSON array string."""
    if not raw:
        return None
    if isinstance(raw, str):
        if raw.startswith('['):
            try:
                urls = json.loads(raw)
                if urls and isinstance(urls, list) and len(urls) > 0:
                    return urls[0].get('url') if isinstance(urls[0], dict) else urls[0]
            except json.JSONDecodeError:
                return raw if raw.startswith('http') else None
        elif raw.startswith('http'):
            return raw
    elif isinstance(raw, list) and len(raw) > 0:
        return raw[0].get('url') if isinstance(raw[0], dict) else raw[0]
    return None


def get_cached_image_urls(cover_urls, auth):
    """
    Call EchoTik's batch cover download API to get temporary accessible URLs.
    
    Args:
        cover_urls: List of original cover URLs (max 10 per call)
        auth: HTTPBasicAuth object with EchoTik credentials
    
    Returns:
        Dict mapping original URL -> temporary accessible URL
    """
    if not cover_urls:
        return {}
    
    # API only accepts URLs from this domain
    valid_urls = [url for url in cover_urls if url and 'echosell-images.tos-ap-southeast-1.volces.com' in url]
    
    if not valid_urls:
        return {}
    
    # Max 10 URLs per request
    url_string = ','.join(valid_urls[:10])
    
    try:
        response = requests.get(
            'https://open.echotik.live/api/v3/echotik/batch/cover/download',
            params={'cover_urls': url_string},
            auth=auth,
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 0 and data.get('data'):
                # Response format: [{"original_url": "signed_url"}, ...]
                result = {}
                for item in data['data']:
                    if isinstance(item, dict):
                        for orig_url, signed_url in item.items():
                            if signed_url and signed_url.startswith('http'):
                                result[orig_url] = signed_url
                return result
        
        print(f"EchoTik image API error: {response.status_code}")
        return {}
        
    except Exception as e:
        print(f"EchoTik image API exception: {e}")
        return {}


def refresh_product_images(batch_size=10, delay=0.5):
    """
    Refresh cached image URLs for all products.
    Call this during daily scan or via /api/refresh-images endpoint.
    
    Args:
        batch_size: Products per API call (max 10)
        delay: Seconds between batches to avoid rate limiting
    
    Returns:
        Dict with stats about the refresh operation
    """
    auth = HTTPBasicAuth(
        os.environ.get('ECHOTIK_USERNAME'),
        os.environ.get('ECHOTIK_PASSWORD')
    )
    
    stats = {
        'total_products': 0,
        'processed': 0,
        'success': 0,
        'failed': 0,
        'skipped': 0,
        'no_image_url': 0
    }
    
    try:
        # Get all products (including those without image_url to count them)
        all_products = Product.query.all()
        stats['total_products'] = len(all_products)
        
        # Filter to products with image_url
        products = [p for p in all_products if p.image_url and p.image_url.strip()]
        stats['no_image_url'] = stats['total_products'] - len(products)
        
        if not products:
            return stats
        
        # Process in batches of 10
        for i in range(0, len(products), batch_size):
            batch = products[i:i + batch_size]
            
            # Build URL -> Product mapping
            url_to_products = {}
            for product in batch:
                try:
                    # Parse the image_url (may be JSON array)
                    parsed_url = parse_cover_url(product.image_url)
                    if parsed_url and 'echosell-images.tos-ap-southeast-1.volces.com' in parsed_url:
                        if parsed_url not in url_to_products:
                            url_to_products[parsed_url] = []
                        url_to_products[parsed_url].append(product)
                    else:
                        stats['skipped'] += 1
                except Exception as e:
                    print(f"Error parsing image_url for product {product.product_id}: {e}")
                    stats['skipped'] += 1
            
            if not url_to_products:
                continue
            
            # Call the API
            cover_urls = list(url_to_products.keys())
            result = get_cached_image_urls(cover_urls, auth)
            
            # Update products with new cached URLs
            for original_url, products_list in url_to_products.items():
                cached_url = result.get(original_url)
                for product in products_list:
                    stats['processed'] += 1
                    if cached_url:
                        product.cached_image_url = cached_url
                        product.image_cached_at = datetime.utcnow()
                        stats['success'] += 1
                    else:
                        stats['failed'] += 1
            
            # Commit this batch
            try:
                db.session.commit()
            except Exception as e:
                print(f"DB commit error: {e}")
                db.session.rollback()
            
            # Rate limiting delay
            if i + batch_size < len(products):
                time.sleep(delay)
        
        return stats
        
    except Exception as e:
        print(f"refresh_product_images error: {e}")
        import traceback
        traceback.print_exc()
        raise


# ============================================================================
# SELLER NAME FETCHING
# ============================================================================

def get_seller_details(seller_ids, auth):
    """
    Fetch seller details from EchoTik API.
    Uses the seller detail endpoint to get seller names.
    
    Args:
        seller_ids: List of seller IDs to fetch
        auth: HTTPBasicAuth object
    
    Returns:
        Dict mapping seller_id -> seller_name
    """
    result = {}
    
    for seller_id in seller_ids:
        try:
            # Call EchoTik seller detail API
            response = requests.get(
                'https://open.echotik.live/api/v3/echotik/seller/detail',
                params={
                    'seller_id': seller_id,
                    'region': 'US'
                },
                auth=auth,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get('code') == 0 and data.get('data'):
                    seller_data = data['data']
                    # Handle both single object and list response
                    if isinstance(seller_data, list) and len(seller_data) > 0:
                        seller_data = seller_data[0]
                    
                    seller_name = seller_data.get('seller_name') or seller_data.get('shop_name') or seller_data.get('name')
                    if seller_name:
                        result[seller_id] = seller_name
                        print(f"  Found seller: {seller_id} -> {seller_name}")
            else:
                print(f"  Seller API error for {seller_id}: {response.status_code}")
                
        except Exception as e:
            print(f"  Error fetching seller {seller_id}: {e}")
    
    return result


def refresh_seller_names(delay=0.3):
    """
    Refresh seller names for all products that have seller_id but no seller_name.
    
    Args:
        delay: Seconds between API calls to avoid rate limiting
    
    Returns:
        Dict with stats about the refresh operation
    """
    auth = HTTPBasicAuth(
        os.environ.get('ECHOTIK_USERNAME'),
        os.environ.get('ECHOTIK_PASSWORD')
    )
    
    stats = {
        'total_products': 0,
        'unique_sellers': 0,
        'sellers_found': 0,
        'products_updated': 0,
        'already_have_name': 0
    }
    
    try:
        # Get all products
        all_products = Product.query.all()
        stats['total_products'] = len(all_products)
        
        # Find unique seller_ids that need names
        seller_to_products = {}
        for p in all_products:
            if p.seller_id and p.seller_id.strip():
                if p.seller_name and p.seller_name.strip() and p.seller_name != 'TikTok Shop':
                    stats['already_have_name'] += 1
                else:
                    if p.seller_id not in seller_to_products:
                        seller_to_products[p.seller_id] = []
                    seller_to_products[p.seller_id].append(p)
        
        stats['unique_sellers'] = len(seller_to_products)
        
        if not seller_to_products:
            return stats
        
        print(f"Fetching names for {len(seller_to_products)} unique sellers...")
        
        # Fetch seller details one at a time (API doesn't support batch)
        seller_names = {}
        for i, seller_id in enumerate(seller_to_products.keys()):
            print(f"  [{i+1}/{len(seller_to_products)}] Fetching seller {seller_id}...")
            
            names = get_seller_details([seller_id], auth)
            seller_names.update(names)
            
            if seller_id in names:
                stats['sellers_found'] += 1
            
            # Rate limiting
            if i < len(seller_to_products) - 1:
                time.sleep(delay)
        
        # Update products with seller names
        for seller_id, products in seller_to_products.items():
            if seller_id in seller_names:
                for p in products:
                    p.seller_name = seller_names[seller_id]
                    stats['products_updated'] += 1
        
        # Commit all changes
        db.session.commit()
        print(f"Updated {stats['products_updated']} products with seller names")
        
        return stats
        
    except Exception as e:
        print(f"refresh_seller_names error: {e}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        raise

def get_safe_product_data(product):
    """Safely extract product data handling missing columns"""
    # Try to use the model's to_dict() if it's a model instance
    if hasattr(product, 'to_dict'):
        return product.to_dict()
    
    # Otherwise, handle raw row data
    result = {}
    
    # Map of expected fields with defaults
    field_defaults = {
        'id': 0,
        'product_id': '',
        'product_name': 'Unknown',
        'gmv': 0,
        'sales': 0,
        'commission_rate': 0,
        'potential_earnings': 0,
        'seller_name': 'Unknown',
        'image_url': '',
        'influencer_count': 0,
    }
    
    for field, default in field_defaults.items():
        try:
            val = getattr(product, field, default) if hasattr(product, field) else default
            # Convert Decimal to float
            if isinstance(val, Decimal):
                val = float(val)
            result[field] = val
        except:
            result[field] = default
    
    return result


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
def dashboard():
    """Main dashboard page"""
    return render_template('dashboard_v3.html')


@app.route('/api/products')
def get_products():
    """
    Get products with filtering and sorting
    
    Query params:
    - page: Page number (default 1)
    - per_page: Items per page (default 20, max 100)
    - sort_by: Field to sort by (gmv, sales, commission_rate, momentum_score, etc.)
    - sort_order: asc or desc (default desc)
    - min_commission: Minimum commission rate filter
    - max_influencers: Maximum influencer count filter
    - min_influencers: Minimum influencer count filter
    - competition: Competition level filter (untapped, low, medium, high, saturated)
    - trend: Trend filter (rising, stable, falling)
    - search: Product name search
    - scan_type: Filter by scan type
    """
    try:
        # Pagination
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        
        # Sorting
        sort_by = request.args.get('sort_by', 'gmv')
        sort_order = request.args.get('sort_order', 'desc')
        
        # Start query
        query = Product.query
        
        # Exclude non-promotable products by default (0 commission = can't promote)
        query = query.filter(Product.commission_rate > 0)
        
        # Time period filter
        period = request.args.get('period', 'all')
        now = datetime.utcnow()
        if period == 'today':
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(Product.first_seen >= start_date)
        elif period == 'yesterday':
            start_date = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(Product.first_seen >= start_date, Product.first_seen < end_date)
        elif period == 'week':
            start_date = now - timedelta(days=7)
            query = query.filter(Product.first_seen >= start_date)
        elif period == 'month':
            start_date = now - timedelta(days=30)
            query = query.filter(Product.first_seen >= start_date)
        
        # Apply filters
        min_commission = request.args.get('min_commission', type=float)
        if min_commission:
            query = query.filter(Product.commission_rate >= min_commission)
        
        max_influencers = request.args.get('max_influencers', type=int)
        if max_influencers:
            query = query.filter(Product.influencer_count <= max_influencers)
        
        min_influencers = request.args.get('min_influencers', type=int)
        if min_influencers:
            query = query.filter(Product.influencer_count >= min_influencers)
        
        # Competition level filter (requires calculating on fly or pre-stored)
        competition = request.args.get('competition')
        if competition == 'untapped':
            query = query.filter(Product.influencer_count <= 3)
        elif competition == 'low':
            query = query.filter(Product.influencer_count.between(4, 10))
        elif competition == 'medium':
            query = query.filter(Product.influencer_count.between(11, 30))
        elif competition == 'high':
            query = query.filter(Product.influencer_count.between(31, 50))
        elif competition == 'saturated':
            query = query.filter(Product.influencer_count > 50)
        
        # Trend filter
        trend = request.args.get('trend')
        if trend == 'rising':
            query = query.filter(Product.sales_trend == 1)
        elif trend == 'stable':
            query = query.filter(Product.sales_trend == 0)
        elif trend == 'falling':
            query = query.filter(Product.sales_trend == 2)
        
        # Search
        search = request.args.get('search')
        if search:
            query = query.filter(Product.product_name.ilike(f'%{search}%'))
        
        # Scan type filter
        scan_type = request.args.get('scan_type')
        if scan_type:
            query = query.filter(Product.scan_type == scan_type)
        
        # Apply sorting
        sort_column = getattr(Product, sort_by, Product.gmv)
        if sort_order == 'desc':
            query = query.order_by(desc(sort_column))
        else:
            query = query.order_by(asc(sort_column))
        
        # Execute paginated query
        paginated = query.paginate(page=page, per_page=per_page, error_out=False)
        
        # Serialize products
        products = [get_safe_product_data(p) for p in paginated.items]
        
        return jsonify({
            'success': True,
            'products': products,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': paginated.total,
                'pages': paginated.pages,
                'has_next': paginated.has_next,
                'has_prev': paginated.has_prev
            }
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'products': []
        }), 500


@app.route('/api/stats')
def get_stats():
    """Get dashboard statistics"""
    try:
        total_products = Product.query.count()
        
        # Try to get advanced stats (handle missing columns gracefully)
        stats = {
            'total_products': total_products,
            'hidden_gems': 0,
            'high_commission': 0,
            'rising_products': 0,
            'total_gmv': 0,
            'avg_commission': 0,
            'untapped_products': 0,
        }
        
        # Count hidden gems (3-50 influencers)
        try:
            stats['hidden_gems'] = Product.query.filter(
                Product.influencer_count.between(3, 50)
            ).count()
        except:
            pass
        
        # Count high commission (>15%)
        try:
            stats['high_commission'] = Product.query.filter(
                Product.commission_rate >= 15
            ).count()
        except:
            pass
        
        # Count rising products
        try:
            stats['rising_products'] = Product.query.filter(
                Product.sales_trend == 1
            ).count()
        except:
            pass
        
        # Sum total GMV
        try:
            result = db.session.query(func.sum(Product.gmv)).scalar()
            stats['total_gmv'] = float(result) if result else 0
        except:
            pass
        
        # Average commission
        try:
            result = db.session.query(func.avg(Product.commission_rate)).scalar()
            stats['avg_commission'] = round(float(result), 2) if result else 0
        except:
            pass
        
        # Untapped products (0-3 influencers)
        try:
            stats['untapped_products'] = Product.query.filter(
                Product.influencer_count <= 3
            ).count()
        except:
            pass
        
        return jsonify({
            'success': True,
            'stats': stats
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'stats': {'total_products': 0}
        }), 500


@app.route('/api/opportunities')
def get_opportunities():
    """
    Get best opportunities - products with high potential and low competition
    
    Scoring formula:
    - Potential earnings weight: 40%
    - Low competition bonus: 30%
    - Momentum weight: 20%
    - Commission rate: 10%
    """
    try:
        # Query products with enough data
        products = Product.query.filter(
            Product.gmv > 0,
            Product.commission_rate > 0
        ).all()
        
        opportunities = []
        for p in products:
            # Calculate opportunity score
            earnings_score = min((p.potential_earnings or 0) / 10000, 1) * 40  # Max 40 points
            
            # Competition score (lower is better)
            ifl_count = p.influencer_count or 0
            if ifl_count <= 3:
                competition_score = 30  # Untapped
            elif ifl_count <= 10:
                competition_score = 25  # Low
            elif ifl_count <= 30:
                competition_score = 15  # Medium
            elif ifl_count <= 50:
                competition_score = 5   # High
            else:
                competition_score = 0   # Saturated
            
            # Momentum score
            momentum = p.calculate_momentum() if hasattr(p, 'calculate_momentum') else 50
            momentum_score = (momentum / 100) * 20  # Max 20 points
            
            # Commission score
            comm_rate = float(p.commission_rate or 0)
            commission_score = min(comm_rate / 40, 1) * 10  # Max 10 points at 40%
            
            total_score = earnings_score + competition_score + momentum_score + commission_score
            
            product_data = get_safe_product_data(p)
            product_data['opportunity_score'] = round(total_score, 1)
            opportunities.append(product_data)
        
        # Sort by opportunity score
        opportunities.sort(key=lambda x: x['opportunity_score'], reverse=True)
        
        return jsonify({
            'success': True,
            'opportunities': opportunities[:50],  # Top 50
            'total': len(opportunities)
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'opportunities': []
        }), 500


@app.route('/api/hidden-gems')
def get_hidden_gems():
    """Get products with 3-50 influencers (the sweet spot)"""
    try:
        products = Product.query.filter(
            Product.influencer_count.between(3, 50),
            Product.gmv > 100  # At least some sales
        ).order_by(desc(Product.potential_earnings)).limit(100).all()
        
        return jsonify({
            'success': True,
            'products': [get_safe_product_data(p) for p in products],
            'total': len(products)
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'products': []
        }), 500


@app.route('/api/untapped')
def get_untapped():
    """Get products with 0-3 influencers (first mover advantage)"""
    try:
        products = Product.query.filter(
            Product.influencer_count <= 3,
            Product.gmv > 50  # Has some traction
        ).order_by(desc(Product.gmv)).limit(100).all()
        
        return jsonify({
            'success': True,
            'products': [get_safe_product_data(p) for p in products],
            'total': len(products)
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'products': []
        }), 500


@app.route('/api/rising')
def get_rising():
    """Get products with rising sales trend"""
    try:
        products = Product.query.filter(
            Product.sales_trend == 1
        ).order_by(desc(Product.gmv_7d)).limit(100).all()
        
        return jsonify({
            'success': True,
            'products': [get_safe_product_data(p) for p in products],
            'total': len(products)
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'products': []
        }), 500


@app.route('/api/debug')
def debug_info():
    """Debug endpoint to diagnose issues"""
    try:
        # Get table info
        with db.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'product'
            """))
            columns = [row[0] for row in result.fetchall()]
        
        # Sample product
        sample = Product.query.first()
        sample_data = get_safe_product_data(sample) if sample else None
        
        # Count products
        total = Product.query.count()
        
        return jsonify({
            'success': True,
            'debug': {
                'total_products': total,
                'table_columns': columns,
                'sample_product': sample_data,
                'flask_config': {
                    'database_connected': True,
                    'debug_mode': app.debug
                }
            }
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500



@app.route('/api/image-proxy')
def image_proxy():
    """Proxy images to avoid CORS issues, handles JSON array format"""
    import requests
    import json
    
    url = request.args.get('url', '')
    
    if not url:
        return 'No URL provided', 400
    
    # Handle JSON array format
    if url.startswith('['):
        try:
            images = json.loads(url)
            if isinstance(images, list) and len(images) > 0:
                # Get first image by lowest index
                sorted_images = sorted(images, key=lambda x: x.get('index', 999))
                url = sorted_images[0].get('url', '')
        except:
            pass
    
    if not url or not url.startswith('http'):
        return 'Invalid URL', 400
    
    try:
        response = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.tiktok.com/'
        })
        
        if response.status_code == 200:
            return response.content, 200, {
                'Content-Type': response.headers.get('Content-Type', 'image/jpeg'),
                'Cache-Control': 'public, max-age=86400'
            }
        else:
            return f'Error fetching image: {response.status_code}', response.status_code
            
    except Exception as e:
        return f'Error: {str(e)}', 500


@app.route('/product')
def product_detail_page():
    """Product detail page"""
    return render_template('product_detail.html')


@app.route('/api/product/<product_id>')
def get_product_detail(product_id):
    """Get detailed info for a single product"""
    try:
        product = Product.query.filter_by(product_id=product_id).first()
        
        if not product:
            # Try by database ID
            product = Product.query.filter_by(id=product_id).first()
        
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        
        # Build response with all available fields
        data = {
            'id': product.id,
            'product_id': product.product_id,
            'product_name': getattr(product, 'product_name', '') or '',
            
            # Sales data
            'gmv': float(getattr(product, 'gmv', 0) or 0),
            'gmv_7d': float(getattr(product, 'gmv_7d', 0) or 0),
            'gmv_30d': float(getattr(product, 'gmv_30d', 0) or 0),
            'sales': int(getattr(product, 'sales', 0) or 0),
            'sales_7d': int(getattr(product, 'sales_7d', 0) or 0),
            'sales_30d': int(getattr(product, 'sales_30d', 0) or 0),
            
            # Commission
            'commission_rate': float(getattr(product, 'commission_rate', 0) or 0),
            'potential_earnings': float(getattr(product, 'potential_earnings', 0) or 0),
            
            # Competition - check both field names
            'influencer_count': int(getattr(product, 'influencer_count', 0) or getattr(product, 'total_influencers', 0) or 0),
            'total_influencers': int(getattr(product, 'total_influencers', 0) or getattr(product, 'influencer_count', 0) or 0),
            'video_count': int(getattr(product, 'video_count', 0) or 0),
            'live_count': int(getattr(product, 'live_count', 0) or 0),
            
            # Video analysis (will be populated by refresh)
            'videos_7d': int(getattr(product, 'videos_7d', 0) or 0),
            'videos_30d': int(getattr(product, 'videos_30d', 0) or 0),
            'videos_90d': int(getattr(product, 'videos_90d', 0) or 0),
            
            # Product info
            'price': float(getattr(product, 'price', 0) or getattr(product, 'avg_price', 0) or 0),
            'rating': float(getattr(product, 'rating', 0) or 0),
            'review_count': int(getattr(product, 'review_count', 0) or 0),
            
            # Status
            'sales_trend': getattr(product, 'sales_trend', 0),
            'listing_date': getattr(product, 'listing_date', None),
            'free_shipping': getattr(product, 'free_shipping', False) or False,
            
            # Seller
            'seller_id': getattr(product, 'seller_id', None),
            'seller_name': getattr(product, 'seller_name', '') or 'Unknown',
            
            # Media
            'image_url': getattr(product, 'image_url', '') or getattr(product, 'cover_url', '') or '',
            'cover_url': getattr(product, 'cover_url', '') or getattr(product, 'image_url', '') or '',
            
            # Links
            'tiktok_url': getattr(product, 'tiktok_url', None) or f'https://shop.tiktok.com/view/product/{product.product_id}',
        }
        
        return jsonify({'success': True, 'product': data})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500




@app.route('/api/product/<product_id>/refresh')
def refresh_product(product_id):
    """Refresh product data from EchoTik API"""
    import requests
    from requests.auth import HTTPBasicAuth
    
    API_BASE = "https://open.echotik.live/api/v3/echotik"
    USERNAME = os.getenv('ECHOTIK_USERNAME')
    PASSWORD = os.getenv('ECHOTIK_PASSWORD')
    
    try:
        # Fetch fresh data from EchoTik
        response = requests.get(
            f"{API_BASE}/product/detail",
            params={'product_ids': str(product_id)},
            auth=HTTPBasicAuth(USERNAME, PASSWORD),
            timeout=30
        )
        
        if response.status_code != 200:
            return jsonify({'error': f'API error: {response.status_code}'}), 500
        
        data = response.json()
        products = data.get('data', [])
        
        if not products:
            return jsonify({'error': 'Product not found in API'}), 404
        
        p = products[0] if isinstance(products, list) else products
        
        # Extract all the data
        update_data = {
            'gmv': float(p.get('total_sale_gmv_amt', 0) or 0),
            'gmv_7d': float(p.get('total_sale_gmv_7d_amt', 0) or 0),
            'gmv_30d': float(p.get('total_sale_gmv_30d_amt', 0) or 0),
            'sales': int(p.get('total_sale_cnt', 0) or 0),
            'sales_7d': int(p.get('total_sale_7d_cnt', 0) or 0),
            'sales_30d': int(p.get('total_sale_30d_cnt', 0) or 0),
            'influencer_count': int(p.get('total_ifl_cnt', 0) or 0),
            'video_count': int(p.get('total_video_cnt', 0) or 0),
            'live_count': int(p.get('total_live_cnt', 0) or 0),
            'video_7d': int(p.get('total_video_7d_cnt', 0) or 0),
            'video_30d': int(p.get('total_video_30d_cnt', 0) or 0),
            'live_7d': int(p.get('total_live_7d_cnt', 0) or 0),
            'live_30d': int(p.get('total_live_30d_cnt', 0) or 0),
            'commission_rate': float(p.get('product_commission_rate', 0) or 0),
            'rating': float(p.get('product_rating', 0) or 0),
            'review_count': int(p.get('review_count', 0) or 0),
            'cover_url': extract_image_url(p.get('cover_url', '')),
            'seller_name': p.get('seller_name', '') or p.get('shop_name', ''),
            'seller_id': str(p.get('seller_id', '')) if p.get('seller_id') else '',
        }
        
        # Fix commission rate if needed
        if update_data['commission_rate'] > 0 and update_data['commission_rate'] < 1:
            update_data['commission_rate'] = update_data['commission_rate'] * 100
        
        update_data['potential_earnings'] = update_data['gmv'] * update_data['commission_rate'] / 100
        
        # Update database - use string comparison for product_id
        product = Product.query.filter(Product.product_id == str(product_id)).first()
        
        if product:
            for key, value in update_data.items():
                if hasattr(product, key):
                    setattr(product, key, value)
            
            if update_data['cover_url']:
                product.image_url = update_data['cover_url']
            if update_data['influencer_count']:
                product.total_influencers = update_data['influencer_count']
            
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': 'Product refreshed',
                'data': update_data
            })
        else:
            return jsonify({'error': 'Product not found in database'}), 404
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500



@app.route('/api/scan', methods=['GET', 'POST'])
def run_scan():
    """Run a product scan - can be triggered by cron job"""
    import requests
    from requests.auth import HTTPBasicAuth
    
    API_BASE = "https://open.echotik.live/api/v3/echotik"
    USERNAME = os.getenv('ECHOTIK_USERNAME')
    PASSWORD = os.getenv('ECHOTIK_PASSWORD')
    
    # Optional: Add a secret key to prevent unauthorized scans
    secret = request.args.get('secret', '')
    expected_secret = os.getenv('SCAN_SECRET', '')
    if expected_secret and secret != expected_secret:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        scan_id = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        new_products = 0
        updated_products = 0
        
        # Scan multiple pages
        for page in range(1, 6):  # 5 pages = ~100 products
            response = requests.get(
                f"{API_BASE}/product/list",
                params={
                    'page': page,
                    'size': 20,
                    'sort_by': 'sale_cnt',
                    'sort_type': 'desc'
                },
                auth=HTTPBasicAuth(USERNAME, PASSWORD),
                timeout=30
            )
            
            if response.status_code != 200:
                continue
            
            data = response.json()
            products = data.get('data', [])
            
            if not products:
                break
            
            for p in products:
                product_id = str(p.get('product_id', ''))
                if not product_id:
                    continue
                
                # Check if product exists
                existing = Product.query.filter(Product.product_id == product_id).first()
                
                commission = float(p.get('product_commission_rate', 0) or 0)
                if commission > 0 and commission < 1:
                    commission = commission * 100
                
                # Skip 0% commission
                if commission <= 0:
                    continue
                
                gmv = float(p.get('total_sale_gmv_amt', 0) or 0)
                
                if existing:
                    # Update existing
                    existing.gmv = gmv
                    existing.sales = int(p.get('total_sale_cnt', 0) or 0)
                    existing.commission_rate = commission
                    existing.potential_earnings = gmv * commission / 100
                    existing.influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
                    existing.video_count = int(p.get('total_video_cnt', 0) or 0)
                    existing.updated_at = datetime.utcnow()
                    # Update seller_id if we have one
                    seller_id = p.get('seller_id')
                    if seller_id:
                        existing.seller_id = str(seller_id)
                    # Update image_url if we have one
                    cover_url = p.get('cover_url')
                    if cover_url:
                        existing.image_url = cover_url if isinstance(cover_url, str) else json.dumps(cover_url)
                    updated_products += 1
                else:
                    # Insert new - include image_url and seller_id
                    cover_url = p.get('cover_url')
                    image_url_str = cover_url if isinstance(cover_url, str) else json.dumps(cover_url) if cover_url else None
                    seller_id_val = p.get('seller_id')
                    
                    new_product = Product(
                        product_id=product_id,
                        product_name=p.get('product_name', ''),
                        gmv=gmv,
                        sales=int(p.get('total_sale_cnt', 0) or 0),
                        commission_rate=commission,
                        potential_earnings=gmv * commission / 100,
                        influencer_count=int(p.get('total_ifl_cnt', 0) or 0),
                        total_influencers=int(p.get('total_ifl_cnt', 0) or 0),
                        video_count=int(p.get('total_video_cnt', 0) or 0),
                        price=float(p.get('product_price', 0) or 0),
                        rating=float(p.get('product_rating', 0) or 0),
                        image_url=image_url_str,
                        seller_id=str(seller_id_val) if seller_id_val else None,
                        scan_id=scan_id,
                        first_seen=datetime.utcnow(),
                        created_at=datetime.utcnow()
                    )
                    db.session.add(new_product)
                    new_products += 1
            
            db.session.commit()
        
        # Optional: Refresh images after scan
        image_stats = None
        if request.args.get('refresh_images', '').lower() == 'true':
            print("Refreshing product images...")
            image_stats = refresh_product_images(batch_size=10, delay=0.3)
            print(f"Image refresh: {image_stats['success']} success, {image_stats['failed']} failed")
        
        result = {
            'success': True,
            'scan_id': scan_id,
            'new_products': new_products,
            'updated_products': updated_products,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        if image_stats:
            result['image_refresh'] = image_stats
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/products/recent')
def get_recent_products():
    """Get products filtered by time period"""
    period = request.args.get('period', 'all')  # today, yesterday, week, month, all
    
    now = datetime.utcnow()
    
    if period == 'today':
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'yesterday':
        start_date = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'week':
        start_date = now - timedelta(days=7)
    elif period == 'month':
        start_date = now - timedelta(days=30)
    else:
        start_date = None
    
    query = Product.query
    
    if period == 'yesterday':
        query = query.filter(Product.first_seen >= start_date, Product.first_seen < end_date)
    elif start_date:
        query = query.filter(Product.first_seen >= start_date)
    
    # Apply standard filters
    min_commission = request.args.get('min_commission', type=float)
    if min_commission:
        query = query.filter(Product.commission_rate >= min_commission)
    
    products = query.order_by(Product.first_seen.desc()).limit(100).all()
    
    return jsonify([{
        'product_id': p.product_id,
        'product_name': p.product_name,
        'gmv': float(p.gmv or 0),
        'sales': int(p.sales or 0),
        'commission_rate': float(p.commission_rate or 0),
        'potential_earnings': float(p.potential_earnings or 0),
        'influencer_count': int(p.influencer_count or 0),
        'first_seen': p.first_seen.isoformat() if p.first_seen else None,
        'image_url': p.image_url or '',
        'cached_image_url': p.cached_image_url or ''
    } for p in products])


@app.route('/api/migrate')
def run_migration():
    """Add missing columns to database - run once"""
    try:
        with db.engine.connect() as conn:
            # Add first_seen column if not exists
            conn.execute(text("""
                ALTER TABLE product ADD COLUMN IF NOT EXISTS first_seen TIMESTAMP DEFAULT NOW()
            """))
            conn.execute(text("""
                UPDATE product SET first_seen = created_at WHERE first_seen IS NULL
            """))
            # Add total_influencers column if not exists
            conn.execute(text("""
                ALTER TABLE product ADD COLUMN IF NOT EXISTS total_influencers INTEGER
            """))
            conn.execute(text("""
                UPDATE product SET total_influencers = influencer_count WHERE total_influencers IS NULL
            """))
            conn.commit()
        
        return jsonify({
            'success': True,
            'message': 'Migration completed - first_seen and total_influencers columns added'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/migrate-images')
def migrate_images():
    """Add cached image columns to the database."""
    try:
        with db.engine.connect() as conn:
            # Check if columns exist and add them
            conn.execute(text("""
                ALTER TABLE product ADD COLUMN IF NOT EXISTS cached_image_url TEXT
            """))
            conn.execute(text("""
                ALTER TABLE product ADD COLUMN IF NOT EXISTS image_cached_at TIMESTAMP
            """))
            conn.commit()
            
        return jsonify({
            'success': True,
            'message': 'Image columns added: cached_image_url, image_cached_at'
        })
                
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/refresh-images')
def refresh_images():
    """
    Refresh cached image URLs for all products.
    Can be called manually or added to daily cron job.
    This endpoint is FREE - doesn't count against EchoTik API limits!
    
    Optional params:
    - limit: Max products to process (default: 50 to avoid timeout)
    """
    try:
        limit = request.args.get('limit', 50, type=int)
        
        # Quick debug check first
        total = Product.query.count()
        with_image = Product.query.filter(
            Product.image_url.isnot(None),
            Product.image_url != ''
        ).count()
        
        if with_image == 0:
            return jsonify({
                'success': False,
                'message': f'No products have image_url set. Run /api/scan first to populate image URLs.',
                'total_products': total,
                'products_with_image_url': with_image
            })
        
        # Modified to process limited products
        auth = HTTPBasicAuth(
            os.environ.get('ECHOTIK_USERNAME'),
            os.environ.get('ECHOTIK_PASSWORD')
        )
        
        # Get products that need image refresh (no cached_image_url or old)
        products = Product.query.filter(
            Product.image_url.isnot(None),
            Product.image_url != ''
        ).limit(limit).all()
        
        stats = {'processed': 0, 'success': 0, 'failed': 0, 'skipped': 0}
        
        # Process in batches of 10
        for i in range(0, len(products), 10):
            batch = products[i:i + 10]
            
            url_to_products = {}
            for product in batch:
                try:
                    parsed_url = parse_cover_url(product.image_url)
                    if parsed_url and 'echosell-images' in parsed_url:
                        if parsed_url not in url_to_products:
                            url_to_products[parsed_url] = []
                        url_to_products[parsed_url].append(product)
                    else:
                        stats['skipped'] += 1
                except:
                    stats['skipped'] += 1
            
            if url_to_products:
                result = get_cached_image_urls(list(url_to_products.keys()), auth)
                
                for orig_url, prods in url_to_products.items():
                    cached = result.get(orig_url)
                    for p in prods:
                        stats['processed'] += 1
                        if cached:
                            p.cached_image_url = cached
                            p.image_cached_at = datetime.utcnow()
                            stats['success'] += 1
                        else:
                            stats['failed'] += 1
                
                db.session.commit()
            
            time.sleep(0.3)
        
        return jsonify({
            'success': True,
            'stats': stats,
            'message': f"Refreshed {stats['success']} images, {stats['failed']} failed, {stats['skipped']} skipped",
            'note': f'Processed {limit} products. Call again or use ?limit=100 for more.'
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(error_trace)
        return jsonify({
            'success': False, 
            'error': str(e),
            'trace': error_trace
        }), 500


@app.route('/api/refresh-sellers')
def refresh_sellers():
    """
    Refresh seller names for all products.
    Fetches seller details from EchoTik API for products missing seller_name.
    Uses ~1 API call per unique seller_id.
    
    Optional params:
    - limit: Max sellers to fetch (default: 20 to avoid timeout)
    """
    try:
        limit = request.args.get('limit', 20, type=int)
        
        auth = HTTPBasicAuth(
            os.environ.get('ECHOTIK_USERNAME'),
            os.environ.get('ECHOTIK_PASSWORD')
        )
        
        # Find products needing seller names
        products = Product.query.filter(
            Product.seller_id.isnot(None),
            Product.seller_id != '',
            db.or_(
                Product.seller_name.is_(None),
                Product.seller_name == '',
                Product.seller_name == 'TikTok Shop',
                Product.seller_name == 'Unknown Seller'
            )
        ).all()
        
        if not products:
            return jsonify({
                'success': True,
                'message': 'All products already have seller names!',
                'stats': {'sellers_needed': 0}
            })
        
        # Get unique seller_ids
        seller_to_products = {}
        for p in products:
            if p.seller_id not in seller_to_products:
                seller_to_products[p.seller_id] = []
            seller_to_products[p.seller_id].append(p)
        
        # Limit number of sellers to fetch
        seller_ids = list(seller_to_products.keys())[:limit]
        
        stats = {
            'total_sellers_needed': len(seller_to_products),
            'sellers_processed': 0,
            'sellers_found': 0,
            'products_updated': 0
        }
        
        for seller_id in seller_ids:
            stats['sellers_processed'] += 1
            
            names = get_seller_details([seller_id], auth)
            
            if seller_id in names:
                stats['sellers_found'] += 1
                for p in seller_to_products[seller_id]:
                    p.seller_name = names[seller_id]
                    stats['products_updated'] += 1
            
            time.sleep(0.3)
        
        db.session.commit()
        
        remaining = len(seller_to_products) - len(seller_ids)
        
        return jsonify({
            'success': True,
            'stats': stats,
            'message': f"Found {stats['sellers_found']}/{stats['sellers_processed']} sellers, updated {stats['products_updated']} products",
            'note': f'{remaining} sellers remaining. Call again or use ?limit=50 for more.' if remaining > 0 else 'All done!'
        })
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(error_trace)
        return jsonify({
            'success': False, 
            'error': str(e),
            'trace': error_trace
        }), 500


@app.route('/api/debug-sellers')
def debug_sellers():
    """Debug endpoint to check seller_id status in database"""
    try:
        total = Product.query.count()
        
        # Count by seller_id status
        with_seller_id = Product.query.filter(
            Product.seller_id.isnot(None),
            Product.seller_id != ''
        ).count()
        
        with_seller_name = Product.query.filter(
            Product.seller_name.isnot(None),
            Product.seller_name != '',
            Product.seller_name != 'TikTok Shop',
            Product.seller_name != 'Unknown Seller',
            Product.seller_name != 'Unknown'
        ).count()
        
        # Get sample of seller_ids
        sample_products = Product.query.filter(
            Product.seller_id.isnot(None)
        ).limit(5).all()
        
        samples = [{
            'product_id': p.product_id,
            'product_name': p.product_name[:50] if p.product_name else None,
            'seller_id': p.seller_id,
            'seller_name': p.seller_name
        } for p in sample_products]
        
        return jsonify({
            'success': True,
            'total_products': total,
            'with_seller_id': with_seller_id,
            'with_seller_name': with_seller_name,
            'without_seller_id': total - with_seller_id,
            'sample_products': samples
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'trace': traceback.format_exc()
        }), 500


@app.route('/api/test-image-refresh')
def test_image_refresh():
    """Test image refresh with just 5 products to verify it works"""
    try:
        auth = HTTPBasicAuth(
            os.environ.get('ECHOTIK_USERNAME'),
            os.environ.get('ECHOTIK_PASSWORD')
        )
        
        # Get just 5 products with image_url
        products = Product.query.filter(
            Product.image_url.isnot(None),
            Product.image_url != ''
        ).limit(5).all()
        
        if not products:
            return jsonify({'success': False, 'message': 'No products with image_url'})
        
        results = []
        for p in products:
            parsed_url = parse_cover_url(p.image_url)
            result_item = {
                'product_id': p.product_id,
                'original_url': parsed_url[:50] if parsed_url else None,
                'cached_url': None,
                'status': 'skipped'
            }
            
            if parsed_url and 'echosell-images' in parsed_url:
                # Call API for this one URL
                api_result = get_cached_image_urls([parsed_url], auth)
                if parsed_url in api_result:
                    p.cached_image_url = api_result[parsed_url]
                    p.image_cached_at = datetime.utcnow()
                    result_item['cached_url'] = api_result[parsed_url][:80] + '...'
                    result_item['status'] = 'success'
                else:
                    result_item['status'] = 'api_failed'
            
            results.append(result_item)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Test completed',
            'results': results
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'trace': traceback.format_exc()
        }), 500


@app.route('/api/test-seller-refresh')
def test_seller_refresh():
    """Test seller refresh with just 3 sellers to verify it works"""
    try:
        auth = HTTPBasicAuth(
            os.environ.get('ECHOTIK_USERNAME'),
            os.environ.get('ECHOTIK_PASSWORD')
        )
        
        # Get 3 unique seller_ids that need names
        products = Product.query.filter(
            Product.seller_id.isnot(None),
            Product.seller_id != '',
            db.or_(
                Product.seller_name.is_(None),
                Product.seller_name == '',
                Product.seller_name == 'TikTok Shop'
            )
        ).limit(10).all()
        
        if not products:
            return jsonify({'success': False, 'message': 'No products need seller names'})
        
        # Get unique seller_ids
        seller_ids = list(set(p.seller_id for p in products))[:3]
        
        results = []
        for seller_id in seller_ids:
            result_item = {
                'seller_id': seller_id,
                'seller_name': None,
                'status': 'pending'
            }
            
            # Call API
            names = get_seller_details([seller_id], auth)
            if seller_id in names:
                result_item['seller_name'] = names[seller_id]
                result_item['status'] = 'success'
                
                # Update all products with this seller
                for p in products:
                    if p.seller_id == seller_id:
                        p.seller_name = names[seller_id]
            else:
                result_item['status'] = 'not_found'
            
            results.append(result_item)
            time.sleep(0.3)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Test completed',
            'results': results
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'trace': traceback.format_exc()
        }), 500


if __name__ == '__main__':
    with app.app_context():
        # Try to create tables (won't overwrite existing)
        try:
            db.create_all()
        except:
            pass
    
    print("=" * 50)
    print("🚀 TikTok Shop Product Finder v3.0")
    print("=" * 50)
    print(f"Dashboard: http://127.0.0.1:5000")
    print(f"API Debug: http://127.0.0.1:5000/api/debug")
    print(f"Products:  http://127.0.0.1:5000/api/products")
    print("=" * 50)
    
    app.run(host='0.0.0.0', debug=True, port=5000)
