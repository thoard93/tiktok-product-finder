"""
External/Developer API Routes Blueprint - SaaS API, Stripe Payments, Developer Portal
Extracted from monolithic app.py
"""

import os
import json
import secrets
import uuid

try:
    import stripe
except ImportError:
    stripe = None

from flask import Blueprint, jsonify, request, session, send_from_directory, url_for
from app import db
from app.models import Product, User, ApiKey, ScanJob
from app.routes.auth import login_required, admin_required, get_current_user, log_activity

extern_bp = Blueprint('extern_bp', __name__)

# =============================================================================
# STRIPE CONFIGURATION
# =============================================================================

if stripe:
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
else:
    endpoint_secret = None


# =============================================================================
# SAAS API ROUTES
# =============================================================================

@extern_bp.route('/api/extern/scan', methods=['POST'])
def extern_scan_start():
    """Start a scan via API Key (Async)"""
    api_key_val = request.headers.get('X-API-KEY')
    if not api_key_val:
        return jsonify({'error': 'Missing X-API-KEY header'}), 401

    key = ApiKey.query.filter_by(key=api_key_val, is_active=True).first()
    if not key:
        return jsonify({'error': 'Invalid API Key'}), 401

    if key.credits < 1:
         return jsonify({'error': 'Insufficient Credits'}), 402

    data = request.get_json() or {}
    query = data.get('query') or data.get('url')
    if not query:
        return jsonify({'error': 'Missing query/url'}), 400

    # Deduct Credit
    key.credits -= 1
    key.total_usage += 1

    # Create Job
    job_id = str(uuid.uuid4())
    job = ScanJob(id=job_id, status='queued', input_query=query, api_key_id=key.id)
    db.session.add(job)
    db.session.commit()

    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': 'queued',
        'credits_remaining': key.credits
    })

@extern_bp.route('/api/extern/jobs/<job_id>', methods=['GET'])
def extern_job_status(job_id):
    """Check job status"""
    api_key_val = request.headers.get('X-API-KEY')
    if not api_key_val:
        return jsonify({'error': 'Missing X-API-KEY header'}), 401

    # We could validate key config here but for speed we just check job existence
    job = ScanJob.query.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    resp = {
        'id': job.id,
        'status': job.status,
        'created_at': job.created_at.isoformat(),
        'result': None
    }

    if job.result_json:
        try:
            resp['result'] = json.loads(job.result_json)
        except:
            resp['result'] = job.result_json

    return jsonify(resp)


# =============================================================================
# USER DEVELOPER ROUTES
# =============================================================================

@extern_bp.route('/developer')
@login_required
def developer_page():
    return send_from_directory('pwa', 'developer_v4.html')

@extern_bp.route('/api/developer/me')
@login_required
def api_dev_me():
    user = get_current_user()
    key = ApiKey.query.filter_by(user_id=user.id, is_active=True).first()
    return jsonify({
        'key': key.key if key else None,
        'credits': key.credits if key else 0.0
    })

@extern_bp.route('/api/developer/keygen', methods=['POST'])
@login_required
def api_dev_keygen():
    try:
        user = get_current_user()
        # Deactivate old keys
        old_keys = ApiKey.query.filter_by(user_id=user.id, is_active=True).all()
        existing_credits = sum([k.credits for k in old_keys])

        for k in old_keys:
            k.is_active = False

        # Bonus for new users (if no credits existed)
        if existing_credits == 0 and not old_keys:
             existing_credits = 5.0 # 5 Free Scans

        new_key_str = secrets.token_hex(16)
        new_key = ApiKey(
            key=new_key_str,
            user_id=user.id,
            credits=existing_credits,
            is_active=True
        )
        db.session.add(new_key)
        db.session.commit()

        return jsonify({'success': True, 'key': new_key_str, 'credits': existing_credits})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# STRIPE PAYMENT ROUTES
# =============================================================================

@extern_bp.route('/api/developer/checkout', methods=['POST'])
@login_required
def api_dev_checkout():
    if not stripe:
        return jsonify({'error': 'Payment system not available (Stripe missing)'}), 503

    try:
        user = get_current_user()
        data = request.get_json()
        amount_cents = data.get('amount', 1500) # Default $15.00
        credits_to_add = data.get('credits', 500)

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{credits_to_add} API Credits',
                    },
                    'unit_amount': amount_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=url_for('extern_bp.developer_page', _external=True) + '?success=true',
            cancel_url=url_for('extern_bp.developer_page', _external=True) + '?canceled=true',
            metadata={
                'user_id': user.id,
                'credits': credits_to_add
            }
        )
        return jsonify({'url': checkout_session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    try:
        user = get_current_user()
        data = request.get_json() or {}
        plan = data.get('plan') # starter, pro, enterprise

        if not stripe.api_key:
            return jsonify({'error': 'Stripe not configured (STRIPE_SECRET_KEY missing)'}), 500

        # Define Products (Hardcoded for simplicity, or use Price IDs)
        pricing = {
            'starter': {'amount': 500, 'credits': 100, 'name': 'Starter Pack (100 Credits)'},
            'pro': {'amount': 2000, 'credits': 500, 'name': 'Pro Pack (500 Credits)'},
            'enterprise': {'amount': 5000, 'credits': 1500, 'name': 'Enterprise Pack (1500 Credits)'}
        }

        selected = pricing.get(plan)
        if not selected:
             return jsonify({'error': 'Invalid plan'}), 400

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': selected['amount'], # in cents
                    'product_data': {
                        'name': selected['name'],
                        'description': 'Credits for TikTokShop Finder API',
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.host_url + 'developer?success=true',
            cancel_url=request.host_url + 'developer?canceled=true',
            client_reference_id=str(user.id),
            metadata={
                'credits_to_add': selected['credits'],
                'user_id': user.id
            }
        )
        return jsonify({'url': checkout_session.url})
    except Exception as e:
         return jsonify({'error': str(e)}), 500

@extern_bp.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe Webhooks to fulfill credits"""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        # Verify Signature if secret is set
        if endpoint_secret:
            event = stripe.Webhook.construct_event(
                payload, sig_header, endpoint_secret
            )
        else:
            # Fallback for dev/test without verification (NOT SECURE FOR PROD - warn user)
            data = json.loads(payload)
            event = stripe.Event.construct_from(data, stripe.api_key)

    except ValueError as e:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        return 'Invalid signature', 400

    # Handle the event
    if event['type'] == 'checkout.session.completed':
        session_obj = event['data']['object']

        # Fulfill the purchase
        user_id = session_obj.get('client_reference_id') or session_obj.get('metadata', {}).get('user_id')
        credits = session_obj.get('metadata', {}).get('credits_to_add')

        if user_id and credits:
            fulfill_credits(user_id, int(credits))

    return jsonify({'status': 'success'})


# =============================================================================
# STRIPE FULFILLMENT HELPER
# =============================================================================

def fulfill_credits(user_id, amount):
    """Add credits to user's active key"""
    from flask import current_app
    with current_app.app_context():
        try:
            print(f">> STRIPE: Adding {amount} credits to User {user_id}")
            # Find active key
            key = ApiKey.query.filter_by(user_id=user_id, is_active=True).first()
            if key:
                key.credits += amount
                db.session.commit()
                print(">> STRIPE: Credits added successfully!")
            else:
                # Create a key if they don't have one? Or just log error?
                # Let's create one.
                new_key_str = secrets.token_hex(16)
                new_key = ApiKey(
                    key=new_key_str,
                    user_id=user_id,
                    credits=amount,
                    is_active=True
                )
                db.session.add(new_key)
                db.session.commit()
                print(">> STRIPE: Created new key with credits!")
        except Exception as e:
            print(f"!! STRIPE FULFILLMENT ERROR: {e}")
