"""
PRISM — Payments Blueprint
PayPal recurring subscription checkout ($19.99/month), coupon codes,
webhook handling, and subscription status.

Environment variables:
    PAYPAL_CLIENT_ID       — PayPal REST API client ID
    PAYPAL_SECRET          — PayPal REST API secret
    PAYPAL_PLAN_ID         — PayPal billing plan ID for the $19.99/month plan
    PAYPAL_WEBHOOK_ID      — PayPal webhook ID for signature verification
    PAYPAL_MODE            — 'sandbox' or 'live' (default: sandbox)
    PRISM_BASE_URL         — Base URL for return/cancel redirects (e.g. https://vantagehq.shop)
"""

import os
import json
import logging
import requests
from datetime import datetime
from flask import Blueprint, jsonify, request, session, redirect
from app import db
from app.models import User, Subscription, SystemConfig
from app.routes.auth import login_required, get_current_user, log_activity

log = logging.getLogger(__name__)

# =============================================================================
# BLUEPRINT
# =============================================================================

payments_bp = Blueprint('payments', __name__)

# =============================================================================
# CONFIG
# =============================================================================

PAYPAL_CLIENT_ID = os.environ.get('PAYPAL_CLIENT_ID', '')
PAYPAL_SECRET = os.environ.get('PAYPAL_SECRET', '')
PAYPAL_PLAN_ID = os.environ.get('PAYPAL_PLAN_ID', '')
PAYPAL_COUPON_PLAN_ID = os.environ.get('PAYPAL_COUPON_PLAN_ID', '')  # Plan with $9.99 trial month
PAYPAL_WEBHOOK_ID = os.environ.get('PAYPAL_WEBHOOK_ID', '')
PAYPAL_MODE = os.environ.get('PAYPAL_MODE', 'sandbox')
PRISM_BASE_URL = os.environ.get('PRISM_BASE_URL', 'https://vantagehq.shop')

PAYPAL_API_BASE = (
    'https://api-m.paypal.com' if PAYPAL_MODE == 'live'
    else 'https://api-m.sandbox.paypal.com'
)

# Discord bot token + multi-guild role config for role management from webhooks
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')

# Guild ID → Vantage Pro Role ID mapping (each server has its own role ID)
# Set env vars: VANTAGE_PRO_ROLE_ID, VANTAGE_PRO_ROLE_ID_AA, VANTAGE_PRO_ROLE_ID_3
DISCORD_GUILD_ROLES = {}
for _gid_key, _rid_key in [
    ('DISCORD_GUILD_ID', 'VANTAGE_PRO_ROLE_ID'),
    ('DISCORD_GUILD_ID_AA', 'VANTAGE_PRO_ROLE_ID_AA'),
    ('DISCORD_GUILD_ID_3', 'VANTAGE_PRO_ROLE_ID_3'),
]:
    _gid = os.environ.get(_gid_key, '')
    _rid = os.environ.get(_rid_key, '')
    if _gid and _rid:
        DISCORD_GUILD_ROLES[_gid] = _rid


def _discord_assign_role(discord_id: str):
    """Assign Vantage Pro role across ALL configured guilds via Discord REST API."""
    if not DISCORD_BOT_TOKEN or not DISCORD_GUILD_ROLES:
        log.warning('[Discord] Role assign skipped — missing DISCORD_BOT_TOKEN or no guild/role pairs configured')
        return
    headers = {'Authorization': f'Bot {DISCORD_BOT_TOKEN}', 'Content-Type': 'application/json'}
    for guild_id, role_id in DISCORD_GUILD_ROLES.items():
        try:
            resp = requests.put(
                f'https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}/roles/{role_id}',
                headers=headers, timeout=10,
            )
            if resp.status_code in (200, 204):
                log.info(f'[Discord] Assigned Vantage Pro to {discord_id} in guild {guild_id}')
            elif resp.status_code == 404:
                pass  # User not in this guild — normal
            else:
                log.warning(f'[Discord] Role assign {guild_id}: {resp.status_code} {resp.text[:100]}')
        except Exception as e:
            log.error(f'[Discord] Role assign error in {guild_id}: {e}')


def _discord_remove_role(discord_id: str):
    """Remove Vantage Pro role from ALL configured guilds."""
    if not DISCORD_BOT_TOKEN or not DISCORD_GUILD_ROLES:
        return
    headers = {'Authorization': f'Bot {DISCORD_BOT_TOKEN}', 'Content-Type': 'application/json'}
    for guild_id, role_id in DISCORD_GUILD_ROLES.items():
        try:
            resp = requests.delete(
                f'https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}/roles/{role_id}',
                headers=headers, timeout=10,
            )
            if resp.status_code in (200, 204):
                log.info(f'[Discord] Removed Vantage Pro from {discord_id} in guild {guild_id}')
        except Exception as e:
            log.error(f'[Discord] Role remove error in {guild_id}: {e}')

# Coupon codes: code -> {discount_percent, description, referral_source}
COUPON_CODES = {
    'LAUNCH50': {'discount_percent': 50, 'description': '50% off first month — Launch special', 'referral_source': 'launch'},
    'FRIEND25': {'discount_percent': 25, 'description': '25% off first month — Friend referral', 'referral_source': 'friend'},
    'DISCORD20': {'discount_percent': 20, 'description': '20% off first month — Discord community', 'referral_source': 'discord'},
}


# =============================================================================
# PAYPAL HELPERS
# =============================================================================

def _get_paypal_token():
    """Get PayPal OAuth2 access token."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise ValueError('PayPal credentials not configured')

    resp = requests.post(
        f'{PAYPAL_API_BASE}/v1/oauth2/token',
        data={'grant_type': 'client_credentials'},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        headers={'Accept': 'application/json'},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()['access_token']


def _paypal_headers():
    """Return auth headers for PayPal API calls."""
    token = _get_paypal_token()
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def _verify_webhook(req):
    """Verify PayPal webhook signature. Returns True if valid."""
    if not PAYPAL_WEBHOOK_ID:
        log.error('[PAYMENTS] PAYPAL_WEBHOOK_ID not set — rejecting webhook for security')
        return False

    try:
        headers = req.headers
        verify_body = {
            'auth_algo': headers.get('PAYPAL-AUTH-ALGO', ''),
            'cert_url': headers.get('PAYPAL-CERT-URL', ''),
            'transmission_id': headers.get('PAYPAL-TRANSMISSION-ID', ''),
            'transmission_sig': headers.get('PAYPAL-TRANSMISSION-SIG', ''),
            'transmission_time': headers.get('PAYPAL-TRANSMISSION-TIME', ''),
            'webhook_id': PAYPAL_WEBHOOK_ID,
            'webhook_event': req.get_json(force=True),
        }
        resp = requests.post(
            f'{PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature',
            json=verify_body,
            headers=_paypal_headers(),
            timeout=15,
        )
        result = resp.json()
        return result.get('verification_status') == 'SUCCESS'
    except Exception as exc:
        log.error('[PAYMENTS] Webhook verification error: %s', exc)
        return False


# =============================================================================
# ROUTES
# =============================================================================

@payments_bp.route('/api/subscription/status', methods=['GET'])
@login_required
def subscription_status():
    """Get current user's subscription status."""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401

    sub = Subscription.query.filter_by(user_id=user.id).first()
    if not sub:
        return jsonify({
            'has_subscription': False,
            'is_active': user.is_admin,  # Admins are always "active"
            'is_admin': user.is_admin,
            'plan': None,
        })

    return jsonify({
        'has_subscription': True,
        'is_active': sub.status == 'active' or user.is_admin,
        'is_admin': user.is_admin,
        **sub.to_dict(),
    })


@payments_bp.route('/api/subscribe', methods=['POST'])
@login_required
def create_subscription():
    """Create a PayPal subscription and return approval URL."""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401

    if not PAYPAL_PLAN_ID:
        return jsonify({'error': 'PayPal plan not configured'}), 500

    # Check for existing active subscription
    existing = Subscription.query.filter_by(user_id=user.id, status='active').first()
    if existing:
        return jsonify({'error': 'Already subscribed', 'subscription': existing.to_dict()}), 400

    data = request.get_json(silent=True) or {}
    coupon_code = data.get('coupon_code', '').strip().upper()
    referral_code = data.get('referral_code', '').strip()

    # Use coupon plan if valid coupon + coupon plan exists
    use_coupon_plan = (
        coupon_code and coupon_code in COUPON_CODES and PAYPAL_COUPON_PLAN_ID
    )
    plan_id = PAYPAL_COUPON_PLAN_ID if use_coupon_plan else PAYPAL_PLAN_ID

    sub_payload = {
        'plan_id': plan_id,
        'application_context': {
            'brand_name': 'Vantage',
            'locale': 'en-US',
            'shipping_preference': 'NO_SHIPPING',
            'user_action': 'SUBSCRIBE_NOW',
            'return_url': f'{PRISM_BASE_URL}/api/subscribe/return',
            'cancel_url': f'{PRISM_BASE_URL}/app/subscribe?cancelled=true',
        },
        'custom_id': str(user.id),
    }

    log.info('[PAYMENTS] Creating subscription: plan=%s, coupon=%s, user=%s',
             plan_id, coupon_code or 'none', user.id)

    try:
        resp = requests.post(
            f'{PAYPAL_API_BASE}/v1/billing/subscriptions',
            json=sub_payload,
            headers=_paypal_headers(),
            timeout=20,
        )
        resp.raise_for_status()
        pp_data = resp.json()
    except requests.HTTPError as exc:
        log.error('[PAYMENTS] PayPal create subscription error: %s — %s', exc, exc.response.text[:500])
        return jsonify({'error': 'PayPal API error', 'detail': str(exc)}), 502
    except Exception as exc:
        log.error('[PAYMENTS] PayPal create subscription error: %s', exc)
        return jsonify({'error': str(exc)}), 500

    pp_sub_id = pp_data.get('id')
    approval_url = None
    for link in pp_data.get('links', []):
        if link.get('rel') == 'approve':
            approval_url = link['href']
            break

    if not approval_url:
        return jsonify({'error': 'No approval URL returned from PayPal'}), 500

    # Create or update local subscription record
    sub = Subscription.query.filter_by(user_id=user.id).first()
    if not sub:
        sub = Subscription(user_id=user.id)
        db.session.add(sub)
    sub.paypal_subscription_id = pp_sub_id
    sub.status = 'pending'
    sub.coupon_code = coupon_code if coupon_code in COUPON_CODES else None
    sub.referral_code = referral_code or (COUPON_CODES.get(coupon_code, {}).get('referral_source'))
    sub.created_at = datetime.utcnow()
    db.session.commit()

    log_activity(user.id, 'subscription_created', {
        'paypal_id': pp_sub_id, 'coupon': coupon_code, 'referral': referral_code,
    })

    return jsonify({'approval_url': approval_url, 'subscription_id': pp_sub_id})


@payments_bp.route('/api/subscribe/return', methods=['GET'])
def subscription_return():
    """PayPal redirects here after user approves the subscription."""
    pp_sub_id = request.args.get('subscription_id')
    if not pp_sub_id:
        return redirect(f'{PRISM_BASE_URL}/app/subscribe?error=missing_id')

    try:
        # Get subscription details from PayPal
        resp = requests.get(
            f'{PAYPAL_API_BASE}/v1/billing/subscriptions/{pp_sub_id}',
            headers=_paypal_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        pp_data = resp.json()
    except Exception as exc:
        log.error('[PAYMENTS] PayPal subscription return error: %s', exc)
        return redirect(f'{PRISM_BASE_URL}/app/subscribe?error=paypal_error')

    pp_status = pp_data.get('status', '').upper()

    # Update local subscription
    sub = Subscription.query.filter_by(paypal_subscription_id=pp_sub_id).first()
    if sub:
        if pp_status in ('ACTIVE', 'APPROVED'):
            sub.status = 'active'
        elif pp_status == 'SUSPENDED':
            sub.status = 'past_due'
        else:
            sub.status = 'inactive'

        billing_info = pp_data.get('billing_info', {})
        next_billing = billing_info.get('next_billing_time')
        if next_billing:
            try:
                sub.next_billing_date = datetime.fromisoformat(next_billing.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                pass

        db.session.commit()
        log_activity(sub.user_id, 'subscription_activated', {'paypal_status': pp_status})

    return redirect(f'{PRISM_BASE_URL}/app/dashboard?subscribed=true')


@payments_bp.route('/api/subscribe/webhook', methods=['POST'])
def subscription_webhook():
    """Handle PayPal subscription webhook events."""
    # Verify signature
    if not _verify_webhook(request):
        log.warning('[PAYMENTS] Webhook signature verification failed')
        return jsonify({'error': 'Invalid signature'}), 401

    event = request.get_json(force=True)
    event_type = event.get('event_type', '')
    resource = event.get('resource', {})
    pp_sub_id = resource.get('id') or resource.get('billing_agreement_id')

    log.info('[PAYMENTS] Webhook: %s for %s', event_type, pp_sub_id)

    if not pp_sub_id:
        return jsonify({'status': 'ignored', 'reason': 'no subscription ID'}), 200

    sub = Subscription.query.filter_by(paypal_subscription_id=pp_sub_id).first()
    if not sub:
        log.warning('[PAYMENTS] Webhook for unknown subscription: %s', pp_sub_id)
        return jsonify({'status': 'ignored', 'reason': 'unknown subscription'}), 200

    # Process event types
    if event_type == 'BILLING.SUBSCRIPTION.ACTIVATED':
        sub.status = 'active'
    elif event_type in ('BILLING.SUBSCRIPTION.CANCELLED', 'BILLING.SUBSCRIPTION.EXPIRED'):
        sub.status = 'cancelled'
        sub.cancelled_at = datetime.utcnow()
    elif event_type == 'BILLING.SUBSCRIPTION.SUSPENDED':
        sub.status = 'past_due'
    elif event_type == 'PAYMENT.SALE.COMPLETED':
        sub.status = 'active'  # Confirm active on successful payment
        # Update next billing date
        billing_info = resource.get('billing_info', {})
        next_billing = billing_info.get('next_billing_time')
        if next_billing:
            try:
                sub.next_billing_date = datetime.fromisoformat(next_billing.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                pass

    try:
        db.session.commit()
    except Exception:
        log.exception('[PAYMENTS] Webhook DB commit failed')
        db.session.rollback()

    # ── Discord role management ──
    # Assign/remove "Vantage Pro" role based on subscription status
    try:
        user = User.query.get(sub.user_id)
        if user and user.discord_id:
            if sub.status == 'active':
                _discord_assign_role(user.discord_id)
                log.info(f'[PAYMENTS] Queued Vantage Pro role assign for {user.discord_username}')
            elif sub.status in ('cancelled', 'past_due') and not user.is_admin:
                _discord_remove_role(user.discord_id)
                log.info(f'[PAYMENTS] Queued Vantage Pro role removal for {user.discord_username}')
    except Exception as e:
        log.warning(f'[PAYMENTS] Discord role update failed: {e}')

    return jsonify({'status': 'processed', 'event_type': event_type}), 200


@payments_bp.route('/api/subscribe/cancel', methods=['POST'])
@login_required
def cancel_subscription():
    """Cancel user's PayPal subscription."""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401

    sub = Subscription.query.filter_by(user_id=user.id, status='active').first()
    if not sub:
        return jsonify({'error': 'No active subscription found'}), 404

    # Cancel on PayPal
    if sub.paypal_subscription_id:
        try:
            resp = requests.post(
                f'{PAYPAL_API_BASE}/v1/billing/subscriptions/{sub.paypal_subscription_id}/cancel',
                json={'reason': 'User cancelled via PRISM dashboard'},
                headers=_paypal_headers(),
                timeout=15,
            )
            if resp.status_code not in (200, 204):
                log.warning('[PAYMENTS] PayPal cancel returned %s: %s', resp.status_code, resp.text[:200])
        except Exception as exc:
            log.error('[PAYMENTS] PayPal cancel error: %s', exc)

    sub.status = 'cancelled'
    sub.cancelled_at = datetime.utcnow()
    db.session.commit()

    # Remove Discord role (unless admin)
    if user.discord_id and not user.is_admin:
        try:
            _discord_remove_role(user.discord_id)
        except Exception:
            pass

    log_activity(user.id, 'subscription_cancelled', {'paypal_id': sub.paypal_subscription_id})

    return jsonify({'success': True, 'message': 'Subscription cancelled'})


@payments_bp.route('/api/subscribe/coupon', methods=['POST'])
@login_required
def validate_coupon():
    """Validate a coupon code and return discount info."""
    data = request.get_json(silent=True) or {}
    code = data.get('coupon_code', '').strip().upper()

    if not code:
        return jsonify({'valid': False, 'error': 'No coupon code provided'}), 400

    # Check built-in codes first
    if code in COUPON_CODES:
        coupon = COUPON_CODES[code]
        return jsonify({
            'valid': True,
            'coupon_code': code,
            'discount_percent': coupon['discount_percent'],
            'description': coupon['description'],
            'discounted_price': round(19.99 * (1 - coupon['discount_percent'] / 100), 2),
        })

    # Check DB for dynamic coupons
    db_coupon = SystemConfig.query.filter_by(key=f'coupon_{code}').first()
    if db_coupon:
        try:
            coupon_data = json.loads(db_coupon.value)
            return jsonify({
                'valid': True,
                'coupon_code': code,
                'discount_percent': coupon_data.get('discount_percent', 0),
                'description': coupon_data.get('description', 'Discount'),
                'discounted_price': round(19.99 * (1 - coupon_data.get('discount_percent', 0) / 100), 2),
            })
        except (json.JSONDecodeError, TypeError):
            pass

    return jsonify({'valid': False, 'error': 'Invalid coupon code'}), 404


@payments_bp.route('/subscribe')
def subscribe_page():
    """Serve the subscription landing page."""
    from flask import send_from_directory, current_app
    return send_from_directory('pwa', 'subscribe.html')
