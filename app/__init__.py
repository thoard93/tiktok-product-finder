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

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(products_bp)
    flask_app.register_blueprint(analytics_bp)
    flask_app.register_blueprint(scan_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(ai_bp)
    flask_app.register_blueprint(extern_bp)
    flask_app.register_blueprint(payments_bp)
    flask_app.register_blueprint(views_bp)

    # --- Serve /static/ from project static/ folder (CSS/JS for Jinja2 templates) ---
    import flask
    static_dir = os.path.join(project_root, 'static')

    @flask_app.route('/static/<path:filename>')
    def vantage_static(filename):
        return flask.send_from_directory(static_dir, filename)

    # --- Ensure data/ directory exists (for scraper cookies, etc.) ---
    os.makedirs(os.path.join(project_root, 'data'), exist_ok=True)

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
    Subscription,
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
