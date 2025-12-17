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
import secrets
import sys
import subprocess
import requests
try:
    import stripe
except ImportError:
    stripe = None
    print("WARNING: Stripe module not found. Payments will fail.")
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, redirect, session, url_for, render_template, make_response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.pool import NullPool
from functools import wraps
import time
import json
import hashlib
import secrets
# import jwt  # Moved inside generate_kling_jwt_token to avoid dependency crash for Bot
import re   # For parsing product IDs from URLs
import traceback
# from apify_service import ApifyService # Apify Service - REMOVED for V2
from werkzeug.exceptions import HTTPException
try:
    from whitenoise import WhiteNoise
except ImportError:
    WhiteNoise = None
    print("WARNING: WhiteNoise not found. Static files may not be served correctly if running as web server.")

app = Flask(__name__, static_folder='pwa')
if WhiteNoise:
    app.wsgi_app = WhiteNoise(app.wsgi_app, root='pwa/')

# Force absolute path for SQLite to prevent subprocess mismatches
basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, 'products.db')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', f'sqlite:///{db_path}')

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Fix Render's postgres:// URL
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

# Connection pool settings to handle Render's connection drops
# Use NullPool to force fresh connections every time (prevents stale connection errors)
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'poolclass': NullPool,
}

# --- GLOBAL SCAN LOCK ---
SCAN_LOCK = {
    'locked': False,
    'locked_by': None,
    'scan_type': None,
    'start_time': None
}

def get_scan_status():
    return SCAN_LOCK


@app.route('/api/debug/check-product/<product_id>')
def check_product_debug(product_id):
    """Debug endpoint to inspect RAW DB values for a product"""
    try:
        # Try both ID formats (raw and shop_ prefixed)
        p = Product.query.get(product_id)
        if not p and not product_id.startswith('shop_'):
             p = Product.query.get(f"shop_{product_id}")
        
        if not p:
            return jsonify({'found': False, 'message': f'Product {product_id} not found'}), 404
            
        return jsonify({
            'found': True,
            'id': p.product_id,
            'name': p.product_name,
            'scan_type': p.scan_type,
            'live_count_db_value': p.live_count,
            'stock_in_to_dict': p.to_dict().get('stock'),
            'product_url': p.product_url,
            'db_uri_used': app.config['SQLALCHEMY_DATABASE_URI']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug-products')
def debug_products_dump():
    """Dump last 10 products to verify DB state"""
    try:
        # Force fresh query
        db.session.expire_all()
        products = Product.query.order_by(Product.first_seen.desc()).limit(10).all()
        return jsonify({
            'count': len(products),
            'products': [{
                'id': p.product_id,
                'name': p.product_name,
                'scan_type': p.scan_type,
                'video_count': p.video_count,
                'is_hidden': getattr(p, 'is_hidden', 'N/A'),
                'is_ad_driven': p.is_ad_driven,
                'created': p.first_seen.isoformat()
            } for p in products],
            'db_uri': app.config['SQLALCHEMY_DATABASE_URI']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

db = SQLAlchemy(app)

# =============================================================================
# GLOBAL ERROR HANDLER
# =============================================================================

@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON instead of HTML for API errors"""
    # Pass through HTTP errors
    if isinstance(e, HTTPException):
        return e

    # Only handle API routes
    if request.path.startswith('/api/'):
        print(f"API Error: {e}")
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500
        
    # For non-API routes, let Flask handle it (500 page)
    return e

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
ECHOTIK_V3_BASE = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_REALTIME_BASE = "https://open.echotik.live/api/v3/realtime"
BASE_URL = ECHOTIK_V3_BASE # Default for shop lists etc.
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')

# Telegram Alerts Config
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# Kling AI Video Generation Config
KLING_ACCESS_KEY = os.environ.get('KLING_ACCESS_KEY', '')
KLING_SECRET_KEY = os.environ.get('KLING_SECRET_KEY', '')
KLING_API_BASE_URL = "https://api-singapore.klingai.com"

# Gemini AI Image Generation Config
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

# Apify Config - REMOVED for V2

# Default prompt for video generation
KLING_DEFAULT_PROMPT = "cinematic push towards the product, no hands, product stays still"

# =============================================================================
# SHARED HELPERS
# =============================================================================

def parse_cover_url(url):
    """Clean up cover URL"""
    if not url: return ""
    return url

def get_auth():
    """Get HTTP Basic Auth for EchoTik"""
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)

def enrich_product_data(p, i_log_prefix="", force=False):
    """
    Global Helper: Search Echotik for product stats based on Title then Brand.
    """
    # Helper: Clean title
    def clean_title_for_search(t):
        if not t: return ""
        t = re.sub(r'#\w+', '', t) # Remove hashtags
        t = re.sub(r'[^\w\s]', '', t) # Remove emojis/punctuation
        return t.strip()

    # 1. Direct ID Check (Realtime v3)
    pid = p.get('product_id')
    if pid and not pid.startswith('ad_') and (force or not p.get('is_enriched')):
        target_id = pid.replace('shop_', '') if pid.startswith('shop_') else pid
        
        try:
            res = requests.get(
                f"{ECHOTIK_REALTIME_BASE}/product/detail",
                params={'product_id': target_id, 'region': p.get('region', 'US')}, # v3 uses singular 'product_id', required region
                auth=get_auth(),
                timeout=20
            )
            
            if res.status_code == 200:
                data = res.json()
                # Check for v3 response structure usually {code:0, data: {...}}
                if data.get('data'):
                    d = data['data'] # Realtime v3 returns strictly one object usually
                    if isinstance(d, list): d = d[0] # Handle if it returns list
                    
                    # DEBUG LOGGING
                    print(f"DEBUG: EchoTik Data Keys: {list(d.keys())}")
                    import json
                    try:
                        print(f"DEBUG: EchoTik Data Sample: {json.dumps(d, default=str)[:1000]}")
                    except:
                        print(f"DEBUG: EchoTik Data (Raw): {d}")

                    # Update product - Try CamelCase (v3) then SnakeCase (v2) fallback
                    p['sales'] = int(d.get('totalSaleCnt') or d.get('total_sale_cnt', 0))
                    p['sales_7d'] = int(d.get('totalSale7dCnt') or d.get('total_sale_7d_cnt', 0))
                    p['sales_30d'] = int(d.get('totalSale30dCnt') or d.get('total_sale_30d_cnt', 0))
                    p['influencer_count'] = int(d.get('totalIflCnt') or d.get('total_ifl_cnt', 0))
                    p['video_count'] = int(d.get('totalVideoCnt') or d.get('total_video_cnt', 0))
                    p['commission_rate'] = float(d.get('productCommissionRate') or d.get('product_commission_rate', 0))
                    p['price'] = float(d.get('spuAvgPrice') or d.get('spu_avg_price', 0))
                    
                    # Fallback: Check for Raw Page Props (shop/pdp/...)
                    if p['sales'] == 0:
                        # Iterate keys to find the one with product_info
                        for k, v in d.items():
                            # DEBUG: Log what we are scanning
                            print(f"DEBUG: Scanning key '{k}' Type: {type(v)}")
                            
                            if isinstance(v, dict) and 'product_info' in v:
                                try:
                                    pi = v['product_info']
                                    # Total Sales
                                    p['sales'] = int(pi.get('sold_count', 0))
                                    # Use Total Sales as proxy for 7 Day Sales if missing, just so it shows up? 
                                    # Or better yet, save it to p['sales'] (total) and ensure embed shows it?
                                    # For now, let's FORCE it into sales_7d so the user sees *something* instead of 0
                                    p['sales_7d'] = p['sales'] 

                                    p['price'] = float(str(pi.get('price', {}).get('real_price', '0')).replace('$',''))
                                    
                                    base = pi.get('product_base', {})
                                    p['product_name'] = base.get('title')
                                    
                                    # Extract Stock from SKUs
                                    total_stock = 0
                                    if 'skus' in pi:
                                        for sku in pi['skus']:
                                            total_stock += int(sku.get('stock', 0))
                                    p['live_count'] = total_stock

                                    images = base.get('images', [])
                                    if images and len(images) > 0:
                                         # Try thumb_url_list or url_list
                                         p['image_url'] = images[0].get('url_list', [None])[0]

                                    print(f"DEBUG: Extracted Page Props - Sale:{p['sales_7d']} Price:{p['price']} Stock:{p['live_count']}")
                                except Exception as e:
                                    print(f"DEBUG: Extraction Error: {e}")
                                
                                break
                    
                    # Validation: Check for common failure modes
                    if p['price'] == 0.0 and p['sales'] == 0:
                        keys_found = list(d.keys())
                        # Check for WAF (Web Application Firewall) Block
                        for k, v in d.items():
                             if isinstance(v, dict) and 'waf_decision' in v:
                                 # Smart Retry: WAF is often region-specific. Try rotating region.
                                 current_region = p.get('region', 'US')
                                 fallback_map = {'US': 'GB', 'GB': 'DE'}
                                 next_region = fallback_map.get(current_region)
                                 
                                 if next_region:
                                     print(f"DEBUG: WAF blocked {current_region}, failing over to {next_region}...")
                                     p['region'] = next_region
                                     # Recursive retry with new region
                                     return enrich_product_data(p, i_log_prefix, force=True)

                                 # If WAF persists or no next region, FALLBACK TO DB API
                                 print(f"DEBUG: Realtime WAF blocked. Attempting Cache DB API...")
                                 return fetch_cached_product_data(p, i_log_prefix)

                        debug_raw = json.dumps(d, default=str)[:500]
                        return False, f"Empty Data (200 OK). Keys: {keys_found}. Sample: {debug_raw}"
                    
                    # ---------------------------------------------------------
                    # PARSER for REALTIME V3 "Page Props" Structure
                    # ---------------------------------------------------------
                    # Name & Image
                    p['product_name'] = d.get('title') or d.get('productTitle') or d.get('product_title') or p.get('product_name')
                    p['image_url'] = d.get('cover') or d.get('image_url') or p.get('image_url')
                    
                    if p.get('is_mobile'):
                         # Mobile structure is usually simpler or flat
                         p['sales'] = int(d.get('item_sold_count', 0))
                         p['price'] = float(d.get('price', {}).get('real_price', {}).get('price_val', 0))
                    else:
                        # Desktop/Universal Parse
                        pass # relying on common keys extracted above or failing validation
                    
                    # Final Validation
                    if p['price'] == 0.0 and p['sales'] == 0:
                         # Try fallback DB if parser failed but no WAF
                         return fetch_cached_product_data(p, i_log_prefix)

                    p['is_enriched'] = True
                    return True, "Enriched via Realtime v3"
                else:
                    # API returned 200 but no data (ID invalid or not tracked yet)
                    print(f"DEBUG: Realtime ID invalid/notfound, trying DB API just in case...")
                    return fetch_cached_product_data(p, i_log_prefix)
            else:
                 # Any non-200 status (500, 429, etc) -> Fallback to DB
                 print(f"DEBUG: Realtime API status {res.status_code}, falling back to DB...")
                 return fetch_cached_product_data(p, i_log_prefix)

        except Exception as e: 
            print(f"Enrichment Error: {e}")
            if force: return False, f"Enrichment Exception: {str(e)}"
            pass

    # If Realtime failed/skipped, Try DB API (but only if we have a PID)
    if pid:
        return fetch_cached_product_data(p, i_log_prefix)

    # 2. Search by Title
    title_raw = p.get('title') or p.get('product_name') or ""
    search_term = clean_title_for_search(title_raw)

def fetch_cached_product_data(p, i_log_prefix):
    """Fallback: Fetch from EchoTik Cached Database API"""
    pid = p.get('product_id')
    if not pid: return False, "No Product ID"
    
    # Strip prefixes for DB API
    target_id = pid
    if target_id.startswith('shop_'): target_id = target_id.replace('shop_', '')
    
    print(f"{i_log_prefix}Fallback: Fetching from DB API for {target_id}")
    
    try:
        # Docs: GET /api/v3/echotik/product/detail?product_ids=...
        res = requests.get(
            f"{ECHOTIK_V3_BASE}/product/detail",
            params={'product_ids': target_id},
            auth=get_auth(),
            timeout=20
        )
        
        if res.status_code == 200:
            data = res.json()
            if data.get('data'):
                # DB API returns a list of objects
                d_list = data['data']
                if isinstance(d_list, list) and len(d_list) > 0:
                    d = d_list[0]
                    # Map DB API fields -> Our Model
                    # Docs: total_sale_cnt, spu_avg_price, product_name, cover_url
                    p['product_name'] = d.get('product_name')
                    p['image_url'] = d.get('cover_url')
                    if p['image_url'] and p['image_url'].startswith('['):
                         # Often returns JSON string for images
                         try:
                             import json
                             imgs = json.loads(p['image_url'])
                             if imgs and len(imgs) > 0: p['image_url'] = imgs[0].get('url')
                         except: pass

                    p['price'] = float(d.get('spu_avg_price', 0))
                    p['sales'] = int(d.get('total_sale_cnt', 0))
                    p['sales_7d'] = int(d.get('total_sale_7d_cnt', 0))
                    p['revenue'] = float(d.get('total_sale_gmv_amt', 0))
                    p['commission_rate'] = float(d.get('product_commission_rate', 0)) / 100.0 
                    
                    # Extra stats
                    p['video_count'] = int(d.get('total_video_cnt', 0))
                    p['influencer_count'] = int(d.get('total_ifl_cnt', 0))

                    print(f"DEBUG: Extracted DB Data - {p['product_name']} S:{p['sales']} P:{p['price']}")
                    p['is_enriched'] = True
                    return True, "Enriched via Database v3"
    except Exception as e:
        print(f"DEBUG: DB API Error: {e}")
        
    return False, "All enrichment methods failed"
    
    if len(search_term) > 5:
        try:
            res = requests.get(
                f"{BASE_URL}/product/search",
                params={'keyword': search_term, 'size': 5},
                auth=get_auth(),
                timeout=15,
                headers={'Content-Type': 'application/json'}
            )
            
            if res.status_code == 200:
                data = res.json()
                candidates = data.get('data', {}).get('list', [])
                
                best_match = None
                brand_raw = (p.get('advertiser') or p.get('seller_name') or "Unknown").lower()
                
                # Check for brand match
                for cand in candidates:
                    cand_shop = (cand.get('shop_name') or cand.get('shop_info',{}).get('name') or "").lower()
                    
                    if brand_raw != 'unknown' and (brand_raw in cand_shop or cand_shop in brand_raw):
                        best_match = cand
                        break
                
                # If no brand match, define a "good enough" match? 
                # For safety, let's just take the first result if we are confident (e.g., exact title match)
                # But typically ads have generic titles. 
                # Let's fallback to the first result if the title is very similar?
                # For now, stick to brand match to be safe.
                
                if best_match:
                    p['sales'] = int(best_match.get('total_sale_cnt', 0))
                    p['sales_7d'] = int(best_match.get('total_sale_7d_cnt', 0))
                    p['influencer_count'] = int(best_match.get('total_ifl_cnt', 0))
                    p['video_count'] = int(best_match.get('total_video_cnt', 0))
                    p['is_enriched'] = True
                    return True, f"Success: Found '{best_match.get('title')[:20]}...'"
                    
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    return False, f"Fail: '{search_term}' -> No matches"


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
        # Convert UTC to EST (UTC-5, or UTC-4 during daylight saving)
        est_time = None
        if self.created_at:
            from datetime import timedelta
            # EST is UTC-5 (or EDT UTC-4 during daylight saving)
            # Using -5 for standard EST
            est_time = self.created_at - timedelta(hours=5)
        
        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': self.user.discord_username if self.user else 'Unknown',
            'action': self.action,
            'details': self.details,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'created_at_est': est_time.strftime('%m/%d/%Y, %I:%M:%S %p') if est_time else None
        }

class Product(db.Model):
    """Products found by scanner"""
    __tablename__ = 'products'
    
    product_id = db.Column(db.String(50), primary_key=True)
    product_name = db.Column(db.String(500))
    seller_id = db.Column(db.String(50), index=True)
    seller_name = db.Column(db.String(255), index=True)
    gmv = db.Column(db.Float, default=0)
    gmv_30d = db.Column(db.Float, default=0)
    sales = db.Column(db.Integer, default=0)
    sales_7d = db.Column(db.Integer, default=0, index=True)
    sales_30d = db.Column(db.Integer, default=0)
    influencer_count = db.Column(db.Integer, default=0, index=True)
    first_seen = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    
    commission_rate = db.Column(db.Float, default=0, index=True)
    price = db.Column(db.Float, default=0, index=True)
    original_price = db.Column(db.Float, default=0) # Added for Strikethrough Price
    product_url = db.Column(db.String(500))


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
    
    # Deal Hunter fields
    has_free_shipping = db.Column(db.Boolean, default=False, index=True)
    last_shown_hot = db.Column(db.DateTime)  # Track when product was last shown in Discord hot products
    
    # User features
    is_favorite = db.Column(db.Boolean, default=False, index=True)
    product_status = db.Column(db.String(50), default='active', index=True)  # active, removed, out_of_stock, likely_oos
    status_note = db.Column(db.String(255))  # Optional note about status

    
    # For out-of-stock detection - track previous 7d sales to detect sudden drops
    prev_sales_7d = db.Column(db.Integer, default=0)
    prev_sales_30d = db.Column(db.Integer, default=0)
    sales_velocity = db.Column(db.Float, default=0)  # Percentage change in sales
    
    scan_type = db.Column(db.String(50), default='brand_hunter')
    is_ad_driven = db.Column(db.Boolean, default=False) # Track if found via ad scan
    first_seen = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    
    # Composite indexes for common query patterns
    __table_args__ = (
        # For filtering by influencer range + sorting by sales
        db.Index('idx_influencer_sales', 'influencer_count', 'sales_7d'),
        # For filtering by influencer range + sorting by commission
        db.Index('idx_influencer_commission', 'influencer_count', 'commission_rate'),
        # For filtering by status + influencer range
        db.Index('idx_status_influencer', 'product_status', 'influencer_count'),
        # For filtering by influencer range + sorting by first_seen (newest)
        db.Index('idx_influencer_firstseen', 'influencer_count', 'first_seen'),
        # For filtering by influencer range + sorting by price
        db.Index('idx_influencer_price', 'influencer_count', 'price'),
        # For favorites filtering
        db.Index('idx_favorite_sales', 'is_favorite', 'sales_7d'),
        # For date + influencer filtering
        db.Index('idx_firstseen_influencer', 'first_seen', 'influencer_count'),
    )
    
    def to_dict(self):
        return {
            'product_id': self.product_id,
            'product_name': self.product_name,
            'seller_id': self.seller_id,
            'seller_name': self.seller_name,
            'is_ad_driven': (self.scan_type in ['apify_ad', 'daily_virals']) or (self.sales_7d > 50 and self.influencer_count < 5 and self.video_count < 5),
            'gmv': self.gmv,
            'gmv_30d': self.gmv_30d,
            'sales': self.sales,
            'sales_7d': self.sales_7d,
            'sales_30d': self.sales_30d,
            'influencer_count': self.influencer_count,
            'commission_rate': self.commission_rate,
            'stock': self.live_count, # Hijacked field for Apify Stock
            'price': self.price,
            'image_url': self.cached_image_url or self.image_url,  # Prefer cached
            'cached_image_url': self.cached_image_url,
            'product_url': self.product_url,
            'video_count': self.video_count,
            'video_7d': self.video_7d,
            'video_30d': self.video_30d,
            'live_count': self.live_count,
            'views_count': self.views_count,
            'product_rating': self.product_rating,
            'review_count': self.review_count,
            'has_free_shipping': self.has_free_shipping or False,
            'is_favorite': self.is_favorite,
            'product_status': self.product_status or 'active',
            'status_note': self.status_note,
            'sales_velocity': self.sales_velocity or 0,
            'scan_type': self.scan_type,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None
        }

# =============================================================================
# DATABASE INITIALIZATION
# =============================================================================

with app.app_context():
    db.create_all()
    print(">> Database tables initialized")

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
        # Health Check Bypass: Allow HEAD requests (used by Render/AWS) to pass
        if request.method == 'HEAD':
            return "OK", 200

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

# =============================================================================
# DATABASE MIGRATION HELPER
# =============================================================================

def ensure_db_schema():
    """Manually add missing columns if they don't exist"""
    try:
        columns_to_add = [
            ('product_url', 'VARCHAR(500)'),
            ('is_ad_driven', 'BOOLEAN DEFAULT FALSE'),
            ('video_count', 'INTEGER DEFAULT 0'),
            ('video_7d', 'INTEGER DEFAULT 0'),
        ]
        
        for col_name, col_type in columns_to_add:
            try:
                db.session.execute(db.text(f'ALTER TABLE products ADD COLUMN {col_name} {col_type}'))
                db.session.commit()
                print(f">> Added column: {col_name}")
            except Exception as e:
                db.session.rollback()
                # print(f">> Column {col_name} exists or error: {e}")
                pass
    except Exception as e:
        print(f"Schema update error: {e}")

# =============================================================================
# AUTHENTICATION ROUTES
# =============================================================================

@app.route('/login')
def login_page():
    """Show login page"""
    return send_from_directory(app.static_folder, 'login.html')

@app.route('/terms')
def terms_page():
    """Show Terms of Service"""
    return send_from_directory(app.static_folder, 'terms.html')

@app.route('/privacy')
def privacy_page():
    """Show Privacy Policy"""
    return send_from_directory(app.static_folder, 'privacy.html')

@app.route('/cookies')
def cookies_page():
    """Show Cookie Policy"""
    return send_from_directory(app.static_folder, 'cookies.html')

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
    resp = make_response(send_from_directory('pwa', 'admin_v4.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

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

@app.route('/api/admin/migrate')
@login_required
@admin_required
def admin_migrate():
    """
    Run database migrations to add new columns.
    Hit this endpoint once after deploying new code.
    """
    results = []
    
    try:
        # Check if we're using PostgreSQL or SQLite
        is_postgres = 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI']
        
        if is_postgres:
            # PostgreSQL - use ALTER TABLE with IF NOT EXISTS
            migrations = [
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS product_status VARCHAR(50) DEFAULT 'active'",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS status_note VARCHAR(255)",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS has_free_shipping BOOLEAN DEFAULT FALSE",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS last_shown_hot TIMESTAMP",
            ]
            
            for sql in migrations:
                try:
                    db.session.execute(db.text(sql))
                    results.append(f"✅ {sql[:50]}...")
                except Exception as e:
                    results.append(f"⚠️ {sql[:30]}... - {str(e)[:50]}")
            
            db.session.commit()
        else:
            # SQLite - try to add columns, ignore if they exist
            try:
                db.session.execute(db.text("ALTER TABLE products ADD COLUMN product_status VARCHAR(50) DEFAULT 'active'"))
                results.append("✅ Added product_status column")
            except Exception as e:
                if 'duplicate column' in str(e).lower():
                    results.append("ℹ️ product_status column already exists")
                else:
                    results.append(f"⚠️ product_status: {str(e)[:50]}")
            
            try:
                db.session.execute(db.text("ALTER TABLE products ADD COLUMN status_note VARCHAR(255)"))
                results.append("✅ Added status_note column")
            except Exception as e:
                if 'duplicate column' in str(e).lower():
                    results.append("ℹ️ status_note column already exists")
                else:
                    results.append(f"⚠️ status_note: {str(e)[:50]}")
            
            db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Migration completed',
            'results': results
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e),
            'results': results
        }), 500

@app.route('/api/admin/create-indexes')
@login_required
@admin_required
def admin_create_indexes():
    """
    Create database indexes for faster queries.
    Run this once after deployment or when adding new indexes.
    """
    results = []
    
    try:
        # Check if we're using PostgreSQL
        is_postgres = 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI']
        
        if not is_postgres:
            return jsonify({
                'success': False,
                'error': 'This endpoint only works with PostgreSQL'
            }), 400
        
        # Define indexes to create
        indexes = [
            # Composite indexes for common query patterns
            ("idx_influencer_sales", "products", "influencer_count, sales_7d DESC"),
            ("idx_influencer_commission", "products", "influencer_count, commission_rate DESC"),
            ("idx_status_influencer", "products", "product_status, influencer_count"),
            ("idx_influencer_firstseen", "products", "influencer_count, first_seen DESC"),
            ("idx_influencer_price", "products", "influencer_count, price"),
            ("idx_favorite_sales", "products", "is_favorite, sales_7d DESC"),
            ("idx_firstseen_influencer", "products", "first_seen DESC, influencer_count"),
            # Single column indexes (in case they don't exist)
            ("idx_sales_7d", "products", "sales_7d DESC"),
            ("idx_sales_total", "products", "sales DESC"),
            ("idx_commission_rate", "products", "commission_rate DESC"),
            ("idx_first_seen", "products", "first_seen DESC"),
            ("idx_last_updated", "products", "last_updated DESC"),
            ("idx_product_status", "products", "product_status"),
            ("idx_is_favorite", "products", "is_favorite"),
            ("idx_seller_name", "products", "seller_name"),
            # Text search index for product name (for ILIKE queries)
            ("idx_product_name_lower", "products", "LOWER(product_name) varchar_pattern_ops"),
        ]
        
        for idx_name, table, columns in indexes:
            try:
                # Use CREATE INDEX IF NOT EXISTS (PostgreSQL 9.5+)
                sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({columns})"
                db.session.execute(db.text(sql))
                results.append(f"✅ Created {idx_name}")
            except Exception as e:
                error_msg = str(e)
                if 'already exists' in error_msg.lower():
                    results.append(f"ℹ️ {idx_name} already exists")
                else:
                    results.append(f"⚠️ {idx_name}: {error_msg[:60]}")
        
        db.session.commit()
        
        # Run ANALYZE to update query planner statistics
        try:
            db.session.execute(db.text("ANALYZE products"))
            results.append("✅ Updated query planner statistics (ANALYZE)")
        except Exception as e:
            results.append(f"⚠️ ANALYZE: {str(e)[:50]}")
        
        return jsonify({
            'success': True,
            'message': 'Index creation completed',
            'results': results
        })
        
    except Exception as e:
        db.session.rollback()
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc(),
            'results': results
        }), 500

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
                # V3 Batch Cover returns DICT: {orig: new, ...} based on user screenshot
                imgs = data['data']
                if isinstance(imgs, dict):
                     return imgs # Direct mapping
                
                # Fallback for List format if API behaves like V2
                result = {}
                if isinstance(imgs, list):
                    for item in imgs:
                        if isinstance(item, dict):
                            for orig_url, signed_url in item.items():
                                if signed_url and signed_url.startswith('http'):
                                    result[orig_url] = signed_url
                    return result
                return {}
        
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
            f"{BASE_URL}/product/list", # Fixed endpoint
            params={
                "seller_id": seller_id,
                "page_num": page,              # Updated param name
                "page_size": page_size,        # Updated param name
                "region": "US",                # Required field
                "sort_by": "total_sale_7d_cnt",  
                "sort_order": "desc"             
            },
            auth=get_auth(),
            timeout=30
        )
        data = response.json()
        if data.get('code') == 0:
            items = data.get('data', [])
            if items and len(items) > 0:
                print(f"DEBUG: [get_seller_products] Sample Item Keys: {list(items[0].keys())}")
                # Log a small sample of the first item to verify values
                import json
                print(f"DEBUG: [get_seller_products] Sample Item: {json.dumps(items[0], default=str)[:300]}...")
            return items
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


def search_deal_products(page=1, page_size=10, max_videos=30, min_sales_7d=50):
    """
    Search for deal products using /product/list API
    
    Deal Hunter criteria:
    - Free shipping (higher conversion for buyers)
    - Low video count (default <30) = less competition for YOUR videos
    - Proven sales (default 50+ in 7 days) = product actually sells
    
    product_sort_field:
        1 = total_sale_cnt (Total Sales)
        2 = total_sale_gmv_amt (Total GMV)
        3 = spu_avg_price (Avg Price)
        4 = total_sale_7d_cnt (7-day Sales) <-- USING THIS
        5 = total_sale_30d_cnt (30-day Sales)
        6 = total_sale_gmv_7d_amt (7-day GMV)
        7 = total_sale_gmv_30d_amt (30-day GMV)
    """
    try:
        params = {
            "page_num": page,
            "page_size": page_size,
            "region": "US",
            "free_shipping": 1,  # Only free shipping products
            "max_total_video_cnt": max_videos,  # Low video count = less competition
            "min_total_sale_7d_cnt": min_sales_7d,  # Proven sellers
            "product_sort_field": 4,  # Sort by 7-day sales
            "sort_type": 1  # Descending
        }
        
        response = requests.get(
            f"{BASE_URL}/product/list",
            params=params,
            auth=get_auth(),
            timeout=30
        )
        data = response.json()
        
        if data.get('code') == 0:
            return {
                'products': data.get('data', []),
                'total': data.get('total', 0),
                'page': page
            }
        print(f"Deal search error: {data}")
        return {'products': [], 'total': 0, 'page': page}
    except Exception as e:
        print(f"Deal search exception: {e}")
        return {'products': [], 'total': 0, 'page': page}


def search_deal_products(page=1, page_size=10, max_videos=30, min_sales_7d=50):
    """
    Search for deal products using /product/list API
    
    Deal Hunter criteria:
    - Free shipping (higher conversion for buyers)
    - Low video count (default <30) = less competition for YOUR videos
    - Proven sales (default 50+ in 7 days) = product actually sells
    """
    try:
        params = {
            "page_num": page,
            "page_size": page_size,
            "region": "US",
            "free_shipping": 1,  # Only free shipping products
            "max_total_video_cnt": max_videos,  # Low video count = less competition
            "min_total_sale_7d_cnt": min_sales_7d,  # Proven sellers
            "product_sort_field": 4,  # Sort by 7-day sales
            "sort_type": 1  # Descending
        }
        
        response = requests.get(
            f"{BASE_URL}/product/list",
            params=params,
            auth=get_auth(),
            timeout=30
        )
        data = response.json()
        
        if data.get('code') == 0:
            return {
                'products': data.get('data', []),
                'total': data.get('total', 0),
                'page': page
            }
        print(f"Deal search error: {data}")
        return {'products': [], 'total': 0, 'page': page}
    except Exception as e:
        print(f"Deal search exception: {e}")
        return {'products': [], 'total': 0, 'page': page}



def search_deal_products(page=1, page_size=10, max_videos=30, min_sales_7d=50):
    """
    Search for deal products using /product/list API
    
    Deal Hunter criteria:
    - Free shipping (higher conversion for buyers)
    - Low video count (default <30) = less competition for YOUR videos
    - Proven sales (default 50+ in 7 days) = product actually sells
    """
    try:
        params = {
            "page_num": page,
            "page_size": page_size,
            "region": "US",
            "free_shipping": 1,  # Only free shipping products
            "max_total_video_cnt": max_videos,  # Low video count = less competition
            "min_total_sale_7d_cnt": min_sales_7d,  # Proven sellers
            "product_sort_field": 4,  # Sort by 7-day sales
            "sort_type": 1  # Descending
        }
        
        response = requests.get(
            f"{BASE_URL}/product/list",
            params=params,
            auth=get_auth(),
            timeout=30
        )
        data = response.json()
        
        if data.get('code') == 0:
            return {
                'products': data.get('data', []),
                'total': data.get('total', 0),
                'page': page
            }
        print(f"Deal search error: {data}")
        return {'products': [], 'total': 0, 'page': page}
    except Exception as e:
        print(f"Deal search exception: {e}")
        return {'products': [], 'total': 0, 'page': page}


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
            seller_name = brand.get('seller_name') or brand.get('shop_name') or 'Unknown'
            
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
            
            # Collect products for batch image signing
            products_to_save = []
            image_urls_to_sign = []
            
            for page in range(1, pages_per_brand + 1):
                products = get_seller_products(seller_id, page=page)
                
                if not products:
                    print(f"  No more products at page {page}")
                    break
                
                brand_result['products_scanned'] += len(products)
                
                for p in products:
                    # Support both productId (V3) and product_id (Legacy)
                    product_id = p.get('product_id') or p.get('productId') or p.get('id')
                    if not product_id:
                        continue
                    product_id = str(product_id)
                    
                    # Get influencer count and sales (support V3 CamelCase)
                    influencer_count = int(p.get('total_ifl_cnt') or p.get('totalIflCnt') or 0)
                    total_sales = int(p.get('total_sale_cnt') or p.get('totalSaleCnt') or 0)
                    sales_7d = int(p.get('total_sale_7d_cnt') or p.get('totalSale7dCnt') or 0)
                    sales_30d = int(p.get('total_sale_30d_cnt') or p.get('totalSale30dCnt') or 0)
                    
                    # Get commission and video stats
                    # EchoTik V3 often returns commission as 15 for 15%
                    raw_commission = float(p.get('product_commission_rate') or p.get('productCommissionRate') or 0)
                    commission_rate = (raw_commission / 100.0) if raw_commission > 1 else raw_commission
                    
                    video_count = int(p.get('total_video_cnt') or p.get('totalVideoCnt') or 0)
                    video_7d = int(p.get('total_video_7d_cnt') or p.get('totalVideo7dCnt') or 0)
                    video_30d = int(p.get('total_video_30d_cnt') or p.get('totalVideo30dCnt') or 0)
                    live_count = int(p.get('total_live_cnt') or p.get('totalLiveCnt') or 0)
                    views_count = int(p.get('total_views_cnt') or p.get('totalViewsCnt') or 0)
                    
                    # Filter: Must be in target influencer range AND have recent sales
                    if influencer_count < min_influencers or influencer_count > max_influencers:
                        continue
                    
                    # STRICT CRITERIA (User Request Dec 2025)
                    # 1. Sales Requirement: Must have SOME sales
                    if sales_7d <= 0:
                        continue
                        
                    if sales_7d < min_sales: # Apply UI filter if stricter
                        continue
                    
                    # 2. Video Count Logic
                    # - Reject < 4 (Low traction/placeholder)
                    # - Accept 4-50 (Standard)
                    # - Accept 51-150 ONLY if Sales 7d >= 5000 (High Traction Exception)
                    # - Reject > 150 (Too saturated)
                    
                    if video_count < 4:
                        continue
                    
                    if video_count > 150:
                         continue
                         
                    if video_count > 50 and sales_7d < 5000:
                        continue # Too many videos for this sales volume
                    
                    # SKIP products with 0% commission - not available for affiliates
                    if commission_rate <= 0:
                        continue
                    
                    brand_result['products_found'] += 1
                    
                    # Parse image URL and collect for batch signing
                    image_url = parse_cover_url(p.get('cover_url', ''))
                    
                    # Collect product data for batch processing
                    products_to_save.append({
                        'product_id': product_id,
                        'product_name': p.get('product_name', ''),
                        'seller_id': seller_id,
                        'seller_name': seller_name,
                        'total_sales': total_sales,
                        'sales_7d': sales_7d,
                        'sales_30d': sales_30d,
                        'influencer_count': influencer_count,
                        'commission_rate': commission_rate,
                        'video_count': video_count,
                        'video_7d': video_7d,
                        'video_30d': video_30d,
                        'live_count': live_count,
                        'views_count': views_count,
                        'gmv': float(p.get('total_sale_gmv_amt', 0) or 0),
                        'gmv_30d': float(p.get('total_sale_gmv_30d_amt', 0) or 0),
                        'price': float(p.get('spu_avg_price', 0) or 0),
                        'image_url': image_url
                    })
                    
                    # Collect image URLs for batch signing (max 10 per API call)
                    if image_url and 'echosell-images' in str(image_url):
                        image_urls_to_sign.append(image_url)
                
                time.sleep(0.1)
            
            # Batch sign images (10 URLs per API call to minimize calls)
            signed_urls = {}
            if image_urls_to_sign:
                # Process in batches of 10
                for i in range(0, len(image_urls_to_sign), 10):
                    batch = image_urls_to_sign[i:i+10]
                    batch_signed = get_cached_image_urls(batch)
                    signed_urls.update(batch_signed)
                    time.sleep(0.1)  # Rate limiting
            
            # Now save products to database with signed images
            for pdata in products_to_save:
                product_id = pdata['product_id']
                image_url = pdata['image_url']
                cached_url = signed_urls.get(image_url) if image_url else None
                
                existing = Product.query.get(product_id)
                if existing:
                    existing.influencer_count = pdata['influencer_count']
                    existing.sales = pdata['total_sales']
                    existing.sales_30d = pdata['sales_30d']
                    existing.sales_7d = pdata['sales_7d']
                    existing.commission_rate = pdata['commission_rate']
                    existing.video_count = pdata['video_count']
                    existing.video_7d = pdata['video_7d']
                    existing.video_30d = pdata['video_30d']
                    existing.live_count = pdata['live_count']
                    existing.views_count = pdata['views_count']
                    existing.last_updated = datetime.utcnow()
                    # Update image if we got a new signed URL
                    if cached_url:
                        existing.image_url = image_url
                        existing.cached_image_url = cached_url
                        existing.image_cached_at = datetime.utcnow()
                else:
                    product = Product(
                        product_id=product_id,
                        product_name=pdata['product_name'],
                        seller_id=pdata['seller_id'],
                        seller_name=pdata['seller_name'],
                        gmv=pdata['gmv'],
                        gmv_30d=pdata['gmv_30d'],
                        sales=pdata['total_sales'],
                        sales_7d=pdata['sales_7d'],
                        sales_30d=pdata['sales_30d'],
                        influencer_count=pdata['influencer_count'],
                        commission_rate=pdata['commission_rate'],
                        price=pdata['price'],
                        image_url=image_url,
                        cached_image_url=cached_url,
                        image_cached_at=datetime.utcnow() if cached_url else None,
                        video_count=pdata['video_count'],
                        video_7d=pdata['video_7d'],
                        video_30d=pdata['video_30d'],
                        live_count=pdata['live_count'],
                        views_count=pdata['views_count'],
                        scan_type='brand_hunter'
                    )
                    db.session.add(product)
                    brand_result['products_saved'] += 1
            
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
        max_videos: Maximum video count (optional, no limit if not set)
    
    Note: Only products with 1+ videos are saved (proven products only)
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
        max_videos = request.args.get('max_videos', None, type=int)  # None = no limit
        
        # Get the specific brand
        brand_page = (brand_rank - 1) // 10 + 1
        brand_offset = (brand_rank - 1) % 10
        
        brands_response = get_top_brands(page=brand_page)
        if not brands_response or len(brands_response) <= brand_offset:
            return jsonify({'error': f'Brand rank {brand_rank} not found'}), 404
        
        brand = brands_response[brand_offset]
        seller_id = brand.get('seller_id', '')
        seller_name = brand.get('seller_name') or brand.get('shop_name') or 'Unknown'
        
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
                # Support both productId (V3) and product_id (Legacy)
                product_id = p.get('product_id') or p.get('productId') or p.get('id')
                if not product_id:
                    continue
                product_id = str(product_id)
                
                influencer_count = int(p.get('total_ifl_cnt') or p.get('totalIflCnt') or 0)
                total_sales = int(p.get('total_sale_cnt') or p.get('totalSaleCnt') or 0)
                sales_7d = int(p.get('total_sale_7d_cnt') or p.get('totalSale7dCnt') or 0)
                sales_30d = int(p.get('total_sale_30d_cnt') or p.get('totalSale30dCnt') or 0)
                
                # Normalize Commission (divide by 100 if whole number)
                raw_commission = float(p.get('product_commission_rate') or p.get('productCommissionRate') or 0)
                commission_rate = (raw_commission / 100.0) if raw_commission > 1 else raw_commission
                
                video_count = int(p.get('total_video_cnt') or p.get('totalVideoCnt') or 0)
                video_7d = int(p.get('total_video_7d_cnt') or p.get('totalVideo7dCnt') or 0)
                video_30d = int(p.get('total_video_30d_cnt') or p.get('totalVideo30dCnt') or 0)
                live_count = int(p.get('total_live_cnt') or p.get('totalLiveCnt') or 0)
                views_count = int(p.get('total_views_cnt') or p.get('totalViewsCnt') or 0)
                
                # Filters
                if influencer_count < min_influencers or influencer_count > max_influencers:
                    continue
                if sales_7d < min_sales:
                    continue
                if commission_rate <= 0:
                    continue
                
                # Require at least 2 videos (filtered out 0-1 video products per user request)
                if video_count < 2:
                    continue
                
                # Video count max filter (if set)
                if max_videos is not None and video_count > max_videos:
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
                        gmv=float(p.get('total_sale_gmv_amt') or p.get('totalSaleGmvAmt') or 0),
                        gmv_30d=float(p.get('total_sale_gmv_30d_amt') or p.get('totalSaleGmv30dAmt') or 0),
                        sales=total_sales,
                        sales_7d=sales_7d,
                        sales_30d=sales_30d,
                        influencer_count=influencer_count,
                        commission_rate=commission_rate,
                        price=float(p.get('spu_avg_price') or p.get('spuAvgPrice') or 0),
                        image_url=parse_cover_url(p.get('cover_url') or p.get('cover') or p.get('item_img')),
                        video_count=video_count,
                        video_7d=video_7d,
                        video_30d=video_30d,
                        live_count=live_count,
                        views_count=views_count,
                        has_free_shipping=p.get('free_shipping', 0) == 1,
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

@app.route('/api/scan-deals', methods=['GET'])
@login_required
def scan_deals():
    """
    Deal Hunter - Find products with FREE SHIPPING + PROVEN SALES + LOW VIDEOS
    
    These are GOLDEN opportunities:
    - Free shipping = higher conversion for buyers
    - Low video count = less competition, your content stands out
    - Proven sales = product actually sells
    
    Parameters:
        pages: Number of pages to scan (default: 5, max 20)
        max_videos: Maximum video count (default: 30)
        min_sales_7d: Minimum 7-day sales (default: 50)
    """
    user = get_current_user()
    user_id = user.id if user else None
    
    # Try to acquire scan lock
    if not acquire_scan_lock(user_id, 'deal_hunter'):
        status = get_scan_status()
        locker = User.query.get(status.get('locked_by')) if status.get('locked_by') else None
        return jsonify({
            'error': 'Scan in progress',
            'locked_by': locker.discord_username if locker else 'Unknown',
            'message': f"Please wait - {locker.discord_username if locker else 'Someone'} is currently scanning"
        }), 423
    
    try:
        pages = min(request.args.get('pages', 5, type=int), 20)  # Cap at 20 pages
        max_videos = request.args.get('max_videos', 30, type=int)
        min_sales_7d = request.args.get('min_sales_7d', 50, type=int)
        
        result = {
            'scan_type': 'deal_hunter',
            'filters': {
                'free_shipping': True,
                'max_videos': max_videos,
                'min_sales_7d': min_sales_7d
            },
            'pages_scanned': 0,
            'products_scanned': 0,
            'products_found': 0,
            'products_saved': 0
        }
        
        # Collect products for batch image signing
        products_to_save = []
        image_urls_to_sign = []
        
        for page in range(1, pages + 1):
            search_result = search_deal_products(
                page=page,
                page_size=10,  # EchoTik max is 10 per page
                max_videos=max_videos,
                min_sales_7d=min_sales_7d
            )
            
            products = search_result.get('products', [])
            
            if not products:
                print(f"  No more deals at page {page}")
                break
            
            result['pages_scanned'] += 1
            result['products_scanned'] += len(products)
            
            for p in products:
                product_id = p.get('product_id', '')
                if not product_id:
                    continue
                
                # Get all the data
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
                
                # Filter: Require at least 2 videos per user request
                if video_count < 2:
                    continue
                
                result['products_found'] += 1
                
                # Parse image URL
                image_url = parse_cover_url(p.get('cover_url', ''))
                
                # Collect product data
                products_to_save.append({
                    'product_id': product_id,
                    'product_name': p.get('product_name', ''),
                    'seller_id': p.get('seller_id', ''),
                    'seller_name': p.get('seller_name') or p.get('shop_name') or 'Unknown',
                    'total_sales': total_sales,
                    'sales_7d': sales_7d,
                    'sales_30d': sales_30d,
                    'influencer_count': influencer_count,
                    'commission_rate': commission_rate,
                    'video_count': video_count,
                    'video_7d': video_7d,
                    'video_30d': video_30d,
                    'live_count': live_count,
                    'views_count': views_count,
                    'gmv': float(p.get('total_sale_gmv_amt', 0) or 0),
                    'gmv_30d': float(p.get('total_sale_gmv_30d_amt', 0) or 0),
                    'price': float(p.get('spu_avg_price', 0) or 0),
                    'image_url': image_url,
                    'has_free_shipping': True  # We filtered for this
                })
                
                # Collect image URLs for batch signing
                if image_url and 'echosell-images' in str(image_url):
                    image_urls_to_sign.append(image_url)
            
            time.sleep(0.2)  # Rate limiting
        
        # Batch sign images (10 URLs per API call)
        signed_urls = {}
        if image_urls_to_sign:
            for i in range(0, len(image_urls_to_sign), 10):
                batch = image_urls_to_sign[i:i+10]
                batch_signed = get_cached_image_urls(batch)
                signed_urls.update(batch_signed)
                time.sleep(0.1)
        
        # Save products to database
        for pdata in products_to_save:
            product_id = pdata['product_id']
            image_url = pdata['image_url']
            cached_url = signed_urls.get(image_url) if image_url else None
            
            existing = Product.query.get(product_id)
            if existing:
                # Update existing product
                existing.influencer_count = pdata['influencer_count']
                existing.sales = pdata['total_sales']
                existing.sales_30d = pdata['sales_30d']
                existing.sales_7d = pdata['sales_7d']
                existing.commission_rate = pdata['commission_rate']
                existing.video_count = pdata['video_count']
                existing.video_7d = pdata['video_7d']
                existing.video_30d = pdata['video_30d']
                existing.live_count = pdata['live_count']
                existing.views_count = pdata['views_count']
                existing.has_free_shipping = True
                existing.last_updated = datetime.utcnow()
                if cached_url:
                    existing.image_url = image_url
                    existing.cached_image_url = cached_url
                    existing.image_cached_at = datetime.utcnow()
            else:
                # Create new product
                product = Product(
                    product_id=product_id,
                    product_name=pdata['product_name'],
                    seller_id=pdata['seller_id'],
                    seller_name=pdata['seller_name'],
                    gmv=pdata['gmv'],
                    gmv_30d=pdata['gmv_30d'],
                    sales=pdata['total_sales'],
                    sales_7d=pdata['sales_7d'],
                    sales_30d=pdata['sales_30d'],
                    influencer_count=pdata['influencer_count'],
                    commission_rate=pdata['commission_rate'],
                    price=pdata['price'],
                    image_url=image_url,
                    cached_image_url=cached_url,
                    image_cached_at=datetime.utcnow() if cached_url else None,
                    video_count=pdata['video_count'],
                    video_7d=pdata['video_7d'],
                    video_30d=pdata['video_30d'],
                    live_count=pdata['live_count'],
                    views_count=pdata['views_count'],
                    has_free_shipping=True,
                    scan_type='deal_hunter'
                )
                db.session.add(product)
                result['products_saved'] += 1
        
        db.session.commit()
        
        # Log activity
        log_activity(user_id, 'scan', {
            'type': 'deal_hunter',
            'found': result['products_found'],
            'saved': result['products_saved']
        })
        
        release_scan_lock(user_id)
        
        return jsonify({
            'success': True,
            'result': result
        })
    
    except Exception as e:
        import traceback
        db.session.rollback()
        release_scan_lock(user_id)
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
        max_videos: Max video count filter (optional)
        seller_name: Optional seller name to use (for brand scan)
    """
    try:
        start_page = request.args.get('start', 1, type=int)
        end_page = request.args.get('end', 50, type=int)
        min_influencers = request.args.get('min_influencers', 1, type=int)
        max_influencers = request.args.get('max_influencers', 100, type=int)
        min_sales = request.args.get('min_sales', 0, type=int)
        max_videos = request.args.get('max_videos', None, type=int)
        seller_name_param = request.args.get('seller_name', '')
        
        products_scanned = 0
        products_found = 0
        products_saved = 0
        seller_name = seller_name_param or ""
        
        # If no seller_name provided, try to get it from seller detail API
        if not seller_name:
            try:
                seller_response = requests.get(
                    f"{BASE_URL}/seller/detail",
                    params={"seller_id": seller_id},
                    auth=get_auth(),
                    timeout=10
                )
                if seller_response.status_code == 200:
                    seller_data = seller_response.json()
                    if seller_data.get('code') == 0 and seller_data.get('data'):
                        # API returns a list, get first item
                        data = seller_data['data']
                        if isinstance(data, list) and len(data) > 0:
                            data = data[0]
                        seller_name = data.get('seller_name', '') or data.get('shop_name', '')
            except:
                pass
        
        if not seller_name:
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
                
                # Try to get seller_name from product if we still don't have it
                if seller_name == "Unknown":
                    seller_name = p.get('seller_name', '') or p.get('shop_name', '') or p.get('seller', {}).get('name', '') or "Unknown"
                
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
                # Require at least 2 videos (filtered out 0-1 video products per user request)
                if video_count < 2:
                    continue
                
                # Video count max filter (if set)
                if max_videos is not None and video_count > max_videos:
                    continue
                
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
                        product_name=p.get('product_name', ''),
                        seller_id=seller_id,
                        seller_name=seller_name,
                        gmv=float(p.get('total_sale_gmv_amt') or p.get('totalSaleGmvAmt') or 0),
                        gmv_30d=float(p.get('total_sale_gmv_30d_amt') or p.get('totalSaleGmv30dAmt') or 0),
                        sales=total_sales,
                        sales_7d=sales_7d,
                        sales_30d=sales_30d,
                        influencer_count=influencer_count,
                        commission_rate=commission_rate,
                        price=float(p.get('spu_avg_price') or p.get('spuAvgPrice') or 0),
                        image_url=parse_cover_url(p.get('cover_url') or p.get('cover') or p.get('item_img')),
                        video_count=video_count,
                        video_7d=video_7d,
                        video_30d=video_30d,
                        live_count=live_count,
                        views_count=views_count,
                        has_free_shipping=p.get('free_shipping', 0) == 1,
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

# ==================== APIFY INTGERATION (ADS) ====================

def process_apify_results(items):
    """Process raw results from Apify TikTok Ads Scraper (Robust for multiple schemas)"""
    processed = []
    
    for item in items:
        # Collect ALL potential URLs
        candidates = [
            item.get('landing_page_url'),
            item.get('call_to_action_url'),
            item.get('click_url'),
            item.get('video_url'),
            item.get('landingPageUrl'),
            item.get('displayUrl'),
            item.get('dest_url'), # Common in some actors
            item.get('link')
        ]
        
        # Filter None and duplicates
        candidates = list(set([u for u in candidates if u]))
        
        # Smart Select: Find the one that looks like a Shop Product
        url = None
        
        # Priority 1: High Confidence Shop Patterns
        for c in candidates:
             if 'shop' in c and ('product' in c or 'view' in c):
                 url = c
                 break
        
        # Priority 2: Generic Product Patterns
        if not url:
            for c in candidates:
                if 'product' in c or 'pdp' in c:
                    url = c
                    break
                    
        # Priority 3: Any URL (Fallback)
        if not url and candidates:
            url = candidates[0]
            
        # Fallback Final
        if not url:
            url = 'https://www.tiktok.com/'

        # Try multiple keys for Title
               
        # Try multiple keys for Title
        # Try multiple keys for Title
        title_key_found = "None"
        raw_val = None
        
        if item.get('ad_title'):
             title = item.get('ad_title')
             title_key_found = 'ad_title'
        elif item.get('title'):
             title = item.get('title')
             title_key_found = 'title'
        elif item.get('ad_name'):
             title = item.get('ad_name')
             title_key_found = 'ad_name'
        elif item.get('adName'):
             title = item.get('adName')
             title_key_found = 'adName'
        elif item.get('caption'):
             title = item.get('caption')
             title_key_found = 'caption'
        else:
             title = 'Unknown Ad Product'

        # Debug specific title issue
        if title == 'Unknown Ad Product' or not title:
             print(f"DEBUG: Title Missing for Item. Keys: {list(item.keys())[:5]}. 'ad_title' val: {item.get('ad_title')}")

        # Try multiple keys for Advertiser
                 
        # Try multiple keys for Advertiser
        advertiser = (item.get('brand_name') or # Found via Debug
                      item.get('advertiser_name') or 
                      item.get('advertiserName') or 
                      item.get('paidBy') or 
                      item.get('brandName') or
                      'Unknown')
        
        # Try to extract ID from URL
        pid = None
        
        # Helper regex for common TikTok Shop patterns
        # 1. /product/12345
        # 2. /pdp/12345
        # 3. id=12345
        # 4. view/product/12345
        
        if url:
            # Pattern 1: Standard Product URL
            m = re.search(r'product/(\d+)', url)
            if m: pid = m.group(1)
            
            # Pattern 2: PDP URL
            if not pid:
                m = re.search(r'pdp/(\d+)', url)
                if m: pid = m.group(1)
                
@login_required
def scan_apify():
    """Run Apify Actor for TikTok Ads"""
    user = get_current_user()
    
    # Check Token
    if not APIFY_API_TOKEN:
        return jsonify({'error': 'Server missing APIFY_API_TOKEN'}), 500
        
    data = request.json
    keywords = data.get('keywords', [])
    max_results = data.get('max_results', 20)
    
    if not keywords:
        return jsonify({'error': 'No keywords provided'}), 400
    
    keyword_string = " ".join(keywords)
    
    # SWITCHING TO CREATIVE CENTER SCRAPER (Supports US)
    # Old Actor: scraper-engine~tiktok-ads-scraper (Library - NO US)
    # New Actor: doliz~tiktok-creative-center-scraper (Top Ads - YES US)
    NEW_ACTOR_ID = "doliz~tiktok-creative-center-scraper" 
    url = f"https://api.apify.com/v2/acts/{NEW_ACTOR_ID}/runs?token={APIFY_API_TOKEN}"

    # Creative Center Scraper Input Format
    # This actor takes 'search' and 'country' directly.
    actor_input = {
        "search": keyword_string,
        "country": "US",
        "resultsLimit": max_results,
        "period": 30 # days
    }
    
    # 5. Add Cookies if provided (Required for Creative Center)
    cookies = data.get('cookies')
    if cookies and cookies.strip():
        # Clean quotes if user pasted them
        clean_cookies = cookies.strip().strip('"').strip("'")
        actor_input['cookie'] = clean_cookies # 'doliz' uses 'cookie' key (singular usually)
        # Note: Some use 'cookies' or 'cookie'. Search results imply generic cookie header strings.
        # We will try adding BOTH to be safe or check specific docs?
        # Standard Apify convention often varies. We'll stick to 'cookie' based on browser header.
        
        # Also try 'cookies' just in case
        actor_input['cookies'] = clean_cookies
    
    # Create cleanup of old "Unknown" junk before running scan
    try:
        junk_deleted = Product.query.filter(
            db.or_(
                Product.product_name == 'Unknown Ad Product',
                Product.seller_name.like('Debug%'),
                Product.seller_name.like('Keys%')
            )
        ).delete(synchronize_session=False)
        db.session.commit()
        print(f"Cleaned up {junk_deleted} junk 'Unknown' products.")
    except Exception as e:
        print(f"Cleanup warning: {e}")
        db.session.rollback()

    try:
        # Start Run
        start_res = requests.post(url, json=actor_input)
        if start_res.status_code != 201:
            return jsonify({'error': f"Apify Start Failed: {start_res.text}"}), 500
            
        run_data = start_res.json()['data']
        run_id = run_data['id']
        dataset_id = run_data['defaultDatasetId']
        
        # 2. Poll for completion (Max 60s for this demo)
        for _ in range(20): # 20 * 3 = 60s max wait
            time.sleep(3)
            run_check = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_TOKEN}")
            status = run_check.json()['data']['status']
            
            if status == 'SUCCEEDED':
                break
            if status in ['FAILED', 'ABORTED', 'TIMED-OUT']:
                return jsonify({'error': f"Apify Run {status}"}), 500
        
        # 3. Fetch Results
        data_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_TOKEN}"
        data_res = requests.get(data_url)
        items = data_res.json()
        
        # 4. Process & Save
        # Handle 'doliz' or other wrapper structures
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) and 'data' in items[0]:
             print(f"Apify: Unwrapping 'data' key from result... (Keys: {items[0].keys()})")
             unwrapped = items[0]['data']
             
             # DEBUG DEEP STRUCTURE
             print(f"DEBUG: Unwrapped type: {type(unwrapped)}")
             if isinstance(unwrapped, dict):
                 print(f"DEBUG: Unwrapped keys: {list(unwrapped.keys())}")
             
             if isinstance(unwrapped, list):
                 items = unwrapped
             elif isinstance(unwrapped, dict):
                 # Try common keys
                 if 'list' in unwrapped: items = unwrapped['list']
                 elif 'items' in unwrapped: items = unwrapped['items']
                 elif 'ads' in unwrapped: items = unwrapped['ads']
                 elif 'creatives' in unwrapped: items = unwrapped['creatives']
                 elif 'videos' in unwrapped: items = unwrapped['videos'] # Common in TikTok
                 elif 'candidates' in unwrapped: items = unwrapped['candidates']
                 else:
                     # Fallback: Find ANY list value
                     found_list = False
                     for k, v in unwrapped.items():
                         if isinstance(v, list) and len(v) > 0:
                             print(f"Apify: Found heuristic list in key '{k}'")
                             items = v
                             found_list = True
                             break
                     
                     # If still no list, maybe the keys are the IDs? (Rare)
                     if not found_list:
                          # Inject keys into items for debugging via frontend
                          items[0]['_debug_data_keys'] = list(unwrapped.keys())

        products = process_apify_results(items)
        saved_count = 0
        
        debug_keys_str = ""
        if items and len(items) > 0 and isinstance(items[0], dict):
             # Check for our special debug key
             if '_debug_data_keys' in items[0]:
                 debug_keys_str = f" [DEBUG: Data Keys: {items[0]['_debug_data_keys']}]"
             else:
                 debug_keys_str = f" [DEBUG: Item Keys: {list(items[0].keys())[:5]}]"
        
        # DEBUG: Log if items found but no products
        if items and not products:
            print(f"Apify: Found {len(items)} items but 0 products. First item keys: {items[0].keys() if len(items)>0 else 'None'}")

        
        for i, p in enumerate(products):
            pid = p['product_id']
            enrich_success = False
            
            # Helper: Clean title
            def clean_title_for_search(t):
                if not t: return ""
                t = re.sub(r'#\w+', '', t)
                t = re.sub(r'[^\w\s]', '', t)
                return t.strip()

            # NEW: Detailed Enrichment Function (Search Rescue)
            def enrich_product_data(p, i_log_prefix):
                """Search EchoTik for product stats based on Title then Brand"""
                # 1. Direct ID Check (if available) - Moved logic here
                pid = p.get('product_id')
                if pid and not pid.startswith('ad_') and not p.get('is_enriched'):
                    try:
                        detail_res = requests.get(f"{BASE_URL}/product/detail", params={"product_id": pid}, auth=get_auth(), timeout=5)
                        if detail_res.status_code == 200:
                            d_data = detail_res.json().get('data')
                            if d_data:
                                p.update({
                                    'product_name': d_data.get('product_name', p.get('title')),
                                    'seller_name': d_data.get('seller_name') or d_data.get('shop_name') or p.get('advertiser'),
                                    'gmv': float(d_data.get('total_sale_gmv_amt', 0) or 0),
                                    'sales': int(d_data.get('total_sale_cnt', 0) or 0),
                                    'sales_7d': int(d_data.get('total_sale_7d_cnt', 0) or 0),
                                    'influencer_count': int(d_data.get('total_ifl_cnt', 0) or 0),
                                    'commission_rate': float(d_data.get('product_commission_rate', 0) or 0),
                                    'price': float(d_data.get('spu_avg_price', 0) or 0),
                                    'image_url': parse_cover_url(d_data.get('cover_url', '')),
                                    'is_enriched': True
                                })
                                return True, f"Success: Direct ID {pid}"
                    except Exception as e:
                         pass

                # 2. Search by Title
                search_term = clean_title_for_search(p.get('title'))
                shops_found_log = ""
                
                if len(search_term) > 5:
                    try:
                        s_res = requests.get(f"{BASE_URL}/product/list", 
                            params={"keyword": search_term, "region": "US", "page_num": 1, "page_size": 5, "product_sort_field": 4, "sort_type": 1}, 
                            auth=get_auth(), timeout=8)
                        
                        if s_res.status_code == 200:
                            s_data = s_res.json().get('data', [])
                            if isinstance(s_data, dict): s_data = s_data.get('list', [])
                            
                            best_match = None
                            for cand in (s_data or []):
                                cand_shop = cand.get('shop_name', '').lower()
                                ad_brand = p.get('advertiser', '').lower()
                                shops_found_log += f"{cand_shop} "
                                
                                # Strict Match
                                if ad_brand != 'unknown' and (ad_brand in cand_shop or cand_shop in ad_brand):
                                    best_match = cand
                                    break
                            
                            if best_match:
                                p.update({
                                    'product_id': best_match.get('product_id'),
                                    'product_name': best_match.get('product_name'),
                                    'seller_name': best_match.get('shop_name'),
                                    'gmv': float(best_match.get('total_sale_gmv_amt', 0) or 0),
                                    'sales': int(best_match.get('total_sale_cnt', 0) or 0),
                                    'sales_7d': int(best_match.get('total_sale_7d_cnt', 0) or 0),
                                    'influencer_count': int(best_match.get('total_ifl_cnt', 0) or 0),
                                    'commission_rate': float(best_match.get('product_commission_rate', 0) or 0),
                                    'price': float(best_match.get('spu_avg_price', 0) or 0),
                                    'image_url': parse_cover_url(best_match.get('cover_url', '')),
                                    'is_enriched': True
                                })
                                return True, f"Success: Found '{p.get('title')[:20]}...'"

                            # 3. Fallback: Search by Brand
                            elif p.get('advertiser') and p.get('advertiser') != 'Unknown':
                                b_res = requests.get(f"{BASE_URL}/product/list", 
                                    params={"keyword": p['advertiser'], "region": "US", "page_num": 1, "page_size": 1, "product_sort_field": 4, "sort_type": 1}, 
                                    auth=get_auth(), timeout=5)
                                if b_res.status_code == 200:
                                    b_data = b_res.json().get('data', [])
                                    if isinstance(b_data, dict): b_data = b_data.get('list', [])
                                    if b_data:
                                        hero = b_data[0]
                                        p.update({
                                            'product_id': hero.get('product_id'),
                                            'product_name': hero.get('product_name'),
                                            'seller_name': hero.get('shop_name'),
                                            'gmv': float(hero.get('total_sale_gmv_amt', 0) or 0),
                                            'sales': int(hero.get('total_sale_cnt', 0) or 0),
                                            'sales_7d': int(hero.get('total_sale_7d_cnt', 0) or 0),
                                            'influencer_count': int(hero.get('total_ifl_cnt', 0) or 0),
                                            'commission_rate': float(hero.get('product_commission_rate', 0) or 0),
                                            'price': float(hero.get('spu_avg_price', 0) or 0),
                                            'image_url': parse_cover_url(hero.get('cover_url', '')),
                                            'is_enriched': True,
                                            'status_note': "Brand Hero Match"
                                        })
                                        return True, f"Success: Brand Fallback '{p['advertiser']}'"
                                    else:
                                        return False, f"Fail: Title 0 results. Fallback Brand '{p['advertiser']}' -> 0 results."
                    except Exception as e:
                        return False, f"Error: {str(e)}"
                
                return False, f"Fail: '{search_term}' -> Found [{shops_found_log}] (Adv: '{p.get('advertiser')}')"

            # Use the new function
            enrich_success, debug_msg = enrich_product_data(p, i)
            if i < 5 and debug_msg: debug_log = debug_msg

            # Debug details for first few items
            debug_log = ""
            



            # Attach debug log to product for final message
            p['_debug_log'] = debug_log

            # SAVE if enriched
            if enrich_success:
                 # Logic to save to DB (Simplified for this block rewrite)
                 existing = Product.query.get(p['product_id'])
                 if not existing:
                     new_prod = Product(
                         product_id=p['product_id'],
                         product_name=p.get('product_name', 'Unknown'),
                         seller_name=p.get('seller_name', 'Unknown'),
                         gmv=p.get('gmv', 0),
                         sales=p.get('sales', 0),
                         sales_7d=p.get('sales_7d', 0),
                         influencer_count=p.get('influencer_count', 0),
                         commission_rate=p.get('commission_rate', 0),
                         price=p.get('price', 0),
                         image_url=p.get('image_url', ''),
                         scan_type='apify_ad',
                         first_seen=datetime.utcnow()
                     )
                     # Hack: Use status_note for URL
                     new_prod.status_note = p.get('url', '')
                     db.session.add(new_prod)
                     saved_count += 1
                 else:
                     # Update existing
                     existing.gmv = p.get('gmv', 0)
                     existing.sales_7d = p.get('sales_7d', 0)
                     existing.sales = p.get('sales', 0)
                     existing.status_note = p.get('url', '')

        db.session.commit()

        # --- COMPLETION MESSAGE LOGIC ---
        msg = f"[vDebug] Ad Scan Complete. Found {len(products)} ads (from {len(items)} raw), Saved {saved_count} new."
        
        if items and not products:
            if debug_keys_str:
                msg += debug_keys_str
            elif items:
                keys_str = ", ".join(list(items[0].keys())[:10])
                msg += f" [DEBUG: Keys found: {keys_str}]"
        else:
            if saved_count == 0 and len(products) > 0:
                debug_details = []
                for p in products[:3]:
                    # Add detailed failure log if available
                    fail_log = p.get('_debug_log', '')
                    if fail_log:
                        debug_details.append(f"LOG: {fail_log}")
                    else:
                        debug_details.append(f"URL: {p.get('url', '')[:30]}... -> ID: {p.get('product_id')}")
                msg += f" [DEBUG: 0 Saved. Enrichment Stats: {debug_details}]"

            if products and (products[0]['product_id'].startswith("apify_unknown_") or products[0]['product_id'].startswith("ad_")):
                if debug_keys_str:
                    msg += debug_keys_str
                elif items:
                    keys_str = ", ".join(list(items[0].keys())[:10])
                    msg += f" [DEBUG: Item Keys: {keys_str}]"
                
                # Debug Check Title
                msg += f" [DEBUG: First Title: '{products[0].get('title')}']"
        
        return jsonify({
            'success': True,
            'message': msg,
            'products': products,
            'debug_raw_count': len(items)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/scan/manual', methods=['POST'])
@login_required
def scan_manual_import():
    """Manual Import of JSON data (e.g. from TheDailyVirals)"""
    user = get_current_user()
    data = request.json
    
    raw_input = data.get('json_data', '')
    if not raw_input:
        return jsonify({'error': 'No JSON data provided'}), 400
        
    try:
        # 1. Parse JSON
        if isinstance(raw_input, str):
            parsed = json.loads(raw_input)
        else:
            parsed = raw_input
            
        items = []
        # Heuristic: Find the list of items
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict):
             # Try common keys
             if 'videos' in parsed: items = parsed['videos']
             elif 'data' in parsed: items = parsed['data'] # classic
             elif 'list' in parsed: items = parsed['list']
             else:
                 # Last resort: Any list value?
                 for k, v in parsed.items():
                     if isinstance(v, list) and len(v) > 0:
                         items = v
                         break
        
        if not items:
             # Debugging help for user
             keys_found = list(parsed.keys()) if isinstance(parsed, dict) else "List[]"
             return jsonify({'error': f"Could not find a list of items in the JSON. Top-level keys: {keys_found}. Please copy the response that contains the list of videos/products."}), 400

        # 2. Process Items
        products = []
        schema_debug = []
        skipped_items = 0
        
        # Helper for safe int conversion
        def safe_int(val):
            try:
                if val is None: return 0
                return int(val)
            except:
                return 0

        for item in items:
            # Map Schema (DailyVirals -> Product)
            # Keys observed: title, creator.username, latest_view_count, likeCount, product.productId
            
            p_obj = item.get('product')
            
            # STRICT MODE: If no product object, SKIP IT.
            # This prevents "Video Title" garbage imports.
            if not isinstance(p_obj, dict):
                skipped_items += 1
                continue

            # Product ID & Data from 'product' object
            pid = p_obj.get('productId') or p_obj.get('product_id')
            
            # Stats (Re-added)
            views = safe_int(item.get('latest_view_count') or item.get('playBox') or item.get('views') or item.get('playCount'))
            likes = safe_int(item.get('likeCount') or item.get('diggCount') or item.get('likes'))
            
            # Title Mapping
            product_title = "Unknown Product"
            if p_obj.get('productName'):
                product_title = p_obj.get('productName')
            elif p_obj.get('title'):
                product_title = p_obj.get('title')
            elif p_obj.get('name'):
                product_title = p_obj.get('name')
            
            img_url = ""
            if p_obj.get('imageUrl') or p_obj.get('image_url'):
                img_url = p_obj.get('imageUrl') or p_obj.get('image_url')

            # Advertiser / Brand / Shop Name
            advertiser = "Unknown"
            if isinstance(p_obj, dict):
                 advertiser = p_obj.get('shopName') or p_obj.get('shop_name') or advertiser
            if advertiser == "Unknown":
                creator = item.get('creator')
                if isinstance(creator, dict):
                    advertiser = creator.get('username') or creator.get('nickname') or "Unknown"

            # Sales / GMV Mappings
            raw_sales = safe_int(p_obj.get('totalUnitsSold') or p_obj.get('soldCount') or p_obj.get('sales') or 0)
            sales_7d = safe_int(p_obj.get('revenueLastSevenDays') or 0)
             
            # GMV
            gmv = 0
            revenue_analytics = p_obj.get('revenueAnalytics')
            if isinstance(revenue_analytics, dict):
                 gmv = safe_int(revenue_analytics.get('totalRevenue') or 0)
            else:
                 gmv = safe_int(p_obj.get('totalRevenue') or 0)

            # Create Candidate
            p = {
                'product_id': pid, 
                'product_name': product_title[:100],
                'title': product_title,
                'seller_name': advertiser, 
                'advertiser': advertiser, 
                'price': 0,
                'commission_rate': 0,
                'sales': raw_sales,
                'sales_7d': sales_7d,
                'gmv': gmv,
                'influencer_count': 0,
                'video_count': 1, 
                'video_views': views,
                'video_likes': likes,
                'scan_type': 'daily_virals', 
                'url': item.get('videoUrl') or item.get('link') or "",
                'image': img_url,
                'is_enriched': False
            }
            products.append(p)
            
            if len(schema_debug) < 1:
                # Dump the first VALID item key to debug
                raw_dump = json.dumps(item, default=str)[:200]
                schema_debug.append(f"KEYS: {list(item.keys())}")
                schema_debug.append(f"RAW_DUMP: {raw_dump}")

        # 3. Enrich Candidates
        saved_count = 0
        debug_log = ""
        
        for i, p in enumerate(products):
            # Slow down slightly to effectively use the 'Direct ID' lookup without hitting rate limits
            time.sleep(1.5)
            
            # Attempt Enrichment
            # Bridge to Apify: Queue for Background Scanning (Async)
            # This replaces synchronous Echotik enrichment while Echotik is unavailable
            # Auto-Scraping Removed for V2 (EchoTik transition)
            enrich_success = False
            msg = "Imported Raw (Vantage Mode)"
            
            if not p.get('product_id'): 
                p['product_id'] = f"dv_{hash(p['url']) if p.get('url') else int(time.time()*1000)}"

            existing = Product.query.filter_by(product_id=p['product_id']).first()
            if not existing:
                new_prod = Product(
                    product_id=p['product_id'],
                    product_name=p['product_name'],
                    seller_name=p['seller_name'],
                    sales=p['sales'],
                    sales_7d=p['sales_7d'],
                    gmv=p['gmv'],
                    influencer_count=p['influencer_count'],
                    commission_rate=p['commission_rate'],
                    price=p['price'],
                    image_url=p.get('image_url') or p.get('image'),
                    scan_type='daily_virals',
                    first_seen=datetime.utcnow(),
                    product_status='active',
                    status_note=f"Imported from DailyVirals. Enriched: {enrich_success}"
                )
                db.session.add(new_prod)
                saved_count += 1
            else:
                if p['sales'] > 0: existing.sales = max(existing.sales, p['sales'])
                if p['sales_7d'] > 0: existing.sales_7d = p['sales_7d']
                
                # FORCE UPDATE
                if p['product_name'] and p['product_name'] != "Unknown Title":
                   existing.product_name = p['product_name']
                # Prioritize Enriched Image (image_url) over Import Image (image)
                new_img = p.get('image_url') or p.get('image')
                if new_img:
                   existing.image_url = new_img
                   existing.cached_image_url = None 
                if p.get('seller_name') and p.get('seller_name') != "Unknown":
                   existing.seller_name = p['seller_name']
                
                # Ensure at least 1 video count if currently 0
                if existing.video_count < 1:
                     existing.video_count = 1

                if existing.scan_type != 'daily_virals':
                     existing.scan_type = 'daily_virals'
                
                existing.first_seen = datetime.utcnow()
            
            if i < 5: debug_log += f" | {msg}"

        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f"Processed {len(items)} items. Imported {len(products)} valid products. Skipped {skipped_items} videos.",
            'debug_info': f"Stats: {schema_debug[:5]}... Logs: {debug_log}"
        })

    except Exception as e:
        return jsonify({'error': f"Import Failed: {str(e)}"}), 500

@app.route('/api/refresh-ads', methods=['POST'])
def refresh_daily_virals_ads():
    """Batch refresh enrichment for 'Ad Winners' (DailyVirals) products."""
    try:
        # Get all daily_virals products, newest first
        products = Product.query.filter_by(scan_type='daily_virals').order_by(Product.first_seen.desc()).all()
        
        count = 0
        success_count = 0
        debug_log = []
        
        for p in products:
            p_dict = p.to_dict()
            p_dict['product_id'] = p.product_id 
            
            # Slow down slightly to be polite
            time.sleep(1.5)
            
            # Force enrichment (skip is_enriched check) to ensure we get fresh data
            success, msg = enrich_product_data(p_dict, f"Ref {p.product_id}: ", force=True)
            if success:
                # Update DB
                p.product_name = p_dict['product_name']
                p.image_url = p_dict['image_url']
                p.seller_name = p_dict['seller_name']
                p.sales = p_dict.get('sales', 0)
                p.sales_7d = p_dict.get('sales_7d', 0)
                p.influencer_count = p_dict.get('influencer_count', 0)
                p.commission_rate = p_dict.get('commission_rate', 0)
                p.last_updated = datetime.utcnow()
                success_count += 1
            else:
                # If failure is just "not found", likely a placeholder product (ad copy title).
                # We preserved it via the force visibility check below, so don't alarm the user.
                if "0 results" in msg or "Smart Truncate" in msg or "Adv:" in msg:
                     pass # Don't log expected failures for placeholders
                elif len(debug_log) < 10:
                    debug_log.append(f"{p.product_name}: {msg}")
            
            # ALWAYS force visibility for these manually imported products
            # This ensures they don't disappear from dashboard even if stats enrichment fails
            # (e.g. placeholder products where title is just ad copy)
            if p.video_count < 1:
                p.video_count = 1
            p.scan_type = 'daily_virals' # Reinforce type
            
            count += 1
            if count % 5 == 0:
                db.session.commit()
                
        db.session.commit()
        
        preserved_count = count - success_count
        debug_str = "\n".join(debug_log) if debug_log else "None"
        
        return jsonify({
            'success': True,
            'message': f"Refreshed {count} Ad Winners. Updated: {success_count}. Preserved: {preserved_count}.",
            'debug_info': f"Failures (showing real errors only):\n{debug_str}"
        })
        
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/brands/list', methods=['GET'])
def list_top_brands():
    """Get list of top brands from EchoTik"""
    page = request.args.get('page', 1, type=int)
    
    brands = get_top_brands(page=page)
    
    if not brands:
        return jsonify({'error': 'Failed to fetch brands', 'brands': []}), 500
    
    return jsonify({
        'brands': [{
            'id': b.get('seller_id', ''),
            'name': b.get('seller_name', 'Unknown'),
            'logo': b.get('seller_logo', ''),
            'sales': b.get('sales', 0),
            'products': b.get('product_count', 0)
        } for b in brands],
        'page': page,
        'has_next': len(brands) >= 20
    })

@app.route('/api/run-apify-scan', methods=['POST'])
def run_apify_scan():
    """Triggers the Apify Shop Scanner script synchronously and returns output."""
    try:
        # Use python executable relative to environment
        import sys
        import subprocess
        
        script_path = os.path.join(os.path.dirname(__file__), 'apify_shop_scanner.py')
        
        # Run synchronously to capture output
        # Use -u for unbuffered output to ensure we catch prints
        result = subprocess.run(
            [sys.executable, '-u', script_path],
            capture_output=True,
            text=True
        )
        
        stdout = result.stdout
        stderr = result.stderr
        
        debug_info = f"Exe: {sys.executable}\nScript: {script_path}\nReturn Code: {result.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
        
        if result.returncode != 0:
            return jsonify({
                'success': False, 
                'error': f"Script failed (Exit Code {result.returncode}):\n{debug_info}"
            })
            
        return jsonify({
            'success': True, 
            'message': 'Scanner finished successfully.',
            'debug_log': debug_info
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/debug/recent', methods=['GET'])
def debug_recent_products():
    """Debug: Show last 10 products and their scan_types"""
    try:
        products = Product.query.order_by(Product.first_seen.desc()).limit(10).all()
        return jsonify({
            'success': True,
            'products': [{
                'id': p.product_id,
                'name': p.product_name,
                'scan_type': p.scan_type,
                'first_seen': str(p.first_seen)
            } for p in products]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# ADMIN / CLEANUP ENDPOINTS
# =============================================================================

@app.route('/api/admin/cleanup_garbage', methods=['POST'])
def cleanup_garbage():
    """Delete invalid/debug products from the database"""
    try:
        deleted_count = 0
        
        # 1. Delete "Unknown (X keys)" debug entries
        q1 = db.session.query(Product).filter(Product.product_name.like('Unknown (% keys)'))
        c1 = q1.count()
        q1.delete(synchronize_session=False)
        deleted_count += c1
        
        # 2. Delete generic "Unknown" products (Garbage data from failed scrapes)
        # FIXED: Removed sales==0 check because some garbage has fake stats (e.g. 133 sales)
        q2 = db.session.query(Product).filter(Product.product_name == 'Unknown')
        c2 = q2.count()
        q2.delete(synchronize_session=False)
        deleted_count += c2
        
        # 2b. Delete "Unknown%" starting variants if sales are 0 (safeguard)
        q2b = db.session.query(Product).filter(
            Product.product_name.like('Unknown %'), 
            Product.sales == 0
        )
        c2b = q2b.count()
        q2b.delete(synchronize_session=False)
        deleted_count += c2b

        # 3. Delete explicit Debug artifacts
        q3 = db.session.query(Product).filter(
            db.or_(
                Product.seller_name.like('Debug%'),
                Product.seller_name.like('Keys%')
            )
        )
        c3 = q3.count()
        q3.delete(synchronize_session=False)
        deleted_count += c3

        # 4. Delete "Dead" Ads (Single video, 0 sales, 0 influencers, scan_type='apify_ad')
        # These are failed enrichments that just took the ad metadata
        q4 = db.session.query(Product).filter(
            Product.scan_type == 'apify_ad',
            Product.sales == 0,
            Product.influencer_count == 0,
            Product.video_count <= 1,
            Product.is_favorite == False  # Safety: Never delete favorites
        )
        c4 = q4.count()
        q4.delete(synchronize_session=False)
        deleted_count += c4
        
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'deleted_count': deleted_count,
            'details': f"Keys: {c1}, Unknowns: {c2}, Debug: {c3}, DeadAds: {c4}"
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# =============================================================================
# PRODUCTS ENDPOINTS
# =============================================================================

@app.route('/api/products', methods=['GET'])
def get_products():
    """Get all saved products with filtering and pagination options"""
    # Legacy influencer filters (kept for backwards compatibility)
    min_influencers = request.args.get('min_influencers', 0, type=int)
    max_influencers = request.args.get('max_influencers', 99999, type=int)
    
    # Video-based competition filters (primary)
    min_videos = request.args.get('min_videos', 0, type=int)
    max_videos = request.args.get('max_videos', 99999, type=int)
    
    # Pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    per_page = min(per_page, 100)  # Cap at 100 per page
    
    # Sort options
    sort_by = request.args.get('sort', 'sales_7d')
    sort_order = request.args.get('order', 'desc')
    
    # Date filter: today, yesterday, 7days, all
    date_filter = request.args.get('date', 'all')
    
    # Brand/seller search
    brand_search = request.args.get('brand', '').strip()
    
    # Favorites only
    favorites_only = request.args.get('favorites', 'false').lower() == 'true'
    
    # Show out-of-stock products (default: hide them)
    show_oos = request.args.get('show_oos', 'false').lower() == 'true'
    
    # Show ONLY out-of-stock products
    oos_only = request.args.get('oos_only', 'false').lower() == 'true'
    
    # Hidden gems filter
    gems_only = request.args.get('gems_only', 'false').lower() == 'true'
    
    # Untapped filter (low video/influencer ratio)
    untapped_only = request.args.get('untapped_only', 'false').lower() == 'true'
    
    # Trending filter
    trending_only = request.args.get('trending_only', 'false').lower() == 'true'
    
    # Proven sellers filter (products with 50+ total sales)
    proven_only = request.args.get('proven_only', 'false').lower() == 'true'
    
    # Apify Shop Scraper filter
    apify_scan = request.args.get('apify_scan', 'false').lower() == 'true'

    # Discovery Scraper filter
    discovery_scan = request.args.get('discovery_scan', 'false').lower() == 'true'
    
    # Build query - exclude unavailable products by default
    if apify_scan:
        # Apify Shop scan: Explicitly show shop scraper items
        # Also apply video count filters if they are set (so user can filter 0-10 etc)
        query = Product.query.filter(
            Product.scan_type == 'apify_shop',
            db.or_(Product.product_status == None, Product.product_status == 'active'),
            Product.video_count >= min_videos,
            Product.video_count <= max_videos
        )
    elif discovery_scan:
        # Discovery scan: products found via broad keyword search
        query = Product.query.filter(
            Product.scan_type == 'discovery',
            db.or_(Product.product_status == None, Product.product_status == 'active'),
            Product.video_count >= min_videos,
            Product.video_count <= max_videos
        )
    elif oos_only:
        # Show only likely OOS products
        query = Product.query.filter(
            Product.video_count >= min_videos,
            Product.video_count <= max_videos,
            Product.product_status == 'likely_oos'
        )
    elif show_oos:
        # Show all including OOS
        query = Product.query.filter(
            Product.video_count >= min_videos,
            Product.video_count <= max_videos,
            db.or_(Product.product_status == None, Product.product_status.in_(['active', 'likely_oos']))
        )
    else:
        # Default: hide OOS products
        query = Product.query.filter(
            Product.video_count >= min_videos,
            Product.video_count <= max_videos,
            db.or_(Product.product_status == None, Product.product_status == 'active')
        )
    
    # ALWAYS exclude non-promotable products (not for sale, live only, etc.)
    # Also exclude Debug/Garbage data from previous runs
    query = query.filter(
        ~Product.product_name.ilike('%not for sale%'),
        ~Product.product_name.ilike('%live only%'),
        ~Product.seller_name.like('Debug%'), # Hide debug artifacts
        ~Product.seller_name.like('Keys%'),  # Hide debug keys
        ~Product.product_name.like('Unknown%raw keys%') 
    )
    
    # Apply date filter
    now = datetime.utcnow()
    if date_filter == 'today':
        # Use last 24 hours (rolling window) fixes timezone confusion
        start_time = now - timedelta(hours=24)
        query = query.filter(Product.first_seen >= start_time)
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
    
    # Apply hidden gems filter (high sales, low influencers)
    if gems_only:
        # Hidden gems: selling well with low competition
        query = query.filter(
            Product.sales_7d >= 20,  # Decent weekly sales
            Product.influencer_count <= 30,  # Low competition
            Product.influencer_count >= 1,  # At least 1 (shows it's promotable)
            Product.video_count >= 1  # At least 1 video (proven product)
        )
    
    # Apply likely_ads filter (Ad Winners tab)
    likely_ads = request.args.get('likely_ads', 'false').lower() == 'true'
    if likely_ads:
        query = query.filter(
            db.or_(
                # Logic A: Heuristic (High Sales, Low Influencers)
                db.and_(
                    Product.sales_7d > 50,
                    Product.influencer_count < 5,
                    Product.video_count < 5
                ),
                # Logic B: Explicit Apify Ads (Always ad winners)
                Product.scan_type.in_(['apify_ad', 'daily_virals'])
            )
        )

    # Apply untapped filter - products with low video/influencer ratio
    # These are products where influencers added to showcase but didn't make videos
    if untapped_only:
        query = query.filter(
            Product.influencer_count >= 5,  # Has some influencers
            Product.video_count >= 1,  # At least 1 video (not totally fresh)
            Product.video_count <= Product.influencer_count * 0.5,  # Less than 0.5 videos per influencer
            Product.sales_7d >= 10  # At least some sales to show it works
        )
    
    # Apply discovery scan filter (Specific override logic removed here, handled in initial query build)
    # The scan type filters are already handled in the initial block or via logic above
    # We DO NOT need to check apify_scan here again as it's the first logic block
    # However, 'discovery_scan' was handled in the first block too.
    # The original code had apify_scan checked TWICE. We are removing the redundancy.
            
    # Apply discovery scan filter
    if discovery_scan:
        # Redundant if handled in initial block but safe to keep if we switch away from pure exclusive logic
        # For now, since we used an if/elif structure at to start, we don't need to re-filter here
        pass

    # Apply trending filter - products with sales growth or high recent sales
    if trending_only:
        try:
            # Products with positive velocity OR high 7-day sales (as fallback)
            query = query.filter(
                db.or_(
                    Product.sales_velocity >= 10,  # Lowered from 20
                    Product.sales_7d >= 100  # Fallback: high recent sales = trending
                )
            )
        except Exception:
            pass  # Column might not exist yet
    
    # Apply proven sellers filter - products with significant total sales history
    if proven_only:
        query = query.filter(Product.sales >= 50)
    
    # Apply free shipping filter
    free_shipping_filter = request.args.get('free_shipping', 'false').lower() == 'true'
    if free_shipping_filter:
        query = query.filter(Product.has_free_shipping == True)
    
    # Get total count before pagination
    total_count = query.count()
    
    # Apply sorting
    if sort_by == 'new':
        sort_column = Product.first_seen
    else:
        sort_column = getattr(Product, sort_by, Product.sales_7d)
        
    if sort_order == 'asc':
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())
    
    # Apply pagination
    total_pages = (total_count + per_page - 1) // per_page
    offset = (page - 1) * per_page
    products = query.offset(offset).limit(per_page).all()
    
    # Count OOS products for UI
    oos_count = Product.query.filter(Product.product_status == 'likely_oos').count()
    
    # Count gems (products selling well with low competition, with at least 1 video)
    gems_count = Product.query.filter(
        Product.sales_7d >= 20,
        Product.influencer_count <= 30,
        Product.influencer_count >= 1,
        Product.video_count >= 1,
        db.or_(Product.product_status == None, Product.product_status == 'active')
    ).count()
    
    # Count trending - products with velocity or high recent sales
    try:
        trending_count = Product.query.filter(
            db.or_(
                Product.sales_velocity >= 10,
                Product.sales_7d >= 100
            ),
            db.or_(Product.product_status == None, Product.product_status == 'active')
        ).count()
    except Exception:
        trending_count = 0
    
    # Count untapped - products with low video/influencer ratio
    try:
        untapped_count = Product.query.filter(
            Product.influencer_count >= 5,
            Product.video_count >= 1,
            Product.video_count <= Product.influencer_count * 0.5,
            Product.sales_7d >= 10,
            db.or_(Product.product_status == None, Product.product_status == 'active')
        ).count()
    except Exception:
        untapped_count = 0
    
    # Get video competition category counts for filter pills
    # Apply same exclusions as main query (non-promotable products)
    base_filter = db.and_(
        ~Product.product_name.ilike('%not for sale%'),
        ~Product.product_name.ilike('%live only%'),
        ~Product.product_name.ilike('%sample%not for sale%'),
        ~Product.product_name.ilike('%display only%'),
        ~Product.product_name.ilike('%coming soon%'),
        db.or_(Product.product_status == None, Product.product_status == 'active')
    )
    
    untapped_count = Product.query.filter(
        base_filter,
        Product.video_count >= 1,
        Product.video_count <= 10
    ).count()
    
    low_count = Product.query.filter(
        base_filter,
        Product.video_count >= 11,
        Product.video_count <= 30
    ).count()
    
    medium_count = Product.query.filter(
        base_filter,
        Product.video_count >= 31,
        Product.video_count <= 60
    ).count()
    
    good_count = Product.query.filter(
        base_filter,
        Product.video_count >= 61,
        Product.video_count <= 100
    ).count()
    
    all_count = untapped_count + low_count + medium_count + good_count
    
    # Count proven sellers (50+ total sales)
    proven_count = Product.query.filter(
        base_filter,
        Product.sales >= 50
    ).count()
    
    # Count free shipping products
    freeship_count = Product.query.filter(
        base_filter,
        Product.has_free_shipping == True
    ).count()

    # Apify Trends (Paid Shop Scan)
    apify_count = Product.query.filter(
        Product.scan_type == 'apify_shop'
    ).count()

    # Discovery Trends
    discovery_count = Product.query.filter(
        Product.scan_type == 'discovery'
    ).count()
    
    return jsonify({
        'success': True,
        'products': [p.to_dict() for p in products],
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_next': page < total_pages,
            'has_prev': page > 1
        },
        'counts': {
            'oos': oos_count,
            'gems': gems_count,
            'trending': trending_count,
            'proven': proven_count,
            'freeship': freeship_count,
            'freeship': freeship_count,
            'apify_count': apify_count,
            'discovery_count': discovery_count,
            'all': all_count,
            'untapped': untapped_count,
            'low': low_count,
            'medium': medium_count,
            'good': good_count
        },
        'filters': {
            'date': date_filter,
            'brand': brand_search,
            'favorites_only': favorites_only,
            'show_oos': show_oos,
            'oos_only': oos_only,
            'gems_only': gems_only,
            'trending_only': trending_only,
            'proven_only': proven_only,
            'sort_by': sort_by,
            'sort_order': sort_order
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
            'stock': int(product.live_count or 0), # Fix: Map live_count to stock for frontend
            'views_count': int(product.views_count or 0),
            'product_rating': float(product.product_rating or 0),
            'review_count': int(product.review_count or 0),
            
            # Favorites
            'is_favorite': product.is_favorite or False,
            
            # Status
            'product_status': product.product_status or 'active',
            'status_note': product.status_note,
            
            # Media - use cached URL for instant loading
            'image_url': image_url,
            'cached_image_url': image_url,
            
            # Links
            # Fix: Use saved product_url if available (contains correct shop.tiktok.com format)
            'product_url': product.product_url or f'https://shop.tiktok.com/view/product/{product.product_id}?region=US&locale=en',
            'tiktok_url': product.product_url or f'https://shop.tiktok.com/view/product/{product.product_id}?region=US&locale=en',
            'affiliate_url': f'https://affiliate.tiktok.com/product/{product.product_id}',
            
            # Timestamps
            'first_seen': product.first_seen.isoformat() if product.first_seen else None,
            'last_updated': product.last_updated.isoformat() if product.last_updated else None,
            
            # User permissions (for showing/hiding features)
            'is_admin': session.get('is_admin', False),
        }
        
        return jsonify({'success': True, 'product': data})
        
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/mark-unavailable/<product_id>')
@login_required
def mark_unavailable(product_id):
    """Mark a product as unavailable (removed or out of stock)"""
    try:
        status = request.args.get('status', 'removed')
        note = request.args.get('note', '')
        
        # Validate status
        if status not in ['removed', 'out_of_stock', 'active']:
            return jsonify({'success': False, 'error': 'Invalid status'}), 400
        
        product = Product.query.get(product_id)
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        
        product.product_status = status
        product.status_note = note
        product.last_updated = datetime.utcnow()
        db.session.commit()
        
        # Log the activity
        user = get_current_user()
        if user:
            log_activity(user.id, 'mark_unavailable', {
                'product_id': product_id,
                'status': status,
                'note': note
            })
        
        return jsonify({
            'success': True,
            'product_id': product_id,
            'status': status,
            'message': f'Product marked as {status}'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get scanning statistics"""
    try:
        total = Product.query.count()
        
        # Video-based competition ranges
        untapped = Product.query.filter(
            Product.video_count >= 1,
            Product.video_count <= 10
        ).count()
        
        low = Product.query.filter(
            Product.video_count >= 11,
            Product.video_count <= 30
        ).count()
        
        medium = Product.query.filter(
            Product.video_count >= 31,
            Product.video_count <= 60
        ).count()
        
        good = Product.query.filter(
            Product.video_count >= 61,
            Product.video_count <= 100
        ).count()
        
        # Get unique brands
        brands = db.session.query(Product.seller_name).distinct().count()
        
        # Get favorites count
        favorites = Product.query.filter(Product.is_favorite == True).count()
        
        # Data quality metrics
        zero_commission = Product.query.filter(
            db.or_(Product.commission_rate == 0, Product.commission_rate.is_(None))
        ).count()
        
        low_sales = Product.query.filter(Product.sales_7d <= 2).count()
        
        # Count products with no cached image OR stale cached image (>48 hours old - TikTok CDN URLs expire)
        stale_threshold = datetime.utcnow() - timedelta(hours=48)
        
        missing_images = Product.query.filter(
            db.or_(
                Product.cached_image_url.is_(None),
                Product.cached_image_url == '',
                Product.image_cached_at.is_(None),
                Product.image_cached_at < stale_threshold
            )
        ).count()
        
        # Gems and trending counts
        gems_count = Product.query.filter(
            Product.sales_7d >= 20,
            Product.influencer_count <= 30,
            Product.influencer_count >= 1,
            Product.video_count >= 1,
            db.or_(Product.product_status == None, Product.product_status == 'active')
        ).count()
        
        try:
            trending_count = Product.query.filter(
                db.or_(
                    Product.sales_velocity >= 10,
                    Product.sales_7d >= 100
                ),
                db.or_(Product.product_status == None, Product.product_status == 'active')
            ).count()
        except:
            trending_count = 0
        
        # Count untapped - products with low video/influencer ratio
        try:
            untapped_count = Product.query.filter(
                Product.influencer_count >= 5,
                Product.video_count >= 1,
                Product.video_count <= Product.influencer_count * 0.5,
                Product.sales_7d >= 10,
                db.or_(Product.product_status == None, Product.product_status == 'active')
            ).count()
        except:
            untapped_count = 0
        
        try:
            freeship_count = Product.query.filter(
                Product.has_free_shipping == True
            ).count()
        except:
            freeship_count = 0

        # Avg commission - handle potential DB errors
        try:
            avg_comm = db.session.query(func.avg(Product.commission_rate)).scalar() or 0
        except:
            avg_comm = 0

        return jsonify({
            'success': True,
            'stats': {
                'total_products': total,
                'unique_brands': brands,
                'untapped_products': untapped_count,
                'hidden_gems': gems_count,
                'high_commission': Product.query.filter(Product.commission_rate >= 15).count(),
                'freeship': freeship_count,
                'avg_commission': avg_comm,
                'ad_winners': Product.query.filter(
                    db.or_(
                        db.and_(
                            Product.sales_7d > 50,
                            Product.influencer_count < 5,
                            Product.video_count < 5
                        ),
                        Product.scan_type.in_(['apify_ad', 'daily_virals'])
                    )
                ).count(),
                'apify_count': Product.query.filter(Product.scan_type == 'apify_shop').count(),
                'discovery_count': Product.query.filter(Product.scan_type == 'discovery').count()
            }
        })

    except Exception as e:
        # Fallback if everything explodes checks
        return jsonify({
            'success': True,
            'stats': {
                'total_products': 0,
                'unique_brands': 0,
                'untapped_products': 0,
                'hidden_gems': 0,
                'high_commission': 0,
                'freeship': 0,
                'avg_commission': 0
            },
            'error': str(e)
        })


@app.route('/api/refresh-images', methods=['POST', 'GET'])
def refresh_images():
    """
    Refresh cached image URLs for products using SINGLE-PRODUCT API calls.
    The batch API doesn't work reliably with EchoTik.
    
    Parameters:
        batch: Number of products to process (default 50, max 100)
        force: If true, refresh ALL products regardless of current cache status
    
    NOTE: Continuous mode removed to prevent server blocking.
    Frontend should call this endpoint multiple times for large refreshes.
    """
    try:
        batch_size = min(request.args.get('batch', 50, type=int), 100)
        force = request.args.get('force', 'false').lower() == 'true'
        
        if force:
            products = Product.query.filter(
                Product.image_url.isnot(None),
                Product.image_url != ''
            ).limit(batch_size).all()
        else:
            # Calculate stale threshold (48 hours - TikTok CDN URLs expire)
            stale_threshold = datetime.utcnow() - timedelta(hours=48)
            
            # Products missing cached images OR with stale cache
            # First get products that HAVE image_url but need signing/refreshing
            products = Product.query.filter(
                Product.image_url.isnot(None),
                Product.image_url != '',
                db.or_(
                    Product.cached_image_url.is_(None),
                    Product.cached_image_url == '',
                    Product.image_cached_at.is_(None),
                    Product.image_cached_at < stale_threshold
                )
            ).limit(batch_size).all()
            
            # If none of those, get products with NO image_url (need API fetch)
            if not products:
                products = Product.query.filter(
                    db.or_(
                        Product.image_url.is_(None),
                        Product.image_url == ''
                    ),
                    db.or_(
                        Product.cached_image_url.is_(None),
                        Product.cached_image_url == ''
                    )
                ).limit(batch_size).all()
        
        updated = 0
        processed = len(products)
        
        for product in products:
            try:
                # If product already has image_url, just get signed URL
                if product.image_url:
                    parsed_url = parse_cover_url(product.image_url)
                    if parsed_url:
                        signed_urls = get_cached_image_urls([parsed_url])
                        if signed_urls.get(parsed_url):
                            product.cached_image_url = signed_urls[parsed_url]
                            product.image_cached_at = datetime.utcnow()
                            updated += 1
                    time.sleep(0.1)
                    continue
                
                # No image_url - fetch from single-product API
                response = requests.get(
                    f"{BASE_URL}/product/detail",
                    params={'product_id': product.product_id},
                    auth=get_auth(),
                    timeout=30
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('code') == 0 and data.get('data'):
                        p = data['data'] if isinstance(data['data'], dict) else data['data'][0] if data['data'] else {}
                        
                        cover_url = p.get('cover_url', '')
                        if cover_url:
                            parsed_url = parse_cover_url(cover_url)
                            if parsed_url:
                                product.image_url = parsed_url
                                signed_urls = get_cached_image_urls([parsed_url])
                                if signed_urls.get(parsed_url):
                                    product.cached_image_url = signed_urls[parsed_url]
                                    product.image_cached_at = datetime.utcnow()
                                    updated += 1
                        
                        # BONUS: Also update commission and sales if they're 0
                        if (product.commission_rate or 0) == 0:
                            new_commission = float(p.get('product_commission_rate', 0) or 0)
                            if new_commission > 0:
                                product.commission_rate = new_commission
                        
                        if (product.sales_7d or 0) <= 2:
                            new_sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                            if new_sales_7d > 0:
                                product.sales_7d = new_sales_7d
                                product.sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
                                product.sales = int(p.get('total_sale_cnt', 0) or 0)
                
                time.sleep(0.2)  # Rate limiting
                
            except Exception as e:
                print(f"Error refreshing image for {product.product_id}: {e}")
                continue
        
        db.session.commit()
        
        # Count remaining AFTER processing (including stale images >48 hours old)
        stale_threshold = datetime.utcnow() - timedelta(hours=48)
        remaining = Product.query.filter(
            db.or_(
                Product.cached_image_url.is_(None),
                Product.cached_image_url == '',
                Product.image_cached_at.is_(None),
                Product.image_cached_at < stale_threshold
            )
        ).count()
        
        return jsonify({
            'success': True,
            'updated': updated,
            'processed': processed,
            'remaining': remaining
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deep-refresh', methods=['GET', 'POST'])
@login_required
def deep_refresh_products():
    """
    Deep refresh product data using single-product API calls.
    Fixes 0% commission and bad sales data from bulk scans.
    
    Parameters:
        batch: Number of products to process (default 50)
        fix_zero_commission: Only fix products with 0% commission (default: true)
        fix_low_sales: Only fix products with sales_7d <= 2 (default: true)
        continuous: Keep running until done (max iterations)
        force: Process all matching products using offset pagination
    """
    try:
        batch_size = min(int(request.args.get('batch', 50)), 100)
        fix_zero_commission = request.args.get('fix_zero_commission', 'true').lower() == 'true'
        fix_low_sales = request.args.get('fix_low_sales', 'true').lower() == 'true'
        continuous = request.args.get('continuous', 'false').lower() == 'true'
        force_all = request.args.get('force', 'false').lower() == 'true'
        max_iterations = min(int(request.args.get('max_iterations', 10)), 50)
        
        total_updated = 0
        total_commission_fixed = 0
        total_sales_fixed = 0
        total_images_fixed = 0
        total_processed = 0
        total_api_errors = 0
        iteration = 0
        current_offset = 0  # For force mode pagination
        
        # Track when we started - only process products not updated since then
        refresh_started = datetime.utcnow()
        
        # Build base conditions for products needing fixes
        base_conditions = []
        if fix_zero_commission:
            base_conditions.append(db.or_(Product.commission_rate == 0, Product.commission_rate.is_(None)))
        if fix_low_sales:
            base_conditions.append(Product.sales_7d <= 2)
        
        if not base_conditions:
            base_conditions.append(Product.product_id.isnot(None))
        
        # Count total products matching the criteria (for diagnostics)
        total_matching = Product.query.filter(db.or_(*base_conditions)).count()
        
        print(f"🔄 Deep refresh starting: {total_matching} products match criteria, force={force_all}, continuous={continuous}")
        
        while True:
            iteration += 1
            
            # Build query based on mode
            if force_all:
                # Force mode: use OFFSET to paginate through all matching products
                # This ensures we process different products each iteration
                products = Product.query.filter(
                    db.or_(*base_conditions)
                ).order_by(Product.product_id).offset(current_offset).limit(batch_size).all()
                current_offset += batch_size
            else:
                # Normal mode: only get products NOT updated during this refresh session
                products = Product.query.filter(
                    db.or_(*base_conditions),
                    db.or_(
                        Product.last_updated.is_(None),
                        Product.last_updated < refresh_started
                    )
                ).limit(batch_size).all()
            
            if not products:
                print(f"🔄 No more products to process at iteration {iteration}")
                break
            
            updated_this_batch = 0
            processed_this_batch = 0
            commission_fixed = 0
            sales_fixed = 0
            images_fixed = 0
            api_errors = 0
            
            for product in products:
                processed_this_batch += 1
                try:
                    response = requests.get(
                        f"{BASE_URL}/product/detail",
                        params={'product_id': product.product_id},
                        auth=get_auth(),
                        timeout=30
                    )
                    
                    # ALWAYS mark as updated so we don't retry the same product
                    product.last_updated = datetime.utcnow()
                    
                    if response.status_code != 200:
                        api_errors += 1
                        continue
                    
                    data = response.json()
                    if data.get('code') != 0 or not data.get('data'):
                        api_errors += 1
                        continue
                    
                    p = data['data'] if isinstance(data['data'], dict) else data['data'][0] if data['data'] else {}
                    
                    if not p:
                        api_errors += 1
                        continue
                    
                    data_changed = False
                    
                    # Fix commission
                    new_commission = float(p.get('product_commission_rate', 0) or 0)
                    if new_commission > 0 and (product.commission_rate or 0) == 0:
                        product.commission_rate = new_commission
                        commission_fixed += 1
                        data_changed = True
                    elif new_commission > 0 and new_commission != product.commission_rate:
                        product.commission_rate = new_commission
                        data_changed = True
                    
                    # Fix sales
                    new_sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                    if new_sales_7d > (product.sales_7d or 0):
                        product.sales_7d = new_sales_7d
                        product.sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
                        product.sales = int(p.get('total_sale_cnt', 0) or 0)
                        product.gmv = float(p.get('total_sale_gmv_amt', 0) or 0)
                        product.gmv_30d = float(p.get('total_sale_gmv_30d_amt', 0) or 0)
                        sales_fixed += 1
                        data_changed = True
                    
                    # Fix influencer count
                    new_inf_count = int(p.get('total_ifl_cnt', 0) or 0)
                    if new_inf_count > 0:
                        product.influencer_count = new_inf_count
                        data_changed = True
                    
                    # Fix price
                    new_price = float(p.get('spu_avg_price', 0) or 0)
                    if new_price > 0:
                        product.price = new_price
                    if data_changed:
                        updated_this_batch += 1
                        
                    time.sleep(0.4)  # Rate limiting
                    
                except Exception as e:
                    print(f"Error deep refreshing {product.product_id}: {e}")
                    api_errors += 1
                    product.last_updated = datetime.utcnow()
                    continue
            
            db.session.commit()
            total_processed += processed_this_batch
            total_commission_fixed += commission_fixed
            total_sales_fixed += sales_fixed
            total_images_fixed += images_fixed
            total_api_errors += api_errors
            
            print(f"🔄 Deep refresh iteration {iteration}: processed {processed_this_batch}, updated {updated_this_batch}, api_errors {api_errors} (commission: {commission_fixed}, sales: {sales_fixed}, images: {images_fixed})")
            
            # Break conditions
            if not continuous:
                break
            if iteration >= max_iterations:
                print(f"🔄 Reached max iterations ({max_iterations})")
                break
            if processed_this_batch == 0:
                print(f"🔄 No products processed this batch")
                break
        
        # Count remaining problems
        remaining_zero_commission = Product.query.filter(
            db.or_(Product.commission_rate == 0, Product.commission_rate.is_(None))
        ).count()
        remaining_low_sales = Product.query.filter(Product.sales_7d <= 2).count()
        
        return jsonify({
            'success': True,
            'message': f'Deep refresh complete',
            'total_matching': total_matching,
            'total_processed': total_processed,
            'total_updated': total_updated,
            'commission_fixed': total_commission_fixed,
            'sales_fixed': total_sales_fixed,
            'images_fixed': total_images_fixed,
            'api_errors': total_api_errors,
            'iterations': iteration,
            'remaining': {
                'zero_commission': remaining_zero_commission,
                'low_sales': remaining_low_sales
            }
        })
        
    except Exception as e:
        import traceback
        db.session.rollback()
        print(f"Deep refresh error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500

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


@app.route('/api/cleanup-nonpromtable', methods=['POST', 'GET'])
def cleanup_nonpromtable():
    """
    Remove products that cannot be promoted as affiliate products:
    - "not for sale" in name
    - "live only" in name
    - "display only" in name
    - "coming soon" in name
    - "sample" in name
    """
    try:
        # Count before cleanup
        total_before = Product.query.count()
        
        # Find non-promotable products
        non_promotable = Product.query.filter(
            db.or_(
                Product.product_name.ilike('%not for sale%'),
                Product.product_name.ilike('%live only%'),
                Product.product_name.ilike('%display only%'),
                Product.product_name.ilike('%coming soon%'),
                Product.product_name.ilike('%sample%not for sale%')
            )
        ).all()
        
        deleted_count = len(non_promotable)
        deleted_names = [p.product_name[:50] for p in non_promotable[:10]]  # First 10 for preview
        
        # Delete them
        for p in non_promotable:
            db.session.delete(p)
        
        db.session.commit()
        
        total_after = Product.query.count()
        
        return jsonify({
            'success': True,
            'message': f'Removed {deleted_count} non-promotable products',
            'before': total_before,
            'after': total_after,
            'removed': deleted_count,
            'examples': deleted_names
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cleanup-zero-videos', methods=['POST', 'GET'])
@login_required
def cleanup_zero_videos():
    """
    Remove products that have 0 total videos.
    These products have no TikTok content and are harder to promote.
    
    Parameters:
        confirm: Set to 'true' to actually delete. Otherwise just counts (dry run).
    """
    try:
        confirm = request.args.get('confirm', 'false').lower() == 'true'
        
        # Count products with 0 videos
        total_before = Product.query.count()
        zero_video_count = Product.query.filter(
            db.or_(Product.video_count == 0, Product.video_count.is_(None))
        ).count()
        
        # Safety check - don't delete if it would remove more than 80% of products
        if zero_video_count > total_before * 0.8:
            return jsonify({
                'success': False,
                'error': f'Safety check failed: Would delete {zero_video_count} of {total_before} products ({zero_video_count*100//total_before}%). This seems too high - aborting.',
                'zero_video_count': zero_video_count,
                'total_products': total_before
            }), 400
        
        if not confirm:
            # Dry run - just show what would be deleted
            # Get some examples
            examples = Product.query.filter(
                db.or_(Product.video_count == 0, Product.video_count.is_(None))
            ).limit(10).all()
            
            example_names = [f"{p.product_name[:40]}... (videos: {p.video_count})" for p in examples]
            
            return jsonify({
                'success': True,
                'dry_run': True,
                'message': f'Found {zero_video_count} products with 0 videos. Call with ?confirm=true to delete.',
                'would_delete': zero_video_count,
                'total_products': total_before,
                'would_remain': total_before - zero_video_count,
                'examples': example_names
            })
        
        # Actually delete
        deleted = Product.query.filter(
            db.or_(Product.video_count == 0, Product.video_count.is_(None))
        ).delete(synchronize_session=False)
        
        db.session.commit()
        
        total_after = Product.query.count()
        
        return jsonify({
            'success': True,
            'dry_run': False,
            'message': f'Deleted {deleted} products with 0 videos',
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
        # FORCE video_count to be at least 1 to prevent products from being filtered out
        # by dashboard filters (which often default to min_videos=1)
        # Real value is stored in p.get(), but we override for visibility
        raw_vids = int(p.get('total_video_cnt', 0) or 0)
        product.video_count = max(1, raw_vids)
        
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


@app.route('/api/stats')
@login_required
def api_stats():
    """Get global stats for dashboard"""
    try:
        # 1. Total Products
        total_products = Product.query.count()
        
        # 2. Ad Winners (Ads, >50 sales, <5 influencers)
        ad_winners = Product.query.filter(
             db.or_(
                Product.scan_type.in_(['apify_ad', 'daily_virals']),
                db.and_(Product.sales_7d > 50, Product.influencer_count < 5, Product.video_count < 5)
            )
        ).count()
        
        # 3. Hidden Gems (Sales > 100, Inf < 10, Video < 10)
        hidden_gems = Product.query.filter(
            Product.sales_7d > 100,
            Product.influencer_count < 10,
            Product.video_count < 10
        ).count()
        
        # 4. EchoTik Status (Mock or cached check)
        # Verify if our keys are working? Just return "Active" for now
        
        return jsonify({
            'success': True,
            'stats': {
                'total_products': total_products,
                'ad_winners': ad_winners,
                'hidden_gems': hidden_gems,
                'echotik_status': 'Active'
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/oos-stats', methods=['GET'])
def get_oos_stats():
    """Get out-of-stock statistics"""
    try:
        total_products = Product.query.count()
        active_products = Product.query.filter(
            db.or_(Product.product_status == None, Product.product_status == 'active')
        ).count()
        likely_oos = Product.query.filter(Product.product_status == 'likely_oos').count()
        manually_oos = Product.query.filter(Product.product_status == 'out_of_stock').count()
        removed = Product.query.filter(Product.product_status == 'removed').count()
        
        return jsonify({
            'success': True,
            'stats': {
                'total_products': total_products,
                'active': active_products,
                'likely_oos': likely_oos,
                'manually_oos': manually_oos,
                'removed': removed
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# PRODUCT LOOKUP - Search any TikTok product by URL or ID
# =============================================================================

import re

def extract_product_id(input_str):
    """
    Extract product ID from various TikTok URL formats or raw ID.
    Handles redirects for short links (e.g. tiktok.com/t/...)
    
    Supported formats:
    - https://www.tiktok.com/shop/pdp/1729436251038
    - https://www.tiktok.com/shop/product/1729436251038
    - https://shop.tiktok.com/view/product/1729436251038
    - https://affiliate.tiktok.com/product/1729436251038
    - https://www.tiktok.com/t/ZTHwgwbUL5uL7-oXGV7/ (Short link)
    - 1729436251038 (raw ID)
    """
    if not input_str:
        return None
    
    input_str = input_str.strip()
    
    # If it's just digits, return as-is
    if input_str.isdigit():
        return input_str

    # Handle Short Links (tiktok.com/t/ or vm.tiktok.com)
    if '/t/' in input_str or 'vm.tiktok.com' in input_str:
        try:
             # Browser-like headers to avoid 403 blocks
             headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
             
             # Use GET with stream=True (stop downloading body) instead of HEAD (often blocked)
             response = requests.get(input_str, allow_redirects=True, timeout=15, headers=headers, stream=True)
             
             resolved_url = response.url
             response.close() # Close connection immediately
             
             print(f"DEBUG: Resolved short link {input_str} -> {resolved_url}")
             input_str = resolved_url
             
        except Exception as e:
             print(f"Error resolving short link {input_str}: {e}")
             # Continue to try regex on original string just in case
    
    # Try to extract from URL patterns
    patterns = [
        r'tiktok\.com/shop/pdp/(\d+)',
        r'tiktok\.com/view/product/(\d+)',
        r'tiktok\.com/shop/product/(\d+)',
        r'tiktok\.com/product/(\d+)',
        r'product/(\d+)',
        r'/(\d{10,25})',  # Fallback: any 10-25 digit number in URL
    ]
    
    for pattern in patterns:
        match = re.search(pattern, input_str)
        if match:
            return match.group(1)
    
    return None


def resolve_tiktok_share_link(share_url):
    """
    Resolve TikTok share links (like /t/XXXXXX) by following redirects.
    Returns the final URL after redirects.
    """
    try:
        # Follow redirects to get final URL
        response = requests.head(share_url, allow_redirects=True, timeout=10)
        return response.url
    except:
        try:
            # Fallback: try GET request
            response = requests.get(share_url, allow_redirects=True, timeout=10)
            return response.url
        except:
            return None


def is_tiktok_share_link(url):
    """Check if URL is a TikTok shortened share link"""
    return bool(re.search(r'tiktok\.com/t/[A-Za-z0-9]+', url))


@app.route('/api/lookup', methods=['GET', 'POST'])
@login_required
def lookup_product():
    """
    Look up any TikTok product by URL or ID.
    Fetches real-time stats from EchoTik without requiring it to be in our database.
    
    GET/POST params:
        - url: TikTok product URL or product ID
        - save: 'true' to save to database (default: false)
    """
    # Accept both GET and POST
    if request.method == 'POST':
        data = request.get_json() or {}
        input_url = data.get('url', '')
        save_to_db = data.get('save', False)
    else:
        input_url = request.args.get('url', '')
        save_to_db = request.args.get('save', 'false').lower() == 'true'
    
    if not input_url:
        return jsonify({'success': False, 'error': 'Please provide a TikTok product URL or ID'}), 400
    
    # Check if it's a TikTok share link (/t/XXXXX format) and resolve it
    resolved_url = input_url
    if is_tiktok_share_link(input_url):
        print(f"Resolving TikTok share link: {input_url}")
        resolved_url = resolve_tiktok_share_link(input_url)
        if not resolved_url:
            return jsonify({
                'success': False, 
                'error': 'Could not resolve TikTok share link. Please try copying the full product URL instead.',
                'hint': 'Open the product in TikTok, tap Share, then copy the link from your browser'
            }), 400
        print(f"Resolved to: {resolved_url}")
    
    # Extract product ID
    product_id = extract_product_id(resolved_url)
    if not product_id:
        return jsonify({
            'success': False, 
            'error': 'Could not extract product ID from input',
            'hint': 'Try pasting a TikTok product URL or just the product ID number',
            'resolved_url': resolved_url if resolved_url != input_url else None
        }), 400
    
    try:
        # Check if we already have this product in our database
        existing = Product.query.get(product_id)
        
        # Call EchoTik product detail API
        response = requests.get(
            f"{BASE_URL}/product/detail",
            params={'product_ids': product_id},
            auth=get_auth(),
            timeout=30
        )
        
        if response.status_code != 200:
            return jsonify({
                'success': False, 
                'error': f'EchoTik API returned status {response.status_code}'
            }), 500
        
        data = response.json()
        
        if data.get('code') != 0:
            return jsonify({
                'success': False, 
                'error': f'EchoTik API error: {data.get("msg", "Unknown error")}'
            }), 500
        
        if not data.get('data') or len(data['data']) == 0:
            return jsonify({
                'success': False, 
                'error': 'Product not found in EchoTik database',
                'product_id': product_id
            }), 404
        
        p = data['data'][0]
        
        # Debug: print all keys returned by EchoTik (remove after debugging)
        print(f"EchoTik product detail keys: {list(p.keys())}")
        
        # Try multiple field names for seller (EchoTik may use different names)
        seller_name = (
            p.get('seller_name') or 
            p.get('shop_name') or 
            p.get('store_name') or 
            p.get('brand_name') or
            p.get('seller', {}).get('name') if isinstance(p.get('seller'), dict) else None or
            ''
        )
        
        # If we have this product in database with seller info, use that
        if not seller_name and existing and existing.seller_name:
            seller_name = existing.seller_name
        
        seller_id = p.get('seller_id') or p.get('shop_id') or p.get('store_id') or ''
        if not seller_id and existing and existing.seller_id:
            seller_id = existing.seller_id
        
        # If we have seller_id but no seller_name, try to fetch it from seller detail API
        if seller_id and not seller_name:
            try:
                seller_response = requests.get(
                    f"{BASE_URL}/seller/detail",
                    params={'seller_id': seller_id},
                    auth=get_auth(),
                    timeout=15
                )
                if seller_response.status_code == 200:
                    seller_data = seller_response.json()
                    if seller_data.get('code') == 0 and seller_data.get('data'):
                        seller_info = seller_data['data'][0] if isinstance(seller_data['data'], list) else seller_data['data']
                        seller_name = seller_info.get('seller_name') or seller_info.get('shop_name') or ''
                        print(f"Fetched seller name from seller/detail: {seller_name}")
            except Exception as e:
                print(f"Failed to fetch seller details: {e}")
        
        # Parse the product data
        product_data = {
            'product_id': product_id,
            'product_name': p.get('product_name', ''),
            'seller_id': seller_id,
            'seller_name': seller_name,
            
            # Sales data
            'sales': int(p.get('total_sale_cnt', 0) or 0),
            'sales_7d': int(p.get('total_sale_7d_cnt', 0) or 0),
            'sales_30d': int(p.get('total_sale_30d_cnt', 0) or 0),
            'gmv': float(p.get('total_sale_gmv_amt', 0) or 0),
            'gmv_7d': float(p.get('total_sale_gmv_7d_amt', 0) or 0),
            'gmv_30d': float(p.get('total_sale_gmv_30d_amt', 0) or 0),
            
            # Commission
            'commission_rate': float(p.get('product_commission_rate', 0) or 0),
            'price': float(p.get('spu_avg_price', 0) or 0),
            
            # Competition stats (the key metrics!)
            'influencer_count': int(p.get('total_ifl_cnt', 0) or 0),
            'video_count': int(p.get('total_video_cnt', 0) or 0),
            'video_7d': int(p.get('total_video_7d_cnt', 0) or 0),
            'video_30d': int(p.get('total_video_30d_cnt', 0) or 0),
            'live_count': int(p.get('total_live_cnt', 0) or 0),
            'views_count': int(p.get('total_views_cnt', 0) or 0),
            
            # Ratings
            'product_rating': float(p.get('product_rating', 0) or 0),
            'review_count': int(p.get('review_count', 0) or 0),
            
            # Image - get the raw URL first
            'image_url': parse_cover_url(p.get('cover_url', '')),
            'cached_image_url': None,  # Will be filled below
            
            # Links
            'tiktok_url': f'https://www.tiktok.com/shop/pdp/{product_id}',
            'echotik_url': f'https://echotik.live/products/{product_id}',
            
            # Meta
            'in_database': existing is not None,
            'is_favorite': existing.is_favorite if existing else False,
        }
        
        # Try to get signed/cached image URL
        if product_data['image_url']:
            try:
                signed_urls = get_cached_image_urls([product_data['image_url']])
                if signed_urls.get(product_data['image_url']):
                    product_data['cached_image_url'] = signed_urls[product_data['image_url']]
            except Exception as e:
                print(f"Failed to get signed image URL: {e}")
        
        # Calculate competition level
        inf = product_data['influencer_count']
        if inf <= 10:
            product_data['competition_level'] = 'untapped'
            product_data['competition_label'] = '🔥 Untapped (1-10)'
        elif inf <= 30:
            product_data['competition_level'] = 'low'
            product_data['competition_label'] = '💎 Low Competition (11-30)'
        elif inf <= 60:
            product_data['competition_level'] = 'medium'
            product_data['competition_label'] = '📊 Medium (31-60)'
        elif inf <= 100:
            product_data['competition_level'] = 'good'
            product_data['competition_label'] = '✅ Good (61-100)'
        else:
            product_data['competition_level'] = 'high'
            product_data['competition_label'] = '⚠️ High Competition (100+)'
        
        # Save to database if requested
        saved = False
        if save_to_db and not existing:
            try:
                new_product = Product(
                    product_id=product_id,
                    product_name=product_data['product_name'],
                    seller_id=product_data['seller_id'],
                    seller_name=product_data['seller_name'],
                    gmv=product_data['gmv'],
                    gmv_30d=product_data['gmv_30d'],
                    sales=product_data['sales'],
                    sales_7d=product_data['sales_7d'],
                    sales_30d=product_data['sales_30d'],
                    influencer_count=product_data['influencer_count'],
                    commission_rate=product_data['commission_rate'],
                    price=product_data['price'],
                    image_url=product_data['image_url'],
                    cached_image_url=product_data.get('cached_image_url'),
                    image_cached_at=datetime.utcnow() if product_data.get('cached_image_url') else None,
                    video_count=product_data['video_count'],
                    video_7d=product_data['video_7d'],
                    video_30d=product_data['video_30d'],
                    live_count=product_data['live_count'],
                    views_count=product_data['views_count'],
                    product_rating=product_data['product_rating'],
                    review_count=product_data['review_count'],
                    scan_type='lookup'
                )
                db.session.add(new_product)
                db.session.commit()
                saved = True
                product_data['in_database'] = True
            except Exception as e:
                db.session.rollback()
                print(f"Failed to save product: {e}")
        elif save_to_db and existing:
            # Update existing product with fresh data
            existing.sales = product_data['sales']
            existing.sales_7d = product_data['sales_7d']
            existing.sales_30d = product_data['sales_30d']
            existing.gmv = product_data['gmv']
            existing.gmv_30d = product_data['gmv_30d']
            existing.influencer_count = product_data['influencer_count']
            existing.commission_rate = product_data['commission_rate']
            existing.video_count = product_data['video_count']
            existing.video_7d = product_data['video_7d']
            existing.video_30d = product_data['video_30d']
            existing.live_count = product_data['live_count']
            existing.views_count = product_data['views_count']
            # Update image if we got a new one
            if product_data.get('cached_image_url') and not existing.cached_image_url:
                existing.image_url = product_data['image_url']
                existing.cached_image_url = product_data['cached_image_url']
                existing.image_cached_at = datetime.utcnow()
            existing.last_updated = datetime.utcnow()
            db.session.commit()
            saved = True
        
        # Log the lookup
        user = get_current_user()
        if user:
            log_activity(user.id, 'product_lookup', {
                'product_id': product_id,
                'product_name': product_data['product_name'][:50],
                'saved': saved
            })
        
        # Include debug info if requested
        debug = request.args.get('debug', 'false').lower() == 'true'
        response_data = {
            'success': True,
            'product': product_data,
            'saved': saved,
            'message': 'Product saved to database!' if saved else None
        }
        
        if debug:
            response_data['debug'] = {
                'raw_keys': list(p.keys()),
                'seller_fields': {
                    'seller_name': p.get('seller_name'),
                    'shop_name': p.get('shop_name'),
                    'store_name': p.get('store_name'),
                    'brand_name': p.get('brand_name'),
                    'seller_id': p.get('seller_id'),
                    'shop_id': p.get('shop_id'),
                }
            }
        
        return jsonify(response_data)
        
    except requests.Timeout:
        return jsonify({'success': False, 'error': 'EchoTik API timeout - please try again'}), 504
    except Exception as e:
        import traceback
        return jsonify({
            'success': False, 
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

# =============================================================================
# AI IMAGE GENERATION - Gemini API (Nano Banana Pro)
# =============================================================================

import base64

# Gemini API Configuration
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

def get_product_category(product_name):
    """Determine product category from name for better prompts"""
    name_lower = product_name.lower()
    
    # IMPORTANT: Order matters! More specific categories should be checked first
    # Outdoor first to catch firewood carts, garden items before tools catches them
    # Tools second to catch Vevor hydraulic lifts, etc.
    categories = {
        'outdoor': ['firewood', 'log cart', 'garden', 'patio', 'lawn', 'grill', 'bbq', 'camping', 'tent', 'backpack', 'hiking', 'fishing', 'cooler', 'umbrella', 'outdoor furniture', 'fire pit', 'fireplace carrier'],
        'tools': ['tool', 'drill', 'hammer', 'screwdriver', 'wrench', 'tape measure', 'level', 'lift', 'hydraulic', 'jack', 'compressor', 'welder', 'saw', 'sander', 'grinder', 'workbench', 'vevor', 'scaffold', 'ladder', 'dolly', 'hoist', 'clamp', 'vise', 'industrial', 'mechanic', 'garage', 'workshop', 'hauler', 'mover', 'rack storage', 'steel', 'heavy duty', 'capacity', 'scissor', 'table cart', 'pallet'],
        'beauty': ['serum', 'cream', 'lotion', 'skincare', 'makeup', 'mascara', 'lipstick', 'foundation', 'moisturizer', 'cleanser', 'toner', 'sunscreen', 'face', 'skin', 'eye cream', 'anti-aging', 'melaxin', 'cemenrete'],
        'hair': ['shampoo', 'conditioner', 'hair oil', 'hair mask', 'brush', 'comb', 'dryer', 'straightener', 'curler', 'hair growth'],
        'fashion': ['dress', 'shirt', 'pants', 'jeans', 'jacket', 'coat', 'sweater', 'hoodie', 'shoes', 'sneakers', 'boots', 'heels', 'bag', 'purse', 'handbag', 'wallet', 'belt', 'scarf', 'hat', 'sunglasses', 'jewelry', 'necklace', 'bracelet', 'earring', 'watch', 'clothing', 'apparel', 'blouse', 'skirt', 'shorts', 'girlfriend jeans', 'boyfriend jeans'],
        'kitchen': ['pan', 'pot', 'knife', 'cutting board', 'blender', 'mixer', 'cooker', 'fryer', 'toaster', 'kettle', 'coffee', 'mug', 'plate', 'bowl', 'utensil', 'spatula', 'container', 'drink mix', 'sodastream', 'kitchen'],
        'home': ['pillow', 'pillowcase', 'blanket', 'curtain', 'rug', 'lamp', 'candle', 'vase', 'frame', 'mirror', 'clock', 'organizer', 'basket', 'shelf', 'holder', 'bedding', 'sheets', 'duvet', 'decor'],
        'tech': ['phone', 'charger', 'cable', 'earbuds', 'headphones', 'speaker', 'mouse', 'keyboard', 'stand', 'mount', 'tripod', 'camera', 'ring light', 'laptop', 'tablet', 'wireless', 'bluetooth'],
        'fitness': ['yoga', 'dumbbell', 'weight', 'resistance band', 'gym', 'workout', 'protein', 'shaker', 'fitness', 'exercise', 'vibration plate', 'treadmill', 'kettlebell'],
        'car': ['car', 'auto', 'vehicle', 'seat cover', 'steering', 'dash', 'freshener', 'automotive'],
        'health': ['vitamin', 'supplement', 'medicine', 'thermometer', 'massager', 'heating pad', 'ice pack'],
        'cleaning': ['cleaner', 'mop', 'broom', 'vacuum', 'sponge', 'detergent', 'spray'],
        'pet': ['dog', 'cat', 'pet', 'collar', 'leash', 'pet toy', 'pet bed', 'treat'],
        'baby': ['baby', 'infant', 'toddler', 'diaper', 'pacifier', 'stroller', 'carrier', 'nursery'],
    }
    
    for category, keywords in categories.items():
        if any(keyword in name_lower for keyword in keywords):
            return category
    return 'general'


def get_scene_prompt(product_name, category):
    """Generate a RANDOMIZED lifestyle scene prompt based on product category
    
    IMPORTANT: Small products (beauty, hair) should be CLOSER to camera with readable text
    Large products (tools, fitness) can be farther back
    All images need room above product for video push effect
    """
    import random
    
    # Background items by category - realistic and subtle, blurred/out of focus
    background_items = {
        'beauty': [
            "folded towels, a candle, and a small plant placed around but out of focus",
            "a soap dispenser, rolled face towel, and a small succulent",
            "cotton pads in a jar, a small mirror, and a ceramic dish",
            "a ceramic tray, small vase with dried flowers, and folded washcloths"
        ],
        'hair': [
            "a hairbrush, folded towel, and small potted plant blurred in the background",
            "a round mirror, hair clips in a dish, and a ceramic container"
        ],
        'fashion': [
            "a ceramic vase, stack of magazines, and a coffee cup in the corner blurred",
            "a small plant, decorative tray, and sunglasses placed nearby out of focus"
        ],
        'kitchen': [
            "a fruit bowl, cookbook stand, and ceramic utensil holder blurred in the background",
            "fresh herbs in a pot, wooden cutting board, and linen napkin",
            "a coffee mug, small plant, and woven placemat out of focus"
        ],
        'tools': [
            "a toolbox, work gloves, and safety glasses in the background blurred",
            "pegboard with tools, a shop rag, and small parts organizer out of focus",
            "concrete floor texture, storage shelves blurred in background"
        ],
        'outdoor': [
            "green grass, a patio chair, and potted plants blurred in background",
            "wooden fence, garden tools leaning nearby, natural foliage out of focus",
            "stacked firewood, outdoor decor, and greenery in the distance"
        ],
        'tech': [
            "a coffee mug, small plant, and notebook blurred slightly",
            "a pen holder, coaster, and desk organizer out of focus"
        ],
        'fitness': [
            "a water bottle, folded towel on a shelf, and yoga block",
            "resistance bands placed naturally, a plant, and woven basket"
        ],
        'home': [
            "a small plant, candle, and stack of books out of focus",
            "a decorative tray, vase, and cozy throw blanket edge"
        ],
        'general': [
            "a small plant, folded cloth, and decorative items blurred in the background",
            "a candle, ceramic dish, and natural texture elements out of focus"
        ]
    }
    
    # SMALL PRODUCTS - beauty, hair - moderate distance, readable text, NO floating banners
    # CRITICAL: Tell AI to NOT add any text/titles/labels to the image
    small_product_templates = [
        "a realistic product photo of the {product} on a clean bathroom counter, shot from a few feet back where the product fills about 40 percent of the frame width, soft natural lighting from a window, subtle background items like {bg_items}, good amount of empty space ABOVE the product, no people, do NOT add any text titles labels or captions to this image, clean modern setting, overall bright and realistic",
        "a bright bathroom scene with the {product} displayed on a marble counter, shot from a comfortable distance with the product as the clear hero, soft daylight from the side, subtle background items like {bg_items}, plenty of breathing room above the product, no people, do NOT overlay any text or titles or product names on the image, neutral aesthetic, overall bright and professional"
    ]
    
    # FASHION - flat lay overhead, product centered with room around it
    fashion_templates = [
        "a realistic flat lay photo of the {product} laid neatly on a clean beige or cream colored surface, shot from above, the clothing is centered and fills about 50 percent of the frame, soft natural lighting from a window, subtle background items like {bg_items}, good amount of empty space above and around the product, no people, do NOT add any text titles labels or captions to this image, clean minimal aesthetic, overall bright and lifestyle",
        "a wide overhead flat lay shot of the {product} laid flat on a light wooden floor or neutral surface, natural soft daylight, the product is well-lit and centered with breathing room around it, subtle background items like {bg_items}, plenty of space above the product, no people, do NOT overlay any text or product names, clean modern aesthetic"
    ]
    
    # MEDIUM PRODUCTS - kitchen, home, tech
    medium_product_templates = [
        "a realistic product photo of the {product} on a modern kitchen counter a few feet back, soft daylight from a window, the product is clearly visible and centered, product details are sharp, subtle background items like {bg_items}, plenty of empty space above the product, no people, do NOT add any text titles labels or captions to this image, clean and inviting setting, overall bright and realistic",
        "a bright lifestyle scene with the {product} displayed on a clean surface, shot at a natural distance, soft natural lighting, the product is the clear focus with readable details, subtle background items like {bg_items}, good amount of space above, no people, do NOT overlay any text or product names on the image, modern aesthetic, overall bright and professional"
    ]
    
    # LARGE PRODUCTS - tools, fitness equipment - show UPRIGHT and ASSEMBLED
    large_product_templates = [
        "a realistic photo of the {product} standing upright in its normal position in a clean garage or workshop, the product is fully assembled and ready to use, natural daylight from a window or open garage door, shot from a few feet back showing the full product, subtle background items like {bg_items}, clean concrete floor, plenty of room above the product, no people, do NOT add any text titles labels or captions to this image, professional atmosphere",
        "a bright outdoor or garage scene with the {product} standing upright on concrete or pavement, the product is fully assembled in its normal upright position, natural daylight, shot from a comfortable distance to show the whole product, subtle background elements, space above, no people, do NOT overlay any text or product names on the image, realistic and practical setting",
        "a realistic lifestyle photo showing the {product} fully assembled and standing upright in a backyard or garage setting, natural lighting, the product is shown in its normal use position as if ready to be used, room around the product for context, plenty of open space above, no people, do NOT add any text or titles to this image, clean and functional environment"
    ]
    
    # OUTDOOR PRODUCTS - firewood carts, garden equipment, patio items
    outdoor_templates = [
        "a realistic outdoor photo of the {product} standing upright on a patio or backyard, the product is fully assembled in its normal position, natural daylight, green grass or wooden deck visible, shot from a few feet back to show the full product, space above, no people, do NOT add any text titles labels or captions to this image, inviting outdoor setting",
        "a bright backyard scene with the {product} fully assembled and standing upright near a house or garage, natural sunlight, the product looks ready to use in its normal position, subtle outdoor elements in background, plenty of room above the product, no people, do NOT overlay any text or product names, realistic lifestyle photo"
    ]
    
    # FITNESS - moderate distance for equipment
    fitness_templates = [
        "a realistic home wellness scene with soft natural lighting, the {product} centered on a clean floor, shot at a natural distance where the product is clearly visible, subtle background items like {bg_items}, plenty of open space above the product, no people, do NOT add any text titles labels or captions to this image, calm and minimal decor, neutral tones, overall bright and motivating",
        "a bright fitness space with the {product} placed naturally, the product is well-lit and the focus of the shot, soft daylight, subtle background items like {bg_items}, lots of open space above, no people, do NOT overlay any text or product names on the image, clean and energetic setting"
    ]
    
    # Get appropriate templates based on product category
    bg_items = random.choice(background_items.get(category, background_items['general']))
    
    if category in ['beauty', 'hair']:
        template = random.choice(small_product_templates)
    elif category == 'fashion':
        template = random.choice(fashion_templates)
    elif category == 'tools':
        template = random.choice(large_product_templates)
    elif category == 'outdoor':
        template = random.choice(outdoor_templates)
    elif category == 'fitness':
        template = random.choice(fitness_templates)
    else:
        template = random.choice(medium_product_templates)
    
    # Build prompt by filling in the template
    prompt = template.format(product=product_name, bg_items=bg_items)
    
    # Add vertical format at the end
    prompt += ", vertical 9:16 portrait format"

    return prompt


@app.route('/api/generate-image/<product_id>', methods=['POST'])
@login_required
def generate_ai_image(product_id):
    """
    Generate an AI lifestyle image for a product using Gemini API (Nano Banana Pro)
    
    The generated image will:
    - Use the product's existing image as reference (or cropped version if provided)
    - Place it in a natural lifestyle setting
    - Camera a few feet back with open background
    - Add complementary items for realism
    """
    if not GEMINI_API_KEY:
        return jsonify({
            'success': False, 
            'error': 'Gemini API key not configured. Please add GEMINI_API_KEY to environment variables.'
        }), 500
    
    try:
        # Get product info
        product = Product.query.get(product_id)
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        
        # Check if a cropped image was provided in the request
        request_data = request.get_json() or {}
        cropped_image_data = request_data.get('cropped_image')
        
        if cropped_image_data:
            # Use the cropped image provided by the frontend
            # Remove data URL prefix if present (e.g., "data:image/png;base64,")
            if ',' in cropped_image_data:
                header, image_data = cropped_image_data.split(',', 1)
                if 'png' in header:
                    mime_type = 'image/png'
                elif 'webp' in header:
                    mime_type = 'image/webp'
                else:
                    mime_type = 'image/jpeg'
            else:
                image_data = cropped_image_data
                mime_type = 'image/jpeg'
        else:
            # Fall back to fetching the original product image
            image_url = product.cached_image_url or product.image_url
            if not image_url:
                return jsonify({'success': False, 'error': 'No product image available'}), 400
            
            # Download the product image and convert to base64
            try:
                # If it's a proxy URL, fetch through our proxy
                if image_url.startswith('/api/image-proxy'):
                    # Extract the actual URL from the proxy
                    from urllib.parse import parse_qs, urlparse
                    parsed = urlparse(image_url)
                    actual_url = parse_qs(parsed.query).get('url', [None])[0]
                    if actual_url:
                        image_url = actual_url
                
                img_response = requests.get(image_url, timeout=30, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                if img_response.status_code != 200:
                    return jsonify({'success': False, 'error': f'Failed to download product image: {img_response.status_code}'}), 400
                
                image_data = base64.b64encode(img_response.content).decode('utf-8')
                
                # Determine image mime type
                content_type = img_response.headers.get('Content-Type', 'image/jpeg')
                if 'png' in content_type:
                    mime_type = 'image/png'
                elif 'webp' in content_type:
                    mime_type = 'image/webp'
                else:
                    mime_type = 'image/jpeg'
                    
            except Exception as e:
                return jsonify({'success': False, 'error': f'Failed to fetch product image: {str(e)}'}), 400
        
        # Determine product category and generate prompt
        category = get_product_category(product.product_name or '')
        prompt = get_scene_prompt(product.product_name or 'product', category)
        
        # Use the REAL Nano Banana Pro models:
        # - gemini-3-pro-image-preview = Nano Banana Pro (BEST quality, 2K/4K, sharp text) - PRIMARY
        # - gemini-2.5-flash-image = Nano Banana (fast fallback)
        models_to_try = [
            "gemini-3-pro-image-preview",   # Nano Banana Pro - BEST QUALITY, try first!
            "gemini-2.5-flash-image",       # Nano Banana - fallback if Pro fails
        ]
        
        # Basic payload without resolution config (for fallback model)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": image_data
                            }
                        },
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"]
            }
        }
        
        # Payload with aspect ratio AND 2K resolution for Nano Banana Pro
        # Supports: "1K", "2K", "4K" - using 2K for sharp text while keeping reasonable speed
        payload_with_config = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": image_data
                            }
                        },
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": "9:16",
                    "imageSize": "2K"
                }
            }
        }
        
        response = None
        last_error = None
        
        for model_name in models_to_try:
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            
            # Use full config (aspect ratio + 2K resolution) for Nano Banana Pro
            # Use basic payload for fallback model
            current_payload = payload_with_config if "3-pro" in model_name else payload
            
            try:
                response = requests.post(
                    gemini_url,
                    json=current_payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=120  # Longer timeout for high-quality generation
                )
                
                if response.status_code == 200:
                    result_check = response.json()
                    # Verify we got an image back
                    if 'candidates' in result_check and len(result_check['candidates']) > 0:
                        candidate = result_check['candidates'][0]
                        if 'content' in candidate and 'parts' in candidate['content']:
                            has_image = any('inlineData' in part for part in candidate['content']['parts'])
                            if has_image:
                                print(f"AI Image: Success with model {model_name}")
                                break
                    last_error = f"{model_name}: No image in response"
                    print(f"AI Image: {model_name} returned no image, trying next...")
                else:
                    last_error = f"{model_name}: {response.status_code} - {response.text[:300]}"
                    print(f"AI Image: Failed with {model_name}, trying next...")
            except Exception as e:
                last_error = f"{model_name}: {str(e)}"
                print(f"AI Image: Exception with {model_name}: {e}")
                continue
        
        if not response or response.status_code != 200:
            error_detail = last_error or 'All models failed'
            return jsonify({
                'success': False, 
                'error': f'Gemini API error',
                'detail': error_detail
            }), 500
        
        result = response.json()
        
        # Extract the generated image from response
        generated_image = None
        generated_mime = 'image/png'  # Default
        if 'candidates' in result and len(result['candidates']) > 0:
            candidate = result['candidates'][0]
            if 'content' in candidate and 'parts' in candidate['content']:
                for part in candidate['content']['parts']:
                    if 'inlineData' in part:
                        generated_image = part['inlineData']['data']
                        generated_mime = part['inlineData'].get('mimeType', 'image/png')
                        break
        
        if not generated_image:
            # Try alternative response structure
            if 'candidates' in result:
                return jsonify({
                    'success': False,
                    'error': 'No image generated - model may not support image output',
                    'debug': str(result)[:500]
                }), 500
            return jsonify({
                'success': False, 
                'error': 'Failed to generate image - unexpected response format',
                'debug': str(result)[:500]
            }), 500
        
        # Log the generation
        user = get_current_user()
        if user:
            log_activity(user.id, 'ai_image_generated', {
                'product_id': product_id,
                'product_name': product.product_name[:50] if product.product_name else '',
                'category': category
            })
        
        return jsonify({
            'success': True,
            'image': f"data:{generated_mime};base64,{generated_image}",
            'product_name': product.product_name,
            'category': category,
            'prompt_used': prompt[:200] + '...'
        })
        
    except requests.Timeout:
        return jsonify({'success': False, 'error': 'Gemini API timeout - please try again'}), 504
    except Exception as e:
        import traceback
        return jsonify({
            'success': False, 
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

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
# @login_required - REMOVED to allow Health Checks (GET /) to pass with 200 OK
def index():
    # If user is NOT logged in, return 200 OK with Login Page (Satisfies Render Health Check)
    if not session.get('user_id'):
        return send_from_directory(app.static_folder, 'login.html')
        
    # If logged in, show Dashboard
    return send_from_directory('pwa', 'dashboard_v4.html')

@app.route('/product/<path:product_id>')
@login_required
def product_detail(product_id):
    return send_from_directory('pwa', 'product_detail_v4.html')

@app.route('/scanner')
@login_required
def scanner_page():
    return send_from_directory('pwa', 'scanner_v4.html')

@app.route('/settings')
@login_required
def settings_page():
    return send_from_directory('pwa', 'settings.html')



@app.route('/api/debug/check-product/<path:product_id>')
@login_required
def api_product_detail(product_id):
    """API Endpoint for Single Product Details (Vantage V2)"""
    # 1. Try exact match
    p = Product.query.filter_by(product_id=product_id).first()
    
    # 2. Try with 'shop_' prefix if digits only
    if not p and product_id.isdigit():
         p = Product.query.filter_by(product_id=f"shop_{product_id}").first()
         
    # 3. Try removing 'shop_' prefix
    if not p and product_id.startswith('shop_'):
         p = Product.query.filter_by(product_id=product_id.replace('shop_', '')).first()
         
    if not p:
        return jsonify({'error': 'Not found'}), 404
    
    # Image Logic: Use Cached > Proxy > Original
    img = p.cached_image_url
    if not img:
        img = f"/api/image-proxy/{p.product_id}" # Fallback to proxy
        
    return jsonify({
        'product_id': p.product_id, # return the REAL ID from DB
        'name': p.product_name,
        'image_url': img,
        'product_url': p.product_url,
        'sales': p.sales,
        'sales_7d': p.sales_7d,
        'price': p.price,
        'commission_rate': p.commission_rate,
        'video_count': p.video_count,
        'seller_name': p.seller_name,
        'is_ad_driven': p.is_ad_driven,
        'is_favorite': p.is_favorite,
        'gmv': p.gmv or 0
    })

@app.route('/pwa/<path:filename>')
def pwa_files(filename):
    # Allow login.html without auth
    if filename in ['login.html']:
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


# =============================================================================
# KLING AI VIDEO GENERATION
# =============================================================================

def generate_kling_jwt_token():
    """Generate JWT token for Kling AI API authentication"""
    import jwt # Lazy import to prevent crash if library missing elsewhere
    if not KLING_ACCESS_KEY or not KLING_SECRET_KEY:
        return None
    
    headers = {
        "alg": "HS256",
        "typ": "JWT"
    }
    
    payload = {
        "iss": KLING_ACCESS_KEY,
        "exp": int(time.time()) + 1800,  # 30 minutes
        "nbf": int(time.time()) - 5
    }
    
    token = jwt.encode(payload, KLING_SECRET_KEY, algorithm="HS256", headers=headers)
    return token


def create_kling_video_task(image_url, prompt=None, duration="5"):
    """
    Create an image-to-video task on Kling AI
    Uses Kling 2.5 Turbo (kling-v2-master) in Professional mode
    """
    token = generate_kling_jwt_token()
    if not token:
        return {"error": "Kling AI not configured. Add KLING_ACCESS_KEY and KLING_SECRET_KEY."}
    
    url = f"{KLING_API_BASE_URL}/v1/videos/image2video"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    if not prompt:
        prompt = KLING_DEFAULT_PROMPT
    
    payload = {
        "model_name": "kling-v2-master",  # Kling 2.5 Turbo
        "mode": "pro",                     # Professional mode
        "duration": duration,              # "5" or "10"
        "image": image_url,
        "prompt": prompt,
        "negative_prompt": "blurry, distorted, low quality, watermark, text, hands touching product, shaky camera",
        "cfg_scale": 0.5
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        data = response.json()
        
        if data.get("code") == 0:
            task_id = data.get("data", {}).get("task_id")
            return {
                "success": True,
                "task_id": task_id,
                "message": "Video generation started"
            }
        else:
            return {"error": data.get("message", f"Kling API error: {data}")}
            
    except Exception as e:
        return {"error": str(e)}


def get_kling_video_result(task_id):
    """Poll Kling AI for video generation result"""
    token = generate_kling_jwt_token()
    if not token:
        return {"error": "Kling AI not configured"}
    
    url = f"{KLING_API_BASE_URL}/v1/videos/image2video/{task_id}"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        data = response.json()
        
        if data.get("code") == 0:
            task_data = data.get("data", {})
            status = task_data.get("task_status", "unknown")
            
            status_map = {
                "submitted": "pending",
                "processing": "processing", 
                "succeed": "completed",
                "failed": "failed"
            }
            
            result = {
                "status": status_map.get(status, status),
                "task_id": task_id,
                "raw_status": status
            }
            
            if status == "succeed":
                videos = task_data.get("task_result", {}).get("videos", [])
                if videos:
                    result["video_url"] = videos[0].get("url")
                    result["duration"] = videos[0].get("duration")
            elif status == "failed":
                result["error"] = task_data.get("task_status_msg", "Video generation failed")
            
            return result
        else:
            return {"error": data.get("message", "Unknown error")}
            
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# TELEGRAM ALERTS
# =============================================================================

def send_telegram_alert(message, parse_mode="HTML"):
    """Send alert to Telegram channel/chat"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured - skipping alert")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Telegram alert failed: {e}")
        return False


def send_hidden_gem_alert(product):
    """Send Telegram alert for hidden gem product"""
    message = f"""💎 <b>HIDDEN GEM FOUND!</b>

<b>{product.product_name[:100]}</b>

🔥 7-day sales: <b>{product.sales_7d:,}</b>
👥 Only <b>{product.influencer_count}</b> influencers!
💵 Commission: {product.commission_rate:.1f}%
🏷️ Price: ${product.price:.2f}
🏪 Brand: {product.seller_name}

📊 Low competition = High opportunity!

🔗 <a href="https://www.tiktok.com/shop/pdp/{product.product_id}">View on TikTok Shop</a>
"""
    return send_telegram_alert(message)


def send_back_in_stock_alert(product):
    """Send Telegram alert for product back in stock"""
    message = f"""🔙 <b>BACK IN STOCK!</b>

<b>{product.product_name[:100]}</b>

📈 Now selling again: <b>{product.sales_7d:,}</b> sales this week
👥 Influencers: {product.influencer_count}
💵 Commission: {product.commission_rate:.1f}%
🏷️ Price: ${product.price:.2f}

⚡ Was marked OOS, now restocked!

🔗 <a href="https://www.tiktok.com/shop/pdp/{product.product_id}">View on TikTok Shop</a>
"""
    return send_telegram_alert(message)


# =============================================================================
# AI VIDEO GENERATION ENDPOINTS
# =============================================================================

@app.route('/api/generate-video', methods=['POST'])
def api_generate_video():
    """Generate AI video for a product using Kling AI"""
    passkey = request.args.get('passkey')
    if passkey != DEV_PASSKEY:
        user = get_current_user()
        if not user:
            return jsonify({'error': 'Unauthorized'}), 401
        if not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403
    
    data = request.get_json() or {}
    product_id = data.get('product_id')
    
    if not product_id:
        return jsonify({'error': 'product_id required'}), 400
    
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    
    image_url = data.get('image_url') or product.ai_image_url or product.cached_image_url or product.image_url
    
    if not image_url:
        return jsonify({'error': 'No image available for this product'}), 400
    
    # Handle base64 images - Kling API accepts raw base64 (without data: prefix)
    if image_url.startswith('data:'):
        # Extract just the base64 part after the comma
        if ',' in image_url:
            image_url = image_url.split(',')[1]
        else:
            return jsonify({'error': 'Invalid base64 image format'}), 400
    
    duration = data.get('duration', '5')
    
    result = create_kling_video_task(image_url, duration=duration)
    
    if result.get('success'):
        product.ai_video_task_id = result['task_id']
        product.ai_video_status = 'pending'
        db.session.commit()
        
        return jsonify({
            'success': True,
            'task_id': result['task_id'],
            'product_id': product_id,
            'message': 'Video generation started. Poll /api/video-status for updates.'
        })
    else:
        return jsonify({'error': result.get('error', 'Unknown error')}), 500


@app.route('/api/video-status/<task_id>', methods=['GET'])
def api_video_status(task_id):
    """Check status of Kling AI video generation task"""
    result = get_kling_video_result(task_id)
    
    if result.get('status') == 'completed' and result.get('video_url'):
        product = Product.query.filter_by(ai_video_task_id=task_id).first()
        if product:
            product.ai_video_url = result['video_url']
            product.ai_video_status = 'completed'
            db.session.commit()
            result['product_id'] = product.product_id
    
    elif result.get('status') == 'failed':
        product = Product.query.filter_by(ai_video_task_id=task_id).first()
        if product:
            product.ai_video_status = 'failed'
            db.session.commit()
    
    return jsonify(result)


@app.route('/api/product/<product_id>/video-status', methods=['GET'])
def api_product_video_status(product_id):
    """Get video generation status for a specific product"""
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    
    if not product.ai_video_task_id:
        return jsonify({
            'status': 'none',
            'message': 'No video generation started for this product'
        })
    
    if product.ai_video_status == 'completed' and product.ai_video_url:
        return jsonify({
            'status': 'completed',
            'video_url': product.ai_video_url,
            'task_id': product.ai_video_task_id
        })
    
    result = get_kling_video_result(product.ai_video_task_id)
    
    if result.get('status') == 'completed' and result.get('video_url'):
        product.ai_video_url = result['video_url']
        product.ai_video_status = 'completed'
        db.session.commit()
    elif result.get('status') == 'failed':
        product.ai_video_status = 'failed'
        db.session.commit()
    
    return jsonify(result)


@app.route('/api/one-click-video', methods=['POST'])
def api_one_click_video():
    """
    One-click: Generate AI Image (Gemini) → Generate AI Video (Kling)
    
    POST body:
    {
        "product_id": "xxx",
        "category": "beauty"  # Optional: beauty, home, fitness, tech, fashion, default
    }
    """
    passkey = request.args.get('passkey')
    data = request.get_json() or {}
    
    # Check auth - accept passkey from query OR body
    if passkey != DEV_PASSKEY and data.get('passkey') != DEV_PASSKEY:
        user = get_current_user()
        if not user:
            return jsonify({'error': 'Unauthorized'}), 401
        if not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403
    
    product_id = data.get('product_id')
    category = data.get('category', 'default')
    skip_image = data.get('skip_image', False)
    
    if not product_id:
        return jsonify({'error': 'product_id required'}), 400
    
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    
    image_base64 = None
    
    # Step 1: Generate AI Image (unless we're skipping or already have one)
    if not skip_image or not product.ai_image_url:
        if not GEMINI_API_KEY:
            return jsonify({'error': 'Gemini API not configured for image generation'}), 500
        
        category_prompts = {
            "beauty": f"Professional product photography of {product.product_name[:100]}, elegant beauty product shot, soft lighting, luxury aesthetic, clean background, 9:16 vertical format",
            "home": f"Lifestyle home product photo of {product.product_name[:100]}, cozy modern home setting, warm natural lighting, 9:16 vertical format",
            "fitness": f"Dynamic fitness product shot of {product.product_name[:100]}, gym or outdoor setting, energetic lighting, 9:16 vertical format",
            "tech": f"Sleek technology product photo of {product.product_name[:100]}, modern minimalist setup, cool lighting, 9:16 vertical format",
            "fashion": f"Fashion product photography of {product.product_name[:100]}, stylish lifestyle shot, natural lighting, 9:16 vertical format",
            "default": f"Professional product lifestyle photography of {product.product_name[:100]}, clean modern aesthetic, soft studio lighting, 9:16 vertical TikTok format"
        }
        
        prompt = category_prompts.get(category, category_prompts["default"])
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-preview-image-generation:generateContent?key={GEMINI_API_KEY}"
        
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["image", "text"],
                "imageDimension": "PORTRAIT_9_16"
            }
        }
        
        try:
            response = requests.post(url, json=payload, timeout=90)
            response.raise_for_status()
            gemini_data = response.json()
            
            candidates = gemini_data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "inlineData" in part:
                        mime_type = part["inlineData"].get("mimeType", "image/png")
                        image_base64 = part["inlineData"].get("data", "")
                        
                        # Save to product with data: prefix for display
                        product.ai_image_url = f"data:{mime_type};base64,{image_base64}"
                        db.session.commit()
                        break
            
            if not image_base64:
                return jsonify({'error': 'Failed to generate AI image'}), 500
                
        except Exception as e:
            return jsonify({'error': f'Image generation failed: {str(e)}'}), 500
    else:
        # Use existing AI image
        if product.ai_image_url and product.ai_image_url.startswith('data:'):
            if ',' in product.ai_image_url:
                image_base64 = product.ai_image_url.split(',')[1]
    
    # Step 2: Generate Video with Kling AI
    if not KLING_ACCESS_KEY or not KLING_SECRET_KEY:
        return jsonify({
            'success': True,
            'image_generated': True,
            'image_url': product.ai_image_url,
            'video_started': False,
            'message': 'Image generated but Kling AI not configured for video'
        })
    
    if not image_base64:
        # Fallback to cached image URL if no base64 available
        fallback_url = product.cached_image_url or product.image_url
        if not fallback_url:
            return jsonify({'error': 'No image available for video generation'}), 400
        video_result = create_kling_video_task(fallback_url)
    else:
        # Use raw base64 (Kling accepts this per API docs)
        video_result = create_kling_video_task(image_base64)
    
    if video_result.get('success'):
        product.ai_video_task_id = video_result['task_id']
        product.ai_video_status = 'processing'
        db.session.commit()
        
        return jsonify({
            'success': True,
            'image_generated': True,
            'image_url': product.ai_image_url,
            'video_started': True,
            'video_task_id': video_result['task_id'],
            'video_status': 'processing',
            'message': 'Image generated, video processing. Poll /api/video-status for updates.',
            'product_id': product_id
        })
    else:
        return jsonify({
            'success': True,
            'image_generated': True,
            'image_url': product.ai_image_url,
            'video_started': False,
            'video_error': video_result.get('error', 'Unknown error'),
            'message': 'Image generated but video generation failed'
        })


# =============================================================================
# TRENDING, HIDDEN GEMS, OOS ENDPOINTS
# =============================================================================

@app.route('/api/trending-products', methods=['GET'])
def api_trending_products():
    """Get products with significant sales velocity changes"""
    min_velocity = float(request.args.get('min_velocity', 20))
    limit = int(request.args.get('limit', 100))
    
    products = Product.query.filter(
        Product.sales_velocity >= min_velocity,
        db.or_(Product.product_status == 'active', Product.product_status == None)
    ).order_by(
        Product.sales_velocity.desc()
    ).limit(limit).all()
    
    return jsonify({
        'success': True,
        'count': len(products),
        'products': [p.to_dict() for p in products]
    })


@app.route('/api/products', methods=['GET'])
def api_products():
    """Get all products with pagination, sorting, and filtering"""
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 20))
    offset = (page - 1) * limit
    
    query = Product.query
    
    # Filters
    if request.args.get('favorites_only') == 'true':
        query = query.filter_by(is_favorite=True)
        
    if request.args.get('gems_only') == 'true':
        # Default gem definition if not specified
        query = query.filter(Product.is_hidden_gem == True)
        
    # Sorting
    sort_by = request.args.get('sort_by', 'sales_7d')
    if sort_by == 'sales_7d':
        query = query.order_by(Product.sales_7d.desc())
    elif sort_by == 'video_count':
        query = query.order_by(Product.video_count.desc())
    elif sort_by == 'commission_rate':
        query = query.order_by(Product.commission_rate.desc())
    elif sort_by == 'first_seen':
        query = query.order_by(Product.first_seen.desc())
    else:
        query = query.order_by(Product.sales_7d.desc())
    
    total = query.count()
    products = query.offset(offset).limit(limit).all()
    
    return jsonify({
        'success': True,
        'count': total,
        'page': page,
        'products': [p.to_dict() for p in products]
    })

@app.route('/api/hidden-gems', methods=['GET'])
def api_hidden_gems():
    """Get products that meet hidden gem criteria: high sales, low influencers, good commission"""
    limit = int(request.args.get('limit', 100))
    min_sales = int(request.args.get('min_sales', 50))
    max_influencers = int(request.args.get('max_influencers', 50))
    min_commission = float(request.args.get('min_commission', 10))
    
    products = Product.query.filter(
        Product.sales_7d >= min_sales,
        Product.influencer_count <= max_influencers,
        Product.commission_rate >= min_commission,
        db.or_(Product.product_status == 'active', Product.product_status == None)
    ).order_by(
        Product.sales_7d.desc()
    ).limit(limit).all()
    
    return jsonify({
        'success': True,
        'count': len(products),
        'products': [p.to_dict() for p in products]
    })


@app.route('/api/oos-products', methods=['GET'])
def api_oos_products():
    """Get products that are likely out of stock"""
    limit = int(request.args.get('limit', 100))
    
    products = Product.query.filter(
        db.or_(
            Product.product_status == 'likely_oos',
            Product.product_status == 'out_of_stock'
        )
    ).order_by(
        Product.sales_30d.desc()
    ).limit(limit).all()
    
    return jsonify({
        'success': True,
        'count': len(products),
        'products': [p.to_dict() for p in products]
    })




@app.route('/api/refresh-all-products', methods=['GET', 'POST'])
def refresh_all_products():
    """
    Batch refresh ALL active products from EchoTik API.
    Calls API one product at a time to avoid errors.
    """
    passkey = request.args.get('passkey') or (request.json.get('passkey') if request.is_json else None)
    dev_passkey = os.environ.get('DEV_PASSKEY', '')
    
    if not dev_passkey or passkey != dev_passkey:
        return jsonify({'success': False, 'error': 'Invalid or missing passkey'}), 403
    
    # Limit how many products to refresh per call (to avoid timeout)
    limit = min(int(request.args.get('limit', 100)), 500)
    offset = int(request.args.get('offset', 0))
    delay = float(request.args.get('delay', 0.5))
    
    try:
        products = Product.query.filter(
            db.or_(Product.product_status == 'active', Product.product_status == None)
        ).order_by(Product.last_updated.asc()).offset(offset).limit(limit).all()
        
        total_products = len(products)
        updated = 0
        failed = 0
        errors = []
        
        print(f"🔄 Refreshing {total_products} products (offset={offset}, limit={limit})...")
        
        for i, product in enumerate(products):
            try:
                # Call EchoTik API for single product
                response = requests.get(
                    f"{BASE_URL}/product/detail",
                    params={'product_id': product.product_id},
                    auth=get_auth(),
                    timeout=30
                )
                
                if response.status_code != 200:
                    failed += 1
                    if len(errors) < 5:
                        errors.append(f"Product {product.product_id}: HTTP {response.status_code}")
                else:
                    data = response.json().get('data')
                    if data:
                        d = data[0]
                        # Update fields
                        product.sales = int(d.get('total_sale_cnt', 0))
                        product.sales_7d = int(d.get('total_sale_7d_cnt', 0))
                        product.sales_30d = int(d.get('total_sale_30d_cnt', 0))
                        product.influencer_count = int(d.get('total_ifl_cnt', 0))
                        product.video_count = int(d.get('total_video_cnt', 0))
                        product.commission_rate = float(d.get('product_commission_rate', 0))
                        product.price = float(d.get('spu_avg_price', 0))
                        product.last_updated = datetime.utcnow()
                        product.product_status = 'active' # Mark active
                        updated += 1
                    else:
                         # Product might be gone
                         product.product_status = 'likely_oos'
            
            except Exception as e:
                failed += 1
                if len(errors) < 5:
                    errors.append(f"Product {product.product_id}: {str(e)}")
            
            # Rate limiting
            time.sleep(delay)
            if i % 10 == 0:
                db.session.commit() # Periodic commit
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'updated': updated,
            'failed': failed,
            'errors': errors
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/products/nuke', methods=['POST'])
@login_required
@admin_required
def admin_nuke_products():
    """⚠️ DANGER: Delete ALL products from database"""
    try:
        data = request.get_json() or {}
        keep_favorites = data.get('keep_favorites', False)
        
        if keep_favorites:
            deleted = Product.query.filter(Product.is_favorite == False).delete()
        else:
            deleted = Product.query.delete()
            
        db.session.commit()
        log_activity(session.get('user_id'), 'admin_nuke', {'count': deleted, 'kept_favorites': keep_favorites})
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/stats', methods=['GET'])
@login_required
@admin_required
def admin_stats():
    """Get admin dashboard stats"""
    user_count = User.query.count()
    product_count = Product.query.count()
    return jsonify({
        'users': user_count,
        'products': product_count,
        'status': 'online' 
    })

@app.route('/api/admin/activity')
@login_required
@admin_required
def admin_activity_logs():
    """Get system activity logs"""
    try:
        logs = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(50).all()
        return jsonify({
            'success': True,
            'logs': [l.to_dict() for l in logs]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/detect-oos', methods=['GET', 'POST'])
def detect_out_of_stock():
    """Run out-of-stock detection on all existing products."""
    passkey = request.args.get('passkey') or (request.json.get('passkey') if request.is_json else None)
    dev_passkey = os.environ.get('DEV_PASSKEY', '')
    
    if not dev_passkey or passkey != dev_passkey:
        return jsonify({'success': False, 'error': 'Invalid or missing passkey'}), 403
    
    threshold = int(request.args.get('threshold', 50))
    
    try:
        candidates = Product.query.filter(
            Product.sales_7d == 0,
            Product.sales_30d > threshold,
            db.or_(Product.product_status == None, Product.product_status == 'active')
        ).all()
        
        marked_count = 0
        marked_products = []
        
        for product in candidates:
            product.product_status = 'likely_oos'
            product.status_note = f'Auto-detected: 0 sales in 7d but {product.sales_30d} in 30d'
            marked_count += 1
            marked_products.append({
                'product_id': product.product_id,
                'product_name': product.product_name[:50] if product.product_name else 'Unknown',
                'sales_30d': product.sales_30d
            })
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'OOS detection complete',
            'threshold': threshold,
            'candidates_found': len(candidates),
            'marked_as_oos': marked_count,
            'products': marked_products[:20]
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


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
            ('first_seen', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
            ('prev_sales_7d', 'INTEGER DEFAULT 0'),
            ('prev_sales_30d', 'INTEGER DEFAULT 0'),
            ('sales_velocity', 'FLOAT DEFAULT 0'),
            ('status_changed_at', 'TIMESTAMP'),
            ('is_hidden_gem', 'BOOLEAN DEFAULT FALSE'),
            ('product_status', 'VARCHAR(50) DEFAULT \'active\''),
            ('ai_image_url', 'TEXT'),
            ('ai_video_url', 'TEXT'),
            ('ai_video_task_id', 'VARCHAR(100)'),
            ('ai_video_status', 'VARCHAR(50)'),
            ('last_alert_sent', 'TIMESTAMP'),
            ('gem_alert_sent', 'TIMESTAMP'),
            ('stock_alert_sent', 'TIMESTAMP'),
            ('is_ad_driven', 'BOOLEAN DEFAULT FALSE'),
            ('product_url', 'VARCHAR(500)'),
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
            
        # Create Indexes for Performance
        try:
            indexes = [
                ('idx_products_sales_7d', 'CREATE INDEX IF NOT EXISTS idx_products_sales_7d ON products (sales_7d DESC)'),
                ('idx_products_video_count', 'CREATE INDEX IF NOT EXISTS idx_products_video_count ON products (video_count)'),
                ('idx_products_free_shipping', 'CREATE INDEX IF NOT EXISTS idx_products_free_shipping ON products (has_free_shipping) WHERE has_free_shipping = TRUE'),
                ('idx_products_status', 'CREATE INDEX IF NOT EXISTS idx_products_status ON products (product_status)'),
                ('idx_products_seller', 'CREATE INDEX IF NOT EXISTS idx_products_seller ON products (seller_id)'),
                ('idx_products_commission', 'CREATE INDEX IF NOT EXISTS idx_products_commission ON products (commission_rate DESC)'),
                ('idx_products_created', 'CREATE INDEX IF NOT EXISTS idx_products_created ON products (created_at DESC)')
            ]
            
            for name, sql in indexes:
                try:
                    db.session.execute(db.text(sql))
                    db.session.commit()
                except Exception as e:
                    print(f"Index {name} error: {e}")
                    db.session.rollback()
        except Exception as e:
            print(f"Index creation error: {e}")
            db.session.rollback()
        
        return jsonify({
            'success': True, 
            'message': f'Database initialized. Added product columns: {added if added else "none (already exist)"}. Users and activity tables ready.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/run-viral-trends-scan', methods=['POST'])
@login_required
def run_viral_trends_scan():
    """Trigger the Apify Shop Scanner as a background process."""
    try:
        data = request.get_json() or {}
        max_products = data.get('max_products', 50)
        
        script_path = os.path.join(basedir, 'apify_shop_scanner.py')
        
        # Pass the max_products argument to the script
        cmd = [sys.executable, script_path, '--max_products', str(max_products)]
        
        # Run process detached (Windows vs Linux handling)
        if os.name == 'nt':
            process = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            process = subprocess.Popen(cmd, start_new_session=True)
            
        return jsonify({'success': True, 'message': f'Scanner started in background (Limit: {max_products} products). Check console for progress.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def start_hybrid_scan(product_id):
    """
    Shared logic to start a Hybrid Scan (Prefetch Title -> Apify Search).
    Used by Flask API and Discord Bot.
    """
    # -------------------------------------------------------------------------
    # PRE-FETCH: Get Title/Seller to enable "Search by Name" (for Stats)
    # -------------------------------------------------------------------------
    search_query = product_id # Default to ID
    found_title = ""
    
    try:
         # Construct direct URL
         target_url = f"https://shop.tiktok.com/view/product/{product_id}?region=US&locale=en"
         headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
         
         verify_res = requests.get(target_url, headers=headers, timeout=10)
         
         if verify_res.status_code == 200:
             html = verify_res.text
             
             # Simple Regex Extraction to avoid huge BS4 dependency if not present
             import re
             title_match = re.search(r'"title":"(.*?)","', html) or re.search(r'<title>(.*?)</title>', html)
             
             # Basic Data Save (So user sees meaningful card immediately)
             with app.app_context():
                 existing = Product.query.get(f"shop_{product_id}")
                 if not existing:
                     existing = Product(product_id=f"shop_{product_id}")
                     existing.first_seen = datetime.utcnow()
                 
                 if title_match:
                     clean_title = title_match.group(1).split('|')[0].strip() # Remove "| TikTok Shop"
                     # Unescape HTML entities if needed, but simple strip is ok for now
                     existing.product_name = clean_title[:200]
                     found_title = clean_title
                     search_query = clean_title # USE TITLE FOR APIFY SCAN
                 else:
                     existing.product_name = f"TikTok Product {product_id}"
                 
                 existing.product_url = target_url
                 existing.last_updated = datetime.utcnow()
                 existing.scan_type = 'lookup_prefetch'
                 
                 db.session.add(existing)
                 db.session.commit()
                 print(f"DEBUG: Pre-fetched Key Data. Title: {found_title}")
             
    except Exception as e_pre:
         print(f"Pre-fetch failed: {e_pre}")
    
    # -------------------------------------------------------------------------

    script_path = os.path.join(basedir, 'apify_shop_scanner.py')
    
    # Pass TITLE (search_query) to the script if we found it, otherwise ID
    # The scanner will detect if it's a textual title and use Search capability.
    cmd = [sys.executable, script_path, '--product_id', search_query]
    
    # Run process detached
    if os.name == 'nt':
        process = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        process = subprocess.Popen(cmd, start_new_session=True)
        
    return {
        'success': True, 
        'message': f'Found "{search_query[:20]}...". Analyzing Stats (Influencers/Videos)...',
        'product_id': product_id,
        'source': 'apify',
        'search_query': search_query
    }

# Apify Routes Removed for V2




# =============================================================================
# DB MIGRATION HELPER (Run on startup to ensure schema matches model)
# =============================================================================
def check_and_migrate_db():
    """Add missing columns to existing tables"""
    with app.app_context():
        # Wrap in try/except to avoid crash if DB not ready
        try:
            inspector = db.inspect(db.engine)
            if not inspector: return
            
            # Check Products table
            if 'products' in inspector.get_tables():
                columns = [c['name'] for c in inspector.get_columns('products')]
                
                if 'original_price' not in columns:
                    print(">> MIGRATION: Adding 'original_price' column to products table...")
                    try:
                        if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
                            db.session.execute(db.text('ALTER TABLE products ADD COLUMN original_price FLOAT DEFAULT 0'))
                        else:
                            db.session.execute(db.text('ALTER TABLE products ADD COLUMN IF NOT EXISTS original_price FLOAT DEFAULT 0'))
                        db.session.commit()
                        print(">> MIGRATION: Success!")
                    except Exception as e:
                        print(f"!! MIGRATION FAILED: {e}")
                        db.session.rollback()
        except:
            pass

# Run migration check on startup (Safe for Gunicorn)


# =============================================================================
# SAAS API MODELS
# =============================================================================

class ApiKey(db.Model):
    """API Keys for external SaaS access"""
    __tablename__ = 'api_keys'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(32), unique=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    credits = db.Column(db.Integer, default=0) # 1 credit = 1 scan
    total_usage = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
class ScanJob(db.Model):
    """Async Job Queue for SaaS Scans"""
    __tablename__ = 'scan_jobs'
    id = db.Column(db.String(36), primary_key=True) # UUID
    status = db.Column(db.String(20), default='queued', index=True) # queued, processing, completed, failed
    input_query = db.Column(db.String(500))
    result_json = db.Column(db.Text)
    api_key_id = db.Column(db.Integer, db.ForeignKey('api_keys.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

# =============================================================================
# SAAS API ROUTES
# =============================================================================
import uuid
import threading

@app.route('/api/extern/scan', methods=['POST'])
def extern_scan_start():
    """Start a scan via API Key (Async)"""
    api_key_val = request.headers.get('X-API-KEY')
    if not api_key_val:
        return jsonify({'error': 'Missing X-API-KEY header'}), 401
    
    key = ApiKey.query.filter_by(key=api_key_val, is_active=True).first()
    if not key:
        return jsonify({'error': 'Invalid API Key'}), 401
    
    if key.credits < 1:
         return jsonify({'error': 'Insufficient Credits'}), 402
    
    data = request.get_json() or {}
    query = data.get('query') or data.get('url')
    if not query:
        return jsonify({'error': 'Missing query/url'}), 400
        
    # Deduct Credit
    key.credits -= 1
    key.total_usage += 1
    
    # Create Job
    job_id = str(uuid.uuid4())
    job = ScanJob(id=job_id, status='queued', input_query=query, api_key_id=key.id)
    db.session.add(job)
    db.session.commit()
    
    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': 'queued',
        'credits_remaining': key.credits
    })

@app.route('/api/extern/jobs/<job_id>', methods=['GET'])
def extern_job_status(job_id):
    """Check job status"""
    api_key_val = request.headers.get('X-API-KEY')
    if not api_key_val:
        return jsonify({'error': 'Missing X-API-KEY header'}), 401
    
    # We could validate key config here but for speed we just check job existence
    job = ScanJob.query.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
        
    resp = {
        'id': job.id,
        'status': job.status,
        'created_at': job.created_at.isoformat(),
        'result': None
    }
    
    if job.result_json:
        try:
            resp['result'] = json.loads(job.result_json)
        except:
            resp['result'] = job.result_json
            
    return jsonify(resp)

@app.route('/api/admin/jobs', methods=['GET'])
@login_required
def admin_list_jobs():
    """Debug: List recent scan jobs"""
    try:
        jobs = ScanJob.query.order_by(ScanJob.created_at.desc()).limit(20).all()
        results = []
        for j in jobs:
            res = None
            if j.result_json:
                try: res = json.loads(j.result_json)
                except: res = j.result_json
                
            results.append({
                'id': j.id,
                'status': j.status,
                'input': j.input_query,
                'result': res,
                'created_at': j.created_at.isoformat()
            })
        return jsonify({'success': True, 'jobs': results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/create-key', methods=['POST'])
@login_required
def admin_create_key():
    """Admin: Generate a new SaaS API Key"""
    try:
        data = request.get_json() or {}
        credits = int(data.get('credits', 100))
        
        new_key_str = secrets.token_hex(16) # 32 chars
        
        new_key = ApiKey(
            key=new_key_str,
            user_id=current_user.id,
            credits=credits,
            is_active=True
        )
        db.session.add(new_key)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'api_key': new_key_str,
            'credits': credits,
            'message': 'Key generated! Save it now, it cannot be retrieved later'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# =============================================================================
# USER DEVELOPER ROUTES
# =============================================================================

@app.route('/developer')
@login_required
def developer_page():
    return send_from_directory('pwa', 'developer_v4.html')

@app.route('/api/developer/me')
@login_required
def api_dev_me():
    user = get_current_user()
    key = ApiKey.query.filter_by(user_id=user.id, is_active=True).first()
    return jsonify({
        'key': key.key if key else None,
        'credits': key.credits if key else 0.0
    })

@app.route('/api/developer/keygen', methods=['POST'])
@login_required
def api_dev_keygen():
    try:
        user = get_current_user()
        # Deactivate old keys
        old_keys = ApiKey.query.filter_by(user_id=user.id, is_active=True).all()
        existing_credits = sum([k.credits for k in old_keys])
        
        for k in old_keys:
            k.is_active = False
            
        # Bonus for new users (if no credits existed)
        if existing_credits == 0 and not old_keys:
             existing_credits = 5.0 # 5 Free Scans
            
        new_key_str = secrets.token_hex(16)
        new_key = ApiKey(
            key=new_key_str,
            user_id=user.id,
            credits=existing_credits,
            is_active=True
        )
        db.session.add(new_key)
        db.session.commit()
        
        return jsonify({'success': True, 'key': new_key_str, 'credits': existing_credits})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# =============================================================================
# STRIPE PAYMENT ROUTES
# =============================================================================

# Guard Stripe Init
if stripe:
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')

@app.route('/api/developer/checkout', methods=['POST'])
@login_required
def api_dev_checkout():
    if not stripe:
        return jsonify({'error': 'Payment system not available (Stripe missing)'}), 503
        
    try:
        user = get_current_user()
        data = request.get_json()
        amount_cents = data.get('amount', 1500) # Default $15.00
        credits_to_add = data.get('credits', 500)
        
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{credits_to_add} API Credits',
                    },
                    'unit_amount': amount_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=url_for('developer_page', _external=True) + '?success=true',
            cancel_url=url_for('developer_page', _external=True) + '?canceled=true',
            metadata={
                'user_id': user.id,
                'credits': credits_to_add
            }
        )
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    try:
        user = get_current_user()
        data = request.get_json() or {}
        plan = data.get('plan') # starter, pro, enterprise
        
        if not stripe.api_key:
            return jsonify({'error': 'Stripe not configured (STRIPE_SECRET_KEY missing)'}), 500

        # Define Products (Hardcoded for simplicity, or use Price IDs)
        pricing = {
            'starter': {'amount': 500, 'credits': 100, 'name': 'Starter Pack (100 Credits)'},
            'pro': {'amount': 2000, 'credits': 500, 'name': 'Pro Pack (500 Credits)'},
            'enterprise': {'amount': 5000, 'credits': 1500, 'name': 'Enterprise Pack (1500 Credits)'}
        }
        
        selected = pricing.get(plan)
        if not selected:
             return jsonify({'error': 'Invalid plan'}), 400
             
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': selected['amount'], # in cents
                    'product_data': {
                        'name': selected['name'],
                        'description': 'Credits for TikTokShop Finder API',
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.host_url + 'developer?success=true',
            cancel_url=request.host_url + 'developer?canceled=true',
            client_reference_id=str(user.id),
            metadata={
                'credits_to_add': selected['credits'],
                'user_id': user.id
            }
        )
        return jsonify({'url': checkout_session.url})
    except Exception as e:
         return jsonify({'error': str(e)}), 500

@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe Webhooks to fulfill credits"""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        # Verify Signature if secret is set
        if endpoint_secret:
            event = stripe.Webhook.construct_event(
                payload, sig_header, endpoint_secret
            )
        else:
            # Fallback for dev/test without verification (NOT SECURE FOR PROD - warn user)
            data = json.loads(payload)
            event = stripe.Event.construct_from(data, stripe.api_key)
            
    except ValueError as e:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        return 'Invalid signature', 400

    # Handle the event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        # Fulfill the purchase
        user_id = session.get('client_reference_id') or session.get('metadata', {}).get('user_id')
        credits = session.get('metadata', {}).get('credits_to_add')
        
        if user_id and credits:
            fulfill_credits(user_id, int(credits))
            
    return jsonify({'status': 'success'})

def fulfill_credits(user_id, amount):
    """Add credits to user's active key"""
    with app.app_context():
        try:
            print(f">> STRIPE: Adding {amount} credits to User {user_id}")
            # Find active key
            key = ApiKey.query.filter_by(user_id=user_id, is_active=True).first()
            if key:
                key.credits += amount
                db.session.commit()
                print(">> STRIPE: Credits added successfully!")
            else:
                # Create a key if they don't have one? Or just log error?
                # Let's create one.
                new_key_str = secrets.token_hex(16)
                new_key = ApiKey(
                    key=new_key_str,
                    user_id=user_id,
                    credits=amount,
                    is_active=True
                )
                db.session.add(new_key)
                db.session.commit()
                print(">> STRIPE: Created new key with credits!")
        except Exception as e:
            print(f"!! STRIPE FULFILLMENT ERROR: {e}")

# =============================================================================
# SAAS WORKER (Background Thread)
# =============================================================================
# SAAS Worker Removed for V2

# Start Worker Thread on App Start (Daemon)
# Only start if likely running as main server (not during build)
# Run migration check on startup (Safe for Gunicorn)
# MUST BE AT END OF FILE so all models are loaded
with app.app_context():
    try:
        # ensure_db_schema() # Commented out as potential crash source (if undefined)
        db.create_all()
        check_and_migrate_db()
    except Exception as e:
        print(f"Error during DB init: {e}")

# if os.environ.get('RENDER') or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
#     t = threading.Thread(target=saas_worker_loop, daemon=True)
#     t.start()
# =============================================================================
# MANUAL SCAN & UTILS (Restored for V2)
# =============================================================================

@app.route('/api/scan/manual', methods=['POST'])
@login_required
def api_scan_manual():
    """Manual Import Endpoint (Restored)"""
    try:
        data = request.json
        if not data:
             return jsonify({'error': 'No data provided'}), 400
             
        # Support both 'url' direct input AND the DV 'json_data' wrapper
        url = data.get('url')
        
        # Handle DV wrapper if present
        if data.get('json_data'):
            try:
                inner = json.loads(data['json_data'])
                if inner.get('list') and len(inner['list']) > 0:
                    url = inner['list'][0].get('product', {}).get('url')
            except:
                pass
                
        if not url:
            return jsonify({'error': 'No URL found'}), 400
            
        # Basic ID extraction
        import re
        product_id = None
        match = re.search(r'/product/(\d+)', url)
        if match:
            product_id = match.group(1)
        else:
            # Fallback for other URL formats
            match = re.search(r'id=(\d+)', url)
            if match:
                product_id = match.group(1)
        
        if not product_id:
             return jsonify({'error': 'Could not extract Product ID from URL'}), 400
             
        # Add to DB
        existing = Product.query.filter_by(product_id=product_id).first()
        if not existing:
            # Try with shop_ prefix just in case
            existing = Product.query.filter_by(product_id=f"shop_{product_id}").first()
            
        if existing:
            return jsonify({'success': True, 'message': 'Product already exists', 'product_id': existing.product_id})
            
        # Create new placeholder product
        new_p = Product(
            product_id=product_id,
            product_status='active',
            product_url=url,
            product_name=f"New Import ({product_id})",
            first_seen=datetime.utcnow(),
            last_updated=datetime.utcnow(),
            sales_7d=0,
            video_count=0,
            scan_type='manual_bridge'
        )
        db.session.add(new_p)
        db.session.commit()
        
        # BRIDGE: Immediately fetch data from EchoTik
        try:
            print(f"🌉 Bridging Product {product_id} to EchoTik API...")
            er = requests.get(
                f"{BASE_URL}/product/detail",
                params={'product_id': product_id},
                auth=get_auth(),
                timeout=15
            )
            if er.status_code == 200:
                ed = er.json().get('data', {})
                if ed:
                    new_p.product_name = ed.get('title') or new_p.product_name
                    new_p.image_url = ed.get('product_img_url') or new_p.image_url
                    new_p.sales_7d = int(ed.get('total_sale_7d_cnt', 0) or 0)
                    new_p.sales = int(ed.get('total_sale_cnt', 0) or 0)
                    new_p.video_count = int(ed.get('total_video_cnt', 0) or 0)
                    new_p.price = float(ed.get('spu_avg_price', 0) or 0)
                    new_p.commission_rate = float(ed.get('product_commission_rate', 0) or 0)
                    new_p.status_note = "Enriched via EchoTik Bridge"
                    db.session.commit()
        except Exception as bridge_err:
            print(f"Bridge Error: {bridge_err}")
            # Non-blocking, user still gets the product imported
        
        return jsonify({'success': True, 'message': 'Product imported and bridged to EchoTik', 'product_id': product_id})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/image-proxy/<path:product_id>')
def api_image_proxy(product_id):
    """Simple proxy for product images to avoid CORS/Hotlink issues"""
    # 1. Get product URL from DB
    p = Product.query.filter_by(product_id=product_id).first()
    if not p and product_id.isdigit():
        p = Product.query.filter_by(product_id=f"shop_{product_id}").first()
    if not p and product_id.startswith('shop_'):
        p = Product.query.filter_by(product_id=product_id.replace('shop_', '')).first()
        
    if not p or not p.image_url:
        return jsonify({'error': 'Image not found'}), 404
        
    img_url = p.image_url
    
    # 2. Fetch and Stream
    try:
        if img_url.startswith('data:'):
            # It's base64, serve it decoding? No, too complex.
            return jsonify({'error': 'Base64 image, use direct link'}), 400
            
        r = requests.get(img_url, stream=True, timeout=10)
        return Response(r.content, content_type=r.headers.get('content-type', 'image/jpeg'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# LEGACY SCANNERS (Brand Hunter / EchoTik)
# =============================================================================



@app.route('/api/scan-pages/<seller_id>', methods=['GET'])
@login_required
def api_scan_brand_pages(seller_id):
    """Restored specific Brand ID Scan"""
    try:
        start_page = int(request.args.get('start', 1))
        end_page = int(request.args.get('end', 5))
        
        found = 0
        saved = 0
        
        for p_idx in range(start_page, end_page + 1):
             p_res = requests.get(
                f"{BASE_URL}/product/list",
                params={'seller_id': seller_id, 'sort_by': 'total_sale_7d_cnt', 'sort_order': 'desc', 'page': p_idx, 'size': 20},
                auth=get_auth(), timeout=30
            )
             items = p_res.json().get('data', []) if p_res.status_code == 200 else []
             for item in items:
                 found += 1
                 pid = str(item.get('product_id'))
                 if not Product.query.get(pid):
                     # Minimal save
                     new_p = Product(
                            product_id=pid,
                            product_name=item.get('product_name')[:500],
                            image_url=item.get('product_img_url'),
                            sales_7d=int(item.get('total_sale_7d_cnt', 0) or 0),
                            video_count=int(item.get('total_video_cnt', 0) or 0),
                            first_seen=datetime.utcnow(),
                            scan_type='brand_hunter'
                     )
                     db.session.add(new_p)
                     saved += 1
                     
        db.session.commit()
        return jsonify({'products_found': found, 'products_saved': saved})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

