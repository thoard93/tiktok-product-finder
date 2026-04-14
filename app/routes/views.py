"""
Vantage -- View Routes Blueprint
Serves Jinja2 templates for all frontend pages.
Existing API routes remain unchanged in their respective blueprints.
"""

from functools import wraps
from flask import Blueprint, render_template, redirect, session, request, jsonify
from sqlalchemy import desc
from app import db
from app.models import Product, BlacklistedBrand, Subscription, User
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
    total = Product.query.filter_by(product_status='active').count()
    trending = Product.query.filter(
        Product.product_status == 'active',
        Product.sales_7d > 50
    ).count()
    from sqlalchemy import func
    avg_comm = db.session.query(func.avg(Product.commission_rate)).filter(
        Product.product_status == 'active',
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
        Product.product_status == 'active'
    ).order_by(desc(Product.sales_7d)).limit(6).all()

    # Add trending_score attribute for template
    for p in ctx['trending_products']:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    # Recent products (last updated)
    ctx['recent_products'] = Product.query.filter(
        Product.product_status == 'active'
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

    query = Product.query.filter(Product.product_status == 'active')

    if search:
        query = query.filter(Product.product_name.ilike(f'%{search}%'))

    if sort == 'commission':
        query = query.order_by(desc(Product.commission_rate))
    elif sort == 'new':
        query = query.order_by(desc(Product.first_seen))
    elif sort == 'gmv':
        query = query.order_by(desc(Product.gmv))
    else:
        query = query.order_by(desc(Product.sales_7d))

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    products = pagination.items

    for p in products:
        p.trending_score = min(99, int((p.sales_7d or 0) / 10 + (p.influencer_count or 0) * 3 + (p.commission_rate or 0) * 200))

    ctx['products'] = products
    ctx['page'] = page
    ctx['total_pages'] = pagination.pages

    return render_template('products.html', **ctx)


@views_bp.route('/app/products/<product_id>')
@login_required
def product_detail(product_id):
    ctx = _base_context('products')

    product = Product.query.get_or_404(product_id)
    product.trending_score = min(99, int((product.sales_7d or 0) / 10 + (product.influencer_count or 0) * 3 + (product.commission_rate or 0) * 200))
    ctx['product'] = product

    # Similar products (same category)
    similar = []
    if product.category:
        similar = Product.query.filter(
            Product.category == product.category,
            Product.product_id != product_id,
            Product.product_status == 'active'
        ).order_by(desc(Product.sales_7d)).limit(4).all()
    ctx['similar_products'] = similar

    # Creator videos placeholder (would come from API)
    ctx['creator_videos'] = []

    return render_template('product_detail.html', **ctx)


@views_bp.route('/app/analytics')
@login_required
def analytics():
    ctx = _base_context('analytics')

    ctx['top_products'] = Product.query.filter(
        Product.product_status == 'active'
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


@views_bp.route('/app/admin')
@login_required
def admin_panel():
    ctx = _base_context('admin')
    user = ctx['current_user']
    if not user or not user.is_admin:
        return redirect('/app/dashboard')

    from sqlalchemy import func
    ctx['total_products'] = Product.query.count()
    ctx['active_products'] = Product.query.filter_by(product_status='active').count()
    ctx['blacklisted_count'] = BlacklistedBrand.query.count()
    ctx['user_count'] = User.query.count()

    # Last sync: most recent last_echotik_sync across all products
    last = db.session.query(func.max(Product.last_echotik_sync)).scalar()
    if last:
        ctx['last_sync'] = last.strftime('%b %d, %Y at %I:%M %p UTC')
    else:
        ctx['last_sync'] = None

    return render_template('admin.html', **ctx)


# ---------------------------------------------------------------------------
# API endpoints for frontend (blacklist CRUD, profile update)
# ---------------------------------------------------------------------------

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
