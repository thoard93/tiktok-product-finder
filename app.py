"""
TikTok Product Finder - Brand Hunter (Community Edition)
Scans TOP BRANDS directly - no follow workflow needed

Features:
- Discord OAuth login (server members only)
- Developer passkey bypass
- Scan locking (one scan at a time)
- User activity logging
- Watermarked exports
- Admin dashboard

Strategy: 
- Get top brands by GMV from EchoTik
- Scan their products sorted by 7-DAY SALES DESCENDING
- Filter for low influencer count (1-100)
- Save hidden gems automatically
"""

import os
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, redirect, session, url_for
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
import time
import json
import hashlib
import secrets

app = Flask(__name__, static_folder='pwa')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///products.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))

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

# =============================================================================
# AUTHENTICATION CONFIG
# =============================================================================

# Discord OAuth Settings (set these in Render environment variables)
DISCORD_CLIENT_ID = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI = os.environ.get('DISCORD_REDIRECT_URI', 'https://tiktok-product-finder.onrender.com/auth/discord/callback')
DISCORD_GUILD_ID = os.environ.get('DISCORD_GUILD_ID', '')  # Your Discord server ID

# Developer passkey (set in Render environment variables)
DEV_PASSKEY = os.environ.get('DEV_PASSKEY', 'change-this-passkey-123')

# Admin Discord user IDs (comma-separated)
ADMIN_DISCORD_IDS = os.environ.get('ADMIN_DISCORD_IDS', '').split(',')

# EchoTik API Config - v3 API with HTTPBasicAuth
BASE_URL = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')

# =============================================================================
# SCAN LOCK (prevent simultaneous scans)
# =============================================================================

scan_lock = {
    'is_locked': False,
    'locked_by': None,
    'locked_at': None,
    'scan_type': None
}

def acquire_scan_lock(user_id, scan_type='quick'):
    """Try to acquire scan lock. Returns True if successful."""
    global scan_lock
    
    # Check if lock is stale (over 10 minutes old)
    if scan_lock['is_locked'] and scan_lock['locked_at']:
        lock_age = (datetime.utcnow() - scan_lock['locked_at']).total_seconds()
        if lock_age > 600:  # 10 minutes
            scan_lock['is_locked'] = False
    
    if scan_lock['is_locked']:
        return False
    
    scan_lock['is_locked'] = True
    scan_lock['locked_by'] = user_id
    scan_lock['locked_at'] = datetime.utcnow()
    scan_lock['scan_type'] = scan_type
    return True

def release_scan_lock(user_id=None):
    """Release scan lock. If user_id provided, only release if they own it."""
    global scan_lock
    if user_id and scan_lock['locked_by'] != user_id:
        return False
    scan_lock['is_locked'] = False
    scan_lock['locked_by'] = None
    scan_lock['locked_at'] = None
    scan_lock['scan_type'] = None
    return True

def get_scan_status():
    """Get current scan lock status"""
    if not scan_lock['is_locked']:
        return {'locked': False}
    return {
        'locked': True,
        'locked_by': scan_lock['locked_by'],
        'locked_at': scan_lock['locked_at'].isoformat() if scan_lock['locked_at'] else None,
        'scan_type': scan_lock['scan_type']
    }

# =============================================================================
# DATABASE MODELS
# =============================================================================

class User(db.Model):
    """Users who can access the tool"""
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), unique=True, nullable=True)
    discord_username = db.Column(db.String(100))
    discord_avatar = db.Column(db.String(255))
    is_admin = db.Column(db.Boolean, default=False)
    is_dev_user = db.Column(db.Boolean, default=False)  # Logged in via passkey
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'discord_id': self.discord_id,
            'discord_username': self.discord_username,
            'is_admin': self.is_admin,
            'is_dev_user': self.is_dev_user,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None
        }

class ActivityLog(db.Model):
    """Log of user activities"""
    __tablename__ = 'activity_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    action = db.Column(db.String(100))  # scan, export, favorite, view, etc.
    details = db.Column(db.Text)  # JSON details
    ip_address = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref='activities')
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': self.user.discord_username if self.user else 'Unknown',
            'action': self.action,
            'details': self.details,
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
# AUTHENTICATION HELPERS
# =============================================================================

def log_activity(user_id, action, details=None):
    """Log user activity"""
    try:
        log = ActivityLog(
            user_id=user_id,
            action=action,
            details=json.dumps(details) if details else None,
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        print(f"Failed to log activity: {e}")

def get_current_user():
    """Get current logged-in user from session"""
    user_id = session.get('user_id')
    if user_id:
        return User.query.get(user_id)
    return None

def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required', 'login_url': '/login'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to require admin access"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def generate_watermark(user):
    """Generate a unique watermark for exports"""
    if not user:
        return "UNKNOWN"
    # Create a hash that can be traced back to user but isn't obvious
    data = f"{user.id}-{user.discord_username}-{datetime.utcnow().strftime('%Y%m%d')}"
    hash_val = hashlib.md5(data.encode()).hexdigest()[:8].upper()
    return f"BH-{hash_val}"

# =============================================================================
# AUTHENTICATION ROUTES
# =============================================================================

@app.route('/login')
def login_page():
    """Show login page"""
    return send_from_directory(app.static_folder, 'login.html')

@app.route('/auth/discord')
def discord_login():
    """Redirect to Discord OAuth"""
    if not DISCORD_CLIENT_ID:
        return jsonify({'error': 'Discord OAuth not configured'}), 500
    
    # Discord OAuth URL
    oauth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify%20guilds"
    )
    return redirect(oauth_url)

@app.route('/auth/discord/callback')
def discord_callback():
    """Handle Discord OAuth callback"""
    code = request.args.get('code')
    if not code:
        return redirect('/login?error=no_code')
    
    try:
        # Exchange code for token
        token_response = requests.post(
            'https://discord.com/api/oauth2/token',
            data={
                'client_id': DISCORD_CLIENT_ID,
                'client_secret': DISCORD_CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': DISCORD_REDIRECT_URI
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        
        if token_response.status_code != 200:
            return redirect('/login?error=token_failed')
        
        tokens = token_response.json()
        access_token = tokens.get('access_token')
        
        # Get user info
        user_response = requests.get(
            'https://discord.com/api/users/@me',
            headers={'Authorization': f'Bearer {access_token}'}
        )
        
        if user_response.status_code != 200:
            return redirect('/login?error=user_failed')
        
        discord_user = user_response.json()
        discord_id = discord_user.get('id')
        username = discord_user.get('username')
        avatar = discord_user.get('avatar')
        
        # Check if user is in the required guild
        if DISCORD_GUILD_ID:
            guilds_response = requests.get(
                'https://discord.com/api/users/@me/guilds',
                headers={'Authorization': f'Bearer {access_token}'}
            )
            
            if guilds_response.status_code == 200:
                guilds = guilds_response.json()
                guild_ids = [g.get('id') for g in guilds]
                
                if DISCORD_GUILD_ID not in guild_ids:
                    return redirect('/login?error=not_in_server')
        
        # Create or update user
        user = User.query.filter_by(discord_id=discord_id).first()
        if not user:
            user = User(
                discord_id=discord_id,
                discord_username=username,
                discord_avatar=avatar,
                is_admin=discord_id in ADMIN_DISCORD_IDS
            )
            db.session.add(user)
        else:
            user.discord_username = username
            user.discord_avatar = avatar
            user.last_login = datetime.utcnow()
        
        db.session.commit()
        
        # Set session
        session['user_id'] = user.id
        session['discord_username'] = username
        session['is_admin'] = user.is_admin
        
        log_activity(user.id, 'login', {'method': 'discord'})
        
        return redirect('/')
        
    except Exception as e:
        print(f"Discord OAuth error: {e}")
        return redirect(f'/login?error=oauth_error')

@app.route('/auth/passkey', methods=['POST'])
def passkey_login():
    """Login with developer passkey"""
    data = request.get_json() or {}
    passkey = data.get('passkey', '')
    
    if not passkey or passkey != DEV_PASSKEY:
        return jsonify({'error': 'Invalid passkey'}), 401
    
    # Create or get dev user
    user = User.query.filter_by(is_dev_user=True, discord_username='Developer').first()
    if not user:
        user = User(
            discord_id=None,
            discord_username='Developer',
            is_admin=True,
            is_dev_user=True
        )
        db.session.add(user)
        db.session.commit()
    else:
        user.last_login = datetime.utcnow()
        db.session.commit()
    
    session['user_id'] = user.id
    session['discord_username'] = 'Developer'
    session['is_admin'] = True
    
    log_activity(user.id, 'login', {'method': 'passkey'})
    
    return jsonify({'success': True, 'redirect': '/'})

@app.route('/auth/logout')
def logout():
    """Logout user"""
    user_id = session.get('user_id')
    if user_id:
        log_activity(user_id, 'logout', {})
    session.clear()
    return redirect('/login')

@app.route('/api/me')
@login_required
def get_me():
    """Get current user info"""
    user = get_current_user()
    return jsonify({
        'user': user.to_dict() if user else None,
        'watermark': generate_watermark(user)
    })

@app.route('/api/scan-status')
@login_required
def api_scan_status():
    """Get current scan lock status"""
    status = get_scan_status()
    if status['locked'] and status.get('locked_by'):
        # Get username of who's scanning
        locker = User.query.get(status['locked_by'])
        status['locked_by_username'] = locker.discord_username if locker else 'Unknown'
    return jsonify(status)

# =============================================================================
# ADMIN ROUTES
# =============================================================================

@app.route('/admin')
@login_required
@admin_required
def admin_page():
    """Admin dashboard"""
    return send_from_directory(app.static_folder, 'admin.html')

@app.route('/api/admin/users')
@login_required
@admin_required
def admin_users():
    """Get all users"""
    users = User.query.order_by(User.last_login.desc()).all()
    return jsonify({'users': [u.to_dict() for u in users]})

@app.route('/api/admin/activity')
@login_required
@admin_required
def admin_activity():
    """Get recent activity"""
    limit = request.args.get('limit', 100, type=int)
    logs = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(limit).all()
    return jsonify({'logs': [l.to_dict() for l in logs]})

@app.route('/api/admin/kick/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_kick_user(user_id):
    """Remove a user's access"""
    user = User.query.get(user_id)
    if user:
        log_activity(session.get('user_id'), 'admin_kick', {'kicked_user': user.discord_username})
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'error': 'User not found'}), 404

@app.route('/api/log-activity', methods=['POST'])
@login_required
def api_log_activity():
    """Log user activity from frontend"""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.get_json() or {}
    action = data.get('action', 'unknown')
    details = data.get('details', {})
    
    log_activity(user.id, action, details)
    return jsonify({'success': True})

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


@app.route('/api/debug-seller/<seller_id>', methods=['GET'])
def debug_seller_products(seller_id):
    """
    Debug endpoint - returns raw API response for a seller's products
    Use this to see what fields EchoTik returns
    """
    try:
        page = request.args.get('page', 1, type=int)
        response = requests.get(
            f"{BASE_URL}/seller/product/list",
            params={
                "seller_id": seller_id,
                "page_num": page,
                "page_size": 5,
                "seller_product_sort_field": 4,
                "sort_type": 1
            },
            auth=get_auth(),
            timeout=30
        )
        data = response.json()
        
        # Get just the first product's keys to see what fields are available
        products = data.get('data', [])
        sample_fields = list(products[0].keys()) if products else []
        
        return jsonify({
            'raw_response_code': data.get('code'),
            'product_count': len(products),
            'available_fields': sample_fields,
            'first_product_sample': products[0] if products else None
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# MAIN SCANNING ENDPOINTS
# =============================================================================

@app.route('/api/top-brands', methods=['GET'])
def get_top_brands_list():
    """
    Get list of top brands by GMV
    
    Parameters:
        start_rank: Starting rank (1 = top brand)
        count: Number of brands to return
    """
    try:
        start_rank = request.args.get('start_rank', 1, type=int)
        count = request.args.get('count', 10, type=int)
        
        # Calculate which pages to fetch
        start_page = (start_rank - 1) // 10 + 1
        start_offset = (start_rank - 1) % 10
        
        all_brands = []
        pages_needed = ((start_offset + count - 1) // 10) + 1
        
        for page in range(start_page, start_page + pages_needed):
            brands_page = get_top_brands(page=page)
            if brands_page:
                all_brands.extend(brands_page)
            time.sleep(0.1)
        
        brands = all_brands[start_offset:start_offset + count]
        
        return jsonify({
            'success': True,
            'brands': brands,
            'count': len(brands)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
                
                time.sleep(0.1)
            
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

@app.route('/api/quick-scan', methods=['GET'])
@login_required
def quick_scan():
    """
    Quick scan - scans ONE brand at a time to avoid timeouts.
    Call this multiple times with different brand_rank values.
    
    Parameters:
        brand_rank: Which brand to scan (1 = top brand, 2 = second, etc.)
        pages: Number of pages to scan (default: 10, max 10 to avoid timeout)
        max_influencers: Maximum influencer count (default: 100)
        min_sales: Minimum 7-day sales (default: 0)
    """
    user = get_current_user()
    user_id = user.id if user else None
    
    # Try to acquire scan lock
    if not acquire_scan_lock(user_id, 'quick'):
        status = get_scan_status()
        locker = User.query.get(status.get('locked_by')) if status.get('locked_by') else None
        return jsonify({
            'error': 'Scan in progress',
            'locked_by': locker.discord_username if locker else 'Unknown',
            'message': f"Please wait - {locker.discord_username if locker else 'Someone'} is currently scanning"
        }), 423  # 423 = Locked
    
    try:
        brand_rank = request.args.get('brand_rank', 1, type=int)
        pages = min(request.args.get('pages', 10, type=int), 10)  # Cap at 10 pages to avoid timeout
        min_influencers = request.args.get('min_influencers', 1, type=int)
        max_influencers = request.args.get('max_influencers', 100, type=int)
        min_sales = request.args.get('min_sales', 0, type=int)
        
        # Get the specific brand
        brand_page = (brand_rank - 1) // 10 + 1
        brand_offset = (brand_rank - 1) % 10
        
        brands_response = get_top_brands(page=brand_page)
        if not brands_response or len(brands_response) <= brand_offset:
            return jsonify({'error': f'Brand rank {brand_rank} not found'}), 404
        
        brand = brands_response[brand_offset]
        seller_id = brand.get('seller_id', '')
        seller_name = brand.get('seller_name', 'Unknown')
        
        result = {
            'brand_rank': brand_rank,
            'seller_id': seller_id,
            'seller_name': seller_name,
            'pages_scanned': 0,
            'products_scanned': 0,
            'products_found': 0,
            'products_saved': 0
        }
        
        for page in range(1, pages + 1):
            products = get_seller_products(seller_id, page=page)
            
            if not products:
                break
            
            result['pages_scanned'] += 1
            result['products_scanned'] += len(products)
            
            for p in products:
                product_id = p.get('product_id', '')
                if not product_id:
                    continue
                
                influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
                total_sales = int(p.get('total_sale_cnt', 0) or 0)
                sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
                commission_rate = float(p.get('product_commission_rate', 0) or 0)
                video_count = int(p.get('total_video_cnt', 0) or 0)
                video_7d = int(p.get('total_video_7d_cnt', 0) or 0)
                video_30d = int(p.get('total_video_30d_cnt', 0) or 0)
                live_count = int(p.get('total_live_cnt', 0) or 0)
                views_count = int(p.get('total_views_cnt', 0) or 0)
                
                # Filters
                if influencer_count < min_influencers or influencer_count > max_influencers:
                    continue
                if sales_7d < min_sales:
                    continue
                if commission_rate <= 0:
                    continue
                
                result['products_found'] += 1
                
                image_url = parse_cover_url(p.get('cover_url', ''))
                
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
                    result['products_saved'] += 1
            
            time.sleep(0.1)
        
        db.session.commit()
        
        # Log activity
        log_activity(user_id, 'scan', {
            'type': 'quick',
            'brand': seller_name,
            'found': result['products_found'],
            'saved': result['products_saved']
        })
        
        # Release lock after successful scan
        release_scan_lock(user_id)
        
        return jsonify({
            'success': True,
            'result': result,
            'next_brand': brand_rank + 1
        })
    
    except Exception as e:
        import traceback
        db.session.rollback()
        release_scan_lock(user_id)  # Release lock on error too
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
        seller_name: Optional seller name to use (for brand scan)
    """
    try:
        start_page = request.args.get('start', 1, type=int)
        end_page = request.args.get('end', 50, type=int)
        min_influencers = request.args.get('min_influencers', 1, type=int)
        max_influencers = request.args.get('max_influencers', 100, type=int)
        min_sales = request.args.get('min_sales', 0, type=int)
        seller_name_param = request.args.get('seller_name', '')
        
        products_scanned = 0
        products_found = 0
        products_saved = 0
        seller_name = seller_name_param or "Unknown"
        
        for page in range(start_page, end_page + 1):
            products = get_seller_products(seller_id, page=page)
            
            if not products:
                continue
            
            for p in products:
                products_scanned += 1
                product_id = p.get('product_id', '')
                if not product_id:
                    continue
                
                # Try to get seller_name from product if we don't have it
                if seller_name == "Unknown":
                    seller_name = p.get('seller_name', '') or p.get('shop_name', '') or "Unknown"
                
                influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
                sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                total_sales = int(p.get('total_sale_cnt', 0) or 0)
                sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
                commission_rate = float(p.get('product_commission_rate', 0) or 0)
                
                # Get video stats
                video_count = int(p.get('total_video_cnt', 0) or 0)
                video_7d = int(p.get('total_video_7d_cnt', 0) or 0)
                video_30d = int(p.get('total_video_30d_cnt', 0) or 0)
                live_count = int(p.get('total_live_cnt', 0) or 0)
                views_count = int(p.get('total_views_cnt', 0) or 0)
                
                # Filters
                if influencer_count < min_influencers or influencer_count > max_influencers:
                    continue
                if sales_7d < min_sales:
                    continue
                # NOTE: Not filtering 0% commission here - seller/product/list API may not return commission
                # Use "Refresh Data" on product detail page to get real commission from product detail API
                
                products_found += 1
                image_url = parse_cover_url(p.get('cover_url', ''))
                
                existing = Product.query.get(product_id)
                if existing:
                    # Update existing product
                    existing.influencer_count = influencer_count
                    existing.sales = total_sales
                    existing.sales_7d = sales_7d
                    existing.sales_30d = sales_30d
                    existing.commission_rate = commission_rate
                    existing.video_count = video_count
                    existing.video_7d = video_7d
                    existing.video_30d = video_30d
                    existing.live_count = live_count
                    existing.views_count = views_count
                    if seller_name != "Unknown":
                        existing.seller_name = seller_name
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
                        scan_type='page_range'
                    )
                    db.session.add(product)
                    products_saved += 1
            
            time.sleep(0.1)
        
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
    
    # Get favorites count
    favorites = Product.query.filter(Product.is_favorite == True).count()
    
    return jsonify({
        'total_products': total,
        'unique_brands': brands,
        'untapped': untapped,
        'low_competition': low,
        'medium_competition': medium,
        'good_competition': good,
        'favorites': favorites,
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
    
    Parameters:
        batch: Number of products to process (default 50)
        force: If true, refresh ALL products regardless of current cache status
    """
    try:
        batch_size = request.args.get('batch', 50, type=int)
        force = request.args.get('force', 'false').lower() == 'true'
        
        if force:
            # Force refresh - get products with ANY image_url
            products = Product.query.filter(
                Product.image_url.isnot(None),
                Product.image_url != ''
            ).limit(batch_size).all()
        else:
            # Normal refresh - only products needing images
            # Include products with empty cached_image_url OR no image_url but have product_id
            products = Product.query.filter(
                db.or_(
                    # Has image_url but no cache
                    db.and_(
                        Product.image_url.isnot(None),
                        Product.image_url != '',
                        db.or_(
                            Product.cached_image_url.is_(None),
                            Product.cached_image_url == ''
                        )
                    ),
                    # No image_url at all - we'll try to fetch from API
                    db.or_(
                        Product.image_url.is_(None),
                        Product.image_url == ''
                    )
                )
            ).limit(batch_size).all()
        
        if not products:
            return jsonify({'success': True, 'message': 'No images need refreshing', 'updated': 0})
        
        updated = 0
        
        # First, handle products WITH image_url - get signed URLs
        products_with_urls = [p for p in products if p.image_url]
        
        for i in range(0, len(products_with_urls), 10):
            batch = products_with_urls[i:i+10]
            
            url_to_product = {}
            for p in batch:
                parsed_url = parse_cover_url(p.image_url)
                if parsed_url:
                    url_to_product[parsed_url] = p
            
            if url_to_product:
                signed_urls = get_cached_image_urls(list(url_to_product.keys()))
                
                for orig_url, signed_url in signed_urls.items():
                    if orig_url in url_to_product and signed_url:
                        product = url_to_product[orig_url]
                        product.cached_image_url = signed_url
                        product.image_cached_at = datetime.utcnow()
                        updated += 1
            
            time.sleep(0.2)
        
        # Second, handle products WITHOUT image_url - try to get from product detail API
        products_without_urls = [p for p in products if not p.image_url]
        
        for i in range(0, len(products_without_urls), 10):
            batch = products_without_urls[i:i+10]
            product_ids = [p.product_id for p in batch]
            
            try:
                # Call product detail API to get images
                response = requests.post(
                    f"{BASE_URL}/product/detail",
                    json={"product_ids": ",".join(product_ids)},
                    auth=get_auth(),
                    timeout=30
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('code') == 0:
                        api_products = data.get('data', [])
                        
                        for api_p in api_products:
                            pid = api_p.get('product_id', '')
                            cover_url = api_p.get('cover_url', '')
                            
                            if pid and cover_url:
                                # Find matching product
                                for p in batch:
                                    if p.product_id == pid:
                                        parsed_url = parse_cover_url(cover_url)
                                        if parsed_url:
                                            p.image_url = parsed_url
                                            # Get signed URL
                                            signed = get_cached_image_urls([parsed_url])
                                            if signed.get(parsed_url):
                                                p.cached_image_url = signed[parsed_url]
                                                p.image_cached_at = datetime.utcnow()
                                                updated += 1
                                        break
            except Exception as e:
                print(f"Error fetching product images: {e}")
            
            time.sleep(0.3)
        
        db.session.commit()
        
        # Count remaining
        remaining = Product.query.filter(
            db.or_(
                Product.cached_image_url.is_(None),
                Product.cached_image_url == ''
            )
        ).count()
        
        return jsonify({
            'success': True,
            'message': f'Updated {updated} images',
            'updated': updated,
            'remaining': remaining
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
@login_required
def index():
    return send_from_directory('pwa', 'brand_hunter.html')

@app.route('/product')
@login_required
def product_page():
    return send_from_directory('pwa', 'product_detail.html')

@app.route('/pwa/<path:filename>')
def pwa_files(filename):
    # Allow login.html and admin.html without auth
    if filename in ['login.html', 'admin.html']:
        return send_from_directory('pwa', filename)
    # Other PWA files need auth check
    if not session.get('user_id'):
        return redirect('/login')
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
        
        # Try to add missing columns to products table
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
        
        # Create users table if not exists
        try:
            db.session.execute(db.text('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    discord_id VARCHAR(50) UNIQUE,
                    discord_username VARCHAR(100),
                    discord_avatar VARCHAR(255),
                    is_admin BOOLEAN DEFAULT FALSE,
                    is_dev_user BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            '''))
            db.session.commit()
        except:
            db.session.rollback()
        
        # Create activity_logs table if not exists
        try:
            db.session.execute(db.text('''
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    action VARCHAR(100),
                    details TEXT,
                    ip_address VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            '''))
            db.session.commit()
        except:
            db.session.rollback()
        
        return jsonify({
            'success': True, 
            'message': f'Database initialized. Added product columns: {added if added else "none (already exist)"}. Users and activity tables ready.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
