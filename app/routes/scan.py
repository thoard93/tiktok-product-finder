"""
PRISM — Scan Blueprint
EchoTik product sync, single-product refresh, deep refresh, and OOS detection.
"""

import os
import time
import traceback
import requests
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
from requests.auth import HTTPBasicAuth
from app import db
from app.models import Product
from app.routes.auth import login_required, admin_required, log_activity

log = logging.getLogger(__name__)

# =============================================================================
# BLUEPRINT
# =============================================================================

scan_bp = Blueprint('scan', __name__)

# --- GLOBAL SCAN LOCK ---
SCAN_LOCK = {
    'locked': False,
    'locked_by': None,
    'scan_type': None,
    'start_time': None,
}

def get_scan_status():
    return SCAN_LOCK

# =============================================================================
# CONFIG
# =============================================================================

ECHOTIK_V3_BASE = "https://open.echotik.live/api/v1"
BASE_URL = ECHOTIK_V3_BASE
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')


def get_auth():
    """Get HTTPBasicAuth object for EchoTik API."""
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)


# =============================================================================
# ECHOTIK SYNC — Manual trigger + scheduled sync
# =============================================================================

@scan_bp.route('/api/scan/echotik-sync', methods=['POST'])
@login_required
@admin_required
def echotik_sync_trigger():
    """
    Manual trigger: fetch top products from EchoTik sorted by GMV,
    filter by commission/sales/price, save to DB.

    Optional JSON body:
        pages     — number of pages to fetch (default 10, max 25)
        page_size — products per page (default 50, max 50)
    """
    if SCAN_LOCK['locked']:
        return jsonify({
            'success': False,
            'error': f"Scan already running: {SCAN_LOCK['scan_type']}",
        }), 409

    try:
        SCAN_LOCK.update(locked=True, locked_by='admin', scan_type='echotik_sync',
                         start_time=datetime.utcnow().isoformat())

        data = request.get_json(silent=True) or {}
        max_pages = min(int(data.get('pages', 25)), 25)
        page_size = min(int(data.get('page_size', 10)), 10)  # EchoTik API max is 10

        result = run_echotik_sync(max_pages=max_pages, page_size=page_size)

        log_activity(None, 'echotik_sync', result)
        return jsonify({'success': True, **result})

    except Exception as e:
        db.session.rollback()
        log.exception("[SYNC] EchoTik sync failed")
        return jsonify({'success': False, 'error': str(e)}), 500

    finally:
        SCAN_LOCK.update(locked=False, locked_by=None, scan_type=None, start_time=None)


def run_echotik_sync(max_pages: int = 10, page_size: int = 10) -> dict:
    """
    Core sync logic — usable from both the API route and the scheduler.

    Fetches up to ``max_pages`` pages of trending products from EchoTik,
    filters by sync criteria, and upserts to the database via
    ``echotik.sync_to_db``.

    Sync criteria (applied post-fetch):
        * commission_rate > 0
        * sales_7d > 50
        * price between $5 and $200

    Returns dict with created/updated/errors/fetched/filtered counts.
    """
    from app.services.echotik import (
        fetch_trending_products, sync_to_db, EchoTikError,
    )

    all_products: list[dict] = []

    for page in range(1, max_pages + 1):
        try:
            products = fetch_trending_products(page=page, page_size=page_size)
            if not products:
                log.info("[SYNC] Page %d returned 0 products — stopping", page)
                break
            all_products.extend(products)
            log.info("[SYNC] Page %d: fetched %d products", page, len(products))
            time.sleep(0.5)
        except EchoTikError as exc:
            log.warning("[SYNC] Page %d failed: %s", page, exc)
            break

    if not all_products:
        return {'fetched': 0, 'filtered': 0, 'created': 0, 'updated': 0, 'errors': 0}

    total_fetched = len(all_products)

    # Apply sync criteria
    filtered = [
        p for p in all_products
        if (p.get('commission_rate') or 0) > 0
        and (p.get('sales_7d') or 0) > 50
        and 5 <= (p.get('price') or 0) <= 200
    ]

    log.info("[SYNC] %d of %d products passed filter criteria", len(filtered), total_fetched)

    if not filtered:
        return {'fetched': total_fetched, 'filtered': 0, 'created': 0, 'updated': 0, 'errors': 0}

    result = sync_to_db(filtered)
    result['fetched'] = total_fetched
    result['filtered'] = len(filtered)

    log.info(
        "[SYNC] Complete: %d fetched, %d filtered, %d new, %d updated, %d errors",
        total_fetched, len(filtered), result['created'], result['updated'], result['errors'],
    )

    return result


# =============================================================================
# SINGLE PRODUCT REFRESH
# =============================================================================

@scan_bp.route('/api/refresh-product/<product_id>', methods=['POST'])
def refresh_product_data(product_id):
    """Fetch fresh data for a product from EchoTik product detail API."""
    try:
        from app.services.echotik import fetch_product_detail, sync_to_db

        raw_id = str(product_id).replace('shop_', '')
        detail = fetch_product_detail(raw_id)

        if not detail:
            return jsonify({'success': False, 'error': 'Product not found on EchoTik'}), 404

        result = sync_to_db([detail])
        product = Product.query.get(product_id) or Product.query.get(f"shop_{raw_id}")

        return jsonify({
            'success': True,
            'message': 'Product data refreshed via EchoTik',
            'product': product.to_dict() if product else detail,
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# DEEP REFRESH — batch re-enrich stale products
# =============================================================================

@scan_bp.route('/api/deep-refresh', methods=['GET', 'POST'])
@login_required
def deep_refresh_products():
    """
    Re-fetch product detail for stale products in batches.
    Fixes 0% commission and bad sales data from bulk scans.

    Query params:
        batch  — products per iteration (default 50, max 100)
        continuous — keep running batches (default false)
        max_iterations — cap iterations (default 10, max 50)
    """
    try:
        from app.services.echotik import fetch_product_detail

        batch_size = min(int(request.args.get('batch', 50)), 100)
        continuous = request.args.get('continuous', 'false').lower() == 'true'
        max_iterations = min(int(request.args.get('max_iterations', 10)), 50)

        total_processed = 0
        total_updated = 0
        total_errors = 0
        iteration = 0

        while True:
            iteration += 1

            # Products with 0 commission or very low sales
            products = Product.query.filter(
                db.or_(
                    db.or_(Product.commission_rate == 0, Product.commission_rate.is_(None)),
                    Product.sales_7d <= 2,
                ),
                Product.product_status != 'likely_oos',
            ).limit(batch_size).all()

            if not products:
                break

            for product in products:
                total_processed += 1
                try:
                    raw_id = product.product_id.replace('shop_', '')
                    detail = fetch_product_detail(raw_id)

                    product.last_updated = datetime.utcnow()

                    if not detail:
                        total_errors += 1
                        continue

                    changed = False

                    new_comm = detail.get('commission_rate') or 0
                    if new_comm > 0 and (product.commission_rate or 0) == 0:
                        product.commission_rate = new_comm
                        changed = True

                    new_sales = detail.get('sales_7d') or 0
                    if new_sales > (product.sales_7d or 0):
                        product.sales_7d = new_sales
                        product.sales_30d = detail.get('sales_30d') or product.sales_30d
                        product.sales = detail.get('sales') or product.sales
                        product.gmv = detail.get('gmv') or product.gmv
                        changed = True

                    new_inf = detail.get('influencer_count') or 0
                    if new_inf > 0:
                        product.influencer_count = new_inf

                    new_price = detail.get('price') or 0
                    if new_price > 0:
                        product.price = new_price

                    if changed:
                        total_updated += 1

                    time.sleep(0.4)

                except Exception as e:
                    log.warning("Deep refresh error for %s: %s", product.product_id, e)
                    total_errors += 1
                    product.last_updated = datetime.utcnow()

            db.session.commit()

            if not continuous or iteration >= max_iterations:
                break

        return jsonify({
            'success': True,
            'total_processed': total_processed,
            'total_updated': total_updated,
            'total_errors': total_errors,
            'iterations': iteration,
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# OUT-OF-STOCK DETECTION
# =============================================================================

@scan_bp.route('/api/detect-oos', methods=['GET', 'POST'])
@login_required
@admin_required
def detect_out_of_stock():
    """Mark products with 0 recent sales but high historical sales as likely OOS."""
    threshold = int(request.args.get('threshold', 50))

    try:
        candidates = Product.query.filter(
            Product.sales_7d == 0,
            Product.sales_30d > threshold,
            db.or_(Product.product_status.is_(None), Product.product_status == 'active'),
        ).all()

        for product in candidates:
            product.product_status = 'likely_oos'
            product.status_note = f'Auto-detected: 0 sales in 7d but {product.sales_30d} in 30d'

        db.session.commit()

        return jsonify({
            'success': True,
            'marked_as_oos': len(candidates),
            'threshold': threshold,
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# BRAND / SELLER PAGE SCAN
# =============================================================================

@scan_bp.route('/api/scan-pages/<seller_id>', methods=['GET'])
@login_required
def api_scan_brand_pages(seller_id):
    """Scan all products from a specific seller via EchoTik."""
    from app.services.echotik import _normalize_product, sync_to_db

    try:
        start_page = int(request.args.get('start', 1))
        end_page = int(request.args.get('end', 5))

        all_products = []
        for page_num in range(start_page, end_page + 1):
            res = requests.get(
                f"{BASE_URL}/product/list",
                params={
                    'seller_id': seller_id,
                    'sort_by': 'total_sale_7d_cnt',
                    'sort_order': 'desc',
                    'page_num': page_num,
                    'page_size': 10,  # EchoTik max is 10
                },
                auth=get_auth(),
                timeout=30,
            )
            data = res.json()
            if data.get('code') == 0:
                items = data.get('data', [])
                for item in items:
                    item['seller_id'] = seller_id
                    all_products.append(_normalize_product(item))

        if all_products:
            result = sync_to_db(all_products)
        else:
            result = {'created': 0, 'updated': 0, 'errors': 0}

        return jsonify({
            'success': True,
            'products_found': len(all_products),
            **result,
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
