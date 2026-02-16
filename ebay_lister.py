"""
eBay Auto-Lister — Backend Module
AI-powered eBay listing generator with multi-team support.
Uses Grok 4.1 Fast-Reasoning (xAI) for image analysis & listing generation.
"""

import os
import re
import json
import hashlib
import base64
import logging
import secrets
from datetime import datetime, timedelta
from functools import wraps

import requests as http_requests
from flask import (
    Blueprint, request, jsonify, session, redirect,
    send_from_directory, make_response
)
from flask_sqlalchemy import SQLAlchemy

# ─── Logging ─────────────────────────────────────────────────────────────────
log = logging.getLogger('EbayLister')
logging.basicConfig(level=logging.INFO)

# ─── Import shared db + app from main app ────────────────────────────────────
# These are imported at module load time; app.py must be imported first.
from app import app, db

# ─── Ensure SECRET_KEY is set (required for sessions + remember-me tokens) ───
if not app.config.get('SECRET_KEY'):
    app.config['SECRET_KEY'] = os.environ.get(
        'SECRET_KEY',
        hashlib.sha256(os.environ.get('DATABASE_URL', 'ebay-quicklist-dev-key').encode()).hexdigest()
    )

# ─── Configuration ───────────────────────────────────────────────────────────
XAI_API_KEY = os.environ.get('XAI_API_KEY', '')
XAI_API_URL = 'https://api.x.ai/v1/chat/completions'
XAI_MODEL = 'grok-4-1-fast-reasoning'

EBAY_SANDBOX_API = 'https://api.sandbox.ebay.com'
EBAY_PRODUCTION_API = 'https://api.ebay.com'
EBAY_SANDBOX_AUTH = 'https://auth.sandbox.ebay.com'
EBAY_PRODUCTION_AUTH = 'https://auth.ebay.com'

# ─── Password Hashing (simple, no bcrypt dependency) ────────────────────────
def hash_password(password):
    """Hash password with SHA-256 + salt."""
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{hashed}"

def verify_password(stored, password):
    """Verify password against stored hash."""
    try:
        salt, hashed = stored.split(':')
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == hashed
    except Exception:
        return False


# =============================================================================
# DATABASE MODELS
# =============================================================================

class EbayTeam(db.Model):
    """Team/account group (e.g., Thoard, Reol)"""
    __tablename__ = 'ebay_teams'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    ship_from_zip = db.Column(db.String(10), default='')
    ship_from_city = db.Column(db.String(100), default='')
    ship_from_state = db.Column(db.String(50), default='')
    # eBay API credentials (per team)
    ebay_app_id = db.Column(db.Text, default='')        # Client ID
    ebay_cert_id = db.Column(db.Text, default='')        # Client Secret
    ebay_dev_id = db.Column(db.Text, default='')         # Dev ID (optional)
    ebay_oauth_token = db.Column(db.Text, default='')    # User access token
    ebay_refresh_token = db.Column(db.Text, default='')
    ebay_token_expires = db.Column(db.DateTime)
    ebay_environment = db.Column(db.String(20), default='sandbox')  # sandbox or production
    ebay_location_key = db.Column(db.String(100), default='')  # Inventory location key
    # eBay business policy IDs
    ebay_payment_policy_id = db.Column(db.String(50), default='')
    ebay_return_policy_id = db.Column(db.String(50), default='')
    ebay_fulfillment_policy_id = db.Column(db.String(50), default='')
    # Automation settings
    auto_offer_enabled = db.Column(db.Boolean, default=True)
    auto_offer_days_7 = db.Column(db.Integer, default=10)   # % off after 7 days
    auto_offer_days_14 = db.Column(db.Integer, default=15)
    auto_offer_days_30 = db.Column(db.Integer, default=20)
    auto_promote_enabled = db.Column(db.Boolean, default=True)
    auto_promote_after_days = db.Column(db.Integer, default=14)
    auto_promote_rate = db.Column(db.Float, default=3.0)     # % ad rate
    auto_relist_enabled = db.Column(db.Boolean, default=True)
    # Pricing defaults
    default_undercut_pct = db.Column(db.Integer, default=30)  # % below comps
    min_price_floor = db.Column(db.Float, default=5.0)
    default_condition = db.Column(db.String(50), default='NEW')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship('EbayUser', backref='team', lazy=True)
    listings = db.relationship('EbayListing', backref='team', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'ship_from_zip': self.ship_from_zip,
            'ship_from_city': self.ship_from_city,
            'ship_from_state': self.ship_from_state,
            'ebay_connected': bool(self.ebay_oauth_token),
            'ebay_environment': self.ebay_environment,
            'ebay_app_id': (self.ebay_app_id[:8] + '...') if self.ebay_app_id and len(self.ebay_app_id) > 8 else (self.ebay_app_id or ''),
            'ebay_payment_policy_id': self.ebay_payment_policy_id or '',
            'ebay_return_policy_id': self.ebay_return_policy_id or '',
            'ebay_fulfillment_policy_id': self.ebay_fulfillment_policy_id or '',
            'ebay_location_key': self.ebay_location_key or '',
            'auto_offer_enabled': self.auto_offer_enabled,
            'auto_offer_days_7': self.auto_offer_days_7,
            'auto_offer_days_14': self.auto_offer_days_14,
            'auto_offer_days_30': self.auto_offer_days_30,
            'auto_promote_enabled': self.auto_promote_enabled,
            'auto_promote_after_days': self.auto_promote_after_days,
            'auto_promote_rate': self.auto_promote_rate,
            'auto_relist_enabled': self.auto_relist_enabled,
            'default_undercut_pct': self.default_undercut_pct,
            'min_price_floor': self.min_price_floor,
            'default_condition': self.default_condition,
        }


class EbayUser(db.Model):
    """Individual user account for the eBay lister"""
    __tablename__ = 'ebay_users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(100), default='')
    team_id = db.Column(db.Integer, db.ForeignKey('ebay_teams.id'), nullable=False)
    is_owner = db.Column(db.Boolean, default=False)  # Team owner (can edit settings)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'display_name': self.display_name,
            'team_id': self.team_id,
            'team_name': self.team.name if self.team else '',
            'is_owner': self.is_owner,
        }


class EbayListing(db.Model):
    """Product listing (draft or posted to eBay)"""
    __tablename__ = 'ebay_listings'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('ebay_teams.id'), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('ebay_users.id'))

    # Listing content
    title = db.Column(db.String(80), default='')
    description = db.Column(db.Text, default='')
    category_id = db.Column(db.String(20), default='')
    category_name = db.Column(db.String(200), default='')
    condition = db.Column(db.String(50), default='NEW')
    price = db.Column(db.Float, default=0)
    quantity = db.Column(db.Integer, default=1)
    cost_price = db.Column(db.Float, default=0)  # What you paid (usually $0 for samples)

    # Shipping
    weight_lbs = db.Column(db.Float, default=0)
    weight_oz = db.Column(db.Float, default=0)
    length_in = db.Column(db.Float, default=0)
    width_in = db.Column(db.Float, default=0)
    height_in = db.Column(db.Float, default=0)
    shipping_type = db.Column(db.String(20), default='CALCULATED')  # CALCULATED or FLAT_RATE
    shipping_cost = db.Column(db.Float, default=0)  # For flat rate

    # Images (JSON array of base64 or URLs)
    images_json = db.Column(db.Text, default='[]')

    # eBay API references
    sku = db.Column(db.String(100), index=True)
    ebay_listing_id = db.Column(db.String(50), index=True)
    ebay_offer_id = db.Column(db.String(50))
    ebay_item_url = db.Column(db.Text, default='')

    # Status tracking
    status = db.Column(db.String(20), default='draft')  # draft, active, sold, ended, error
    listed_at = db.Column(db.DateTime)
    sold_at = db.Column(db.DateTime)
    sale_price = db.Column(db.Float)
    ebay_fees = db.Column(db.Float)
    shipping_actual = db.Column(db.Float)
    ad_spend = db.Column(db.Float, default=0)
    net_profit = db.Column(db.Float)

    # Pricing research
    comp_avg_price = db.Column(db.Float)
    comp_low_price = db.Column(db.Float)
    comp_source = db.Column(db.String(50))  # ebay_sold, amazon, google

    # Duplicate detection
    image_hash = db.Column(db.String(64), index=True)
    title_hash = db.Column(db.String(64), index=True)

    # Promotion
    is_promoted = db.Column(db.Boolean, default=False)
    promote_rate = db.Column(db.Float, default=0)
    offer_sent = db.Column(db.Boolean, default=False)
    offer_discount_pct = db.Column(db.Float, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = db.relationship('EbayUser', backref='listings')

    def to_dict(self):
        images = []
        try:
            images = json.loads(self.images_json) if self.images_json else []
        except:
            pass

        return {
            'id': self.id,
            'team_id': self.team_id,
            'created_by': self.created_by,
            'creator_name': self.creator.display_name if self.creator else '',
            'title': self.title,
            'description': self.description,
            'category_id': self.category_id,
            'category_name': self.category_name,
            'condition': self.condition,
            'price': self.price,
            'quantity': self.quantity,
            'cost_price': self.cost_price,
            'weight_lbs': self.weight_lbs,
            'weight_oz': self.weight_oz,
            'length_in': self.length_in,
            'width_in': self.width_in,
            'height_in': self.height_in,
            'shipping_type': self.shipping_type,
            'shipping_cost': self.shipping_cost,
            'images': images,
            'image_count': len(images),
            'sku': self.sku,
            'ebay_listing_id': self.ebay_listing_id,
            'ebay_item_url': self.ebay_item_url,
            'status': self.status,
            'listed_at': self.listed_at.isoformat() if self.listed_at else None,
            'sold_at': self.sold_at.isoformat() if self.sold_at else None,
            'sale_price': self.sale_price,
            'ebay_fees': self.ebay_fees,
            'shipping_actual': self.shipping_actual,
            'ad_spend': self.ad_spend,
            'net_profit': self.net_profit,
            'comp_avg_price': self.comp_avg_price,
            'comp_low_price': self.comp_low_price,
            'is_promoted': self.is_promoted,
            'offer_sent': self.offer_sent,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    @property
    def estimated_profit(self):
        """Calculate estimated profit: price - eBay fees - est shipping."""
        if not self.price:
            return 0
        ebay_fee = (self.price * 0.1325) + 0.30  # 13.25% + $0.30
        est_shipping = self.shipping_cost if self.shipping_type == 'FLAT_RATE' else 10.0
        return round(self.price - self.cost_price - ebay_fee - est_shipping, 2)


# =============================================================================
# CREATE TABLES
# =============================================================================
with app.app_context():
    db.create_all()
    # Seed default teams if they don't exist
    if not EbayTeam.query.filter_by(name='Thoard').first():
        t1 = EbayTeam(
            name='Thoard',
            ship_from_zip='30253',
            ship_from_city='McDonough',
            ship_from_state='GA',
        )
        t2 = EbayTeam(
            name='Reol',
            ship_from_zip='30303',
            ship_from_city='Atlanta',
            ship_from_state='GA',
        )
        db.session.add_all([t1, t2])
        db.session.commit()
        log.info("Seeded default teams: Thoard (30253), Reol (30303)")


# =============================================================================
# AUTH HELPERS
# =============================================================================

def ebay_login_required(f):
    """Decorator to require eBay auth (separate from main app auth)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user_id = session.get('ebay_user_id')
        if not user_id:
            # Check remember-me cookie
            token = request.cookies.get('ebay_remember')
            if token:
                user = EbayUser.query.filter_by(id=_decode_remember_token(token)).first()
                if user:
                    session['ebay_user_id'] = user.id
                    session['ebay_team_id'] = user.team_id
                    return f(*args, **kwargs)
            return jsonify({'error': 'Not authenticated', 'redirect': '/ebay/login'}), 401
        return f(*args, **kwargs)
    return decorated


def _make_remember_token(user_id):
    """Create a signed remember-me token."""
    from itsdangerous import URLSafeSerializer
    s = URLSafeSerializer(app.config['SECRET_KEY'])
    return s.dumps({'uid': user_id})


def _decode_remember_token(token):
    """Decode a remember-me token. Returns user_id or None."""
    try:
        from itsdangerous import URLSafeSerializer
        s = URLSafeSerializer(app.config['SECRET_KEY'])
        data = s.loads(token)
        return data.get('uid')
    except Exception:
        return None


def get_current_ebay_user():
    """Get the current logged-in eBay user."""
    uid = session.get('ebay_user_id')
    if uid:
        return EbayUser.query.get(uid)
    return None


def get_current_team():
    """Get the current team."""
    tid = session.get('ebay_team_id')
    if tid:
        return EbayTeam.query.get(tid)
    return None


# =============================================================================
# AUTH ROUTES
# =============================================================================

@app.route('/ebay/api/register', methods=['POST'])
def ebay_register():
    """Register a new eBay lister user."""
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    display_name = data.get('display_name', '').strip()
    team_name = data.get('team_name', '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    if EbayUser.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 400

    team = EbayTeam.query.filter_by(name=team_name).first()
    if not team:
        return jsonify({'error': f'Team "{team_name}" not found'}), 400

    user = EbayUser(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name or email.split('@')[0],
        team_id=team.id,
        is_owner=(EbayUser.query.filter_by(team_id=team.id).count() == 0),
    )
    db.session.add(user)
    db.session.commit()

    session['ebay_user_id'] = user.id
    session['ebay_team_id'] = user.team_id
    log.info(f"New eBay user registered: {email} -> team {team_name}")

    resp = jsonify({'success': True, 'user': user.to_dict()})
    resp.set_cookie('ebay_remember', _make_remember_token(user.id),
                    max_age=30*24*3600, httponly=True, samesite='Lax')
    return resp


@app.route('/ebay/api/login', methods=['POST'])
def ebay_login():
    """Login to the eBay lister."""
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    remember = data.get('remember', True)

    user = EbayUser.query.filter_by(email=email).first()
    if not user or not verify_password(user.password_hash, password):
        return jsonify({'error': 'Invalid email or password'}), 401

    user.last_login = datetime.utcnow()
    db.session.commit()

    session['ebay_user_id'] = user.id
    session['ebay_team_id'] = user.team_id

    resp = jsonify({'success': True, 'user': user.to_dict()})
    if remember:
        resp.set_cookie('ebay_remember', _make_remember_token(user.id),
                        max_age=30*24*3600, httponly=True, samesite='Lax')
    return resp


@app.route('/ebay/api/logout', methods=['POST'])
def ebay_logout():
    """Logout from eBay lister."""
    session.pop('ebay_user_id', None)
    session.pop('ebay_team_id', None)
    resp = jsonify({'success': True})
    resp.delete_cookie('ebay_remember')
    return resp


@app.route('/ebay/api/me')
def ebay_me():
    """Get current user info."""
    user_id = session.get('ebay_user_id')
    if not user_id:
        token = request.cookies.get('ebay_remember')
        if token:
            uid = _decode_remember_token(token)
            if uid:
                user = EbayUser.query.get(uid)
                if user:
                    session['ebay_user_id'] = user.id
                    session['ebay_team_id'] = user.team_id
                    return jsonify({'authenticated': True, 'user': user.to_dict()})
        return jsonify({'authenticated': False})

    user = EbayUser.query.get(user_id)
    if not user:
        return jsonify({'authenticated': False})
    return jsonify({'authenticated': True, 'user': user.to_dict()})


# =============================================================================
# TEAM / SETTINGS ROUTES
# =============================================================================

@app.route('/ebay/api/teams')
@ebay_login_required
def ebay_get_teams():
    """Get all teams (for tab display)."""
    teams = EbayTeam.query.all()
    return jsonify({'teams': [t.to_dict() for t in teams]})


@app.route('/ebay/api/team/settings', methods=['GET', 'POST'])
@ebay_login_required
def ebay_team_settings():
    """Get or update team settings."""
    team = get_current_team()
    if not team:
        return jsonify({'error': 'Team not found'}), 404

    if request.method == 'GET':
        return jsonify({'team': team.to_dict()})

    # POST — update settings
    user = get_current_ebay_user()
    if not user or not user.is_owner:
        return jsonify({'error': 'Only team owner can update settings'}), 403

    data = request.json or {}

    # Update shipping
    if 'ship_from_zip' in data:
        team.ship_from_zip = data['ship_from_zip']
    if 'ship_from_city' in data:
        team.ship_from_city = data['ship_from_city']
    if 'ship_from_state' in data:
        team.ship_from_state = data['ship_from_state']

    # Update eBay API keys
    if 'ebay_app_id' in data:
        team.ebay_app_id = data['ebay_app_id']
    if 'ebay_cert_id' in data:
        team.ebay_cert_id = data['ebay_cert_id']
    if 'ebay_dev_id' in data:
        team.ebay_dev_id = data['ebay_dev_id']
    if 'ebay_environment' in data:
        team.ebay_environment = data['ebay_environment']

    # Update automation settings
    for field in ['auto_offer_enabled', 'auto_promote_enabled', 'auto_relist_enabled']:
        if field in data:
            setattr(team, field, bool(data[field]))
    for field in ['auto_offer_days_7', 'auto_offer_days_14', 'auto_offer_days_30',
                  'auto_promote_after_days', 'default_undercut_pct']:
        if field in data:
            setattr(team, field, int(data[field]))
    for field in ['auto_promote_rate', 'min_price_floor']:
        if field in data:
            setattr(team, field, float(data[field]))
    if 'default_condition' in data:
        team.default_condition = data['default_condition']

    # Update eBay policy IDs
    for field in ['ebay_payment_policy_id', 'ebay_return_policy_id',
                  'ebay_fulfillment_policy_id', 'ebay_location_key']:
        if field in data:
            setattr(team, field, data[field])

    db.session.commit()
    return jsonify({'success': True, 'team': team.to_dict()})


@app.route('/ebay/api/team/ebay-auth-url')
@ebay_login_required
def ebay_auth_url():
    """Generate eBay OAuth consent URL for user to authorize."""
    team = get_current_team()
    if not team or not team.ebay_app_id:
        return jsonify({'error': 'Set your eBay App ID in settings first'}), 400

    base = EBAY_SANDBOX_AUTH if team.ebay_environment == 'sandbox' else EBAY_PRODUCTION_AUTH
    # Build consent URL
    redirect_uri = request.host_url.rstrip('/') + '/ebay/api/ebay-callback'
    scope = 'https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.marketing https://api.ebay.com/oauth/api_scope/sell.account https://api.ebay.com/oauth/api_scope/sell.fulfillment https://api.ebay.com/oauth/api_scope/sell.negotiation'

    url = (f"{base}/oauth2/authorize?"
           f"client_id={team.ebay_app_id}&"
           f"response_type=code&"
           f"redirect_uri={redirect_uri}&"
           f"scope={scope}&"
           f"state={team.id}")

    return jsonify({'url': url, 'redirect_uri': redirect_uri})


@app.route('/ebay/api/ebay-callback')
def ebay_callback():
    """eBay OAuth callback — exchange code for token."""
    code = request.args.get('code')
    team_id = request.args.get('state')
    if not code or not team_id:
        return redirect('/ebay/settings?error=auth_failed')

    team = EbayTeam.query.get(int(team_id))
    if not team:
        return redirect('/ebay/settings?error=team_not_found')

    base_api = EBAY_SANDBOX_API if team.ebay_environment == 'sandbox' else EBAY_PRODUCTION_API
    redirect_uri = request.host_url.rstrip('/') + '/ebay/api/ebay-callback'

    try:
        # Exchange auth code for tokens
        credentials = base64.b64encode(
            f"{team.ebay_app_id}:{team.ebay_cert_id}".encode()
        ).decode()

        resp = http_requests.post(
            f"{base_api}/identity/v1/oauth2/token",
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {credentials}',
            },
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': redirect_uri,
            }
        )

        if resp.status_code == 200:
            token_data = resp.json()
            team.ebay_oauth_token = token_data.get('access_token', '')
            team.ebay_refresh_token = token_data.get('refresh_token', '')
            expires_in = token_data.get('expires_in', 7200)
            team.ebay_token_expires = datetime.utcnow() + timedelta(seconds=expires_in)
            db.session.commit()
            log.info(f"eBay OAuth success for team {team.name}")
            return redirect('/ebay/settings?success=ebay_connected')
        else:
            log.error(f"eBay OAuth error: {resp.status_code} {resp.text}")
            return redirect(f'/ebay/settings?error=token_exchange_failed')

    except Exception as e:
        log.error(f"eBay OAuth exception: {e}")
        return redirect(f'/ebay/settings?error={str(e)[:100]}')


def _refresh_ebay_token(team):
    """Refresh eBay OAuth token if expired."""
    if not team.ebay_refresh_token:
        return False
    if team.ebay_token_expires and team.ebay_token_expires > datetime.utcnow():
        return True  # Not expired yet

    base_api = EBAY_SANDBOX_API if team.ebay_environment == 'sandbox' else EBAY_PRODUCTION_API
    credentials = base64.b64encode(
        f"{team.ebay_app_id}:{team.ebay_cert_id}".encode()
    ).decode()

    try:
        resp = http_requests.post(
            f"{base_api}/identity/v1/oauth2/token",
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {credentials}',
            },
            data={
                'grant_type': 'refresh_token',
                'refresh_token': team.ebay_refresh_token,
                'scope': 'https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.marketing https://api.ebay.com/oauth/api_scope/sell.fulfillment',
            }
        )
        if resp.status_code == 200:
            data = resp.json()
            team.ebay_oauth_token = data['access_token']
            team.ebay_token_expires = datetime.utcnow() + timedelta(seconds=data.get('expires_in', 7200))
            db.session.commit()
            return True
    except Exception as e:
        log.error(f"Token refresh failed for {team.name}: {e}")
    return False


# =============================================================================
# AI LISTING GENERATION (Grok 4.1)
# =============================================================================

def _call_grok(messages, max_tokens=2000):
    """Call xAI Grok API."""
    if not XAI_API_KEY:
        raise ValueError("XAI_API_KEY not set. Add it to your Render environment variables.")

    resp = http_requests.post(
        XAI_API_URL,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {XAI_API_KEY}',
        },
        json={
            'model': XAI_MODEL,
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': 0.3,
        },
        timeout=60,
    )

    if resp.status_code != 200:
        raise ValueError(f"Grok API error {resp.status_code}: {resp.text[:300]}")

    return resp.json()['choices'][0]['message']['content']


def generate_listing_from_images(image_data_list, user_notes='', team=None):
    """
    Use Grok 4.1 Vision to analyze product images and generate eBay listing.

    Args:
        image_data_list: List of base64 image strings
        user_notes: Optional notes from user (cost, quantity, details)
        team: EbayTeam for pricing/shipping config

    Returns:
        dict with title, description, category, price, weight, dimensions, etc.
    """
    # Build image content blocks
    image_blocks = []
    for img_b64 in image_data_list[:10]:  # Max 10 images
        # Clean up base64 if it has a data URI prefix
        if ',' in img_b64:
            img_b64 = img_b64.split(',', 1)[1]
        image_blocks.append({
            'type': 'image_url',
            'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}
        })

    undercut_pct = team.default_undercut_pct if team else 30
    condition = team.default_condition if team else 'NEW'

    prompt = f"""You are an eBay listing optimization expert. Analyze the product images and generate a complete eBay listing.

RULES:
- Title: Maximum 80 characters, SEO-optimized with relevant keywords buyers would search for. Include brand name, product name, key features, and condition.
- Description: Professional HTML-formatted description with bullet points. Include features, benefits, and "Brand new condition - TikTok Shop sample". Make it compelling.
- Price: Suggest a competitive price. The product cost is $0 (free sample), so aim {undercut_pct}% below typical retail/eBay prices for fast sales while maximizing profit.
- Category: Suggest the most appropriate eBay category name and ID.
- Condition: {condition}
- Weight: Estimate package weight in ounces (product + packaging).
- Dimensions: Estimate package dimensions in inches (L x W x H).

{f'User notes: {user_notes}' if user_notes else ''}

Respond in this EXACT JSON format (no markdown, no code blocks, just raw JSON):
{{
    "title": "80 char max SEO title",
    "description": "<h3>Product Name</h3><ul><li>Feature 1</li><li>Feature 2</li></ul><p>Brand new condition - TikTok Shop sample, limited stock!</p>",
    "category_name": "Health & Beauty > Skin Care > Facial Cleansers",
    "category_id": "67391",
    "price": 24.99,
    "weight_oz": 12,
    "length_in": 8,
    "width_in": 6,
    "height_in": 4,
    "brand": "Brand Name",
    "product_type": "skincare",
    "keywords": ["keyword1", "keyword2", "keyword3"]
}}"""

    messages = [
        {'role': 'system', 'content': 'You are an eBay listing expert. Always respond with valid JSON only.'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            *image_blocks
        ]}
    ]

    raw_response = _call_grok(messages, max_tokens=2000)

    # Parse JSON from response (strip markdown if present)
    json_text = raw_response.strip()
    if json_text.startswith('```'):
        json_text = re.sub(r'^```\w*\n?', '', json_text)
        json_text = re.sub(r'\n?```$', '', json_text)

    try:
        result = json.loads(json_text)
    except json.JSONDecodeError:
        # Try to extract JSON from mixed text
        match = re.search(r'\{[\s\S]*\}', json_text)
        if match:
            result = json.loads(match.group())
        else:
            raise ValueError(f"Could not parse AI response as JSON: {json_text[:200]}")

    return result


def research_pricing(title, category=''):
    """
    Research competitive pricing using Grok to analyze market data.
    Returns price suggestions.
    """
    prompt = f"""Research the typical eBay selling price for this product:
Title: {title}
Category: {category}

Based on your knowledge of eBay sold listings and typical prices:
1. What is the average sold price on eBay for this exact or very similar product?
2. What is the lowest "Buy It Now" price currently?
3. What price would sell quickly (fast sale)?
4. What price maximizes profit while still selling within 7 days?

Respond in JSON format only:
{{
    "avg_sold_price": 29.99,
    "lowest_current": 24.99,
    "quick_sale_price": 19.99,
    "optimal_price": 22.99,
    "price_reasoning": "Brief explanation"
}}"""

    messages = [
        {'role': 'system', 'content': 'You are an eBay pricing expert. Respond with JSON only.'},
        {'role': 'user', 'content': prompt}
    ]

    raw = _call_grok(messages, max_tokens=500)
    json_text = raw.strip()
    if json_text.startswith('```'):
        json_text = re.sub(r'^```\w*\n?', '', json_text)
        json_text = re.sub(r'\n?```$', '', json_text)

    try:
        return json.loads(json_text)
    except:
        match = re.search(r'\{[\s\S]*\}', json_text)
        if match:
            return json.loads(match.group())
        return {'avg_sold_price': 0, 'optimal_price': 0, 'price_reasoning': 'Could not research pricing'}


# =============================================================================
# LISTING ROUTES
# =============================================================================

@app.route('/ebay/api/generate', methods=['POST'])
@ebay_login_required
def ebay_generate_listing():
    """Generate an eBay listing from uploaded images using AI."""
    try:
        data = request.json or {}
        images = data.get('images', [])  # Base64 encoded images
        notes = data.get('notes', '')

        if not images:
            return jsonify({'error': 'Please upload at least one image'}), 400

        team = get_current_team()
        user = get_current_ebay_user()

        # Generate listing with Grok
        listing_data = generate_listing_from_images(images, notes, team)

        # Research pricing
        pricing = research_pricing(listing_data.get('title', ''), listing_data.get('category_name', ''))

        # Calculate profit estimate
        suggested_price = pricing.get('optimal_price', listing_data.get('price', 0))
        if team and team.min_price_floor and suggested_price < team.min_price_floor:
            suggested_price = team.min_price_floor

        ebay_fee = (suggested_price * 0.1325) + 0.30
        est_shipping = 10.0  # Default USPS Ground estimate
        est_profit = round(suggested_price - ebay_fee - est_shipping, 2)

        # Generate SKU
        sku = f"EBAY-{team.name[:3].upper()}-{secrets.token_hex(4).upper()}"

        # Check for duplicates
        duplicates = []
        if listing_data.get('title'):
            title_hash = hashlib.md5(listing_data['title'].lower().encode()).hexdigest()
            existing = EbayListing.query.filter_by(
                team_id=team.id, title_hash=title_hash
            ).filter(EbayListing.status.in_(['draft', 'active'])).all()
            duplicates = [{'id': e.id, 'title': e.title, 'status': e.status} for e in existing]

        result = {
            'listing': {
                **listing_data,
                'sku': sku,
                'price': suggested_price,
                'condition': team.default_condition if team else 'NEW',
            },
            'pricing': pricing,
            'profit_estimate': {
                'price': suggested_price,
                'ebay_fees': round(ebay_fee, 2),
                'est_shipping': est_shipping,
                'est_profit': est_profit,
                'cost_price': 0,
            },
            'duplicates': duplicates,
            'images': images,
        }

        return jsonify({'success': True, **result})

    except Exception as e:
        log.error(f"Generate listing error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/ebay/api/listings', methods=['GET', 'POST'])
@ebay_login_required
def ebay_listings():
    """Get team listings or create a new one."""
    team = get_current_team()

    if request.method == 'GET':
        status_filter = request.args.get('status', 'all')
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))

        # Allow viewing other team's listings (for tab switching)
        view_team_id = int(request.args.get('team_id', team.id))

        query = EbayListing.query.filter_by(team_id=view_team_id)
        if status_filter != 'all':
            query = query.filter_by(status=status_filter)

        query = query.order_by(EbayListing.created_at.desc())
        paginated = query.paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            'listings': [l.to_dict() for l in paginated.items],
            'total': paginated.total,
            'page': page,
            'pages': paginated.pages,
        })

    # POST — save a new listing (draft or ready to post)
    data = request.json or {}
    user = get_current_ebay_user()

    title = data.get('title', '')
    sku = data.get('sku', f"EBAY-{team.name[:3].upper()}-{secrets.token_hex(4).upper()}")

    listing = EbayListing(
        team_id=team.id,
        created_by=user.id if user else None,
        title=title[:80],
        description=data.get('description', ''),
        category_id=data.get('category_id', ''),
        category_name=data.get('category_name', ''),
        condition=data.get('condition', 'NEW'),
        price=float(data.get('price', 0)),
        quantity=int(data.get('quantity', 1)),
        cost_price=float(data.get('cost_price', 0)),
        weight_lbs=float(data.get('weight_lbs', 0)),
        weight_oz=float(data.get('weight_oz', data.get('weight_oz', 0))),
        length_in=float(data.get('length_in', 0)),
        width_in=float(data.get('width_in', 0)),
        height_in=float(data.get('height_in', 0)),
        shipping_type=data.get('shipping_type', 'CALCULATED'),
        shipping_cost=float(data.get('shipping_cost', 0)),
        images_json=json.dumps(data.get('images', [])),
        sku=sku,
        status=data.get('status', 'draft'),
        comp_avg_price=data.get('comp_avg_price'),
        comp_low_price=data.get('comp_low_price'),
        title_hash=hashlib.md5(title.lower().encode()).hexdigest() if title else None,
    )

    db.session.add(listing)
    db.session.commit()

    return jsonify({'success': True, 'listing': listing.to_dict()})


@app.route('/ebay/api/listings/<int:listing_id>', methods=['GET', 'PUT', 'DELETE'])
@ebay_login_required
def ebay_listing_detail(listing_id):
    """Get, update, or delete a listing."""
    listing = EbayListing.query.get(listing_id)
    if not listing:
        return jsonify({'error': 'Listing not found'}), 404

    if request.method == 'GET':
        return jsonify({'listing': listing.to_dict()})

    if request.method == 'DELETE':
        db.session.delete(listing)
        db.session.commit()
        return jsonify({'success': True})

    # PUT — update
    data = request.json or {}
    for field in ['title', 'description', 'category_id', 'category_name', 'condition',
                  'shipping_type', 'status', 'sku']:
        if field in data:
            if field == 'title':
                setattr(listing, field, data[field][:80])
            else:
                setattr(listing, field, data[field])

    for field in ['price', 'quantity', 'cost_price', 'weight_lbs', 'weight_oz',
                  'length_in', 'width_in', 'height_in', 'shipping_cost']:
        if field in data:
            setattr(listing, field, float(data[field]))

    if 'images' in data:
        listing.images_json = json.dumps(data['images'])

    if listing.title:
        listing.title_hash = hashlib.md5(listing.title.lower().encode()).hexdigest()

    db.session.commit()
    return jsonify({'success': True, 'listing': listing.to_dict()})


@app.route('/ebay/api/listings/<int:listing_id>/post', methods=['POST'])
@ebay_login_required
def ebay_post_listing(listing_id):
    """Post a listing to eBay via API."""
    listing = EbayListing.query.get(listing_id)
    if not listing:
        return jsonify({'error': 'Listing not found'}), 404

    team = EbayTeam.query.get(listing.team_id)
    if not team or not team.ebay_oauth_token:
        return jsonify({'error': 'eBay not connected. Go to Settings to connect your eBay account.'}), 400

    # Refresh token if needed
    if not _refresh_ebay_token(team):
        return jsonify({'error': 'eBay token expired. Please reconnect in Settings.'}), 400

    base_api = EBAY_SANDBOX_API if team.ebay_environment == 'sandbox' else EBAY_PRODUCTION_API
    headers = {
        'Authorization': f'Bearer {team.ebay_oauth_token}',
        'Content-Type': 'application/json',
        'Content-Language': 'en-US',
    }

    try:
        # Step 1: Create/Update Inventory Item
        weight_oz = listing.weight_oz or (listing.weight_lbs * 16)
        inventory_item = {
            'availability': {
                'shipToLocationAvailability': {
                    'quantity': listing.quantity
                }
            },
            'condition': listing.condition,
            'product': {
                'title': listing.title,
                'description': listing.description,
                'aspects': {},
            },
            'packageWeightAndSize': {
                'dimensions': {
                    'height': listing.height_in,
                    'length': listing.length_in,
                    'width': listing.width_in,
                    'unit': 'INCH',
                },
                'weight': {
                    'value': weight_oz,
                    'unit': 'OUNCE',
                },
                'packageType': 'MAILING_BOX',
            }
        }

        resp = http_requests.put(
            f"{base_api}/sell/inventory/v1/inventory_item/{listing.sku}",
            headers=headers,
            json=inventory_item,
        )

        if resp.status_code not in [200, 201, 204]:
            return jsonify({'error': f'Failed to create inventory item: {resp.text[:300]}'}), 400

        # Step 2: Upload images
        images = json.loads(listing.images_json) if listing.images_json else []
        image_urls = []
        for img_b64 in images[:12]:
            if img_b64.startswith('http'):
                image_urls.append(img_b64)
                continue
            # Upload via eBay
            if ',' in img_b64:
                img_b64 = img_b64.split(',', 1)[1]
            img_bytes = base64.b64decode(img_b64)
            img_resp = http_requests.post(
                f"{base_api}/commerce/media/v1_beta/image",
                headers={
                    'Authorization': f'Bearer {team.ebay_oauth_token}',
                    'Content-Type': 'image/jpeg',
                },
                data=img_bytes,
            )
            if img_resp.status_code in [200, 201]:
                img_data = img_resp.json()
                image_urls.append(img_data.get('imageUrl', ''))

        # Step 3: Create Offer
        offer_data = {
            'sku': listing.sku,
            'marketplaceId': 'EBAY_US',
            'format': 'FIXED_PRICE',
            'availableQuantity': listing.quantity,
            'categoryId': listing.category_id or '1',
            'listingDescription': listing.description,
            'listingPolicies': {
                'fulfillmentPolicyId': team.ebay_fulfillment_policy_id,
                'paymentPolicyId': team.ebay_payment_policy_id,
                'returnPolicyId': team.ebay_return_policy_id,
            },
            'pricingSummary': {
                'price': {
                    'currency': 'USD',
                    'value': str(listing.price),
                }
            },
            'merchantLocationKey': team.ebay_location_key or 'default',
        }

        if image_urls:
            offer_data['listingPolicies']['pictureKeys'] = image_urls

        resp = http_requests.post(
            f"{base_api}/sell/inventory/v1/offer",
            headers=headers,
            json=offer_data,
        )

        if resp.status_code not in [200, 201]:
            return jsonify({'error': f'Failed to create offer: {resp.text[:300]}'}), 400

        offer_result = resp.json()
        offer_id = offer_result.get('offerId', '')

        # Step 4: Publish Offer
        resp = http_requests.post(
            f"{base_api}/sell/inventory/v1/offer/{offer_id}/publish",
            headers=headers,
        )

        if resp.status_code not in [200, 201]:
            return jsonify({'error': f'Failed to publish: {resp.text[:300]}'}), 400

        publish_result = resp.json()
        ebay_listing_id = publish_result.get('listingId', '')

        # Update listing record
        listing.status = 'active'
        listing.listed_at = datetime.utcnow()
        listing.ebay_listing_id = ebay_listing_id
        listing.ebay_offer_id = offer_id
        listing.ebay_item_url = f"https://www.ebay.com/itm/{ebay_listing_id}"
        db.session.commit()

        return jsonify({
            'success': True,
            'listing': listing.to_dict(),
            'ebay_url': listing.ebay_item_url,
        })

    except Exception as e:
        log.error(f"Post to eBay error: {e}")
        listing.status = 'error'
        db.session.commit()
        return jsonify({'error': str(e)}), 500


# =============================================================================
# STATS ROUTES
# =============================================================================

@app.route('/ebay/api/stats')
@ebay_login_required
def ebay_stats():
    """Get team stats (revenue, profit, listings)."""
    team = get_current_team()
    view_team_id = int(request.args.get('team_id', team.id))
    period = request.args.get('period', '30')  # days

    cutoff = datetime.utcnow() - timedelta(days=int(period))

    all_listings = EbayListing.query.filter_by(team_id=view_team_id).all()

    # Filter by period for financial stats
    period_listings = [l for l in all_listings if l.created_at and l.created_at > cutoff]

    # Counts use current state (active/drafts are always current)
    active = len([l for l in all_listings if l.status == 'active'])
    drafts = len([l for l in all_listings if l.status == 'draft'])

    # Financial stats filtered by period
    sold_in_period = [l for l in all_listings if l.status == 'sold' and l.sold_at and l.sold_at > cutoff]
    sold = len(sold_in_period)
    total_listed = active + sold

    total_revenue = sum(l.sale_price or 0 for l in sold_in_period)
    total_fees = sum(l.ebay_fees or 0 for l in sold_in_period)
    total_shipping = sum(l.shipping_actual or 0 for l in sold_in_period)
    total_ad_spend = sum(l.ad_spend or 0 for l in sold_in_period)
    total_cost = sum(l.cost_price or 0 for l in sold_in_period)
    total_profit = total_revenue - total_fees - total_shipping - total_ad_spend - total_cost

    # Recent sold (for chart)
    recent_sold = [l.to_dict() for l in sold_in_period]

    return jsonify({
        'team_id': view_team_id,
        'period_days': int(period),
        'summary': {
            'total_listed': total_listed,
            'active': active,
            'sold': sold,
            'drafts': drafts,
            'total_revenue': round(total_revenue, 2),
            'total_fees': round(total_fees, 2),
            'total_shipping': round(total_shipping, 2),
            'total_ad_spend': round(total_ad_spend, 2),
            'total_cost': round(total_cost, 2),
            'total_profit': round(total_profit, 2),
        },
        'recent_sold': recent_sold,
    })


# =============================================================================
# PWA PAGE ROUTES
# =============================================================================

@app.route('/ebay')
@app.route('/ebay/')
def ebay_home():
    return send_from_directory('pwa/ebay', 'dashboard.html')

@app.route('/ebay/login')
def ebay_login_page():
    return send_from_directory('pwa/ebay', 'login.html')

@app.route('/ebay/new')
def ebay_new_listing_page():
    return send_from_directory('pwa/ebay', 'new_listing.html')

@app.route('/ebay/listings')
def ebay_listings_page():
    return send_from_directory('pwa/ebay', 'listings.html')

@app.route('/ebay/stats')
def ebay_stats_page():
    return send_from_directory('pwa/ebay', 'stats.html')

@app.route('/ebay/settings')
def ebay_settings_page():
    return send_from_directory('pwa/ebay', 'settings.html')

@app.route('/ebay/manifest.json')
def ebay_manifest():
    return send_from_directory('pwa/ebay', 'manifest.json')

@app.route('/ebay/sw.js')
def ebay_sw():
    return send_from_directory('pwa/ebay', 'sw.js')

@app.route('/pwa/ebay/<path:filename>')
def ebay_pwa_files(filename):
    return send_from_directory('pwa/ebay', filename)


log.info("eBay Auto-Lister module loaded successfully")
