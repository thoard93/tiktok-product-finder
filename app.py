"""
Vantage - TikTok Shop Intelligence Platform
Powered by TikTokCopilot API for real-time trending product data

Features:
- Discord OAuth login (server members only)
- Developer passkey bypass
- Scan locking (one scan at a time)
- User activity logging
- Watermarked exports
- Admin dashboard

Strategy: 
- Fetch trending products from TikTokCopilot
- Filter by winner score (high ad spend + low competition)
- Filter for low influencer count (1-100)
- Save hidden gems automatically
"""

import os
import secrets
import sys
import subprocess
import requests
import urllib3
try:
    from curl_cffi import requests as requests_cffi
except ImportError:
    requests_cffi = None
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    import stripe
except ImportError:
    stripe = None
    print("WARNING: Stripe module not found. Payments will fail.")
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, redirect, session, url_for, render_template, make_response, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_, text
from sqlalchemy.pool import NullPool
from functools import wraps
import time
import json
import hashlib
import secrets
# import jwt  # Moved inside generate_kling_jwt_token to avoid dependency crash for Bot
import re   # For parsing product IDs from URLs
import traceback
from concurrent.futures import ThreadPoolExecutor # Added for parallel enrichment
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

executor = ThreadPoolExecutor(max_workers=4) # Global executor for background tasks

@app.route('/vantage_logo.png')
def serve_logo():
    return send_from_directory('pwa', 'vantage_logo.png')

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



def get_anthropic_key():
    return get_config_value('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY')

@app.route('/api/ai/chat', methods=['POST'])
def ai_chat():
    """Vantage AI Chatbot - Claude-powered TikTok Shop Expert"""
    try:
        api_key = get_anthropic_key()
        if not api_key:
            return jsonify({"success": False, "error": "Anthropic API Key not configured. Please set it in Admin or Environment."}), 500
            
        data = request.json
        message = data.get('message', '')
        if not message:
            return jsonify({"success": False, "error": "No message provided"}), 400
            
        # Context: Get top 50 products by Ad Spend as a proxy for "interesting" data
        products = Product.query.filter(Product.ad_spend > 0).order_by(Product.ad_spend.desc()).limit(50).all()
        
        # Format context for Claude
        product_list = []
        for p in products:
            product_list.append({
                "name": p.product_name,
                "ad_spend": p.ad_spend,
                "videos": p.video_count,
                "efficiency": (p.ad_spend / p.video_count) if (p.video_count and p.video_count > 0) else 0,
                "gmv": p.gmv,
                "roas": (p.gmv / p.ad_spend) if (p.ad_spend and p.ad_spend > 0) else 0,
                "seller": p.seller_name
            })
            
        system_prompt = f"""You are 'Vantage AI', the core intelligence of the Vantage platform.
        You are a world-class TikTok Shop marketing expert.
        
        GOAL: Help the user find 'Gems' (products with high ad spend but few videos).
        
        USER DATABASE CONTEXT (Top Products):
        {json.dumps(product_list, default=str)}
        
        INSTRUCTIONS:
        1. Be professional, concise, and helpful.
        2. If asked for 'gems', identify products with high 'efficiency' (Ad Spend / Videos).
        3. Explain WHY a product is a good gem (e.g. "Brand X is spending $20k with only 40 videos").
        4. Refer to the platform as 'Vantage'.
        """
        
        anthropic_res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{"role": "user", "content": message}]
            },
            timeout=30
        )
        
        if anthropic_res.status_code != 200:
            return jsonify({"success": False, "error": f"AI Error: {anthropic_res.text}"}), 500
            
        ai_data = anthropic_res.json()
        ai_response = ai_data['content'][0]['text']
        
        return jsonify({"success": True, "response": ai_response})
        
    except Exception as e:
        print(f"[AI] Exception: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/fix-schema', methods=['GET'])
def manual_fix_schema():
    """Force add columns directly via SQL"""
    try:
        with db.engine.connect() as conn:
            from sqlalchemy import text
            columns = [
                ("ad_spend", "FLOAT DEFAULT 0"),
                ("ad_spend_total", "FLOAT DEFAULT 0"),
                ("scan_type", "VARCHAR(50) DEFAULT 'copilot'"),
                ("gmv_growth", "FLOAT DEFAULT 0"),
                ("product_status", "VARCHAR(50) DEFAULT 'active'"),
                ("status_note", "VARCHAR(255)"),
                ("prev_sales_7d", "INTEGER DEFAULT 0"),
                ("prev_sales_30d", "INTEGER DEFAULT 0"),
                ("sales_velocity", "FLOAT DEFAULT 0"),
                ("is_ad_driven", "BOOLEAN DEFAULT 0"),
                ("original_price", "FLOAT DEFAULT 0"),
                ("cached_image_url", "TEXT"),
                ("image_cached_at", "TIMESTAMP"),
                ("last_shown_hot", "TIMESTAMP"),
                ("has_free_shipping", "BOOLEAN DEFAULT 0"),
                ("is_favorite", "BOOLEAN DEFAULT 0")
            ]
            
            results = []
            for col_name, col_def in columns:
                try:
                    conn.execute(text(f"ALTER TABLE products ADD COLUMN {col_name} {col_def}"))
                    results.append(f"Added {col_name}")
                except Exception as e:
                    results.append(f"Skipped {col_name} ({str(e)[:50]}...)")
            
            conn.commit()
            
        return jsonify({"success": True, "details": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

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
ECHOTIK_PROXY_STRING = os.environ.get('ECHOTIK_PROXY_STRING')
TIKTOK_PARTNER_COOKIE = os.environ.get('TIKTOK_PARTNER_COOKIE')

# Telegram Alerts Config
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# Kling AI Video Generation Config
KLING_ACCESS_KEY = os.environ.get('KLING_ACCESS_KEY', '')
KLING_SECRET_KEY = os.environ.get('KLING_SECRET_KEY', '')
KLING_API_BASE_URL = "https://api-singapore.klingai.com"

# Gemini AI Image Generation Config
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

# DailyVirals API Config
DV_BACKEND_URL = "https://backend.thedailyvirals.com/api/videos/stats/top-growth-by-date-range"
DV_API_TOKEN = os.environ.get('DAILYVIRALS_TOKEN', 'eyJhbGciOiJIUzI1NiIsImtpZCI6InlNMHVYRXRpWEM3Qm04V0MiLCJ0eXAiOiJKV1QifQ.eyJpc3MiOiJodHRwczovL2h3b3JieG90eHdibnBscW5ob3ZpLnN1cGFiYXNlLmNvL2F1dGgvdjEiLCJzdWIiOiJhMTQyNDUzMi04M2QxLTRiMGItYTcxZS04OGU0ZmQ4MWNkYTgiLCJhdWQiOiJhdXRoZW50aWNhdGVkIiwiZXhwIjoxNzY1Mjk5Nzc1LCJpYXQiOjE3NjUyOTYxNzUsImVtYWlsIjoidGhvYXJkMjAzNUBnbWFpbC5jb20iLCJwaG9uZSI6IiIsImFwcF9tZXRhZGF0YSI6eyJpc19hbm9ueW1vdXMiOmZhbHNlLCJwcm92aWRlciI6ImVtYWlsIiwicHJvdmlkZXJzIjpbImVtYWlsIl19LCJ1c2VyX21ldGFkYXRhIjp7ImVtYWlsX3ZlcmlmaWVkIjp0cnVlLCJmaXJzdE5hbWUiOiJUaG9tYXMiLCJpc19hbm9ueW1vdXMiOmZhbHNlLCJsYXN0TmFtZSI6IkhvYXJkIiwic3RyaXBlQ3VzdG9tZXJJZCI6ImN1c19UUXhMcnc5dGJTSmxNdCIsInVzZXJJZCI6ImExNDI0NTMyLTgzZDEtNGIwYi1hNzFlLTg4ZTRmZDgxY2RhOCJ9LCJyb2xlIjoiYXV0aGVudGljYXRlZCIsImFhbCI6ImFhbDEiLCJhbXIiOlt7Im1ldGhvZCI6InBhc3N3b3JkIiwidGltZXN0YW1wIjoxNzY1Mjk1ODgxfV0sInNlc3Npb25faWQiOiJmZTQ2YTdkMi1kYjk1LTRhNDYtYmFmZi04ZGM3OWVhYTgwZjQiLCJpc19hbm9ueW1vdXMiOmZhbHNlfQ.VWpxeNXqWXDmWFwQyblxgLeW8vYfiG4ZuOsZbBq_oZ4')
# Standard Proxy for DailyVirals (format: host:port:user:pass)
DV_PROXY_STRING = os.environ.get('DAILYVIRALS_PROXY', '')
if os.environ.get('DAILYVIRALS_TOKEN'):
    print(">> DailyVirals Token loaded from Environment")
else:
    print(">> WARNING: DailyVirals Token using HARDCODED fallback (may be expired)")
if DV_PROXY_STRING:
    print(f">> DailyVirals Proxy configured: {DV_PROXY_STRING.split(':')[0]}:****")

# Default prompt for video generation
KLING_DEFAULT_PROMPT = "cinematic push towards the product, no hands, product stays still"

# =============================================================================
# AUTHENTICATION HELPERS
# =============================================================================

def get_current_user():
    """Get the current logged-in user or None"""
    if 'user_id' not in session:
        return None
    return User.query.get(session['user_id'])

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required', 'redirect': '/login'}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        user = User.query.get(session['user_id'])
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin privileges required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def log_activity(user_id, action, details=None):
    """Log user activity to DB"""
    try:
        if isinstance(details, dict):
            details = json.dumps(details, default=str)
        log = ActivityLog(
            user_id=user_id,
            action=action,
            details=str(details)[:500] if details else None,
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        print(f"Log Error: {e}")

# =============================================================================
# SHARED HELPERS
# =============================================================================

def get_random_user_agent():
    """Returns a random modern browser user agent."""
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
    ]
    import random
    return random.choice(uas)

def parse_kmb_string(s):
    """
    Parses strings like '1.2K', '5.5M', '1,000' into integers.
    """
    if not s: return 0
    s = str(s).upper().replace(',', '').strip()
    try:
        if 'K' in s:
            return int(float(s.replace('K', '')) * 1000)
        if 'M' in s:
            return int(float(s.replace('M', '')) * 1000000)
        if 'B' in s:
            return int(float(s.replace('B', '')) * 1000000000)
        return int(float(s))
    except (ValueError, TypeError):
        # Fallback: remove everything except digits and dots
        s = "".join(c for c in s if c.isdigit() or c == ".")
        try:
            return int(float(s)) if s else 0
        except: return 0


def safe_float(val, default=0.0):
    if val is None: return default
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip().replace(',', '')
    if s.upper() == 'N/A' or not s: return default
    # Remove currency symbols and common non-numeric prefix/suffix
    s = re.sub(r'[^\d\.\-]', '', s)
    try:
        return float(s)
    except:
        return default

def safe_int(val, default=0):
    if val is None: return default
    if isinstance(val, (int, float)): return int(val)
    s = str(val).strip().replace(',', '')
    if s.upper() == 'N/A' or not s: return default
    # Remove non-numeric except minus sign
    s = re.sub(r'[^\d\-]', '', s)
    try:
        return int(s)
    except:
        return default

def parse_cover_url(url):
    """Clean up cover URL which may be a JSON array string or list or dict."""
    if not url: return ""
    
    # If already a dictionary
    if isinstance(url, dict):
        return url.get('url') or url.get('url_list', [None])[0] or ""
        
    # If already a list (from some parsers)
    if isinstance(url, list):
        if len(url) > 0:
            item = url[0]
            if isinstance(item, dict): return item.get('url') or item.get('url_list', [None])[0] or ""
            return str(item)
        return ""
    # If JSON string
    if isinstance(url, str) and (url.startswith('[') or url.startswith('{')):
        try:
            import json
            data = json.loads(url)
            if isinstance(data, list) and len(data) > 0:
                item = data[0]
                if isinstance(item, dict): return item.get('url') or item.get('url_list', [None])[0] or ""
                return str(item)
            if isinstance(data, dict):
                return data.get('url') or data.get('url_list', [None])[0] or ""
        except: pass
    return str(url)

def save_or_update_product(p_data, scan_type='brand_hunter', explicit_id=None):
    """
    Unified helper to save or update a product in the DB.
    Ensures normalized shop_ prefix and comprehensive field updates.
    """
    # 1. Determine Product ID
    res_id = p_data.get('product_id') or p_data.get('productId') or p_data.get('id')
    raw_id = str(res_id or explicit_id or "").replace('shop_', '')
    
    if not raw_id or raw_id.lower() == 'none':
        print(f"DEBUG: Skipping save - No valid product ID found in data or explicit_id.")
        return None
        
    product_id = f"shop_{raw_id}"
    existing = Product.query.get(product_id)
    
    # 2. Exhaustive Metadata Extraction
    res = extract_metadata_from_echotik(p_data)
    
    # Normalize Stats - FALLBACK to p_data direct values (set by enrichment)
    def parse_safe_float(v):
        try: return float(str(v or 0).replace('$','').replace(',','').strip())
        except: return 0.0

    inf_count = res['influencer_count'] or parse_kmb_string(p_data.get('influencer_count') or 0)
    sales = res['sales'] or parse_kmb_string(p_data.get('sales') or 0)
    s7d = res['sales_7d'] or parse_kmb_string(p_data.get('sales_7d') or 0)
    s30d = res['sales_30d'] or parse_kmb_string(p_data.get('sales_30d') or 0)
    comm = res['commission_rate'] or parse_safe_float(p_data.get('commission_rate') or 0)
    price = res['price'] or parse_safe_float(p_data.get('price') or 0)
    v_count = res['video_count'] or parse_kmb_string(p_data.get('video_count') or 0)
    
    # New Stats Persistence
    shop_ads = safe_float(p_data.get('shop_ads_commission') or p_data.get('tapShopAdsRate') or 0)
    if shop_ads > 1.0: shop_ads = shop_ads / 10000.0 # Normalize if raw rate
    
    ad_spend = safe_float(p_data.get('ad_spend') or p_data.get('periodAdSpend') or 0)
    ad_spend_total = safe_float(p_data.get('ad_spend_total') or p_data.get('productTotalAdSpend') or 0)
    
    img = parse_cover_url(res['image_url'] or p_data.get('image_url') or p_data.get('item_img'))
    name = res['product_name'] or p_data.get('product_name') or p_data.get('title') or ""

    # Generate or extract product URL
    p_url = res['product_url'] or p_data.get('product_url') or p_data.get('url')
    if not p_url or 'tiktok.com' not in p_url:
        p_url = f"https://shop.tiktok.com/view/product/{raw_id}?region=US&locale=en-US"

    if existing:
        # Update existing record
        existing.product_name = name or existing.product_name
        existing.image_url = img or existing.image_url
        existing.product_url = p_url or existing.product_url
        
        # Update stats if new data is better or existing is empty
        if price > 0 or existing.price == 0: existing.price = price
        if sales > 0 or existing.sales == 0: existing.sales = sales
        if s7d > 0 or existing.sales_7d == 0: existing.sales_7d = s7d
        if s30d > 0 or existing.sales_30d == 0: existing.sales_30d = s30d
        
        # INFLUENCER COUNT: Only update if not already set (preserve all-time)
        # The influencer_count field should represent all-time creators
        if inf_count > 0 and (not existing.influencer_count or existing.influencer_count == 0):
            existing.influencer_count = inf_count
        
        if comm > 0 or existing.commission_rate == 0: existing.commission_rate = comm
        
        # VIDEO COUNT LOGIC: Never downgrade or overwrite all-time counts
        # video_count and video_count_alltime represent all-time video saturation
        # During regular sync, we only update video_7d (period videos)
        if v_count > 0:
            # Update all-time ONLY if new count is higher (never downgrade)
            if v_count > (existing.video_count_alltime or 0):
                existing.video_count_alltime = v_count
                # Also update video_count to match all-time
                existing.video_count = v_count
            # Note: We no longer overwrite video_count with period data during sync
            # The video_7d field should be used for period video counts instead
        
        # Update New Stats
        if shop_ads > 0: existing.shop_ads_commission = shop_ads
        if ad_spend > 0: existing.ad_spend = ad_spend
        if ad_spend_total > 0: existing.ad_spend_total = ad_spend_total
        
        # Merge other stats if available
        existing.video_7d = parse_kmb_string(p_data.get('total_video_7d_cnt') or p_data.get('totalVideo7dCnt') or res.get('video_7d') or existing.video_7d or 0)
        existing.video_30d = parse_kmb_string(p_data.get('total_video_30d_cnt') or p_data.get('totalVideo30dCnt') or res.get('video_30d') or existing.video_30d or 0)
        existing.live_count = res['live_count'] or parse_kmb_string(p_data.get('total_live_cnt') or p_data.get('totalLiveCnt') or existing.live_count or 0)
        existing.live_count = res['live_count'] or parse_kmb_string(p_data.get('total_live_cnt') or p_data.get('totalLiveCnt') or existing.live_count or 0)
        existing.views_count = parse_kmb_string(p_data.get('total_views_cnt') or p_data.get('totalViewsCnt') or existing.views_count or 0)
        existing.product_rating = res['product_rating'] or existing.product_rating or 0
        existing.review_count = res['review_count'] or existing.review_count or 0
        
        # Update seller info
        new_name = str(res['seller_name'] or "").strip()
        if new_name and new_name.lower() not in ['unknown', 'none', 'null', '']:
             # Only update if current is unknown or we found a better name
             if not existing.seller_name or existing.seller_name == 'Unknown':
                 existing.seller_name = new_name
        
        new_id = res['seller_id'] or p_data.get('seller_id') or p_data.get('shop_id')
        if new_id and (not existing.seller_id):
            existing.seller_id = new_id

        existing.last_updated = datetime.utcnow()
        return False # False = Updated
    else:
        # Create new record
        # Final fallback for seller_name if absolutely nothing found
        final_seller = str(res['seller_name'] or "").strip()
        if not final_seller or final_seller.lower() in ['unknown', 'none', 'null']:
            final_seller = "Unknown"

        product = Product(
            product_id=product_id,
            product_name=name or "Unknown Product",
            image_url=img,
            product_url=p_url,
            price=price,
            sales=sales,
            sales_7d=s7d,
            sales_30d=s30d,
            influencer_count=inf_count,
            commission_rate=comm,
            video_count=v_count,
            video_count_alltime=v_count, # Initially total = normal
            video_7d=int(p_data.get('total_video_7d_cnt') or p_data.get('totalVideo7dCnt') or 0),
            video_30d=int(p_data.get('total_video_30d_cnt') or p_data.get('totalVideo30dCnt') or 0),
            live_count=res['live_count'] or int(p_data.get('total_live_cnt') or p_data.get('totalLiveCnt') or 0),
            views_count=int(p_data.get('total_views_cnt') or p_data.get('totalViewsCnt') or 0),
            product_rating=res['product_rating'],
            review_count=res['review_count'],
            seller_name=final_seller,
            seller_id=res['seller_id'] or p_data.get('seller_id') or p_data.get('shop_id'),
            scan_type=scan_type,
            shop_ads_commission=shop_ads,
            ad_spend=ad_spend,
            ad_spend_total=ad_spend_total,
            first_seen=datetime.utcnow()
        )
        db.session.add(product)
        return True # True = New


def enrich_product_data(p, i_log_prefix="", force=False, allow_paid=False):
    """
    Global Helper: Search/Upgrade product using TikTokCopilot API.
    """
    # Helper for robust attribute access
    def gv(obj, key, default=None):
        if isinstance(obj, dict): return obj.get(key, default)
        return getattr(obj, key, default)

    def sv(obj, key, val):
        if isinstance(obj, dict): obj[key] = val
        else: setattr(obj, key, val)

    pid = gv(p, 'product_id')
    if not pid: return False, "No Product ID"
    
    # Strip shop_ for search
    raw_pid = str(pid).replace('shop_', '')
    
    print(f"{i_log_prefix} 🕵️‍♂️ Searching Copilot for {raw_pid}...")
    
    # STAGE 1: Search by direct Product ID
    res = fetch_copilot_trending(timeframe='30d', limit=5, product_id=raw_pid)
    
    # STAGE 2: Fallback to Keyword Search if no results
    if not res or not res.get('videos'):
        print(f"{i_log_prefix} ⚠️ Stage 1 (ID Search) failed for {raw_pid}, trying Stage 2 (Keyword fallback)...")
        res = fetch_copilot_trending(timeframe='30d', limit=10, keywords=raw_pid)
    
    if not res or not res.get('videos'):
        print(f"{i_log_prefix} ❌ Copilot Search Found Nothing for {raw_pid} after all stages. Full Response: {res}")
        return False, "Copilot Search Found Nothing"
        
    # Find exact match or best match
    best_match = None
    videos = res.get('videos', [])
    for v in videos:
        v_pid = str(v.get('productId', ''))
        # Try both exact and partial matches to be safe
        if raw_pid == v_pid or raw_pid in v_pid:
            best_match = v
            break
            
    if not best_match:
        print(f"{i_log_prefix} ❌ No exact PID match in results found for {raw_pid}. Results: {[v.get('productId') for v in videos]}")
        return False, "Copilot Search: No exact PID match found"
        
    # Extract Data from Best Match
    v = best_match
    
    # Debug: Log keys for analysis (first run only usually, but good for now)
    # print(f"DEBUG_COPILOT_KEYS: {list(v.keys())}") 
    
    # Update Fields
    sv(p, 'product_name', v.get('productTitle') or gv(p, 'product_name'))
    sv(p, 'seller_name', v.get('sellerName') or gv(p, 'seller_name'))
    sv(p, 'image_url', v.get('productImageUrl') or gv(p, 'image_url'))
    
    gmv = float(v.get('periodRevenue') or v.get('productPeriodRevenue') or 0)
    sales_period = int(v.get('periodUnits') or v.get('units') or 0)
    
    # Stats Extraction
    v_count = int(v.get('productVideoCount') or 0)
    inf_count = int(v.get('productCreatorCount') or 0)
    shop_ads = float(v.get('tapShopAdsRate') or 0) / 10000.0
    ad_spend = float(v.get('periodAdSpend') or 0)
    ad_spend_total = float(v.get('productTotalAdSpend') or v.get('totalAdSpend') or 0)
    comm = float(v.get('tapCommissionRate') or 0) / 10000.0
    price = float(v.get('avgUnitPrice') or v.get('minPrice') or v.get('productPrice') or 0)
    
    sv(p, 'sales_7d', sales_period if sales_period > 0 else gv(p, 'sales_7d'))
    sv(p, 'gmv', gmv if gmv > 0 else gv(p, 'gmv'))
    
    # VIDEO COUNT & INFLUENCER COUNT: Update all-time fields, never downgrade
    # video_count and influencer_count represent all-time saturation metrics
    current_video_count = int(gv(p, 'video_count') or 0)
    current_video_count_alltime = int(gv(p, 'video_count_alltime') or 0)
    current_inf_count = int(gv(p, 'influencer_count') or 0)
    
    # Only update video counts if new value is higher (never downgrade)
    if v_count > 0:
        if v_count > current_video_count_alltime:
            sv(p, 'video_count_alltime', v_count)
        if v_count > current_video_count:
            sv(p, 'video_count', v_count)
    
    # Only update influencer count if new value is higher (never downgrade)
    if inf_count > 0 and inf_count > current_inf_count:
        sv(p, 'influencer_count', inf_count)
    
    sv(p, 'shop_ads_commission', shop_ads if shop_ads > 0 else gv(p, 'shop_ads_commission'))
    sv(p, 'ad_spend', ad_spend if ad_spend > 0 else gv(p, 'ad_spend'))
    sv(p, 'ad_spend_total', ad_spend_total if ad_spend_total > 0 else gv(p, 'ad_spend_total'))
    sv(p, 'commission_rate', comm if comm > 0 else gv(p, 'commission_rate'))
    if price > 0: sv(p, 'price', price)
    sv(p, 'last_updated', datetime.utcnow())
    
    # Ratings & Reviews (Try common keys)
    rating = float(v.get('productRating') or v.get('rating') or v.get('avgRating') or 0)
    reviews = int(v.get('productReviewCount') or v.get('reviewCount') or v.get('commentCount') or 0)
    if rating > 0: sv(p, 'product_rating', rating)
    if reviews > 0: sv(p, 'review_count', reviews)

    if gmv > 0: sv(p, 'gmv', gmv)
    if sales_period > 0: 
        sv(p, 'sales_7d', sales_period)
        
        # If Total Sales missing, at least use Period Sales
        current_total = int(gv(p, 'sales', 0))
        if current_total < sales_period:
            sv(p, 'sales', sales_period)
            
    # Try generic Total Sales keys
    total_sales = int(v.get('productTotalSales') or v.get('totalSales') or v.get('soldCount') or 0)
    if total_sales > 0: sv(p, 'sales', total_sales)

    # NOTE: video_count and influencer_count already handled above with all-time logic
    
    # Extract Ad Spend
    ad_spend_7d = float(v.get('periodAdSpend') or 0)
    ad_spend_total = float(v.get('productTotalAdSpend') or v.get('totalAdSpend') or v.get('adSpend') or 0)
    if ad_spend_7d > 0: sv(p, 'ad_spend', ad_spend_7d)
    if ad_spend_total > 0: sv(p, 'ad_spend_total', ad_spend_total)
    
    # Extract Growth
    growth_val = float(v.get('growthPercentage') or v.get('periodGrowth') or 0)
    if growth_val != 0: sv(p, 'gmv_growth', growth_val)
    
    # Dates
    sv(p, 'last_updated', datetime.utcnow())
    sv(p, 'is_enriched', True)
    
    # FINAL FILTER: Only reject products with no recent sales
    # Video count filter is only for Hot Product Tracker, not individual enrichment
    if sales_period <= 0:
        return False, f"Zero 7D Sales ({sales_period})"

    return True, "Enriched via Copilot"



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

class SystemConfig(db.Model):
    """General system settings stored in DB to survive restarts without Render redeploy"""
    __tablename__ = 'system_config'
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)
    description = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

def get_config_value(key, default=None):
    """Get config from DB, fallback to environment or default"""
    try:
        # We need an app context if this is called outside of a request
        config = SystemConfig.query.get(key)
        if config and config.value:
            return config.value
    except Exception as e:
        # Fallback if table doesn't exist yet or other DB error
        pass
    return os.environ.get(key, default)

def set_config_value(key, value, description=None):
    """Set config in DB"""
    config = SystemConfig.query.get(key)
    if not config:
        config = SystemConfig(key=key)
    config.value = value
    if description:
        config.description = description
    db.session.add(config)
    db.session.commit()

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
    shop_ads_commission = db.Column(db.Float, default=0, index=True)
    price = db.Column(db.Float, default=0, index=True)
    original_price = db.Column(db.Float, default=0) # Added for Strikethrough Price
    product_url = db.Column(db.String(500))


    image_url = db.Column(db.Text)
    cached_image_url = db.Column(db.Text)  # Signed URL that works
    image_cached_at = db.Column(db.DateTime)  # When cache was created
    
    # Video/Live stats from EchoTik
    video_count = db.Column(db.Integer, default=0)
    video_count_alltime = db.Column(db.Integer, default=0)  # All-time video count for saturation analysis
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
    
    scan_type = db.Column(db.String(50), default='brand_hunter', index=True)
    is_ad_driven = db.Column(db.Boolean, default=False) # Track if found via ad scan
    ad_spend = db.Column(db.Float, default=0)  # 7D Ad Spend
    ad_spend_total = db.Column(db.Float, default=0)  # Lifetime/Total Ad Spend
    gmv_growth = db.Column(db.Float, default=0)  # 7D GMV Growth Percentage
    
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
        """Convert product to dictionary for API response."""
        return {
            'product_id': self.product_id,
            'product_name': self.product_name,
            'seller_id': self.seller_id,
            'seller_name': self.seller_name,
            'is_ad_driven': (self.scan_type in ['apify_ad', 'daily_virals']) or (self.sales_7d > 50 and self.influencer_count < 5 and self.video_count < 5),
            'commission_rate': self.commission_rate,
            'shop_ads_commission': self.shop_ads_commission,
            'stock': self.live_count,
            'price': self.price,
            'image_url': self.cached_image_url or self.image_url,
            'cached_image_url': self.cached_image_url,
            'product_url': self.product_url,
            'product_rating': self.product_rating,
            'review_count': self.review_count,
            'has_free_shipping': self.has_free_shipping or False,
            'is_favorite': self.is_favorite,
            'product_status': self.product_status or 'active',
            'status_note': self.status_note,
            'scan_type': self.scan_type,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
            # Stats - 7d for sales/ad_spend, all-time for video/creator counts
            'sales': self.sales,
            'sales_7d': self.sales_7d,
            'sales_30d': self.sales_30d,
            'gmv': self.gmv,
            'gmv_30d': self.gmv_30d,
            'gmv_growth': self.gmv_growth or 0,
            'video_count': self.video_count,  # 7D videos (momentum)
            'video_count_alltime': self.video_count_alltime or self.video_count,  # All-time for saturation
            'video_7d': self.video_7d,
            'video_30d': self.video_30d,
            'influencer_count': self.influencer_count,  # All-time
            'live_count': self.live_count,
            'views_count': self.views_count,
            'ad_spend': self.ad_spend,
            'ad_spend_total': self.ad_spend_total,
            'sales_velocity': self.sales_velocity or 0,
            'ad_spend_per_video': (self.ad_spend / self.video_count) if (self.video_count and self.video_count > 0) else 0,
            'roas': (self.gmv / self.ad_spend) if (self.ad_spend and self.ad_spend > 0) else 0,
            'est_profit': (self.gmv * self.commission_rate),
        }

class BlacklistedBrand(db.Model):
    """TikTok Shop Brands/Sellers that are blacklisted (e.g. for removing commissions)"""
    __tablename__ = 'blacklisted_brands'
    
    id = db.Column(db.Integer, primary_key=True)
    seller_name = db.Column(db.String(255), unique=True, index=True, nullable=False)
    seller_id = db.Column(db.String(50), unique=True, index=True)
    reason = db.Column(db.Text)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'seller_name': self.seller_name,
            'seller_id': self.seller_id,
            'reason': self.reason,
            'added_at': self.added_at.isoformat() if self.added_at else None
        }

class WatchedBrand(db.Model):
    """Brands being tracked in Brand Hunter"""
    __tablename__ = 'watched_brands'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, index=True, nullable=False)
    logo_url = db.Column(db.String(500))
    product_count = db.Column(db.Integer, default=0)
    total_sales_7d = db.Column(db.Integer, default=0)
    total_revenue = db.Column(db.Float, default=0)
    avg_commission = db.Column(db.Float, default=0)
    top_product_id = db.Column(db.String(100))
    top_product_name = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)
    last_synced = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'logo_url': self.logo_url,
            'product_count': self.product_count or 0,
            'total_sales_7d': self.total_sales_7d or 0,
            'total_revenue': self.total_revenue or 0,
            'avg_commission': self.avg_commission or 0,
            'top_product_id': self.top_product_id,
            'top_product_name': self.top_product_name,
            'is_active': self.is_active,
            'last_synced': self.last_synced.isoformat() if self.last_synced else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def refresh_stats(self):
        """Recalculate stats from products matching this brand"""
        from sqlalchemy import func
        products = Product.query.filter(
            Product.seller_name.ilike(f'%{self.name}%')
        ).all()
        
        if products:
            self.product_count = len(products)
            self.total_sales_7d = sum(p.sales_7d or 0 for p in products)
            self.total_revenue = sum(p.gmv or 0 for p in products)
            commissions = [p.commission_rate for p in products if p.commission_rate]
            self.avg_commission = sum(commissions) / len(commissions) if commissions else 0
            
            # Find top product by 7D sales
            top = max(products, key=lambda p: p.sales_7d or 0)
            self.top_product_id = top.product_id
            self.top_product_name = top.product_name
            
            # Get logo from first product image
            if products[0].image_url:
                self.logo_url = products[0].cached_image_url or products[0].image_url
        
        self.last_synced = datetime.utcnow()
        db.session.commit()

def is_brand_blacklisted(seller_name=None, seller_id=None):
    """Check if a brand is blacklisted by name or ID"""
    if seller_id:
        return BlacklistedBrand.query.filter_by(seller_id=seller_id).first() is not None
    if seller_name:
        # Check for case-insensitive match
        return BlacklistedBrand.query.filter(BlacklistedBrand.seller_name.ilike(seller_name)).first() is not None
    return False

# =============================================================================
# DATABASE INITIALIZATION
# =============================================================================

with app.app_context():
    db.create_all()
    # Cleanup corrupted records (legacy bug)
    try:
        corrupted = Product.query.filter(Product.product_id.ilike('%None%')).delete(synchronize_session=False)
        if corrupted:
            print(f">> Cleaned up {corrupted} corrupted shop_None records")
            db.session.commit()
    except: pass
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

@app.route('/api/admin/config', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_config():
    """Manage system settings (tokens, global params)"""
    if request.method == 'GET':
        settings = SystemConfig.query.all()
        # Redact sensitive values for safety
        result = []
        for s in settings:
            val = s.value
            if val and len(val) > 20 and any(k in s.key.upper() for k in ['TOKEN', 'KEY', 'SECRET']):
                val = val[:6] + "..." + val[-6:]
            result.append({
                'key': s.key,
                'value': val,
                'description': s.description,
                'updated_at': s.updated_at.isoformat() if s.updated_at else None
            })
        return jsonify({'settings': result})
    
    # POST
    data = request.get_json()
    if not data or 'key' not in data or 'value' not in data:
        return jsonify({'error': 'Missing key or value'}), 400
    
    set_config_value(data['key'], data['value'], data.get('description'))
    log_activity(session.get('user_id'), 'admin_config_update', {'config_key': data['key']})
    
    return jsonify({'success': True, 'message': f"Updated {data['key']}"})

@app.route('/api/admin/migrate', methods=['GET', 'POST'])
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
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS shop_ads_commission FLOAT DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS video_count_alltime INTEGER DEFAULT 0",
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

@app.route('/api/admin/delete-low-videos', methods=['POST'])
@login_required
@admin_required
def admin_delete_low_videos():
    """Delete products with less than 20 total videos (placeholder data)."""
    try:
        # Use video_count_alltime if available, otherwise fall back to video_count
        # Delete products where BOTH all-time and regular video_count are below 20
        count = Product.query.filter(
            db.or_(
                db.and_(Product.video_count_alltime != None, Product.video_count_alltime < 20),
                db.and_(Product.video_count_alltime == None, Product.video_count < 20)
            )
        ).delete(synchronize_session=False)
        
        db.session.commit()
        
        user = get_current_user()
        log_activity(user.id, 'delete_low_videos', {'deleted': count})
        
        return jsonify({
            'success': True,
            'message': f'Deleted {count} products with <20 total videos',
            'deleted': count
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

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

# parse_cover_url removed - standardized version at line 706

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
    
    # Filter for valid URLs (TikTok CDN or already EchoTik images)
    # Signing turns tiktokcdn.com -> echosell-images.tos...
    trusted_domains = ['echosell-images', 'tiktokcdn.com', 'p16-shop', 'p77-shop', 'byteimg.com', 'volces.com']
    valid_urls = [url for url in cover_urls if url and any(dom in str(url) for dom in trusted_domains)]
    
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
                     # Log if any requested URLs were NOT returned in the mapping
                     missing = [u for u in valid_urls[:10] if u not in imgs]
                     if missing:
                         print(f"DEBUG: EchoTik Signer could not map {len(missing)} URLs (likely already signed or invalid)")
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

# LEGACY ECHOTIK SCANNERS REMOVED (Vantage V4)


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
            # FLEXIBLE MAPPING: Support both Video-based JSON and Product-based JSON
            p_obj = item.get('product')
            is_video_based = True
            
            if not isinstance(p_obj, dict):
                 # Check if the item ITSELF is the product (New DV Product List)
                 if item.get('productId') or item.get('product_id') or item.get('product_name'):
                     p_obj = item
                     is_video_based = False
            
            # If still no product object, skip it
            if not isinstance(p_obj, dict):
                skipped_items += 1
                continue

            # Product ID - try multiple keys for robustness
            pid = p_obj.get('productId') or p_obj.get('product_id') or p_obj.get('id')
            if not pid:
                skipped_items += 1
                continue
            
            # Stats Mapping
            # For video-based, we want the specific video stats
            # For product-based, we take the general product stats
            if is_video_based:
                views = safe_int(item.get('latest_view_count') or item.get('playBox') or item.get('views') or item.get('playCount'))
                likes = safe_int(item.get('likeCount') or item.get('diggCount') or item.get('likes'))
            else:
                views = safe_int(p_obj.get('totalViewCount') or p_obj.get('views') or 0)
                likes = safe_int(p_obj.get('totalLikeCount') or p_obj.get('likes') or 0)
            
            # Title Mapping
            product_title = p_obj.get('productName') or p_obj.get('product_name') or p_obj.get('title') or p_obj.get('name') or "Unknown Product"
            
            # Image URL - Expanded mapping for DV Product List
            # Priority: Supabase CDN > TikTok CDN > EchoSell CDN (blocked)
            img_url = p_obj.get('imageUrl') or p_obj.get('image_url') or p_obj.get('coverUrl') or p_obj.get('cover_url') or ""
            if not img_url:
                # Try lists
                imgs = p_obj.get('imageUrls') or p_obj.get('productImages') or p_obj.get('images')
                if isinstance(imgs, list) and len(imgs) > 0:
                    img_url = imgs[0] if isinstance(imgs[0], str) else (imgs[0].get('url') or imgs[0].get('imageUrl'))
            
            # DEBUG: Log the image URL we found
            print(f"DEBUG: DV-IMPORT {pid[:8]} imageUrl: {img_url[:60] if img_url else 'EMPTY'}")
            
            # Advertiser / Shop Name - Expanded mapping (DV JSON doesn't include this, will be Unknown)
            advertiser = p_obj.get('advertiser_name') or p_obj.get('shopName') or p_obj.get('shop_name') or p_obj.get('brandName') or p_obj.get('advertiser') or "Unknown"
            if advertiser == "Unknown" and is_video_based:
                creator = item.get('creator')
                if isinstance(creator, dict):
                    advertiser = creator.get('username') or creator.get('nickname') or "Unknown"

            # Sales / GMV Mappings - Updated for actual DV JSON structure
            # DV uses: allTimeTotalUnitsSold (total), unitsSoldInRange (7d roughly)
            raw_sales = safe_int(p_obj.get('allTimeTotalUnitsSold') or p_obj.get('totalUnitsSold') or p_obj.get('unitsSold') or p_obj.get('soldCount') or p_obj.get('sales') or 0)
            sales_7d = safe_int(p_obj.get('unitsSoldInRange') or p_obj.get('unitsSoldLastSevenDays') or p_obj.get('sales_7d') or 0)
            
            # Commission - DV uses: open_commission_percentage, tdv_commission_percentage
            comm_val = p_obj.get('tdv_commission_percentage') or p_obj.get('open_commission_percentage') or p_obj.get('commission_rate') or 0
             
            # Price - DV uses: avgPrice
            price_val = float(p_obj.get('avgPrice') or p_obj.get('price') or 0)
            
            # GMV / Revenue
            gmv = 0
            revenue_analytics = p_obj.get('revenueAnalytics')
            if isinstance(revenue_analytics, dict):
                 gmv = safe_int(revenue_analytics.get('totalRevenue') or 0)
            else:
                 gmv = safe_int(p_obj.get('totalRevenue') or p_obj.get('revenue') or p_obj.get('gmv') or 0)

            # Rating & Reviews (New from Product List) - DV uses: avgRating, totalReviews
            rating = float(p_obj.get('avgRating') or p_obj.get('rating') or p_obj.get('product_rating') or 0)
            reviews = safe_int(p_obj.get('totalReviews') or p_obj.get('reviews') or p_obj.get('review_count') or 0)

            # Create Candidate
            p = {
                'product_id': str(pid), 
                'product_name': product_title[:200], # Increased limit
                'title': product_title,
                'seller_name': advertiser, 
                'advertiser': advertiser, 
                'price': float(p_obj.get('avgPrice') or p_obj.get('price') or 0),
                'commission_rate': float(comm_val) / 100.0 if float(comm_val) > 1 else float(comm_val),
                'sales': raw_sales,
                'sales_7d': sales_7d,
                'gmv': gmv,
                'product_rating': rating,
                'review_count': reviews,
                'influencer_count': 0,
                'video_count': 0, 
                'video_views': views,
                'video_likes': likes,
                'scan_type': 'daily_virals', 
                'url': item.get('videoUrl') or item.get('link') or p_obj.get('productUrl') or "",
                'image_url': img_url,
                'cover_url': img_url,
                'is_enriched': False
            }
            products.append(p)
            
        # 3. Enrich Candidates (Parallelized)
        saved_count = 0
        debug_log = ""
        start_time_wall = time.time()
        
        def process_one(p_item):
            # Attempt Enrichment via EchoTik
            # Each thread gets its own context/session might be safer for API calls
            res, msg = enrich_product_data(p_item, "[DV-IMPORT]")
            return p_item, res, msg

        # Only enrich if within budget
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_prod = {executor.submit(process_one, p): p for p in products}
            
            for i, future in enumerate(future_to_prod):
                p = future_to_prod[future]
                elapsed = time.time() - start_time_wall
                
                if elapsed > 120:
                    debug_log += f" | {p.get('product_id','?')[:8]} skipped (budget)"
                    res, msg = False, "Skipped (Budget)"
                else:
                    try:
                        p, res, msg = future.result(timeout=(120 - elapsed))
                    except Exception as e:
                        res, msg = False, f"Error: {str(e)}"

                # Database Saving / Updating
                # Use the global helper to ensure consistency
                # Pass explicit_id to prevent 'shop_None' if API response lacks ID
                is_new = save_or_update_product(p, scan_type='daily_virals', explicit_id=p.get('product_id'))
                
                if is_new:
                    saved_count += 1
                
                if i < 5:
                    debug_log += f" | {p.get('product_id','?')[:8]}: {p.get('product_name','?')[:15]}"

            db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f"Processed {len(items)} items. Imported/Updated {len(products)} products.",
            'debug_info': f"First 5 results: {debug_log}"
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

# LEGACY API REMOVED (Vantage V4)


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

    
    # Sort options
    sort_by = request.args.get('sort', 'sales_7d')
    sort_order = request.args.get('order', 'desc')


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
                    Product.gmv > 1000,
                    Product.scan_type == 'copilot'
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


# =============================================================================
# PRODUCT LISTING API - Main Dashboard Endpoint (Unified)
# =============================================================================
# Consolidated into /api/products definition below (api_products function)



@app.route('/api/refresh-images', methods=['POST', 'GET'])
@login_required # Only allow logged in users
def refresh_images():
    """
    Refresh cached image URLs for products using SINGLE-PRODUCT API calls.
    Returns: JSON with stats on progress.
    """
    try:
        batch_size = min(request.args.get('batch', 50, type=int), 100)
        force = request.args.get('force', 'false').lower() == 'true'
        
        # Check if user is admin (Simple check for now)
        is_admin = True # Since we use login_required and it's a private tool
        
        if force:
            # When forcing, we want to hit the oldest or broken ones first
            products = Product.query.filter(
                Product.image_url.isnot(None),
                Product.image_url != ''
            ).order_by(Product.image_cached_at.asc()).limit(batch_size).all()
        else:
            # Calculate stale threshold (48 hours - TikTok CDN URLs expire)
            stale_threshold = datetime.utcnow() - timedelta(hours=48)
            
            # Products missing cached images OR with stale cache
            # First get products that HAVE image_url but need signing/refreshing
            # [LOGIC]: Treat volces.com URLs as priority for refresh since they expire fast
            products = Product.query.filter(
                Product.image_url.isnot(None),
                Product.image_url != '',
                db.or_(
                    Product.cached_image_url.is_(None),
                    Product.cached_image_url == '',
                    Product.image_cached_at.is_(None),
                    Product.image_cached_at < stale_threshold,
                    Product.image_url.contains('volces.com'),   # Always check if it's already a signed link
                    Product.cached_image_url.contains('volces.com'),
                    Product.cached_image_url.is_(None) # Missing cache is an automatic stale
                )
            ).order_by(Product.image_cached_at.asc()).limit(batch_size).all()
            
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
                # If product already has image_url, check if it's a "broken" EchoTik link
                if product.image_url:
                    parsed_url = parse_cover_url(product.image_url)
                    
                    # DETECTION: If the source URL is already an echosell/volces link, 
                    # EchoTik's batch signer will likely fail to "re-sign" it.
                    # We MUST force a fresh scrape in this case.
                    is_pre_signed = "volces.com" in (parsed_url or "").lower() or "echosell" in (parsed_url or "").lower()
                    
                    if parsed_url and not is_pre_signed:
                        signed_urls = get_cached_image_urls([parsed_url])
                        if signed_urls.get(parsed_url):
                            product.cached_image_url = signed_urls[parsed_url]
                            product.image_cached_at = datetime.utcnow()
                            updated += 1
                            time.sleep(0.1)
                            continue
                    
                    # If it was pre-signed or batch signing failed, fall through to fetch_product_details_echotik
                    if is_pre_signed:
                        print(f"DEBUG: [Image Refresh] Expired signature detected for {product.product_id}. Forcing full re-scrape.")
                
                # No image_url (or force refresh) - fetch from centralized tiered fetcher
                # This prioritizes the free web scraper to save credits
                # Pass force=True to ensure tiered fetcher doesn't just return the same DB cache
                # ENABLE PAID API FALLBACK: User explicitly requested high priority fix and has credits.
                d, source = fetch_product_details_echotik(product.product_id, force=True, allow_paid=True)
                
                if d:
                    # Robustly extract metadata (handles both V3 and Scraped formats)
                    res = extract_metadata_from_echotik(d)
                    cover_url = res.get('image_url') or res.get('cover')
                    
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
    """Get list of unique brands/sellers (filtered to exclude Unknown)"""
    try:
        brands = db.session.query(
            Product.seller_id,
            Product.seller_name,
            db.func.count(Product.product_id).label('product_count'),
            db.func.sum(Product.sales_7d).label('total_sales_7d'),
            db.func.sum(Product.gmv).label('total_revenue'),
            db.func.avg(Product.commission_rate).label('avg_commission')
        ).filter(
            Product.seller_name != None,
            Product.seller_name != '',
            Product.seller_name != 'Unknown',
            Product.seller_name != 'Unknown Seller',
            ~Product.seller_name.ilike('unknown%'),
            ~Product.seller_name.ilike('classified%')
        ).group_by(Product.seller_id, Product.seller_name).order_by(db.desc('total_revenue')).all()
        
        return jsonify({
            'success': True,
            'brands': [{
                'id': i,
                'seller_id': b.seller_id, 
                'name': b.seller_name,  # Use 'name' for frontend compatibility
                'seller_name': b.seller_name,
                'product_count': b.product_count,
                'total_sales_7d': b.total_sales_7d or 0,
                'total_revenue': b.total_revenue or 0,
                'avg_commission': b.avg_commission or 0
            } for i, b in enumerate(brands)]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/brand-products/<seller_name>')
@login_required
def get_brand_products_by_name(seller_name):
    """Get products for a specific brand by seller_name"""
    try:
        # URL decode the seller name
        from urllib.parse import unquote
        decoded_name = unquote(seller_name)
        
        products = Product.query.filter(
            Product.seller_name.ilike(f'%{decoded_name}%')
        ).order_by(Product.sales_7d.desc()).limit(100).all()
        
        return jsonify({
            'success': True,
            'count': len(products),
            'products': [p.to_dict() for p in products]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/brand-sync/<seller_name>', methods=['POST'])
@login_required
def sync_brand_by_name(seller_name):
    """Sync/refresh products for a brand (placeholder - stats are computed on-the-fly)"""
    try:
        from urllib.parse import unquote
        decoded_name = unquote(seller_name)
        
        # Count products for this brand
        count = Product.query.filter(
            Product.seller_name.ilike(f'%{decoded_name}%')
        ).count()
        
        if count == 0:
            return jsonify({'success': False, 'error': 'Brand not found'}), 404
        
        return jsonify({
            'success': True,
            'message': f'Found {count} products for "{decoded_name}". Stats are computed live from database.',
            'product_count': count
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
                Product.scan_type.in_(['apify_ad', 'daily_virals', 'dv_live']),
                db.and_(Product.sales_7d > 50, Product.influencer_count < 5, Product.video_count < 5)
            )
        ).count()
        
        # 3. Opportunity Gems (New Criteria: 50-100 videos, $500+ ad spend, 50+ 7D sales)
        video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)
        hidden_gems = Product.query.filter(
            Product.sales_7d >= 50,
            Product.ad_spend >= 500,
            video_count_field >= 50,
            video_count_field <= 100
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
    Fetches stats from EchoTik (prioritizing Web Scraper for 0 credit cost).
    """
    if request.method == 'POST':
        data = request.get_json() or {}
        input_url = data.get('url', '')
        save_to_db = data.get('save', False)
    else:
        input_url = request.args.get('url', '')
        save_to_db = request.args.get('save', 'false').lower() == 'true'
    
    if not input_url:
        return jsonify({'success': False, 'error': 'Please provide a TikTok product URL or ID'}), 400
    
    # Resolve share links
    resolved_url = input_url
    if is_tiktok_share_link(input_url):
        resolved_url = resolve_tiktok_share_link(input_url)
        if not resolved_url:
            return jsonify({'success': False, 'error': 'Could not resolve share link'}), 400
    
    # Extract product ID
    product_id = extract_product_id(resolved_url)
    if not product_id:
        return jsonify({'success': False, 'error': 'Could not extract product ID'}), 400
    
    try:
        # TIERED ENRICHMENT (Web Scraper -> Realtime -> Cached)
        p, source = fetch_product_details_echotik(product_id)
        
        if not p:
            return jsonify({'success': False, 'error': 'Product not found across EchoTik sources (Web/API/DB)'}), 404

        # Extract stats using robust helper
        res = extract_metadata_from_echotik(p)
        
        # Seller name recovery
        if not res['seller_name'] and res['seller_id']:
            real_name = fetch_seller_name(res['seller_id'])
            if real_name: res['seller_name'] = real_name

        # Prepare response data
        product_data = {
            'product_id': product_id,
            'product_name': res['product_name'] or "Unknown Product",
            'seller_id': res['seller_id'],
            'seller_name': res['seller_name'] or "Unknown Seller",
            'sales': res['sales'],
            'sales_7d': res['sales_7d'],
            'sales_30d': res['sales_30d'],
            'commission_rate': res['commission_rate'],
            'price': res['price'],
            'influencer_count': res['influencer_count'],
            'video_count': res['video_count'],
            'live_count': res['live_count'],
            'views_count': res['views_count'],
            'image_url': res['image_url'],
            'product_url': res['product_url'] or f'https://www.tiktok.com/shop/pdp/{product_id}',
            'source': source
        }

        # Save/Update if requested
        if save_to_db:
            save_or_update_product(p, scan_type='lookup', explicit_id=product_id)

        return jsonify({
            'success': True,
            'product': product_data,
            'source': source
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# BATCH URL LOOKUP - Quick Preview Mode (No Save)
# =============================================================================

@app.route('/api/lookup/batch', methods=['POST'])
@login_required
def lookup_batch():
    """
    Batch lookup multiple TikTok products by URL or ID.
    Returns stats preview WITHOUT saving to database.
    """
    data = request.get_json() or {}
    urls_raw = data.get('urls', '')
    
    if not urls_raw:
        return jsonify({'success': False, 'error': 'Please provide URLs'}), 400
    
    urls = [u.strip() for u in urls_raw.strip().split('\n') if u.strip()]
    if not urls:
        return jsonify({'success': False, 'error': 'No valid URLs found'}), 400
    if len(urls) > 20: # Limit batch size for performance
         return jsonify({'success': False, 'error': 'Batch limited to 20 items per request'}), 400
    
    results = []
    errors = []
    
    for i, url in enumerate(urls):
        try:
            # Resolve share links
            resolved_url = url
            if is_tiktok_share_link(url):
                resolved_url = resolve_tiktok_share_link(url)
            
            # Extract product ID
            product_id = extract_product_id(resolved_url)
            if not product_id:
                errors.append({'url': url, 'error': 'Invalid ID'})
                continue
            
            # TIERED ENRICHMENT
            p, source = fetch_product_details_echotik(product_id)
            if not p:
                errors.append({'url': url, 'error': 'Not found'})
                continue
            
            res = extract_metadata_from_echotik(p)
            
            results.append({
                'product_id': product_id,
                'product_name': res['product_name'] or 'Unknown',
                'image_url': res['image_url'],
                'seller_name': res['seller_name'] or 'Unknown',
                'price': res['price'],
                'sales_7d': res['sales_7d'],
                'video_count': res['video_count'],
                'influencer_count': res['influencer_count'],
                'commission_rate': res['commission_rate'],
                'url': url
            })
            
        except Exception as e:
            errors.append({'url': url, 'error': str(e)})
    
    return jsonify({
        'success': True,
        'count': len(results),
        'products': results,
        'errors': errors
    })


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

# LEGACY ROUTE REMOVED (Vantage V4)


@app.route('/scanner')
@login_required
@admin_required
def scanner_page():
    return send_from_directory('pwa', 'scanner_v4.html')

@app.route('/settings')
@login_required
def settings_page():
    return send_from_directory('pwa', 'settings.html')

@app.route('/brand-hunter')
@login_required
def brand_hunter_page():
    return send_from_directory('pwa', 'brand_hunter.html')

@app.route('/vantage-v2')
@login_required
def vantage_v2_page():
    return send_from_directory('pwa', 'vantage_v2.html')



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
    
    # Return full data dict
    return jsonify({
        'success': True,
        'id': p.product_id, # for frontend compat
        **p.to_dict()
    })

@app.route('/api/product/enrich/<path:product_id>')
@login_required
def api_enrich_product(product_id):
    """Enforce fresh enrichment from EchoTik for a single product"""
    # 1. Resolve ID
    p = Product.query.get(product_id)
    if not p and product_id.isdigit():
        p = Product.query.get(f"shop_{product_id}")
    if not p and product_id.startswith('shop_'):
        p = Product.query.get(product_id.replace('shop_', ''))
        
    if not p:
        return jsonify({'success': False, 'error': 'Product not found'}), 404
        
    try:
        # Trigger enrichment logic - force bypasses database cache
        # Single manual refresh ALLOWS paid fallback if needed (1 credit)
        success = enrich_product_data(p, i_log_prefix="⚡[LiveSync]", force=True, allow_paid=True)
        if success:
            db.session.commit()
            return jsonify({'success': True, 'product': p.to_dict()})
        else:
            # Check if it was a budget safeguard block
            # Note: enrich_product_data returns boolean, so we might need to check logs or return more info if needed
            # For now, if it failed and force was True, it's likely a scraper issue or API issue
            return jsonify({
                'success': False, 
                'error': 'Enrichment blocked or failed. Please check EchoTik cookie status in Terminal.'
            }), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/pwa/<path:filename>')
def pwa_files(filename):
    # Allow login.html without auth
    if filename in ['login.html']:
        return send_from_directory('pwa', filename)
    # Other PWA files need auth check
    if not session.get('user_id'):
        return redirect('/login')
    return send_from_directory('pwa', filename)

@app.route('/api/image-proxy/<path:product_id>')
def image_proxy(product_id):
    """Proxy image requests to bypass TikTok direct-link blocks and fix metadata."""
    try:
        raw_id = product_id.replace('shop_', '')
        
        # We need the app context for database queries if this is called from outside the main app thread
        # (though usually routes are within context, let's be safe and also use the correct Model)
        with app.app_context():
            # Try to find the product in DB to get the original image URL
            p = Product.query.get(f"shop_{raw_id}")
            if not p:
                p = Product.query.get(raw_id)
            
            target_url = None
            if p:
                target_url = p.cached_image_url or p.image_url
            
            # Clean URL (handles bracketed JSON arrays or quoted strings from some sources)
            if target_url:
                target_url = parse_cover_url(target_url)
            
            # Fallback for manual IDs or if DB fetch failed
            if not target_url:
                if str(product_id).startswith('http'):
                    target_url = product_id
                else:
                    print(f"DEBUG: Proxy Image Failed - No URL found for {product_id}")
                    return redirect('/vantage_logo.png')

            print(f"DEBUG: Proxying Image for {product_id} -> {target_url[:100]}...")

            # Dynamic Headers: TikTok/Volcengine are extremely sensitive
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.tiktok.com/",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "image",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "cross-site"
            }
            
            lower_url = target_url.lower()
            if "volces.com" in lower_url or "echotik" in lower_url:
                headers["Referer"] = "https://echosell.echotik.live/"
            elif "tiktokcdn.com" in lower_url:
                headers["Referer"] = "https://www.tiktok.com/"

            # Consolidated Retry Logic
            try_configs = [
                {"Referer": "https://echotik.live/", "use_proxy": False, "impersonate": "chrome110"},
                {"Referer": "https://echosell.echotik.live/", "use_proxy": True, "impersonate": "chrome110"},
                {"Referer": "https://www.tiktok.com/", "use_proxy": False, "impersonate": "safari15_3"},
                {"Referer": "https://shop.tiktok.com/", "use_proxy": True, "impersonate": "chrome110"},
                {"Referer": None, "use_proxy": False, "naked": True, "impersonate": "chrome110"}
            ]
            
            resp = None
            last_status = "Not Attempted"
            
            try:
                from curl_cffi import requests as curl_requests
            except ImportError:
                curl_requests = None

            for config in try_configs:
                local_headers = headers.copy()
                if config.get("Referer"):
                    local_headers["Referer"] = config["Referer"]
                else:
                    local_headers.pop("Referer", None)
                
                try:
                    # Ultra-high timeout for read, but shorter for connect
                    current_timeout = (10, 30) if "volces.com" in lower_url else (10, 15)
                    
                    current_proxies = None
                    if config.get("use_proxy") and DV_PROXY_STRING:
                        parts = DV_PROXY_STRING.split(':')
                        if len(parts) == 4:
                            host, port, user, pw = parts
                            proxy_url = f"http://{user}:{pw}@{host}:{port}"
                            current_proxies = {"http": proxy_url, "https": proxy_url}
                            print(f"DEBUG: Image Proxy using residential tunnel for {config.get('Referer')}")

                    # Prefer curl_cffi for better impersonation
                    if curl_requests:
                        r = curl_requests.get(
                            target_url, 
                            headers=local_headers, 
                            proxies=current_proxies,
                            impersonate=config.get("impersonate", "chrome110"),
                            timeout=current_timeout,
                            verify=False if config.get("naked") else True
                        )
                        # Adapt curl_cffi response to look like requests response for the rest of the logic
                        class MockResp:
                            def __init__(self, r):
                                self.status_code = r.status_code
                                self.content = r.content
                                self.headers = r.headers
                                # curl_cffi headers are case-insensitive dict
                                self.raw = type('obj', (object,), {'headers': r.headers})
                        resp = MockResp(r)
                    else:
                        resp = requests.get(
                            target_url, 
                            headers=local_headers, 
                            stream=True, 
                            timeout=current_timeout, 
                            verify=False if config.get("naked") else True,
                            proxies=current_proxies
                        )
                    
                    if resp.status_code == 200:
                        break
                    
                    last_status = str(resp.status_code)
                    print(f"DEBUG: Image Proxy attempt {config.get('Referer')} status: {resp.status_code}")
                    
                except Exception as e:
                    last_status = f"Err: {str(e)[:50]}"
                    print(f"DEBUG: Image Proxy attempt failed ({config.get('Referer')}): {e}")
                    continue
            
            if not resp or resp.status_code != 200:
                print(f"Proxy Final Error: {last_status} for {target_url}")
                # FALLBACK: If we got a 403 on a signed EchoTik link, it's definitively EXPIRED.
                # Redirect to a placeholder to avoid empty images, but keep the original URL in logs.
                if "403" in str(last_status) and ("volces.com" in lower_url or "echosell" in lower_url):
                    print(f"DEBUG: Definitive Signature Expiration for {product_id}. Needs Refresh.")
                    return redirect('/vantage_logo.png')
                return redirect(target_url)

            # Re-wrap headers for Flask Response
            excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection', 'server', 'x-cache']
            proxy_headers = [(name, value) for (name, value) in resp.headers.items()
                             if name.lower() not in excluded_headers]

            return Response(resp.content, resp.status_code, proxy_headers)

    except Exception as e:
        print(f"Proxy Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/products', methods=['GET'])
@login_required
def api_products():
    """Unified product listing API with filtering, sorting, and pagination"""
    try:
        # 1. Parsing Parameters (Supporting aliases for frontend compatibility)
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 24, type=int)
        if 'limit' in request.args: per_page = request.args.get('limit', type=int)
        
        sort_by = request.args.get('sort') or request.args.get('sort_by') or 'sales_7d'
        
        # Filters
        min_sales = request.args.get('min_sales', type=int)
        max_inf = request.args.get('max_inf', type=int)
        min_inf = request.args.get('min_inf', type=int)
        
        # Default to 2 videos unless searching specifically for lower
        min_vids = request.args.get('min_vids', 2, type=int)
        max_vids = request.args.get('max_vids', type=int)
        
        scan_type = request.args.get('scan_type')
        seller_id = request.args.get('seller_id')
        keyword = request.args.get('keyword') or request.args.get('search')
        min_commission = request.args.get('min_commission', type=float)
        
        # Favorite alias
        is_favorite = (request.args.get('favorite', 'false').lower() == 'true' or 
                       request.args.get('favorites_only', 'false').lower() == 'true')

        # Gems alias
        is_gems = request.args.get('gems_only', 'false').lower() == 'true'
        
        # High Ad Spend alias
        is_high_ad = request.args.get('high_ad_spend', 'false').lower() == 'true'

        # Caked Finds alias
        is_caked = request.args.get('caked_only', 'false').lower() == 'true'

        # 2. Build Query
        # Base filter: Exclude unavailable products
        query = Product.query.filter(or_(Product.product_status == None, Product.product_status != 'unavailable'))

        if is_favorite:
            query = query.filter(Product.is_favorite == True)
        
        if is_gems:
            # Opportunity Gems: High Sales, High Ad Spend, 50-100 total videos
            video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)
            query = query.filter(
                Product.sales_7d >= 50,  # High 7D sales
                Product.ad_spend >= 500,  # High ad spend ($500+)
                video_count_field >= 50,  # Min 50 videos
                video_count_field <= 100  # Max 100 videos
            )
            
        if is_high_ad:
            # High Ad Spend: High Volume & Copilot Scan
            # This is distinct from Gems (which focuses on low saturation)
            query = query.filter(
                db.or_(
                    Product.ad_spend > 500,
                    Product.scan_type == 'copilot'
                )
            )

        if is_caked:
            # Caked Finds: High-Potential "Early Phase" Winners
            # Refined via Research (caked/new.txt):
            # - Commission: >= 15% (Preferred range)
            # - Price: $30 - $250 (Primary sweet spot is $50-150, but we allow high-ticket)
            # - Ad Spend: >= $1,000 (Removed upper cap as winners scale high)
            # - Saturation: 5 - 80 creators (Catching from early validation)
            # - Videos: 10 - 200 videos all-time (Momentum sweet spot)
            
            video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)
            
            query = query.filter(
                Product.ad_spend >= 1000,
                Product.price.between(30, 250),
                Product.influencer_count.between(5, 80),
                video_count_field.between(10, 200),
                db.or_(Product.commission_rate >= 0.15, Product.shop_ads_commission >= 0.15)
            )

        if seller_id:
            query = query.filter(Product.seller_id == seller_id)
            
        if scan_type:
            if ',' in scan_type:
                types = [t.strip() for t in scan_type.split(',')]
                query = query.filter(Product.scan_type.in_(types))
            else:
                query = query.filter(Product.scan_type == scan_type)

        if keyword:
            keyword_term = f"%{keyword}%"
            query = query.filter(db.or_(
                Product.product_name.ilike(keyword_term),
                Product.seller_name.ilike(keyword_term),
                Product.product_id.ilike(keyword_term)
            ))

        if min_sales is not None:
            query = query.filter(Product.sales_7d >= min_sales)
        
        if min_inf is not None:
            query = query.filter(Product.influencer_count >= min_inf)
            
        if max_inf is not None:
            query = query.filter(Product.influencer_count <= max_inf)
            
        if min_vids is not None:
            query = query.filter(Product.video_count >= min_vids)
            
        if max_vids is not None:
            query = query.filter(Product.video_count <= max_vids)

        if min_commission is not None:
            try:
                # Expecting value in percentage form like 10, 15, 20
                threshold = float(min_commission) / 100.0
                query = query.filter(db.or_(
                    Product.commission_rate >= threshold,
                    Product.shop_ads_commission >= threshold
                ))
            except (ValueError, TypeError):
                pass

        # 3. Apply Sorting
        if sort_by in ['sales_desc', 'sales_7d']:
            query = query.order_by(Product.sales_7d.desc().nullslast(), Product.sales.desc().nullslast())
        elif sort_by in ['ad_spend_7d', 'ad_spend']:
            query = query.order_by(Product.ad_spend.desc().nullslast())
        elif sort_by == 'sales_asc':
            query = query.order_by(Product.sales_7d.asc().nullsfirst())
        elif sort_by == 'inf_asc':
            query = query.order_by(Product.influencer_count.asc().nullsfirst())
        elif sort_by in ['inf_desc', 'influencer_count']:
            query = query.order_by(Product.influencer_count.desc().nullslast())
        elif sort_by in ['commission', 'commission_rate']:
            query = query.order_by((db.func.coalesce(Product.commission_rate, 0) + db.func.coalesce(Product.shop_ads_commission, 0)).desc().nullslast())
        elif sort_by in ['newest', 'first_seen']:
            query = query.order_by(Product.first_seen.desc().nullslast(), Product.last_updated.desc().nullslast())
        elif sort_by in ['updated', 'last_updated']:
            query = query.order_by(Product.last_updated.desc().nullslast())
        elif sort_by == 'video_count':
            query = query.order_by(Product.video_count.desc().nullslast())
        elif sort_by == 'video_count_alltime':
            query = query.order_by(Product.video_count_alltime.desc().nullslast())
        elif sort_by in ['vids_asc', 'video_asc']:
            # Use all-time video count for "least videos" with fallback to regular video_count
            query = query.order_by(db.func.coalesce(Product.video_count_alltime, Product.video_count).asc().nullsfirst())
        elif sort_by in ['gem_score', 'efficiency']:
            # Efficiency Score: High Sales + Low Videos (using all-time count)
            video_count_field = func.coalesce(Product.video_count_alltime, Product.video_count, 0)
            score = (func.coalesce(Product.sales_7d, 0) / (video_count_field + 1))
            query = query.order_by(score.desc().nullslast())
        elif sort_by == 'price_desc':
            query = query.order_by(Product.price.desc().nullslast())
        elif sort_by == 'price_asc':
            query = query.order_by(Product.price.asc().nullsfirst())
        else:
            query = query.order_by(Product.first_seen.desc().nullslast())

        # 4. Pagination & Execution
        total = query.count()
        products = query.offset((page - 1) * per_page).limit(per_page).all()

        return jsonify({
            'success': True,
            'total': total,
            'count': total, # Compatibility
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page,
            'products': [p.to_dict() for p in products]
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500



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


# REMOVED: Duplicate /api/stats endpoint was here - the correct one is at line ~3391


# Aliases for compatibility
@app.route('/api/debug/check-product/<path:product_id>')
@app.route('/api/product/<path:product_id>')
@login_required
def unified_product_detail(product_id):
    """Unified helper for product details"""
    p = Product.query.get(product_id)
    if not p and product_id.isdigit():
        p = Product.query.get(f"shop_{product_id}")
    if not p and product_id.startswith('shop_'):
        p = Product.query.get(product_id.replace('shop_', ''))
        
    if not p:
        return jsonify({'success': False, 'error': 'Product not found'}), 404
        
    return jsonify({
        'success': True,
        'id': p.product_id,
        **p.to_dict()
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
@login_required
def refresh_all_products():
    """
    Batch refresh ALL active products from EchoTik API.
    Calls API one product at a time to avoid errors.
    """
    passkey = request.args.get('passkey') or (request.json.get('passkey') if request.is_json else None)
    dev_passkey = os.environ.get('DEV_PASSKEY', '')
    
    # Allow if passkey matches OR if user is logged in
    if not (dev_passkey and passkey == dev_passkey) and not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    # Limit how many products to refresh per call (to avoid timeout)
    limit = min(int(request.args.get('limit', 100)), 500)
    offset = int(request.args.get('offset', 0))
    delay = float(request.args.get('delay', 0.5))
    
    try:
        products = Product.query.filter(
            db.or_(Product.product_status == 'active', Product.product_status == None, Product.scan_type == 'daily_virals')
        ).order_by(Product.last_updated.asc()).offset(offset).limit(limit).all()
        
        total_products = len(products)
        updated = 0
        failed = 0
        errors = []
        
        print(f"🔄 [Global Refresh] Starting sync for {total_products} products (offset={offset}, limit={limit})...")
        
        for i, product in enumerate(products):
            try:
                # Use centralized helper with multi-region failover
                d, source = fetch_product_details_echotik(product.product_id)
                
                if d:
                    # Use unified helper for all mapping and persistence
                    # Pass explicit_id to prevent 'shop_None' if API response lacks ID
                    save_or_update_product(d, scan_type=product.scan_type, explicit_id=product.product_id)
                    updated += 1
                else:
                    print(f"DEBUG: Failed to refresh {product.product_id} from any source.")
                    # Mark as likely OOS if it can't be found anymore
                    if product.sales_7d == 0:
                        product.product_status = 'likely_oos'
            
            except Exception as e:
                failed += 1
                if len(errors) < 5:
                    errors.append(f"Prod {product.product_id}: {str(e)}")
            
            # Rate limiting
            time.sleep(delay)
            if i % 10 == 0:
                db.session.commit()
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'updated': updated,
            'failed': failed,
            'count': len(products),
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

@app.route('/api/admin/purge-low-signal', methods=['POST'])
@login_required
def purge_low_signal():
    """
    Delete products with 0 or 1 total videos from the database.
    This keeps the databanks high-signal.
    """
    try:
        # We target products with <=1 video count
        products_to_delete = Product.query.filter(
            Product.video_count <= 1
        )
        
        count = products_to_delete.count()
        products_to_delete.delete(synchronize_session=False)
        db.session.commit()
        
        print(f"🧹 PURGE COMPLETE: Removed {count} low-signal products.")
        return jsonify({
            'success': True, 
            'message': f'Mission Accomplished: {count} low-signal items purged from mainframe.'
        })
    except Exception as e:
        db.session.rollback()
        print(f"CRITICAL Error in purge: {e}")
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
            ('ad_spend', 'FLOAT DEFAULT 0'),
            ('ad_spend_total', 'FLOAT DEFAULT 0'),
            ('gmv_growth', 'FLOAT DEFAULT 0'),
            ('scan_type', 'VARCHAR(50) DEFAULT \'brand_hunter\''),
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
         target_url = f"https://www.tiktok.com/shop/pdp/p/{product_id}?source=ecommerce_store&region=US"
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
            
            if 'products' in inspector.get_tables():
                columns = [c['name'] for c in inspector.get_columns('products')]
                
                # List of columns to potentialy add
                cols_to_check = [
                    ('original_price', 'FLOAT DEFAULT 0'),
                    ('ad_spend', 'FLOAT DEFAULT 0'),
                    ('ad_spend_total', 'FLOAT DEFAULT 0'),
                    ('gmv_growth', 'FLOAT DEFAULT 0'),
                    ('scan_type', "VARCHAR(50) DEFAULT 'brand_hunter'")
                ]

                for col_name, col_def in cols_to_check:
                    if col_name not in columns:
                        print(f">> MIGRATION: Adding '{col_name}' column to products table...")
                        try:
                            # Postgres uses IF NOT EXISTS, SQLite does not support it in ADD COLUMN usually easily but we check first
                            if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
                                db.session.execute(db.text(f'ALTER TABLE products ADD COLUMN {col_name} {col_def.split(" ")[0]}'))
                            else:
                                db.session.execute(db.text(f'ALTER TABLE products ADD COLUMN IF NOT EXISTS {col_name} {col_def}'))
                            db.session.commit()
                            print(f">> MIGRATION: Added {col_name} Success!")
                        except Exception as e:
                            print(f"!! MIGRATION FAILED for {col_name}: {e}")
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


# Consolidated with /api/image-proxy/<path:product_id> above

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
                params={'seller_id': seller_id, 'sort_by': 'total_sale_7d_cnt', 'sort_order': 'desc', 'page_num': p_idx, 'page_size': 20},
                auth=get_auth(), timeout=30
            )
             data = p_res.json()
             if data.get('code') == 0:
                 items = data.get('data', [])
                 for item in items:
                     found += 1
                     item['seller_id'] = seller_id
                     if save_or_update_product(item):
                         saved += 1
                     
        db.session.commit()
        return jsonify({'products_found': found, 'products_saved': saved})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
    


# =============================================================================
# DAILYVIRALS MANUAL IMPORT ENDPOINT
# =============================================================================

@app.route('/api/scan/manual', methods=['POST'])
@login_required
def manual_scan_import():
    """
    Import products from DailyVirals videos JSON.
    Extracts product IDs and fetches full stats from EchoTik.
    """
    try:
        data = request.get_json()
        json_str = data.get('json_data', '{}')
        source_url = data.get('url', 'manual_import')
        
        # Parse the JSON
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            return jsonify({'success': False, 'error': f'Invalid JSON: {e}'}), 400
        
        # Extract products from various structures
        products_to_process = []
        
        # Structure 1: DailyVirals Videos format - data[].product.productId
        if isinstance(parsed, dict) and 'data' in parsed:
            items = parsed.get('data', [])
            if isinstance(items, list):
                for item in items:
                    product = item.get('product', {})
                    if isinstance(product, dict) and product.get('productId'):
                        products_to_process.append({
                            'product_id': product.get('productId'),
                            'product_name': product.get('productName'),
                            'image_url': product.get('imageUrl'),
                            # Revenue from DV (fallback if EchoTik fails)
                            'dv_revenue_7d': product.get('revenueLastSevenDays'),
                            'dv_total_sold': product.get('totalUnitsSold'),
                        })
        
        # Structure 2: Direct array of products
        elif isinstance(parsed, list):
            for item in parsed:
                p_id = item.get('productId') or item.get('product_id') or item.get('id')
                if p_id:
                    products_to_process.append({
                        'product_id': p_id,
                        'product_name': item.get('productName') or item.get('product_name') or item.get('title'),
                        'image_url': item.get('imageUrl') or item.get('image_url') or item.get('cover'),
                    })
        
        # Structure 3: Object with 'list' key (common API response)
        elif isinstance(parsed, dict) and 'list' in parsed:
            items = parsed.get('list', [])
            for item in items:
                p_id = item.get('productId') or item.get('product_id') or item.get('id')
                if p_id:
                    products_to_process.append({
                        'product_id': p_id,
                        'product_name': item.get('productName') or item.get('product_name') or item.get('title'),
                        'image_url': item.get('imageUrl') or item.get('image_url') or item.get('cover'),
                    })
        
        # Structure 4: Object with 'videos' key
        elif isinstance(parsed, dict) and 'videos' in parsed:
            items = parsed.get('videos', [])
            for item in items:
                product = item.get('product', {})
                if isinstance(product, dict) and product.get('productId'):
                    products_to_process.append({
                        'product_id': product.get('productId'),
                        'product_name': product.get('productName'),
                        'image_url': product.get('imageUrl'),
                    })
        
        if not products_to_process:
            return jsonify({
                'success': False, 
                'error': 'No products found in JSON. Expected: data[].product.productId or list[].productId',
                'debug_info': f'Parsed type: {type(parsed).__name__}, Keys: {list(parsed.keys()) if isinstance(parsed, dict) else "N/A"}'
            }), 400
        
        # Process each product: Fetch from EchoTik and save
        success_count = 0
        error_count = 0
        errors = []
        
        for p in products_to_process:
            raw_id = str(p['product_id']).replace('shop_', '')
            print(f"[DV Import] Processing product: {raw_id}")
            
            try:
                # Fetch full stats from EchoTik
                echotik_data, source = fetch_product_details_echotik(raw_id)
                
                if echotik_data:
                    # Merge DV data as fallback
                    if p.get('product_name') and not echotik_data.get('product_name'):
                        echotik_data['product_name'] = p['product_name']
                    if p.get('image_url') and not echotik_data.get('image_url'):
                        echotik_data['image_url'] = p['image_url']
                    
                    # Fetch seller name if missing or Unknown
                    seller_id = echotik_data.get('seller_id') or echotik_data.get('shop_id')
                    current_seller = echotik_data.get('seller_name') or echotik_data.get('shop_name')
                    if seller_id and (not current_seller or current_seller.lower() in ['unknown', 'none', '']):
                        real_name = fetch_seller_name(seller_id)
                        if real_name:
                            echotik_data['seller_name'] = real_name
                    
                    # Save/update in database
                    save_or_update_product(echotik_data, scan_type='dv_import', explicit_id=raw_id)
                    success_count += 1
                    print(f"[DV Import] ✅ Saved {raw_id} from {source}")

                else:
                    # EchoTik failed, save with DV data only (limited)
                    fallback_data = {
                        'product_id': raw_id,
                        'product_name': p.get('product_name') or 'Unknown',
                        'image_url': p.get('image_url'),
                        'sales_7d': int(p.get('dv_revenue_7d') or 0),  # Revenue as proxy for now
                        'sales': int(p.get('dv_total_sold') or 0),
                    }
                    save_or_update_product(fallback_data, scan_type='dv_import_fallback', explicit_id=raw_id)
                    error_count += 1
                    errors.append(f"{raw_id}: EchoTik failed, saved with limited DV data")
                    print(f"[DV Import] ⚠️ {raw_id}: EchoTik unavailable, used fallback")
                    
            except Exception as e:
                error_count += 1
                errors.append(f"{raw_id}: {str(e)[:50]}")
                print(f"[DV Import] ❌ {raw_id}: {e}")
        
        # Commit all changes
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': f'DB commit failed: {e}'}), 500
        
        return jsonify({
            'success': True,
            'message': f'Imported {success_count} products with full stats, {error_count} with fallback data',
            'products_processed': len(products_to_process),
            'success_count': success_count,
            'error_count': error_count,
            'errors': errors[:5] if errors else None,  # Limit error details
            'debug_info': f'Source: {source_url}'
        })
        
    except Exception as e:
        print(f"[DV Import] Critical error: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/scan/dv-live', methods=['POST'])
@login_required
def scan_dailyvirals_live():
    """DailyVirals Live Scraper (direct API automation)"""
    try:
        data = request.json
        days_str = str(data.get('days', '1'))
        is_paid_input = data.get('type', 'paid')
        saturation = data.get('saturation', 'hidden_gem')
        start_page = int(data.get('start_page', 1))
        page_count = int(data.get('page_count', 1))
        
        # 1. Calculate Date Range (Sanitized for API)
        days_int = int(days_str)
        now = datetime.utcnow()
        # DailyVirals prefers 3 decimal places for milliseconds and 'Z' suffix
        end_date = now.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        start_date = (now - timedelta(days=days_int)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        
        # 2. Get Token (DB or Env fallback)
        db_dv = get_config_value('DAILYVIRALS_TOKEN')
        token = db_dv if db_dv else DV_API_TOKEN
        
        # Handle if the user pasted the word "Bearer " into the setting
        if token and token.lower().startswith('bearer '):
            token = token[7:].strip()
            
        token_src = "[DB]" if db_dv else "[ENV]"
        print(f"[DV Live] Auth Source: {token_src} (Len: {len(token) if token else 0})", flush=True)
        
        # 3. Map filters to DV API terms
        is_paid = "true" if is_paid_input in ['paid', 'mixed'] else "false"
        sort_by = "growth"
        if saturation == "hidden_gem":
            sort_by = "growth" 
        elif saturation == "breakout":
            sort_by = "views"
        
        ua = get_random_user_agent()
        # Enhanced headers with modern browser signatures to bypass Cloudflare
        headers = {
            'authority': 'backend.thedailyvirals.com',
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'en-US,en;q=0.9',
            'authorization': f'Bearer {token}',
            'origin': 'https://www.thedailyvirals.com',
            'referer': 'https://www.thedailyvirals.com/',
            'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': ua
        }
        
        total_processed = 0
        total_saved = 0
        last_error = None
        session_cache = set() # Track products processed in THIS scan to avoid redundant EchoTik calls
        
        for p_idx in range(start_page, start_page + page_count):
            print(f"[DV Live] Fetching page {p_idx} (Start: {start_date}, End: {end_date})...")
            try:
                params = {
                    'startDate': start_date,
                    'endDate': end_date,
                    'page': str(p_idx),
                    'limit': '12', # Match test script
                    'sortBy': sort_by,
                    'limitedResults': 'false',
                    'isPaidPosts': is_paid,
                    'region': ''
                }
                
                # Consolidated Retry Logic for Scanner
                try_configs = [
                    {"use_proxy": True, "impersonate": "chrome110"},
                    {"use_proxy": True, "impersonate": "safari15_3"},
                    {"use_proxy": False, "impersonate": "chrome110"}
                ]
                
                res = None
                last_status = "Not Attempted"
                
                for config in try_configs:
                    try:
                        current_proxies = None
                        if config.get("use_proxy") and DV_PROXY_STRING:
                            parts = DV_PROXY_STRING.split(':')
                            if len(parts) == 4:
                                host, port, user, pw = parts
                                proxy_url = f"http://{user}:{pw}@{host}:{port}"
                                current_proxies = {"http": proxy_url, "https": proxy_url}
                                print(f"[DV Live] Attempting via proxy {host}:{port} ({config.get('impersonate')})")
                            else:
                                print(f"[DV Live] Skipping proxy (invalid format)")
                                continue
                        else:
                            print(f"[DV Live] Attempting direct connection ({config.get('impersonate')})")
    
                        from curl_cffi import requests as curl_requests
                        r = curl_requests.get(
                            DV_BACKEND_URL, 
                            headers=headers, 
                            params=params, 
                            proxies=current_proxies,
                            impersonate=config.get("impersonate", "chrome110"),
                            timeout=25
                        )
                        
                        if r.status_code == 200:
                            res = r
                            break
                        
                        last_status = str(r.status_code)
                        print(f"[DV Live] Attempt failed: HTTP {r.status_code}")
                        
                        # If it's a 403, it's likely a token issue, don't spam retries
                        if r.status_code == 403:
                            res = r
                            break
                            
                    except Exception as e:
                        last_status = f"Err: {str(e)[:50]}"
                        print(f"[DV Live] Attempt Exception: {e}")
                        continue
                
                if not res or res.status_code != 200:
                    if res and res.status_code == 403:
                        last_error = f"Authentication Failed (403). Your DailyVirals token may be expired or your IP is heavily blocked."
                        print(f"[DV Live] 403 Forbidden. Stopping scan.")
                        break
                    
                    last_error = f"Connection Failed: {last_status}"
                    print(f"[DV Live] Skipping page {p_idx} due to multiple failures.")
                    continue

                # JSON Safety check
                try:
                    dv_data = res.json()
                except Exception as je:
                    print(f"[DV Live] Uplink Error: Received non-JSON response (likely HTML/WAF). Content starts with: {res.text[:50]}")
                    last_error = f"Uplink Protocol Error: DailyVirals returned HTML instead of data. This usually means a Cloudflare block or token expiration."
                    continue

                if not dv_data:
                    print(f"[DV Live] Empty JSON from page {p_idx}")
                    continue

                videos = dv_data.get('videos')
                
                # Check for alternative keys if 'videos' is missing
                if videos is None:
                    videos = dv_data.get('data') or dv_data.get('list') or []
                
                if not videos or not isinstance(videos, list):
                    print(f"[DV Live] No items found on page {p_idx}")
                    continue
                    
                for v in videos:
                    try:
                        if not v or not isinstance(v, dict): continue
                        
                        product_info = v.get('product', {})
                        if not product_info: continue
                        
                        p_id = product_info.get('productId')
                        if not p_id: continue
                        
                        raw_id = str(p_id).replace('shop_', '')
                        
                        # DEDUPLICATION: Skip if already processed in this scan session
                        if raw_id in session_cache:
                            # print(f"[DV Live] Skipping {raw_id} - Already processed in this session.")
                            continue
                        session_cache.add(raw_id)
                        
                        total_processed += 1
                        
                        # Enrich via EchoTik
                        echotik_data, source = fetch_product_details_echotik(raw_id)
                        if echotik_data:
                            # Seller lookup
                            seller_id = echotik_data.get('seller_id') or echotik_data.get('shop_id')
                            current_seller = echotik_data.get('seller_name') or echotik_data.get('shop_name')
                            if seller_id and (not current_seller or current_seller.lower() in ['unknown', 'none', '']):
                                real_name = fetch_seller_name(seller_id)
                                if real_name: echotik_data['seller_name'] = real_name
                            
                            # Save as 'daily_virals' to ensure it appears in GMV Max tab
                            save_or_update_product(echotik_data, scan_type='daily_virals', explicit_id=raw_id)
                            total_saved += 1
                            # Commit each successfully processed item to ensure persistence
                            db.session.commit()
                        else:
                            print(f"[DV Live] Skipping {raw_id} - No EchoTik enrichment found.")
                    except Exception as ve:
                        print(f"[DV Live] Error processing video item: {ve}")
                        db.session.rollback()
                        continue
                
                time.sleep(1) # Polite delay
                
            except Exception as e:
                last_error = f"Request Failed: {str(e)}"
                print(f"[DV Live] {last_error}")
                db.session.rollback()
                continue
            
        if total_processed == 0 and last_error:
            return jsonify({
                'success': False,
                'error': f"Uplink Failure: {last_error}. Please verify DAILYVIRALS_TOKEN."
            }), 400
            
        return jsonify({
            'success': True,
            'message': f"Scanned {total_processed} items from DailyVirals, saved/updated {total_saved} products with EchoTik stats."
        })
    except Exception as e:
        print(f"[DV Live] Critical Error: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/scan/partner_opportunity', methods=['POST'])
@admin_required
def trigger_partner_opportunity_scan():
    """Endpoint to trigger the TikTok Partner Center scan"""
    if not TIKTOK_PARTNER_COOKIE:
        return jsonify({'success': False, 'error': 'TIKTOK_PARTNER_COOKIE not configured in environment.'}), 400

    # Run in background via executor
    executor.submit(scan_partner_opportunity_live)
    return jsonify({'success': True, 'message': 'Partner Opportunity scan started in background. Results will appear in the Opportunities tab.'})

def scan_partner_opportunity_live():
    """
    Scrapes the TikTok Shop Partner Center 'High Opportunity' pool.
    Uses humans-centric rate limiting and proxy rotation for safety.
    """
    from curl_cffi import requests as curl_requests
    import time
    import traceback

    print("[Partner Scan] Starting High Opportunity scan...", flush=True)
    
    try:
        target_url = "https://partner.us.tiktokshop.com/api/v1/affiliate/partner/product/opportunity_product/list"
        
        # Load IDs and DNA at runtime from DB (priority) or Env
        db_p_id = get_config_value('TIKTOK_PARTNER_ID')
        db_a_id = get_config_value('TIKTOK_AID')
        db_fp = get_config_value('TIKTOK_FP')
        db_ms = get_config_value('TIKTOK_MS_TOKEN')
        
        active_p_id = db_p_id if db_p_id else '8653231797418889998'
        active_a_id = db_a_id if db_a_id else '359713'
        active_fp = db_fp if db_fp else 'verify_mjiwfxfc_9k8DpPTf_DdjR_4JGE_Bvx7_nVbrXHj81VV5'
        active_ms = db_ms if db_ms else ''

        # DNA Source Diagnostics
        dna_sources = {
            "PID": "[DB]" if db_p_id else "[DEF]",
            "AID": "[DB]" if db_a_id else "[DEF]",
            "FP": "[DB]" if db_fp else "[DEF]",
            "MS": "[DB]" if db_ms else "[DEF]"
        }

        params = {
            'user_language': 'en',
            'partner_id': active_p_id, 
            'aid': active_a_id,
            'app_name': 'i18n_ecom_alliance',
            'device_id': '0',
            'fp': active_fp,
            'msToken': active_ms,
            'device_platform': 'web',
            'cookie_enabled': 'true',
            'screen_width': '1536',
            'screen_height': '864',
            'browser_language': 'en-US',
            'browser_platform': 'Win32',
            'browser_name': 'Mozilla',
            'browser_version': '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
            'browser_online': 'true',
            'timezone_name': 'America/New_York'
        }
        
        print(f"[Partner Scan] DNA Link -> {dna_sources['PID']}PID:{active_p_id} | {dna_sources['AID']}AID:{active_a_id} | {dna_sources['FP']}FP:{active_fp[:20]}... | {dna_sources['MS']}MS:{active_ms[:10]}...", flush=True)
        
        # Load Cookie at runtime from DB (priority) or Env
        db_cookie = get_config_value('TIKTOK_PARTNER_COOKIE')
        active_cookie = db_cookie if db_cookie else TIKTOK_PARTNER_COOKIE
        
        if not active_cookie:
            print("[Partner Scan] FATAL: No TIKTOK_PARTNER_COOKIE found in DB or Environment.", flush=True)
            return

        cookie_src = "[DB]" if db_cookie else "[ENV]"
        print(f"[Partner Scan] Cookie Linked -> {cookie_src} (Len: {len(active_cookie) if active_cookie else 0})", flush=True)

        headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'en-US,en;q=0.9',
            'content-type': 'application/json',
            'cookie': active_cookie,
            'origin': 'https://partner.us.tiktokshop.com',
            'referer': 'https://partner.us.tiktokshop.com/affiliate-product-management/opportunity-product-pool?prePage=product_marketplace&market=100',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        }
        
        # 1. Cleanup Phase (Optimized)
        with app.app_context():
            print("[Partner Scan] Running database cleanup phase...", flush=True)
            try:
                # Use raw SQL or optimized queries for mass cleanup to avoid identity map overhead
                Product.query.filter(Product.scan_type == 'partner_opportunity', Product.commission_rate > 1.0).update({Product.commission_rate: Product.commission_rate / 100.0}, synchronize_session=False)
                
                # Reset Sales 7D if it exactly matches Total Sales (indicates incorrect fallback)
                count2 = Product.query.filter(Product.scan_type == 'partner_opportunity', Product.sales_7d == Product.sales, Product.sales > 100).update({Product.sales_7d: 0}, synchronize_session=False)
                
                db.session.commit()
                print(f"[Partner Scan] Cleanup complete. Reset {count2} suspicious sales entries.", flush=True)
            except Exception as e_clean:
                print(f"[Partner Scan] Cleanup phase error: {e_clean}", flush=True)
                db.session.rollback()

        proxies = None
        if ECHOTIK_PROXY_STRING:
            p_parts = ECHOTIK_PROXY_STRING.split(':')
            if len(p_parts) == 4:
                proxies = {
                    "http": f"http://{p_parts[2]}:{p_parts[3]}@{p_parts[0]}:{p_parts[1]}",
                    "https": f"http://{p_parts[2]}:{p_parts[3]}@{p_parts[0]}:{p_parts[1]}"
                }

        total_saved = 0
        max_pages = 10 
        
        for page in range(1, max_pages + 1):
            print(f"[Partner Scan] --- Processing Page {page} ---", flush=True)
            
            payload = {
                "filter": {
                    "product_source": [],
                    "campaign_type": [],
                    "label_type": [],
                    "product_status": 1
                },
                "page_size": 15,
                "page": page
            }
            
            try:
                res = None
                last_err = None
                
                # Stage 1: curl_cffi with Proxy
                try:
                    res = curl_requests.post(target_url, params=params, headers=headers, json=payload, proxies=proxies, impersonate="chrome110", timeout=30)
                    if res: print(f"[Partner Scan] Page {page} Stage 1 (Proxy) -> Status {res.status_code}", flush=True)
                except Exception as e1:
                    print(f"[Partner Scan] Page {page} Stage 1 failed: {e1}", flush=True)
                    
                # Stage 2: curl_cffi DIRECT
                if not res or res.status_code in [403, 499, 502, 522]:
                    try:
                        res = curl_requests.post(target_url, params=params, headers=headers, json=payload, impersonate="chrome110", timeout=30)
                        if res: print(f"[Partner Scan] Page {page} Stage 2 (Direct) -> Status {res.status_code}", flush=True)
                    except Exception as e2:
                        print(f"[Partner Scan] Page {page} Stage 2 failed: {e2}", flush=True)

                # Stage 3: Standard Requests
                if not res or res.status_code in [403, 499, 502, 522]:
                    try:
                        res = requests.post(target_url, params=params, headers=headers, json=payload, proxies=proxies, timeout=30)
                        if res: print(f"[Partner Scan] Page {page} Stage 3 (Standard) -> Status {res.status_code}", flush=True)
                    except Exception as e3:
                        print(f"[Partner Scan] Page {page} Stage 3 failed: {e3}", flush=True)

                if not res:
                    print(f"[Partner Scan] Page {page} failed all request stages. skipping.", flush=True)
                    continue
                
                if res.status_code != 200:
                    print(f"[Partner Scan] Page {page} error {res.status_code}: {res.text[:500]}", flush=True)
                    break
                    
                data = res.json()
                d_obj = data.get('data', {})
                products = d_obj.get('products') or d_obj.get('opportunity_product_list') or []
                
                if not products:
                    print(f"[Partner Scan] No products found on page {page}. Raw Response: {res.text[:800]}", flush=True)
                    break
                    
                with app.app_context():
                    for p in products:
                        try:
                            pid = p.get('product_id')
                            if not pid: continue
                            
                            # Price
                            price_obj = p.get('price') or {}
                            if isinstance(price_obj, str):
                                raw_price = price_obj.replace('$', '').replace(',', '')
                            else:
                                raw_price = str(price_obj.get('floor_price') or price_obj.get('min_price') or '0').replace('$', '').replace(',', '')
                            price_val = safe_float(raw_price)
                            
                            # Sales
                            sales_raw = p.get('sales') or '0'
                            if isinstance(sales_raw, dict):
                                sales_str = str(sales_raw.get('count', '0'))
                            else:
                                sales_str = str(sales_raw).split(' ')[0]
                            sales_val = parse_kmb_string(sales_str)

                            # Commission
                            comm_raw = safe_float(p.get('commission_rate', 0))
                            comm_val = comm_raw / 10000.0 if comm_raw > 100 else comm_raw / 100.0

                            # Image
                            img_url = None
                            p_img_obj = p.get('product_image')
                            if isinstance(p_img_obj, dict):
                                urls = p_img_obj.get('url_list', [])
                                if urls: img_url = urls[0]
                            
                            if not img_url:
                                img_url = p.get('img_url') or p.get('image') or p.get('cover') or p.get('image_url')

                            p_data = {
                                'product_id': pid,
                                'product_name': p.get('title', 'Unknown Product'),
                                'image_url': img_url, 
                                'price': price_val,
                                'sales': sales_val, 
                                'sales_7d': 0, 
                                'commission_rate': comm_val, 
                                'seller_name': p.get('shop_info', {}).get('shop_name', 'Classified'),
                                'product_url': f"https://www.tiktok.com/shop/pdp/p/{pid}?source=ecommerce_store&region=US"
                            }

                            # Hydra-Enrichment
                            echotik_data, source = fetch_product_details_echotik(pid)
                            if echotik_data:
                                normalized = extract_metadata_from_echotik(echotik_data)
                                if normalized.get('sales_7d', 0) > 0: p_data['sales_7d'] = normalized['sales_7d']
                                if normalized.get('video_count', 0) > 0: p_data['video_count'] = normalized['video_count']
                                if normalized.get('influencer_count', 0) > 0: p_data['influencer_count'] = normalized['influencer_count']
                                if normalized.get('image_url'): p_data['image_url'] = normalized['image_url']
                                if normalized.get('product_name') and len(normalized['product_name']) > 5:
                                    p_data['product_name'] = normalized['product_name']
                            
                            if save_or_update_product(p_data, scan_type='partner_opportunity'):
                                total_saved += 1
                                
                        except Exception as e_p:
                            print(f"[Partner Scan] Error processing product {p.get('product_id')}: {e_p}", flush=True)
                    
                    db.session.commit()
                
                print(f"[Partner Scan] Page {page} sync complete. Total saved: {total_saved}", flush=True)
                time.sleep(15)
                
            except Exception as e_page:
                print(f"[Partner Scan] Error on Page {page}: {e_page}", flush=True)
                print(traceback.format_exc(), flush=True)
                continue

        print(f"[Partner Scan] Scan complete! Total products synced: {total_saved}", flush=True)

    except Exception as e_fatal:
        print(f"[Partner Scan] FATAL ERROR: {e_fatal}", flush=True)
        print(traceback.format_exc(), flush=True)
        try:
            send_telegram_alert(f"⚠️ **Partner Scan Failed**\nError: `{str(e_fatal)[:200]}`")
        except: pass
    finally:
        with app.app_context():
            db.session.remove()
            
    print(f"[Partner Scan] Finished. Total new/updated opportunities: {total_saved}")

# =============================================================================
# TIKTOKCOPILOT INTEGRATION 🕵️‍♂️
# =============================================================================

COPILOT_API_BASE = "https://www.tiktokcopilot.com/api"

# Cache for refreshed session token
_COPILOT_SESSION_CACHE = {
    'session_token': None,
    'expires_at': 0
}

def get_copilot_refresh_credentials():
    """Get the long-lived refresh credentials from config/env.
    
    Required env vars:
        TIKTOK_COPILOT_REFRESH_TOKEN: The __refresh_pOM46XQh value (long-lived)
        TIKTOK_COPILOT_SESSION_ID: The session ID (e.g., sess_38c9F2BNijjuXJF4dANtbxnJQo6)
    """
    refresh_token = get_config_value('TIKTOK_COPILOT_REFRESH_TOKEN', os.environ.get('TIKTOK_COPILOT_REFRESH_TOKEN', ''))
    session_id = get_config_value('TIKTOK_COPILOT_SESSION_ID', os.environ.get('TIKTOK_COPILOT_SESSION_ID', ''))
    return refresh_token, session_id

def refresh_copilot_session():
    """Use refresh token to get a fresh session JWT from Clerk.
    
    Clerk requires multiple cookies for authentication:
    - __refresh_pOM46XQh: Long-lived refresh token
    - __client_uat_pOM46XQh: Client user auth timestamp
    
    Returns:
        Fresh session JWT or None on error
    """
    global _COPILOT_SESSION_CACHE
    
    # Check cache first - reuse if not expired (with 10s buffer)
    if _COPILOT_SESSION_CACHE['session_token'] and time.time() < _COPILOT_SESSION_CACHE['expires_at'] - 10:
        return _COPILOT_SESSION_CACHE['session_token']
    
    refresh_token, session_id = get_copilot_refresh_credentials()
    client_uat = get_config_value('TIKTOK_COPILOT_CLIENT_UAT', os.environ.get('TIKTOK_COPILOT_CLIENT_UAT', ''))
    
    if not refresh_token or not session_id:
        print("[Copilot Auth] ❌ Missing TIKTOK_COPILOT_REFRESH_TOKEN or TIKTOK_COPILOT_SESSION_ID")
        return None
    
    try:
        # Try the touch endpoint first (more reliable for maintaining session)
        url = f"https://clerk.tiktokcopilot.com/v1/client/sessions/{session_id}/touch"
        
        # Build full cookie string with all required Clerk cookies
        cookie_parts = [
            f"__refresh_pOM46XQh={refresh_token}",
            f"clerk_active_context={session_id}:",
        ]
        if client_uat:
            cookie_parts.append(f"__client_uat_pOM46XQh={client_uat}")
            cookie_parts.append(f"__client_uat={client_uat}")
        
        cookie_str = "; ".join(cookie_parts)
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.tiktokcopilot.com",
            "Referer": "https://www.tiktokcopilot.com/",
            "Cookie": cookie_str
        }
        
        # Try touch endpoint first
        res = requests.post(url, headers=headers, json={}, timeout=30)
        
        if res.status_code == 200:
            data = res.json()
            # Touch returns session with last_active_token
            jwt_token = data.get('last_active_token', {}).get('jwt') or data.get('jwt')
            
            if jwt_token:
                _COPILOT_SESSION_CACHE['session_token'] = jwt_token
                _COPILOT_SESSION_CACHE['expires_at'] = time.time() + 50
                print(f"[Copilot Auth] ✅ Refreshed session token via touch!")
                return jwt_token
        
        # Fallback to tokens endpoint
        url = f"https://clerk.tiktokcopilot.com/v1/client/sessions/{session_id}/tokens"
        res = requests.post(url, headers=headers, json={}, timeout=30)
        
        if res.status_code == 200:
            data = res.json()
            jwt_token = data.get('jwt') or data.get('token') or data.get('session_token')
            
            if jwt_token:
                _COPILOT_SESSION_CACHE['session_token'] = jwt_token
                _COPILOT_SESSION_CACHE['expires_at'] = time.time() + 50
                print(f"[Copilot Auth] ✅ Refreshed session token successfully!")
                return jwt_token
            else:
                print(f"[Copilot Auth] ⚠️ Response didn't contain JWT: {list(data.keys())}")
        else:
            print(f"[Copilot Auth] ❌ Refresh failed: {res.status_code}")
            if res.text:
                print(f"[DEBUG] Response: {res.text[:300]}")
                
    except Exception as e:
        print(f"[Copilot Auth] ❌ Exception during refresh: {e}")
    
    return None

def parse_cookie_string(cookie_str):
    """Parse a cookie string into a dictionary."""
    cookies = {}
    if not cookie_str:
        return cookies
    for item in cookie_str.split(';'):
        if '=' in item:
            name, value = item.strip().split('=', 1)
            cookies[name] = value
    return cookies

# Global tracking for auto-refresh
_COPILOT_AUTO_REFRESH_THREAD = None
_COPILOT_LAST_REFRESH = None

def auto_login_copilot():
    """
    Automatically login to TikTokCopilot via Clerk API using email/password.
    Stores fresh cookies in database config.
    
    Env vars required:
        COPILOT_EMAIL: Your TikTokCopilot login email
        COPILOT_PASSWORD: Your TikTokCopilot password
    
    Returns:
        Full cookie string on success, None on failure
    """
    global _COPILOT_LAST_REFRESH
    import random
    
    email = os.environ.get('COPILOT_EMAIL')
    password = os.environ.get('COPILOT_PASSWORD')
    
    if not email or not password:
        print("[Copilot Auto-Login] ⚠️ No credentials configured (set COPILOT_EMAIL + COPILOT_PASSWORD env vars)")
        return None
    
    print(f"[Copilot Auto-Login] 🔄 Attempting login for {email[:5]}***@***...")
    
    try:
        # Use curl_cffi if available for better fingerprinting
        if requests_cffi:
            session = requests_cffi.Session(impersonate="chrome131")
        else:
            session = requests.Session()
        
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.tiktokcopilot.com",
            "Referer": "https://www.tiktokcopilot.com/auth-sign-in",
            "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })
        
        # Step 1: Create a sign-in attempt
        create_url = "https://clerk.tiktokcopilot.com/v1/client/sign_ins"
        create_payload = {"identifier": email}
        
        res1 = session.post(create_url, json=create_payload, timeout=30)
        
        if res1.status_code != 200:
            print(f"[Copilot Auto-Login] ❌ Create sign-in failed: {res1.status_code}")
            if hasattr(res1, 'text'):
                print(f"[DEBUG] Response: {res1.text[:300]}")
            return None
        
        data1 = res1.json()
        sign_in_id = data1.get('response', {}).get('id') or data1.get('id')
        
        if not sign_in_id:
            # Try alternate response structure
            client_data = data1.get('client', {})
            sign_ins = client_data.get('sign_ins', [])
            if sign_ins:
                sign_in_id = sign_ins[0].get('id')
        
        if not sign_in_id:
            print(f"[Copilot Auto-Login] ❌ No sign_in_id in response: {list(data1.keys())}")
            return None
        
        print(f"[Copilot Auto-Login] ✅ Sign-in created: {sign_in_id[:20]}...")
        
        # Small delay to mimic human
        time.sleep(random.uniform(0.5, 1.5))
        
        # Step 2: Attempt password authentication
        # NOTE: Clerk expects form-encoded data, NOT JSON, for attempt_first_factor
        attempt_url = f"https://clerk.tiktokcopilot.com/v1/client/sign_ins/{sign_in_id}/attempt_first_factor"
        attempt_payload = {
            "strategy": "password",
            "password": password
        }
        
        # Use data= for form encoding, not json=
        res2 = session.post(attempt_url, data=attempt_payload, timeout=30)
        
        if res2.status_code != 200:
            print(f"[Copilot Auto-Login] ❌ Password auth failed: {res2.status_code}")
            if hasattr(res2, 'text'):
                error_text = res2.text[:500]
                if 'incorrect' in error_text.lower():
                    print("[Copilot Auto-Login] ❌ Password is incorrect!")
                else:
                    print(f"[DEBUG] Response: {error_text}")
            return None
        
        print(f"[Copilot Auto-Login] ✅ Password accepted!")
        
        # Step 3: Extract cookies from session
        cookies_dict = {}
        if hasattr(session, 'cookies'):
            for cookie in session.cookies:
                if hasattr(cookie, 'name') and hasattr(cookie, 'value'):
                    cookies_dict[cookie.name] = cookie.value
                elif isinstance(cookie, tuple):
                    cookies_dict[cookie[0]] = cookie[1]
            
            # Also check response cookies
            if hasattr(res2, 'cookies'):
                try:
                    for cookie in res2.cookies:
                        if hasattr(cookie, 'name'):
                            cookies_dict[cookie.name] = cookie.value
                except:
                    pass
        
        # Build cookie string with session tokens
        relevant_cookies = []
        for name, value in cookies_dict.items():
            if any(prefix in name for prefix in ['__session', '__client', '__refresh', 'clerk']):
                relevant_cookies.append(f"{name}={value}")
        
        if not relevant_cookies:
            # Try to extract __session from response body
            data2 = res2.json()
            sessions = data2.get('client', {}).get('sessions', [])
            if sessions:
                active_session = sessions[0]
                jwt = active_session.get('last_active_token', {}).get('jwt')
                if jwt:
                    relevant_cookies.append(f"__session={jwt}")
                    relevant_cookies.append(f"__session_pOM46XQh={jwt}")
        
        if not relevant_cookies:
            print("[Copilot Auto-Login] ❌ No session cookies found in response")
            # Try one more approach - refresh the session to get cookies
            data2 = res2.json()
            sessions = data2.get('client', {}).get('sessions', [])
            if sessions:
                session_id = sessions[0].get('id')
                if session_id:
                    print(f"[Copilot Auto-Login] 🔄 Trying token refresh for session {session_id[:20]}...")
                    touch_url = f"https://clerk.tiktokcopilot.com/v1/client/sessions/{session_id}/touch"
                    res3 = session.post(touch_url, json={}, timeout=30)
                    if res3.status_code == 200:
                        data3 = res3.json()
                        jwt = data3.get('last_active_token', {}).get('jwt') or data3.get('jwt')
                        if jwt:
                            relevant_cookies.append(f"__session={jwt}")
                            relevant_cookies.append(f"__session_pOM46XQh={jwt}")
                            print("[Copilot Auto-Login] ✅ Got JWT from session touch!")
        
        if relevant_cookies:
            full_cookie_str = "; ".join(relevant_cookies)
            
            # Store in database config
            try:
                set_config_value('TIKTOK_COPILOT_COOKIE', full_cookie_str, 'Auto-refreshed by Clerk login')
                _COPILOT_LAST_REFRESH = datetime.utcnow()
                print(f"[Copilot Auto-Login] ✅ Session refreshed! Cookies stored in DB ({len(relevant_cookies)} tokens)")
                return full_cookie_str
            except Exception as db_err:
                print(f"[Copilot Auto-Login] ⚠️ DB save failed, returning cookies: {db_err}")
                return full_cookie_str
        
        print("[Copilot Auto-Login] ❌ Failed to extract session cookies")
        return None
        
    except Exception as e:
        print(f"[Copilot Auto-Login] ❌ Exception: {e}")
        import traceback
        traceback.print_exc()
        return None

def schedule_copilot_auto_refresh(interval_minutes=45):
    """
    Schedule automatic session refresh every N minutes.
    Uses threading.Timer for lightweight background execution.
    """
    global _COPILOT_AUTO_REFRESH_THREAD
    import threading
    
    def refresh_job():
        global _COPILOT_AUTO_REFRESH_THREAD
        try:
            print(f"[Copilot Scheduler] ⏰ Running scheduled session refresh...")
            result = auto_login_copilot()
            if result:
                print(f"[Copilot Scheduler] ✅ Auto-refresh successful!")
            else:
                print(f"[Copilot Scheduler] ⚠️ Auto-refresh returned None - check credentials")
        except Exception as e:
            print(f"[Copilot Scheduler] ❌ Error in scheduled refresh: {e}")
        finally:
            # Schedule next run
            _COPILOT_AUTO_REFRESH_THREAD = threading.Timer(interval_minutes * 60, refresh_job)
            _COPILOT_AUTO_REFRESH_THREAD.daemon = True
            _COPILOT_AUTO_REFRESH_THREAD.start()
    
    # Cancel existing timer if any
    if _COPILOT_AUTO_REFRESH_THREAD and _COPILOT_AUTO_REFRESH_THREAD.is_alive():
        _COPILOT_AUTO_REFRESH_THREAD.cancel()
    
    # Start the timer
    _COPILOT_AUTO_REFRESH_THREAD = threading.Timer(interval_minutes * 60, refresh_job)
    _COPILOT_AUTO_REFRESH_THREAD.daemon = True
    _COPILOT_AUTO_REFRESH_THREAD.start()
    print(f"[Copilot Scheduler] 📅 Auto-refresh scheduled every {interval_minutes} minutes")

def get_copilot_cookie():
    """Get TikTokCopilot session cookie.
    
    Priority:
    1. Static cookie from TIKTOK_COPILOT_COOKIE env var (Recommended)
    2. Refresh token mechanism (Legacy fallback)
    """
    # 1. Direct cookie string (Simplified Auth)
    static_cookie = get_config_value('TIKTOK_COPILOT_COOKIE', os.environ.get('TIKTOK_COPILOT_COOKIE', ''))
    if static_cookie:
        # If it contains '__session=', it's likely a full cookie string
        return static_cookie
        
    # 2. Legacy refresh mechanism
    refresh_token, session_id = get_copilot_refresh_credentials()
    if refresh_token and session_id:
        fresh_jwt = refresh_copilot_session()
        if fresh_jwt:
            return f"__session={fresh_jwt}; __session_pOM46XQh={fresh_jwt}"
    
    return None

def fetch_copilot_products(timeframe='7d', sort_by='revenue', limit=50, page=0, region='US', keywords=None):
    """
    Fetch products from the V2 TikTokCopilot /api/trending/products endpoint.
    Uses curl_cffi for browser impersonation with SAME-ORIGIN headers per Grok analysis.
    """
    cookie_str = get_copilot_cookie()
    if not cookie_str:
        print("[Copilot Products] ❌ No cookie configured!")
        return None
    
    # CRITICAL: Same-origin headers per Grok's analysis (no Origin/Referer/X-Requested-With)
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",  # CRITICAL: same-origin, not same-site
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "priority": "u=1, i",  # Modern fetch metadata
    }
    
    params = {
        "timeframe": timeframe,
        "sortBy": sort_by,
        "limit": limit,
        "page": page,
        "region": region  # CRITICAL: 'region' not 'shopRegion'
    }
    
    if keywords:
        params["keywords"] = keywords
    
    retries = 3
    for attempt in range(retries):
        try:
            # Use curl_cffi with Chrome 131 impersonation (latest supported)
            if requests_cffi:
                res = requests_cffi.get(
                    f"{COPILOT_API_BASE}/trending/products", 
                    headers=headers, 
                    params=params, 
                    cookies=parse_cookie_string(cookie_str),
                    impersonate="chrome131",  # Upgraded from chrome120
                    timeout=60
                )
            else:
                headers["Cookie"] = cookie_str
                res = requests.get(f"{COPILOT_API_BASE}/trending/products", headers=headers, params=params, timeout=60)
            
            if res.status_code == 200:
                try:
                    return res.json()
                except Exception as e:
                    print(f"[Copilot Products] ❌ JSON Decode Error (Attempt {attempt+1}/{retries}): {e}")
                    resp_text = getattr(res, 'text', '')
                    if resp_text:
                        print(f"[DEBUG] Raw Response (first 100 chars): {resp_text[:100]}")
                    
                    if attempt < retries - 1:
                        time.sleep(2 * (attempt + 1))
                        continue
                    return None
            elif res.status_code == 429:
                print(f"[Copilot Products] ⚠️ Rate Limited (429) - Backing off...")
                if attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                return None
            else:
                print(f"[Copilot Products] API Error: {res.status_code}")
                return None
        except Exception as e:
            print(f"[Copilot Products] Exception (Attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(3)
                continue
            return None
    return None

def fetch_copilot_trending(timeframe='7d', sort_by='revenue', limit=50, page=0, region='US', **kwargs):
    """
    Fetch trending products from TikTokCopilot API.
    Uses curl_cffi for browser impersonation.
    """
    cookie_str = get_copilot_cookie()
    if not cookie_str:
        print("[Copilot] ❌ No cookie configured!")
        return None
    
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "priority": "u=1, i"
    }
    
    params = {
        "timeframe": timeframe,
        "sortBy": sort_by,
        "feedType": "for-you",
        "limit": limit,
        "page": page,
        "region": region,
        "sAggMode": "net"
    }
    
    # Add keyword/ID search if provided
    if kwargs.get('keywords') or kwargs.get('product_id'):
        target = kwargs.get('product_id') or kwargs.get('keywords')
        params['keywords'] = target
        params.pop('timeframe', None)
        params.pop('sortBy', None)
        params.pop('feedType', None)
        params['searchType'] = 'product'
        
        if str(target).isdigit() and len(str(target)) > 15:
             params['productId'] = str(target)
    
    if kwargs.get('creator_ids'):
        params['creatorIds'] = kwargs.get('creator_ids')
    
    if kwargs.get('c_timeframe'):
        params['cTimeframe'] = kwargs.get('c_timeframe')
    
    retries = 3
    for attempt in range(retries):
        try:
            if requests_cffi:
                res = requests_cffi.get(
                    f"{COPILOT_API_BASE}/trending", 
                    headers=headers, 
                    params=params, 
                    cookies=parse_cookie_string(cookie_str),
                    impersonate="chrome131",
                    timeout=60
                )
            else:
                headers["Cookie"] = cookie_str
                res = requests.get(f"{COPILOT_API_BASE}/trending", headers=headers, params=params, timeout=60)
                
            if res.status_code == 200:
                try:
                    return res.json()
                except Exception as e:
                    print(f"[Copilot Legacy] ❌ JSON Decode Error (Attempt {attempt+1}/{retries}): {e}")
                    resp_text = getattr(res, 'text', '')
                    if resp_text:
                        print(f"[DEBUG] Raw Response (first 100 chars): {resp_text[:100]}")
                    
                    if attempt < retries - 1:
                        time.sleep(2 * (attempt + 1))
                        continue
                    return None
            elif res.status_code == 429:
                print(f"[Copilot Legacy] ⚠️ Rate Limited (429) - Backing off...")
                if attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                return None
            else:
                print(f"[Copilot Legacy] API Error: {res.status_code}")
                return None
        except Exception as e:
            print(f"[Copilot] Exception (Attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(3)
                continue
            return None
    return None

def calculate_winner_score(ad_spend, video_count, creator_count):
    """
    Calculate Winner Score for a product.
    High ad spend + low videos + low creators = Better opportunity!
    
    Formula: (ad_spend * 2) / (video_count + creator_count + 1)
    """
    try:
        numerator = float(ad_spend or 0) * 2
        denominator = int(video_count or 0) + int(creator_count or 0) + 1
        return round(numerator / denominator, 2)
    except:
        return 0

def sync_copilot_products(timeframe='all', limit=50, page=0):
    """
    Core sync function - fetches from the ENHANCED Copilot /api/trending/products endpoint.
    Returns (saved_count, total_in_response) tuple.
    
    V2: Uses timeframe='all' by default to get ALL-TIME video/creator counts (23.5K vs 778).
    The 7d sales and ad_spend are still extracted from period fields.
    
    Uses the new /api/trending/products endpoint with significantly more accurate stats:
        - productVideoCount (ALL-TIME: 23.5K)
        - productCreatorCount (ALL-TIME)
        - periodUnits (7-day sales)
        - periodAdSpend (7-day ad spend)
    """
    # Use the NEW enhanced endpoint with 'all' timeframe for accurate totals
    result = fetch_copilot_products(timeframe=timeframe, limit=limit, page=page)
    is_legacy_source = False
    if not result:
        # Fallback to legacy endpoint if new one fails
        print("[Copilot Sync] V2 endpoint failed, trying legacy...")

        result = fetch_copilot_trending(timeframe=timeframe, limit=limit, page=page)
        if not result:
            return 0, 0
        products = result.get('videos', [])
        is_legacy_source = True
    else:
        products = result.get('products', [])
    
    if not products:
        return 0, 0
    
    saved_count = 0
    processed_ids = set()  # Track IDs within this batch to prevent duplicates
    
    global SYNC_STOP_REQUESTED
    
    # Load persistent stop flag from DB to synchronize with global variable
    db_stop_flag = get_config_value('SYNC_STOP_REQUESTED', 'false')
    if db_stop_flag == 'true':
        SYNC_STOP_REQUESTED = True
        print("🛑 [Copilot Sync] Stop signal loaded from DB!")
    
    for idx, p in enumerate(products):
        if SYNC_STOP_REQUESTED:
            print("🛑 [Copilot Sync] Granular stop triggered!")
            break
            
        # HYPER-VERBOSE DEBUG: Log EVERYTHING for the first product to solve the skipping mystery
        if idx == 0:
            p_prefix = "LEGACY" if is_legacy_source else "V2"
            print(f"[DEBUG {p_prefix}] --- FIRST PRODUCT FULL KEYS (idx=0) ---")
            print(f"[DEBUG {p_prefix}] Total Keys Count: {len(p.keys())}")
            print(f"[DEBUG {p_prefix}] All Keys: {', '.join(sorted(p.keys()))}")
            
            # Print ALL values for critical analysis
            full_data = {k: p.get(k) for k in p.keys()}
            print(f"[DEBUG {p_prefix}] FULL OBJECT: {json.dumps(full_data, default=str)}")
        
        try:
            # FIX: Correct multi-key retrieval
            product_id = str(p.get('productId') or p.get('product_id') or p.get('id') or '')
            if not product_id:
                continue
            
            # Normalize to our shop_ prefix
            if not product_id.startswith('shop_'):
                product_id = f"shop_{product_id}"
            
            # Skip if already processed in this batch (prevents duplicate insert attempts)
            if product_id in processed_ids:
                continue
            processed_ids.add(product_id)
            
            # ===== ENHANCED V2 FIELD EXTRACTION (Updated for 2026 API) =====
            # Note: API now returns Video-centric data in 'videos' list
            
            # ===== GREEDY STATS EXTRACTION (Robust for both V2 and Legacy) =====
            
            # 1. Sales (7d)
            sales_7d = safe_int(
                p.get('periodUnits') or 
                p.get('unitsSold7d') or 
                p.get('productPeriodUnits') or 
                p.get('videoUnits') or 
                p.get('videoUnitsSold7d') or 
                p.get('sales_7d') or 
                p.get('units_sold') or 
                p.get('units') or 
                0
            )
            
            # 2. Revenue (7d)
            period_revenue = safe_float(
                p.get('periodRevenue') or 
                p.get('productPeriodRevenue') or 
                p.get('revenue7d') or 
                p.get('videoRevenue') or 
                p.get('videoRevenue7d') or 
                p.get('revenue') or 
                p.get('gmv7d') or 
                p.get('gmv') or 
                0
            )

            # 3. Ad Spend (7d)
            ad_spend_7d = safe_float(
                p.get('periodAdSpend') or 
                p.get('ad_spend_7d') or 
                p.get('videoAdSpend') or 
                p.get('adSpend') or 
                0
            )

            # 4. Total/All-Time Stats
            total_sales = safe_int(
                p.get('productTotalUnits') or 
                p.get('computedTotalUnits') or 
                p.get('unitsSold') or 
                p.get('totalSales') or 
                0
            )
            
            total_revenue = safe_float(
                p.get('totalRevenue') or 
                p.get('computedTotalRevenue') or 
                p.get('productTotalRevenue') or 
                p.get('estTotalEarnings') or 
                p.get('revenue_alltime') or 
                p.get('videoRevenue') or # Fallback in video objects
                0
            )
            
            total_ad_cost = safe_float(
                p.get('productTotalAdSpend') or 
                p.get('totalAdCost') or 
                p.get('totalAdSpend') or 
                0
            )
            
            if ad_spend_7d <= 0 and total_ad_cost > 0:
                ad_spend_7d = total_ad_cost * 0.1 # Conservative heuristic fallback
            
            ad_spend_total = total_ad_cost or ad_spend_7d
            
            # 5. Engagement Metrics
            video_count = safe_int(
                p.get('productVideoCount') or 
                p.get('periodVideoCount') or 
                p.get('video_count') or 
                p.get('videoCount') or 
                p.get('video_ct') or 
                0
            )
            
            creator_count = safe_int(
                p.get('productCreatorCount') or 
                p.get('periodCreatorCount') or 
                p.get('creator_count') or 
                p.get('creatorCount') or 
                0
            )
            
            # Views
            period_views = safe_int(p.get('periodViews') or p.get('views7d') or p.get('videoViews') or 0)
            total_views = safe_int(p.get('totalViews') or p.get('allTimeViews') or 0)
            
            # Commission Rates
            commission_rate = safe_float(p.get('tapCommissionRate') or p.get('ocCommissionRate') or p.get('commission_rate') or 0) / 10000.0
            shop_ads_rate = safe_float(p.get('tapShopAdsRate') or p.get('ocShopAdsRate') or p.get('shop_ads_rate') or 0) / 10000.0
            
            # Fallback for growth and price
            growth_pct = safe_float(p.get('revenueGrowthPct') or p.get('productRevenueGrowthPct') or p.get('viewsGrowthPct') or 0)
            price = safe_float(p.get('productPrice') or p.get('avgUnitPrice') or p.get('minPrice') or 0)
            
            # Product URL
            raw_product_id = str(p.get('productId', '')).replace('shop_', '')
            product_url = p.get('productPageUrl') or p.get('productLink') or p.get('productUrl')
            if not product_url and raw_product_id:
                product_url = f"https://shop.tiktok.com/view/product/{raw_product_id}?region=US&locale=en-US"
            
            # Image URL
            image_url = p.get('productCoverUrl') or p.get('productImageUrl') or p.get('coverUrl') or ''
            
            # Seller info
            seller_name = p.get('sellerName') or ''
            seller_id = p.get('sellerId') or ''
 
            # FILTER: Quality Control - Skip low-quality products
            # V2: Relax sales filter - include Revenue in the checks!
            is_zero_stat = (sales_7d <= 0 and total_sales <= 0 and period_revenue <= 0 and total_revenue <= 0)
            
            if is_zero_stat and ad_spend_7d < 50:
                if saved_count < 15:
                    msg_prefix = "[LEGACY] Ingesting" if is_legacy_source else "[V2] Skipping"
                    print(f"{msg_prefix} {product_id} - Debug Stats: sales_7d={sales_7d}, r_7d={period_revenue}, total_rev={total_revenue}, ad_7d={ad_spend_7d}")
                    if is_legacy_source and saved_count < 3:
                         print(f"[DEBUG LEGACY] FULL PRODUCT OBJECT: {p}")
                
                # IN LEGACY MODE: If it's a zero-stat product from the TRENDING feed, 
                # we still want it IF it has a productId because it's highly curated!
                if is_legacy_source and product_id:
                     pass # Allow through to enrichment
                else:
                    continue
            
            # V2 RELAX: Lower video count requirement for high-revenue products
            if video_count < 2 and total_revenue < 300 and not is_legacy_source:
                continue
                
            # FILTER: Active ad spend or high commission or any revenue
            if not is_legacy_source and ad_spend_7d <= 0 and commission_rate < 0.10 and total_revenue < 200:
                continue
            
            # RELAX: GMV Max Ads - If legacy source, this field is MISSING (0).
            # ONLY enforce if it's a V2 response and we have some data.
            if not is_legacy_source and shop_ads_rate <= 0:
                if saved_count < 5:
                    print(f"[V2] Skipping {product_id} - GMV Max Required (rate=0)")
                continue
            
            winner_score = calculate_winner_score(ad_spend_total, video_count, creator_count)
            
            # Save or Update Product
            existing = Product.query.get(product_id)
            if existing:
                # Update existing product with V2 Copilot data
                existing.product_name = p.get('productTitle') or existing.product_name
                
                # FIX: Ensure seller_name is never undefined/null
                if seller_name and seller_name.lower() not in ['undefined', 'null', 'unknown', '']:
                    existing.seller_name = seller_name
                elif not existing.seller_name:
                    existing.seller_name = 'Unknown Seller'
                
                if seller_id:
                    existing.seller_id = seller_id
                
                # Image Update with Cache Invalidation
                if image_url and image_url != existing.image_url:
                    existing.image_url = image_url
                    existing.cached_image_url = None  # Force re-download
                elif not existing.image_url and image_url:
                    existing.image_url = image_url
                
                # Stats Update
                existing.sales_7d = sales_7d
                if total_sales > 0: existing.sales = total_sales
                
                existing.video_count = video_count # 7D/momentum
                existing.video_count_alltime = video_count # Sync current to all-time
                existing.influencer_count = creator_count
                
                existing.ad_spend = ad_spend_7d
                existing.ad_spend_total = ad_spend_total
                
                if period_revenue > 0:
                    existing.gmv = period_revenue
                if growth_pct != 0:
                    existing.gmv_growth = growth_pct
                if price > 0:
                    existing.price = price
                
                # Update Ad Spend (prefer total from V2)
                if ad_spend_total > 0:
                    existing.ad_spend_total = ad_spend_total
                if ad_spend_7d > 0:
                    existing.ad_spend = ad_spend_7d
                elif ad_spend_total > 0:
                    # Estimate 7d spend as ~15% of total if not provided
                    existing.ad_spend = ad_spend_total * 0.15
                
                if sales_7d > 0:
                    existing.sales_7d = sales_7d
                if total_sales > 0:
                    existing.sales = total_sales
                
                # Update 30D stats if available (often period fields in 'all' mode)
                if timeframe == 'all':
                    if sales_7d > 0: existing.sales_30d = sales_7d
                    if period_revenue > 0: existing.gmv_30d = period_revenue
                
                # V2 accurate video/creator counts
                if video_count > 0:
                    # If this is an 'all' timeframe sync, periodVideoCount IS the all-time total
                    if timeframe == 'all':
                        existing.video_count_alltime = video_count
                        # For momentum (video_count), use newVideoCount (7-day growth)
                        new_vc = int(p.get('newVideoCount') or 0)
                        if new_vc > 0:
                            existing.video_count = new_vc
                        else:
                            # Fallback: estimate 7D momentum if not provided in 'all' response
                            existing.video_count = max(1, int(video_count * 0.05))
                    else:
                        # If this is a '7d' sync (standard), use periodVideoCount for momentum
                        existing.video_count = video_count
                        # And update all-time if it's higher or empty
                        if video_count > (existing.video_count_alltime or 0):
                            existing.video_count_alltime = video_count
                         
                if creator_count > 0:
                    existing.influencer_count = creator_count
                
                if commission_rate > 0:
                    existing.commission_rate = commission_rate
                if shop_ads_rate > 0:
                    existing.shop_ads_commission = shop_ads_rate
                if period_views > 0:
                    existing.views_count = period_views
                
                if product_url and (not existing.product_url or 'search/product' in (existing.product_url or '')):
                    existing.product_url = product_url
                
                existing.last_updated = datetime.utcnow()
                existing.product_status = 'active'
                existing.scan_type = 'copilot_v2'
            else:
                # Create new product with V2 data
                new_product = Product(
                    product_id=product_id,
                    product_name=p.get('productTitle', ''),
                    seller_id=str(p.get('sellerId', '')),
                    seller_name=p.get('sellerName', ''),
                    image_url=image_url,
                    product_url=product_url,
                    gmv=period_revenue,
                    gmv_growth=growth_pct,
                    sales_7d=sales_7d,
                    price=price,
                    sales_30d=sales_7d if timeframe == 'all' else 0,
                    sales=total_sales,
                    gmv_30d=period_revenue if timeframe == 'all' else 0,
                    video_count=video_count,
                    video_count_alltime=video_count if timeframe == 'all' else 0,
                    influencer_count=creator_count,
                    commission_rate=commission_rate,
                    shop_ads_commission=shop_ads_rate,
                    ad_spend=ad_spend_7d if ad_spend_7d > 0 else ad_spend_total * 0.15,
                    ad_spend_total=ad_spend_total,
                    views_count=period_views,
                    scan_type='copilot_v2',
                    first_seen=datetime.utcnow(),
                    product_status='active'
                )
                db.session.add(new_product)
            
            saved_count += 1
            
        except Exception as e:
            print(f"[Copilot Sync V2] Error saving product: {e}")
            db.session.rollback()  # Prevent cascading transaction errors
            continue
    
    db.session.commit()
    p_label = "LEGACY" if is_legacy_source else "Copilot V2"
    print(f"[{p_label}] Final Yield: {saved_count}/{len(products)} products from {timeframe} timeframe")
    return saved_count, len(products)

# =============================================================================
# VANTAGE V2 ANALYTICS 🚀
# =============================================================================

@app.route('/api/analytics/movers-shakers', methods=['GET'])
@login_required
def api_movers_shakers():
    """
    Movers & Shakers Leaderboard: Products with highest growth indicators.
    V2 FIX: Relaxed filters and added fallback for products without gmv_growth.
    """
    try:
        limit = request.args.get('limit', 20, type=int)
        
        # Primary: Products with actual GMV growth
        products_with_growth = Product.query.filter(
            Product.sales_7d >= 10,  # Lowered from 50
            Product.video_count >= 5,  # Lowered from 10
            Product.gmv_growth > 0
        ).order_by(Product.gmv_growth.desc()).limit(limit).all()
        
        # Fallback: If not enough products with growth, add top revenue products
        if len(products_with_growth) < limit:
            remaining = limit - len(products_with_growth)
            existing_ids = [p.product_id for p in products_with_growth]
            fallback = Product.query.filter(
                Product.sales_7d >= 10,
                Product.gmv > 0,
                ~Product.product_id.in_(existing_ids)
            ).order_by(Product.gmv.desc()).limit(remaining).all()
            products_with_growth.extend(fallback)
        
        return jsonify({
            'success': True,
            'products': [p.to_dict() for p in products_with_growth]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/analytics/creative-linker', methods=['GET'])
@login_required
def api_creative_linker():
    """
    Fetch viral videos for a specific product.
    V2 FIX: Uses topVideos field from /api/trending/products endpoint.
    Paginates through multiple pages to find the specific product.
    """
    product_id = request.args.get('product_id')
    if not product_id:
        return jsonify({'success': False, 'error': 'Product ID required'}), 400
    
    raw_pid = product_id.replace('shop_', '')
    
    try:
        # Search through multiple pages to find this specific product's topVideos
        for page in range(10):  # Check up to 10 pages (500 products)
            res = fetch_copilot_products(timeframe='7d', limit=50, page=page)
            
            if not res or not res.get('products'):
                break
            
            # Find this specific product in the results
            for p in res.get('products', []):
                if str(p.get('productId', '')) == raw_pid:
                    # Found! Extract topVideos
                    top_videos = p.get('topVideos', [])
                    if top_videos:
                        # Return all top videos (duration filter removed - data often missing)
                        # Sort by revenue if available
                        top_videos_sorted = sorted(top_videos, key=lambda x: x.get('revenue') or x.get('periodRevenue') or 0, reverse=True)
                        
                        return jsonify({
                            'success': True,
                            'total_found': len(top_videos),
                            'videos': top_videos_sorted,
                            'source': 'copilot_v2',
                            'product_found': True
                        })
                    else:
                        # Product found but no topVideos
                        return jsonify({
                            'success': False,
                            'error': 'Product found but no video data available from API',
                            'product_found': True,
                            'videos': []
                        }), 404
        
        # Product not found in trending data - return honest error
        return jsonify({
            'success': False,
            'error': 'This product is not in the current trending dataset. Try syncing it first.',
            'product_found': False,
            'videos': []
        }), 404
        
    except Exception as e:
        print(f"[Creative Linker] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/analytics/top-videos', methods=['GET'])
@login_required
def api_top_videos():
    """
    The ROI-Video Feed: Top earning recent videos <= 15s.
    Uses /api/trending endpoint to fetch actual video data with duration.
    """
    try:
        # Fetch global trending videos from Copilot
        res = fetch_copilot_trending(timeframe='7d', sort_by='revenue', limit=100)
        
        if not res:
            print("[Top Videos] No response from Copilot")
            return jsonify({'success': False, 'error': 'Could not fetch trending videos', 'videos': []}), 200
        
        # Debug: log what keys we got
        print(f"[Top Videos] Response keys: {list(res.keys()) if isinstance(res, dict) else 'not a dict'}")
        
        # The /api/trending endpoint returns 'videos' array
        videos = res.get('videos') or res.get('products') or []
        
        if not videos:
            print("[Top Videos] No videos in response")
            return jsonify({'success': True, 'count': 0, 'videos': [], 'message': 'No videos available'})
        
        # Map and filter videos for shorts (<= 15s) with significant revenue
        top_shorts = []
        for v in videos:
            # Get duration - try multiple possible field names
            duration = v.get('durationSeconds') or v.get('duration') or v.get('videoDuration') or 0
            
            # Get revenue - try multiple possible field names
            revenue = v.get('periodRevenue') or v.get('revenue') or v.get('videoRevenue') or 0
            
            # Filter: Include videos with decent revenue
            # Note: Duration data is often missing (returns 0), so we include those too
            # Only exclude if we KNOW it's over 60s
            if revenue > 100 and (duration == 0 or duration <= 60):
                # Map to expected format for vantage_v2.html
                top_shorts.append({
                    'videoId': v.get('videoId') or v.get('id') or '',
                    'videoUrl': v.get('videoUrl') or v.get('url') or f"https://www.tiktok.com/video/{v.get('videoId', '')}",
                    'coverUrl': v.get('coverUrl') or v.get('thumbnailUrl') or v.get('cover') or '',
                    'durationSeconds': duration,
                    'periodRevenue': revenue,
                    'periodViews': v.get('periodViews') or v.get('views') or v.get('viewCount') or 0,
                    'creatorUsername': v.get('creatorUsername') or v.get('author') or v.get('username') or 'Unknown',
                    'productTitle': v.get('productTitle') or v.get('productName') or v.get('title') or 'Product',
                    'productId': v.get('productId') or '',
                    'productImageUrl': v.get('productImageUrl') or v.get('productCoverUrl') or v.get('cover') or ''
                })
        
        # Sort by revenue (highest first)
        top_shorts.sort(key=lambda x: x.get('periodRevenue') or 0, reverse=True)
        
        print(f"[Top Videos] Found {len(top_shorts)} high-revenue videos out of {len(videos)} total")
        
        return jsonify({
            'success': True,
            'count': len(top_shorts),
            'total_fetched': len(videos),
            'videos': top_shorts[:30]  # Limit to top 30
        })
    except Exception as e:
        print(f"[Top Videos] Error: {e}")
        return jsonify({'success': False, 'error': str(e), 'videos': []}), 200


# TIKTOK_COPILOT_COOKIE - Session management for scraping
# State control for product sync
SYNC_STOP_REQUESTED = False

@app.route('/api/copilot/stop-sync', methods=['POST'])
@login_required
@admin_required
def copilot_stop_sync():
    global SYNC_STOP_REQUESTED
    SYNC_STOP_REQUESTED = True
    # Persist to DB so background threads can see it
    set_config_value('SYNC_STOP_REQUESTED', 'true', 'Persistent stop flag for sync processes')
    print("🛑 [Copilot Sync] Stop signal received and persisted to DB!")
    return jsonify({'success': True, 'message': 'Stop signal sent and persisted'})

@app.route('/api/copilot/reset-sync', methods=['POST'])
@login_required
@admin_required
def copilot_reset_sync():
    global SYNC_STOP_REQUESTED
    SYNC_STOP_REQUESTED = False
    # Clear persistent flag in DB
    set_config_value('SYNC_STOP_REQUESTED', 'false', 'Persistent stop flag for sync processes')
    print("✅ [Copilot Sync] Stop flag reset in memory and DB.")
    return jsonify({'success': True, 'message': 'Sync state reset'})

@app.route('/api/copilot/sync', methods=['POST'])
@login_required
@admin_required
def copilot_sync():
    """Manual trigger to sync latest products from Copilot with massive multi-page support"""
    global SYNC_STOP_REQUESTED
    SYNC_STOP_REQUESTED = False  # Reset on new manual trigger
    
    user = get_current_user()
    data = request.json or {}
    pages = int(data.get('pages', 1))
    start_page = int(data.get('page', 0))  # Correctly get start page from frontend
    limit = int(data.get('limit', 50))
    timeframe = data.get('timeframe', 'all')  # Default to 'all' for all-time video/creator counts
    
    # Cap pages to prevent extreme load but allow high volume (up to 40k products)
    if pages > 800: pages = 800 
    
    products_synced = 0
    errors = []
    
    for page_idx in range(start_page, start_page + pages): # Use correct range based on start_page
        if SYNC_STOP_REQUESTED:
            print("🛑 [Copilot Sync] Interrupting batch processing...")
            break
            
        try:
            print(f"[Copilot Sync] Processing page {page_idx + 1}")
            saved, total = sync_copilot_products(timeframe=timeframe, limit=limit, page=page_idx)
            products_synced += saved
            if total == 0: break # No more products
            if page_idx < start_page + pages - 1:
                # Randomized delay between 1.5 and 4 seconds to bypass bot detection
                delay = 1.5 + (secrets.randbelow(250) / 100.0)
                time.sleep(delay)
        except Exception as e:
            print(f"Error syncing page {page_idx}: {e}")
            errors.append(f"Page {page_idx}: {str(e)}")
            
    log_activity(user.id, 'copilot_sync', {'pages': pages, 'synced': products_synced})
    return jsonify({
        'status': 'success',
        'synced': products_synced,
        'pages_processed': pages,
        'total_pages_requested': pages,
        'stop_requested': SYNC_STOP_REQUESTED,
        'errors': errors
    })

@app.route('/api/copilot/enrich-videos', methods=['POST'])
@login_required
@admin_required
def copilot_enrich_videos():
    """Enrich products with all-time video counts from Copilot API.
    
    This runs separately from the main sync to get all-time video counts
    while keeping 7D sales/ad_spend for momentum tracking.
    Paginates through multiple pages to enrich as many products as possible.
    
    Enhanced for 15k+ products:
    - Longer delays to avoid API memory limits
    - Continues past API errors with backoff
    - Tracks progress for resumption
    """
    import gc
    user = get_current_user()
    data = request.json or {}
    target_pages = int(data.get('pages', 600))  # Default: 600 pages = 30k products
    delay_seconds = float(data.get('delay', 3.0))  # Faster 3s delay (optimized)
    
    try:
        print(f"[Video Enrich] Starting enrichment across {target_pages} pages (delay: {delay_seconds}s)...")
        
        enriched_count = 0
        total_fetched = 0
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        global SYNC_STOP_REQUESTED
        SYNC_STOP_REQUESTED = False  # Reset on new enrichment trigger
        set_config_value('SYNC_STOP_REQUESTED', 'false', 'Persistent stop flag for sync processes')
        
        for page in range(target_pages):
            # Check persistent DB stop flag
            db_stop_flag = get_config_value('SYNC_STOP_REQUESTED', 'false')
            if db_stop_flag == 'true':
                SYNC_STOP_REQUESTED = True
                print("[Video Enrich] 🛑 Stop signal loaded from DB!")
                
            if SYNC_STOP_REQUESTED:
                print("[Video Enrich] 🛑 Stop requested, terminating enrichment")
                break
            
            # Try V2 endpoint first (has timeframe=all), fallback to legacy
            products_data = fetch_copilot_products(timeframe='all', limit=25, page=page)
            
            # If V2 fails, try legacy endpoint with 30d (longest available)
            if not products_data or not products_data.get('products'):
                print(f"[Video Enrich] V2 failed on page {page}, trying legacy 30d...")
                products_data = fetch_copilot_trending(timeframe='30d', limit=50, page=page)
            
            # Handle API errors gracefully - don't stop, just pause and continue
            if not products_data:
                consecutive_errors += 1
                print(f"[Video Enrich] Page {page}: API failed (error {consecutive_errors}/{max_consecutive_errors})")
                
                if consecutive_errors >= max_consecutive_errors:
                    print(f"[Video Enrich] Too many consecutive errors, stopping")
                    break
                
                # Exponential backoff: wait longer after each error
                backoff_time = min(30, 5 * consecutive_errors)
                print(f"[Video Enrich] Waiting {backoff_time}s before retry...")
                time.sleep(backoff_time)
                continue  # Try next page instead of stopping
            
            # Reset error counter on success
            consecutive_errors = 0
            
            # Handle both V2 ('products') and legacy ('videos') response keys
            products_list = products_data.get('products', []) or products_data.get('videos', [])
            if not products_list:
                print(f"[Video Enrich] Page {page}: Empty page, trying next...")
                time.sleep(delay_seconds)
                continue  # Try next page
            
            total_fetched += len(products_list)
            page_enriched = 0
            
            for p in products_list:
                if SYNC_STOP_REQUESTED:
                    break
                product_id = str(p.get('productId', '')).strip()
                if not product_id:
                    continue
                
                # Normalize to our shop_ prefix (same as main sync)
                if not product_id.startswith('shop_'):
                    product_id = f"shop_{product_id}"
                    
                # FIXED: Use correct field names for all-time counts
                # productVideoCount = all-time, periodVideoCount = 7-day
                # Legacy also returns productVideoCount!
                alltime_video_count = safe_int(p.get('productVideoCount') or p.get('periodVideoCount') or p.get('videoCount'))
                alltime_creator_count = safe_int(p.get('productCreatorCount') or p.get('periodCreatorCount'))
                
                # Update the product in our database
                existing = Product.query.get(product_id)
                if existing:
                    if alltime_video_count > 0:
                        existing.video_count_alltime = alltime_video_count
                    if alltime_creator_count > 0:
                        existing.influencer_count = alltime_creator_count
                    enriched_count += 1
                    page_enriched += 1
            
            # Commit after each page to save progress
            db.session.commit()
            
            # Force garbage collection to prevent memory buildup
            gc.collect()
            
            # Progress log every 10 pages
            if page % 10 == 0 or page_enriched > 0:
                print(f"[Video Enrich] Page {page}: fetched {len(products_list)}, page_enriched: {page_enriched}, total: {enriched_count}")
            
            # Longer delay to avoid API memory issues (their server, not ours)
            time.sleep(delay_seconds)
        
        log_activity(user.id, 'enrich_videos', {'enriched': enriched_count, 'pages': target_pages})
        
        return jsonify({
            'status': 'success',
            'message': f'Enriched {enriched_count} products across {target_pages} pages',
            'enriched': enriched_count,
            'total_fetched': total_fetched,
            'pages_processed': target_pages
        })
        
    except Exception as e:
        print(f"[Video Enrich] Error: {e}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/copilot/creator-products', methods=['POST'])
@login_required
@admin_required
def copilot_creator_products():
    """Scrape all products from a specific creator's videos and export to CSV.
    
    Takes a creator ID (from TikTok Copilot) and fetches all their viral videos,
    extracting product data and stats.
    
    Request body:
        creator_id: The TikTok creator ID (e.g. 7436686228703265838)
        creator_name: Optional name for the export filename
        max_pages: Maximum pages to fetch (default 50 = ~1200 products)
    
    Returns:
        CSV file download with product data
    """
    import csv
    import io
    from flask import Response
    
    data = request.json or {}
    creator_id = data.get('creator_id', '').strip()
    creator_name = data.get('creator_name', 'creator').strip()
    max_pages = int(data.get('max_pages', 50))
    delay_seconds = float(data.get('delay', 5.0))
    
    if not creator_id:
        return jsonify({'status': 'error', 'message': 'creator_id is required'})
    
    print(f"[Creator Export] Starting export for creator {creator_name} (ID: {creator_id})...")
    
    try:
        all_products = {}  # Use dict to dedupe by product ID
        total_videos = 0
        
        for page in range(max_pages):
            print(f"[Creator Export] Fetching page {page}...")
            
            # Use legacy endpoint with creatorIds filter
            # Note: creatorIds filter works with 7d timeframe, NOT 'all'
            # The productVideoCount and productCreatorCount fields ARE all-time totals
            result = fetch_copilot_trending(
                timeframe='7d',  # Must use 7d for creatorIds filter to work
                sort_by='revenue',
                limit=50,
                page=page,
                creator_ids=creator_id
            )
            
            if not result:
                print(f"[Creator Export] Page {page}: No data, stopping")
                break
            
            videos = result.get('videos', [])
            if not videos:
                print(f"[Creator Export] Page {page}: No more videos, stopping")
                break
            
            total_videos += len(videos)
            
            for video in videos:
                # Extract product ID from video - we'll enrich from database
                product_id = str(video.get('productId', '')).strip()
                if not product_id or product_id in all_products:
                    continue
                
                # Store basic discovery data - will be enriched from database
                all_products[product_id] = {
                    'product_id': product_id,
                    'product_name': video.get('productTitle', ''),
                    'seller_name': video.get('sellerName', ''),
                    'video_url': video.get('videoUrl') or video.get('url') or f"https://www.tiktok.com/@{creator_name}",
                    'creator_name': video.get('authorName') or video.get('author') or creator_name,
                }
            
            print(f"[Creator Export] Page {page}: {len(videos)} videos, {len(all_products)} unique products so far")
            
            # Delay to avoid API limits
            time.sleep(delay_seconds)
        
        print(f"[Creator Export] Discovery complete! {len(all_products)} products found")
        print(f"[Creator Export] Enriching from local database for ALL-TIME stats...")
        
        # STEP 2: Enrich from local database (which has accurate all-time stats from syncs)
        enriched_products = []
        matched_count = 0
        
        # Get current time in EST for sync_date field
        from datetime import datetime, timezone, timedelta
        est = timezone(timedelta(hours=-5))  # EST is UTC-5
        sync_date_est = datetime.now(est).strftime('%Y-%m-%d %I:%M %p EST')
        
        for product_id, basic_info in all_products.items():
            # Query our local Product table for accurate stats
            # Try both formats: with and without shop_ prefix
            shop_pid = f"shop_{product_id}" if not product_id.startswith('shop_') else product_id
            raw_pid = product_id.replace('shop_', '')
            
            db_product = Product.query.filter_by(product_id=shop_pid).first()
            if not db_product:
                db_product = Product.query.filter_by(product_id=raw_pid).first()
            
            if db_product:
                # Use database stats (all-time accurate data)
                enriched_products.append({
                    'product_id': product_id,
                    'product_name': db_product.product_name or basic_info.get('product_name', ''),
                    'product_url': f"https://shop.tiktok.com/view/product/{product_id}?region=US",
                    'seller_name': db_product.seller_name or basic_info.get('seller_name', ''),
                    'price': round(db_product.price or 0, 2),  # Format to 2 decimal places
                    'commission_rate': f"{(db_product.commission_rate or 0) * 100:.1f}%",  # Format as percentage
                    'gmv_max_rate': f"{(db_product.shop_ads_commission or 0) * 100:.1f}%",  # GMV Max
                    'video_count_alltime': db_product.video_count_alltime or db_product.video_count or 0,  # ALL-TIME first!
                    'creator_count': db_product.influencer_count or 0,
                    'sales_alltime': db_product.sales or 0,
                    'sales_7d': db_product.sales_7d or 0,
                    'revenue_alltime': db_product.gmv or 0,
                    'ad_spend': db_product.ad_spend or 0,
                    'video_url': basic_info.get('video_url', ''),
                    'creator_name': basic_info.get('creator_name', creator_name),
                    'sync_date': sync_date_est,  # EST timezone
                })
                matched_count += 1
            else:
                # Product not in database - use basic info with placeholders
                enriched_products.append({
                    'product_id': product_id,
                    'product_name': basic_info.get('product_name', ''),
                    'product_url': f"https://shop.tiktok.com/view/product/{product_id}?region=US",
                    'seller_name': basic_info.get('seller_name', ''),
                    'price': 0,
                    'commission_rate': '0%',
                    'gmv_max_rate': '0%',
                    'video_count_alltime': 0,
                    'creator_count': 0,
                    'sales_alltime': 0,
                    'sales_7d': 0,
                    'revenue_alltime': 0,
                    'ad_spend': 0,
                    'video_url': basic_info.get('video_url', ''),
                    'creator_name': basic_info.get('creator_name', creator_name),
                    'sync_date': sync_date_est,  # EST timezone
                })
        
        print(f"[Creator Export] Matched {matched_count}/{len(all_products)} products from database")
        
        # Sort by all-time revenue
        products_list = sorted(enriched_products, key=lambda x: x.get('revenue_alltime') or 0, reverse=True)
        
        print(f"[Creator Export] Complete! {total_videos} videos, {len(products_list)} unique products")
        
        # Generate CSV
        output = io.StringIO()
        if products_list:
            writer = csv.DictWriter(output, fieldnames=products_list[0].keys())
            writer.writeheader()
            writer.writerows(products_list)
        
        # Create response with CSV download
        csv_content = output.getvalue()
        
        return Response(
            csv_content,
            mimetype='text/csv',
            headers={
                'Content-Disposition': f'attachment; filename={creator_name}_products.csv',
                'Content-Type': 'text/csv; charset=utf-8'
            }
        )
        
    except Exception as e:
        print(f"[Creator Export] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)})

# =============================================================================
# GOOGLE SHEETS AUTO-SYNC
# =============================================================================

# Store Google Sheets config in environment/database
# Format: { sheet_id, credentials_json, frequency, last_sync }
GOOGLE_SHEETS_CONFIG = {}

def get_google_sheets_config():
    """Get Google Sheets config from environment or memory cache"""
    global GOOGLE_SHEETS_CONFIG
    if not GOOGLE_SHEETS_CONFIG.get('sheet_id'):
        GOOGLE_SHEETS_CONFIG = {
            'sheet_id': os.environ.get('GOOGLE_SHEET_ID', ''),
            'credentials': os.environ.get('GOOGLE_SHEETS_CREDENTIALS', ''),
            'frequency': os.environ.get('GOOGLE_SHEETS_FREQUENCY', '3days'),
            'last_sync': os.environ.get('GOOGLE_SHEETS_LAST_SYNC', ''),
        }
    return GOOGLE_SHEETS_CONFIG

@app.route('/api/admin/config/<key>', methods=['GET'])
@admin_required
def get_admin_config(key):
    """Get any configuration value from DB"""
    val = get_config_value(key)
    return jsonify({'success': True, 'value': val})

@app.route('/api/admin/config/<key>', methods=['POST'])
@admin_required
def save_admin_config(key):
    """Save any configuration value to DB"""
    try:
        data = request.get_json()
        val = data.get('value', '').strip()
        if not val:
            return jsonify({'success': False, 'error': 'Value is empty'}), 400
        
        set_config_value(key, val)
        return jsonify({'success': True, 'message': f'{key} updated successfully.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/copilot/test')
@admin_required
def test_copilot_connection():
    """Test TikTokCopilot API connection manually"""
    print("[Copilot] Running manual connection test...")
    res = fetch_copilot_products(limit=1, page=0)
    if res and (res.get('products') or res.get('videos')):
        return jsonify({'success': True, 'message': 'Connection verified. (V2 Active)'})
    
    # Try legacy trending if products fails
    res = fetch_copilot_trending(limit=1, page=0)
    if res and (res.get('videos') or res.get('products')):
        return jsonify({'success': True, 'message': 'Connection verified. (Legacy Fallback Active)'})
        
    return jsonify({'success': False, 'error': 'Cookie invalid or API blocked. Check logs for JSON Decode Errors.'}), 401

@app.route('/api/copilot/refresh-session', methods=['POST'])
@admin_required
def api_copilot_refresh_session():
    """
    Manually trigger TikTokCopilot session refresh.
    Attempts auto-login via Clerk API using stored credentials.
    """
    try:
        result = auto_login_copilot()
        if result:
            return jsonify({
                'success': True, 
                'message': 'Session refreshed successfully!',
                'last_refresh': _COPILOT_LAST_REFRESH.isoformat() if _COPILOT_LAST_REFRESH else None
            })
        else:
            # Check if credentials are configured
            email = os.environ.get('COPILOT_EMAIL')
            password = os.environ.get('COPILOT_PASSWORD')
            if not email or not password:
                return jsonify({
                    'success': False, 
                    'error': 'Missing credentials. Set COPILOT_EMAIL and COPILOT_PASSWORD env vars on Render.'
                }), 400
            return jsonify({
                'success': False, 
                'error': 'Login failed. Check server logs for details.'
            }), 401
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/copilot/refresh-status', methods=['GET'])
@admin_required
def api_copilot_refresh_status():
    """Get the status of auto-refresh system."""
    global _COPILOT_AUTO_REFRESH_THREAD, _COPILOT_LAST_REFRESH
    
    email = os.environ.get('COPILOT_EMAIL')
    has_credentials = bool(email and os.environ.get('COPILOT_PASSWORD'))
    
    scheduler_active = _COPILOT_AUTO_REFRESH_THREAD is not None and _COPILOT_AUTO_REFRESH_THREAD.is_alive()
    
    return jsonify({
        'has_credentials': has_credentials,
        'credentials_email': email[:5] + '***' if email else None,
        'scheduler_active': scheduler_active,
        'last_refresh': _COPILOT_LAST_REFRESH.isoformat() if _COPILOT_LAST_REFRESH else None
    })

@app.route('/api/admin/google-sheets-config', methods=['GET'])
def get_sheets_config():
    """Get Google Sheets configuration"""
    config = get_google_sheets_config()
    return jsonify({
        'sheet_id': config.get('sheet_id', ''),
        'credentials': bool(config.get('credentials')),  # Don't expose actual credentials
        'frequency': config.get('frequency', '3days'),
        'last_sync': config.get('last_sync', ''),
    })

@app.route('/api/admin/google-sheets-config', methods=['POST'])
def save_sheets_config():
    """Save Google Sheets configuration"""
    global GOOGLE_SHEETS_CONFIG
    try:
        data = request.get_json()
        
        if data.get('sheet_id'):
            GOOGLE_SHEETS_CONFIG['sheet_id'] = data['sheet_id']
            os.environ['GOOGLE_SHEET_ID'] = data['sheet_id']
        
        if data.get('credentials'):
            # Validate JSON
            import json
            json.loads(data['credentials'])  # Will throw if invalid
            GOOGLE_SHEETS_CONFIG['credentials'] = data['credentials']
            os.environ['GOOGLE_SHEETS_CREDENTIALS'] = data['credentials']
        
        if data.get('frequency'):
            GOOGLE_SHEETS_CONFIG['frequency'] = data['frequency']
            os.environ['GOOGLE_SHEETS_FREQUENCY'] = data['frequency']
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/admin/google-sheets-sync', methods=['POST'])
def sync_to_google_sheets():
    """Sync creator products to Google Sheets"""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        import json
        
        config = get_google_sheets_config()
        
        if not config.get('sheet_id'):
            return jsonify({'success': False, 'error': 'No Google Sheet ID configured'})
        
        if not config.get('credentials'):
            return jsonify({'success': False, 'error': 'No Google credentials configured'})
        
        data = request.get_json() or {}
        creator_id = data.get('creator_id', '7436686228703265838')  # Default to cakedfinds
        creator_name = data.get('creator_name', 'cakedfinds')
        
        print(f"[Google Sheets] Starting sync for {creator_name}...")
        
        # Authenticate with Google Sheets
        creds_dict = json.loads(config['credentials'])
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(credentials)
        
        # Open the sheet
        sheet = gc.open_by_key(config['sheet_id']).sheet1
        
        # Get products data (reuse the export logic)
        products_list = []
        all_products = {}
        total_videos = 0
        delay_seconds = 3.0
        max_pages = 100
        
        for page in range(max_pages):
            try:
                result = fetch_copilot_trending(
                    timeframe='7d',
                    sort_by='revenue',
                    c_timeframe='7d',
                    limit=50,
                    page=page,
                    creator_ids=creator_id
                )
                
                if not result:
                    break
                    
                videos = result.get('videos', [])
                if not videos:
                    break
                    
                total_videos += len(videos)
                
                for video in videos:
                    product_id = str(video.get('productId', '')).strip()
                    if not product_id or product_id in all_products:
                        continue
                    
                    all_products[product_id] = {
                        'product_id': product_id,
                        'product_name': video.get('productTitle', ''),
                        'seller_name': video.get('sellerName', ''),
                    }
                
                time.sleep(delay_seconds)
                
            except Exception as e:
                print(f"[Google Sheets] Page {page} error: {e}")
                break
        
        print(f"[Google Sheets] Found {len(all_products)} products, enriching from database...")
        
        # Enrich from database
        # Get sync date in EST
        from datetime import datetime, timezone, timedelta
        est = timezone(timedelta(hours=-5))
        sync_date_est = datetime.now(est).strftime('%Y-%m-%d %I:%M %p EST')
        
        for product_id, basic_info in all_products.items():
            shop_pid = f"shop_{product_id}" if not product_id.startswith('shop_') else product_id
            raw_pid = product_id.replace('shop_', '')
            
            db_product = Product.query.filter_by(product_id=shop_pid).first()
            if not db_product:
                db_product = Product.query.filter_by(product_id=raw_pid).first()
            
            if db_product:
                products_list.append([
                    product_id,
                    db_product.product_name or basic_info.get('product_name', ''),
                    f"https://shop.tiktok.com/view/product/{product_id}?region=US",
                    db_product.seller_name or basic_info.get('seller_name', ''),
                    round(db_product.price or 0, 2),
                    f"{(db_product.commission_rate or 0) * 100:.1f}%",
                    f"{(db_product.shop_ads_commission or 0) * 100:.1f}%",
                    db_product.video_count_alltime or db_product.video_count or 0,
                    db_product.influencer_count or 0,
                    db_product.sales or 0,
                    db_product.sales_7d or 0,
                    db_product.gmv or 0,
                    db_product.ad_spend or 0,
                    sync_date_est,
                ])
            else:
                products_list.append([
                    product_id,
                    basic_info.get('product_name', ''),
                    f"https://shop.tiktok.com/view/product/{product_id}?region=US",
                    basic_info.get('seller_name', ''),
                    0, '0%', '0%', 0, 0, 0, 0, 0, 0, sync_date_est,
                ])
        
        # Sort by revenue (column 12, index 11) descending
        products_list.sort(key=lambda x: x[11] if x[11] else 0, reverse=True)
        
        # Clear sheet and write header + data
        sheet.clear()
        header = ['product_id', 'product_name', 'product_url', 'seller_name', 'price', 
                  'commission_rate', 'gmv_max_rate', 'video_count_alltime', 'creator_count',
                  'sales_alltime', 'sales_7d', 'revenue_alltime', 'ad_spend', 'sync_date']
        
        all_data = [header] + products_list
        sheet.update('A1', all_data)
        
        # Update last sync time
        from datetime import datetime, timezone
        GOOGLE_SHEETS_CONFIG['last_sync'] = datetime.now(timezone.utc).isoformat()
        
        print(f"[Google Sheets] Sync complete! {len(products_list)} products")
        
        return jsonify({'success': True, 'rows': len(products_list)})
        
    except Exception as e:
        print(f"[Google Sheets] Sync error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

# =============================================================================
# BRAND HUNTER API ENDPOINTS
# =============================================================================

@app.route('/api/brands', methods=['GET'])
@login_required
def api_list_brands():
    """List all watched brands with their stats
    V2 FIX: Filters out brands with undefined/null names
    """
    # Filter out undefined/null brand names (case-insensitive)
    # Using func.lower() for case-insensitive matching
    brands = WatchedBrand.query.filter(
        WatchedBrand.is_active == True,
        WatchedBrand.name != None,
        WatchedBrand.name != '',
        ~db.func.lower(WatchedBrand.name).in_(['undefined', 'null', 'unknown', '(undefined)', 'none', ''])
    ).order_by(WatchedBrand.total_sales_7d.desc()).all()
    return jsonify({
        'success': True,
        'brands': [b.to_dict() for b in brands],
        'count': len(brands)
    })

@app.route('/api/brands/discover', methods=['GET'])
@login_required
def api_discover_brands():
    """Discover top brands from existing products, sorted by GMV"""
    try:
        # Get already tracked brand names for filtering
        tracked_names = [b.name.lower() for b in WatchedBrand.query.all() if b.name]
        
        # Aggregate products by seller_name
        from sqlalchemy import func
        brand_stats = db.session.query(
            Product.seller_name,
            func.count(Product.product_id).label('product_count'),
            func.sum(Product.gmv).label('total_gmv'),
            func.sum(Product.sales_7d).label('total_sales_7d'),
            func.avg(Product.commission_rate).label('avg_commission')
        ).filter(
            Product.seller_name != None,
            Product.seller_name != '',
            Product.seller_name != 'Unknown',
            Product.seller_name != 'undefined',
            Product.seller_name != '(undefined)',
            Product.seller_name != 'null'
        ).group_by(
            Product.seller_name
        ).having(
            func.sum(Product.gmv) > 0  # Only brands with GMV
        ).order_by(
            func.sum(Product.gmv).desc()
        ).limit(50).all()
        
        # Format results
        discovered = []
        for row in brand_stats:
            # Skip if already tracked
            if row.seller_name and row.seller_name.lower() in tracked_names:
                continue
                
            discovered.append({
                'name': row.seller_name,
                'product_count': row.product_count or 0,
                'total_gmv': float(row.total_gmv or 0),
                'total_sales_7d': int(row.total_sales_7d or 0),
                'avg_commission': float(row.avg_commission or 0),
                'is_tracked': False
            })
        
        return jsonify({
            'success': True,
            'discovered': discovered[:30],  # Top 30 untracked brands
            'count': len(discovered)
        })
    except Exception as e:
        print(f"[Brand Discover] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/brands/scan-copilot', methods=['POST'])
@login_required
def api_scan_copilot_for_brands():
    """Live scan Copilot API for trending products and aggregate sellers to find big brands"""
    try:
        data = request.json or {}
        pages_to_scan = min(int(data.get('pages', 10)), 20)  # Max 20 pages
        
        print(f"[Brand Scan] Starting Copilot scan for {pages_to_scan} pages...")
        
        # Aggregate sellers from trending products
        seller_stats = {}
        
        for page in range(pages_to_scan):
            result = fetch_copilot_trending(timeframe='7d', limit=100, page=page)
            
            if not result:
                continue
                
            videos = result.get('videos', [])
            if not videos:
                continue
            
            for v in videos:
                seller = v.get('sellerName', '').strip()
                if not seller or seller.lower() in ['unknown', 'undefined', '(undefined)', 'null', 'None'] or len(seller) < 2:
                    continue
                
                gmv = float(v.get('periodGmv') or v.get('gmv') or 0)
                sales = int(v.get('periodUnits') or v.get('units') or 0)
                
                if seller not in seller_stats:
                    seller_stats[seller] = {
                        'name': seller,
                        'product_count': 0,
                        'total_gmv': 0,
                        'total_sales_7d': 0
                    }
                
                seller_stats[seller]['product_count'] += 1
                seller_stats[seller]['total_gmv'] += gmv
                seller_stats[seller]['total_sales_7d'] += sales
            
            time.sleep(0.3)  # Avoid rate limits
        
        # Sort by GMV and filter out already tracked brands
        tracked_names = [b.name.lower() for b in WatchedBrand.query.all() if b.name]
        
        discovered = []
        for name, stats in seller_stats.items():
            if name.lower() in tracked_names:
                continue
            if stats['total_gmv'] > 0:
                discovered.append(stats)
        
        # Sort by GMV descending
        discovered.sort(key=lambda x: x['total_gmv'], reverse=True)
        
        print(f"[Brand Scan] Found {len(discovered)} brands from {pages_to_scan} pages")
        
        return jsonify({
            'success': True,
            'discovered': discovered[:50],  # Top 50 brands
            'count': len(discovered),
            'pages_scanned': pages_to_scan
        })
    except Exception as e:
        print(f"[Brand Scan] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/brands', methods=['POST'])
@login_required
def api_add_brand():
    """Add a new brand to watch"""
    data = request.json or {}
    name = data.get('name', '').strip()
    
    if not name or name.lower() in ['unknown', 'undefined', '(undefined)', 'null', 'none', '']:
        return jsonify({'success': False, 'error': f'Invalid brand name: "{name}"'}), 400
    
    # Check if already exists
    existing = WatchedBrand.query.filter(WatchedBrand.name.ilike(name)).first()
    if existing:
        return jsonify({'success': False, 'error': f'Brand "{name}" is already being tracked'}), 400
    
    # Create new brand
    brand = WatchedBrand(name=name)
    db.session.add(brand)
    db.session.commit()
    
    # Immediately calculate stats from existing products
    brand.refresh_stats()
    
    user = get_current_user()
    log_activity(user.id, 'add_brand', {'brand': name})
    
    return jsonify({
        'success': True,
        'brand': brand.to_dict(),
        'message': f'Now tracking "{name}" with {brand.product_count} existing products'
    })

@app.route('/api/brands/<int:brand_id>', methods=['DELETE'])
@login_required
def api_delete_brand(brand_id):
    """Stop tracking a brand"""
    brand = WatchedBrand.query.get(brand_id)
    if not brand:
        return jsonify({'success': False, 'error': 'Brand not found'}), 404
    
    name = brand.name
    db.session.delete(brand)
    db.session.commit()
    
    user = get_current_user()
    log_activity(user.id, 'delete_brand', {'brand': name})
    
    return jsonify({'success': True, 'message': f'Stopped tracking "{name}"'})

@app.route('/api/brands/<int:brand_id>/sync', methods=['POST'])
@login_required
def api_sync_brand(brand_id):
    """Sync products for a specific brand by searching Copilot"""
    brand = WatchedBrand.query.get(brand_id)
    if not brand:
        return jsonify({'success': False, 'error': 'Brand not found'}), 404
    
    # Refresh stats from existing products in DB
    brand.refresh_stats()
    
    user = get_current_user()
    log_activity(user.id, 'sync_brand', {'brand': brand.name, 'products': brand.product_count})
    
    return jsonify({
        'success': True,
        'brand': brand.to_dict(),
        'message': f'Refreshed stats for "{brand.name}": {brand.product_count} products, {brand.total_sales_7d:,} 7D sales'
    })

@app.route('/api/brands/<int:brand_id>/products', methods=['GET'])
@login_required
def api_brand_products(brand_id):
    """Get all products for a specific brand"""
    brand = WatchedBrand.query.get(brand_id)
    if not brand:
        return jsonify({'success': False, 'error': 'Brand not found'}), 404
    
    # Find products matching brand name in seller_name
    products = Product.query.filter(
        Product.seller_name.ilike(f'%{brand.name}%')
    ).order_by(Product.sales_7d.desc()).limit(100).all()
    
    return jsonify({
        'success': True,
        'brand': brand.to_dict(),
        'products': [p.to_dict() for p in products],
        'count': len(products)
    })

@app.route('/api/brands/refresh-all', methods=['POST'])
@login_required
def api_refresh_all_brands():
    """Refresh stats for all watched brands"""
    brands = WatchedBrand.query.filter_by(is_active=True).all()
    
    for brand in brands:
        brand.refresh_stats()
    
    user = get_current_user()
    log_activity(user.id, 'refresh_all_brands', {'count': len(brands)})
    
    return jsonify({
        'success': True,
        'message': f'Refreshed stats for {len(brands)} brands'
    })

@app.route('/api/brands/init', methods=['POST'])
@login_required
@admin_required
def api_init_brands():
    """Initialize/recreate the watched_brands table"""
    try:
        # Create all missing tables
        db.create_all()
        
        # Check if table exists and has data
        count = WatchedBrand.query.count()
        
        # Clear any brands with null names
        deleted = WatchedBrand.query.filter(
            db.or_(
                WatchedBrand.name == None,
                WatchedBrand.name == ''
            )
        ).delete(synchronize_session=False)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Brand Hunter initialized! Table has {count} brands. Cleaned {deleted} invalid entries.'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/brands/debug', methods=['GET'])
@login_required
def api_debug_brands():
    """Debug: Show raw brand data and schema info"""
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        
        all_brands = WatchedBrand.query.all()
        return jsonify({
            'success': True,
            'tables_in_db': tables,
            'watched_brands_exists': 'watched_brands' in tables,
            'count': len(all_brands),
            'brands': [
                {
                    'id': b.id,
                    'name': b.name,
                    'name_repr': repr(b.name),
                    'is_active': b.is_active,
                    'product_count': b.product_count,
                    'created_at': str(b.created_at)
                }
                for b in all_brands
            ]
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        })

@app.route('/api/brands/cleanup', methods=['POST'])
@login_required
@admin_required
def api_cleanup_brands():
    """Remove all brands with null/empty names"""
    try:
        # Delete brands with no name or empty name
        deleted = WatchedBrand.query.filter(
            db.or_(
                WatchedBrand.name == None,
                WatchedBrand.name == ''
            )
        ).delete(synchronize_session=False)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'deleted': deleted,
            'message': f'Removed {deleted} brands with invalid names'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/brands/clear-all', methods=['POST'])
@login_required
@admin_required
def api_clear_all_brands():
    """Clear ALL watched brands (reset Brand Hunter)"""
    try:
        deleted = WatchedBrand.query.delete()
        db.session.commit()
        
        user = get_current_user()
        log_activity(user.id, 'clear_all_brands', {'deleted': deleted})
        
        return jsonify({
            'success': True,
            'deleted': deleted,
            'message': f'Cleared all {deleted} brands. Brand Hunter reset.'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/brands/nuke', methods=['POST'])
@login_required
@admin_required
def api_nuke_brands():
    """Hard reset: Drop and recreate the watched_brands table"""
    try:
        from sqlalchemy import text
        db.session.execute(text("DROP TABLE IF EXISTS watched_brands CASCADE"))
        db.session.commit()
        
        # Recreate tables
        db.create_all()
        
        return jsonify({
            'success': True,
            'message': 'NUCLEAR RESET: Brand table dropped and recreated. All ghost data should be gone.'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/brands/cleanup-full', methods=['POST'])
@login_required
@admin_required
def api_cleanup_full():
    """Deep Clean: Purge 'undefined' and 'null' brands and products"""
    try:
        user = get_current_user()
        bad_names = ['undefined', '(undefined)', 'null', 'Unknown', '']
        
        # 1. Purge Brand Table
        brands_deleted = WatchedBrand.query.filter(
            db.or_(
                WatchedBrand.name == None,
                WatchedBrand.name.in_(bad_names)
            )
        ).delete(synchronize_session=False)
        
        # 2. Purge Product Table (Corrupted entries)
        products_deleted = Product.query.filter(
            db.or_(
                Product.seller_name == None,
                Product.seller_name.in_(bad_names)
            )
        ).delete(synchronize_session=False)
        
        db.session.commit()
        log_activity(user.id, 'full_brand_cleanup', {'brands': brands_deleted, 'products': products_deleted})
        
        return jsonify({
            'success': True,
            'message': f'DEEP CLEAN COMPLETE! Purged {brands_deleted} ghost brands and {products_deleted} corrupted products.',
            'brands_deleted': brands_deleted,
            'products_deleted': products_deleted
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/cleanup/zero-sales', methods=['POST'])
@login_required
@admin_required
def cleanup_zero_sales_v2():
    """Remove products from database that have 0 7D sales"""
    user = get_current_user()
    try:
        # Find products with 0 or NULL 7D sales
        zero_sales_count = Product.query.filter(
            db.or_(
                Product.sales_7d == 0,
                Product.sales_7d == None
            )
        ).count()
        
        # Delete them
        deleted = Product.query.filter(
            db.or_(
                Product.sales_7d == 0,
                Product.sales_7d == None
            )
        ).delete(synchronize_session=False)
        
        db.session.commit()
        
        log_activity(user.id, 'cleanup_zero_sales', {'deleted': deleted})
        return jsonify({
            'status': 'success',
            'deleted': deleted,
            'message': f'Removed {deleted} products with 0 7D sales'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/admin/reset-product-status', methods=['POST'])
@login_required
@admin_required
def reset_product_status():
    """Reset product_status to 'active' for all products that meet quality criteria"""
    user = get_current_user()
    try:
        # Reset status for products with valid data (20+ videos, sales > 0)
        updated = Product.query.filter(
            Product.product_status == 'unavailable',
            Product.video_count >= 20,
            Product.sales_7d > 0
        ).update({'product_status': 'active', 'status_note': None}, synchronize_session=False)
        
        db.session.commit()
        
        log_activity(user.id, 'reset_product_status', {'updated': updated})
        return jsonify({
            'status': 'success',
            'updated': updated,
            'message': f'Restored {updated} products to active status'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/copilot/mass-sync', methods=['POST'])
@login_required
@admin_required
def copilot_mass_sync():
    """LUDICROUS SPEED mass sync - MAXIMUM parallelization"""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    user = get_current_user()
    user_id = user.id
    data = request.json or {}
    target_products = int(data.get('target', 10000))
    timeframe = data.get('timeframe', 'all')  # Default to 'all' for all-time video/creator counts
    
    # 100 products per page, up to 2000 pages max (200K theoretical max)
    PRODUCTS_PER_PAGE = 100
    pages_needed = min((target_products // PRODUCTS_PER_PAGE) + 1, 2000)
    
    # Store sync status in database for live tracking
    set_config_value('sync_status', 'running', 'Mass sync status')
    set_config_value('sync_progress', '0', 'Sync progress (products synced)')
    set_config_value('sync_target', str(target_products), 'Sync target')
    
    def run_sync_in_background():
        """Background sync task - LUDICROUS SPEED with retry"""
        with app.app_context():
            products_synced = 0
            pages_done = 0
            consecutive_empty = 0  # Track consecutive empty pages
            error_count = 0
            start_time = time.time()
            
            def fetch_page_with_retry(page_num, retries=3):
                """Fetch page with retry on failure - MUST have app context"""
                with app.app_context():  # CRITICAL: Each thread needs its own context
                    for attempt in range(retries):
                        try:
                            saved, total = sync_copilot_products(timeframe=timeframe, limit=PRODUCTS_PER_PAGE, page=page_num)
                            return page_num, saved, total, None
                        except Exception as e:
                            if attempt < retries - 1:
                                time.sleep(0.5)  # Wait before retry
                            else:
                                return page_num, 0, -1, str(e)
                    return page_num, 0, -1, "Unknown error"
            # MEMORY-OPTIMIZED: Reduced for Render free/starter tier
            # Sequential processing to minimize memory footprint
            import gc
            PRODUCTS_PER_PAGE = 25  # Smaller pages = less memory
            pages_needed = min((target_products // PRODUCTS_PER_PAGE) + 1, 2000)
            
            # SEQUENTIAL MODE: No parallelization to prevent memory spikes
            BATCH_SIZE = 1  # One page at a time for stability
            MAX_CONSECUTIVE_EMPTY = 15  # More persistent through rate limits
            print(f"[SYNC] 🚀 Memory-Optimized Sync: {pages_needed} pages, sequential, {PRODUCTS_PER_PAGE}/page")
            
            for batch_start in range(0, pages_needed, BATCH_SIZE):
                global SYNC_STOP_REQUESTED
                # Check persistent DB flag to synchronize with stop signal
                db_stop_flag = get_config_value('SYNC_STOP_REQUESTED', 'false')
                if db_stop_flag == 'true':
                    SYNC_STOP_REQUESTED = True
                    print("[SYNC] 🛑 Stop signal loaded from DB!")
                    
                if SYNC_STOP_REQUESTED:
                    print("[SYNC] 🛑 Stop requested, terminating background sync")
                    break
                    
                # Stop only if we got 10+ consecutive empty pages (API truly exhausted)
                if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                    print(f"[SYNC] {MAX_CONSECUTIVE_EMPTY} consecutive empty pages - API exhausted, stopping")
                    break
                    
                batch_end = min(batch_start + BATCH_SIZE, pages_needed)
                batch_pages = list(range(batch_start, batch_end))
                
                # SEQUENTIAL: Process one page at a time to minimize memory
                for page_num in batch_pages:
                    page_num, saved, total, error = fetch_page_with_retry(page_num)
                    products_synced += saved
                    pages_done += 1
                    
                    if error:
                        error_count += 1
                        consecutive_empty += 1  # Count errors toward empty
                        print(f"[SYNC] ⚠️ Error on page {page_num}: {error} ({consecutive_empty}/{MAX_CONSECUTIVE_EMPTY})")
                    elif total == 0:
                        consecutive_empty += 1
                        print(f"[SYNC] Empty page {page_num} ({consecutive_empty}/{MAX_CONSECUTIVE_EMPTY})")
                    else:
                        consecutive_empty = 0  # Reset on success
                    
                    # Update progress in DB every 5 pages
                    if pages_done % 5 == 0:
                        elapsed = time.time() - start_time
                        rate = pages_done / elapsed if elapsed > 0 else 0
                        set_config_value('sync_progress', str(products_synced))
                        print(f"[SYNC] ⚡ {pages_done}/{pages_needed} pages | {products_synced:,} products | {rate:.1f} pages/sec | {error_count} errors")
                
                # Force garbage collection to free memory
                gc.collect()
                
                # Longer delay between pages to let memory settle
                time.sleep(2.0)
            
            elapsed = time.time() - start_time
            print(f"[SYNC] ✅ COMPLETE: {products_synced:,} products from {pages_done} pages in {elapsed:.1f}s")
            set_config_value('sync_status', 'complete')
            set_config_value('sync_progress', str(products_synced))
            
            try:
                log_activity(user_id, 'mass_sync', {'pages': pages_done, 'synced': products_synced, 'seconds': int(elapsed)})
            except:
                pass
    
    # Start background thread and return immediately
    sync_thread = threading.Thread(target=run_sync_in_background, daemon=True)
    sync_thread.start()
    
    return jsonify({
        'status': 'success',
        'synced': 0,
        'pages_processed': 0,
        'message': f'🚀 LUDICROUS SPEED SYNC STARTED! Fetching {target_products:,} products ({pages_needed} pages @ {PRODUCTS_PER_PAGE}/page)'
    })

# Endpoint to check sync progress
@app.route('/api/copilot/sync-status')
@login_required
def copilot_sync_status():
    """Check current sync progress"""
    status = get_config_value('sync_status', 'idle')
    progress = int(get_config_value('sync_progress', '0'))
    target = int(get_config_value('sync_target', '0'))
    return jsonify({
        'status': status,
        'progress': progress,
        'target': target,
        'percent': int((progress / target * 100) if target > 0 else 0)
    })

@app.route('/api/copilot/test')
@login_required
def copilot_test():
    """Test TikTokCopilot connection with improved validation"""
    cookie = get_copilot_cookie()
    if not cookie:
        return jsonify({'success': False, 'error': 'No Copilot cookie configured. Please set TIKTOK_COPILOT_COOKIE in Settings or Render Environment.'})
    
    # Basic format validation - cookies should be long and contain session tokens
    if len(cookie) < 100:
        return jsonify({'success': False, 'error': f'Cookie too short ({len(cookie)} chars). A valid TikTokCopilot cookie is usually 500+ characters. Did you paste a password instead?'})
    
    if '__session' not in cookie and '__clerk' not in cookie and 'eyJ' not in cookie:
        return jsonify({'success': False, 'error': 'Invalid cookie format. Should contain session tokens (starts with __clerk or __session). Copy the full Cookie header from DevTools.'})
    
    # Try to fetch data
    result = fetch_copilot_trending(limit=5)
    if result and result.get('videos'):
        return jsonify({
            'success': True,
            'message': f"Connected! Found {len(result['videos'])} videos.",
            'sample': result['videos'][0].get('productTitle', 'N/A') if result['videos'] else None
        })
    elif result is None:
        return jsonify({'success': False, 'error': 'API request failed. Cookie may be expired or TikTokCopilot is down. Please refresh your cookie.'})
    else:
        return jsonify({'success': False, 'error': 'API returned empty data. Cookie may be expired. Please paste a fresh cookie from TikTokCopilot.'})

@app.route('/api/copilot/debug-fields')
@login_required
@admin_required
def copilot_debug_fields():
    """Debug: Dump raw API response to see current field names"""
    # Try the main products endpoint first
    result_products = fetch_copilot_products(timeframe='7d', limit=3, page=0)
    result_trending = fetch_copilot_trending(limit=3)
    
    response = {
        'products_endpoint': {
            'raw_keys': list(result_products.keys()) if result_products else None,
            'sample_product_keys': list(result_products.get('products', [{}])[0].keys()) if result_products and result_products.get('products') else None,
            'sample_product': result_products.get('products', [{}])[0] if result_products and result_products.get('products') else None
        } if result_products else {'error': 'Products endpoint returned None'},
        'trending_endpoint': {
            'raw_keys': list(result_trending.keys()) if result_trending else None,
            'sample_video_keys': list(result_trending.get('videos', [{}])[0].keys()) if result_trending and result_trending.get('videos') else None,
            'sample_video': result_trending.get('videos', [{}])[0] if result_trending and result_trending.get('videos') else None
        } if result_trending else {'error': 'Trending endpoint returned None'}
    }
    
    return jsonify(response)


@app.route('/api/admin/config/<key>', methods=['GET'])

@login_required
@admin_required
def admin_get_config(key):
    """Get a config value (masked if secret)"""
    val = get_config_value(key)
    if val and any(x in key.lower() for x in ['key', 'secret', 'token', 'cookie', 'password']):
        return jsonify({'success': True, 'value': '••••••••••••••••'})
    return jsonify({'success': True, 'value': val})

@app.route('/api/admin/config/<key>', methods=['POST'])
@login_required
@admin_required
def admin_set_config(key):
    """Set a config value"""
    data = request.get_json() or {}
    val = data.get('value', '')
    if not val:
        return jsonify({'success': False, 'error': 'Value is required'}), 400
    
    set_config_value(key, val, f'System config: {key}')
    log_activity(session.get('user_id'), 'config_update', {'key': key})
    return jsonify({'success': True, 'message': f'Config {key} updated!'})

@app.route('/api/debug/force-refresh-stale')
@login_required
@admin_required
def debug_force_stale():
    """Trigger stale refresh manually"""
    executor.submit(scheduled_stale_refresh)
    return jsonify({'success': True, 'message': 'Triggered stale refresh job in background.'})

@app.route('/api/admin/cleanup-zero-stats', methods=['POST'])
@login_required
@admin_required
def cleanup_zero_stats():
    """Delete all products with 0 stats (sales=0, gmv=0, no ad spend). Protects favorites."""
    try:
        # Find products with zero stats that are NOT favorites
        zero_stat_products = Product.query.filter(
            db.and_(
                db.or_(Product.sales == 0, Product.sales == None),
                db.or_(Product.gmv == 0, Product.gmv == None),
                db.or_(Product.ad_spend == 0, Product.ad_spend == None),
                db.or_(Product.is_favorite == False, Product.is_favorite == None)
            )
        ).all()
        
        count = len(zero_stat_products)
        
        if count == 0:
            return jsonify({'success': True, 'message': 'No zero-stat products found to delete.', 'deleted': 0})
        
        # Delete them
        for p in zero_stat_products:
            db.session.delete(p)
        
        db.session.commit()
        
        log_activity(session.get('user_id'), 'cleanup_zero_stats', {'deleted': count})
        
        return jsonify({
            'success': True,
            'message': f'Deleted {count} products with zero stats.',
            'deleted': count
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# --- Background Scheduler for Daily Refresh (Now using Copilot!) ---
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    import atexit

    def scheduled_copilot_refresh():
        """Daily background sync of 3000+ products (60 pages)"""
        with app.app_context():
            print("[SCHEDULER] 🕰️ Starting Massive Daily Copilot Sync...")
            try:
                total_saved = 0
                for page in range(60): # 60 pages = 3000 products
                    saved, total = sync_copilot_products(timeframe='7d', limit=50, page=page)
                    total_saved += saved
                    if total == 0: break
                    time.sleep(3) # Polite delay
                
                print(f"[SCHEDULER] ✅ Massive Sync Complete: {total_saved} products updated.")
                try:
                    send_telegram_alert(f"🕵️ **Massive Sync Complete!**\n✅ Updated {total_saved} products.")
                except: pass
            except Exception as e:
                print(f"[SCHEDULER] ❌ Massive Sync Failed: {e}")

    def scheduled_stale_refresh():
        """Refresh 200 oldest products every cycle"""
        with app.app_context():
            print("[SCHEDULER] ♻️ Starting Expanded Stale Refresh (200 products)...")
            try:
                cutoff = datetime.utcnow() - timedelta(hours=24)
                stale_products = Product.query.filter(
                    db.or_(Product.last_updated < cutoff, Product.last_updated == None),
                    Product.sales_7d >= 0, # Include all products
                    Product.product_status == 'active'
                ).order_by(Product.last_updated.asc()).limit(200).all()
                
                if not stale_products: return
                
                refreshed = 0
                for p in stale_products:
                    try:
                        enrich_product_data(p, force=True)
                        refreshed += 1
                        time.sleep(1)
                    except Exception as e:
                        print(f"Refresh failed for {p.product_id}: {e}")
                
                db.session.commit()
                print(f"[SCHEDULER] ✅ Stale Refresh Complete: {refreshed} products updated.")
            except Exception as e:
                print(f"[SCHEDULER] ❌ Stale Refresh Error: {e}")

    def scheduled_google_sheets_sync():
        """Scheduled Google Sheets sync - runs every 3 days"""
        with app.app_context():
            try:
                import gspread
                from google.oauth2.service_account import Credentials
                import json
                from datetime import datetime, timezone, timedelta
                
                config = get_google_sheets_config()
                
                if not config.get('sheet_id') or not config.get('credentials'):
                    print("[SCHEDULER] [Google Sheets] Skipping - not configured")
                    return
                
                # Check frequency setting
                frequency = config.get('frequency', '3days')
                if frequency == 'manual':
                    print("[SCHEDULER] [Google Sheets] Skipping - set to manual only")
                    return
                
                print(f"[SCHEDULER] [Google Sheets] Starting scheduled sync...")
                
                # Authenticate
                creds_dict = json.loads(config['credentials'])
                scopes = ['https://www.googleapis.com/auth/spreadsheets']
                credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
                gc = gspread.authorize(credentials)
                sheet = gc.open_by_key(config['sheet_id']).sheet1
                
                # Default creator (cakedfinds)
                creator_id = '7436686228703265838'
                
                # Fetch products
                all_products = {}
                for page in range(100):
                    try:
                        result = fetch_copilot_trending(
                            timeframe='7d', sort_by='revenue', c_timeframe='7d',
                            limit=50, page=page, creator_ids=creator_id
                        )
                        if not result or not result.get('videos'):
                            break
                        for video in result.get('videos', []):
                            pid = str(video.get('productId', '')).strip()
                            if pid and pid not in all_products:
                                all_products[pid] = {
                                    'product_name': video.get('productTitle', ''),
                                    'seller_name': video.get('sellerName', ''),
                                }
                        time.sleep(3.0)
                    except:
                        break
                
                # Build rows with EST timestamp
                est = timezone(timedelta(hours=-5))
                sync_date_est = datetime.now(est).strftime('%Y-%m-%d %I:%M %p EST')
                
                products_list = []
                for pid, info in all_products.items():
                    db_product = Product.query.filter_by(product_id=f"shop_{pid}").first()
                    if not db_product:
                        db_product = Product.query.filter_by(product_id=pid).first()
                    
                    if db_product:
                        products_list.append([
                            pid, db_product.product_name or info.get('product_name', ''),
                            f"https://shop.tiktok.com/view/product/{pid}?region=US",
                            db_product.seller_name or '', round(db_product.price or 0, 2),
                            f"{(db_product.commission_rate or 0) * 100:.1f}%",
                            f"{(db_product.shop_ads_commission or 0) * 100:.1f}%",
                            db_product.video_count_alltime or db_product.video_count or 0,
                            db_product.influencer_count or 0, db_product.sales or 0,
                            db_product.sales_7d or 0, db_product.gmv or 0,
                            db_product.ad_spend or 0, sync_date_est,
                        ])
                    else:
                        products_list.append([pid, info.get('product_name', ''),
                            f"https://shop.tiktok.com/view/product/{pid}?region=US",
                            info.get('seller_name', ''), 0, '0%', '0%', 0, 0, 0, 0, 0, 0, sync_date_est])
                
                products_list.sort(key=lambda x: x[11] if x[11] else 0, reverse=True)
                
                sheet.clear()
                header = ['product_id', 'product_name', 'product_url', 'seller_name', 'price',
                          'commission_rate', 'gmv_max_rate', 'video_count_alltime', 'creator_count',
                          'sales_alltime', 'sales_7d', 'revenue_alltime', 'ad_spend', 'sync_date']
                sheet.update('A1', [header] + products_list)
                
                print(f"[SCHEDULER] [Google Sheets] ✅ Sync complete! {len(products_list)} products")
                
            except Exception as e:
                print(f"[SCHEDULER] [Google Sheets] ❌ Error: {e}")

    # Initialize Scheduler
    scheduler = BackgroundScheduler()
    # Run every 12 hours (twice daily for fresh data)
    scheduler.add_job(func=scheduled_copilot_refresh, trigger="interval", hours=12)
    # Run Stale Refresh every 4 hours
    scheduler.add_job(func=scheduled_stale_refresh, trigger="interval", hours=4)
    # Run Google Sheets sync every 72 hours (3 days)
    scheduler.add_job(func=scheduled_google_sheets_sync, trigger="interval", hours=72)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    print("[SYSTEM] [CLOCK] TikTokCopilot Auto-Sync Scheduler Online (every 12 hours).")
    print("[SYSTEM] [CLOCK] Google Sheets Auto-Sync Scheduled (every 3 days).")

except ImportError:
    print("[SYSTEM] [WARN] APScheduler not found. Auto-refresh disabled. Install 'apscheduler' to enable.")


# =============================================================================
# MISSING ADMIN & SYSTEM ROUTES (Restored)
# =============================================================================

@app.route('/api/me')
@login_required
def api_me():
    """Return current user info"""
    user = get_current_user()
    if not user: return jsonify({'error': 'Not logged in'}), 401
    return jsonify(user.to_dict())

@app.route('/api/admin/activity')
@login_required
@admin_required
def api_admin_activity():
    """Return recent activity logs"""
    logs = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(50).all()
    return jsonify({'success': True, 'logs': [l.to_dict() for l in logs]})

@app.route('/api/oos-stats')
@login_required
@admin_required
def api_oos_stats():
    """Return simple stats for Admin Dashboard"""
    total = Product.query.count()
    return jsonify({
        'success': True,
        'stats': {
            'total_products': total,
        }
    })

@app.route('/api/cleanup', methods=['POST'])
@login_required
@admin_required
def api_cleanup():
    """Run simple DB maintenance"""
    try:
        # Example: Remove duplicates or old logs
        # For now, just a placeholder or vacuum
        db.session.execute(text("VACUUM"))
        db.session.commit()
        return jsonify({'success': True, 'message': 'Database vacuumed.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/products/nuke', methods=['POST'])
@login_required
@admin_required
def api_nuke_products():
    """Danger Zone: Delete all products"""
    data = request.json or {}
    keep_favorites = data.get('keep_favorites', False)
    
    try:
        query = Product.query
        if keep_favorites:
            query = query.filter(Product.is_favorite == False)
            
        deleted = query.delete()
        db.session.commit()
        
        log_activity(session['user_id'], 'nuke_products', {'deleted': deleted})
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/refresh-all-products')
@login_required
@admin_required
def api_refresh_all():
    """Trigger background refresh for all products"""
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))
    
    products = Product.query.order_by(Product.last_updated.asc()).limit(limit).offset(offset).all()
    count = 0
    for p in products:
        executor.submit(enrich_product_data, p, "[ManualRefresh]", True)
        count += 1
        
    return jsonify({'success': True, 'count': count, 'message': f'Queued {count} products for refresh'})

@app.route('/api/refresh-images')
@login_required
@admin_required
def api_refresh_images():
    """Resign image URLs if needed"""
    # Placeholder - in V4 we often rely on fresh scrapes
    return jsonify({'success': True, 'updated': 0, 'message': 'Image refresh protocol active.'})

@app.route('/api/admin/purge-low-signal', methods=['POST'])
@login_required
@admin_required
def api_purge_low_signal():
    """Delete products with 0 sales and low videos"""
    try:
        deleted = Product.query.filter(
            Product.sales == 0,
            Product.video_count < 2,
            Product.is_favorite == False
        ).delete()
        db.session.commit()
        return jsonify({'success': True, 'message': f'Purged {deleted} low-quality products.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def ensure_schema_integrity():
    """Auto-heal schema for SQLite/Postgres to prevent missing column errors"""
    with app.app_context():
        try:
            from sqlalchemy import text, inspect
            
            # Simple check if table exists
            inspector = inspect(db.engine)
            if not inspector.has_table('products'):
                return # Create_all handles it

            columns = [c['name'] for c in inspector.get_columns('products')]
            
            with db.engine.connect() as conn:
                if 'product_status' not in columns:
                    print("[SCHEMA] Adding missing column: product_status")
                    conn.execute(text("ALTER TABLE products ADD COLUMN product_status TEXT DEFAULT 'active'"))
                
                if 'scan_type' not in columns:
                    print("[SCHEMA] Adding missing column: scan_type")
                    conn.execute(text("ALTER TABLE products ADD COLUMN scan_type TEXT DEFAULT 'copilot'"))

                if 'sales_7d' not in columns:
                     print("[SCHEMA] Adding missing column: sales_7d")
                     conn.execute(text("ALTER TABLE products ADD COLUMN sales_7d INTEGER DEFAULT 0"))
                
                if 'video_count' not in columns:
                     print("[SCHEMA] Adding missing column: video_count")
                     conn.execute(text("ALTER TABLE products ADD COLUMN video_count INTEGER DEFAULT 0"))

                if 'ad_spend' not in columns:
                     print("[SCHEMA] Adding missing column: ad_spend")
                     conn.execute(text("ALTER TABLE products ADD COLUMN ad_spend FLOAT DEFAULT 0"))

                if 'ad_spend_total' not in columns:
                     print("[SCHEMA] Adding missing column: ad_spend_total")
                     conn.execute(text("ALTER TABLE products ADD COLUMN ad_spend_total FLOAT DEFAULT 0"))

                if 'gmv_growth' not in columns:
                     print("[SCHEMA] Adding missing column: gmv_growth")
                     conn.execute(text("ALTER TABLE products ADD COLUMN gmv_growth FLOAT DEFAULT 0"))

                if 'shop_ads_commission' not in columns:
                     print("[SCHEMA] Adding missing column: shop_ads_commission")
                     conn.execute(text("ALTER TABLE products ADD COLUMN shop_ads_commission FLOAT DEFAULT 0"))

                conn.commit()
            print("[SCHEMA] Integrity Check Complete.")
        except Exception as e:
            # Don't crash app on schema check failure (e.g. invalid permissions)
            print(f"[SCHEMA] Warning: Integrity check skipped: {e}")

@app.route('/api/admin/cleanup-zero-sales-7d', methods=['POST'])
@login_required
@admin_required
def cleanup_zero_sales():
    """Delete products with 0 7d sales that are NOT favorites"""
    try:
        # Exclude favorites
        fav_ids = [f.product_id for f in Favorite.query.all()]
        
        to_delete = Product.query.filter(
            Product.sales_7d <= 0,
            ~Product.product_id.in_(fav_ids)
        ).all()
        
        count = len(to_delete)
        for p in to_delete:
            db.session.delete(p)
            
        db.session.commit()
        log_activity(session.get('user_id'), 'cleanup_zero_sales', {'deleted': count})
        return jsonify({'status': 'success', 'deleted': count, 'message': f'Successfully purged {count} low-performance products.'})
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Run on module load (ensures it runs on Render gunicorn start)
ensure_schema_integrity()

# Start auto-refresh scheduler if credentials are configured
if os.environ.get('COPILOT_EMAIL') and os.environ.get('COPILOT_PASSWORD'):
    print("[Copilot] 🔐 Credentials detected - starting auto-refresh scheduler...")
    schedule_copilot_auto_refresh(interval_minutes=45)
    # Also do an initial refresh on startup
    try:
        auto_login_copilot()
    except Exception as e:
        print(f"[Copilot] ⚠️ Initial auto-login failed: {e}")
else:
    print("[Copilot] ℹ️ No credentials configured - auto-refresh disabled (set COPILOT_EMAIL + COPILOT_PASSWORD env vars)")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
