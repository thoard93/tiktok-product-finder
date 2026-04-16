"""
Vantage — Auth Blueprint
All authentication routes, helpers, config helpers, and maintenance endpoints.

Exports used by other blueprints:
    get_current_user, login_required, admin_required, log_activity,
    get_config_value, set_config_value, is_maintenance_mode, set_maintenance_mode,
    generate_watermark,
    DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI, DISCORD_GUILD_ID,
    DEV_PASSKEY, ADMIN_DISCORD_IDS
"""

import os
import json
import hashlib
import requests
from datetime import datetime
from functools import wraps
from flask import (
    Blueprint, jsonify, request, session, redirect,
    send_from_directory, current_app,
)
from app import db
from app.models import User, ActivityLog, SystemConfig, SiteConfig, Subscription

# =============================================================================
# BLUEPRINT
# =============================================================================

auth_bp = Blueprint('auth', __name__)

# =============================================================================
# AUTHENTICATION CONFIG (constants)
# =============================================================================

# Discord OAuth Settings (set these in Render environment variables)
DISCORD_CLIENT_ID = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI = os.environ.get('DISCORD_REDIRECT_URI', 'https://thoardburgersauce.com/auth/discord/callback')
DISCORD_GUILD_ID = os.environ.get('DISCORD_GUILD_ID', '')  # Your Discord server ID
DISCORD_GUILD_ID_AA = os.environ.get('DISCORD_GUILD_ID_AA', '')  # Affiliate Automated server
DISCORD_GUILD_ID_3 = os.environ.get('DISCORD_GUILD_ID_3', '')  # Third server

# All allowed guild IDs (filter out empty strings)
ALLOWED_GUILD_IDS = [gid for gid in [DISCORD_GUILD_ID, DISCORD_GUILD_ID_AA, DISCORD_GUILD_ID_3] if gid]

# Developer passkey (set in Render environment variables)
DEV_PASSKEY = os.environ.get('DEV_PASSKEY', 'change-this-passkey-123')

# Admin Discord user IDs (comma-separated, whitespace-stripped)
ADMIN_DISCORD_IDS = [x.strip() for x in os.environ.get('ADMIN_DISCORD_IDS', '').split(',') if x.strip()]

# =============================================================================
# AUTHENTICATION HELPERS
# =============================================================================

def get_current_user():
    """Get the current logged-in user or None. Enforces admin whitelist."""
    if 'user_id' not in session:
        return None
    user = User.query.get(session['user_id'])
    if user and user.discord_id and str(user.discord_id) in ADMIN_DISCORD_IDS:
        if not user.is_admin:
            user.is_admin = True
            db.session.commit()
    return user

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required', 'redirect': '/login'}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        user = User.query.get(session['user_id'])
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin privileges required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def subscription_required(f):
    """Require an active subscription. Admins bypass."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required', 'redirect': '/login'}), 401
        user = User.query.get(session['user_id'])
        if not user:
            return jsonify({'error': 'Authentication required', 'redirect': '/login'}), 401
        # Admins always bypass subscription check
        if user.is_admin:
            return f(*args, **kwargs)
        # Check for active subscription
        sub = Subscription.query.filter_by(user_id=user.id, status='active').first()
        if not sub:
            return jsonify({
                'error': 'Active subscription required',
                'redirect': '/subscribe',
                'subscription_required': True,
            }), 403
        return f(*args, **kwargs)
    return decorated_function


def log_activity(user_id, action, details=None):
    """Log user activity to DB"""
    try:
        if isinstance(details, dict):
            details = json.dumps(details, default=str)
        log = ActivityLog(
            user_id=user_id,
            action=action,
            details=str(details)[:500] if details else None,
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        print(f"Log Error: {e}")

# =============================================================================
# CONFIG HELPERS
# =============================================================================

def get_config_value(key, default=None):
    """Get config from DB, fallback to environment or default"""
    try:
        # We need an app context if this is called outside of a request
        config = SystemConfig.query.get(key)
        if config and config.value:
            return config.value
    except Exception as e:
        # Fallback if table doesn't exist yet or other DB error
        pass
    return os.environ.get(key, default)

def set_config_value(key, value, description=None):
    """Set config in DB"""
    config = SystemConfig.query.get(key)
    if not config:
        config = SystemConfig(key=key)
    config.value = value
    if description:
        config.description = description
    db.session.add(config)
    db.session.commit()

def _ensure_site_config_table():
    """Create site_config table if it doesn't exist"""
    try:
        db.session.execute(db.text('''
            CREATE TABLE IF NOT EXISTS site_config (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        '''))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[Maintenance] Table check error: {e}", flush=True)

def is_maintenance_mode():
    """Check if site is in maintenance mode"""
    try:
        _ensure_site_config_table()
        config = SiteConfig.query.get('MAINTENANCE_MODE')
        return config and config.value == 'true'
    except:
        return False

def set_maintenance_mode(enabled: bool):
    """Enable or disable maintenance mode"""
    try:
        _ensure_site_config_table()
        config = SiteConfig.query.get('MAINTENANCE_MODE')
        if not config:
            config = SiteConfig(key='MAINTENANCE_MODE')
        config.value = 'true' if enabled else 'false'
        config.updated_at = datetime.utcnow()
        db.session.add(config)
        db.session.commit()
        print(f"[Maintenance] Mode set to: {'ENABLED' if enabled else 'DISABLED'}", flush=True)
        return True
    except Exception as e:
        db.session.rollback()
        print(f"[Maintenance] Error setting mode: {e}", flush=True)
        return False

# =============================================================================
# WATERMARK HELPER
# =============================================================================

def generate_watermark(user):
    """Generate a unique watermark for exports"""
    if not user:
        return "UNKNOWN"
    # Create a hash that can be traced back to user but isn't obvious
    data = f"{user.id}-{user.discord_username}-{datetime.utcnow().strftime('%Y%m%d')}"
    hash_val = hashlib.md5(data.encode()).hexdigest()[:8].upper()
    return f"BH-{hash_val}"

# =============================================================================
# MAINTENANCE MODE MIDDLEWARE
# =============================================================================

@auth_bp.before_app_request
def check_maintenance_mode():
    """Block all access when in maintenance mode, except for maintenance endpoints"""
    # These paths are always allowed
    allowed_paths = [
        '/maintenance',
        '/pwa/maintenance.html',
        '/api/maintenance/status',
        '/api/maintenance/resume',
        '/pwa/css/',
        '/pwa/js/',
        '/favicon.ico',
        '/logo.png'
    ]

    # Check if path starts with any allowed prefix
    for allowed in allowed_paths:
        if request.path.startswith(allowed):
            return None

    # Check if maintenance mode is enabled
    try:
        if is_maintenance_mode():
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Site in maintenance mode', 'maintenance': True}), 503
            return redirect('/maintenance')
    except Exception as e:
        # If error checking, allow through (don't break the site)
        pass

    return None

# =============================================================================
# AUTHENTICATION ROUTES
# =============================================================================

@auth_bp.route('/legacy-login')
def login_page_legacy():
    """Legacy login — /login is now handled by views_bp."""
    return send_from_directory(current_app.static_folder, 'login.html')

@auth_bp.route('/terms')
def terms_page():
    """Show Terms of Service"""
    return send_from_directory(current_app.static_folder, 'terms.html')

@auth_bp.route('/privacy')
def privacy_page():
    """Show Privacy Policy"""
    return send_from_directory(current_app.static_folder, 'privacy.html')

@auth_bp.route('/cookies')
def cookies_page():
    """Show Cookie Policy"""
    return send_from_directory(current_app.static_folder, 'cookies.html')

# =============================================================================
# MAINTENANCE ROUTES
# =============================================================================

@auth_bp.route('/maintenance')
def maintenance_page():
    """Show Maintenance Mode page"""
    return send_from_directory(current_app.static_folder, 'maintenance.html')

@auth_bp.route('/api/maintenance/status')
def maintenance_status():
    """Check if site is in maintenance mode"""
    return jsonify({'maintenance': is_maintenance_mode()})

@auth_bp.route('/api/maintenance/enable', methods=['POST'])
@login_required
@admin_required
def enable_maintenance():
    """Enable maintenance mode - admin only"""
    user = get_current_user()
    set_maintenance_mode(True)
    log_activity(user.id if user else None, 'maintenance_enabled', {})
    return jsonify({'success': True, 'message': 'Maintenance mode enabled. Site is now blocked.'})

@auth_bp.route('/api/maintenance/disable', methods=['POST'])
@login_required
@admin_required
def disable_maintenance():
    """Disable maintenance mode - admin only"""
    user = get_current_user()
    set_maintenance_mode(False)
    log_activity(user.id if user else None, 'maintenance_disabled', {})
    return jsonify({'success': True, 'message': 'Maintenance mode disabled. Site is now live.'})

@auth_bp.route('/api/maintenance/resume', methods=['POST'])
def resume_from_maintenance():
    """Resume site from maintenance mode with password"""
    data = request.json or {}
    password = data.get('password', '')

    # Check against developer password (fallback to common password if not set)
    correct_password = os.environ.get('DEVELOPER_PASSWORD', 'Batman7193!')

    print(f"[Maintenance Resume] Attempt with password length: {len(password)}", flush=True)

    if password == correct_password:
        set_maintenance_mode(False)
        log_activity(None, 'maintenance_resumed', {'method': 'password'})
        print("[Maintenance Resume] SUCCESS - Site resumed!", flush=True)
        return jsonify({'success': True, 'message': 'Site resumed from maintenance mode'})

    print(f"[Maintenance Resume] FAILED - Password mismatch", flush=True)
    return jsonify({'success': False, 'error': 'Invalid password'}), 401

# =============================================================================
# DISCORD OAUTH ROUTES
# =============================================================================

@auth_bp.route('/auth/discord')
def discord_login():
    """Redirect to Discord OAuth"""
    if not DISCORD_CLIENT_ID:
        return jsonify({'error': 'Discord OAuth not configured'}), 500

    # Discord OAuth URL
    oauth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify%20guilds"
    )
    return redirect(oauth_url)

@auth_bp.route('/auth/discord/callback')
def discord_callback():
    """Handle Discord OAuth callback"""
    code = request.args.get('code')
    if not code:
        return redirect('/login?error=no_code')

    try:
        # Exchange code for token
        token_response = requests.post(
            'https://discord.com/api/oauth2/token',
            data={
                'client_id': DISCORD_CLIENT_ID,
                'client_secret': DISCORD_CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': DISCORD_REDIRECT_URI
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )

        if token_response.status_code != 200:
            return redirect('/login?error=token_failed')

        tokens = token_response.json()
        access_token = tokens.get('access_token')

        # Get user info
        user_response = requests.get(
            'https://discord.com/api/users/@me',
            headers={'Authorization': f'Bearer {access_token}'}
        )

        if user_response.status_code != 200:
            return redirect('/login?error=user_failed')

        discord_user = user_response.json()
        discord_id = discord_user.get('id')
        username = discord_user.get('username')
        avatar = discord_user.get('avatar')

        # Check if user is in ANY of the allowed guilds
        if ALLOWED_GUILD_IDS:
            guilds_response = requests.get(
                'https://discord.com/api/users/@me/guilds',
                headers={'Authorization': f'Bearer {access_token}'}
            )

            if guilds_response.status_code == 200:
                guilds = guilds_response.json()
                user_guild_ids = [str(g.get('id')) for g in guilds]

                is_allowed = any(gid in user_guild_ids for gid in ALLOWED_GUILD_IDS)
                if not is_allowed:
                    return redirect('/login?error=not_in_server')

        # Create or update user
        user = User.query.filter_by(discord_id=discord_id).first()
        if not user:
            user = User(
                discord_id=discord_id,
                discord_username=username,
                discord_avatar=avatar,
                is_admin=str(discord_id) in ADMIN_DISCORD_IDS
            )
            db.session.add(user)
        else:
            user.discord_username = username
            user.discord_avatar = avatar
            user.last_login = datetime.utcnow()

        # Always enforce admin whitelist from env var on every login
        if str(discord_id) in ADMIN_DISCORD_IDS:
            user.is_admin = True

        db.session.commit()

        # Set session (permanent = survives browser close for 30 days)
        session.permanent = True
        session['user_id'] = user.id
        session['discord_username'] = username
        session['is_admin'] = user.is_admin

        log_activity(user.id, 'login', {'method': 'discord'})

        return redirect('/')

    except Exception as e:
        print(f"Discord OAuth error: {e}")
        return redirect(f'/login?error=oauth_error')

@auth_bp.route('/auth/passkey', methods=['POST'])
def passkey_login():
    """Login with developer passkey"""
    data = request.get_json() or {}
    passkey = data.get('passkey', '')

    if not passkey or passkey != DEV_PASSKEY:
        return jsonify({'error': 'Invalid passkey'}), 401

    # Create or get dev user
    user = User.query.filter_by(is_dev_user=True, discord_username='Developer').first()
    if not user:
        user = User(
            discord_id=None,
            discord_username='Developer',
            is_admin=True,
            is_dev_user=True
        )
        db.session.add(user)
        db.session.commit()
    else:
        user.last_login = datetime.utcnow()
        db.session.commit()

    session.permanent = True
    session['user_id'] = user.id
    session['discord_username'] = 'Developer'
    session['is_admin'] = True

    log_activity(user.id, 'login', {'method': 'passkey'})

    return jsonify({'success': True, 'redirect': '/'})

@auth_bp.route('/auth/logout')
def logout():
    """Logout user"""
    user_id = session.get('user_id')
    if user_id:
        log_activity(user_id, 'logout', {})
    session.clear()
    return redirect('/login')

# =============================================================================
# USER API
# =============================================================================

@auth_bp.route('/api/me')
@login_required
def get_me():
    """Get current user info"""
    user = get_current_user()
    return jsonify({
        'user': user.to_dict() if user else None,
        'watermark': generate_watermark(user)
    })

@auth_bp.route('/api/scan-status')
@login_required
def api_scan_status():
    """Get current scan lock status"""
    # Import at call time to avoid circular imports — SCAN_LOCK lives in the
    # monolith (app.py) or will be moved to a scan service module later.
    from app import app as _legacy_app
    try:
        from app.routes.scan import get_scan_status
    except ImportError:
        # Fallback: pull from the legacy monolith module-level dict
        import app as legacy_app_module
        get_scan_status = getattr(legacy_app_module, 'get_scan_status', lambda: {'locked': False})
    status = get_scan_status()
    if status['locked'] and status.get('locked_by'):
        # Get username of who's scanning
        locker = User.query.get(status['locked_by'])
        status['locked_by_username'] = locker.discord_username if locker else 'Unknown'
    return jsonify(status)
