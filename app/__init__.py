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


def create_app():
    """Create and configure the Flask application."""
    flask_app = Flask(__name__, static_folder='../pwa')

    # Optional WhiteNoise for static files
    try:
        from whitenoise import WhiteNoise
        flask_app.wsgi_app = WhiteNoise(flask_app.wsgi_app, root='pwa/')
    except ImportError:
        print("WARNING: WhiteNoise not found. Static files may not be served correctly.")

    # --- Database configuration ---
    basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    db_path = os.path.join(basedir, 'products.db')
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL', f'sqlite:///{db_path}'
    )
    flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    flask_app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))

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

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(products_bp)
    flask_app.register_blueprint(analytics_bp)
    flask_app.register_blueprint(scan_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(ai_bp)
    flask_app.register_blueprint(extern_bp)

    # --- Database init ---
    with flask_app.app_context():
        from app import models  # noqa: F401 — ensure models registered
        try:
            db.create_all()
        except Exception as e:
            print(f"Error during DB init: {e}")

    # --- Response caching headers for read-heavy API endpoints ---
    @flask_app.after_request
    def add_cache_headers(response):
        path = request.path
        if path == '/api/products' and request.method == 'GET':
            response.headers['Cache-Control'] = 'private, max-age=60'
        elif path == '/api/stats' and request.method == 'GET':
            response.headers['Cache-Control'] = 'private, max-age=30'
        return response

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
    log_activity,
    is_maintenance_mode,
    set_maintenance_mode,
    generate_watermark,
)


# Copilot stubs (API shut down Feb 4, 2026)
def get_copilot_cookie():
    return None


def fetch_copilot_products(**kwargs):
    return None


def fetch_copilot_trending(**kwargs):
    return None


def sync_copilot_products(**kwargs):
    return 0, 0
