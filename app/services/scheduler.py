"""
PRISM — Background Scheduler
APScheduler jobs for EchoTik sync and stale product refresh.

Jobs:
    echotik_daily_sync   — daily at 8 PM EST (1 AM UTC) — fetch & filter trending products
    echotik_deep_refresh — every 24 h — re-fetch detail for stale products
    google_sheets_sync   — every 72 h — export products to Google Sheets
"""

import atexit
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

def echotik_daily_sync(app):
    """
    Daily sync: pull up to 500 trending products from EchoTik,
    filter by commission/sales/price, upsert to DB.

    Uses run_echotik_sync() from scan.py so the logic is shared
    with the manual /api/scan/echotik-sync endpoint.
    """
    with app.app_context():
        log.info("[SCHEDULER] EchoTik daily sync starting")

        try:
            from app.routes.scan import run_echotik_sync
            result = run_echotik_sync(max_pages=10, page_size=50)
            log.info(
                "[SCHEDULER] Daily sync complete: %d fetched, %d filtered, "
                "%d new, %d updated, %d errors",
                result.get('fetched', 0),
                result.get('filtered', 0),
                result.get('created', 0),
                result.get('updated', 0),
                result.get('errors', 0),
            )
        except Exception:
            log.exception("[SCHEDULER] Daily sync failed")


def echotik_deep_refresh(app):
    """
    Re-fetch product detail for every active product whose last_echotik_sync
    is older than 23 hours.  Processes in batches of 200 with a 0.5 s delay
    between API calls to stay under rate limits.
    """
    from app import db
    from app.models import Product
    from app.services.echotik import fetch_product_detail, sync_to_db, EchoTikError

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
        batch: list[dict] = []

        for product in stale:
            raw_id = product.product_id.replace('shop_', '')
            try:
                detail = fetch_product_detail(raw_id)
                if detail:
                    batch.append(detail)
                    refreshed += 1
                time.sleep(0.5)
            except EchoTikError as exc:
                log.debug("[SCHEDULER] Deep refresh skip %s: %s", raw_id, exc)
                errors += 1
            except Exception as exc:
                log.warning("[SCHEDULER] Deep refresh error %s: %s", raw_id, exc)
                errors += 1

            # Flush every 50 products
            if len(batch) >= 50:
                try:
                    sync_to_db(batch)
                except Exception:
                    log.exception("[SCHEDULER] Deep refresh batch commit failed")
                batch = []

        # Flush remainder
        if batch:
            try:
                sync_to_db(batch)
            except Exception:
                log.exception("[SCHEDULER] Deep refresh final batch commit failed")

        log.info(
            "[SCHEDULER] Deep refresh complete: %d refreshed, %d errors (of %d stale)",
            refreshed, errors, len(stale),
        )


def google_sheets_sync(app):
    """Export top products to Google Sheets."""
    try:
        from app.services.google_sheets import export_products_to_sheet
    except ImportError:
        log.debug("[SCHEDULER] google_sheets module not available — skipping")
        return

    with app.app_context():
        log.info("[SCHEDULER] Google Sheets sync starting")
        try:
            result = export_products_to_sheet()
            log.info("[SCHEDULER] Google Sheets sync done: %s", result)
        except Exception:
            log.exception("[SCHEDULER] Google Sheets sync failed")


# ---------------------------------------------------------------------------
# Scheduler init
# ---------------------------------------------------------------------------

def init_scheduler(app):
    """
    Start the APScheduler BackgroundScheduler with all jobs.
    Safe to call multiple times (gunicorn preload) — uses a module-level guard.
    """
    if getattr(init_scheduler, '_started', False):
        return
    init_scheduler._started = True

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning(
            "[SCHEDULER] APScheduler not installed — background jobs disabled. "
            "pip install apscheduler"
        )
        return

    scheduler = BackgroundScheduler(
        job_defaults={'coalesce': True, 'max_instances': 1},
    )

    # EchoTik daily sync — 8 PM EST = 1:00 AM UTC (next day)
    # Runs after EchoTik's midnight China time data refresh
    scheduler.add_job(
        func=echotik_daily_sync,
        args=[app],
        trigger=CronTrigger(hour=1, minute=0, timezone='UTC'),
        id='echotik_daily_sync',
        name='EchoTik Daily Sync (8 PM EST)',
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
        "[SCHEDULER] Started — daily sync (8 PM EST / 1 AM UTC), "
        "deep refresh (24h), Google Sheets (72h)"
    )
