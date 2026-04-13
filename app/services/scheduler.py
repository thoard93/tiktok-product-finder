"""
PRISM — Background Scheduler
APScheduler jobs for EchoTik sync, stale product refresh,
and Google Sheets export.

Jobs:
    echotik_trending_sync   — every 6 h  — fetch pages 1-5 of trending products
    echotik_deep_refresh    — every 24 h — re-fetch detail for stale products
    google_sheets_sync      — every 72 h — export products to Google Sheets

The Gmail auto-scanner runs in its own daemon thread inside price_research.py
(every 5 min) and is loaded separately — no APScheduler job needed for it.
"""

import atexit
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

def echotik_trending_sync(app):
    """Fetch pages 1-5 of EchoTik trending products, then upsert to DB."""
    from app.services.echotik import (
        fetch_trending_products, sync_to_db, EchoTikError,
    )

    with app.app_context():
        log.info("[SCHEDULER] EchoTik trending sync starting")
        all_products = []

        for page in range(1, 6):
            try:
                products = fetch_trending_products(page=page)
                all_products.extend(products)
                log.info("[SCHEDULER] Page %d: fetched %d products", page, len(products))
                time.sleep(1)  # rate-limit between pages
            except EchoTikError as exc:
                log.warning("[SCHEDULER] Page %d failed: %s", page, exc)
                break

        if not all_products:
            log.warning("[SCHEDULER] Trending sync: 0 products fetched, skipping DB sync")
            return

        try:
            result = sync_to_db(all_products)
            log.info(
                "[SCHEDULER] Trending sync complete: %d new, %d updated, %d errors "
                "(from %d fetched)",
                result['created'], result['updated'], result['errors'],
                len(all_products),
            )
        except Exception:
            log.exception("[SCHEDULER] Trending sync DB commit failed")


def echotik_scraper_sync(app):
    """
    Run the Playwright browser scraper to capture EchoTik product data
    from XHR interception.  Offset 3 h from the API sync so the DB
    effectively refreshes every ~3 hours.
    """
    try:
        from app.services.echotik_scraper import (
            run_scraper_sync, ScraperCookieError, ScraperBlockedError,
            ScraperTimeoutError, ScraperError,
        )
    except ImportError:
        log.warning("[SCHEDULER] echotik_scraper not available — skipping browser sync")
        return

    with app.app_context():
        log.info("[SCHEDULER] EchoTik browser scrape starting")
        try:
            result = run_scraper_sync(app, pages=5)
            log.info(
                "[SCHEDULER] Browser scrape complete in %.1fs: "
                "%d new, %d updated, %d skipped, %d errors",
                result.get('duration_s', 0),
                result.get('created', 0),
                result.get('updated', 0),
                result.get('skipped', 0),
                result.get('errors', 0),
            )
        except ScraperCookieError as exc:
            log.critical("[SCHEDULER] Scraper COOKIE ERROR — admin must re-upload cookies: %s", exc)
        except ScraperBlockedError as exc:
            log.warning("[SCHEDULER] Scraper BLOCKED by EchoTik: %s", exc)
        except ScraperTimeoutError as exc:
            log.warning("[SCHEDULER] Scraper TIMEOUT: %s", exc)
        except ScraperError as exc:
            log.error("[SCHEDULER] Scraper error: %s", exc)
        except Exception:
            log.exception("[SCHEDULER] Scraper unexpected error")


def echotik_deep_refresh(app):
    """
    Re-fetch product detail for every active product whose last_echotik_sync
    is older than 23 hours.  Processes in batches of 200 with a 0.5 s delay
    between API calls to stay under rate limits.
    """
    from app import db
    from app.models import Product
    from app.services.echotik import (
        fetch_product_detail, EchoTikError,
    )

    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(hours=23)
        stale = (
            Product.query
            .filter(
                Product.product_status == 'active',
                db.or_(
                    Product.last_echotik_sync < cutoff,
                    Product.last_echotik_sync.is_(None),
                ),
            )
            .order_by(Product.last_echotik_sync.asc().nullsfirst())
            .limit(500)
            .all()
        )

        if not stale:
            log.info("[SCHEDULER] Deep refresh: no stale products")
            return

        log.info("[SCHEDULER] Deep refresh starting for %d stale products", len(stale))
        refreshed = 0
        errors = 0
        now = datetime.utcnow()

        for product in stale:
            try:
                detail = fetch_product_detail(product.product_id)
                if not detail:
                    product.last_echotik_sync = now
                    product.last_updated = now
                    errors += 1
                    continue

                # Apply fields
                _apply_detail(product, detail)
                product.last_echotik_sync = now
                product.last_updated = now
                refreshed += 1

                time.sleep(0.5)

            except EchoTikError as exc:
                log.warning("[SCHEDULER] Deep refresh %s failed: %s",
                            product.product_id, exc)
                # Mark as touched so we don't retry the same product immediately
                product.last_echotik_sync = now
                product.last_updated = now
                errors += 1
            except Exception:
                log.exception("[SCHEDULER] Deep refresh unexpected error for %s",
                              product.product_id)
                product.last_echotik_sync = now
                product.last_updated = now
                errors += 1

        try:
            db.session.commit()
        except Exception:
            log.exception("[SCHEDULER] Deep refresh commit failed")
            db.session.rollback()
            return

        log.info(
            "[SCHEDULER] Deep refresh complete: %d refreshed, %d errors out of %d",
            refreshed, errors, len(stale),
        )


def google_sheets_sync(app):
    """Export current products to a configured Google Sheet."""
    with app.app_context():
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError:
            log.warning("[SCHEDULER] Google Sheets sync skipped — gspread not installed")
            return

        import json
        from datetime import timezone
        from app.models import Product
        from app.routes.admin import get_google_sheets_config

        try:
            config = get_google_sheets_config()
            if not config.get('sheet_id') or not config.get('credentials'):
                log.info("[SCHEDULER] Google Sheets sync skipped — not configured")
                return

            if config.get('frequency') == 'manual':
                log.info("[SCHEDULER] Google Sheets sync skipped — set to manual")
                return

            creds_dict = json.loads(config['credentials'])
            scopes = ['https://www.googleapis.com/auth/spreadsheets']
            credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            gc = gspread.authorize(credentials)
            sheet = gc.open_by_key(config['sheet_id']).sheet1

            # Fetch active products sorted by GMV
            products = (
                Product.query
                .filter(Product.product_status == 'active')
                .order_by(Product.gmv.desc())
                .limit(2000)
                .all()
            )

            est = timezone(timedelta(hours=-5))
            sync_date = datetime.now(est).strftime('%Y-%m-%d %I:%M %p EST')

            rows = []
            for p in products:
                raw_id = str(p.product_id).replace('shop_', '')
                rows.append([
                    raw_id,
                    p.product_name or '',
                    f"https://shop.tiktok.com/view/product/{raw_id}?region=US",
                    p.seller_name or '',
                    round(p.price or 0, 2),
                    f"{(p.commission_rate or 0) * 100:.1f}%",
                    f"{(p.shop_ads_commission or 0) * 100:.1f}%",
                    p.video_count_alltime or p.video_count or 0,
                    p.influencer_count or 0,
                    p.sales or 0,
                    p.sales_7d or 0,
                    p.gmv or 0,
                    p.ad_spend or 0,
                    sync_date,
                ])

            header = [
                'product_id', 'product_name', 'product_url', 'seller_name',
                'price', 'commission_rate', 'gmv_max_rate',
                'video_count_alltime', 'creator_count', 'sales_alltime',
                'sales_7d', 'revenue_alltime', 'ad_spend', 'sync_date',
            ]
            sheet.clear()
            sheet.update('A1', [header] + rows)

            log.info("[SCHEDULER] Google Sheets sync complete: %d products", len(rows))

        except Exception:
            log.exception("[SCHEDULER] Google Sheets sync error")


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _apply_detail(product, detail: dict):
    """Apply normalized detail dict to an existing Product row."""
    from app.services.echotik import _safe_int

    # Sales / GMV — always update to latest
    product.sales = detail.get('sales') or product.sales or 0
    product.sales_7d = detail.get('sales_7d') or product.sales_7d or 0
    product.sales_30d = detail.get('sales_30d') or product.sales_30d or 0
    product.gmv = detail.get('gmv') or product.gmv or 0
    product.gmv_30d = detail.get('gmv_30d') or product.gmv_30d or 0

    # Influencer / commission
    inf = detail.get('influencer_count') or 0
    if inf > 0:
        product.influencer_count = inf
    comm = detail.get('commission_rate') or 0
    if comm > 0:
        product.commission_rate = comm

    # Price
    price = detail.get('price') or 0
    if price > 0:
        product.price = price

    # Video counts — never downgrade all-time
    v_alltime = detail.get('video_count_alltime') or 0
    if v_alltime > (product.video_count_alltime or 0):
        product.video_count_alltime = v_alltime
        product.video_count = v_alltime

    product.video_7d = detail.get('video_count_7d') or product.video_7d or 0
    product.video_30d = detail.get('video_30d') or product.video_30d or 0
    product.live_count = detail.get('live_count') or product.live_count or 0
    product.views_count = detail.get('views_count') or product.views_count or 0
    product.product_rating = detail.get('rating') or product.product_rating or 0
    product.review_count = detail.get('review_count') or product.review_count or 0
    product.ad_spend = detail.get('ad_spend') or product.ad_spend or 0

    # New enrichment fields
    product.category = detail.get('category') or product.category
    product.subcategory = detail.get('subcategory') or product.subcategory
    product.return_rate = detail.get('return_rate') or product.return_rate
    product.rating = detail.get('rating') or product.rating

    # Velocity
    s7d = product.sales_7d or 0
    v7d = product.video_7d or 0
    product.sales_velocity = round(s7d / max(v7d, 1), 2)


# ---------------------------------------------------------------------------
# Scheduler init — called from app factory
# ---------------------------------------------------------------------------

def init_scheduler(app):
    """
    Start the APScheduler BackgroundScheduler with all jobs.
    Safe to call multiple times (gunicorn preload) — uses a module-level guard.
    """
    # Prevent double-start when gunicorn forks or module reloads
    if getattr(init_scheduler, '_started', False):
        return
    init_scheduler._started = True

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        log.warning(
            "[SCHEDULER] APScheduler not installed — background jobs disabled. "
            "pip install apscheduler"
        )
        return

    scheduler = BackgroundScheduler(
        job_defaults={'coalesce': True, 'max_instances': 1},
    )

    # EchoTik trending sync — every 6 hours
    scheduler.add_job(
        func=echotik_trending_sync,
        args=[app],
        trigger='interval',
        hours=6,
        id='echotik_trending_sync',
        name='EchoTik Trending Sync',
    )

    # EchoTik browser scraper — every 6 hours, offset 3 h from API sync
    from datetime import datetime as _dt, timedelta as _td
    scraper_start = _dt.utcnow() + _td(hours=3)
    scheduler.add_job(
        func=echotik_scraper_sync,
        args=[app],
        trigger='interval',
        hours=6,
        next_run_time=scraper_start,
        id='echotik_scraper_sync',
        name='EchoTik Browser Scraper',
    )

    # EchoTik deep refresh — every 24 hours
    scheduler.add_job(
        func=echotik_deep_refresh,
        args=[app],
        trigger='interval',
        hours=24,
        id='echotik_deep_refresh',
        name='EchoTik Deep Refresh',
    )

    # Google Sheets export — every 72 hours
    scheduler.add_job(
        func=google_sheets_sync,
        args=[app],
        trigger='interval',
        hours=72,
        id='google_sheets_sync',
        name='Google Sheets Sync',
    )

    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))

    log.info(
        "[SCHEDULER] Started — EchoTik API trending (6h), browser scraper (6h +3h offset), "
        "deep refresh (24h), Google Sheets (72h)"
    )
