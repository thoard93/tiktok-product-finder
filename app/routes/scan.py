"""
Vantage — Scan Blueprint
All scanning, importing, refreshing, and enrichment routes.
"""

import os
import re
import sys
import json
import time
import random
import secrets
import subprocess
import traceback
import requests
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, session, current_app
from requests.auth import HTTPBasicAuth
from app import db, executor
from app.models import Product
from app.routes.auth import (
    login_required, admin_required, get_current_user, log_activity,
    get_config_value, set_config_value, DEV_PASSKEY,
)

# =============================================================================
# BLUEPRINT
# =============================================================================

scan_bp = Blueprint('scan', __name__)

# --- GLOBAL SCAN LOCK ---
SCAN_LOCK = {
    'locked': False,
    'locked_by': None,
    'scan_type': None,
    'start_time': None
}


def get_scan_status():
    return SCAN_LOCK

# =============================================================================
# CONFIG (loaded from environment)
# =============================================================================

# EchoTik API
ECHOTIK_V3_BASE = "https://open.echotik.live/api/v1"
BASE_URL = ECHOTIK_V3_BASE
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')
ECHOTIK_PROXY_STRING = os.environ.get('ECHOTIK_PROXY_STRING')

# TikTok Partner
TIKTOK_PARTNER_COOKIE = os.environ.get('TIKTOK_PARTNER_COOKIE')

# DailyVirals
DV_BACKEND_URL = "https://backend.thedailyvirals.com/api/videos/stats/top-growth-by-date-range"
DV_API_TOKEN = os.environ.get('DAILYVIRALS_TOKEN', '')
DV_PROXY_STRING = os.environ.get('DAILYVIRALS_PROXY', '')

# Apify
APIFY_API_TOKEN = os.environ.get('APIFY_API_TOKEN', '')

basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def get_auth():
    """Get HTTPBasicAuth object for EchoTik API"""
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)


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
    return random.choice(uas)


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

        if not pid:
            pid = f"apify_unknown_{hash(url) % 10000}"

        processed.append({
            'product_id': pid,
            'title': title,
            'advertiser': advertiser,
            'url': url,
            'image_url': item.get('video_cover_url') or item.get('thumbnailUrl') or item.get('coverUrl') or '',
            'likes': item.get('likes') or item.get('likeCount') or 0,
            'views': item.get('impressions') or item.get('views') or item.get('viewCount') or 0,
        })

    return processed


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
             title_match = re.search(r'"title":"(.*?)","', html) or re.search(r'<title>(.*?)</title>', html)

             # Basic Data Save (So user sees meaningful card immediately)
             with current_app.app_context():
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


# =============================================================================
# SCAN ROUTES
# =============================================================================

@scan_bp.route('/api/scan/manual', methods=['POST'])
@login_required
def manual_scan_import():
    """
    Import products from DailyVirals videos JSON.
    Extracts product IDs and fetches full stats from EchoTik.
    """
    # Import these at function scope to avoid circular imports
    from app import app as flask_app
    try:
        # Access helpers from the monolith (still in app.py during migration)
        from app import app as _app
        import importlib
        main_mod = importlib.import_module('app')
    except Exception:
        pass

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

        # Import monolith helpers (still in app.py during migration)
        import app as monolith
        fetch_product_details_echotik = getattr(monolith, 'fetch_product_details_echotik', None)
        fetch_seller_name = getattr(monolith, 'fetch_seller_name', None)
        save_or_update_product = getattr(monolith, 'save_or_update_product', None)

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
                    print(f"[DV Import] Saved {raw_id} from {source}")

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
                    print(f"[DV Import] {raw_id}: EchoTik unavailable, used fallback")

            except Exception as e:
                error_count += 1
                errors.append(f"{raw_id}: {str(e)[:50]}")
                print(f"[DV Import] {raw_id}: {e}")

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


@scan_bp.route('/api/scan/dv-live', methods=['POST'])
@login_required
def scan_dailyvirals_live():
    """DailyVirals Live Scraper (direct API automation)"""
    # Import monolith helpers
    import app as monolith
    fetch_product_details_echotik = getattr(monolith, 'fetch_product_details_echotik', None)
    fetch_seller_name = getattr(monolith, 'fetch_seller_name', None)
    save_or_update_product = getattr(monolith, 'save_or_update_product', None)

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


@scan_bp.route('/api/scan/partner_opportunity', methods=['POST'])
@admin_required
def trigger_partner_opportunity_scan():
    """Endpoint to trigger the TikTok Partner Center scan"""
    if not TIKTOK_PARTNER_COOKIE:
        return jsonify({'success': False, 'error': 'TIKTOK_PARTNER_COOKIE not configured in environment.'}), 400

    # Run in background via executor
    executor.submit(_scan_partner_opportunity_live)
    return jsonify({'success': True, 'message': 'Partner Opportunity scan started in background. Results will appear in the Opportunities tab.'})


def _scan_partner_opportunity_live():
    """
    Scrapes the TikTok Shop Partner Center 'High Opportunity' pool.
    Uses humans-centric rate limiting and proxy rotation for safety.
    """
    from curl_cffi import requests as curl_requests
    import app as monolith
    fetch_product_details_echotik = getattr(monolith, 'fetch_product_details_echotik', None)
    extract_metadata_from_echotik = getattr(monolith, 'extract_metadata_from_echotik', None)
    save_or_update_product = getattr(monolith, 'save_or_update_product', None)
    parse_kmb_string = getattr(monolith, 'parse_kmb_string', None)
    safe_float = getattr(monolith, 'safe_float', None)
    send_telegram_alert = getattr(monolith, 'send_telegram_alert', None)

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
        with current_app._get_current_object().app_context():
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

                with current_app._get_current_object().app_context():
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
            send_telegram_alert(f"Warning: **Partner Scan Failed**\nError: `{str(e_fatal)[:200]}`")
        except: pass
    finally:
        with current_app._get_current_object().app_context():
            db.session.remove()

    print(f"[Partner Scan] Finished. Total new/updated opportunities: {total_saved}")


@scan_bp.route('/api/run-viral-trends-scan', methods=['POST'])
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


@scan_bp.route('/api/run-apify-scan', methods=['POST'])
def run_apify_scan():
    """Triggers the Apify Shop Scanner script synchronously and returns output."""
    try:
        # Use python executable relative to environment
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'apify_shop_scanner.py')

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


@scan_bp.route('/api/refresh-ads', methods=['POST'])
def refresh_daily_virals_ads():
    """Batch refresh enrichment for 'Ad Winners' (DailyVirals) products."""
    import app as monolith
    enrich_product_data = getattr(monolith, 'enrich_product_data', None)

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
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500


@scan_bp.route('/api/refresh-images', methods=['POST', 'GET'])
@login_required # Only allow logged in users
def refresh_images():
    """
    Refresh cached image URLs for products using SINGLE-PRODUCT API calls.
    Returns: JSON with stats on progress.
    """
    import app as monolith
    parse_cover_url = getattr(monolith, 'parse_cover_url', None)
    get_cached_image_urls = getattr(monolith, 'get_cached_image_urls', None)
    fetch_product_details_echotik = getattr(monolith, 'fetch_product_details_echotik', None)
    extract_metadata_from_echotik = getattr(monolith, 'extract_metadata_from_echotik', None)

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
            products = Product.query.filter(
                Product.image_url.isnot(None),
                Product.image_url != '',
                db.or_(
                    Product.cached_image_url.is_(None),
                    Product.cached_image_url == '',
                    Product.image_cached_at.is_(None),
                    Product.image_cached_at < stale_threshold,
                    Product.image_url.contains('volces.com'),
                    Product.cached_image_url.contains('volces.com'),
                    Product.cached_image_url.is_(None)
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


@scan_bp.route('/api/refresh-product/<product_id>', methods=['POST'])
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
        raw_vids = int(p.get('total_video_cnt', 0) or 0)
        if raw_vids > 0:
            # Update all-time counts (never downgrade)
            if raw_vids > (product.video_count_alltime or 0):
                product.video_count_alltime = raw_vids
                product.video_count = raw_vids

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
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500


@scan_bp.route('/api/deep-refresh', methods=['GET', 'POST'])
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

        print(f"Deep refresh starting: {total_matching} products match criteria, force={force_all}, continuous={continuous}")

        while True:
            iteration += 1

            # Build query based on mode
            if force_all:
                products = Product.query.filter(
                    db.or_(*base_conditions)
                ).order_by(Product.product_id).offset(current_offset).limit(batch_size).all()
                current_offset += batch_size
            else:
                products = Product.query.filter(
                    db.or_(*base_conditions),
                    db.or_(
                        Product.last_updated.is_(None),
                        Product.last_updated < refresh_started
                    )
                ).limit(batch_size).all()

            if not products:
                print(f"No more products to process at iteration {iteration}")
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

            print(f"Deep refresh iteration {iteration}: processed {processed_this_batch}, updated {updated_this_batch}, api_errors {api_errors} (commission: {commission_fixed}, sales: {sales_fixed}, images: {images_fixed})")

            # Break conditions
            if not continuous:
                break
            if iteration >= max_iterations:
                print(f"Reached max iterations ({max_iterations})")
                break
            if processed_this_batch == 0:
                print(f"No products processed this batch")
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
        db.session.rollback()
        print(f"Deep refresh error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500


@scan_bp.route('/api/refresh-all-products')
@login_required
@admin_required
def api_refresh_all():
    """Trigger background refresh for all products"""
    import app as monolith
    enrich_product_data = getattr(monolith, 'enrich_product_data', None)

    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))

    products = Product.query.order_by(Product.last_updated.asc()).limit(limit).offset(offset).all()
    count = 0
    for p in products:
        executor.submit(enrich_product_data, p, "[ManualRefresh]", True)
        count += 1

    return jsonify({'success': True, 'count': count, 'message': f'Queued {count} products for refresh'})


@scan_bp.route('/api/detect-oos', methods=['GET', 'POST'])
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


@scan_bp.route('/api/scan-pages/<seller_id>', methods=['GET'])
@login_required
def api_scan_brand_pages(seller_id):
    """Restored specific Brand ID Scan"""
    import app as monolith
    save_or_update_product = getattr(monolith, 'save_or_update_product', None)

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
# DEPRECATED COPILOT STUB
# =============================================================================

@scan_bp.route('/api/copilot/sync', methods=['GET', 'POST'])
@login_required
@admin_required
def copilot_sync_deprecated():
    return jsonify({'success': False, 'error': 'TikTokCopilot API shut down Feb 2026. Use EchoTik instead.'}), 503
