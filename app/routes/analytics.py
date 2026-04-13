"""
Vantage — Analytics Blueprint
Dashboard stats, trending products, movers & shakers, creative linker, top videos.
"""

import os
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, session
from app import db
from app.models import Product
from app.routes.auth import login_required, admin_required, subscription_required, get_current_user, log_activity

# =============================================================================
# BLUEPRINT
# =============================================================================

analytics_bp = Blueprint('analytics', __name__)

# =============================================================================
# ROUTES
# =============================================================================

@analytics_bp.route('/api/stats')
@login_required
@subscription_required
def api_stats():
    """Get global stats for dashboard"""
    try:
        # 1. Total Products
        total_products = Product.query.count()

        # 2. Ad Winners (Ads, >50 sales, <5 influencers)
        ad_winners = Product.query.filter(
             db.or_(
                Product.scan_type.in_(['apify_ad', 'daily_virals', 'dv_live']),
                db.and_(Product.sales_7d > 50, Product.influencer_count < 5, Product.video_count < 5)
            )
        ).count()

        # 3. Opportunity Gems (New Criteria: 50-100 videos, $500+ ad spend, 50+ 7D sales)
        video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)
        hidden_gems = Product.query.filter(
            Product.sales_7d >= 50,
            Product.ad_spend >= 500,
            video_count_field >= 50,
            video_count_field <= 100
        ).count()

        # 4. EchoTik Status (Mock or cached check)
        # Verify if our keys are working? Just return "Active" for now

        return jsonify({
            'success': True,
            'stats': {
                'total_products': total_products,
                'ad_winners': ad_winners,
                'hidden_gems': hidden_gems,
                'echotik_status': 'Active'
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@analytics_bp.route('/api/oos-stats', methods=['GET'])
def get_oos_stats():
    """Get out-of-stock statistics"""
    try:
        total_products = Product.query.count()
        active_products = Product.query.filter(
            db.or_(Product.product_status == None, Product.product_status == 'active')
        ).count()
        likely_oos = Product.query.filter(Product.product_status == 'likely_oos').count()
        manually_oos = Product.query.filter(Product.product_status == 'out_of_stock').count()
        removed = Product.query.filter(Product.product_status == 'removed').count()

        return jsonify({
            'success': True,
            'stats': {
                'total_products': total_products,
                'active': active_products,
                'likely_oos': likely_oos,
                'manually_oos': manually_oos,
                'removed': removed
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@analytics_bp.route('/api/trending-products', methods=['GET'])
def api_trending_products():
    """Get products with significant sales velocity changes"""
    min_velocity = float(request.args.get('min_velocity', 20))
    limit = int(request.args.get('limit', 100))

    products = Product.query.filter(
        Product.sales_velocity >= min_velocity,
        db.or_(Product.product_status == 'active', Product.product_status == None)
    ).order_by(
        Product.sales_velocity.desc()
    ).limit(limit).all()

    return jsonify({
        'success': True,
        'count': len(products),
        'products': [p.to_dict() for p in products]
    })


@analytics_bp.route('/api/analytics/movers-shakers', methods=['GET'])
@login_required
@subscription_required
def api_movers_shakers():
    """
    Movers & Shakers Leaderboard: Products with highest growth indicators.
    V2 FIX: Relaxed filters and added fallback for products without gmv_growth.
    """
    try:
        limit = request.args.get('limit', 20, type=int)

        # Primary: Products with actual GMV growth
        products_with_growth = Product.query.filter(
            Product.sales_7d >= 10,  # Lowered from 50
            Product.video_count >= 5,  # Lowered from 10
            Product.gmv_growth > 0
        ).order_by(Product.gmv_growth.desc()).limit(limit).all()

        # Fallback: If not enough products with growth, add top revenue products
        if len(products_with_growth) < limit:
            remaining = limit - len(products_with_growth)
            existing_ids = [p.product_id for p in products_with_growth]
            fallback = Product.query.filter(
                Product.sales_7d >= 10,
                Product.gmv > 0,
                ~Product.product_id.in_(existing_ids)
            ).order_by(Product.gmv.desc()).limit(remaining).all()
            products_with_growth.extend(fallback)

        return jsonify({
            'success': True,
            'products': [p.to_dict() for p in products_with_growth]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@analytics_bp.route('/api/analytics/creative-linker', methods=['GET'])
@login_required
@subscription_required
def api_creative_linker():
    """
    Fetch viral videos for a specific product.
    Note: Video data is sourced from EchoTik product detail when available.
    """
    product_id = request.args.get('product_id')
    if not product_id:
        return jsonify({'success': False, 'error': 'Product ID required'}), 400

    raw_pid = product_id.replace('shop_', '')

    try:
        # Try fetching video data from EchoTik
        from app.services.echotik import fetch_product_detail, EchoTikError
        try:
            detail = fetch_product_detail(raw_pid)
        except EchoTikError:
            detail = None

        if detail and detail.get('top_videos'):
            videos = detail['top_videos']
            videos_sorted = sorted(videos, key=lambda x: x.get('revenue') or x.get('periodRevenue') or 0, reverse=True)
            return jsonify({
                'success': True,
                'total_found': len(videos),
                'videos': videos_sorted,
                'source': 'echotik',
                'product_found': True,
            })

        return jsonify({
            'success': False,
            'error': 'No video data available for this product.',
            'product_found': detail is not None,
            'videos': [],
        }), 404

    except Exception as e:
        print(f"[Creative Linker] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@analytics_bp.route('/api/analytics/top-videos', methods=['GET'])
@login_required
@subscription_required
def api_top_videos():
    """
    The ROI-Video Feed: Top earning recent videos.
    Uses high-GMV products from the database as source since Copilot is defunct.
    """
    try:
        # Build video feed from top-performing products in DB
        products = Product.query.filter(
            Product.sales_7d >= 10,
            Product.gmv > 0,
            db.or_(Product.product_status == 'active', Product.product_status == None),
        ).order_by(Product.gmv.desc()).limit(30).all()

        videos = []
        for p in products:
            videos.append({
                'videoId': '',
                'videoUrl': p.product_url or f"https://shop.tiktok.com/view/product/{str(p.product_id).replace('shop_', '')}?region=US",
                'coverUrl': p.cached_image_url or p.image_url or '',
                'durationSeconds': 0,
                'periodRevenue': p.gmv or 0,
                'periodViews': p.views_count or 0,
                'creatorUsername': p.seller_name or 'Unknown',
                'productTitle': p.product_name or 'Product',
                'productId': p.product_id,
                'productImageUrl': p.cached_image_url or p.image_url or '',
            })

        return jsonify({
            'success': True,
            'count': len(videos),
            'total_fetched': len(videos),
            'videos': videos,
        })
    except Exception as e:
        print(f"[Top Videos] Error: {e}")
        return jsonify({'success': False, 'error': str(e), 'videos': []}), 200
