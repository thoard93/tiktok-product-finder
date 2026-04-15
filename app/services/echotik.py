"""
PRISM — EchoTik Data Service
Centralized client for the EchoTik open API (v3).

Handles authentication, retries, proxy routing, and DB sync for
trending product discovery and single-product enrichment.

Environment variables:
    ECHOTIK_USERNAME       — HTTP Basic Auth username for open.echotik.live
    ECHOTIK_PASSWORD       — HTTP Basic Auth password for open.echotik.live
    ECHOTIK_PROXY_STRING   — Optional proxy in format host:port:username:password
"""

import os
import time
import logging
from datetime import datetime
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ECHOTIK_V3_BASE = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_REALTIME_BASE = "https://open.echotik.live/api/v3/realtime"

ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')
ECHOTIK_PROXY_STRING = os.environ.get('ECHOTIK_PROXY_STRING')

# TikTok Shop top-level category ID → name mapping
# These are the primary category IDs from EchoTik's product/list response
TIKTOK_CATEGORIES = {
    '600001': 'Womenswear & Underwear',
    '600002': 'Menswear & Underwear',
    '600003': 'Shoes',
    '600004': 'Beauty & Personal Care',
    '600005': 'Health',
    '600006': 'Food & Beverages',
    '600007': 'Electronics',
    '600008': 'Home Supplies',
    '600009': 'Baby & Maternity',
    '600010': 'Sports & Outdoor',
    '600011': 'Toys & Hobbies',
    '600012': 'Pet Supplies',
    '600013': 'Luggage & Bags',
    '600014': 'Accessories',
    '600015': 'Home Appliances',
    '600016': 'Automotive',
    '600017': 'Books & Stationery',
    '600018': 'Kitchenware',
    '824328': 'Health & Wellness',
    '839944': 'Beauty',
    '852104': 'Fashion',
}

# Retry config
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # seconds
AUTH_RETRY_LIMIT = 1   # re-auth once, then give up


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def _get_auth() -> HTTPBasicAuth:
    # Read from env at call time so rotated creds are picked up without restart
    username = os.environ.get('ECHOTIK_USERNAME', '') or ECHOTIK_USERNAME
    password = os.environ.get('ECHOTIK_PASSWORD', '') or ECHOTIK_PASSWORD
    return HTTPBasicAuth(username, password)


def _get_proxies() -> Optional[dict]:
    if not ECHOTIK_PROXY_STRING:
        return None
    parts = ECHOTIK_PROXY_STRING.split(':')
    if len(parts) == 4:
        proxy_url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        return {"http": proxy_url, "https": proxy_url}
    return None


def _request(method: str, url: str, *, params=None, json_body=None,
             timeout=30, use_proxy=False) -> dict:
    """
    Fire an HTTP request with automatic retries + exponential backoff.

    Returns the parsed JSON body on success.
    Raises ``EchoTikError`` on unrecoverable failure.
    """
    proxies = _get_proxies() if use_proxy else None
    auth = _get_auth()
    last_exc = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method, url,
                params=params,
                json=json_body,
                auth=auth,
                proxies=proxies,
                timeout=timeout,
            )

            # Auth failure — retry once with fresh creds (env may have rotated)
            if resp.status_code in (401, 403):
                if attempt <= AUTH_RETRY_LIMIT:
                    log.warning("EchoTik auth error %s on attempt %d — retrying",
                                resp.status_code, attempt)
                    time.sleep(INITIAL_BACKOFF)
                    continue
                raise EchoTikAuthError(
                    f"Authentication failed ({resp.status_code}): "
                    f"check ECHOTIK_USERNAME / ECHOTIK_PASSWORD"
                )

            if resp.status_code != 200:
                raise EchoTikError(f"HTTP {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            if data.get('code') != 0:
                raise EchoTikError(
                    f"API error code={data.get('code')}: {data.get('message', '')}"
                )
            return data

        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            wait = INITIAL_BACKOFF * (2 ** (attempt - 1))
            log.warning("EchoTik network error on attempt %d/%d — retrying in %.1fs: %s",
                        attempt, MAX_RETRIES, wait, exc)
            time.sleep(wait)

    raise EchoTikError(f"All {MAX_RETRIES} retries exhausted") from last_exc


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EchoTikError(Exception):
    """Base exception for EchoTik API errors."""


class EchoTikAuthError(EchoTikError):
    """Authentication / authorization failure."""


# ---------------------------------------------------------------------------
# Helpers — field parsing
# ---------------------------------------------------------------------------

def _safe_int(val, default=0) -> int:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return int(val)
    try:
        cleaned = str(val).replace(',', '').strip()
        if not cleaned:
            return default
        # Handle K/M/B suffixes
        multiplier = 1
        upper = cleaned.upper()
        if upper.endswith('K'):
            multiplier, cleaned = 1_000, cleaned[:-1]
        elif upper.endswith('M'):
            multiplier, cleaned = 1_000_000, cleaned[:-1]
        elif upper.endswith('B'):
            multiplier, cleaned = 1_000_000_000, cleaned[:-1]
        return int(float(cleaned) * multiplier)
    except (ValueError, TypeError):
        return default


def _safe_float(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        return float(str(val).replace('$', '').replace(',', '').strip() or default)
    except (ValueError, TypeError):
        return default


def _pick(*candidates, default=None):
    """Return the first truthy value from *candidates*."""
    for c in candidates:
        if c:
            return c
    return default


def _extract_image_url(raw):
    """
    Extract a single usable image URL from whatever EchoTik sends.

    Handles: plain URL string, dict with 'url' key, list of dicts,
    or a JSON-encoded string of any of the above.
    """
    import json as _json

    if not raw:
        return None

    # If it's a string that looks like JSON (starts with [ or {), parse it
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith(('[', '{')):
            try:
                raw = _json.loads(stripped)
            except (ValueError, TypeError):
                pass

    # List of dicts — pick the first one with a url
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                url = item.get('url') or item.get('thumb_url')
                if url and isinstance(url, str) and url.startswith('http'):
                    return url[:500]
            elif isinstance(item, str) and item.startswith('http'):
                return item[:500]
        return None

    # Single dict
    if isinstance(raw, dict):
        url = (raw.get('url')
               or (raw.get('url_list') or [None])[0]
               or raw.get('thumb_url'))
        if url and isinstance(url, str):
            return url[:500]
        return None

    # Plain string URL
    if isinstance(raw, str) and raw.startswith('http'):
        return raw[:500]

    return None


# ---------------------------------------------------------------------------
# Public API — fetch_trending_products
# ---------------------------------------------------------------------------

def fetch_trending_products(page: int = 1, category: Optional[str] = None,
                            page_size: int = 20) -> list[dict]:
    """
    Fetch trending / top-selling products from EchoTik product list API.

    Args:
        page:      Page number (1-indexed).
        category:  Optional category filter string.
        page_size: Products per page (max 50, default 20).

    Returns:
        List of dicts, each with the canonical field set.
    """
    # Sort fields: 1=total_sale, 2=revenue, 3=commission, 4=7d_sales, 5=video_count
    # Rotate sort field based on page to discover different products
    sort_fields = [4, 2, 3, 5, 1]  # 7d sales, revenue, commission, videos, total sales
    sort_field = sort_fields[(page - 1) % len(sort_fields)]

    params = {
        'region': 'US',
        'page_num': page,
        'page_size': min(page_size, 10),  # EchoTik API hard limit is 10
        'product_sort_field': sort_field,
        'sort_type': 1,            # Descending
    }
    if category:
        params['category'] = category

    data = _request('GET', f"{ECHOTIK_V3_BASE}/product/list", params=params)

    raw_list = data.get('data', [])
    if isinstance(raw_list, dict):
        raw_list = raw_list.get('list', [])

    return [_normalize_product(item) for item in (raw_list or [])]


# ---------------------------------------------------------------------------
# Public API — fetch_product_detail
# ---------------------------------------------------------------------------

def fetch_product_detail(product_id: str) -> Optional[dict]:
    """
    Fetch enriched detail for a single product.

    Args:
        product_id: Raw product ID (with or without ``shop_`` prefix).

    Returns:
        Normalized dict, or ``None`` if the product was not found.
    """
    raw_id = str(product_id).replace('shop_', '')

    data = _request('GET', f"{ECHOTIK_V3_BASE}/product/detail",
                    params={'product_ids': raw_id})

    payload = data.get('data')
    if not payload:
        return None

    if isinstance(payload, list):
        if not payload:
            return None
        payload = payload[0]

    return _normalize_product(payload)


# ---------------------------------------------------------------------------
# Public API — fetch_batch_images
# ---------------------------------------------------------------------------

def fetch_batch_images(cover_urls: list[str]) -> dict[str, str]:
    """
    Call EchoTik batch cover download to get signed/cached image URLs.

    Args:
        cover_urls: Up to 10 original TikTok CDN URLs.

    Returns:
        Dict mapping original URL -> signed URL.
    """
    trusted_domains = [
        'echosell-images', 'tiktokcdn.com', 'p16-shop',
        'p77-shop', 'byteimg.com', 'volces.com',
    ]
    valid = [u for u in cover_urls
             if u and any(dom in str(u) for dom in trusted_domains)]
    if not valid:
        return {}

    url_string = ','.join(valid[:10])
    try:
        data = _request('GET', f"{ECHOTIK_V3_BASE}/batch/cover/download",
                        params={'cover_urls': url_string})
        imgs = data.get('data', {})
        if isinstance(imgs, dict):
            return imgs
        if isinstance(imgs, list):
            result = {}
            for item in imgs:
                if isinstance(item, dict):
                    for orig, signed in item.items():
                        if signed and str(signed).startswith('http'):
                            result[orig] = signed
            return result
    except EchoTikError as exc:
        log.warning("Batch image signing failed: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Public API — fetch_product_videos
# ---------------------------------------------------------------------------

def fetch_product_videos(product_id: str, page_size: int = 10) -> list[dict]:
    """
    Fetch top videos for a product from EchoTik.

    Args:
        product_id: Raw product ID (without shop_ prefix).
        page_size: Number of videos to fetch (default 10).

    Returns:
        List of video dicts with normalized field names.
    """
    raw_id = str(product_id).replace('shop_', '')

    try:
        data = _request('GET', f"{ECHOTIK_V3_BASE}/product/video",
                        params={'product_id': raw_id, 'page_size': min(page_size, 10), 'page_num': 1})
    except EchoTikError as exc:
        log.warning("fetch_product_videos(%s) failed: %s", raw_id, exc)
        return []

    raw_list = data.get('data', [])
    if isinstance(raw_list, dict):
        raw_list = raw_list.get('list', [])

    videos = []
    for v in (raw_list or []):
        vid = {
            'video_id': str(v.get('video_id') or v.get('videoId') or v.get('id', '')),
            'video_url': v.get('video_url') or v.get('play_url') or v.get('share_url', ''),
            'cover_url': _extract_image_url(v.get('cover') or v.get('cover_url') or v.get('dynamic_cover')),
            'creator_name': v.get('author_name') or v.get('nickname') or v.get('authorName', ''),
            'creator_handle': v.get('author_unique_id') or v.get('unique_id') or v.get('authorUniqueId', ''),
            'creator_avatar': _extract_image_url(v.get('author_avatar') or v.get('avatar')),
            'view_count': _safe_int(v.get('play_count') or v.get('vv') or v.get('view_count')),
            'like_count': _safe_int(v.get('digg_count') or v.get('like_count') or v.get('likeCount')),
            'duration': _safe_int(v.get('duration') or v.get('video_duration') or v.get('duration_seconds')),
        }
        # Normalize duration: if >1000, assume milliseconds
        if vid['duration'] > 1000:
            vid['duration'] = vid['duration'] // 1000
        videos.append(vid)

    return videos


# ---------------------------------------------------------------------------
# Public API — fetch_top_shops
# ---------------------------------------------------------------------------

def fetch_top_shops(country: str = "US", page_size: int = 10, page: int = 1) -> list[dict]:
    """
    Fetch top TikTok Shop sellers from EchoTik v3.

    Endpoint: GET https://open.echotik.live/api/v3/echotik/seller/list
    Auth: HTTP Basic (ECHOTIK_USERNAME:PASSWORD) — same as product API.
    Response: {"code":0, "data": [{seller}, {seller}, ...]}
    """
    try:
        data = _request('GET', f"{ECHOTIK_V3_BASE}/seller/list",
                        params={"region": country, "page_num": page, "page_size": min(page_size, 10)})
    except EchoTikError as exc:
        log.warning("[EchoTik] seller/list failed: %s", exc)
        return []

    # Response data can be a list directly or {"list": [...]}
    raw = data.get("data") or []
    if isinstance(raw, dict):
        raw = raw.get("list") or raw.get("records") or []

    if not raw:
        log.info("[EchoTik] seller/list returned empty data")
        return []

    # Log first item keys so we can see the actual field names
    if raw:
        log.info("[EchoTik] seller/list: %d items, keys=%s", len(raw), list(raw[0].keys())[:20])

    # Normalize — use every possible field name from EchoTik seller response
    shops = []
    for s in raw:
        # ID — try every variant
        sid = str(s.get('shop_id') or s.get('seller_id') or s.get('shopId')
                  or s.get('sellerId') or s.get('id') or '')

        # Name
        name = (s.get('shop_name') or s.get('seller_name') or s.get('shopName')
                or s.get('sellerName') or s.get('name') or s.get('title') or '')

        # Avatar/cover
        avatar = _extract_image_url(
            s.get('cover_url') or s.get('avatar') or s.get('logo_url')
            or s.get('shop_logo') or s.get('seller_logo') or s.get('avatar_url')
        )

        # Category
        cat = (s.get('category_name') or s.get('categoryName')
               or s.get('first_category_name') or s.get('main_category')
               or s.get('category') or '')

        # Stats
        followers = _safe_int(
            s.get('total_fans_cnt') or s.get('follower_count') or s.get('followerCount')
            or s.get('fans_count') or s.get('fansCount')
        )
        gmv = _safe_float(
            s.get('total_sale_gmv_amt') or s.get('total_sale_gmv_30d_amt')
            or s.get('gmv_30d') or s.get('gmv') or s.get('sales_30d')
        )
        products = _safe_int(
            s.get('total_spu_cnt') or s.get('product_count') or s.get('productCount')
            or s.get('spu_cnt') or s.get('product_num')
        )
        score = _safe_float(
            s.get('trending_score') or s.get('score') or s.get('rank_score')
        )

        # Shop URL
        shop_url = s.get('shop_url') or s.get('shopUrl') or s.get('seller_url') or ''

        if sid:
            shops.append({
                'shop_id': sid, 'name': name, 'avatar_url': avatar,
                'country': s.get('country') or s.get('region') or country,
                'category': cat, 'follower_count': followers,
                'gmv_30d': gmv, 'product_count': products,
                'trending_score': score, 'shop_url': shop_url,
            })

    log.info("[EchoTik] Normalized %d sellers", len(shops))
    return shops


# ---------------------------------------------------------------------------
# Public API — fetch_brand_products
# ---------------------------------------------------------------------------

def fetch_brand_products(shop_id: str, page: int = 1, page_size: int = 10) -> list[dict]:
    """
    Fetch products for a specific seller/shop from EchoTik.

    Tries multiple endpoint + param name combos since EchoTik uses
    different field names across endpoints.
    """
    sid = str(shop_id)

    # Try different endpoint + param name combos
    attempts = [
        (f"{ECHOTIK_V3_BASE}/seller/product/list", {'seller_id': sid}),
        (f"{ECHOTIK_V3_BASE}/seller/product/list", {'shop_id': sid}),
        (f"{ECHOTIK_V3_BASE}/product/list", {'seller_id': sid}),
        (f"{ECHOTIK_V3_BASE}/product/list", {'shop_id': sid}),
    ]

    for url, id_param in attempts:
        try:
            params = {**id_param, 'page_num': page, 'page_size': min(page_size, 10)}
            data = _request('GET', url, params=params)
            raw = data.get('data') or []
            if isinstance(raw, dict):
                raw = raw.get('list') or raw.get('records') or []
            if raw:
                endpoint = url.split('/echotik/')[-1]
                log.info("[EchoTik] Got %d products for seller %s via %s (%s)",
                         len(raw), sid, endpoint, list(id_param.keys())[0])
                return [_normalize_product(p) for p in raw if p]
        except EchoTikError as exc:
            log.debug("[EchoTik] brand products %s failed: %s", url.split('/echotik/')[-1], exc)
            continue

    log.info("[EchoTik] No products found for seller %s via any endpoint", sid)
    return []


# ---------------------------------------------------------------------------
# Public API — fetch_product_trend
# ---------------------------------------------------------------------------

def fetch_product_trend(product_id: str, days: int = 30) -> list[dict]:
    """
    Fetch trend data for a product from EchoTik.

    Args:
        product_id: Raw product ID (without shop_ prefix).
        days: Number of days of history (default 30).

    Returns:
        List of dicts with 'date', 'sales', 'gmv' keys.
    """
    from datetime import timedelta

    raw_id = str(product_id).replace('shop_', '')
    end = datetime.utcnow()
    start = end - timedelta(days=days)

    try:
        data = _request('GET', f"{ECHOTIK_V3_BASE}/product/trend",
                        params={
                            'product_id': raw_id,
                            'start_date': start.strftime('%Y-%m-%d'),
                            'end_date': end.strftime('%Y-%m-%d'),
                            'page_num': 1,
                            'page_size': min(days, 10),
                        })
    except EchoTikError as exc:
        log.warning("fetch_product_trend(%s) failed: %s", raw_id, exc)
        return []

    raw_list = data.get('data', [])
    if isinstance(raw_list, dict):
        raw_list = raw_list.get('list') or raw_list.get('trend', [])

    trend = []
    for item in (raw_list or []):
        trend.append({
            'date': item.get('date') or item.get('day', ''),
            'sales': _safe_int(item.get('sale_cnt') or item.get('sales') or item.get('saleCnt')),
            'gmv': _safe_float(item.get('sale_gmv_amt') or item.get('gmv') or item.get('saleGmvAmt')),
        })

    return trend


# ---------------------------------------------------------------------------
# Public API — sync_to_db
# ---------------------------------------------------------------------------

def sync_to_db(products_list: list[dict]) -> dict:
    """
    Upsert a list of normalized product dicts into the Product model.

    Sets ``last_echotik_sync`` on every touched row and computes
    ``sales_velocity = sales_7d / max(video_count_7d, 1)``.

    Args:
        products_list: Output from ``fetch_trending_products`` or a list of
                       ``fetch_product_detail`` results.

    Returns:
        ``{'created': int, 'updated': int, 'errors': int}``
    """
    from app import db
    from app.models import Product
    from sqlalchemy.exc import IntegrityError

    created = 0
    updated = 0
    errors = 0
    now = datetime.utcnow()

    for p in products_list:
        if not p:
            continue
        try:
            raw_id = str(p.get('product_id', '')).replace('shop_', '')
            if not raw_id:
                errors += 1
                continue

            product_id = f"shop_{raw_id}"

            sales_7d = p.get('sales_7d', 0) or 0
            video_7d = p.get('video_count_7d', 0) or 0
            velocity = round(sales_7d / max(video_7d, 1), 2)

            # Use a savepoint so a single product failure doesn't poison the
            # whole session.  Also handles the check-then-act race: if two
            # workers both see "not exists" and try to insert, one will hit an
            # IntegrityError which we catch and convert to an update.
            nested = db.session.begin_nested()
            try:
                existing = Product.query.get(product_id)
                if existing:
                    _update_existing(existing, p, velocity, now)
                    updated += 1
                else:
                    _create_new(product_id, p, velocity, now, db)
                    created += 1
                nested.commit()
            except IntegrityError:
                nested.rollback()
                # Race: another worker inserted first — fall back to update
                existing = Product.query.get(product_id)
                if existing:
                    _update_existing(existing, p, velocity, now)
                    updated += 1
                else:
                    errors += 1
            except Exception:
                nested.rollback()
                raise

        except Exception:
            log.exception("sync_to_db error for product %s", p.get('product_id'))
            errors += 1

    try:
        db.session.commit()
    except Exception:
        log.exception("sync_to_db commit failed")
        db.session.rollback()
        raise

    # --- Post-sync: sign images in batches of 10 ---
    try:
        _sign_product_images(db)
    except Exception:
        log.exception("Post-sync image signing failed (non-fatal)")

    # --- Post-sync: enrich categories for products missing them ---
    try:
        _enrich_missing_categories(db)
    except Exception:
        log.exception("Post-sync category enrichment failed (non-fatal)")

    log.info("sync_to_db: created=%d updated=%d errors=%d", created, updated, errors)
    return {'created': created, 'updated': updated, 'errors': errors}


def _enrich_missing_categories(db):
    """Fetch categories from product detail API for products missing category."""
    from app.models import Product
    import time as _time

    products = Product.query.filter(
        db.or_(Product.category.is_(None), Product.category == ''),
        Product.sales_7d > 0,
    ).limit(20).all()

    if not products:
        return

    enriched = 0
    for p in products:
        try:
            raw_id = p.product_id.replace('shop_', '')
            detail = fetch_product_detail(raw_id)
            if detail and detail.get('category'):
                p.category = str(detail['category'])[:100]
                enriched += 1
            if detail and detail.get('subcategory') and not p.subcategory:
                p.subcategory = str(detail['subcategory'])[:100]
            _time.sleep(0.3)
        except Exception:
            continue

    if enriched:
        db.session.commit()
        log.info("Category enrichment: %d products updated", enriched)


def _sign_product_images(db):
    """Sign image URLs for products that don't have cached_image_url yet."""
    from app.models import Product

    products = Product.query.filter(
        Product.image_url.isnot(None),
        Product.cached_image_url.is_(None),
    ).limit(50).all()

    if not products:
        return

    # Build batches of 10
    for i in range(0, len(products), 10):
        batch = products[i:i + 10]
        urls = [p.image_url for p in batch if p.image_url and p.image_url.startswith('http')]
        if not urls:
            continue

        signed = fetch_batch_images(urls)
        if not signed:
            continue

        for p in batch:
            if p.image_url in signed:
                p.cached_image_url = signed[p.image_url][:500]
                log.debug("Signed image for %s", p.product_id)

    try:
        db.session.commit()
    except Exception:
        log.warning("Image signing commit failed")
        db.session.rollback()


# ---------------------------------------------------------------------------
# Internal — normalize a raw EchoTik API dict into canonical fields
# ---------------------------------------------------------------------------

def _normalize_product(d: dict) -> dict:
    """
    Map the messy EchoTik response (camelCase, snake_case, nested) into the
    canonical field set that the rest of PRISM expects.
    """
    # --- Sales ---
    sales = _safe_int(_pick(
        d.get('total_sale_cnt'), d.get('totalSaleCnt'),
        d.get('totalSale'), d.get('sales'), d.get('sale_cnt'),
    ))
    sales_7d = _safe_int(_pick(
        d.get('total_sale_7d_cnt'), d.get('totalSale7dCnt'),
        d.get('totalSale7d'), d.get('sales_7d'),
    ))
    sales_30d = _safe_int(_pick(
        d.get('total_sale_30d_cnt'), d.get('totalSale30dCnt'),
        d.get('totalSale30d'), d.get('sales_30d'),
    ))

    # --- GMV ---
    gmv = _safe_float(_pick(
        d.get('total_sale_gmv_amt'), d.get('totalSaleGmvAmt'),
        d.get('gmv'),
    ))
    gmv_30d = _safe_float(_pick(
        d.get('total_sale_gmv_30d_amt'), d.get('totalSaleGmv30dAmt'),
        d.get('gmv_30d'),
    ))

    # --- Videos / influencers ---
    influencer_count = _safe_int(_pick(
        d.get('total_ifl_cnt'), d.get('totalIflCnt'),
        d.get('influencer_count'), d.get('ifl_cnt'),
    ))
    video_count_alltime = _safe_int(_pick(
        d.get('total_video_cnt'), d.get('totalVideoCnt'),
        d.get('video_count'),
    ))
    video_7d = _safe_int(_pick(
        d.get('total_video_7d_cnt'), d.get('totalVideo7dCnt'),
        d.get('video_7d'),
    ))
    video_30d = _safe_int(_pick(
        d.get('total_video_30d_cnt'), d.get('totalVideo30dCnt'),
        d.get('video_30d'),
    ))
    live_count = _safe_int(_pick(
        d.get('total_live_cnt'), d.get('totalLiveCnt'),
        d.get('live_count'),
    ))
    views_count = _safe_int(_pick(
        d.get('total_views_cnt'), d.get('totalViewsCnt'),
        d.get('views_count'),
    ))

    # --- Pricing / commission ---
    price = _safe_float(_pick(
        d.get('spu_avg_price'), d.get('spuAvgPrice'),
        d.get('price'), d.get('avg_price'),
    ))
    original_price = _safe_float(d.get('original_price') or d.get('originalPrice'))

    commission = _safe_float(_pick(
        d.get('product_commission_rate'), d.get('productCommissionRate'),
        d.get('commission_rate'), d.get('commission'),
    ))
    # Normalize to 0-1 range if reported as percentage
    if commission > 1:
        commission /= 100.0

    # --- Quality ---
    rating = _safe_float(_pick(
        d.get('product_rating'), d.get('productRating'),
        d.get('rating'), d.get('star'), d.get('score'),
    ))
    review_count = _safe_int(_pick(
        d.get('review_count'), d.get('reviewCnt'),
        d.get('reviews'), d.get('total_review_cnt'),
    ))
    return_rate = _safe_float(d.get('return_rate') or d.get('returnRate'))

    # --- Ad spend ---
    ad_spend = _safe_float(d.get('ad_spend') or d.get('periodAdSpend'))

    # --- Identity ---
    product_id = str(
        d.get('product_id') or d.get('productId') or d.get('id') or ''
    ).replace('shop_', '')

    product_name = _pick(
        d.get('title'), d.get('productTitle'), d.get('product_title'),
        d.get('productName'), d.get('product_name'), d.get('name'),
    )

    # Image — may be a dict, list of dicts, or JSON string
    raw_img = _pick(
        d.get('product_image'), d.get('image'), d.get('cover'),
        d.get('image_url'), d.get('cover_url'), d.get('product_img_url'),
    )
    image_url = _extract_image_url(raw_img)

    product_url = _pick(d.get('product_url'), d.get('productUrl'))
    if not product_url and product_id:
        product_url = (
            f"https://shop.tiktok.com/view/product/{product_id}"
            f"?region=US&locale=en-US"
        )

    # --- Seller ---
    seller_name = _pick(
        d.get('seller_name'), d.get('shop_name'), d.get('shopName'),
        d.get('sellerName'), d.get('store_name'), d.get('brandName'),
        d.get('brand_name'), d.get('advertiser'),
    )
    # Check nested seller object
    seller_obj = d.get('seller') or d.get('shop')
    if not seller_name and isinstance(seller_obj, dict):
        seller_name = seller_obj.get('name') or seller_obj.get('shop_name')

    seller_id = _pick(
        d.get('seller_id'), d.get('shop_id'), d.get('sellerId'),
    )

    # --- Category ---
    # EchoTik uses many different key names across endpoints
    category = _pick(
        d.get('category'), d.get('category_name'), d.get('categoryName'),
        d.get('first_category_name'), d.get('firstCategoryName'),
        d.get('cate_name'), d.get('cateName'),
        d.get('product_category'), d.get('productCategory'),
    )
    # Check nested category objects
    if not category:
        cat_obj = d.get('category_info') or d.get('categoryInfo') or d.get('cate') or {}
        if isinstance(cat_obj, dict):
            category = cat_obj.get('name') or cat_obj.get('category_name') or cat_obj.get('first_name')
        elif isinstance(cat_obj, str):
            category = cat_obj
    # Check first_cate_list array
    if not category:
        cate_list = d.get('first_cate_list') or d.get('firstCateList') or d.get('cate_list') or []
        if isinstance(cate_list, list) and cate_list:
            first = cate_list[0]
            if isinstance(first, dict):
                category = first.get('name') or first.get('cate_name')
            elif isinstance(first, str):
                category = first

    # Fallback: map category_id to a name using the TikTok category lookup
    if not category:
        cat_id = str(d.get('category_id') or d.get('categoryId') or d.get('first_cate_id') or '')
        if cat_id and cat_id in TIKTOK_CATEGORIES:
            category = TIKTOK_CATEGORIES[cat_id]
        elif cat_id:
            # Try parent category (first 4 digits)
            parent_id = cat_id[:6] if len(cat_id) >= 6 else cat_id
            category = TIKTOK_CATEGORIES.get(parent_id)

    subcategory = _pick(
        d.get('subcategory'), d.get('sub_category'),
        d.get('second_category_name'), d.get('secondCategoryName'),
        d.get('sub_cate_name'), d.get('subCateName'),
    )

    return {
        'product_id': product_id,
        'product_name': product_name,
        'seller_name': seller_name,
        'seller_id': seller_id,
        'price': price,
        'original_price': original_price,
        'sales': sales,
        'sales_7d': sales_7d,
        'sales_30d': sales_30d,
        'gmv': gmv,
        'gmv_30d': gmv_30d,
        'video_count_7d': video_7d,
        'video_count_alltime': video_count_alltime,
        'video_30d': video_30d,
        'influencer_count': influencer_count,
        'commission_rate': commission,
        'ad_spend': ad_spend,
        'image_url': image_url,
        'product_url': product_url,
        'category': category,
        'subcategory': subcategory,
        'return_rate': return_rate,
        'rating': rating,
        'review_count': review_count,
        'live_count': live_count,
        'views_count': views_count,
    }


# ---------------------------------------------------------------------------
# Internal — DB upsert helpers
# ---------------------------------------------------------------------------

def _update_existing(product, p: dict, velocity: float, now: datetime):
    """Update an existing Product row with fresh EchoTik data."""
    price = p.get('price', 0) or 0
    sales = p.get('sales', 0) or 0
    s7d = p.get('sales_7d', 0) or 0
    s30d = p.get('sales_30d', 0) or 0
    inf = p.get('influencer_count', 0) or 0
    comm = p.get('commission_rate', 0) or 0
    v_alltime = p.get('video_count_alltime', 0) or 0

    # Only overwrite if the new value is better or the existing is empty
    if price > 0 or not product.price:
        product.price = price
    if sales > 0 or not product.sales:
        product.sales = sales
    if s7d > 0 or not product.sales_7d:
        product.sales_7d = s7d
    if s30d > 0 or not product.sales_30d:
        product.sales_30d = s30d
    if inf > 0 and not product.influencer_count:
        product.influencer_count = inf
    if comm > 0 or not product.commission_rate:
        product.commission_rate = comm

    # Video counts — never downgrade all-time
    if v_alltime > (product.video_count_alltime or 0):
        product.video_count_alltime = v_alltime
        product.video_count = v_alltime

    product.video_7d = p.get('video_count_7d') or product.video_7d or 0
    product.video_30d = p.get('video_30d') or product.video_30d or 0
    product.live_count = p.get('live_count') or product.live_count or 0
    product.views_count = p.get('views_count') or product.views_count or 0
    product.product_rating = p.get('rating') or product.product_rating or 0
    product.review_count = p.get('review_count') or product.review_count or 0

    product.gmv = p.get('gmv') or product.gmv or 0
    product.gmv_30d = p.get('gmv_30d') or product.gmv_30d or 0
    product.ad_spend = p.get('ad_spend') or product.ad_spend or 0

    # Name / seller — only update if current is unknown
    name = (p.get('product_name') or '').strip()[:500]
    if name and name.lower() not in ('', 'unknown', 'none', 'null'):
        product.product_name = name

    img = p.get('image_url')
    if img:
        product.image_url = str(img)[:500]

    url = p.get('product_url')
    if url:
        product.product_url = str(url)[:500]

    sname = (p.get('seller_name') or '').strip()
    if sname and sname.lower() not in ('', 'unknown', 'none', 'null'):
        if not product.seller_name or product.seller_name == 'Unknown':
            product.seller_name = sname

    sid = p.get('seller_id')
    if sid and not product.seller_id:
        product.seller_id = sid

    # New fields
    product.category = p.get('category') or product.category
    product.subcategory = p.get('subcategory') or product.subcategory
    product.return_rate = p.get('return_rate') or product.return_rate
    product.rating = p.get('rating') or product.rating

    product.sales_velocity = velocity
    product.last_echotik_sync = now
    product.last_updated = now


def _create_new(product_id: str, p: dict, velocity: float, now: datetime, db):
    """Insert a new Product row from EchoTik data."""
    from app.models import Product

    raw_id = product_id.replace('shop_', '')
    product_url = p.get('product_url') or (
        f"https://shop.tiktok.com/view/product/{raw_id}?region=US&locale=en-US"
    )

    seller_name = (p.get('seller_name') or '').strip()
    if not seller_name or seller_name.lower() in ('unknown', 'none', 'null', ''):
        seller_name = 'Unknown'

    v_alltime = p.get('video_count_alltime', 0) or 0

    product = Product(
        product_id=product_id,
        product_name=(p.get('product_name') or 'Unknown Product')[:500],
        image_url=(p.get('image_url') or '')[:500] or None,
        product_url=(product_url or '')[:500] or None,
        price=p.get('price', 0) or 0,
        original_price=p.get('original_price', 0) or 0,
        sales=p.get('sales', 0) or 0,
        sales_7d=p.get('sales_7d', 0) or 0,
        sales_30d=p.get('sales_30d', 0) or 0,
        gmv=p.get('gmv', 0) or 0,
        gmv_30d=p.get('gmv_30d', 0) or 0,
        influencer_count=p.get('influencer_count', 0) or 0,
        commission_rate=p.get('commission_rate', 0) or 0,
        video_count=v_alltime,
        video_count_alltime=v_alltime,
        video_7d=p.get('video_count_7d', 0) or 0,
        video_30d=p.get('video_30d', 0) or 0,
        live_count=p.get('live_count', 0) or 0,
        views_count=p.get('views_count', 0) or 0,
        product_rating=p.get('rating', 0) or 0,
        review_count=p.get('review_count', 0) or 0,
        ad_spend=p.get('ad_spend', 0) or 0,
        seller_name=seller_name,
        seller_id=p.get('seller_id'),
        scan_type='echotik_trending',
        category=p.get('category'),
        subcategory=p.get('subcategory'),
        return_rate=p.get('return_rate'),
        rating=p.get('rating'),
        sales_velocity=velocity,
        last_echotik_sync=now,
        first_seen=now,
    )
    db.session.add(product)
