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
            duration = v.get('duration', 999)
            if duration > 15:
                continue  # HARD FILTER — skip anything over 15 seconds

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

        log.info("[SCHEDULER] === Daily sync complete ===")


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
