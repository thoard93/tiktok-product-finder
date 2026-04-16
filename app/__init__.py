"""
PRISM — Flask Application Factory
Creates the Flask app, initializes extensions, registers all blueprints.
"""

import os
import secrets
from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.pool import NullPool
from concurrent.futures import ThreadPoolExecutor

db = SQLAlchemy()
executor = ThreadPoolExecutor(max_workers=4)


def _auto_migrate(app, db):
    """Safely add missing columns and fix broken tables. Idempotent."""
    # Add missing columns to existing tables
    column_migrations = [
        ("products", "trend_data_json", "TEXT"),
        ("products", "trend_last_synced", "TIMESTAMP"),
        ("products", "lookup_count", "INTEGER DEFAULT 0"),
        # Brand Hunter v2 — new columns on brand_scan_jobs
        ("brand_scan_jobs", "brand_id_str", "VARCHAR(100)"),
        ("brand_scan_jobs", "brand_name", "VARCHAR(300)"),
        ("brand_scan_jobs", "brand_logo_url", "VARCHAR(500)"),
    ]
    for table, column, col_type in column_migrations:
        try:
            db.session.execute(db.text(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
            ))
            db.session.commit()
            print(f"[MIGRATE] Added {table}.{column}")
        except Exception:
            db.session.rollback()

    # Fix brands table if it was created without id column
    try:
        db.session.execute(db.text("SELECT id FROM brands LIMIT 1"))
        db.session.rollback()
    except Exception:
        db.session.rollback()
        try:
            db.session.execute(db.text("DROP TABLE IF EXISTS brands"))
            db.session.commit()
            db.create_all()
            print("[MIGRATE] Recreated brands table with correct schema")
        except Exception as e:
            db.session.rollback()
            print(f"[MIGRATE] brands table fix failed: {e}")

    # Fix product_videos table if missing
    try:
        db.session.execute(db.text("SELECT id FROM product_videos LIMIT 1"))
        db.session.rollback()
    except Exception:
        db.session.rollback()
        try:
            db.create_all()
            print("[MIGRATE] Created product_videos table")
        except Exception as e:
            db.session.rollback()
            print(f"[MIGRATE] product_videos creation failed: {e}")

    # Fix tap_products table if missing
    try:
        db.session.execute(db.text("SELECT id FROM tap_products LIMIT 1"))
        db.session.rollback()
    except Exception:
        db.session.rollback()
        try:
            db.create_all()
            print("[MIGRATE] Created tap_products table")
        except Exception as e:
            db.session.rollback()
            print(f"[MIGRATE] tap_products creation failed: {e}")

    # Fix tap_lists table if missing
    try:
        db.session.execute(db.text("SELECT id FROM tap_lists LIMIT 1"))
        db.session.rollback()
    except Exception:
        db.session.rollback()
        try:
            db.create_all()
            print("[MIGRATE] Created tap_lists table")
        except Exception as e:
            db.session.rollback()
            print(f"[MIGRATE] tap_lists creation failed: {e}")

    # Fix product_views table if missing
    try:
        db.session.execute(db.text("SELECT id FROM product_views LIMIT 1"))
        db.session.rollback()
    except Exception:
        db.session.rollback()
        try:
            db.create_all()
            print("[MIGRATE] Created product_views table")
        except Exception as e:
            db.session.rollback()
            print(f"[MIGRATE] product_views creation failed: {e}")

    # Fix ebay tables if missing
    for tbl in ['ebay_watchlist', 'ebay_search_history']:
        try:
            db.session.execute(db.text(f"SELECT id FROM {tbl} LIMIT 1"))
            db.session.rollback()
        except Exception:
            db.session.rollback()
            try:
                db.create_all()
                print(f"[MIGRATE] Created {tbl} table")
            except Exception as e:
                db.session.rollback()
                print(f"[MIGRATE] {tbl} creation failed: {e}")

    # Fix campaign_banners table if missing
    try:
        db.session.execute(db.text("SELECT id FROM campaign_banners LIMIT 1"))
        db.session.rollback()
    except Exception:
        db.session.rollback()
        try:
            db.create_all()
            print("[MIGRATE] Created campaign_banners table")
        except Exception as e:
            db.session.rollback()
            print(f"[MIGRATE] campaign_banners creation failed: {e}")

    # Fix coupon tables if missing
    for tbl in ['coupon_codes', 'coupon_redemptions']:
        try:
            db.session.execute(db.text(f"SELECT id FROM {tbl} LIMIT 1"))
            db.session.rollback()
        except Exception:
            db.session.rollback()
            try:
                db.create_all()
                print(f"[MIGRATE] Created {tbl} table")
            except Exception as e:
                db.session.rollback()
                print(f"[MIGRATE] {tbl} creation failed: {e}")

    # Fix Brand Hunter v2 tables if missing
    for tbl in ['scanned_brands', 'brand_products']:
        try:
            db.session.execute(db.text(f"SELECT id FROM {tbl} LIMIT 1"))
            db.session.rollback()
        except Exception:
            db.session.rollback()
            try:
                db.create_all()
                print(f"[MIGRATE] Created {tbl} table")
            except Exception as e:
                db.session.rollback()
                print(f"[MIGRATE] {tbl} creation failed: {e}")

    # brand_scan_jobs — drop & recreate if schema is wrong (safe, just job tracking)
    try:
        db.session.execute(db.text("SELECT brand_id_str FROM brand_scan_jobs LIMIT 1"))
        db.session.rollback()
    except Exception:
        db.session.rollback()
        try:
            db.session.execute(db.text("DROP TABLE IF EXISTS brand_scan_jobs"))
            db.session.commit()
            db.create_all()
            print("[MIGRATE] Recreated brand_scan_jobs table with correct schema")
        except Exception as e:
            db.session.rollback()
            print(f"[MIGRATE] brand_scan_jobs fix failed: {e}")


def create_app():
    """Create and configure the Flask application."""
    # root_path must point to the PROJECT root (one level up from this file)
    # so that send_from_directory('pwa', ...) resolves to <project>/pwa/
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    flask_app = Flask(
        __name__,
        static_folder=os.path.join(project_root, 'pwa'),
        template_folder=os.path.join(project_root, 'templates'),
        root_path=project_root,
    )

    # Optional WhiteNoise for static files
    pwa_dir = os.path.join(project_root, 'pwa')
    try:
        from whitenoise import WhiteNoise
        flask_app.wsgi_app = WhiteNoise(flask_app.wsgi_app, root=pwa_dir)
    except ImportError:
        print("WARNING: WhiteNoise not found. Static files may not be served correctly.")

    # --- Database configuration ---
    basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    db_path = os.path.join(basedir, 'products.db')
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL', f'sqlite:///{db_path}'
    )
    flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    # SECRET_KEY must be stable across restarts — random fallback invalidates all sessions
    secret = os.environ.get('SECRET_KEY')
    if not secret:
        secret = 'prism-dev-key-change-in-production-' + os.environ.get('DATABASE_URL', 'local')[:32]
        print("WARNING: SECRET_KEY not set — using deterministic fallback. Set SECRET_KEY env var in production!")
    flask_app.config['SECRET_KEY'] = secret

    # --- Session cookie config (persist login across deploys) ---
    from datetime import timedelta
    flask_app.config['SESSION_COOKIE_SECURE'] = True          # HTTPS only
    flask_app.config['SESSION_COOKIE_HTTPONLY'] = True         # No JS access
    flask_app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'       # CSRF protection
    flask_app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)  # 30-day sessions
    flask_app.config['SESSION_COOKIE_NAME'] = 'vantage_session'

    # Fix Render's postgres:// URL
    if flask_app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
        flask_app.config['SQLALCHEMY_DATABASE_URI'] = flask_app.config[
            'SQLALCHEMY_DATABASE_URI'
        ].replace('postgres://', 'postgresql://', 1)

    # Connection pool — NullPool for Render (fresh connections every time)
    flask_app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'poolclass': NullPool,
    }

    # --- Initialize extensions ---
    db.init_app(flask_app)

    # --- Register blueprints ---
    from app.routes.auth import auth_bp
    from app.routes.products import products_bp
    from app.routes.analytics import analytics_bp
    from app.routes.scan import scan_bp
    from app.routes.admin import admin_bp
    from app.routes.ai import ai_bp
    from app.routes.extern import extern_bp
    from app.routes.payments import payments_bp
    from app.routes.views import views_bp
    from app.routes.ebay import ebay_bp

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(products_bp)
    flask_app.register_blueprint(analytics_bp)
    flask_app.register_blueprint(scan_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(ai_bp)
    flask_app.register_blueprint(extern_bp)
    flask_app.register_blueprint(payments_bp)
    flask_app.register_blueprint(views_bp)
    flask_app.register_blueprint(ebay_bp)

    # --- Serve /static/ from project static/ folder (CSS/JS for Jinja2 templates) ---
    import flask
    static_dir = os.path.join(project_root, 'static')

    @flask_app.route('/static/<path:filename>')
    def vantage_static(filename):
        return flask.send_from_directory(static_dir, filename)

    # --- Jinja2 template filters ---
    @flask_app.template_filter('format_number')
    def format_number_filter(value):
        if not value:
            return '0'
        value = int(value)
        if value >= 1_000_000:
            return f'{value / 1_000_000:.1f}M'
        if value >= 1_000:
            return f'{value / 1_000:.1f}K'
        return str(value)

    @flask_app.template_filter('format_currency')
    def format_currency_filter(value):
        try:
            v = float(value or 0)
            if v >= 1_000_000:
                return f'{v / 1_000_000:.2f}M'
            if v >= 1_000:
                return f'{v / 1_000:.1f}K'
            return f'{v:,.2f}'
        except Exception:
            return str(value)

    # --- Ensure data/ directory exists (for scraper cookies, etc.) ---
    os.makedirs(os.path.join(project_root, 'data'), exist_ok=True)

    # --- Database init ---
    with flask_app.app_context():
        from app import models  # noqa: F401 — ensure models registered
        try:
            db.create_all()
        except Exception as e:
            print(f"Error during DB init: {e}")

        # Auto-add missing columns to existing tables (safe for Postgres)
        _auto_migrate(flask_app, db)

    # --- Response caching headers for read-heavy API endpoints ---
    @flask_app.after_request
    def add_cache_headers(response):
        path = request.path
        if path == '/api/products' and request.method == 'GET':
            response.headers['Cache-Control'] = 'private, max-age=60'
        elif path == '/api/stats' and request.method == 'GET':
            response.headers['Cache-Control'] = 'private, max-age=30'
        return response

    # --- Campaign banner context processor (inject into every template) ---
    @flask_app.context_processor
    def inject_campaign_banner():
        try:
            from app.models import CampaignBanner
            from datetime import datetime as dt, timedelta as td
            now_est = dt.utcnow() - td(hours=5)
            # Get highest-priority active campaign that's either live or upcoming (EST)
            campaign = CampaignBanner.query.filter(
                CampaignBanner.is_active == True,
                db.or_(
                    CampaignBanner.ends_at.is_(None),
                    CampaignBanner.ends_at > now_est
                )
            ).order_by(CampaignBanner.priority.desc()).first()
            return {'active_campaign': campaign}
        except Exception:
            return {'active_campaign': None}

    # --- Start background scheduler ---
    from app.services.scheduler import init_scheduler
    init_scheduler(flask_app)

    return flask_app


# ---------------------------------------------------------------------------
# Create the module-level app instance.
# This enables backward-compatible imports:
#   from app import app, db, Product, User, ApiKey
# Used by price_research.py and discord_bot.py.
# ---------------------------------------------------------------------------
app = create_app()

# --- Load PriceBlade module (after app is assigned for backward-compat imports) ---
with app.app_context():
    try:
        import price_research  # noqa: F401, E402
        print("PriceBlade module loaded")
    except Exception as e:
        print(f"PriceBlade module not loaded: {e}")

# Re-export models at package level for backward compat
from app.models import (  # noqa: E402, F401
    Product,
    User,
    ActivityLog,
    SystemConfig,
    SiteConfig,
    BlacklistedBrand,
    CreatorList,
    WatchedBrand,
    ApiKey,
    ScanJob,
    Subscription,
    ProductVideo,
    Brand,
    TapProduct,
    TapList,
    EbayWatchlistItem,
    EbaySearchHistory,
    ProductView,
    CampaignBanner,
    CouponCode,
    CouponRedemption,
    ScannedBrand,
    BrandProduct,
    BrandScanJob,
)

# Re-export helper functions for backward compat (used by discord_bot.py, price_research.py, etc.)
from app.routes.products import (  # noqa: E402, F401
    save_or_update_product,
    enrich_product_data,
    parse_cover_url,
    get_cached_image_urls,
    is_brand_blacklisted,
    fetch_product_details_echotik,
    extract_metadata_from_echotik,
    fetch_seller_name,
)
from app.routes.auth import (  # noqa: E402, F401
    get_config_value,
    set_config_value,
    get_current_user,
    login_required,
    admin_required,
    subscription_required,
    log_activity,
    is_maintenance_mode,
    set_maintenance_mode,
    generate_watermark,
)
