"""
PRISM — Background Scheduler
Single daily sync at 8 PM EST — products, videos, brands.

Credit budget (~86k/month of 100k):
    Product scan (incremental):  ~60,000/month
    Video sync (piggybacked):    ~15,000/month
    Brand sync:                   ~6,000/month
    On-demand trend charts:       ~5,000/month
"""

import atexit
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Video sync helper — called per product during deep refresh
# ---------------------------------------------------------------------------

def _sync_videos_for_product(product, db):
    """Sync <=15s videos for a single product. Called during deep refresh."""
    from app.models import ProductVideo
    from app.services.echotik import fetch_product_videos

    raw_id = product.product_id.replace('shop_', '')
    try:
        videos = fetch_product_videos(raw_id, page_size=10)
        if not videos:
            return 0

        # Clear old videos for this product
        ProductVideo.query.filter_by(product_id=product.product_id).delete()

        stored = 0
        for v in videos:
            duration = v.get('duration', 0)

            vid = ProductVideo(
                product_id=product.product_id,
                video_id=str(v.get('video_id', ''))[:100],
                video_url=(v.get('video_url') or '')[:500],
                cover_url=(v.get('cover_url') or '')[:500],
                creator_name=(v.get('creator_name') or '')[:200],
                creator_handle=(v.get('creator_handle') or '')[:200],
                creator_avatar=(v.get('creator_avatar') or '')[:500],
                view_count=v.get('view_count', 0),
                like_count=v.get('like_count', 0),
                duration_seconds=duration,
            )
            db.session.add(vid)
            stored += 1
            if stored >= 5:
                break

        return stored
    except Exception as exc:
        log.debug("Video sync skip %s: %s", raw_id, exc)
        return 0


# ---------------------------------------------------------------------------
# Brand sync
# ---------------------------------------------------------------------------

def _run_brand_sync(app):
    """Sync top 100 shops/brands from EchoTik."""
    from app import db
    from app.models import Brand
    from app.services.echotik import fetch_top_shops

    with app.app_context():
        log.info("[SCHEDULER] Brand sync starting")
        total = 0

        all_shops = []
        for pg in range(1, 6):  # 5 pages x 10 = 50 sellers
            page_shops = fetch_top_shops(country="US", page_size=10, page=pg)
            if not page_shops:
                break
            all_shops.extend(page_shops)
            time.sleep(0.3)
        if all_shops:
            for s in all_shops:
                sid = s.get('shop_id', '')
                if not sid:
                    continue
                brand = Brand.query.filter_by(shop_id=sid).first()
                if not brand:
                    brand = Brand(shop_id=sid, name=s.get('name', 'Unknown'))
                    db.session.add(brand)
                brand.name = (s.get('name') or brand.name)[:300]
                brand.avatar_url = (s.get('avatar_url') or '')[:500] or brand.avatar_url
                brand.country = s.get('country', 'US')
                brand.category = (s.get('category') or '')[:100] or brand.category
                brand.follower_count = s.get('follower_count', 0)
                brand.gmv_30d = s.get('gmv_30d', 0)
                brand.product_count = s.get('product_count', 0)
                brand.trending_score = s.get('trending_score', 0)
                brand.tiktok_shop_url = (s.get('shop_url') or '')[:500]
                brand.last_synced = datetime.utcnow()
                total += 1

        db.session.commit()
        log.info("[SCHEDULER] Brand sync complete: %d brands", total)


# ---------------------------------------------------------------------------
# Main daily sync — products (incremental) + videos + brands
# ---------------------------------------------------------------------------

def daily_sync(app):
    """
    Single daily sync at 8 PM EST.

    1. Fetch trending product list (250 products)
    2. For new/stale products, fetch detail + videos (incremental — skip <24h)
    3. Sync brands
    """
    with app.app_context():
        log.info("[SCHEDULER] === Daily sync starting ===")

        # Step 1: Product list sync (uses existing run_echotik_sync)
        try:
            from app.routes.scan import run_echotik_sync
            result = run_echotik_sync(max_pages=25, page_size=10)
            log.info(
                "[SCHEDULER] Product list: %d fetched, %d filtered, %d new, %d updated",
                result.get('fetched', 0), result.get('filtered', 0),
                result.get('created', 0), result.get('updated', 0),
            )
        except Exception:
            log.exception("[SCHEDULER] Product list sync failed")

        # Step 2: Deep refresh stale products + video sync
        try:
            _deep_refresh_with_videos(app)
        except Exception:
            log.exception("[SCHEDULER] Deep refresh failed")

        # Step 3: Brand sync
        try:
            _run_brand_sync(app)
        except Exception:
            log.exception("[SCHEDULER] Brand sync failed")

        # Step 4: Refresh Brand Hunter product stats
        try:
            _refresh_brand_products(app)
        except Exception:
            log.exception("[SCHEDULER] Brand product refresh failed")

        # Step 5: Enrich missing seller names (100 products per run)
        try:
            _enrich_seller_names(app)
        except Exception:
            log.exception("[SCHEDULER] Seller enrichment failed")

        # Step 6: Warm Opportunity Score cache for all active products
        try:
            _warm_score_cache(app)
        except Exception:
            log.exception("[SCHEDULER] Score cache warm failed")

        log.info("[SCHEDULER] === Daily sync complete ===")


def _warm_score_cache(app):
    """Pre-compute and cache Opportunity Scores for all active products."""
    from app import db
    from app.models import Product
    from app.routes.views import _calc_score_raw
    from sqlalchemy import or_

    with app.app_context():
        products = Product.query.filter(
            or_(Product.product_status == 'active', Product.product_status.is_(None)),
        ).all()

        count = 0
        for p in products:
            try:
                p.cached_score = _calc_score_raw(p)
                p.score_cached_at = datetime.utcnow()
                count += 1
            except Exception:
                continue

        try:
            db.session.commit()
            log.info("[SCHEDULER] Score cache warmed for %d products", count)
        except Exception:
            db.session.rollback()
            log.exception("[SCHEDULER] Score cache commit failed")


def _deep_refresh_with_videos(app):
    """Re-fetch detail + videos for stale products (>24h since last sync)."""
    from app import db
    from app.models import Product
    from app.services.echotik import fetch_product_detail, sync_to_db, EchoTikError

    cutoff = datetime.utcnow() - timedelta(hours=24)
    stale = (
        Product.query
        .filter(
            db.or_(
                Product.product_status == 'active',
                Product.product_status.is_(None),
            ),
            db.or_(
                Product.last_echotik_sync < cutoff,
                Product.last_echotik_sync.is_(None),
            ),
        )
        .order_by(Product.last_echotik_sync.asc().nullsfirst())
        .limit(200)
        .all()
    )

    if not stale:
        log.info("[SCHEDULER] Deep refresh: no stale products")
        return

    log.info("[SCHEDULER] Deep refresh: %d stale products", len(stale))
    refreshed = 0
    videos_synced = 0
    batch = []

    for product in stale:
        raw_id = product.product_id.replace('shop_', '')
        try:
            detail = fetch_product_detail(raw_id)
            if detail:
                batch.append(detail)
                refreshed += 1
            time.sleep(0.3)

            # Sync videos for this product too
            v = _sync_videos_for_product(product, db)
            videos_synced += v
            time.sleep(0.2)

        except EchoTikError as exc:
            log.debug("[SCHEDULER] Refresh skip %s: %s", raw_id, exc)
        except Exception as exc:
            log.warning("[SCHEDULER] Refresh error %s: %s", raw_id, exc)

        if len(batch) >= 50:
            try:
                sync_to_db(batch)
            except Exception:
                log.exception("[SCHEDULER] Batch commit failed")
            batch = []

    if batch:
        try:
            sync_to_db(batch)
        except Exception:
            log.exception("[SCHEDULER] Final batch commit failed")

    # Commit video inserts
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    log.info(
        "[SCHEDULER] Deep refresh: %d products refreshed, %d videos synced",
        refreshed, videos_synced,
    )


def _refresh_brand_products(app):
    """Refresh stats for Brand Hunter products (BrandProduct table)."""
    from app import db
    from app.models import BrandProduct, ScannedBrand
    from app.services.echotik import fetch_brand_products

    brands = ScannedBrand.query.filter(
        ScannedBrand.scan_status == 'complete',
        ScannedBrand.total_products > 0,
    ).all()

    if not brands:
        log.info("[SCHEDULER] No scanned brands to refresh")
        return

    log.info("[SCHEDULER] Refreshing %d scanned brands", len(brands))
    total_updated = 0

    for brand in brands:
        try:
            # Fetch first 2 pages (20 products) for a quick stats refresh
            fresh_products = []
            for page in range(1, 3):
                products = fetch_brand_products(brand.brand_id, page=page, page_size=10)
                if products:
                    fresh_products.extend(products)
                time.sleep(0.3)

            if not fresh_products:
                continue

            # Build a lookup of fresh data by product_id
            fresh_map = {}
            for p in fresh_products:
                pid = p.get('product_id', '')
                if pid:
                    fresh_map[pid] = p

            # Update existing BrandProduct records that match
            existing = BrandProduct.query.filter_by(brand_id=brand.id).limit(100).all()
            for bp in existing:
                raw_pid = bp.product_id.replace('shop_', '')
                fresh = fresh_map.get(raw_pid) or fresh_map.get(bp.product_id)
                if fresh:
                    bp.sales_30d = fresh.get('sales_30d', 0) or fresh.get('sales', 0) or bp.sales_30d
                    bp.revenue_30d = fresh.get('gmv_30d', 0) or fresh.get('gmv', 0) or bp.revenue_30d
                    bp.total_videos = fresh.get('video_count_alltime', 0) or fresh.get('video_count', 0) or bp.total_videos
                    bp.influencer_count = fresh.get('influencer_count', 0) or bp.influencer_count
                    bp.price = fresh.get('price', 0) or bp.price
                    bp.commission_rate = fresh.get('commission_rate', 0) or bp.commission_rate
                    total_updated += 1

            # Update brand aggregate stats
            all_bp = BrandProduct.query.filter_by(brand_id=brand.id).all()
            brand.sales_30d = sum(bp.sales_30d or 0 for bp in all_bp)
            brand.revenue_30d = sum(bp.revenue_30d or 0 for bp in all_bp)
            brand.last_scanned = datetime.utcnow()
            db.session.commit()

        except Exception as e:
            log.warning("[SCHEDULER] Brand refresh error for %s: %s", brand.brand_name, e)
            db.session.rollback()
            continue

    log.info("[SCHEDULER] Brand products refreshed: %d products updated across %d brands", total_updated, len(brands))


def _enrich_seller_names(app):
    """Fetch seller names for products with 'Unknown' seller via product detail API."""
    from app import db
    from app.models import Product
    from app.services.echotik import fetch_product_detail
    from sqlalchemy import or_

    products = Product.query.filter(
        or_(Product.product_status == 'active', Product.product_status.is_(None)),
        or_(
            Product.seller_name.is_(None),
            Product.seller_name == '',
            Product.seller_name == 'Unknown',
            Product.seller_name == 'Unknown Seller',
        ),
    ).order_by(Product.sales_7d.desc().nullslast()).limit(100).all()

    if not products:
        log.info("[SCHEDULER] Seller enrichment: no products with missing sellers")
        return

    log.info("[SCHEDULER] Enriching seller names for %d products", len(products))
    enriched = 0

    for p in products:
        raw_id = p.product_id.replace('shop_', '')
        try:
            detail = fetch_product_detail(raw_id)
            if detail:
                sname = (detail.get('seller_name') or '').strip()
                if sname and sname.lower() not in ('unknown', 'none', 'null', ''):
                    p.seller_name = sname
                    enriched += 1
                sid = detail.get('seller_id')
                if sid and not p.seller_id:
                    p.seller_id = sid
            time.sleep(0.3)
        except Exception:
            continue

    db.session.commit()
    log.info("[SCHEDULER] Seller enrichment: %d/%d products enriched", enriched, len(products))


# ---------------------------------------------------------------------------
# Scheduler init — SINGLE daily job at 8 PM EST
# ---------------------------------------------------------------------------

def init_scheduler(app):
    """Start APScheduler with a single daily job. Safe to call multiple times."""
    if getattr(init_scheduler, '_started', False):
        return
    init_scheduler._started = True

    import os
    if os.environ.get('SKIP_SCHEDULER'):
        log.info("[SCHEDULER] SKIP_SCHEDULER set — scheduler disabled")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("[SCHEDULER] APScheduler not installed — pip install apscheduler")
        return

    scheduler = BackgroundScheduler(
        job_defaults={'coalesce': True, 'max_instances': 1},
    )

    # Single daily sync — 8 PM EST = 1:00 AM UTC (next day)
    # APScheduler handles DST automatically when using timezone-aware triggers
    scheduler.add_job(
        func=daily_sync,
        args=[app],
        trigger=CronTrigger(hour=1, minute=0, timezone='UTC'),
        id='daily_sync',
        name='Daily EchoTik Sync (8 PM EST)',
    )

    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    log.info("[SCHEDULER] Started — daily sync at 8 PM EST (1:00 AM UTC)")
