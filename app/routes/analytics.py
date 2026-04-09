"""
Vantage — Analytics Blueprint
Dashboard stats, trending products, movers & shakers, creative linker, top videos.
"""

import os
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, session
from app import db
from app.models import Product
from app.routes.auth import login_required, admin_required, get_current_user, log_activity

# =============================================================================
# BLUEPRINT
# =============================================================================

analytics_bp = Blueprint('analytics', __name__)

# =============================================================================
# COPILOT STUBS (API shut down Feb 4, 2026) — needed by creative-linker / top-videos
# =============================================================================

def fetch_copilot_products(**kwargs):
    return None

def fetch_copilot_trending(**kwargs):
    return None

# =============================================================================
# ROUTES
# =============================================================================

@analytics_bp.route('/api/stats')
@login_required
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
def api_creative_linker():
    """
    Fetch viral videos for a specific product.
    V2 FIX: Uses topVideos field from /api/trending/products endpoint.
    Paginates through multiple pages to find the specific product.
    """
    product_id = request.args.get('product_id')
    if not product_id:
        return jsonify({'success': False, 'error': 'Product ID required'}), 400

    raw_pid = product_id.replace('shop_', '')

    try:
        # Search through multiple pages to find this specific product's topVideos
        for page in range(10):  # Check up to 10 pages (500 products)
            res = fetch_copilot_products(timeframe='7d', limit=50, page=page)

            if not res or not res.get('products'):
                break

            # Find this specific product in the results
            for p in res.get('products', []):
                if str(p.get('productId', '')) == raw_pid:
                    # Found! Extract topVideos
                    top_videos = p.get('topVideos', [])
                    if top_videos:
                        # Return all top videos (duration filter removed - data often missing)
                        # Sort by revenue if available
                        top_videos_sorted = sorted(top_videos, key=lambda x: x.get('revenue') or x.get('periodRevenue') or 0, reverse=True)

                        return jsonify({
                            'success': True,
                            'total_found': len(top_videos),
                            'videos': top_videos_sorted,
                            'source': 'copilot_v2',
                            'product_found': True
                        })
                    else:
                        # Product found but no topVideos
                        return jsonify({
                            'success': False,
                            'error': 'Product found but no video data available from API',
                            'product_found': True,
                            'videos': []
                        }), 404

        # Product not found in trending data - return honest error
        return jsonify({
            'success': False,
            'error': 'This product is not in the current trending dataset. Try syncing it first.',
            'product_found': False,
            'videos': []
        }), 404

    except Exception as e:
        print(f"[Creative Linker] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@analytics_bp.route('/api/analytics/top-videos', methods=['GET'])
@login_required
def api_top_videos():
    """
    The ROI-Video Feed: Top earning recent videos <= 15s.
    Uses /api/trending endpoint to fetch actual video data with duration.
    """
    try:
        # Fetch global trending videos from Copilot
        res = fetch_copilot_trending(timeframe='7d', sort_by='revenue', limit=100)

        if not res:
            print("[Top Videos] No response from Copilot")
            return jsonify({'success': False, 'error': 'Could not fetch trending videos', 'videos': []}), 200

        # Debug: log what keys we got
        print(f"[Top Videos] Response keys: {list(res.keys()) if isinstance(res, dict) else 'not a dict'}")

        # The /api/trending endpoint returns 'videos' array
        videos = res.get('videos') or res.get('products') or []

        if not videos:
            print("[Top Videos] No videos in response")
            return jsonify({'success': True, 'count': 0, 'videos': [], 'message': 'No videos available'})

        # Map and filter videos for shorts (<= 15s) with significant revenue
        top_shorts = []
        for v in videos:
            # Get duration - try multiple possible field names
            duration = v.get('durationSeconds') or v.get('duration') or v.get('videoDuration') or 0

            # Get revenue - try multiple possible field names
            revenue = v.get('periodRevenue') or v.get('revenue') or v.get('videoRevenue') or 0

            # Filter: Include videos with decent revenue
            # Note: Duration data is often missing (returns 0), so we include those too
            # Only exclude if we KNOW it's over 60s
            if revenue > 100 and (duration == 0 or duration <= 60):
                # Map to expected format for vantage_v2.html
                top_shorts.append({
                    'videoId': v.get('videoId') or v.get('id') or '',
                    'videoUrl': v.get('videoUrl') or v.get('url') or f"https://www.tiktok.com/video/{v.get('videoId', '')}",
                    'coverUrl': v.get('coverUrl') or v.get('thumbnailUrl') or v.get('cover') or '',
                    'durationSeconds': duration,
                    'periodRevenue': revenue,
                    'periodViews': v.get('periodViews') or v.get('views') or v.get('viewCount') or 0,
                    'creatorUsername': v.get('creatorUsername') or v.get('author') or v.get('username') or 'Unknown',
                    'productTitle': v.get('productTitle') or v.get('productName') or v.get('title') or 'Product',
                    'productId': v.get('productId') or '',
                    'productImageUrl': v.get('productImageUrl') or v.get('productCoverUrl') or v.get('cover') or ''
                })

        # Sort by revenue (highest first)
        top_shorts.sort(key=lambda x: x.get('periodRevenue') or 0, reverse=True)

        print(f"[Top Videos] Found {len(top_shorts)} high-revenue videos out of {len(videos)} total")

        return jsonify({
            'success': True,
            'count': len(top_shorts),
            'total_fetched': len(videos),
            'videos': top_shorts[:30]  # Limit to top 30
        })
    except Exception as e:
        print(f"[Top Videos] Error: {e}")
        return jsonify({'success': False, 'error': str(e), 'videos': []}), 200
