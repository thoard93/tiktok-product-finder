"""
PRISM — EchoTik Playwright Browser Scraper

Headless Chromium scraper that logs into EchoTik with saved session cookies,
intercepts XHR/Fetch network requests on product listing pages, captures the
JSON responses, and syncs them through the existing normalization pipeline.

Includes a 6-hour cache layer: products whose last_echotik_sync is < 6 h old
are skipped automatically.

Cookie workflow:
    1. Admin exports cookies from browser (DevTools > Application > Cookies)
    2. Admin uploads via POST /api/admin/echotik-cookies
    3. Cookies are saved to data/echotik_cookies.json
    4. Scraper loads them into a fresh Playwright context each run

Environment:
    ECHOTIK_SCRAPER_HEADED  — set to "1" for visible browser (debug)
    ECHOTIK_SCRAPER_PAGES   — number of pages to scrape (default 5)
"""

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COOKIE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'echotik_cookies.json',
)

ECHOTIK_DOMAIN = 'echotik.live'
ECHOTIK_PRODUCTS_URL = 'https://echotik.live/products/trending'
CACHE_HOURS = 6
DEFAULT_PAGES = 5

# User agents for rotation
_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
]

# XHR URL patterns that carry product data (broad match, refined by response shape)
_XHR_PRODUCT_PATTERNS = [
    '/api/',
    '/product/list',
    '/product/trending',
    '/echotik/product',
    '/v3/echotik',
    '/v3/realtime',
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ScraperError(Exception):
    """Base scraper exception."""


class ScraperCookieError(ScraperError):
    """Cookie file missing, invalid, or expired."""


class ScraperBlockedError(ScraperError):
    """EchoTik detected automation or requires captcha."""


class ScraperTimeoutError(ScraperError):
    """Page load or network wait timed out."""


# ---------------------------------------------------------------------------
# Cookie management
# ---------------------------------------------------------------------------

def load_cookies() -> list[dict]:
    """
    Load EchoTik session cookies from disk.

    Returns list of Playwright-format cookie dicts.
    Raises ScraperCookieError if file is missing or invalid.
    """
    if not os.path.exists(COOKIE_PATH):
        raise ScraperCookieError(
            f"Cookie file not found at {COOKIE_PATH}. "
            "Upload cookies via POST /api/admin/echotik-cookies"
        )

    try:
        with open(COOKIE_PATH, 'r') as f:
            cookies = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ScraperCookieError(f"Failed to parse cookie file: {exc}") from exc

    if not isinstance(cookies, list) or len(cookies) == 0:
        raise ScraperCookieError("Cookie file is empty or not a list")

    # Validate at least one cookie targets echotik
    echotik_cookies = [c for c in cookies if ECHOTIK_DOMAIN in (c.get('domain', ''))]
    if not echotik_cookies:
        raise ScraperCookieError(
            f"No cookies found for domain *{ECHOTIK_DOMAIN}*. "
            "Ensure cookies are exported from echotik.live"
        )

    # Normalize cookies for Playwright
    normalized = []
    for c in cookies:
        cookie = {
            'name': c['name'],
            'value': c['value'],
            'domain': c.get('domain', f'.{ECHOTIK_DOMAIN}'),
            'path': c.get('path', '/'),
        }
        # Optional fields
        if c.get('expires') and c['expires'] > 0:
            cookie['expires'] = c['expires']
        if c.get('httpOnly') is not None:
            cookie['httpOnly'] = bool(c['httpOnly'])
        if c.get('secure') is not None:
            cookie['secure'] = bool(c['secure'])
        if c.get('sameSite'):
            ss = str(c['sameSite']).capitalize()
            if ss in ('Strict', 'Lax', 'None'):
                cookie['sameSite'] = ss

        normalized.append(cookie)

    return normalized


def save_cookies(cookies: list[dict]) -> dict:
    """
    Save cookies to disk. Returns status dict.
    """
    os.makedirs(os.path.dirname(COOKIE_PATH), exist_ok=True)
    with open(COOKIE_PATH, 'w') as f:
        json.dump(cookies, f, indent=2)

    echotik_count = sum(1 for c in cookies if ECHOTIK_DOMAIN in c.get('domain', ''))
    return {
        'total': len(cookies),
        'echotik_cookies': echotik_count,
        'path': COOKIE_PATH,
    }


def get_cookie_status() -> dict:
    """Return cookie file status (without exposing values)."""
    if not os.path.exists(COOKIE_PATH):
        return {'configured': False, 'message': 'No cookie file found'}

    try:
        with open(COOKIE_PATH, 'r') as f:
            cookies = json.load(f)
        mtime = os.path.getmtime(COOKIE_PATH)
        echotik_count = sum(1 for c in cookies if ECHOTIK_DOMAIN in c.get('domain', ''))
        return {
            'configured': True,
            'total_cookies': len(cookies),
            'echotik_cookies': echotik_count,
            'last_updated': datetime.fromtimestamp(mtime).isoformat(),
        }
    except Exception as exc:
        return {'configured': False, 'message': f'Cookie file corrupt: {exc}'}


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

def _get_recently_synced_ids(app) -> set:
    """
    Return set of product_ids that were synced within the last CACHE_HOURS.
    Must be called inside an app context.
    """
    from app import db
    from app.models import Product

    cutoff = datetime.utcnow() - timedelta(hours=CACHE_HOURS)
    rows = (
        db.session.query(Product.product_id)
        .filter(Product.last_echotik_sync >= cutoff)
        .all()
    )
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# XHR Response Interception
# ---------------------------------------------------------------------------

def _matches_product_xhr(url: str) -> bool:
    """Check if a response URL looks like it carries product data."""
    url_lower = url.lower()
    return any(pattern in url_lower for pattern in _XHR_PRODUCT_PATTERNS)


def _extract_products_from_response(body: dict) -> list[dict]:
    """
    Try to extract a product list from an XHR response body.

    EchoTik API responses typically look like:
        { "code": 0, "data": [...] }
      or
        { "code": 0, "data": { "list": [...] } }

    We also handle nested structures and look for product-like objects
    (dicts with a product_id or productId field).
    """
    if not isinstance(body, dict):
        return []

    data = body.get('data')
    if data is None:
        return []

    # Direct list
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict) and _looks_like_product(item)]

    # Nested { "list": [...] }
    if isinstance(data, dict):
        inner = data.get('list') or data.get('products') or data.get('items')
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict) and _looks_like_product(item)]
        # Maybe the data dict itself is a single product
        if _looks_like_product(data):
            return [data]

    return []


def _looks_like_product(d: dict) -> bool:
    """Heuristic: does this dict look like an EchoTik product object?"""
    # Must have some form of product ID
    id_keys = ('product_id', 'productId', 'id', 'product_ids', 'spu_id', 'spuId')
    has_id = any(d.get(k) for k in id_keys)

    # Must have some form of product name or sales data
    data_keys = (
        'product_name', 'productName', 'title', 'name',
        'total_sale_cnt', 'totalSaleCnt', 'sales', 'sale_cnt',
        'total_sale_7d_cnt', 'totalSale7dCnt',
    )
    has_data = any(d.get(k) for k in data_keys)

    return has_id and has_data


# ---------------------------------------------------------------------------
# Core scraper (async)
# ---------------------------------------------------------------------------

async def scrape_products(
    pages: int = DEFAULT_PAGES,
    headed: bool = False,
    debug: bool = False,
) -> dict:
    """
    Launch headless Chromium, load EchoTik cookies, navigate product pages,
    intercept XHR responses, and return collected product data.

    Args:
        pages:  Number of listing pages to scrape.
        headed: If True, run in visible browser mode (for debugging).
        debug:  If True, return raw XHR data instead of syncing to DB.

    Returns:
        Dict with keys: products (list), xhr_urls (list), stats (dict)
    """
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout

    cookies = load_cookies()
    collected_products = []
    collected_xhr_urls = []
    collected_responses = []

    async def on_response(response):
        """Intercept all responses and filter for product data."""
        url = response.url
        if not _matches_product_xhr(url):
            return
        try:
            if 'application/json' not in (response.headers.get('content-type', '')):
                return
            body = await response.json()
            collected_xhr_urls.append(url)

            if debug:
                collected_responses.append({'url': url, 'body': body})

            products = _extract_products_from_response(body)
            if products:
                collected_products.extend(products)
                log.info("[SCRAPER] Intercepted %d products from %s", len(products), url[:120])
        except Exception as exc:
            log.debug("[SCRAPER] Failed to parse response from %s: %s", url[:80], exc)

    ua = random.choice(_USER_AGENTS)
    launch_args = [
        '--disable-blink-features=AutomationControlled',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headed,
            args=launch_args,
        )
        try:
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=ua,
                locale='en-US',
                timezone_id='America/New_York',
                java_script_enabled=True,
            )
            await context.add_cookies(cookies)

            page = await context.new_page()
            page.set_default_timeout(30_000)
            page.on('response', on_response)

            # Navigate to product listing
            log.info("[SCRAPER] Navigating to %s", ECHOTIK_PRODUCTS_URL)
            try:
                await page.goto(ECHOTIK_PRODUCTS_URL, wait_until='networkidle', timeout=45_000)
            except PwTimeout:
                raise ScraperTimeoutError(f"Timed out loading {ECHOTIK_PRODUCTS_URL}")

            # Check for login redirect (cookie expiry detection)
            current_url = page.url.lower()
            if 'login' in current_url or 'signin' in current_url or 'auth' in current_url:
                raise ScraperCookieError(
                    f"Redirected to login page ({page.url}). "
                    "Session cookies are expired — re-upload via admin panel."
                )

            # Wait for initial content to render
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # Paginate through listing pages
            for page_num in range(2, pages + 1):
                try:
                    # Look for pagination controls — try common selectors
                    next_btn = await page.query_selector(
                        'button.next, a.next, [aria-label="Next"], '
                        '.pagination .next, .ant-pagination-next, '
                        'button:has-text("Next"), li.next > a'
                    )
                    if next_btn:
                        is_disabled = await next_btn.get_attribute('disabled')
                        aria_disabled = await next_btn.get_attribute('aria-disabled')
                        if is_disabled is not None or aria_disabled == 'true':
                            log.info("[SCRAPER] Next button disabled on page %d, stopping", page_num - 1)
                            break

                        await next_btn.click()
                        # Wait for network requests triggered by pagination
                        await page.wait_for_load_state('networkidle', timeout=15_000)
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                        log.info("[SCRAPER] Navigated to page %d", page_num)
                    else:
                        # Try URL-based pagination
                        sep = '&' if '?' in ECHOTIK_PRODUCTS_URL else '?'
                        page_url = f"{ECHOTIK_PRODUCTS_URL}{sep}page={page_num}"
                        await page.goto(page_url, wait_until='networkidle', timeout=30_000)
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                        log.info("[SCRAPER] Loaded page %d via URL", page_num)

                except PwTimeout:
                    log.warning("[SCRAPER] Timeout on page %d, continuing with what we have", page_num)
                    break
                except Exception as exc:
                    log.warning("[SCRAPER] Pagination error on page %d: %s", page_num, exc)
                    break

        finally:
            await browser.close()

    stats = {
        'xhr_urls_captured': len(collected_xhr_urls),
        'raw_products_captured': len(collected_products),
        'pages_attempted': pages,
    }

    log.info("[SCRAPER] Capture complete: %d XHR calls, %d products",
             len(collected_xhr_urls), len(collected_products))

    return {
        'products': collected_products,
        'xhr_urls': collected_xhr_urls,
        'debug_responses': collected_responses if debug else [],
        'stats': stats,
    }


# ---------------------------------------------------------------------------
# Sync pipeline — normalizes captured data and writes to DB
# ---------------------------------------------------------------------------

def sync_scraped_products(app, raw_products: list[dict]) -> dict:
    """
    Normalize and sync scraped products to DB, skipping recently-synced ones.

    Args:
        app:          Flask app instance (for app context).
        raw_products: List of raw product dicts from XHR interception.

    Returns:
        Dict with created/updated/skipped/error counts.
    """
    from app.services.echotik import _normalize_product, sync_to_db

    with app.app_context():
        # Get IDs synced within cache window
        recently_synced = _get_recently_synced_ids(app)
        log.info("[SCRAPER] Cache: %d products synced in last %dh (will skip)",
                 len(recently_synced), CACHE_HOURS)

        # Normalize all products
        normalized = []
        normalize_errors = 0
        for raw in raw_products:
            try:
                product = _normalize_product(raw)
                pid = product.get('product_id')
                if not pid:
                    normalize_errors += 1
                    continue
                if pid in recently_synced:
                    continue  # Skip — cached
                normalized.append(product)
            except Exception as exc:
                log.debug("[SCRAPER] Normalize error: %s", exc)
                normalize_errors += 1

        skipped = len(raw_products) - len(normalized) - normalize_errors
        log.info("[SCRAPER] After cache filter: %d to sync, %d skipped, %d errors",
                 len(normalized), skipped, normalize_errors)

        if not normalized:
            return {
                'created': 0, 'updated': 0,
                'skipped': skipped, 'errors': normalize_errors,
            }

        # Use existing sync pipeline
        result = sync_to_db(normalized)
        result['skipped'] = skipped
        result['normalize_errors'] = normalize_errors
        return result


# ---------------------------------------------------------------------------
# Synchronous entry points (for scheduler and admin endpoints)
# ---------------------------------------------------------------------------

def run_scraper_sync(app, pages: int = DEFAULT_PAGES) -> dict:
    """
    Full scrape → normalize → sync pipeline. Synchronous wrapper for async scraper.

    Args:
        app:   Flask app instance.
        pages: Number of listing pages to scrape.

    Returns:
        Combined stats dict.
    """
    headed = os.environ.get('ECHOTIK_SCRAPER_HEADED', '') == '1'
    env_pages = os.environ.get('ECHOTIK_SCRAPER_PAGES')
    if env_pages:
        try:
            pages = int(env_pages)
        except ValueError:
            pass

    log.info("[SCRAPER] Starting full scrape: %d pages, headed=%s", pages, headed)
    start = time.time()

    # Run async scraper in a fresh event loop (safe for threaded scheduler)
    loop = asyncio.new_event_loop()
    try:
        scrape_result = loop.run_until_complete(
            scrape_products(pages=pages, headed=headed)
        )
    finally:
        loop.close()

    raw_products = scrape_result['products']
    if not raw_products:
        log.warning("[SCRAPER] No products captured from XHR interception")
        return {
            'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0,
            'xhr_urls': len(scrape_result['xhr_urls']),
            'duration_s': round(time.time() - start, 1),
        }

    # Sync to database
    sync_result = sync_scraped_products(app, raw_products)
    sync_result['xhr_urls'] = len(scrape_result['xhr_urls'])
    sync_result['raw_captured'] = len(raw_products)
    sync_result['duration_s'] = round(time.time() - start, 1)

    log.info(
        "[SCRAPER] Complete in %.1fs: %d captured → %d new, %d updated, %d skipped, %d errors",
        sync_result['duration_s'], len(raw_products),
        sync_result.get('created', 0), sync_result.get('updated', 0),
        sync_result.get('skipped', 0), sync_result.get('errors', 0),
    )
    return sync_result


def run_debug_scrape(app, pages: int = 1) -> dict:
    """
    Debug scrape: capture XHR but return raw data instead of syncing.

    Returns dict with xhr_urls, sample products, and response structure.
    """
    log.info("[SCRAPER] Debug scrape: %d page(s)", pages)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            scrape_products(pages=pages, headed=False, debug=True)
        )
    finally:
        loop.close()

    # Build debug summary — truncate large payloads
    sample_products = result['products'][:3]  # First 3 products
    debug_responses = []
    for resp in result.get('debug_responses', [])[:5]:
        debug_responses.append({
            'url': resp['url'],
            'keys': list(resp['body'].keys()) if isinstance(resp['body'], dict) else str(type(resp['body'])),
            'product_count': len(_extract_products_from_response(resp['body'])),
        })

    return {
        'xhr_urls': result['xhr_urls'],
        'xhr_summary': debug_responses,
        'sample_products': sample_products,
        'total_products_found': len(result['products']),
        'stats': result['stats'],
    }
