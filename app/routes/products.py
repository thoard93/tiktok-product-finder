"""
PRISM — Product Routes Blueprint
All product-related routes: CRUD, listing, lookup, favorites, brand products,
brand hunter, hidden gems, OOS, image proxy, enrichment, and page routes.
"""

import os
import re
import json
import time
import traceback
import requests
import base64
from datetime import datetime
from urllib.parse import unquote
from functools import wraps

from flask import (
    Blueprint, jsonify, request, send_from_directory, redirect,
    session, url_for, Response, current_app
)
from sqlalchemy import func, or_, text

from app import db
from app.models import Product, WatchedBrand, BlacklistedBrand

from app.routes.auth import login_required, admin_required, subscription_required, get_current_user, log_activity


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

products_bp = Blueprint('products', __name__)


# ---------------------------------------------------------------------------
# Config — pulled from environment (same as monolithic app.py)
# ---------------------------------------------------------------------------

ECHOTIK_V3_BASE = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_REALTIME_BASE = "https://open.echotik.live/api/v3/realtime"
BASE_URL = ECHOTIK_V3_BASE
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')
DV_PROXY_STRING = os.environ.get('DAILYVIRALS_PROXY', '')

try:
    from curl_cffi import requests as requests_cffi
except ImportError:
    requests_cffi = None

try:
    from fuzzywuzzy import fuzz, process
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False

from requests.auth import HTTPBasicAuth


def get_auth():
    """Get HTTPBasicAuth object for EchoTik API"""
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)


# ---------------------------------------------------------------------------
# Shared helpers (moved verbatim from app.py)
# ---------------------------------------------------------------------------

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
    Global Helper: Enrich product data using EchoTik API.
    Replaces the defunct TikTokCopilot pipeline.
    """
    from app.services.echotik import fetch_product_detail, EchoTikError

    # Helper for robust attribute access
    def gv(obj, key, default=None):
        if isinstance(obj, dict): return obj.get(key, default)
        return getattr(obj, key, default)

    def sv(obj, key, val):
        if isinstance(obj, dict): obj[key] = val
        else: setattr(obj, key, val)

    pid = gv(p, 'product_id')
    if not pid:
        return False, "No Product ID"

    raw_pid = str(pid).replace('shop_', '')

    print(f"{i_log_prefix} Enriching {raw_pid} via EchoTik...")

    try:
        detail = fetch_product_detail(raw_pid)
    except EchoTikError as exc:
        print(f"{i_log_prefix} EchoTik error for {raw_pid}: {exc}")
        return False, f"EchoTik API error: {exc}"

    if not detail:
        print(f"{i_log_prefix} EchoTik returned no data for {raw_pid}")
        return False, "Product not found in EchoTik"

    # Apply EchoTik data — the _normalize_product() in echotik.py already maps
    # to canonical field names matching our Product model
    sv(p, 'product_name', detail.get('product_name') or gv(p, 'product_name'))
    sv(p, 'seller_name', detail.get('seller_name') or gv(p, 'seller_name'))
    sv(p, 'seller_id', detail.get('seller_id') or gv(p, 'seller_id'))
    sv(p, 'image_url', detail.get('image_url') or gv(p, 'image_url'))
    sv(p, 'product_url', detail.get('product_url') or gv(p, 'product_url'))

    # Sales & GMV
    sales = detail.get('sales') or 0
    sales_7d = detail.get('sales_7d') or 0
    sales_30d = detail.get('sales_30d') or 0
    gmv = detail.get('gmv') or 0
    gmv_30d = detail.get('gmv_30d') or 0

    if sales > 0: sv(p, 'sales', sales)
    if sales_7d > 0: sv(p, 'sales_7d', sales_7d)
    if sales_30d > 0: sv(p, 'sales_30d', sales_30d)
    if gmv > 0: sv(p, 'gmv', gmv)
    if gmv_30d > 0: sv(p, 'gmv_30d', gmv_30d)

    # Video counts — never downgrade all-time
    v_alltime = detail.get('video_count_alltime') or 0
    v_7d = detail.get('video_count_7d') or 0
    current_alltime = int(gv(p, 'video_count_alltime') or 0)
    current_vcount = int(gv(p, 'video_count') or 0)

    if v_alltime > current_alltime:
        sv(p, 'video_count_alltime', v_alltime)
    if v_alltime > current_vcount:
        sv(p, 'video_count', v_alltime)
    if v_7d > 0:
        sv(p, 'video_7d', v_7d)

    # Influencer count — never downgrade
    inf = detail.get('influencer_count') or 0
    current_inf = int(gv(p, 'influencer_count') or 0)
    if inf > current_inf:
        sv(p, 'influencer_count', inf)

    # Pricing & commission
    comm = detail.get('commission_rate') or 0
    price = detail.get('price') or 0
    if comm > 0: sv(p, 'commission_rate', comm)
    if price > 0: sv(p, 'price', price)

    # Ad spend
    ad_spend = detail.get('ad_spend') or 0
    if ad_spend > 0: sv(p, 'ad_spend', ad_spend)

    # Ratings & quality
    rating = detail.get('rating') or 0
    reviews = detail.get('review_count') or 0
    if rating > 0: sv(p, 'product_rating', rating)
    if reviews > 0: sv(p, 'review_count', reviews)

    # Category
    if detail.get('category'): sv(p, 'category', detail['category'])
    if detail.get('subcategory'): sv(p, 'subcategory', detail['subcategory'])

    # Metadata
    sv(p, 'last_updated', datetime.utcnow())
    sv(p, 'last_echotik_sync', datetime.utcnow())

    # Reject products with zero recent sales
    if sales_7d <= 0:
        return False, f"Zero 7D sales ({sales_7d})"

    return True, "Enriched via EchoTik"


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


def is_brand_blacklisted(seller_name=None, seller_id=None):
    """Check if a brand is blacklisted by name or ID"""
    if seller_id:
        return BlacklistedBrand.query.filter_by(seller_id=seller_id).first() is not None
    if seller_name:
        # Check for case-insensitive match
        return BlacklistedBrand.query.filter(BlacklistedBrand.seller_name.ilike(seller_name)).first() is not None
    return False


# ---------------------------------------------------------------------------
# External service stubs — these are defined in app.py and will be moved to
# app/services/ in a future refactor.  For now, import from the monolith at
# runtime so existing behavior is preserved.
# ---------------------------------------------------------------------------

def _lazy_import_from_monolith(name):
    """Import a function from the monolithic app.py at call-time."""
    import importlib
    mod = importlib.import_module('app_module_compat')
    return getattr(mod, name)


# These functions are referenced by routes below but live outside this module.
# They will be provided by whichever layer currently defines them (app.py stubs,
# services, etc.).  We define thin wrappers so the routes compile cleanly.

def extract_metadata_from_echotik(p_data):
    """Proxy — delegates to the monolith's extract_metadata_from_echotik."""
    # Imported lazily to avoid circular imports during app factory startup.
    import sys
    main_mod = sys.modules.get('__main__')
    fn = getattr(main_mod, 'extract_metadata_from_echotik', None)
    if fn:
        return fn(p_data)
    # Fallback: return empty dict matching expected keys
    return {
        'product_name': None, 'seller_name': None, 'seller_id': None,
        'image_url': None, 'product_url': None, 'price': 0, 'sales': 0,
        'sales_7d': 0, 'sales_30d': 0, 'influencer_count': 0,
        'video_count': 0, 'live_count': 0, 'views_count': 0,
        'commission_rate': 0, 'product_rating': 0, 'review_count': 0,
    }


def fetch_product_details_echotik(product_id, force=False, allow_paid=False):
    """Proxy — delegates to the monolith's fetch_product_details_echotik."""
    import sys
    main_mod = sys.modules.get('__main__')
    fn = getattr(main_mod, 'fetch_product_details_echotik', None)
    if fn:
        return fn(product_id, force=force, allow_paid=allow_paid)
    return None, None


def fetch_seller_name(seller_id):
    """Proxy — delegates to the monolith's fetch_seller_name."""
    import sys
    main_mod = sys.modules.get('__main__')
    fn = getattr(main_mod, 'fetch_seller_name', None)
    if fn:
        return fn(seller_id)
    return None


def fetch_copilot_trending(**kwargs):
    """Stub — TikTokCopilot API was shut down Feb 4, 2026. Use EchoTik instead."""
    return None


# ---------------------------------------------------------------------------
# Product lookup helpers
# ---------------------------------------------------------------------------

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


# =============================================================================
# PAGE ROUTES
# =============================================================================

@products_bp.route('/vantage_logo.png')
def serve_logo():
    return send_from_directory('pwa', 'vantage_logo.png')


@products_bp.route('/favicon.ico')
def favicon():
    """Serve favicon — uses the manifest SVG icon as fallback."""
    return Response(
        '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
        <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
        <stop stop-color="#7c3aed"/><stop offset="1" stop-color="#06b6d4"/>
        </linearGradient></defs>
        <rect width="32" height="32" rx="6" fill="#0a0a0f"/>
        <path d="M16 4L28 26H4Z" fill="url(#g)" opacity="0.9"/>
        </svg>''',
        mimetype='image/svg+xml',
        headers={'Cache-Control': 'public, max-age=86400'}
    )


@products_bp.route('/legacy-index')
def legacy_index():
    """Legacy index — /  is now handled by views_bp."""
    return redirect('/')

@products_bp.route('/product/<path:product_id>')
@login_required
def product_detail(product_id):
    return send_from_directory('pwa', 'product_detail_v4.html')

@products_bp.route('/scanner')
@login_required
@admin_required
def scanner_page():
    return send_from_directory('pwa', 'scanner_v4.html')

@products_bp.route('/settings')
@login_required
def settings_page():
    return send_from_directory('pwa', 'settings.html')

@products_bp.route('/brand-hunter')
@login_required
def brand_hunter_page():
    return send_from_directory('pwa', 'brand_hunter.html')

@products_bp.route('/vantage-v2')
@login_required
def vantage_v2_page():
    return send_from_directory('pwa', 'vantage_v2.html')

@products_bp.route('/pwa/<path:filename>')
def pwa_files(filename):
    # Static assets (CSS, JS, images, fonts, manifests) — always public, never auth-gated.
    # Blocking these behind auth causes 302 redirects instead of actual files,
    # which breaks styling/scripts on every page.
    static_prefixes = ('css/', 'js/', 'img/', 'fonts/', 'icons/')
    static_extensions = ('.css', '.js', '.png', '.jpg', '.svg', '.ico', '.woff', '.woff2', '.json', '.webmanifest')
    if filename.startswith(static_prefixes) or filename.endswith(static_extensions):
        return send_from_directory('pwa', filename)
    # Allow login.html without auth
    if filename in ['login.html']:
        return send_from_directory('pwa', filename)
    # Other PWA files (HTML pages) need auth check
    if not session.get('user_id'):
        return redirect('/login')
    return send_from_directory('pwa', filename)


# =============================================================================
# PRODUCT ENRICHMENT
# =============================================================================

@products_bp.route('/api/product/enrich/<path:product_id>')
@login_required
@subscription_required
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


# =============================================================================
# IMAGE PROXY
# =============================================================================

@products_bp.route('/api/image-proxy/<path:product_id>')
def image_proxy(product_id):
    """Proxy image requests to bypass TikTok direct-link blocks and fix metadata."""
    try:
        raw_id = product_id.replace('shop_', '')

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


# =============================================================================
# PRODUCT LIST API
# =============================================================================

@products_bp.route('/api/products', methods=['GET'])
@login_required
@subscription_required
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
        min_vids = request.args.get('min_vids', 0, type=int)  # Default 0: show all products even before Phase 2 enriches video counts
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
            # High Volume products with significant ad investment
            query = query.filter(Product.sales_7d >= 100)

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

        # Skip generic video count filters when specialized filters handle their own video criteria
        if not (is_gems or is_caked):
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
            query = query.order_by(db.func.coalesce(Product.video_count_alltime, Product.video_count).asc().nullslast())
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
# PRODUCT DETAIL API
# =============================================================================

# Aliases for compatibility
@products_bp.route('/api/debug/check-product/<path:product_id>')
@products_bp.route('/api/product/<path:product_id>')
@login_required
@subscription_required
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


# =============================================================================
# PRODUCT VIDEO STATUS
# =============================================================================

@products_bp.route('/api/product/<product_id>/video-status', methods=['GET'])
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

    # Import Kling helper at runtime to avoid circular dependency
    import sys
    main_mod = sys.modules.get('__main__')
    get_kling_video_result = getattr(main_mod, 'get_kling_video_result', None)

    if not get_kling_video_result:
        return jsonify({'status': product.ai_video_status or 'unknown', 'task_id': product.ai_video_task_id})

    result = get_kling_video_result(product.ai_video_task_id)

    if result.get('status') == 'completed' and result.get('video_url'):
        product.ai_video_url = result['video_url']
        product.ai_video_status = 'completed'
        db.session.commit()
    elif result.get('status') == 'failed':
        product.ai_video_status = 'failed'
        db.session.commit()

    return jsonify(result)


# =============================================================================
# FAVORITES
# =============================================================================

@products_bp.route('/api/favorite/<product_id>', methods=['POST'])
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


@products_bp.route('/api/favorites', methods=['GET'])
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


# =============================================================================
# BRAND PRODUCTS (by seller_name)
# =============================================================================

@products_bp.route('/api/brand-products/<seller_name>')
@login_required
@subscription_required
def get_brand_products_by_name(seller_name):
    """Get products for a specific brand by seller_name"""
    try:
        # URL decode the seller name
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


@products_bp.route('/api/brand-sync/<seller_name>', methods=['POST'])
@login_required
@subscription_required
def sync_brand_by_name(seller_name):
    """Sync/refresh products for a brand (placeholder - stats are computed on-the-fly)"""
    try:
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


# =============================================================================
# PRODUCT LOOKUP
# =============================================================================

@products_bp.route('/api/lookup', methods=['GET', 'POST'])
@login_required
@subscription_required
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


@products_bp.route('/api/lookup/batch', methods=['POST'])
@login_required
@subscription_required
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
# HIDDEN GEMS & OOS
# =============================================================================

@products_bp.route('/api/hidden-gems', methods=['GET'])
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


@products_bp.route('/api/oos-products', methods=['GET'])
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


# =============================================================================
# BRAND HUNTER API ENDPOINTS
# =============================================================================

@products_bp.route('/api/brands', methods=['GET'])
@login_required
@subscription_required
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

@products_bp.route('/api/brands/discover', methods=['GET'])
@login_required
@subscription_required
def api_discover_brands():
    """Discover top brands from existing products, sorted by GMV"""
    try:
        # Get already tracked brand names for filtering
        tracked_names = [b.name.lower() for b in WatchedBrand.query.all() if b.name]

        # Aggregate products by seller_name
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

@products_bp.route('/api/brands', methods=['POST'])
@login_required
@subscription_required
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

@products_bp.route('/api/brands/<int:brand_id>', methods=['DELETE'])
@login_required
@subscription_required
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

@products_bp.route('/api/brands/<int:brand_id>/sync', methods=['POST'])
@login_required
@subscription_required
def api_sync_brand(brand_id):
    """Refresh stats for a specific brand from products in the database."""
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

@products_bp.route('/api/brands/<int:brand_id>/products', methods=['GET'])
@login_required
@subscription_required
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

@products_bp.route('/api/brands/refresh-all', methods=['POST'])
@login_required
@subscription_required
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

@products_bp.route('/api/brands/init', methods=['POST'])
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

@products_bp.route('/api/brands/debug', methods=['GET'])
@login_required
@subscription_required
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

@products_bp.route('/api/brands/cleanup', methods=['POST'])
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

@products_bp.route('/api/brands/clear-all', methods=['POST'])
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

@products_bp.route('/api/brands/nuke', methods=['POST'])
@login_required
@admin_required
def api_nuke_brands():
    """Hard reset: Drop and recreate the watched_brands table"""
    try:
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

@products_bp.route('/api/brands/cleanup-full', methods=['POST'])
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
