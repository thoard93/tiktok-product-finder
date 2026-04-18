"""
Vantage — Admin Blueprint
Admin dashboard, config, cleanup, debug, migration, and maintenance routes.
"""

import os
import json
import logging
import time
import traceback
import secrets
import requests

log = logging.getLogger(__name__)
from datetime import datetime, timedelta
from flask import (
    Blueprint, jsonify, request, session,
    send_from_directory, make_response, current_app,
)
from sqlalchemy import func, or_, text, inspect
from requests.auth import HTTPBasicAuth
from app import db, executor
from app.models import Product, User, ActivityLog, ApiKey, ScanJob
from app.routes.auth import (
    login_required, admin_required, get_current_user, log_activity,
    get_config_value, set_config_value,
)

# =============================================================================
# BLUEPRINT
# =============================================================================

admin_bp = Blueprint('admin', __name__)


# =============================================================================
# GLOBAL ADMIN GUARD — every route on this blueprint requires is_admin.
# This is belt-and-suspenders on top of per-route @admin_required decorators,
# because several legacy routes forgot to include the decorator and we don't
# want a new route on this blueprint to ever ship without protection.
# =============================================================================

# Endpoints on admin_bp that are safe to leave open (e.g. user-level logging).
# Add sparingly — everything else gets admin-gated.
_ADMIN_BP_PUBLIC_ENDPOINTS = {
    'admin.api_log_activity',   # regular users log their own activity
}


@admin_bp.before_request
def _require_admin_on_admin_bp():
    from flask import request as _req
    # Skip the OPTIONS preflight so CORS still works
    if _req.method == 'OPTIONS':
        return None
    endpoint = _req.endpoint or ''
    if endpoint in _ADMIN_BP_PUBLIC_ENDPOINTS:
        return None
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401
    user = User.query.get(session.get('user_id'))
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin privileges required'}), 403
    return None

# =============================================================================
# CONFIG (loaded from environment)
# =============================================================================

ECHOTIK_V3_BASE = "https://open.echotik.live/api/v1"
BASE_URL = ECHOTIK_V3_BASE
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')


def get_auth():
    """Get HTTPBasicAuth object for EchoTik API"""
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)


# =============================================================================
# ADMIN CONFIG HELPERS
# =============================================================================

_admin_config_table_created = False


def _ensure_admin_config_table():
    """Create admin_config table if it doesn't exist."""
    global _admin_config_table_created
    if _admin_config_table_created:
        return True

    try:
        # CRITICAL: Rollback any aborted transaction first
        try:
            db.session.rollback()
        except:
            pass

        # Check if table exists first
        check_result = db.session.execute(text(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'admin_config')"
        )).scalar()

        if not check_result:
            print("[Admin Config] Creating admin_config table...", flush=True)
            db.session.execute(text('''
                CREATE TABLE admin_config (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT,
                    description TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            '''))
            db.session.commit()
            print("[Admin Config] Table created successfully", flush=True)
        else:
            print("[Admin Config] Table already exists", flush=True)

        _admin_config_table_created = True
        return True
    except Exception as e:
        db.session.rollback()
        print(f"[Admin Config] Table creation error: {e}", flush=True)
        return False


def get_admin_config(key, default=None):
    """Get admin config value from database."""
    if not _ensure_admin_config_table():
        return default
    try:
        result = db.session.execute(
            text("SELECT value FROM admin_config WHERE key = :key"),
            {"key": key}
        ).fetchone()
        return result[0] if result else default
    except Exception as e:
        print(f"[Admin Config] Get error for {key}: {e}", flush=True)
        return default


def set_admin_config(key, value, description=None):
    """Set admin config value in database."""
    if not _ensure_admin_config_table():
        print(f"[Admin Config] Cannot save {key} - table creation failed", flush=True)
        return False
    try:
        # Use UPSERT pattern
        db.session.execute(text('''
            INSERT INTO admin_config (key, value, description, updated_at)
            VALUES (:key, :value, :desc, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                description = COALESCE(EXCLUDED.description, admin_config.description),
                updated_at = CURRENT_TIMESTAMP
        '''), {"key": key, "value": value, "desc": description})
        db.session.commit()
        print(f"[Admin Config] Saved {key} (length: {len(value) if value else 0})", flush=True)
        return True
    except Exception as e:
        db.session.rollback()
        print(f"[Admin Config] Set error for {key}: {e}", flush=True)
        return False


# =============================================================================
# SCHEMA HELPERS
# =============================================================================

def ensure_db_schema():
    """Manually add missing columns if they don't exist"""
    try:
        columns_to_add = [
            ('product_url', 'VARCHAR(500)'),
            ('is_ad_driven', 'BOOLEAN DEFAULT FALSE'),
            ('video_count', 'INTEGER DEFAULT 0'),
            ('video_7d', 'INTEGER DEFAULT 0'),
        ]

        for col_name, col_type in columns_to_add:
            try:
                db.session.execute(db.text(f'ALTER TABLE products ADD COLUMN {col_name} {col_type}'))
                db.session.commit()
                print(f">> Added column: {col_name}")
            except Exception as e:
                db.session.rollback()
                pass
    except Exception as e:
        print(f"Schema update error: {e}")


def check_and_migrate_db():
    """Add missing columns to existing tables"""
    with current_app.app_context():
        # Wrap in try/except to avoid crash if DB not ready
        try:
            inspector_obj = inspect(db.engine)
            if not inspector_obj: return

            if 'products' in inspector_obj.get_table_names():
                columns = [c['name'] for c in inspector_obj.get_columns('products')]

                # List of columns to potentialy add
                cols_to_check = [
                    ('original_price', 'FLOAT DEFAULT 0'),
                    ('ad_spend', 'FLOAT DEFAULT 0'),
                    ('ad_spend_total', 'FLOAT DEFAULT 0'),
                    ('gmv_growth', 'FLOAT DEFAULT 0'),
                    ('scan_type', "VARCHAR(50) DEFAULT 'brand_hunter'")
                ]

                for col_name, col_def in cols_to_check:
                    if col_name not in columns:
                        print(f">> MIGRATION: Adding '{col_name}' column to products table...")
                        try:
                            # Postgres uses IF NOT EXISTS, SQLite does not support it in ADD COLUMN usually easily but we check first
                            if 'sqlite' in current_app.config['SQLALCHEMY_DATABASE_URI']:
                                db.session.execute(db.text(f'ALTER TABLE products ADD COLUMN {col_name} {col_def.split(" ")[0]}'))
                            else:
                                db.session.execute(db.text(f'ALTER TABLE products ADD COLUMN IF NOT EXISTS {col_name} {col_def}'))
                            db.session.commit()
                            print(f">> MIGRATION: Added {col_name} Success!")
                        except Exception as e:
                            print(f"!! MIGRATION FAILED for {col_name}: {e}")
                            db.session.rollback()
        except:
            pass


def ensure_schema_integrity():
    """Auto-heal schema for SQLite/Postgres to prevent missing column errors"""
    with current_app.app_context():
        try:
            # Simple check if table exists
            inspector_obj = inspect(db.engine)
            if not inspector_obj.has_table('products'):
                return # Create_all handles it

            columns = [c['name'] for c in inspector_obj.get_columns('products')]

            with db.engine.connect() as conn:
                if 'product_status' not in columns:
                    print("[SCHEMA] Adding missing column: product_status")
                    conn.execute(text("ALTER TABLE products ADD COLUMN product_status TEXT DEFAULT 'active'"))

                if 'scan_type' not in columns:
                    print("[SCHEMA] Adding missing column: scan_type")
                    conn.execute(text("ALTER TABLE products ADD COLUMN scan_type TEXT DEFAULT 'copilot'"))

                if 'sales_7d' not in columns:
                     print("[SCHEMA] Adding missing column: sales_7d")
                     conn.execute(text("ALTER TABLE products ADD COLUMN sales_7d INTEGER DEFAULT 0"))

                if 'video_count' not in columns:
                     print("[SCHEMA] Adding missing column: video_count")
                     conn.execute(text("ALTER TABLE products ADD COLUMN video_count INTEGER DEFAULT 0"))

                if 'ad_spend' not in columns:
                     print("[SCHEMA] Adding missing column: ad_spend")
                     conn.execute(text("ALTER TABLE products ADD COLUMN ad_spend FLOAT DEFAULT 0"))

                if 'ad_spend_total' not in columns:
                     print("[SCHEMA] Adding missing column: ad_spend_total")
                     conn.execute(text("ALTER TABLE products ADD COLUMN ad_spend_total FLOAT DEFAULT 0"))

                if 'gmv_growth' not in columns:
                     print("[SCHEMA] Adding missing column: gmv_growth")
                     conn.execute(text("ALTER TABLE products ADD COLUMN gmv_growth FLOAT DEFAULT 0"))

                if 'shop_ads_commission' not in columns:
                     print("[SCHEMA] Adding missing column: shop_ads_commission")
                     conn.execute(text("ALTER TABLE products ADD COLUMN shop_ads_commission FLOAT DEFAULT 0"))

                conn.commit()
            print("[SCHEMA] Integrity Check Complete.")
        except Exception as e:
            # Don't crash app on schema check failure (e.g. invalid permissions)
            print(f"[SCHEMA] Warning: Integrity check skipped: {e}")


# =============================================================================
# GOOGLE SHEETS HELPERS
# =============================================================================

# Store Google Sheets config in environment/database
# Format: { sheet_id, credentials_json, frequency, last_sync }
GOOGLE_SHEETS_CONFIG = {}


def get_google_sheets_config():
    """Get Google Sheets config from environment or memory cache"""
    global GOOGLE_SHEETS_CONFIG
    if not GOOGLE_SHEETS_CONFIG.get('sheet_id'):
        GOOGLE_SHEETS_CONFIG = {
            'sheet_id': os.environ.get('GOOGLE_SHEET_ID', ''),
            'credentials': os.environ.get('GOOGLE_SHEETS_CREDENTIALS', ''),
            'frequency': os.environ.get('GOOGLE_SHEETS_FREQUENCY', '3days'),
            'last_sync': os.environ.get('GOOGLE_SHEETS_LAST_SYNC', ''),
        }
    return GOOGLE_SHEETS_CONFIG


# =============================================================================
# COPILOT STUBS (needed by google sheets sync)
# =============================================================================

def fetch_copilot_trending(**kwargs):
    return None


# =============================================================================
# ADMIN ROUTES
# =============================================================================

@admin_bp.route('/admin')
@login_required
@admin_required
def admin_page():
    """Admin dashboard"""
    resp = make_response(send_from_directory('pwa', 'admin_v4.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@admin_bp.route('/api/admin/users')
@login_required
@admin_required
def admin_users():
    """Get all users"""
    users = User.query.order_by(User.last_login.desc()).all()
    return jsonify({'users': [u.to_dict() for u in users]})


@admin_bp.route('/api/admin/activity')
@login_required
@admin_required
def admin_activity():
    """Get recent activity"""
    limit = request.args.get('limit', 100, type=int)
    logs = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(limit).all()
    return jsonify({'logs': [l.to_dict() for l in logs]})


# =============================================================================
# ADMIN ACTIVITY LOG FEED  — filterable, paginated, categorized
# =============================================================================

# Mapping from raw action strings → display category. Anything not mapped
# falls into 'other'. Kept broad so adding new actions doesn't silently fall
# through the filter UI.
_ACTION_CATEGORY_MAP = {
    # Auth
    'login': 'auth', 'logout': 'auth',
    'maintenance_enabled': 'auth', 'maintenance_disabled': 'auth',
    'maintenance_resumed': 'auth',
    # Subscription lifecycle
    'subscription_created': 'subscription',
    'subscription_activated': 'subscription',
    'subscription_cancelled': 'subscription',
    'subscription_paused': 'subscription',
    'save_offer_accepted': 'subscription',
    # User product/creator interactions
    'favorite_added': 'user', 'favorite_removed': 'user',
    'creator_saved': 'user', 'creator_unsaved': 'user',
    'product_lookup': 'user',
    'onboarding_completed': 'user',
    'add_brand': 'user', 'delete_brand': 'user',
    'sync_brand': 'user',
    'ai_image_generated': 'user',
    # Scheduler + background
    'scheduler_daily_sync_started': 'scheduler',
    'scheduler_daily_sync_complete': 'scheduler',
    'scheduler_product_sync': 'scheduler',
    'scheduler_product_sync_failed': 'scheduler',
    'scheduler_deep_refresh_complete': 'scheduler',
    'scheduler_deep_refresh_failed': 'scheduler',
    'scheduler_brand_sync_complete': 'scheduler',
    'scheduler_brand_sync_failed': 'scheduler',
}


def _categorize_action(action):
    if not action:
        return 'other'
    if action in _ACTION_CATEGORY_MAP:
        return _ACTION_CATEGORY_MAP[action]
    if action.startswith('admin_') or action.startswith('cleanup_') \
       or action.startswith('config_') or action.startswith('echotik_') \
       or action in ('reset_product_status', 'deduplicate_products',
                     'delete_low_videos', 'mark_unavailable'):
        return 'admin'
    if action.startswith('scheduler_'):
        return 'scheduler'
    return 'other'


@admin_bp.route('/api/admin/scheduler/run-now', methods=['POST'])
@login_required
@admin_required
def admin_scheduler_run_now():
    """Admin: trigger the daily EchoTik sync immediately (in a background
    thread). Useful to confirm scheduler logs are wired up correctly
    without waiting for 8 PM EST."""
    from app import executor
    from flask import current_app
    try:
        from app.services.scheduler import daily_sync
        app = current_app._get_current_object()
        executor.submit(daily_sync, app)
        return jsonify({'success': True, 'message': 'Daily sync started in background. Refresh logs in a few seconds.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/scheduler/status')
@login_required
@admin_required
def admin_scheduler_status():
    """Return the last scheduler run + next scheduled run time."""
    from datetime import timedelta as _td
    last = ActivityLog.query.filter(
        ActivityLog.action == 'scheduler_daily_sync_complete'
    ).order_by(ActivityLog.created_at.desc()).first()
    last_started = ActivityLog.query.filter(
        ActivityLog.action == 'scheduler_daily_sync_started'
    ).order_by(ActivityLog.created_at.desc()).first()
    last_failed = ActivityLog.query.filter(
        ActivityLog.action.like('scheduler_%_failed')
    ).order_by(ActivityLog.created_at.desc()).first()

    # Next scheduled run = next 1 AM UTC
    now = datetime.utcnow()
    next_run = now.replace(hour=1, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run = next_run + _td(days=1)

    return jsonify({
        'last_completed_at': last.created_at.isoformat() if last else None,
        'last_started_at': last_started.created_at.isoformat() if last_started else None,
        'last_failed_at': last_failed.created_at.isoformat() if last_failed else None,
        'last_failed_action': last_failed.action if last_failed else None,
        'next_scheduled_at': next_run.isoformat() + 'Z',
    })


@admin_bp.route('/api/admin/logs')
@login_required
@admin_required
def admin_logs_feed():
    """
    Filterable activity log feed for the admin logs page.

    Query params:
      category  — auth|subscription|user|admin|scheduler|other|all (default all)
      action    — exact action string (optional)
      user_id   — filter by user (optional)
      q         — free-text search in action/details (optional)
      since_id  — only return rows with id > since_id (for incremental polling)
      limit     — default 100, max 500
    """
    from datetime import timedelta

    category = (request.args.get('category') or 'all').lower()
    action = (request.args.get('action') or '').strip()
    user_id = request.args.get('user_id', type=int)
    q = (request.args.get('q') or '').strip()
    since_id = request.args.get('since_id', type=int)
    try:
        limit = min(max(int(request.args.get('limit', 100)), 1), 500)
    except Exception:
        limit = 100

    query = ActivityLog.query
    if action:
        query = query.filter(ActivityLog.action == action)
    if user_id:
        query = query.filter(ActivityLog.user_id == user_id)
    if q:
        like = f'%{q}%'
        query = query.filter(
            db.or_(ActivityLog.action.ilike(like), ActivityLog.details.ilike(like))
        )
    if since_id:
        query = query.filter(ActivityLog.id > since_id)

    # Fetch a larger pool so the client-side category filter can still
    # return `limit` rows when most are filtered out. Then compute the
    # summary from the SAME pool (pre-category) so chip counts match
    # what the user can actually see when they click a chip.
    pool = query.order_by(ActivityLog.created_at.desc()).limit(limit * 3).all()

    summary = {}
    for r in pool:
        cat = _categorize_action(r.action)
        summary[cat] = summary.get(cat, 0) + 1
    summary['total'] = len(pool)

    # Apply the category filter to produce the display rows
    if category and category != 'all':
        rows = [r for r in pool if _categorize_action(r.action) == category]
    else:
        rows = pool
    rows = rows[:limit]

    payload = []
    for r in rows:
        d = r.to_dict()
        d['category'] = _categorize_action(r.action)
        payload.append(d)

    return jsonify({'logs': payload, 'summary': summary,
                    'latest_id': (rows[0].id if rows else (since_id or 0))})




@admin_bp.route('/api/admin/kick/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_kick_user(user_id):
    """Remove a user's access"""
    user = User.query.get(user_id)
    if user:
        log_activity(session.get('user_id'), 'admin_kick', {'kicked_user': user.discord_username})
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'error': 'User not found'}), 404


@admin_bp.route('/api/admin/migrate', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_migrate():
    """
    Run database migrations to add new columns.
    Hit this endpoint once after deploying new code.
    """
    results = []

    try:
        # Check if we're using PostgreSQL or SQLite
        is_postgres = 'postgresql' in current_app.config['SQLALCHEMY_DATABASE_URI']

        if is_postgres:
            # PostgreSQL - use ALTER TABLE with IF NOT EXISTS
            migrations = [
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS product_status VARCHAR(50) DEFAULT 'active'",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS status_note VARCHAR(255)",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS has_free_shipping BOOLEAN DEFAULT FALSE",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS last_shown_hot TIMESTAMP",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS shop_ads_commission FLOAT DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS video_count_alltime INTEGER DEFAULT 0",
            ]

            for sql in migrations:
                try:
                    db.session.execute(db.text(sql))
                    results.append(f"Done: {sql[:50]}...")
                except Exception as e:
                    results.append(f"Warn: {sql[:30]}... - {str(e)[:50]}")

            db.session.commit()
        else:
            # SQLite - try to add columns, ignore if they exist
            try:
                db.session.execute(db.text("ALTER TABLE products ADD COLUMN product_status VARCHAR(50) DEFAULT 'active'"))
                results.append("Added product_status column")
            except Exception as e:
                if 'duplicate column' in str(e).lower():
                    results.append("product_status column already exists")
                else:
                    results.append(f"product_status: {str(e)[:50]}")

            try:
                db.session.execute(db.text("ALTER TABLE products ADD COLUMN status_note VARCHAR(255)"))
                results.append("Added status_note column")
            except Exception as e:
                if 'duplicate column' in str(e).lower():
                    results.append("status_note column already exists")
                else:
                    results.append(f"status_note: {str(e)[:50]}")

            db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Migration completed',
            'results': results
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e),
            'results': results
        }), 500


@admin_bp.route('/api/admin/delete-low-videos', methods=['POST'])
@login_required
@admin_required
def admin_delete_low_videos():
    """Delete products with 0-1 total videos (placeholder/junk data from Copilot)."""
    try:
        # Delete products where all-time video count is 0 or 1 (placeholders)
        count = Product.query.filter(
            db.or_(
                db.and_(Product.video_count_alltime != None, Product.video_count_alltime <= 1),
                db.and_(Product.video_count_alltime == None, Product.video_count <= 1)
            )
        ).delete(synchronize_session=False)

        db.session.commit()

        user = get_current_user()
        log_activity(user.id, 'delete_low_videos', {'deleted': count})

        return jsonify({
            'success': True,
            'message': f'Deleted {count} placeholder products with <=1 video',
            'deleted': count
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/create-indexes')
@login_required
@admin_required
def admin_create_indexes():
    """
    Create database indexes for faster queries.
    Run this once after deployment or when adding new indexes.
    """
    results = []

    try:
        # Check if we're using PostgreSQL
        is_postgres = 'postgresql' in current_app.config['SQLALCHEMY_DATABASE_URI']

        if not is_postgres:
            return jsonify({
                'success': False,
                'error': 'This endpoint only works with PostgreSQL'
            }), 400

        # Define indexes to create
        indexes = [
            # Composite indexes for common query patterns
            ("idx_influencer_sales", "products", "influencer_count, sales_7d DESC"),
            ("idx_influencer_commission", "products", "influencer_count, commission_rate DESC"),
            ("idx_status_influencer", "products", "product_status, influencer_count"),
            ("idx_influencer_firstseen", "products", "influencer_count, first_seen DESC"),
            ("idx_influencer_price", "products", "influencer_count, price"),
            ("idx_favorite_sales", "products", "is_favorite, sales_7d DESC"),
            ("idx_firstseen_influencer", "products", "first_seen DESC, influencer_count"),
            # Single column indexes (in case they don't exist)
            ("idx_sales_7d", "products", "sales_7d DESC"),
            ("idx_sales_total", "products", "sales DESC"),
            ("idx_commission_rate", "products", "commission_rate DESC"),
            ("idx_first_seen", "products", "first_seen DESC"),
            ("idx_last_updated", "products", "last_updated DESC"),
            ("idx_product_status", "products", "product_status"),
            ("idx_is_favorite", "products", "is_favorite"),
            ("idx_seller_name", "products", "seller_name"),
            # Text search index for product name (for ILIKE queries)
            ("idx_product_name_lower", "products", "LOWER(product_name) varchar_pattern_ops"),
        ]

        for idx_name, table, columns in indexes:
            try:
                # Use CREATE INDEX IF NOT EXISTS (PostgreSQL 9.5+)
                sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({columns})"
                db.session.execute(db.text(sql))
                results.append(f"Created {idx_name}")
            except Exception as e:
                error_msg = str(e)
                if 'already exists' in error_msg.lower():
                    results.append(f"{idx_name} already exists")
                else:
                    results.append(f"{idx_name}: {error_msg[:60]}")

        db.session.commit()

        # Run ANALYZE to update query planner statistics
        try:
            db.session.execute(db.text("ANALYZE products"))
            results.append("Updated query planner statistics (ANALYZE)")
        except Exception as e:
            results.append(f"ANALYZE: {str(e)[:50]}")

        return jsonify({
            'success': True,
            'message': 'Index creation completed',
            'results': results
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc(),
            'results': results
        }), 500


@admin_bp.route('/api/admin/cleanup_garbage', methods=['POST'])
def cleanup_garbage():
    """Delete invalid/debug products from the database"""
    try:
        deleted_count = 0

        # 1. Delete "Unknown (X keys)" debug entries
        q1 = db.session.query(Product).filter(Product.product_name.like('Unknown (% keys)'))
        c1 = q1.count()
        q1.delete(synchronize_session=False)
        deleted_count += c1

        # 2. Delete generic "Unknown" products (Garbage data from failed scrapes)
        q2 = db.session.query(Product).filter(Product.product_name == 'Unknown')
        c2 = q2.count()
        q2.delete(synchronize_session=False)
        deleted_count += c2

        # 2b. Delete "Unknown%" starting variants if sales are 0 (safeguard)
        q2b = db.session.query(Product).filter(
            Product.product_name.like('Unknown %'),
            Product.sales == 0
        )
        c2b = q2b.count()
        q2b.delete(synchronize_session=False)
        deleted_count += c2b

        # 3. Delete explicit Debug artifacts
        q3 = db.session.query(Product).filter(
            db.or_(
                Product.seller_name.like('Debug%'),
                Product.seller_name.like('Keys%')
            )
        )
        c3 = q3.count()
        q3.delete(synchronize_session=False)
        deleted_count += c3

        # 4. Delete "Dead" Ads (Single video, 0 sales, 0 influencers, scan_type='apify_ad')
        q4 = db.session.query(Product).filter(
            Product.scan_type == 'apify_ad',
            Product.sales == 0,
            Product.influencer_count == 0,
            Product.video_count <= 1,
            Product.is_favorite == False  # Safety: Never delete favorites
        )
        c4 = q4.count()
        q4.delete(synchronize_session=False)
        deleted_count += c4

        db.session.commit()

        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'details': f"Keys: {c1}, Unknowns: {c2}, Debug: {c3}, DeadAds: {c4}"
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/products/nuke', methods=['POST'])
@login_required
@admin_required
def admin_nuke_products():
    """DANGER: Delete ALL products from database"""
    try:
        data = request.get_json() or {}
        keep_favorites = data.get('keep_favorites', False)

        if keep_favorites:
            deleted = Product.query.filter(Product.is_favorite == False).delete()
        else:
            deleted = Product.query.delete()

        db.session.commit()
        log_activity(session.get('user_id'), 'admin_nuke', {'count': deleted, 'kept_favorites': keep_favorites})
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/purge-unenriched', methods=['POST'])
@login_required
def purge_unenriched():
    """Delete products where video_count_alltime == video_count (enrichment never matched)."""
    try:
        # Products where all-time == period OR all-time is NULL means enrichment didn't work
        products_to_delete = Product.query.filter(
            db.or_(
                # Case 1: alltime was set to same as period (no real enrichment)
                db.and_(
                    Product.video_count_alltime != None,
                    Product.video_count != None,
                    Product.video_count_alltime == Product.video_count,
                    Product.video_count > 0
                ),
                # Case 2: alltime is NULL (enrichment never ran or never matched)
                Product.video_count_alltime == None
            )
        )

        count = products_to_delete.count()

        if count == 0:
            return jsonify({'success': True, 'message': 'No unenriched products found. All products have accurate all-time data.'})

        products_to_delete.delete(synchronize_session=False)
        db.session.commit()

        print(f"PURGE UNENRICHED: Removed {count} products with matching period/all-time video counts.")
        return jsonify({
            'success': True,
            'message': f'Purged {count} unenriched products (all-time videos == period videos).'
        })
    except Exception as e:
        db.session.rollback()
        print(f"CRITICAL Error in purge-unenriched: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/purge-low-signal', methods=['POST'])
@login_required
@admin_required
def api_purge_low_signal():
    """Delete products with 0 sales and low videos"""
    try:
        deleted = Product.query.filter(
            Product.sales == 0,
            Product.video_count < 2,
            Product.is_favorite == False
        ).delete()
        db.session.commit()
        return jsonify({'success': True, 'message': f'Purged {deleted} low-quality products.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/stats', methods=['GET'])
@login_required
@admin_required
def admin_stats():
    """Get admin dashboard stats"""
    user_count = User.query.count()
    product_count = Product.query.count()
    return jsonify({
        'users': user_count,
        'products': product_count,
        'status': 'online'
    })


@admin_bp.route('/api/admin/config', methods=['GET'])
@login_required
@admin_required
def get_all_admin_config():
    """Get all admin config values for settings page."""
    if not _ensure_admin_config_table():
        return jsonify({"success": False, "error": "Table creation failed", "settings": []})
    try:
        result = db.session.execute(
            text("SELECT key, value, description, updated_at FROM admin_config")
        ).fetchall()
        settings = [
            {"key": row[0], "value": row[1], "description": row[2], "updated_at": str(row[3])}
            for row in result
        ]
        return jsonify({"success": True, "settings": settings})
    except Exception as e:
        print(f"[Admin Config] GET error: {e}", flush=True)
        return jsonify({"success": False, "error": str(e), "settings": []})


@admin_bp.route('/api/admin/config', methods=['POST'])
@login_required
@admin_required
def save_admin_config():
    """Save admin config value. Expects {key, value, description}."""
    data = request.json or {}
    key = data.get('key', '').strip()
    value = data.get('value', '')
    description = data.get('description', '')

    print(f"[Admin Config] POST received: key='{key}', value_length={len(value) if value else 0}", flush=True)

    if not key:
        print("[Admin Config] POST rejected: no key", flush=True)
        return jsonify({"success": False, "error": "Key is required"}), 400

    success = set_admin_config(key, value, description)

    if success:
        print(f"[Admin Config] POST success: {key}", flush=True)
        return jsonify({"success": True, "message": f"Saved {key}"})
    else:
        print(f"[Admin Config] POST failed: {key}", flush=True)
        return jsonify({"success": False, "error": "Failed to save config"}), 500


@admin_bp.route('/api/admin/config/<key>', methods=['GET'])
@login_required
@admin_required
def admin_get_config(key):
    """Get a config value (masked if secret)"""
    val = get_config_value(key)
    if val and any(x in key.lower() for x in ['key', 'secret', 'token', 'cookie', 'password']):
        return jsonify({'success': True, 'value': '••••••••••••••••'})
    return jsonify({'success': True, 'value': val})


@admin_bp.route('/api/admin/config/<key>', methods=['POST'])
@login_required
@admin_required
def admin_set_config(key):
    """Set a config value"""
    data = request.get_json() or {}
    val = data.get('value', '')
    if not val:
        return jsonify({'success': False, 'error': 'Value is required'}), 400

    set_config_value(key, val, f'System config: {key}')
    log_activity(session.get('user_id'), 'config_update', {'key': key})
    return jsonify({'success': True, 'message': f'Config {key} updated!'})


@admin_bp.route('/api/admin/google-sheets-config', methods=['GET'])
def get_sheets_config():
    """Get Google Sheets configuration"""
    config = get_google_sheets_config()
    return jsonify({
        'sheet_id': config.get('sheet_id', ''),
        'credentials': bool(config.get('credentials')),  # Don't expose actual credentials
        'frequency': config.get('frequency', '3days'),
        'last_sync': config.get('last_sync', ''),
    })


@admin_bp.route('/api/admin/google-sheets-config', methods=['POST'])
def save_sheets_config():
    """Save Google Sheets configuration"""
    global GOOGLE_SHEETS_CONFIG
    try:
        data = request.get_json()

        if data.get('sheet_id'):
            GOOGLE_SHEETS_CONFIG['sheet_id'] = data['sheet_id']
            os.environ['GOOGLE_SHEET_ID'] = data['sheet_id']

        if data.get('credentials'):
            # Validate JSON
            json.loads(data['credentials'])  # Will throw if invalid
            GOOGLE_SHEETS_CONFIG['credentials'] = data['credentials']
            os.environ['GOOGLE_SHEETS_CREDENTIALS'] = data['credentials']

        if data.get('frequency'):
            GOOGLE_SHEETS_CONFIG['frequency'] = data['frequency']
            os.environ['GOOGLE_SHEETS_FREQUENCY'] = data['frequency']

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@admin_bp.route('/api/admin/google-sheets-sync', methods=['POST'])
def sync_to_google_sheets():
    """Sync creator products to Google Sheets"""
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        config = get_google_sheets_config()

        if not config.get('sheet_id'):
            return jsonify({'success': False, 'error': 'No Google Sheet ID configured'})

        if not config.get('credentials'):
            return jsonify({'success': False, 'error': 'No Google credentials configured'})

        data = request.get_json() or {}
        creator_id = data.get('creator_id', '7436686228703265838')  # Default to cakedfinds
        creator_name = data.get('creator_name', 'cakedfinds')

        print(f"[Google Sheets] Starting sync for {creator_name}...")

        # Authenticate with Google Sheets
        creds_dict = json.loads(config['credentials'])
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(credentials)

        # Open the sheet
        sheet = gc.open_by_key(config['sheet_id']).sheet1

        # Get products data (reuse the export logic)
        products_list = []
        all_products = {}
        total_videos = 0
        delay_seconds = 3.0
        max_pages = 100

        for page in range(max_pages):
            try:
                result = fetch_copilot_trending(
                    timeframe='7d',
                    sort_by='revenue',
                    c_timeframe='7d',
                    limit=50,
                    page=page,
                    creator_ids=creator_id
                )

                if not result:
                    break

                videos = result.get('videos', [])
                if not videos:
                    break

                total_videos += len(videos)

                for video in videos:
                    product_id = str(video.get('productId', '')).strip()
                    if not product_id or product_id in all_products:
                        continue

                    all_products[product_id] = {
                        'product_id': product_id,
                        'product_name': video.get('productTitle', ''),
                        'seller_name': video.get('sellerName', ''),
                    }

                time.sleep(delay_seconds)

            except Exception as e:
                print(f"[Google Sheets] Page {page} error: {e}")
                break

        print(f"[Google Sheets] Found {len(all_products)} products, enriching from database...")

        # Enrich from database
        # Get sync date in EST
        from datetime import timezone
        est = timezone(timedelta(hours=-5))
        sync_date_est = datetime.now(est).strftime('%Y-%m-%d %I:%M %p EST')

        for product_id, basic_info in all_products.items():
            shop_pid = f"shop_{product_id}" if not product_id.startswith('shop_') else product_id
            raw_pid = product_id.replace('shop_', '')

            db_product = Product.query.filter_by(product_id=shop_pid).first()
            if not db_product:
                db_product = Product.query.filter_by(product_id=raw_pid).first()

            if db_product:
                products_list.append([
                    product_id,
                    db_product.product_name or basic_info.get('product_name', ''),
                    f"https://shop.tiktok.com/view/product/{product_id}?region=US",
                    db_product.seller_name or basic_info.get('seller_name', ''),
                    round(db_product.price or 0, 2),
                    f"{(db_product.commission_rate or 0) * 100:.1f}%",
                    f"{(db_product.shop_ads_commission or 0) * 100:.1f}%",
                    db_product.video_count_alltime or db_product.video_count or 0,
                    db_product.influencer_count or 0,
                    db_product.sales or 0,
                    db_product.sales_7d or 0,
                    db_product.gmv or 0,
                    db_product.ad_spend or 0,
                    sync_date_est,
                ])
            else:
                products_list.append([
                    product_id,
                    basic_info.get('product_name', ''),
                    f"https://shop.tiktok.com/view/product/{product_id}?region=US",
                    basic_info.get('seller_name', ''),
                    0, '0%', '0%', 0, 0, 0, 0, 0, 0, sync_date_est,
                ])

        # Sort by revenue (column 12, index 11) descending
        products_list.sort(key=lambda x: x[11] if x[11] else 0, reverse=True)

        # Clear sheet and write header + data
        sheet.clear()
        header = ['product_id', 'product_name', 'product_url', 'seller_name', 'price',
                  'commission_rate', 'gmv_max_rate', 'video_count_alltime', 'creator_count',
                  'sales_alltime', 'sales_7d', 'revenue_alltime', 'ad_spend', 'sync_date']

        all_data = [header] + products_list
        sheet.update('A1', all_data)

        # Update last sync time
        from datetime import timezone as tz
        GOOGLE_SHEETS_CONFIG['last_sync'] = datetime.now(tz.utc).isoformat()

        print(f"[Google Sheets] Sync complete! {len(products_list)} products")

        return jsonify({'success': True, 'rows': len(products_list)})

    except Exception as e:
        print(f"[Google Sheets] Sync error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@admin_bp.route('/api/init-db', methods=['POST', 'GET'])
def init_database():
    """Initialize database tables and add any missing columns"""
    try:
        db.create_all()

        # Try to add missing columns to products table
        columns_to_add = [
            ('sales_7d', 'INTEGER DEFAULT 0'),
            ('cached_image_url', 'TEXT'),
            ('image_cached_at', 'TIMESTAMP'),
            ('video_count', 'INTEGER DEFAULT 0'),
            ('video_7d', 'INTEGER DEFAULT 0'),
            ('video_30d', 'INTEGER DEFAULT 0'),
            ('live_count', 'INTEGER DEFAULT 0'),
            ('views_count', 'INTEGER DEFAULT 0'),
            ('product_rating', 'FLOAT DEFAULT 0'),
            ('review_count', 'INTEGER DEFAULT 0'),
            ('is_favorite', 'BOOLEAN DEFAULT FALSE'),
            ('first_seen', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
            ('prev_sales_7d', 'INTEGER DEFAULT 0'),
            ('prev_sales_30d', 'INTEGER DEFAULT 0'),
            ('sales_velocity', 'FLOAT DEFAULT 0'),
            ('status_changed_at', 'TIMESTAMP'),
            ('is_hidden_gem', 'BOOLEAN DEFAULT FALSE'),
            ('product_status', "VARCHAR(50) DEFAULT 'active'"),
            ('ai_image_url', 'TEXT'),
            ('ai_video_url', 'TEXT'),
            ('ai_video_task_id', 'VARCHAR(100)'),
            ('ai_video_status', 'VARCHAR(50)'),
            ('last_alert_sent', 'TIMESTAMP'),
            ('gem_alert_sent', 'TIMESTAMP'),
            ('stock_alert_sent', 'TIMESTAMP'),
            ('is_ad_driven', 'BOOLEAN DEFAULT FALSE'),
            ('product_url', 'VARCHAR(500)'),
            ('ad_spend', 'FLOAT DEFAULT 0'),
            ('ad_spend_total', 'FLOAT DEFAULT 0'),
            ('gmv_growth', 'FLOAT DEFAULT 0'),
            ('scan_type', "VARCHAR(50) DEFAULT 'brand_hunter'"),
        ]

        added = []
        for col_name, col_type in columns_to_add:
            try:
                db.session.execute(db.text(f'ALTER TABLE products ADD COLUMN {col_name} {col_type}'))
                db.session.commit()
                added.append(col_name)
            except Exception as e:
                db.session.rollback()
                # Column probably already exists

        # Create users table if not exists
        try:
            db.session.execute(db.text('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    discord_id VARCHAR(50) UNIQUE,
                    discord_username VARCHAR(100),
                    discord_avatar VARCHAR(255),
                    is_admin BOOLEAN DEFAULT FALSE,
                    is_dev_user BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            '''))
            db.session.commit()
        except:
            db.session.rollback()

        # Create activity_logs table if not exists
        try:
            db.session.execute(db.text('''
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    action VARCHAR(100),
                    details TEXT,
                    ip_address VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            '''))
            db.session.commit()
        except:
            db.session.rollback()

        # Create Indexes for Performance
        try:
            indexes = [
                ('idx_products_sales_7d', 'CREATE INDEX IF NOT EXISTS idx_products_sales_7d ON products (sales_7d DESC)'),
                ('idx_products_video_count', 'CREATE INDEX IF NOT EXISTS idx_products_video_count ON products (video_count)'),
                ('idx_products_free_shipping', 'CREATE INDEX IF NOT EXISTS idx_products_free_shipping ON products (has_free_shipping) WHERE has_free_shipping = TRUE'),
                ('idx_products_status', 'CREATE INDEX IF NOT EXISTS idx_products_status ON products (product_status)'),
                ('idx_products_seller', 'CREATE INDEX IF NOT EXISTS idx_products_seller ON products (seller_id)'),
                ('idx_products_commission', 'CREATE INDEX IF NOT EXISTS idx_products_commission ON products (commission_rate DESC)'),
                ('idx_products_created', 'CREATE INDEX IF NOT EXISTS idx_products_created ON products (created_at DESC)')
            ]

            for name, sql in indexes:
                try:
                    db.session.execute(db.text(sql))
                    db.session.commit()
                except Exception as e:
                    print(f"Index {name} error: {e}")
                    db.session.rollback()
        except Exception as e:
            print(f"Index creation error: {e}")
            db.session.rollback()

        return jsonify({
            'success': True,
            'message': f'Database initialized. Added product columns: {added if added else "none (already exist)"}. Users and activity tables ready.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@admin_bp.route('/api/cleanup', methods=['POST'])
@login_required
@admin_required
def api_cleanup():
    """Run simple DB maintenance"""
    try:
        # Example: Remove duplicates or old logs
        # For now, just a placeholder or vacuum
        db.session.execute(text("VACUUM"))
        db.session.commit()
        return jsonify({'success': True, 'message': 'Database vacuumed.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/cleanup-nonpromtable', methods=['POST', 'GET'])
def cleanup_nonpromtable():
    """
    Remove products that cannot be promoted as affiliate products:
    - "not for sale" in name
    - "live only" in name
    - "display only" in name
    - "coming soon" in name
    - "sample" in name
    """
    try:
        # Count before cleanup
        total_before = Product.query.count()

        # Find non-promotable products
        non_promotable = Product.query.filter(
            db.or_(
                Product.product_name.ilike('%not for sale%'),
                Product.product_name.ilike('%live only%'),
                Product.product_name.ilike('%display only%'),
                Product.product_name.ilike('%coming soon%'),
                Product.product_name.ilike('%sample%not for sale%')
            )
        ).all()

        deleted_count = len(non_promotable)
        deleted_names = [p.product_name[:50] for p in non_promotable[:10]]  # First 10 for preview

        # Delete them
        for p in non_promotable:
            db.session.delete(p)

        db.session.commit()

        total_after = Product.query.count()

        return jsonify({
            'success': True,
            'message': f'Removed {deleted_count} non-promotable products',
            'before': total_before,
            'after': total_after,
            'removed': deleted_count,
            'examples': deleted_names
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/cleanup-zero-videos', methods=['POST', 'GET'])
@login_required
def cleanup_zero_videos():
    """
    Remove products that have 0 total videos.
    These products have no TikTok content and are harder to promote.

    Parameters:
        confirm: Set to 'true' to actually delete. Otherwise just counts (dry run).
    """
    try:
        confirm = request.args.get('confirm', 'false').lower() == 'true'

        # Count products with 0 videos
        total_before = Product.query.count()
        zero_video_count = Product.query.filter(
            db.or_(Product.video_count == 0, Product.video_count.is_(None))
        ).count()

        # Safety check - don't delete if it would remove more than 80% of products
        if zero_video_count > total_before * 0.8:
            return jsonify({
                'success': False,
                'error': f'Safety check failed: Would delete {zero_video_count} of {total_before} products ({zero_video_count*100//total_before}%). This seems too high - aborting.',
                'zero_video_count': zero_video_count,
                'total_products': total_before
            }), 400

        if not confirm:
            # Dry run - just show what would be deleted
            # Get some examples
            examples = Product.query.filter(
                db.or_(Product.video_count == 0, Product.video_count.is_(None))
            ).limit(10).all()

            example_names = [f"{p.product_name[:40]}... (videos: {p.video_count})" for p in examples]

            return jsonify({
                'success': True,
                'dry_run': True,
                'message': f'Found {zero_video_count} products with 0 videos. Call with ?confirm=true to delete.',
                'would_delete': zero_video_count,
                'total_products': total_before,
                'would_remain': total_before - zero_video_count,
                'examples': example_names
            })

        # Actually delete
        deleted = Product.query.filter(
            db.or_(Product.video_count == 0, Product.video_count.is_(None))
        ).delete(synchronize_session=False)

        db.session.commit()

        total_after = Product.query.count()

        return jsonify({
            'success': True,
            'dry_run': False,
            'message': f'Deleted {deleted} products with 0 videos',
            'before': total_before,
            'after': total_after,
            'removed': deleted
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/cleanup/zero-sales', methods=['POST'])
@login_required
@admin_required
def cleanup_zero_sales_v2():
    """Remove products from database that have 0 7D sales"""
    user = get_current_user()
    try:
        # Find products with 0 or NULL 7D sales
        zero_sales_count = Product.query.filter(
            db.or_(
                Product.sales_7d == 0,
                Product.sales_7d == None
            )
        ).count()

        # Delete them
        deleted = Product.query.filter(
            db.or_(
                Product.sales_7d == 0,
                Product.sales_7d == None
            )
        ).delete(synchronize_session=False)

        db.session.commit()

        log_activity(user.id, 'cleanup_zero_sales', {'deleted': deleted})
        return jsonify({
            'status': 'success',
            'deleted': deleted,
            'message': f'Removed {deleted} products with 0 7D sales'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@admin_bp.route('/api/admin/cleanup-zero-stats', methods=['POST'])
@login_required
@admin_required
def cleanup_zero_stats():
    """Delete all products with 0 stats (sales=0, gmv=0, no ad spend). Protects favorites."""
    try:
        # Find products with zero stats that are NOT favorites
        zero_stat_products = Product.query.filter(
            db.and_(
                db.or_(Product.sales == 0, Product.sales == None),
                db.or_(Product.gmv == 0, Product.gmv == None),
                db.or_(Product.ad_spend == 0, Product.ad_spend == None),
                db.or_(Product.is_favorite == False, Product.is_favorite == None)
            )
        ).all()

        count = len(zero_stat_products)

        if count == 0:
            return jsonify({'success': True, 'message': 'No zero-stat products found to delete.', 'deleted': 0})

        # Delete them
        for p in zero_stat_products:
            db.session.delete(p)

        db.session.commit()

        log_activity(session.get('user_id'), 'cleanup_zero_stats', {'deleted': count})

        return jsonify({
            'success': True,
            'message': f'Deleted {count} products with zero stats.',
            'deleted': count
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/cleanup-zero-sales-7d', methods=['POST'])
@login_required
@admin_required
def cleanup_zero_sales():
    """Delete products with 0 7d sales that are NOT favorites"""
    try:
        # Note: Favorite model referenced in original app.py may not exist.
        # Using Product.is_favorite field instead as a safe fallback.
        to_delete = Product.query.filter(
            Product.sales_7d <= 0,
            db.or_(Product.is_favorite == False, Product.is_favorite == None)
        ).all()

        count = len(to_delete)
        for p in to_delete:
            db.session.delete(p)

        db.session.commit()
        log_activity(session.get('user_id'), 'cleanup_zero_sales', {'deleted': count})
        return jsonify({'status': 'success', 'deleted': count, 'message': f'Successfully purged {count} low-performance products.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@admin_bp.route('/api/admin/reset-product-status', methods=['POST'])
@login_required
@admin_required
def reset_product_status():
    """Reset product_status to 'active' for all products that meet quality criteria"""
    user = get_current_user()
    try:
        # Reset status for products with valid data (20+ videos, sales > 0)
        updated = Product.query.filter(
            Product.product_status == 'unavailable',
            Product.video_count >= 20,
            Product.sales_7d > 0
        ).update({'product_status': 'active', 'status_note': None}, synchronize_session=False)

        db.session.commit()

        log_activity(user.id, 'reset_product_status', {'updated': updated})
        return jsonify({
            'status': 'success',
            'updated': updated,
            'message': f'Restored {updated} products to active status'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@admin_bp.route('/api/admin/hot-test')
@login_required
@admin_required
def hot_products_test():
    """Run the EXACT same query as Discord bot's get_hot_products() from web context."""
    try:
        cutoff_date = datetime.utcnow() - timedelta(days=3)

        video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)

        # EXACT same query as discord_bot.py get_hot_products()
        products = Product.query.filter(
            video_count_field >= 30,
            video_count_field <= 200,
            Product.sales_7d >= 20,
            Product.ad_spend >= 50,
            Product.commission_rate > 0,
            db.or_(
                Product.last_shown_hot == None,
                Product.last_shown_hot < cutoff_date
            )
        ).order_by(
            db.func.coalesce(Product.ad_spend, 0).desc(),
            db.func.coalesce(Product.sales_7d, 0).desc(),
            video_count_field.asc()
        ).limit(45).all()

        # Also try raw SQL for comparison
        raw_sql = db.text("""
            SELECT product_id, product_name,
                   COALESCE(video_count_alltime, video_count) as vids,
                   sales_7d, ad_spend, commission_rate, last_shown_hot
            FROM products
            WHERE COALESCE(video_count_alltime, video_count) >= 30
              AND COALESCE(video_count_alltime, video_count) <= 200
              AND sales_7d >= 20
              AND ad_spend >= 50
              AND commission_rate > 0
              AND (last_shown_hot IS NULL OR last_shown_hot < :cutoff)
            ORDER BY COALESCE(ad_spend, 0) DESC
            LIMIT 10
        """)
        raw_results = db.session.execute(raw_sql, {'cutoff': cutoff_date}).fetchall()

        # Also test: call actual get_hot_products from discord_bot
        try:
            from discord_bot import get_hot_products
            bot_results = get_hot_products()
            bot_count = len(bot_results) if bot_results else 0
        except Exception as e:
            bot_results = []
            bot_count = f"ERROR: {str(e)}"

        return jsonify({
            'orm_query_count': len(products),
            'orm_top_5': [{
                'id': p.product_id[:30],
                'name': (p.product_name or '')[:40],
                'vids_alltime': p.video_count_alltime,
                'vids_7d': p.video_count,
                'coalesced_vids': p.video_count_alltime if p.video_count_alltime else p.video_count,
                'sales_7d': p.sales_7d,
                'ad_spend': p.ad_spend,
                'commission': p.commission_rate,
                'last_shown': str(p.last_shown_hot)
            } for p in products[:5]],
            'raw_sql_count': len(raw_results),
            'raw_sql_top_5': [{
                'id': str(r[0])[:30],
                'name': str(r[1] or '')[:40],
                'vids': r[2],
                'sales_7d': r[3],
                'ad_spend': r[4],
                'commission': r[5],
                'last_shown': str(r[6])
            } for r in raw_results[:5]],
            'bot_function_count': bot_count,
            'bot_function_results': bot_results[:3] if isinstance(bot_results, list) else []
        })
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@admin_bp.route('/api/admin/hot-diag')
@login_required
@admin_required
def hot_products_diagnostic():
    """Diagnostic: show how many products pass each hot-product filter individually."""
    try:
        video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)

        total = Product.query.count()
        active = Product.query.filter(Product.product_status == 'active').count()

        # Individual filter counts
        has_videos_any = Product.query.filter(video_count_field > 0).count()
        has_videos_30_200 = Product.query.filter(video_count_field >= 30, video_count_field <= 200).count()
        has_videos_50_120 = Product.query.filter(video_count_field >= 50, video_count_field <= 120).count()
        has_sales_20 = Product.query.filter(Product.sales_7d >= 20).count()
        has_sales_50 = Product.query.filter(Product.sales_7d >= 50).count()
        has_ads_50 = Product.query.filter(Product.ad_spend >= 50).count()
        has_ads_100 = Product.query.filter(Product.ad_spend >= 100).count()
        has_ads_any = Product.query.filter(Product.ad_spend > 0).count()
        has_commission = Product.query.filter(Product.commission_rate > 0).count()

        # last_shown_hot stats
        cutoff = datetime.utcnow() - timedelta(days=3)
        never_shown = Product.query.filter(Product.last_shown_hot == None).count()
        shown_before_cutoff = Product.query.filter(Product.last_shown_hot < cutoff).count()
        shown_recently = Product.query.filter(Product.last_shown_hot >= cutoff).count()

        # Combined: original filters (50-120 videos, 50+ sales, 100+ ads, commission)
        combined_original = Product.query.filter(
            video_count_field >= 50, video_count_field <= 120,
            Product.sales_7d >= 50, Product.ad_spend >= 100,
            Product.commission_rate > 0
        ).count()

        # Combined: loosened filters (30-200, 20+ sales, 50+ ads)
        combined_loose = Product.query.filter(
            video_count_field >= 30, video_count_field <= 200,
            Product.sales_7d >= 20, Product.ad_spend >= 50,
            Product.commission_rate > 0
        ).count()

        # Combined with last_shown_hot
        combined_with_shown = Product.query.filter(
            video_count_field >= 30, video_count_field <= 200,
            Product.sales_7d >= 20, Product.ad_spend >= 50,
            Product.commission_rate > 0,
            db.or_(Product.last_shown_hot == None, Product.last_shown_hot < cutoff)
        ).count()

        # Sample of top products by ad_spend (to see what data looks like)
        top_samples = Product.query.order_by(
            db.func.coalesce(Product.ad_spend, 0).desc()
        ).limit(5).all()
        samples = [{
            'id': p.product_id[:30], 'name': (p.product_name or '')[:40],
            'videos_alltime': p.video_count_alltime, 'videos_7d': p.video_count,
            'sales_7d': p.sales_7d, 'ad_spend': p.ad_spend,
            'commission': p.commission_rate, 'last_shown': str(p.last_shown_hot)
        } for p in top_samples]

        return jsonify({
            'total_products': total,
            'active_products': active,
            'filters': {
                'has_any_videos': has_videos_any,
                'videos_30_200': has_videos_30_200,
                'videos_50_120': has_videos_50_120,
                'sales_7d_gte_20': has_sales_20,
                'sales_7d_gte_50': has_sales_50,
                'ad_spend_gt_0': has_ads_any,
                'ad_spend_gte_50': has_ads_50,
                'ad_spend_gte_100': has_ads_100,
                'commission_gt_0': has_commission,
            },
            'last_shown_hot': {
                'never_shown': never_shown,
                'shown_before_3d_cutoff': shown_before_cutoff,
                'shown_in_last_3d': shown_recently,
            },
            'combined': {
                'original_filters_no_shown': combined_original,
                'loose_filters_no_shown': combined_loose,
                'loose_filters_with_shown': combined_with_shown,
            },
            'top_5_by_ad_spend': samples
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/admin/deduplicate', methods=['POST'])
@login_required
@admin_required
def deduplicate_products():
    """Fast dedup using a single SQL DELETE with window functions.
    Keeps the product with the most recent last_updated per name group.
    Supports dry_run=true to preview."""
    try:
        dry_run = request.args.get('dry_run', 'false').lower() == 'true'

        if dry_run:
            # Count duplicates that WOULD be deleted
            count_sql = db.text("""
                SELECT COUNT(*) FROM (
                    SELECT product_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY lower(trim(product_name))
                               ORDER BY last_updated DESC, COALESCE(sales_7d, 0) DESC
                           ) as rn
                    FROM products
                    WHERE product_name IS NOT NULL AND product_name != ''
                ) ranked
                WHERE rn > 1
            """)
            dupe_count = db.session.execute(count_sql).scalar()

            # Get sample of what would be deleted
            sample_sql = db.text("""
                SELECT product_id, product_name, last_updated FROM (
                    SELECT product_id, product_name, last_updated,
                           ROW_NUMBER() OVER (
                               PARTITION BY lower(trim(product_name))
                               ORDER BY last_updated DESC, COALESCE(sales_7d, 0) DESC
                           ) as rn
                    FROM products
                    WHERE product_name IS NOT NULL AND product_name != ''
                ) ranked
                WHERE rn > 1
                LIMIT 20
            """)
            samples = db.session.execute(sample_sql).fetchall()

            # Count unique product names
            unique_sql = db.text("""
                SELECT COUNT(DISTINCT lower(trim(product_name)))
                FROM products
                WHERE product_name IS NOT NULL AND product_name != ''
            """)
            unique_count = db.session.execute(unique_sql).scalar()

            total_count = Product.query.count()

            return jsonify({
                'success': True,
                'dry_run': True,
                'total_products': total_count,
                'unique_product_names': unique_count,
                'duplicates_to_delete': dupe_count,
                'products_after_dedup': total_count - dupe_count,
                'sample_deletions': [{'id': str(s[0])[:40], 'name': str(s[1] or '')[:60]} for s in samples],
                'message': f'[DRY RUN] Would delete {dupe_count} duplicate products. {total_count} -> {total_count - dupe_count} products.'
            })
        else:
            # Actually delete duplicates in a single fast query
            before_count = Product.query.count()

            delete_sql = db.text("""
                DELETE FROM products
                WHERE product_id IN (
                    SELECT product_id FROM (
                        SELECT product_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY lower(trim(product_name))
                                   ORDER BY last_updated DESC, COALESCE(sales_7d, 0) DESC
                               ) as rn
                        FROM products
                        WHERE product_name IS NOT NULL AND product_name != ''
                    ) ranked
                    WHERE rn > 1
                )
            """)
            result = db.session.execute(delete_sql)
            deleted = result.rowcount
            db.session.commit()

            after_count = Product.query.count()

            log_activity(session.get('user_id'), 'deduplicate_products', {
                'deleted': deleted,
                'before': before_count,
                'after': after_count
            })

            return jsonify({
                'success': True,
                'dry_run': False,
                'deleted': deleted,
                'before_count': before_count,
                'after_count': after_count,
                'message': f'Deleted {deleted} duplicate products. {before_count} -> {after_count} products.'
            })

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500


@admin_bp.route('/api/admin/jobs', methods=['GET'])
@login_required
def admin_list_jobs():
    """Debug: List recent scan jobs"""
    try:
        jobs = ScanJob.query.order_by(ScanJob.created_at.desc()).limit(20).all()
        results = []
        for j in jobs:
            res = None
            if j.result_json:
                try: res = json.loads(j.result_json)
                except: res = j.result_json

            results.append({
                'id': j.id,
                'status': j.status,
                'input': j.input_query,
                'result': res,
                'created_at': j.created_at.isoformat()
            })
        return jsonify({'success': True, 'jobs': results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/create-key', methods=['POST'])
@login_required
def admin_create_key():
    """Admin: Generate a new SaaS API Key"""
    try:
        data = request.get_json() or {}
        credits = int(data.get('credits', 100))

        user = get_current_user()
        new_key_str = secrets.token_hex(16) # 32 chars

        new_key = ApiKey(
            key=new_key_str,
            user_id=user.id,
            credits=credits,
            is_active=True
        )
        db.session.add(new_key)
        db.session.commit()

        return jsonify({
            'success': True,
            'api_key': new_key_str,
            'credits': credits,
            'message': 'Key generated! Save it now, it cannot be retrieved later'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/mark-unavailable/<product_id>')
@login_required
def mark_unavailable(product_id):
    """Mark a product as unavailable (removed or out of stock)"""
    try:
        status = request.args.get('status', 'removed')
        note = request.args.get('note', '')

        # Validate status
        if status not in ['removed', 'out_of_stock', 'active']:
            return jsonify({'success': False, 'error': 'Invalid status'}), 400

        product = Product.query.get(product_id)
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        product.product_status = status
        product.status_note = note
        product.last_updated = datetime.utcnow()
        db.session.commit()

        # Log the activity
        user = get_current_user()
        if user:
            log_activity(user.id, 'mark_unavailable', {
                'product_id': product_id,
                'status': status,
                'note': note
            })

        return jsonify({
            'success': True,
            'message': f'Product marked as {status}',
            'product_id': product_id,
            'status': status
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# DEBUG / SCHEMA ROUTES
# =============================================================================

@admin_bp.route('/api/fix-schema', methods=['GET'])
def manual_fix_schema():
    """Force add columns directly via SQL"""
    try:
        with db.engine.connect() as conn:
            columns = [
                ("ad_spend", "FLOAT DEFAULT 0"),
                ("ad_spend_total", "FLOAT DEFAULT 0"),
                ("scan_type", "VARCHAR(50) DEFAULT 'copilot'"),
                ("gmv_growth", "FLOAT DEFAULT 0"),
                ("product_status", "VARCHAR(50) DEFAULT 'active'"),
                ("status_note", "VARCHAR(255)"),
                ("prev_sales_7d", "INTEGER DEFAULT 0"),
                ("prev_sales_30d", "INTEGER DEFAULT 0"),
                ("sales_velocity", "FLOAT DEFAULT 0"),
                ("is_ad_driven", "BOOLEAN DEFAULT 0"),
                ("original_price", "FLOAT DEFAULT 0"),
                ("cached_image_url", "TEXT"),
                ("image_cached_at", "TIMESTAMP"),
                ("last_shown_hot", "TIMESTAMP"),
                ("has_free_shipping", "BOOLEAN DEFAULT 0"),
                ("is_favorite", "BOOLEAN DEFAULT 0")
            ]

            results = []
            for col_name, col_def in columns:
                try:
                    conn.execute(text(f"ALTER TABLE products ADD COLUMN {col_name} {col_def}"))
                    results.append(f"Added {col_name}")
                except Exception as e:
                    results.append(f"Skipped {col_name} ({str(e)[:50]}...)")

            conn.commit()

        return jsonify({"success": True, "details": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route('/api/debug-products')
def debug_products_dump():
    """Dump last 10 products to verify DB state"""
    try:
        # Force fresh query
        db.session.expire_all()
        products = Product.query.order_by(Product.first_seen.desc()).limit(10).all()
        return jsonify({
            'count': len(products),
            'products': [{
                'id': p.product_id,
                'name': p.product_name,
                'scan_type': p.scan_type,
                'video_count': p.video_count,
                'sales_7d': p.sales_7d,
                'first_seen': str(p.first_seen),
            } for p in products]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/debug', methods=['GET'])
def debug_api():
    """Debug endpoint to test EchoTik connection and see raw response"""
    try:
        response = requests.get(
            f"{BASE_URL}/seller/list",
            params={
                "page_num": 1,
                "page_size": 2,
                "region": "US",
                "seller_sort_field": 2,
                "sort_type": 1
            },
            auth=get_auth(),
            timeout=10
        )
        data = response.json()

        return jsonify({
            'debug_info': {
                'url': f"{BASE_URL}/seller/list",
                'params': {"page_num": 1, "page_size": 2, "region": "US", "seller_sort_field": 2, "sort_type": 1},
                'username_set': bool(ECHOTIK_USERNAME),
                'username_preview': ECHOTIK_USERNAME[:3] + '***' if ECHOTIK_USERNAME else 'NOT SET',
                'password_set': bool(ECHOTIK_PASSWORD)
            },
            'response': {
                'status_code': response.status_code,
                'api_code': data.get('code'),
                'message': data.get('message'),
                'brands_count': len(data.get('data', [])),
                'raw_data': data
            }
        })
    except Exception as e:
        return jsonify({
            'debug_info': {
                'username_set': bool(ECHOTIK_USERNAME),
                'password_set': bool(ECHOTIK_PASSWORD)
            },
            'error': str(e)
        })


@admin_bp.route('/api/debug/recent', methods=['GET'])
def debug_recent_products():
    """Debug: Show last 10 products and their scan_types"""
    try:
        products = Product.query.order_by(Product.first_seen.desc()).limit(10).all()
        return jsonify({
            'success': True,
            'products': [{
                'id': p.product_id,
                'name': p.product_name,
                'scan_type': p.scan_type,
                'first_seen': str(p.first_seen)
            } for p in products]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/debug-cookie', methods=['GET'])
@login_required
@admin_required
def debug_cookie_status():
    """Debug endpoint to check cookie storage and retrieval."""
    # Rollback any aborted transaction first
    try:
        db.session.rollback()
    except:
        pass

    debug_info = {
        "table_creation_attempted": False,
        "table_creation_result": None,
        "admin_config_table_exists": False,
        "cookie_in_admin_config": None,
        "cookie_in_env_var": None,
        "cookie_from_get_copilot_cookie": None,
        "scrapfly_key_set": bool(os.environ.get('SCRAPFLY_API_KEY', '').strip())
    }

    # First, try to ensure table exists
    try:
        debug_info["table_creation_attempted"] = True
        result = _ensure_admin_config_table()
        debug_info["table_creation_result"] = "Success" if result else "Failed"
    except Exception as e:
        debug_info["table_creation_result"] = f"Error: {e}"

    # Check if admin_config table exists
    try:
        db.session.rollback()  # Clear any transaction state
        result = db.session.execute(text(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'admin_config')"
        )).scalar()
        debug_info["admin_config_table_exists"] = result
    except Exception as e:
        debug_info["admin_config_table_exists"] = f"Error: {e}"

    # Check cookie in admin_config
    try:
        db.session.rollback()  # Clear any transaction state
        result = db.session.execute(text(
            "SELECT value FROM admin_config WHERE key = 'TIKTOK_COPILOT_COOKIE'"
        )).fetchone()
        if result and result[0]:
            debug_info["cookie_in_admin_config"] = f"Found (length: {len(result[0])})"
        else:
            debug_info["cookie_in_admin_config"] = "Not found"
    except Exception as e:
        debug_info["cookie_in_admin_config"] = f"Error: {e}"

    # Check cookie in env var
    env_cookie = os.environ.get('TIKTOK_COPILOT_COOKIE', '')
    debug_info["cookie_in_env_var"] = f"Found (length: {len(env_cookie)})" if env_cookie else "Not set"

    # Check what get_copilot_cookie returns and analyze format
    try:
        cookie = None  # get_copilot_cookie() stub returns None
        if cookie:
            debug_info["cookie_from_get_copilot_cookie"] = f"Found (length: {len(cookie)})"
            # Show first 50 chars for format verification (redacted)
            debug_info["cookie_preview"] = cookie[:50] + "..." if len(cookie) > 50 else cookie
            # Check for required __session token
            has_session = "__session=" in cookie
            debug_info["has_session_token"] = has_session
            if not has_session:
                debug_info["cookie_warning"] = "Cookie missing __session= token - may not authenticate properly"
            # Count parsed cookie parts
            cookie_parts = [p.strip() for p in cookie.split(';') if '=' in p]
            debug_info["parsed_cookie_count"] = len(cookie_parts)
            debug_info["cookie_keys"] = [p.split('=')[0].strip() for p in cookie_parts][:10]  # First 10 keys
        else:
            debug_info["cookie_from_get_copilot_cookie"] = "None"
    except Exception as e:
        debug_info["cookie_from_get_copilot_cookie"] = f"Error: {e}"

    return jsonify(debug_info)


@admin_bp.route('/api/test-cookie-save', methods=['GET'])
@login_required
@admin_required
def test_cookie_save():
    """Test the admin_config save/read cycle with a test value."""
    test_key = "TEST_COOKIE_SAVE"
    test_value = f"test_value_{int(time.time())}"

    results = {
        "step1_table_check": None,
        "step2_write_result": None,
        "step3_read_result": None,
        "step4_direct_query": None,
        "step5_all_keys": None,
        "test_passed": False
    }

    # Step 1: Check table exists
    try:
        db.session.rollback()
        table_exists = _ensure_admin_config_table()
        results["step1_table_check"] = "Table ready" if table_exists else "Table creation failed"
    except Exception as e:
        results["step1_table_check"] = f"Error: {e}"
        return jsonify(results)

    # Step 2: Write test value
    try:
        write_success = set_admin_config(test_key, test_value, "Test write")
        results["step2_write_result"] = f"Success - wrote '{test_value}'" if write_success else "Failed"
    except Exception as e:
        results["step2_write_result"] = f"Error: {e}"
        return jsonify(results)

    # Step 3: Read back via function
    read_value = None
    try:
        read_value = get_admin_config(test_key)
        results["step3_read_result"] = f"Read back: '{read_value}'" if read_value else "Got None"
    except Exception as e:
        results["step3_read_result"] = f"Error: {e}"

    # Step 4: Direct SQL query
    try:
        db.session.rollback()
        result = db.session.execute(
            text("SELECT key, value FROM admin_config WHERE key = :key"),
            {"key": test_key}
        ).fetchone()
        if result:
            results["step4_direct_query"] = f"Found: key='{result[0]}', value='{result[1]}'"
        else:
            results["step4_direct_query"] = "Not found in database"
    except Exception as e:
        results["step4_direct_query"] = f"Error: {e}"

    # Step 5: List all keys in admin_config
    try:
        db.session.rollback()
        all_rows = db.session.execute(text("SELECT key, LENGTH(value) as val_len FROM admin_config")).fetchall()
        results["step5_all_keys"] = [{"key": r[0], "value_length": r[1]} for r in all_rows]
    except Exception as e:
        results["step5_all_keys"] = f"Error: {e}"

    # Final verdict
    results["test_passed"] = (read_value == test_value) if read_value else False

    return jsonify(results)


@admin_bp.route('/api/debug/force-refresh-stale')
@login_required
@admin_required
def debug_force_stale():
    """Trigger stale refresh manually"""
    import app_legacy as monolith
    scheduled_stale_refresh = getattr(monolith, 'scheduled_stale_refresh', None)
    if scheduled_stale_refresh:
        executor.submit(scheduled_stale_refresh)
    return jsonify({'success': True, 'message': 'Triggered stale refresh job in background.'})


# =============================================================================
# ECHOTIK BROWSER SCRAPER — Cookie Management & Manual Triggers
# =============================================================================

@admin_bp.route('/api/admin/echotik-cookies', methods=['POST'])
@login_required
@admin_required
def upload_echotik_cookies():
    """
    Upload EchoTik session cookies for the Playwright scraper.

    Expects JSON body: { "cookies": [ { name, value, domain, ... }, ... ] }
    Cookies can be exported from browser DevTools or EditThisCookie extension.
    """
    data = request.get_json()
    if not data or not isinstance(data.get('cookies'), list):
        return jsonify({'error': 'Body must be {"cookies": [...]}'}), 400

    cookies = data['cookies']
    if len(cookies) == 0:
        return jsonify({'error': 'Cookie list is empty'}), 400

    # Validate each cookie has at least name + value
    for i, c in enumerate(cookies):
        if not isinstance(c, dict) or not c.get('name') or not c.get('value'):
            return jsonify({'error': f'Cookie at index {i} missing name or value'}), 400

    try:
        from app.services.echotik_scraper import save_cookies
        result = save_cookies(cookies)
        log_activity(session.get('user_id'), 'echotik_cookies_upload',
                     {'total': result['total'], 'echotik_cookies': result['echotik_cookies']})
        return jsonify({'success': True, **result})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@admin_bp.route('/api/admin/echotik-cookies/status', methods=['GET'])
@login_required
@admin_required
def echotik_cookies_status():
    """Check if EchoTik cookies are configured (does NOT expose cookie values)."""
    try:
        from app.services.echotik_scraper import get_cookie_status
        return jsonify(get_cookie_status())
    except Exception as exc:
        return jsonify({'configured': False, 'error': str(exc)}), 500


@admin_bp.route('/api/admin/echotik-scraper/run', methods=['POST'])
@login_required
@admin_required
def trigger_echotik_scraper():
    """
    Manually trigger a full EchoTik browser scrape.
    Runs in background thread — returns immediately.
    """
    pages = request.get_json(silent=True) or {}
    num_pages = pages.get('pages', 5)

    try:
        from app.services.echotik_scraper import run_scraper_sync
    except ImportError:
        return jsonify({'error': 'echotik_scraper module not available'}), 500

    def _run():
        try:
            result = run_scraper_sync(current_app._get_current_object(), pages=num_pages)
            log.info("[ADMIN] Manual scraper run complete: %s", result)
        except Exception as exc:
            log.error("[ADMIN] Manual scraper run failed: %s", exc)

    executor.submit(_run)
    log_activity(session.get('user_id'), 'echotik_scraper_manual',
                 {'pages': num_pages})
    return jsonify({'success': True, 'message': f'Scraper started ({num_pages} pages) — running in background'})


@admin_bp.route('/api/admin/echotik-scraper/debug', methods=['POST'])
@login_required
@admin_required
def debug_echotik_scraper():
    """
    Debug scrape: capture 1 page of XHR traffic and return raw results
    instead of syncing to DB. Useful for discovering XHR URL patterns
    and response structures.
    """
    try:
        from app.services.echotik_scraper import run_debug_scrape
    except ImportError:
        return jsonify({'error': 'echotik_scraper module not available'}), 500

    try:
        result = run_debug_scrape(current_app._get_current_object(), pages=1)
        return jsonify({'success': True, **result})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@admin_bp.route('/api/log-activity', methods=['POST'])
@login_required
def api_log_activity():
    """Log user activity from frontend"""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.get_json() or {}
    action = data.get('action', 'unknown')
    details = data.get('details', {})

    log_activity(user.id, action, details)
    return jsonify({'success': True})
