"""
Vantage -- View Routes Blueprint
Serves Jinja2 templates for all frontend pages.
Existing API routes remain unchanged in their respective blueprints.
"""

from functools import wraps
from datetime import datetime
from flask import Blueprint, render_template, redirect, session, request, jsonify
from sqlalchemy import desc, or_
from app import db
from app.models import Product, BlacklistedBrand, Subscription, User, Brand, ProductVideo, TapProduct
from app.routes.auth import get_current_user


def login_required(f):
    """Page-level login check: redirects to /login instead of returning JSON."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def api_auth(f):
    """API-level login check: returns JSON 401."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated

views_bp = Blueprint('views', __name__)

# Products with status='active' or NULL (new products may not have status set)
_active_filter = or_(Product.product_status == 'active', Product.product_status.is_(None))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_subscription(user):
    """Get active subscription for a user, or None."""
    if not user:
        return None
    return Subscription.query.filter_by(user_id=user.id).order_by(desc(Subscription.created_at)).first()


def _base_context(active_page='dashboard'):
    """Build the common context dict shared by all app templates."""
    user = get_current_user()
    sub = _get_subscription(user)
    return {
        'current_user': user,
        'subscription': sub,
        'active_page': active_page,
    }

# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@views_bp.route('/')
def landing():
    if 'user_id' in session:
        return redirect('/app/dashboard')
    return render_template('landing.html')


@views_bp.route('/login')
@views_bp.route('/register')
def auth_page():
    if 'user_id' in session:
        return redirect('/app/dashboard')
    return render_template('auth.html')


@views_bp.route('/health')
def health():
    return 'ok', 200

# ---------------------------------------------------------------------------
# App pages (require login)
# ---------------------------------------------------------------------------

@views_bp.route('/app/dashboard')
@login_required
def dashboard():
    ctx = _base_context('dashboard')
    user = ctx['current_user']

    # Stats
    total = Product.query.filter(_active_filter).count()
    trending = Product.query.filter(
        _active_filter,
        Product.sales_7d > 50
    ).count()
    from sqlalchemy import func
    avg_comm = db.session.query(func.avg(Product.commission_rate)).filter(
        _active_filter,
        Product.commission_rate > 0
    ).scalar() or 0
    saved = Product.query.filter_by(is_favorite=True).count()

    ctx['stats'] = {
        'tracked_count': total,
        'trending_today': trending,
        'avg_commission': avg_comm * 100,
        'saved_count': saved,
    }

    # Trending products (top 6 by 7d sales, min 5 videos)
    ctx['trending_products'] = Product.query.filter(
        _active_filter, Product.video_count >= 5
    ).order_by(desc(Product.sales_7d)).limit(6).all()

    for p in ctx['trending_products']:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    # Recent products (last updated, min 5 videos)
    ctx['recent_products'] = Product.query.filter(
        _active_filter, Product.video_count >= 5
    ).order_by(desc(Product.last_updated)).limit(5).all()
    for p in ctx['recent_products']:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    return render_template('dashboard.html', **ctx)


@views_bp.route('/app/products')
@login_required
def products_list():
    ctx = _base_context('products')
    try:
        return _products_list_inner(ctx)
    except Exception as e:
        import logging, traceback
        logging.getLogger(__name__).error("Products page error: %s\n%s", e, traceback.format_exc())
        ctx['products'] = []
        ctx['page'] = 1
        ctx['total_pages'] = 0
        ctx['total_count'] = 0
        ctx['has_filters'] = False
        ctx['categories'] = []
        ctx['current_sort'] = 'trending'
        ctx['current_category'] = ''
        ctx['current_search'] = ''
        ctx['current_min_commission'] = 0
        ctx['current_max_price'] = 0
        ctx['current_max_videos'] = 0
        from flask import flash
        flash(f'Error loading products: {str(e)[:200]}', 'error')
        return render_template('products.html', **ctx)

def _products_list_inner(ctx):
    page = request.args.get('page', 1, type=int)
    per_page = 30
    sort = request.args.get('sort', 'trending')
    search = request.args.get('search', '').strip()
    category = request.args.get('category', '').strip()
    min_comm = request.args.get('min_commission', 0, type=float)
    max_price = request.args.get('max_price', 0, type=float)
    max_videos = request.args.get('max_videos', 0, type=int)

    query = Product.query.filter(
        _active_filter,
        Product.video_count >= 5  # Filter out placeholder products with <5 videos
    )

    if search:
        query = query.filter(Product.product_name.ilike(f'%{search}%'))

    if category:
        query = query.filter(Product.category == category)

    if min_comm > 0:
        query = query.filter(Product.commission_rate >= min_comm / 100.0)

    if max_price > 0:
        query = query.filter(Product.price <= max_price)

    if max_videos > 0:
        query = query.filter(Product.video_count <= max_videos)

    if sort == 'commission':
        query = query.order_by(desc(Product.commission_rate))
    elif sort == 'new':
        query = query.order_by(desc(Product.first_seen))
    elif sort == 'gmv':
        query = query.order_by(desc(Product.gmv))
    elif sort == 'sales':
        query = query.order_by(desc(Product.sales_7d))
    elif sort == 'videos_low':
        query = query.order_by(Product.video_count.asc())
    else:  # trending (default)
        query = query.order_by(desc(Product.sales_7d))

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    products = pagination.items

    # Get distinct categories for filter dropdown
    from sqlalchemy import func as sqlfunc
    categories = db.session.query(Product.category).filter(
        _active_filter,
        Product.category.isnot(None),
        Product.category != '',
    ).distinct().order_by(Product.category).all()
    ctx['categories'] = [c[0] for c in categories if c[0]]

    # Pass current filter values back for form state
    ctx['current_sort'] = sort
    ctx['current_category'] = category
    ctx['current_search'] = search
    ctx['current_min_commission'] = min_comm
    ctx['current_max_price'] = max_price
    ctx['current_max_videos'] = max_videos

    for p in products:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    ctx['products'] = products
    ctx['page'] = page
    ctx['total_pages'] = pagination.pages
    ctx['total_count'] = pagination.total
    ctx['has_filters'] = bool(category or search or min_comm > 0 or max_price > 0 or max_videos > 0)

    return render_template('products.html', **ctx)


@views_bp.route('/app/products/<product_id>')
@login_required
def product_detail(product_id):
    import json
    from datetime import timedelta
    ctx = _base_context('products')

    product = Product.query.get_or_404(product_id)
    product.trending_score = min(99, int((product.sales_7d or 0) / 10 + (product.influencer_count or 0) * 3 + (product.commission_rate or 0) * 200))
    ctx['product'] = product

    # Build stats dict from existing model fields — per time period
    ctx['stats'] = {
        # 7-day
        'sales_7d': product.sales_7d or 0,
        'revenue_7d': product.gmv or 0,
        'videos_7d': product.video_7d or 0,
        'growth_7d': product.gmv_growth or 0,
        # 30-day
        'sales_30d': product.sales_30d or 0,
        'revenue_30d': product.gmv_30d or 0,
        'videos_30d': product.video_30d or 0,
        # All-time
        'total_sales': product.sales or 0,
        'total_revenue': product.gmv or 0,
        'total_videos': product.video_count_alltime or product.video_count or 0,
        'total_creators': product.influencer_count or 0,
        # Always visible
        'trending_score': product.trending_score,
        'commission_rate': round((product.commission_rate or 0) * 100, 1),
        'avg_order_value': product.price or 0,
    }

    # Lazy-load trend data (cache 24h) — defensive if columns don't exist yet
    trend_data = []
    try:
        trend_synced = getattr(product, 'trend_last_synced', None)
        if not trend_synced or trend_synced < datetime.utcnow() - timedelta(hours=24):
            from app.services.echotik import fetch_product_trend
            raw_id = product_id.replace('shop_', '')
            trend = fetch_product_trend(raw_id)
            if trend:
                product.trend_data_json = json.dumps(trend)
                product.trend_last_synced = datetime.utcnow()
                db.session.commit()
        raw_json = getattr(product, 'trend_data_json', None)
        if raw_json:
            trend_data = json.loads(raw_json)
    except Exception:
        pass
    ctx['trend_data'] = trend_data

    # Videos (<=15s, from DB) — defensive if table doesn't exist yet
    try:
        ctx['videos'] = ProductVideo.query.filter_by(
            product_id=product_id
        ).order_by(desc(ProductVideo.view_count)).limit(5).all()
    except Exception:
        ctx['videos'] = []

    # Similar products (same category)
    similar = []
    if product.category:
        similar = Product.query.filter(
            Product.category == product.category,
            Product.product_id != product_id,
            _active_filter
        ).order_by(desc(Product.sales_7d)).limit(4).all()
    ctx['similar_products'] = similar

    return render_template('product_detail.html', **ctx)


@views_bp.route('/app/analytics')
@login_required
def analytics():
    ctx = _base_context('analytics')

    ctx['top_products'] = Product.query.filter(
        _active_filter
    ).order_by(desc(Product.gmv)).limit(10).all()

    for p in ctx['top_products']:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    return render_template('analytics.html', **ctx)


@views_bp.route('/app/blacklist')
@login_required
def blacklist():
    ctx = _base_context('blacklist')
    ctx['blacklist_items'] = BlacklistedBrand.query.order_by(desc(BlacklistedBrand.added_at)).all()
    return render_template('blacklist.html', **ctx)


@views_bp.route('/app/settings')
@login_required
def settings():
    ctx = _base_context('settings')
    return render_template('settings.html', **ctx)


@views_bp.route('/app/subscribe')
@login_required
def subscribe():
    ctx = _base_context('subscribe')
    return render_template('subscribe.html', **ctx)


@views_bp.route('/app/brands')
@login_required
def brands_list():
    ctx = _base_context('brands')
    page = request.args.get('page', 1, type=int)
    sort = request.args.get('sort', 'gmv')
    category = request.args.get('category', '').strip()
    search = request.args.get('q', '').strip()

    try:
        query = Brand.query
        if category:
            query = query.filter(Brand.category == category)
        if search:
            query = query.filter(Brand.name.ilike(f'%{search}%'))
        if sort == 'followers':
            query = query.order_by(desc(Brand.follower_count))
        elif sort == 'trending':
            query = query.order_by(desc(Brand.trending_score))
        elif sort == 'products':
            query = query.order_by(desc(Brand.product_count))
        else:
            query = query.order_by(desc(Brand.gmv_30d))

        pagination = query.paginate(page=page, per_page=30, error_out=False)

        categories = db.session.query(Brand.category).filter(
            Brand.category.isnot(None), Brand.category != ''
        ).distinct().order_by(Brand.category).all()

        ctx['brands'] = pagination.items
        ctx['page'] = page
        ctx['total_pages'] = pagination.pages
        ctx['total_count'] = pagination.total
        ctx['categories'] = [c[0] for c in categories if c[0]]
    except Exception:
        # Table may not exist yet — show empty state
        ctx['brands'] = []
        ctx['page'] = 1
        ctx['total_pages'] = 0
        ctx['total_count'] = 0
        ctx['categories'] = []

    ctx['current_sort'] = sort
    ctx['current_category'] = category
    ctx['current_search'] = search

    return render_template('brands.html', **ctx)


@views_bp.route('/app/brands/<int:brand_id>')
@login_required
def brand_detail(brand_id):
    ctx = _base_context('brands')
    brand = Brand.query.get_or_404(brand_id)
    brand_sort = request.args.get('sort', 'sales')
    ctx['brand'] = brand

    # Try fetching live products from EchoTik for this seller
    live_products = []
    try:
        from app.services.echotik import fetch_brand_products, fetch_batch_images, _extract_image_url
        if brand.shop_id:
            live_products = fetch_brand_products(brand.shop_id, page=1, page_size=10)
            # Sign images for live products
            if live_products:
                urls_to_sign = [p.get('image_url', '') for p in live_products if p.get('image_url', '').startswith('http')]
                if urls_to_sign:
                    try:
                        signed = fetch_batch_images(urls_to_sign[:10])
                        for p in live_products:
                            if p.get('image_url') in signed:
                                p['image_url'] = signed[p['image_url']]
                    except Exception:
                        pass
                # Update brand product count
                if not brand.product_count or brand.product_count == 0:
                    brand.product_count = len(live_products)
                    db.session.commit()
    except Exception:
        pass

    # Fallback: products in our DB matching this seller ID or name
    db_products = []
    if not live_products:
        # Build query — try seller_id first, then name
        db_q = None
        if brand.shop_id:
            db_q = Product.query.filter(Product.seller_id == brand.shop_id, _active_filter)
        if db_q is None or db_q.count() == 0:
            if brand.name:
                db_q = Product.query.filter(Product.seller_name.ilike(f'%{brand.name}%'), _active_filter)

        if db_q:
            if brand_sort == 'commission':
                db_q = db_q.order_by(desc(Product.commission_rate))
            elif brand_sort == 'price':
                db_q = db_q.order_by(desc(Product.price))
            elif brand_sort == 'videos':
                db_q = db_q.order_by(desc(Product.video_count))
            elif brand_sort == 'new':
                db_q = db_q.order_by(desc(Product.first_seen))
            else:
                db_q = db_q.order_by(desc(Product.sales_7d))
            db_products = db_q.limit(30).all()

        for p in db_products:
            p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))
        if db_products and (not brand.product_count or brand.product_count == 0):
            brand.product_count = len(db_products)
            db.session.commit()

    ctx['live_products'] = live_products
    ctx['db_products'] = db_products
    ctx['source'] = 'live' if live_products else 'db'
    ctx['brand_sort'] = brand_sort

    return render_template('brand_detail.html', **ctx)


@views_bp.route('/app/admin')
@login_required
def admin_panel():
    ctx = _base_context('admin')
    user = ctx['current_user']
    if not user or not user.is_admin:
        return redirect('/app/dashboard')

    try:
        from sqlalchemy import func
        ctx['total_products'] = Product.query.count()
        ctx['active_products'] = Product.query.filter(_active_filter).count()
        ctx['blacklisted_count'] = BlacklistedBrand.query.count()
        ctx['user_count'] = User.query.count()

        last = db.session.query(func.max(Product.last_echotik_sync)).scalar()
        ctx['last_sync'] = last.strftime('%b %d, %Y at %I:%M %p UTC') if last else None

        ctx['missing_images'] = Product.query.filter(
            _active_filter,
            or_(Product.image_url.is_(None), Product.image_url == '',
                Product.cached_image_url.is_(None))
        ).count()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Admin stats query failed: %s", e)
        ctx.setdefault('total_products', 0)
        ctx.setdefault('active_products', 0)
        ctx.setdefault('blacklisted_count', 0)
        ctx.setdefault('user_count', 0)
        ctx.setdefault('last_sync', None)
        ctx.setdefault('missing_images', 0)

    try:
        ctx['brand_count'] = Brand.query.count()
    except Exception:
        ctx['brand_count'] = 0

    return render_template('admin.html', **ctx)


# ---------------------------------------------------------------------------
# API endpoints for frontend (blacklist CRUD, profile update, image sync)
# ---------------------------------------------------------------------------

@views_bp.route('/api/admin/sync-images', methods=['POST'])
@api_auth
def api_sync_images():
    """Sign images for products missing cached_image_url."""
    user = get_current_user()
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin required'}), 403

    from app.services.echotik import fetch_batch_images
    import logging
    log = logging.getLogger(__name__)

    products = Product.query.filter(
        _active_filter,
        Product.image_url.isnot(None),
        Product.image_url != '',
        or_(Product.cached_image_url.is_(None), Product.cached_image_url == '')
    ).limit(100).all()

    if not products:
        return jsonify({'success': True, 'signed': 0, 'missing': 0, 'total': Product.query.count()})

    signed = 0
    for i in range(0, len(products), 10):
        batch = products[i:i + 10]
        urls = [p.image_url for p in batch if p.image_url and p.image_url.startswith('http')]
        if not urls:
            continue
        try:
            result = fetch_batch_images(urls)
            for p in batch:
                if p.image_url in result:
                    p.cached_image_url = result[p.image_url][:500]
                    signed += 1
        except Exception as e:
            log.warning("Batch sign failed: %s", e)

    db.session.commit()

    still_missing = Product.query.filter(
        _active_filter,
        or_(Product.image_url.is_(None), Product.image_url == '',
            Product.cached_image_url.is_(None))
    ).count()

    return jsonify({'success': True, 'signed': signed, 'missing': still_missing, 'total': Product.query.count()})


@views_bp.route('/api/admin/sync-brands', methods=['POST'])
@api_auth
def api_sync_brands():
    """Fetch top shops from EchoTik and upsert to Brand table."""
    user = get_current_user()
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin required'}), 403

    import logging
    log = logging.getLogger(__name__)

    try:
        from app.services.echotik import fetch_top_shops, fetch_batch_images
    except ImportError:
        return jsonify({'error': 'echotik module not available'}), 500

    try:
        import time as _time
        total_synced = 0
        images_with = 0
        all_shops = []
        for pg in range(1, 6):  # 5 pages x 10 = 50 sellers
            page_shops = fetch_top_shops(country="US", page_size=10, page=pg)
            if not page_shops:
                break
            all_shops.extend(page_shops)
            _time.sleep(0.3)

        # Log first seller for debugging
        if all_shops:
            log.info("[BrandSync] First seller raw: %s", {k: str(v)[:80] for k, v in all_shops[0].items()})

        if all_shops:
            for s in all_shops:
                sid = s.get('shop_id', '')
                if not sid:
                    continue
                brand = Brand.query.filter_by(shop_id=sid).first()
                if not brand:
                    brand = Brand(shop_id=sid, name=s.get('name', 'Unknown'))
                    db.session.add(brand)
                brand.name = (s.get('name') or brand.name)[:300]
                brand.avatar_url = (s.get('avatar_url') or '')[:500] or brand.avatar_url
                brand.country = s.get('country', 'US')
                brand.category = (s.get('category') or '')[:100] or brand.category
                brand.follower_count = s.get('follower_count', 0)
                brand.gmv_30d = s.get('gmv_30d', 0)
                brand.product_count = s.get('product_count', 0)
                brand.trending_score = s.get('trending_score', 0)
                brand.tiktok_shop_url = (s.get('shop_url') or '')[:500]
                brand.last_synced = datetime.utcnow()
                total_synced += 1
                if brand.avatar_url:
                    images_with += 1

        db.session.commit()

        # Sign brand images that are EchoTik CDN URLs
        try:
            brands_needing_sign = Brand.query.filter(
                Brand.avatar_url.isnot(None),
                Brand.avatar_url != '',
                Brand.avatar_url.like('%echosell-images%')
            ).limit(50).all()
            if brands_needing_sign:
                urls = [b.avatar_url for b in brands_needing_sign if b.avatar_url]
                for i in range(0, len(urls), 10):
                    batch = urls[i:i+10]
                    signed = fetch_batch_images(batch)
                    for b in brands_needing_sign:
                        if b.avatar_url in signed:
                            b.avatar_url = signed[b.avatar_url][:500]
                db.session.commit()
                log.info("[BrandSync] Signed %d brand images", len(brands_needing_sign))
        except Exception as e:
            log.warning("[BrandSync] Image signing failed: %s", e)

        total = 0
        try:
            total = Brand.query.count()
        except Exception:
            pass
        return jsonify({'success': True, 'synced': total_synced, 'total': total,
                        'with_images': images_with})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@views_bp.route('/api/admin/echotik-debug', methods=['POST'])
@api_auth
def api_echotik_debug():
    """Brute-force test every EchoTik base URL + path + auth combo for shop data."""
    user = get_current_user()
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin required'}), 403

    import os, requests as req
    from requests.auth import HTTPBasicAuth

    username = os.environ.get('ECHOTIK_USERNAME', '')
    password = os.environ.get('ECHOTIK_PASSWORD', '')
    api_key = os.environ.get('ECHOTIK_API_KEY', '')

    BASE_URLS = [
        "https://open.echotik.live/api/v3/echotik",
        "https://open.echotik.live/api/v3",
        "https://open.echotik.live/api/v1",
        "https://echoes.echotik.live/api/v3",
        "https://api.echotik.live/v3",
        "https://api.echotik.live/api/v3",
    ]

    PATHS = [
        "/seller/list",
        "/shop/list",
        "/shop/rank/list",
        "/shop/rank",
        "/shop/top/list",
        "/seller/rank/list",
    ]

    PARAMS = {"country": "US", "region": "US", "page_num": 1, "page_size": 5, "date_type": 7}

    # Build auth variants
    auth_methods = []
    if username and password:
        auth_methods.append(("basic_user_pass", {"auth": HTTPBasicAuth(username, password)}))
    if api_key:
        import base64
        cred = base64.b64encode(f"{api_key}:".encode()).decode()
        auth_methods.append(("basic_apikey_empty", {"headers": {"Authorization": f"Basic {cred}"}}))
        auth_methods.append(("bearer_apikey", {"headers": {"Authorization": f"Bearer {api_key}"}}))
        auth_methods.append(("header_token", {"headers": {"token": api_key}}))
        auth_methods.append(("header_api-key", {"headers": {"api-key": api_key}}))
        auth_methods.append(("raw_auth_header", {"headers": {"Authorization": api_key}}))
    if not auth_methods:
        auth_methods.append(("no_auth", {}))

    results = []

    # First: test auth methods on one known-working endpoint (product/list)
    test_url = "https://open.echotik.live/api/v3/echotik/product/list"
    test_params = {"region": "US", "page_num": 1, "page_size": 1}
    results.append({"section": "=== AUTH METHOD TEST (product/list) ==="})
    for label, kwargs in auth_methods:
        try:
            r = req.get(test_url, params=test_params, timeout=10,
                        auth=kwargs.get('auth'), headers=kwargs.get('headers'))
            body = r.text[:200]
            has_data = '"data"' in body or '"list"' in body
            results.append({
                "auth": label, "url": "product/list", "status": r.status_code,
                "has_data": has_data, "body": body
            })
        except Exception as e:
            results.append({"auth": label, "url": "product/list", "status": "error", "body": str(e)})

    # Second: test all base+path combos with the first working auth
    working_auth = auth_methods[0]  # default to first
    for label, kwargs in auth_methods:
        for r in results:
            if r.get('auth') == label and r.get('status') == 200 and r.get('has_data'):
                working_auth = (label, kwargs)
                break

    results.append({"section": f"=== SHOP ENDPOINT SCAN (using {working_auth[0]}) ==="})
    for base in BASE_URLS:
        for path in PATHS:
            url = f"{base}{path}"
            try:
                r = req.get(url, params=PARAMS, timeout=8,
                            auth=working_auth[1].get('auth'),
                            headers=working_auth[1].get('headers'))
                body = r.text[:300]
                has_data = '"data"' in body and ('"list"' in body or '"records"' in body)
                entry = {
                    "url": url, "status": r.status_code,
                    "has_data": has_data, "body": body
                }
                if has_data and r.status_code == 200:
                    entry["MATCH"] = True
                results.append(entry)
            except Exception as e:
                results.append({"url": url, "status": "error", "body": str(e)[:200]})

    return jsonify({'results': results})


# ---------------------------------------------------------------------------
# TAP (Boosted Commission) routes
# ---------------------------------------------------------------------------

@views_bp.route('/app/admin/tap')
@login_required
def admin_tap():
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    ctx = _base_context('admin')
    try:
        ctx['taps'] = TapProduct.query.order_by(desc(TapProduct.created_at)).all()
        ctx['products'] = Product.query.filter(_active_filter).order_by(Product.product_name).limit(500).all()
    except Exception:
        ctx['taps'] = []
        ctx['products'] = []
    return render_template('admin_tap.html', **ctx)


@views_bp.route('/app/admin/tap/add', methods=['POST'])
@login_required
def admin_tap_add():
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    data = request.form
    product_id = data.get('product_id')
    if not product_id:
        return redirect('/app/admin/tap')
    # Remove existing TAP for this product
    TapProduct.query.filter_by(product_id=product_id).delete()
    tap = TapProduct(
        product_id=product_id,
        tap_link=data.get('tap_link', ''),
        boosted_commission=float(data.get('boosted_commission', 0)) / 100,
        base_commission=float(data.get('base_commission', 0)) / 100,
        partner_name=data.get('partner_name', 'Affiliate Automated'),
        expires_at=datetime.strptime(data['expires_at'], '%Y-%m-%d') if data.get('expires_at') else None,
    )
    db.session.add(tap)
    db.session.commit()
    return redirect('/app/admin/tap')


@views_bp.route('/app/admin/tap/delete/<int:tap_id>', methods=['POST'])
@login_required
def admin_tap_delete(tap_id):
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    tap = TapProduct.query.get(tap_id)
    if tap:
        db.session.delete(tap)
        db.session.commit()
    return redirect('/app/admin/tap')


@views_bp.route('/api/tap/click/<path:product_id>')
def tap_click_track(product_id):
    """Track click and redirect to TAP link."""
    tap = TapProduct.query.filter_by(product_id=product_id, is_active=True).first()
    if not tap:
        return redirect('/app/products')
    tap.clicks = (tap.clicks or 0) + 1
    db.session.commit()
    return redirect(tap.tap_link)


@views_bp.route('/api/blacklist/add', methods=['POST'])
@api_auth
def api_blacklist_add():
    data = request.get_json() or {}
    product_id = data.get('product_id')
    if not product_id:
        return jsonify({'error': 'product_id required'}), 400
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    # Use seller_name for blacklisting (brand-level)
    existing = BlacklistedBrand.query.filter_by(seller_name=product.seller_name or product.product_name).first()
    if existing:
        return jsonify({'success': True, 'message': 'Already blacklisted'})
    bl = BlacklistedBrand(
        seller_name=product.seller_name or product.product_name,
        seller_id=product.seller_id,
        reason='Blacklisted from product card'
    )
    db.session.add(bl)
    db.session.commit()
    return jsonify({'success': True})


@views_bp.route('/api/blacklist/<int:item_id>', methods=['DELETE'])
@api_auth
def api_blacklist_delete(item_id):
    item = BlacklistedBrand.query.get(item_id)
    if not item:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(item)
    db.session.commit()
    return jsonify({'success': True})


@views_bp.route('/api/blacklist/<int:item_id>', methods=['PATCH'])
@api_auth
def api_blacklist_update(item_id):
    item = BlacklistedBrand.query.get(item_id)
    if not item:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    if 'reason' in data:
        item.reason = data['reason']
    db.session.commit()
    return jsonify({'success': True})


@views_bp.route('/api/me/update', methods=['POST'])
@api_auth
def api_profile_update():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json() or {}
    if 'display_name' in data and data['display_name'].strip():
        user.discord_username = data['display_name'].strip()[:100]
    db.session.commit()
    return jsonify({'success': True})
