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
from app.models import Product, BlacklistedBrand, Subscription, User, Brand, ProductVideo
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

    # Trending products (top 6 by 7d sales)
    ctx['trending_products'] = Product.query.filter(
        _active_filter
    ).order_by(desc(Product.sales_7d)).limit(6).all()

    # Add trending_score attribute for template
    for p in ctx['trending_products']:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    # Recent products (last updated)
    ctx['recent_products'] = Product.query.filter(
        _active_filter
    ).order_by(desc(Product.last_updated)).limit(5).all()
    for p in ctx['recent_products']:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    return render_template('dashboard.html', **ctx)


@views_bp.route('/app/products')
@login_required
def products_list():
    ctx = _base_context('products')

    page = request.args.get('page', 1, type=int)
    per_page = 30
    sort = request.args.get('sort', 'trending')
    search = request.args.get('search', '').strip()
    category = request.args.get('category', '').strip()
    min_comm = request.args.get('min_commission', 0, type=float)

    query = Product.query.filter(_active_filter)

    if search:
        query = query.filter(Product.product_name.ilike(f'%{search}%'))

    if category:
        query = query.filter(Product.category == category)

    if min_comm > 0:
        query = query.filter(Product.commission_rate >= min_comm / 100.0)

    if sort == 'commission':
        query = query.order_by(desc(Product.commission_rate))
    elif sort == 'new':
        query = query.order_by(desc(Product.first_seen))
    elif sort == 'gmv':
        query = query.order_by(desc(Product.gmv))
    elif sort == 'sales':
        query = query.order_by(desc(Product.sales_7d))
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

    for p in products:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    ctx['products'] = products
    ctx['page'] = page
    ctx['total_pages'] = pagination.pages
    ctx['total_count'] = pagination.total
    ctx['has_filters'] = bool(category or search or min_comm > 0)

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

    # Build stats dict from existing model fields
    ctx['stats'] = {
        'sales_7d': product.sales_7d or 0,
        'revenue_7d': product.gmv or 0,
        'sales_30d': product.sales_30d or 0,
        'revenue_30d': product.gmv_30d or 0,
        'total_sales': product.sales or 0,
        'total_revenue': product.gmv or 0,
        'trending_score': product.trending_score,
        'commission_rate': round((product.commission_rate or 0) * 100, 1),
        'creator_count': product.influencer_count or 0,
        'video_count': product.video_count or 0,
        'avg_order_value': product.price or 0,
        'growth_7d': product.gmv_growth or 0,
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
        ).filter(
            ProductVideo.duration_seconds <= 15
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


@views_bp.route('/app/admin')
@login_required
def admin_panel():
    ctx = _base_context('admin')
    user = ctx['current_user']
    if not user or not user.is_admin:
        return redirect('/app/dashboard')

    from sqlalchemy import func
    ctx['total_products'] = Product.query.count()
    ctx['active_products'] = Product.query.filter(_active_filter).count()
    ctx['blacklisted_count'] = BlacklistedBrand.query.count()
    ctx['user_count'] = User.query.count()
    try:
        ctx['brand_count'] = Brand.query.count()
    except Exception:
        ctx['brand_count'] = 0

    # Last sync: most recent last_echotik_sync across all products
    last = db.session.query(func.max(Product.last_echotik_sync)).scalar()
    if last:
        ctx['last_sync'] = last.strftime('%b %d, %Y at %I:%M %p UTC')
    else:
        ctx['last_sync'] = None

    # Count products missing images
    ctx['missing_images'] = Product.query.filter(
        _active_filter,
        or_(Product.image_url.is_(None), Product.image_url == '',
            Product.cached_image_url.is_(None))
    ).count()

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
        from app.services.echotik import fetch_top_shops
    except ImportError:
        return jsonify({'error': 'echotik module not available'}), 500

    total_synced = 0
    for page in range(1, 6):  # 5 pages x 20 = 100 brands max
        shops = fetch_top_shops(page=page, page_size=20)
        if not shops:
            break
        for s in shops:
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

    db.session.commit()
    return jsonify({'success': True, 'synced': total_synced, 'total': Brand.query.count()})


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
