"""
Vantage -- View Routes Blueprint
Serves Jinja2 templates for all frontend pages.
Existing API routes remain unchanged in their respective blueprints.
"""

from functools import wraps
from datetime import datetime
from datetime import timedelta
from flask import Blueprint, render_template, redirect, session, request, jsonify, flash
from sqlalchemy import desc, or_
from app import db
from app.models import Product, BlacklistedBrand, Subscription, User, Brand, ProductVideo, TapProduct, TapList, ProductView, CampaignBanner, CouponCode, CouponRedemption, ScannedBrand, BrandProduct, BrandScanJob
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


def _calc_score(p):
    """
    Opportunity Score (0-99) — composite metric for affiliate product potential.

    Components:
      - Demand (0-25): proven sales velocity, log-scaled
      - Momentum (0-20): is it growing? sales_7d vs sales_30d ratio
      - Commission (0-20): higher commission = more money per sale
      - Saturation Gap (0-20): few creators relative to sales = untapped
      - Price Sweet Spot (0-14): $10-60 range converts best on TikTok

    The score answers: "How good is this product for an affiliate to promote RIGHT NOW?"
    """
    import math

    sales_7d = p.sales_7d or 0
    sales_30d = p.sales_30d or 0
    creators = p.influencer_count or 0
    comm = (p.commission_rate or 0) * 100
    videos = p.video_count or 0
    price = p.price or 0
    growth = p.gmv_growth or 0

    # --- DEMAND (0-25): proven sales, log-scaled ---
    # 100 sales=11, 1000=17, 5000=21, 20000=25
    demand = min(25, math.log10(max(sales_7d, 1)) * 5.8)

    # --- MOMENTUM (0-20): is it accelerating? ---
    if sales_30d > 0:
        # Weekly rate: sales_7d / (sales_30d / 4.3)
        weekly_avg = sales_30d / 4.3
        if weekly_avg > 0:
            accel = sales_7d / weekly_avg  # >1 = accelerating, <1 = slowing
            momentum = min(20, max(0, (accel - 0.5) * 16))  # 0.5x=0, 1x=8, 1.5x=16, 1.75x=20
        else:
            momentum = 10
    else:
        momentum = 10 if sales_7d > 0 else 0

    # Bonus for explicit growth percentage
    if growth > 50:
        momentum = min(20, momentum + 5)
    elif growth > 20:
        momentum = min(20, momentum + 2)

    # --- COMMISSION (0-20): more money per sale ---
    # 5%=4, 10%=8, 15%=12, 20%=16, 25%=20
    commission_score = min(20, comm * 0.8)

    # --- SATURATION GAP (0-20): opportunity vs competition ---
    # High sales + few creators = massive untapped opportunity
    # Low sales + many creators = oversaturated, avoid
    if sales_7d > 0 and creators > 0:
        sales_per_creator = sales_7d / creators
        # 1 sale/creator = bad (2pts), 10 = okay (10pts), 100 = gold (18pts)
        gap = min(20, math.log10(max(sales_per_creator, 0.1)) * 9)
    elif sales_7d > 100 and creators == 0:
        gap = 20  # No creators but selling = maximum opportunity
    else:
        gap = 5

    # Video saturation penalty within gap score
    if videos > 0 and sales_7d > 0:
        sales_per_video = sales_7d / videos
        if sales_per_video < 1:  # More videos than sales = oversaturated
            gap = max(0, gap - 5)

    # --- PRICE SWEET SPOT (0-14): TikTok impulse buy range ---
    if 10 <= price <= 35:
        price_score = 14  # Sweet spot
    elif 35 < price <= 60:
        price_score = 10  # Still good
    elif 5 <= price < 10:
        price_score = 8   # Cheap but low commission $
    elif 60 < price <= 100:
        price_score = 6   # Higher consideration
    elif price > 100:
        price_score = 3   # Hard sell on TikTok
    else:
        price_score = 2   # Too cheap or unknown

    # --- COMBINE ---
    raw = demand + momentum + commission_score + gap + price_score
    return max(1, min(99, int(raw)))


def _calc_lifecycle(p):
    """
    Determine product lifecycle stage based on sales trajectory.
    Returns: 'rising', 'peak', 'declining', or 'new'
    """
    sales_7d = p.sales_7d or 0
    sales_30d = p.sales_30d or 0
    growth = p.gmv_growth or 0

    if sales_7d == 0:
        return 'new'

    if sales_30d > 0:
        weekly_avg = sales_30d / 4.3
        if weekly_avg > 0:
            ratio = sales_7d / weekly_avg
            if ratio > 1.3 or growth > 30:
                return 'rising'
            elif ratio > 0.85:
                return 'peak'
            else:
                return 'declining'

    if growth > 20:
        return 'rising'
    elif growth < -20:
        return 'declining'

    return 'peak'


def _is_seasonal_hot(p):
    """Check if a product matches current seasonal trends using keywords."""
    from datetime import datetime
    month = datetime.utcnow().month
    name = (p.product_name or '').lower()
    cat = (p.category or '').lower()

    # Seasonal keyword sets
    summer = ['sunscreen', 'spf', 'sun protection', 'swimsuit', 'bikini', 'swimwear',
              'pool float', 'beach', 'outdoor', 'patio', 'grill', 'bbq', 'cooler',
              'fan', 'cooling', 'ice', 'summer', 'tank top', 'shorts', 'sandal',
              'sunglasses', 'uv', 'water bottle', 'hydration', 'mosquito', 'bug spray',
              'aloe vera', 'after sun', 'self tanner', 'tanning']
    winter = ['jacket', 'coat', 'sweater', 'hoodie', 'beanie', 'glove', 'scarf',
              'thermal', 'heated', 'blanket', 'hot chocolate', 'moisturizer',
              'lip balm', 'humidifier', 'heater', 'winter', 'warm', 'fleece']
    spring = ['allergy', 'garden', 'planting', 'seed', 'cleaning', 'organize',
              'spring', 'floral', 'rain', 'umbrella', 'lightweight']
    fall = ['pumpkin', 'halloween', 'costume', 'fall', 'autumn', 'flannel',
            'boots', 'candle', 'cinnamon', 'apple cider', 'thanksgiving']
    back_to_school = ['backpack', 'school', 'notebook', 'pencil', 'laptop',
                      'desk', 'study', 'college', 'dorm']

    # Determine current season keywords
    if month in (6, 7, 8):  # Summer
        hot_keywords = summer
    elif month in (12, 1, 2):  # Winter
        hot_keywords = winter
    elif month in (3, 4, 5):  # Spring
        hot_keywords = spring
    elif month in (9, 10, 11):  # Fall
        hot_keywords = fall + (back_to_school if month == 9 else [])
    else:
        hot_keywords = []

    # Year-round hot categories
    always_hot = ['vitamin', 'supplement', 'protein', 'collagen', 'probiotic',
                  'teeth whitening', 'skincare set', 'led strip']

    all_keywords = hot_keywords + always_hot
    return any(kw in name for kw in all_keywords)


def _score_breakdown(p):
    """Return a dict of individual score components for display."""
    import math
    sales_7d = p.sales_7d or 0
    sales_30d = p.sales_30d or 0
    creators = p.influencer_count or 0
    comm = (p.commission_rate or 0) * 100
    price = p.price or 0

    demand = min(25, math.log10(max(sales_7d, 1)) * 5.8)

    if sales_30d > 0:
        weekly_avg = sales_30d / 4.3
        accel = sales_7d / weekly_avg if weekly_avg > 0 else 1
        momentum = min(20, max(0, (accel - 0.5) * 16))
    else:
        momentum = 10 if sales_7d > 0 else 0

    commission_score = min(20, comm * 0.8)

    if sales_7d > 0 and creators > 0:
        gap = min(20, math.log10(max(sales_7d / creators, 0.1)) * 9)
    elif sales_7d > 100:
        gap = 20
    else:
        gap = 5

    if 10 <= price <= 35:
        price_score = 14
    elif 35 < price <= 60:
        price_score = 10
    elif 5 <= price < 10:
        price_score = 8
    elif 60 < price <= 100:
        price_score = 6
    else:
        price_score = 3

    return {
        'demand': round(demand),
        'demand_max': 25,
        'momentum': round(momentum),
        'momentum_max': 20,
        'commission': round(commission_score),
        'commission_max': 20,
        'saturation_gap': round(gap),
        'saturation_gap_max': 20,
        'price_fit': round(price_score),
        'price_fit_max': 14,
    }


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
    is_pro = bool(
        (sub and sub.status == 'active') or
        (user and user.is_admin)
    )
    return {
        'current_user': user,
        'subscription': sub,
        'active_page': active_page,
        'is_pro': is_pro,
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
    try:
        brand_count = ScannedBrand.query.count()
    except Exception:
        try:
            brand_count = Brand.query.count()
        except Exception:
            brand_count = 0

    ctx['stats'] = {
        'tracked_count': total,
        'trending_today': trending,
        'avg_commission': avg_comm * 100,
        'brand_count': brand_count,
    }

    # Trending products (top 6 by 7d sales, min 5 videos, has sales)
    ctx['trending_products'] = Product.query.filter(
        _active_filter, Product.video_count >= 5, Product.sales_7d > 0
    ).order_by(desc(Product.sales_7d)).limit(6).all()

    for p in ctx['trending_products']:
        p.trending_score = _calc_score(p)

    # Recently viewed products (real user tracking)
    recent_products = []
    try:
        user = ctx['current_user']
        if user:
            recent_views = db.session.query(ProductView, Product).join(
                Product, ProductView.product_id == Product.product_id
            ).filter(
                ProductView.user_id == user.id
            ).order_by(desc(ProductView.viewed_at)).limit(5).all()
            recent_products = [p for _, p in recent_views]
            for p in recent_products:
                p.trending_score = _calc_score(p)
    except Exception:
        pass
    ctx['recent_products'] = recent_products

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
    tap_only = request.args.get('tap_only', '') == '1'
    on_sale = request.args.get('on_sale', '') == '1'

    query = Product.query.filter(
        _active_filter,
        Product.video_count >= 5,
        Product.sales_7d > 0
    )

    # TAP boosted filter — only show products with active TAP links
    if tap_only:
        try:
            query = query.join(TapProduct, Product.product_id == TapProduct.product_id).filter(
                TapProduct.is_active == True
            )
        except Exception:
            pass

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

    if on_sale:
        query = query.filter(
            Product.original_price > Product.price,
            Product.original_price > 0,
            Product.price > 0,
        )

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

    # Build filter params string for sort header links
    params = []
    if category: params.append(f'category={category}')
    if search: params.append(f'search={search}')
    if min_comm: params.append(f'min_commission={min_comm}')
    if max_price: params.append(f'max_price={max_price}')
    if max_videos: params.append(f'max_videos={max_videos}')
    if on_sale: params.append('on_sale=1')
    ctx['filter_params'] = '&'.join(params)

    # Load TAP data for products (safe — won't crash if table missing)
    tap_map = {}
    try:
        tap_ids = [p.product_id for p in products]
        if tap_ids:
            taps = TapProduct.query.filter(
                TapProduct.product_id.in_(tap_ids),
                TapProduct.is_active == True
            ).all()
            tap_map = {t.product_id: t for t in taps}
    except Exception:
        pass

    for p in products:
        p.trending_score = _calc_score(p)
        p.lifecycle = _calc_lifecycle(p)
        p._tap = tap_map.get(p.product_id)
        p.is_hot = _is_seasonal_hot(p)
        # Discount / sale logic
        p.is_on_sale = bool(p.original_price and p.original_price > p.price and p.price > 0)
        p.discount_pct = round((1 - p.price / p.original_price) * 100) if p.is_on_sale else 0
        p.is_hot_deal = p.is_on_sale and p.trending_score >= 60
        # Sales momentum: compare 7d vs weekly average of 30d
        s7 = p.sales_7d or 0
        s30 = p.sales_30d or 0
        if s30 > 0 and s7 > 0:
            weekly_avg = s30 / 4.3
            p.sales_growth = round(((s7 - weekly_avg) / weekly_avg) * 100)
        else:
            p.sales_growth = 0

    ctx['products'] = products
    ctx['page'] = page
    ctx['total_pages'] = pagination.pages
    ctx['total_count'] = pagination.total
    ctx['has_filters'] = bool(category or search or min_comm > 0 or max_price > 0 or max_videos > 0 or on_sale)
    ctx['tap_filter'] = request.args.get('tap_only', '') == '1'
    ctx['on_sale_filter'] = on_sale

    return render_template('products.html', **ctx)


@views_bp.route('/app/favorites')
@login_required
def favorites_page():
    ctx = _base_context('favorites')
    products = Product.query.filter_by(is_favorite=True).filter(
        _active_filter
    ).order_by(desc(Product.sales_7d)).all()
    for p in products:
        p.trending_score = _calc_score(p)
        p.lifecycle = _calc_lifecycle(p)
    ctx['products'] = products
    return render_template('favorites.html', **ctx)


@views_bp.route('/app/products/<product_id>')
@login_required
def product_detail(product_id):
    import json
    from datetime import timedelta
    ctx = _base_context('products')

    # Try both raw ID and shop_ prefixed ID
    product = Product.query.get(product_id)
    if not product and not product_id.startswith('shop_'):
        product = Product.query.get(f'shop_{product_id}')
    if not product and product_id.startswith('shop_'):
        product = Product.query.get(product_id.replace('shop_', ''))
    if not product:
        from flask import abort
        abort(404)
    product.trending_score = _calc_score(product)
    product.lifecycle = _calc_lifecycle(product)
    ctx['product'] = product
    ctx['score_breakdown'] = _score_breakdown(product)

    # Similar opportunities (same category, sorted by score)
    similar = []
    if product.category:
        candidates = Product.query.filter(
            Product.category == product.category,
            Product.product_id != product_id,
            _active_filter,
            Product.sales_7d > 0,
            Product.video_count >= 5,
        ).order_by(desc(Product.sales_7d)).limit(20).all()
        for c in candidates:
            c.trending_score = _calc_score(c)
        similar = sorted(candidates, key=lambda x: x.trending_score, reverse=True)[:4]
    ctx['similar_products'] = similar

    # Track this view
    try:
        user = ctx['current_user']
        if user:
            # Upsert — update timestamp if already viewed, else create
            existing_view = ProductView.query.filter_by(
                user_id=user.id, product_id=product_id
            ).first()
            if existing_view:
                existing_view.viewed_at = datetime.utcnow()
            else:
                db.session.add(ProductView(user_id=user.id, product_id=product_id))
            db.session.commit()
    except Exception:
        db.session.rollback()

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
    import json
    from sqlalchemy import func
    ctx = _base_context('analytics')

    days = request.args.get('days', 30, type=int)
    if days not in (7, 30, 90):
        days = 30
    ctx['days'] = days

    base_q = Product.query.filter(_active_filter, Product.video_count >= 5)

    # KPI stats
    ctx['total_products'] = base_q.count()
    ctx['total_gmv'] = db.session.query(func.sum(Product.gmv)).filter(
        _active_filter, Product.video_count >= 5).scalar() or 0
    ctx['avg_commission'] = (db.session.query(func.avg(Product.commission_rate)).filter(
        _active_filter, Product.video_count >= 5, Product.commission_rate > 0).scalar() or 0) * 100
    ctx['total_creators'] = db.session.query(func.sum(Product.influencer_count)).filter(
        _active_filter, Product.video_count >= 5).scalar() or 0

    # Top categories by GMV
    cat_data = db.session.query(
        Product.category, func.sum(Product.gmv).label('total_gmv'), func.count().label('cnt')
    ).filter(
        _active_filter, Product.video_count >= 5,
        Product.category.isnot(None), Product.category != ''
    ).group_by(Product.category).order_by(desc('total_gmv')).limit(8).all()
    ctx['category_labels'] = json.dumps([c[0] for c in cat_data])
    ctx['category_values'] = json.dumps([round(c[1] or 0, 0) for c in cat_data])
    ctx['categories'] = [c[0] for c in cat_data]

    # Commission rate distribution
    comm_ranges = []
    for low, high, label in [(0, 0.05, '0-5%'), (0.05, 0.10, '5-10%'), (0.10, 0.15, '10-15%'),
                              (0.15, 0.20, '15-20%'), (0.20, 0.25, '20-25%'), (0.25, 1.0, '25%+')]:
        cnt = base_q.filter(Product.commission_rate >= low, Product.commission_rate < high).count()
        comm_ranges.append({'label': label, 'count': cnt})
    ctx['comm_labels'] = json.dumps([r['label'] for r in comm_ranges])
    ctx['comm_values'] = json.dumps([r['count'] for r in comm_ranges])

    # Score distribution
    high_score = base_q.filter(Product.sales_7d > 5000).count()
    mid_score = base_q.filter(Product.sales_7d.between(1000, 5000)).count()
    low_score = base_q.filter(Product.sales_7d < 1000).count()
    ctx['score_labels'] = json.dumps(['High Demand (5K+)', 'Medium (1K-5K)', 'Emerging (<1K)'])
    ctx['score_values'] = json.dumps([high_score, mid_score, low_score])

    # Price distribution
    price_ranges = []
    for low, high, label in [(0, 10, '<$10'), (10, 25, '$10-25'), (25, 50, '$25-50'),
                              (50, 100, '$50-100'), (100, 9999, '$100+')]:
        cnt = base_q.filter(Product.price >= low, Product.price < high).count()
        price_ranges.append({'label': label, 'count': cnt})
    ctx['price_labels'] = json.dumps([r['label'] for r in price_ranges])
    ctx['price_values'] = json.dumps([r['count'] for r in price_ranges])

    # Top 10 products by GMV
    ctx['top_products'] = base_q.order_by(desc(Product.gmv)).limit(10).all()
    for p in ctx['top_products']:
        p.trending_score = _calc_score(p)

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
def brands_list_redirect():
    return redirect('/app/brand-hunter')


@views_bp.route('/app/brands-legacy')
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
            query = query.order_by(desc(Brand.follower_count.nullslast()))
        elif sort == 'trending':
            query = query.order_by(desc(Brand.trending_score.nullslast()))
        elif sort == 'products':
            query = query.order_by(desc(Brand.product_count.nullslast()))
        else:
            query = query.order_by(Brand.gmv_30d.desc().nullslast())

        pagination = query.paginate(page=page, per_page=30, error_out=False)

        categories = db.session.query(Brand.category).filter(
            Brand.category.isnot(None), Brand.category != ''
        ).distinct().order_by(Brand.category).all()

        brands = pagination.items
        # Update product counts for brands showing 0
        updated = False
        for b in brands:
            if not b.product_count or b.product_count == 0:
                if b.shop_id:
                    cnt = Product.query.filter(Product.seller_id == b.shop_id, _active_filter).count()
                    if cnt > 0:
                        b.product_count = cnt
                        updated = True
        if updated:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        ctx['brands'] = brands
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

    # Try fetching live products from EchoTik and save to DB
    try:
        from app.services.echotik import fetch_brand_products, sync_to_db
        if brand.shop_id:
            raw_products = fetch_brand_products(brand.shop_id, page=1, page_size=10)
            if raw_products:
                # Save to our DB so detail pages work
                try:
                    sync_to_db(raw_products)
                except Exception:
                    pass
                # Update brand product count
                brand.product_count = max(brand.product_count or 0, len(raw_products))
                db.session.commit()
    except Exception:
        pass

    # Get products from DB (includes any just-synced live products)
    db_products = []
    if True:
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
            p.trending_score = _calc_score(p)
        if db_products and (not brand.product_count or brand.product_count == 0):
            brand.product_count = len(db_products)
            db.session.commit()

    ctx['products'] = db_products
    ctx['brand_sort'] = brand_sort

    return render_template('brand_detail.html', **ctx)


# ---------------------------------------------------------------------------
# TAP Lists (boosted commission groups)
# ---------------------------------------------------------------------------

@views_bp.route('/app/tap-lists')
@login_required
def tap_lists_page():
    ctx = _base_context('boosted')
    category = request.args.get('category', '')
    try:
        query = TapList.query.filter_by(is_active=True)
        if category:
            query = query.filter(TapList.category == category)
        ctx['lists'] = query.order_by(desc(TapList.created_at)).all()
        cats = db.session.query(TapList.category).filter(
            TapList.is_active == True, TapList.category.isnot(None), TapList.category != ''
        ).distinct().all()
        ctx['categories'] = [c[0] for c in cats if c[0]]
    except Exception:
        ctx['lists'] = []
        ctx['categories'] = []
    ctx['current_category'] = category
    return render_template('tap_lists.html', **ctx)


@views_bp.route('/app/admin/tap-lists')
@login_required
def admin_tap_lists():
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    ctx = _base_context('admin')
    try:
        ctx['lists'] = TapList.query.order_by(desc(TapList.created_at)).all()
    except Exception:
        ctx['lists'] = []
    return render_template('admin_tap_lists.html', **ctx)


@views_bp.route('/app/admin/tap-lists/add', methods=['POST'])
@login_required
def admin_tap_lists_add():
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    data = request.form
    share_link = data.get('share_link', '').strip()
    if not share_link:
        return redirect('/app/admin/tap-lists')

    # Extract list_id from TikTok redirect
    tiktok_list_id = None
    try:
        import requests as req, re
        resp = req.get(share_link, allow_redirects=False, timeout=10,
                       headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
        location = resp.headers.get("Location", "")
        match = re.search(r'list_id=(\d+)', location)
        if match:
            tiktok_list_id = match.group(1)
    except Exception:
        pass

    tap = TapList(
        name=data.get('name', 'Untitled')[:200],
        partner=data.get('partner', 'Affiliate Automated')[:100],
        category=data.get('category', '')[:100],
        share_link=share_link,
        tiktok_list_id=tiktok_list_id,
        product_count=int(data.get('product_count', 0) or 0),
        description=data.get('description', ''),
    )
    db.session.add(tap)
    db.session.commit()
    return redirect('/app/admin/tap-lists')


@views_bp.route('/app/admin/tap-lists/<int:list_id>/toggle', methods=['POST'])
@api_auth
def admin_tap_lists_toggle(list_id):
    user = get_current_user()
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin required'}), 403
    tap = TapList.query.get(list_id)
    if tap:
        tap.is_active = not tap.is_active
        db.session.commit()
        return jsonify({'active': tap.is_active})
    return jsonify({'error': 'Not found'}), 404


@views_bp.route('/app/admin/tap-lists/<int:list_id>/delete', methods=['POST'])
@login_required
def admin_tap_lists_delete(list_id):
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    tap = TapList.query.get(list_id)
    if tap:
        db.session.delete(tap)
        db.session.commit()
    return redirect('/app/admin/tap-lists')


@views_bp.route('/api/tap-lists')
@api_auth
def api_tap_lists():
    """JSON API for active TAP lists (Discord bot, etc)."""
    try:
        lists = TapList.query.filter_by(is_active=True).order_by(desc(TapList.created_at)).all()
        return jsonify([{
            'id': t.id, 'name': t.name, 'partner': t.partner,
            'category': t.category, 'share_link': t.share_link,
            'product_count': t.product_count, 'description': t.description,
            'created_at': t.created_at.isoformat() if t.created_at else None,
        } for t in lists])
    except Exception:
        return jsonify([])


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

@views_bp.route('/api/admin/sync-categories', methods=['POST'])
@api_auth
def api_sync_categories():
    """Auto-categorize products by name keywords. No API calls needed."""
    user = get_current_user()
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin required'}), 403

    # Keyword → category mapping (checked in order, first match wins)
    RULES = [
        # Beauty & Personal Care
        ('Beauty & Personal Care', ['serum', 'moistur', 'cream', 'skincare', 'skin care', 'makeup',
            'cosmetic', 'lipstick', 'mascara', 'foundation', 'concealer', 'blush', 'eyeshadow',
            'eyeliner', 'cleanser', 'toner', 'sunscreen', 'spf', 'acne', 'collagen', 'retinol',
            'vitamin c serum', 'hyaluronic', 'face mask', 'facial', 'beauty', 'lash', 'brow',
            'nail polish', 'perfume', 'fragrance', 'deodorant', 'shampoo', 'conditioner',
            'hair oil', 'hair mask', 'hair care', 'body wash', 'lotion', 'soap', 'toothpaste',
            'whitening strip', 'teeth', 'pimple', 'blackhead', 'pore', 'wrinkle', 'anti-aging',
            'eye patch', 'lip gloss', 'lip balm', 'bronzer', 'primer', 'setting spray',
            'micellar', 'exfoliat', 'scrub', 'peel', 'turmeric', 'niacinamide', 'cosrx',
            'medicube', 'dr.melaxin', 'melaxin', 'anua', 'snp', 'tonymoly']),
        # Health & Wellness
        ('Health & Wellness', ['vitamin', 'supplement', 'protein', 'probiotic', 'magnesium',
            'omega', 'collagen supplement', 'ashwagandha', 'turmeric capsule', 'cbd',
            'melatonin', 'iron supplement', 'calcium', 'zinc', 'biotin', 'fiber', 'detox',
            'weight loss', 'diet', 'keto', 'creatine', 'pre workout', 'bcaa', 'electrolyte',
            'health', 'wellness', 'goli', 'neocell', 'toplux', 'capsule', 'tablet',
            'gummy', 'gummies', 'nutrition']),
        # Fashion & Apparel
        ('Fashion & Apparel', ['dress', 'shirt', 'blouse', 'jeans', 'pants', 'skirt',
            'jacket', 'coat', 'sweater', 'hoodie', 'legging', 'bra ', 'underwear', 'lingerie',
            'bikini', 'swimsuit', 'sock', 'belt', 'scarf', 'hat', 'cap', 'sunglasses',
            'watch', 'jewelry', 'necklace', 'bracelet', 'earring', 'ring ', 'tshirt',
            't-shirt', 'polo', 'cardigan', 'romper', 'jumpsuit', 'bodysuit', 'corset',
            'shapewear', 'activewear', 'athletic', 'yoga pants', 'sports bra',
            'women', 'clothing', 'apparel', 'fashion', 'outfit', 'wear',
            'oeak', 'jelly bra', 'wirefree']),
        # Home & Kitchen
        ('Home & Kitchen', ['kitchen', 'cookware', 'blender', 'mixer', 'utensil', 'knife',
            'cutting board', 'pan ', 'pot ', 'spatula', 'container', 'storage', 'organizer',
            'shelf', 'rack', 'hanger', 'pillow', 'blanket', 'bedding', 'mattress', 'towel',
            'curtain', 'rug', 'carpet', 'lamp', 'light', 'candle', 'vase', 'decoration',
            'wall art', 'mirror', 'clock', 'furniture', 'table', 'chair', 'desk',
            'home', 'house', 'bathroom', 'shower', 'peeler', 'slicer', 'grinder']),
        # Electronics & Accessories
        ('Electronics', ['phone case', 'charger', 'cable', 'headphone', 'earphone', 'earbud',
            'speaker', 'bluetooth', 'wireless', 'usb', 'power bank', 'adapter', 'led strip',
            'led light', 'ring light', 'camera', 'tripod', 'microphone', 'keyboard', 'mouse',
            'monitor', 'laptop', 'tablet', 'smart watch', 'smartwatch', 'gaming',
            'controller', 'vr', 'drone', 'electronic', 'tech', 'digital', 'headlamp']),
        # Food & Beverage
        ('Food & Beverage', ['coffee', 'tea ', 'snack', 'candy', 'chocolate', 'protein bar',
            'energy drink', 'juice', 'sauce', 'spice', 'seasoning', 'honey', 'syrup',
            'sugar', 'zero sugar', 'food', 'beverage', 'drink', 'matcha', 'gum']),
        # Sports & Outdoor
        ('Sports & Outdoor', ['gym', 'fitness', 'workout', 'exercise', 'resistance band',
            'dumbbell', 'yoga mat', 'sports', 'outdoor', 'camping', 'hiking', 'bicycle',
            'golf', 'basketball', 'soccer', 'football', 'tennis', 'running', 'athletic shoe',
            'massager', 'massage gun', 'foam roller', 'neck massager']),
        # Pet Supplies
        ('Pet Supplies', ['dog', 'cat', 'pet', 'puppy', 'kitten', 'fish tank', 'aquarium',
            'bird', 'hamster', 'leash', 'collar', 'pet food', 'pet toy']),
        # Baby & Kids
        ('Baby & Kids', ['baby', 'toddler', 'infant', 'diaper', 'pacifier', 'stroller',
            'crib', 'nursery', 'kids', 'children', 'toy', 'puzzle', 'plush', 'stuffed',
            'squishy', 'blind box', 'action figure', 'lego', 'building block']),
        # Automotive
        ('Automotive', ['car ', 'vehicle', 'automotive', 'auto ', 'tire', 'windshield',
            'dash cam', 'car seat', 'steering', 'headlight', 'headlamp restoration']),
    ]

    products = Product.query.filter(
        _active_filter,
        or_(Product.category.is_(None), Product.category == ''),
    ).all()

    enriched = 0
    for p in products:
        name = (p.product_name or '').lower()
        for cat_name, keywords in RULES:
            if any(kw in name for kw in keywords):
                p.category = cat_name
                enriched += 1
                break

    db.session.commit()

    remaining = Product.query.filter(
        _active_filter,
        or_(Product.category.is_(None), Product.category == ''),
    ).count()

    return jsonify({'success': True, 'enriched': enriched, 'remaining': remaining})


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


@views_bp.route('/api/admin/sync-sellers', methods=['POST'])
@api_auth
def api_sync_sellers():
    """Enrich seller/shop names for products that have 'Unknown' seller via product detail API."""
    user = get_current_user()
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin required'}), 403

    import time as _time
    from app.services.echotik import fetch_product_detail

    products = Product.query.filter(
        _active_filter,
        or_(
            Product.seller_name.is_(None),
            Product.seller_name == '',
            Product.seller_name == 'Unknown',
            Product.seller_name == 'Unknown Seller',
        ),
    ).order_by(Product.sales_7d.desc().nullslast()).limit(50).all()

    if not products:
        return jsonify({'success': True, 'enriched': 0, 'remaining': 0})

    enriched = 0
    for p in products:
        raw_id = p.product_id.replace('shop_', '')
        try:
            detail = fetch_product_detail(raw_id)
            if detail:
                sname = (detail.get('seller_name') or '').strip()
                if sname and sname.lower() not in ('unknown', 'none', 'null', ''):
                    p.seller_name = sname
                    enriched += 1
                sid = detail.get('seller_id')
                if sid and not p.seller_id:
                    p.seller_id = sid
            _time.sleep(0.3)
        except Exception:
            continue

    db.session.commit()

    remaining = Product.query.filter(
        _active_filter,
        or_(
            Product.seller_name.is_(None),
            Product.seller_name == '',
            Product.seller_name == 'Unknown',
        ),
    ).count()

    return jsonify({'success': True, 'enriched': enriched, 'remaining': remaining})


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


# ---------------------------------------------------------------------------
# Admin — Campaign Banner Management
# ---------------------------------------------------------------------------

@views_bp.route('/app/admin/campaigns')
@login_required
def admin_campaigns():
    ctx = _base_context('campaigns')
    user = ctx['current_user']
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    ctx['campaigns'] = CampaignBanner.query.order_by(
        CampaignBanner.priority.desc(), CampaignBanner.created_at.desc()
    ).all()
    return render_template('admin_campaigns.html', **ctx)


@views_bp.route('/app/admin/campaigns/add', methods=['POST'])
@login_required
def admin_campaign_add():
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')

    title = request.form.get('title', '').strip()
    message = request.form.get('message', '').strip()
    if not title or not message:
        return redirect('/app/admin/campaigns')

    campaign = CampaignBanner(
        title=title[:200],
        message=message[:500],
        link_url=request.form.get('link_url', '').strip()[:500] or None,
        link_text=request.form.get('link_text', '').strip()[:100] or None,
        color_scheme=request.form.get('color_scheme', 'fire'),
        is_dismissible=request.form.get('is_dismissible') == 'on',
        priority=int(request.form.get('priority', 0) or 0),
        is_active=True,
    )

    # Parse dates
    starts = request.form.get('starts_at', '').strip()
    ends = request.form.get('ends_at', '').strip()
    if starts:
        try:
            campaign.starts_at = datetime.fromisoformat(starts)
        except ValueError:
            pass
    if ends:
        try:
            campaign.ends_at = datetime.fromisoformat(ends)
        except ValueError:
            pass

    db.session.add(campaign)
    db.session.commit()
    return redirect('/app/admin/campaigns')


@views_bp.route('/app/admin/campaigns/<int:cid>/toggle', methods=['POST'])
@login_required
def admin_campaign_toggle(cid):
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    c = CampaignBanner.query.get(cid)
    if c:
        c.is_active = not c.is_active
        db.session.commit()
    return redirect('/app/admin/campaigns')


@views_bp.route('/app/admin/campaigns/<int:cid>/delete', methods=['POST'])
@login_required
def admin_campaign_delete(cid):
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    c = CampaignBanner.query.get(cid)
    if c:
        db.session.delete(c)
        db.session.commit()
    return redirect('/app/admin/campaigns')


# ---------------------------------------------------------------------------
# Coupon / Promo Code System
# ---------------------------------------------------------------------------

@views_bp.route('/app/redeem', methods=['GET', 'POST'])
@login_required
def redeem_coupon():
    ctx = _base_context('redeem')
    user = ctx['current_user']
    if not user:
        return redirect('/login')

    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        if not code:
            flash('Please enter a coupon code.', 'error')
            return redirect('/app/redeem')

        coupon = CouponCode.query.filter_by(code=code).first()

        if not coupon or not coupon.is_active:
            flash('Invalid coupon code.', 'error')
        elif coupon.max_uses and coupon.times_used >= coupon.max_uses:
            flash('This coupon has reached its maximum uses.', 'error')
        elif coupon.expires_at and datetime.utcnow() > coupon.expires_at:
            flash('This coupon has expired.', 'error')
        elif CouponRedemption.query.filter_by(coupon_id=coupon.id, user_id=user.id).first():
            flash("You've already redeemed this coupon.", 'error')
        else:
            # Apply the coupon
            if coupon.discount_type == 'free_months':
                # Grant or extend subscription
                sub = Subscription.query.filter_by(user_id=user.id).first()
                if not sub:
                    sub = Subscription(user_id=user.id)
                    db.session.add(sub)

                # Calculate new end date
                base = datetime.utcnow()
                if sub.status == 'active' and sub.next_billing_date and sub.next_billing_date > base:
                    base = sub.next_billing_date  # Extend from current end

                new_end = base + timedelta(days=30 * coupon.discount_value)
                sub.status = 'active'
                sub.plan = f'coupon_{coupon.discount_value}mo'
                sub.coupon_code = coupon.code
                sub.next_billing_date = new_end

                # Log redemption
                redemption = CouponRedemption(coupon_id=coupon.id, user_id=user.id)
                coupon.times_used += 1
                db.session.add(redemption)
                db.session.commit()

                flash(f"Coupon applied! You have Pro access until {new_end.strftime('%B %d, %Y')}.", 'success')
                return redirect('/app/dashboard')
            else:
                # percent_off / fixed_off — not used for direct access, just log it
                redemption = CouponRedemption(coupon_id=coupon.id, user_id=user.id)
                coupon.times_used += 1
                db.session.add(redemption)
                db.session.commit()
                flash(f"Coupon {code} applied!", 'success')
                return redirect('/app/subscribe')

        return redirect('/app/redeem')

    # GET — show recent redemptions for this user
    ctx['redemptions'] = CouponRedemption.query.filter_by(user_id=user.id).order_by(
        CouponRedemption.redeemed_at.desc()
    ).all()
    return render_template('redeem.html', **ctx)


# ---------------------------------------------------------------------------
# Admin — Coupon Management
# ---------------------------------------------------------------------------

@views_bp.route('/app/admin/coupons')
@login_required
def admin_coupons():
    ctx = _base_context('coupons')
    user = ctx['current_user']
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    ctx['coupons'] = CouponCode.query.order_by(CouponCode.created_at.desc()).all()
    ctx['now'] = datetime.utcnow()
    return render_template('admin_coupons.html', **ctx)


@views_bp.route('/app/admin/coupons/add', methods=['POST'])
@login_required
def admin_coupon_add():
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')

    code = request.form.get('code', '').strip().upper()
    discount_type = request.form.get('discount_type', 'free_months')
    discount_value = int(request.form.get('discount_value', 1) or 1)
    max_uses = request.form.get('max_uses', '1').strip()
    expires = request.form.get('expires_at', '').strip()

    if not code:
        flash('Coupon code is required.', 'error')
        return redirect('/app/admin/coupons')

    if CouponCode.query.filter_by(code=code).first():
        flash(f'Coupon "{code}" already exists.', 'error')
        return redirect('/app/admin/coupons')

    coupon = CouponCode(
        code=code,
        discount_type=discount_type,
        discount_value=discount_value,
        max_uses=int(max_uses) if max_uses else None,
        is_active=True,
        created_by=user.discord_username,
    )
    if expires:
        try:
            coupon.expires_at = datetime.fromisoformat(expires)
        except ValueError:
            pass

    db.session.add(coupon)
    db.session.commit()
    flash(f'Coupon {code} created!', 'success')
    return redirect('/app/admin/coupons')


@views_bp.route('/app/admin/coupons/<int:cid>/toggle', methods=['POST'])
@login_required
def admin_coupon_toggle(cid):
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    c = CouponCode.query.get(cid)
    if c:
        c.is_active = not c.is_active
        db.session.commit()
    return redirect('/app/admin/coupons')


@views_bp.route('/app/admin/coupons/<int:cid>/delete', methods=['POST'])
@login_required
def admin_coupon_delete(cid):
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/dashboard')
    c = CouponCode.query.get(cid)
    if c:
        db.session.delete(c)
        db.session.commit()
    return redirect('/app/admin/coupons')


# ---------------------------------------------------------------------------
# Brand Hunter v2 — Deep Scan System
# ---------------------------------------------------------------------------

@views_bp.route('/app/brand-hunter')
@login_required
def brand_hunter():
    ctx = _base_context('brands')
    try:
        ctx['brands'] = ScannedBrand.query.order_by(
            ScannedBrand.sales_30d.desc().nullslast()
        ).all()
    except Exception:
        db.session.rollback()
        ctx['brands'] = []

    try:
        active_job = BrandScanJob.query.filter(
            BrandScanJob.status.in_(['queued', 'running'])
        ).order_by(BrandScanJob.created_at.desc()).first()
        ctx['active_job'] = active_job
    except Exception:
        db.session.rollback()
        ctx['active_job'] = None

    return render_template('brand_hunter.html', **ctx)


@views_bp.route('/app/brand-hunter/search')
@login_required
def brand_hunter_search():
    """Typeahead search for brands via EchoTik API."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    try:
        from app.services.echotik import search_sellers
        results = search_sellers(q)
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@views_bp.route('/app/brand-hunter/browse')
@login_required
def brand_hunter_browse():
    """Fetch top brands from EchoTik ranking for selection. Returns JSON."""
    import logging
    log = logging.getLogger(__name__)
    page = request.args.get('page', 1, type=int)
    try:
        from app.services.echotik import fetch_top_shops
        sellers = fetch_top_shops(country="US", page_size=10, page=page)
        log.info(f"[BrandHunter] browse page={page}: got {len(sellers)} sellers")
        # Add ranking number based on page position
        results = []
        for i, s in enumerate(sellers):
            s['rank'] = (page - 1) * 10 + i + 1
            # Check if already scanned
            try:
                existing = ScannedBrand.query.filter_by(brand_id=str(s.get('shop_id', ''))).first()
                s['already_scanned'] = bool(existing)
            except Exception:
                s['already_scanned'] = False
            results.append(s)
        return jsonify({'brands': results, 'page': page})
    except Exception as e:
        log.error(f"[BrandHunter] browse error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'brands': []}), 500


@views_bp.route('/app/brand-hunter/scan', methods=['POST'])
@login_required
def brand_hunter_scan():
    """Launch background scan of product pages for one or more brands."""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Auth required'}), 401

    data = request.get_json() or {}
    page_start = int(data.get('page_start', 1))
    page_end = int(data.get('page_end', 50))

    # Accept single brand or array of brands
    brands_to_scan = data.get('brands', [])
    if not brands_to_scan:
        # Fallback: single brand format
        bid = data.get('brand_id', '').strip()
        bname = data.get('brand_name', '').strip()
        if bid and bname:
            brands_to_scan = [{'shop_id': bid, 'name': bname}]

    if not brands_to_scan:
        return jsonify({'error': 'Select at least one brand'}), 400

    # Sanity checks
    page_start = max(1, page_start)
    page_end = min(500, page_end)
    if page_end < page_start:
        page_end = page_start

    # Check for existing running job
    existing = BrandScanJob.query.filter(
        BrandScanJob.status.in_(['queued', 'running'])
    ).first()
    if existing:
        return jsonify({'error': 'A scan is already running', 'job_id': existing.id}), 409

    # Store brand list as comma-separated in brand_name for progress display
    brand_names = [b.get('name', '?') for b in brands_to_scan]
    job = BrandScanJob(
        brand_id_str=','.join(b.get('shop_id', '') for b in brands_to_scan),
        brand_name=brand_names[0] if len(brand_names) == 1 else f"{len(brand_names)} brands",
        page_start=page_start,
        page_end=page_end,
        status='queued',
    )
    db.session.add(job)
    db.session.commit()

    from app import executor
    from flask import current_app
    app = current_app._get_current_object()
    executor.submit(_run_batch_brand_scan, app, job.id, brands_to_scan)

    return jsonify({'job_id': job.id})


@views_bp.route('/app/brand-hunter/scan/<int:job_id>/status')
@login_required
def brand_hunter_scan_status(job_id):
    """Poll scan progress."""
    job = BrandScanJob.query.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'status': job.status,
        'current_page': job.current_page,
        'page_start': job.page_start,
        'page_end': job.page_end,
        'brand_name': job.brand_name or '',
        'products_found': job.products_found,
        'error_message': job.error_message,
    })


@views_bp.route('/app/brand-hunter/scan/<int:job_id>/stop', methods=['POST'])
@login_required
def brand_hunter_scan_stop(job_id):
    """Stop a running scan."""
    job = BrandScanJob.query.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.status in ('queued', 'running'):
        job.status = 'stopped'
        job.completed_at = datetime.utcnow()
        # Mark the brand as complete with whatever products we have so far
        if job.brand_id_str:
            brand = ScannedBrand.query.filter_by(brand_id=job.brand_id_str).first()
            if brand:
                brand.scan_status = 'complete'
        db.session.commit()
    return jsonify({'success': True})


def _scan_single_brand(shop_id, brand_name, page_start, page_end, job):
    """Scan product pages for a single brand. Called within app context."""
    import time as _time
    from app.services.echotik import fetch_brand_products

    brand = ScannedBrand.query.filter_by(brand_id=str(shop_id)).first()
    if not brand:
        brand = ScannedBrand(brand_id=str(shop_id))
        db.session.add(brand)
    brand.brand_name = brand_name
    brand.scan_status = 'scanning'
    brand.pages_scanned = f"{page_start}-{page_end}"
    db.session.flush()
    db.session.commit()

    all_products = []
    empty_streak = 0

    for page in range(page_start, page_end + 1):
        # Check if scan was stopped
        db.session.refresh(job)
        if job.status in ('stopped', 'error'):
            break

        job.current_page = page
        job.brand_name = brand_name  # Update display name for current brand
        db.session.commit()

        try:
            products = fetch_brand_products(str(shop_id), page=page, page_size=10)
        except Exception:
            products = []

        if not products:
            empty_streak += 1
            if empty_streak >= 5:
                break
            _time.sleep(0.2)
            continue
        empty_streak = 0

        for p in products:
            bp = BrandProduct(
                brand_id=brand.id,
                product_id=p.get('product_id', ''),
                title=(p.get('product_name', '') or '')[:500],
                image_url=p.get('image_url', ''),
                price=p.get('price', 0) or 0,
                commission_rate=p.get('commission_rate', 0) or 0,
                sales_30d=p.get('sales_30d', 0) or p.get('sales', 0) or 0,
                revenue_30d=p.get('gmv_30d', 0) or p.get('gmv', 0) or 0,
                total_videos=p.get('video_count_alltime', 0) or p.get('video_count', 0) or 0,
                total_sales=p.get('sales', 0) or 0,
                influencer_count=p.get('influencer_count', 0) or 0,
                category=p.get('category', ''),
                page_found=page,
                is_hidden_gem=(page > 99),
            )
            all_products.append(bp)

        job.products_found = (job.products_found or 0) + len(products)
        db.session.commit()
        _time.sleep(0.3)

    # Clear old products, save new
    BrandProduct.query.filter_by(brand_id=brand.id).delete()
    for bp in all_products:
        bp.brand_id = brand.id
        db.session.add(bp)

    # Update brand stats
    brand.total_products = len(all_products)
    brand.sales_30d = sum(bp.sales_30d or 0 for bp in all_products)
    brand.revenue_30d = sum(bp.revenue_30d or 0 for bp in all_products)
    brand.units_sold_30d = sum(bp.total_sales or 0 for bp in all_products)
    if all_products:
        comms = [bp.commission_rate for bp in all_products if bp.commission_rate and bp.commission_rate > 0]
        brand.avg_commission = sum(comms) / len(comms) if comms else 0
        top = max(all_products, key=lambda bp: bp.total_sales or 0)
        brand.top_product_name = top.title
    brand.is_hidden_gem = any(bp.is_hidden_gem for bp in all_products)
    brand.scan_status = 'complete'
    brand.last_scanned = datetime.utcnow()
    db.session.commit()

    return len(all_products)


def _run_batch_brand_scan(app, job_id, brands_list):
    """Background: scan product pages for one or more brands."""
    with app.app_context():
        job = BrandScanJob.query.get(job_id)
        if not job:
            return
        job.status = 'running'
        job.started_at = datetime.utcnow()
        job.products_found = 0
        db.session.commit()

        try:
            for brand_info in brands_list:
                db.session.refresh(job)
                if job.status in ('stopped', 'error'):
                    break

                shop_id = brand_info.get('shop_id', '')
                name = brand_info.get('name', 'Unknown')
                if not shop_id:
                    continue

                _scan_single_brand(shop_id, name, job.page_start, job.page_end, job)

            if job.status == 'running':
                job.status = 'complete'
            job.completed_at = datetime.utcnow()
            db.session.commit()

        except Exception as e:
            job.status = 'error'
            job.error_message = str(e)[:500]
            job.completed_at = datetime.utcnow()
            db.session.commit()


@views_bp.route('/app/brand-hunter/<int:brand_id>')
@login_required
def brand_hunter_detail(brand_id):
    ctx = _base_context('brands')
    brand = ScannedBrand.query.get_or_404(brand_id)
    sort = request.args.get('sort', 'videos')
    gems_only = request.args.get('gems_only', '') == '1'

    query = BrandProduct.query.filter_by(brand_id=brand.id)

    if gems_only:
        query = query.filter(BrandProduct.is_hidden_gem == True)

    if sort == 'sales':
        query = query.order_by(BrandProduct.sales_30d.desc().nullslast())
    elif sort == 'commission':
        query = query.order_by(BrandProduct.commission_rate.desc().nullslast())
    elif sort == 'price':
        query = query.order_by(BrandProduct.price.desc().nullslast())
    elif sort == 'score':
        query = query.order_by(BrandProduct.vantage_score.desc().nullslast())
    else:  # videos (default)
        query = query.order_by(BrandProduct.total_videos.desc().nullslast())

    products = query.limit(100).all()

    ctx['brand'] = brand
    ctx['products'] = products
    ctx['brand_sort'] = sort
    ctx['gems_only'] = gems_only
    return render_template('brand_hunter_detail.html', **ctx)


@views_bp.route('/app/brand-hunter/delete-batch', methods=['POST'])
@login_required
def brand_hunter_delete_batch():
    """Mass delete selected brands."""
    user = get_current_user()
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin required'}), 403
    data = request.get_json() or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'error': 'No brands selected'}), 400
    deleted = 0
    for bid in ids:
        brand = ScannedBrand.query.get(int(bid))
        if brand:
            db.session.delete(brand)
            deleted += 1
    db.session.commit()
    return jsonify({'success': True, 'deleted': deleted})


@views_bp.route('/app/brand-hunter/delete-all', methods=['POST'])
@login_required
def brand_hunter_delete_all():
    """Delete ALL scanned brands."""
    user = get_current_user()
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin required'}), 403
    BrandProduct.query.delete()
    ScannedBrand.query.delete()
    db.session.commit()
    return jsonify({'success': True})


@views_bp.route('/app/brand-hunter/<int:brand_id>/delete', methods=['POST'])
@login_required
def brand_hunter_delete(brand_id):
    user = get_current_user()
    if not user or not user.is_admin:
        return redirect('/app/brand-hunter')
    brand = ScannedBrand.query.get(brand_id)
    if brand:
        db.session.delete(brand)
        db.session.commit()
    return redirect('/app/brand-hunter')
