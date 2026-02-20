"""
Snap2List API Bridge — Direct API integration for eBay listing creation.
Bypasses Playwright entirely. Uses Snap2List's Gemini AI for listing generation
and their eBay Inventory API integration for posting.

Endpoints used:
  POST /api/image/upload              → Upload images to ImageKit CDN
  POST /api/generate-title-description-gemini → AI listing generation
  POST /api/get-category-suggestions  → eBay category auto-detect
  POST /api/get-suggested-price       → Smart pricing from eBay sold data
  POST /api/create-listing            → Create eBay listing
  POST /api/get-listing-fees          → Calculate eBay fees
"""

import os
import io
import json
import time
import logging
import requests
from datetime import datetime

log = logging.getLogger('Snap2List')

SNAP2LIST_BASE = 'https://www.snaptolist.com'
CLERK_BASE = 'https://clerk.snaptolist.com'

# Default package for TikTok samples (overridable per listing)
DEFAULT_WEIGHT = {"value": 1, "unit": "POUND"}
DEFAULT_DIMENSIONS = {"length": 9, "width": 6, "height": 4, "unit": "INCH"}


def _headers(session_jwt, content_type='application/json'):
    """Build request headers with Clerk JWT auth."""
    h = {
        'accept': 'application/json, text/plain, */*',
        'authorization': f'Bearer {session_jwt}' if session_jwt else '',
        'origin': SNAP2LIST_BASE,
        'referer': f'{SNAP2LIST_BASE}/dashboard/create-listing',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
    }
    if content_type:
        h['content-type'] = content_type
    return h


def _cookies(session_jwt, client_cookie=None):
    """Build cookie dict with Clerk session."""
    c = {
        '__session': session_jwt,
        '__session_nwBmEOgz': session_jwt,
        'cookieConsent': 'accepted',
    }
    if client_cookie:
        c['__client'] = client_cookie
        c['__client_uat_nwBmEOgz'] = str(int(time.time()))
        c['__client_uat'] = str(int(time.time()))
    return c


def refresh_session(client_cookie, session_id):
    """
    Refresh the Clerk JWT using the __client cookie.
    Returns a fresh __session JWT string, or None on failure.
    """
    url = f'{CLERK_BASE}/v1/client/sessions/{session_id}/tokens'
    params = {'__clerk_api_version': '2025-11-10'}
    cookies = {'__client': client_cookie}
    headers = {
        'accept': '*/*',
        'origin': SNAP2LIST_BASE,
        'referer': f'{SNAP2LIST_BASE}/',
        'content-type': 'application/x-www-form-urlencoded',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
    }

    try:
        resp = requests.post(url, params=params, cookies=cookies, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            jwt = data.get('jwt')
            if jwt:
                log.info(f"Clerk session refreshed (JWT len={len(jwt)})")
                return jwt
            log.warning(f"Clerk response missing JWT: {json.dumps(data)[:200]}")
        else:
            log.error(f"Clerk refresh failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"Clerk refresh error: {e}")

    return None


def upload_image(session_jwt, image_bytes, filename='product.jpg', content_type='image/jpeg', client_cookie=None):
    """
    Upload an image to Snap2List's ImageKit CDN.
    Returns the CDN URL string, or None on failure.
    """
    url = f'{SNAP2LIST_BASE}/api/image/upload'
    files = {'file': (filename, io.BytesIO(image_bytes), content_type)}

    try:
        resp = requests.post(
            url,
            files=files,
            cookies=_cookies(session_jwt, client_cookie),
            headers=_headers(session_jwt, content_type=None),  # multipart sets its own
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Snap2List returns 'urls' (list) not 'url' (string)
            cdn_url = data.get('url') or data.get('imageUrl') or data.get('image_url')
            # Check for 'urls' (plural) list format
            if not cdn_url:
                urls_list = data.get('urls') or data.get('originalUrls')
                if isinstance(urls_list, list) and urls_list:
                    cdn_url = urls_list[0]
            if cdn_url and isinstance(cdn_url, str):
                log.info(f"Image uploaded: {cdn_url[:80]}")
                return cdn_url
            # Fallback: return full response for debugging
            log.warning(f"Upload OK but could not extract URL: {json.dumps(data)[:400]}")
            return None
        else:
            log.error(f"Upload failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"Upload error: {e}")

    return None


def generate_listing(session_jwt, image_urls, fast_mode=False):
    """
    Use Snap2List's Gemini AI to generate title, description, aspects, etc.
    Returns dict with 'title', 'description', 'aspects', 'category', etc.
    """
    url = f'{SNAP2LIST_BASE}/api/generate-title-description-gemini'
    payload = {
        'images': image_urls,
        'flawImageIndices': [],
        'flawDescriptions': {},
        'fastMode': fast_mode,
        'marketplace': 'EBAY_US',
        'skipTitleStyleReferences': False,
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            cookies=_cookies(session_jwt),
            headers=_headers(session_jwt),
            timeout=120,  # AI generation can take up to 60s
        )
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"AI generated listing: title='{data.get('title', '')[:60]}...'")
            return data
        else:
            log.error(f"Generate failed: {resp.status_code} {resp.text[:300]}")
    except Exception as e:
        log.error(f"Generate error: {e}")

    return None


def get_category_suggestions(session_jwt, ebay_token, title):
    """Get eBay category suggestions for a title."""
    url = f'{SNAP2LIST_BASE}/api/get-category-suggestions'
    payload = {
        'token': ebay_token,
        'title': title,
        'locale': 'en-US',
        'marketplaceId': 'EBAY_US',
        'categoryTreeId': '0',
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            cookies=_cookies(session_jwt),
            headers=_headers(session_jwt),
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"Category suggestions: {json.dumps(data)[:200]}")
            return data
        else:
            log.error(f"Category suggest failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"Category suggest error: {e}")

    return None


def get_suggested_price(session_jwt, ebay_token, title, category_id, condition='NEW'):
    """Get smart pricing suggestion from eBay sold data."""
    url = f'{SNAP2LIST_BASE}/api/get-suggested-price'
    payload = {
        'listing': {'product': {'title': title}},
        'token': ebay_token,
        'condition': condition,
        'selectedCategory': str(category_id),
        'marketplace_id': 'EBAY_US',
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            cookies=_cookies(session_jwt),
            headers=_headers(session_jwt),
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"Price suggestion: {json.dumps(data)[:200]}")
            return data
        else:
            log.error(f"Price suggest failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"Price suggest error: {e}")

    return None


def create_listing(session_jwt, ebay_token, listing_data, account_id, user_id,
                   fulfillment_policy_id, payment_policy_id, return_policy_id):
    """
    Create an eBay listing via Snap2List's API.

    listing_data should contain:
      - title (str)
      - description (str, HTML)
      - price (float)
      - sku (str)
      - category_id (str)
      - condition (str, e.g. 'NEW')
      - image_urls (list of CDN URLs)
      - aspects (dict, e.g. {'Brand': ['Decor'], ...})
      - weight (dict, optional, default 1 lb)
      - dimensions (dict, optional, default 9x6x4)
      - quantity (int, default 1)
    """
    url = f'{SNAP2LIST_BASE}/api/create-listing'

    title = listing_data['title']
    description = listing_data.get('description', '')
    price = str(listing_data.get('price', '10'))
    sku = listing_data.get('sku', f'S2L-{int(time.time())}')
    category_id = str(listing_data.get('category_id', ''))
    condition = listing_data.get('condition', 'NEW')
    image_urls = listing_data.get('image_urls', [])
    aspects = listing_data.get('aspects', {})
    weight = listing_data.get('weight', DEFAULT_WEIGHT)
    dimensions = listing_data.get('dimensions', DEFAULT_DIMENSIONS)
    quantity = listing_data.get('quantity', 1)

    payload = {
        'token': ebay_token,
        'inventoryItem': {
            'sku': sku,
            'product': {
                'title': title,
                'description': description,
                'aspects': aspects,
                'imageUrls': image_urls,
                'upc': ['Does not apply'],
            },
            'condition': condition,
            'availability': {
                'shipToLocationAvailability': {
                    'quantity': quantity,
                    'merchantLocationKey': 'home',
                }
            },
            'categoryId': category_id,
            'countryCode': 'US',
            'packageWeightAndSize': {
                'weight': weight,
                'dimensions': dimensions,
            },
        },
        'offerDetails': {
            'sku': sku,
            'marketplaceId': 'EBAY_US',
            'format': 'FIXED_PRICE',
            'listingDescription': description,
            'availableQuantity': quantity,
            'pricingSummary': {
                'price': {'value': price},
            },
            'listingPolicies': {
                'fulfillmentPolicyId': fulfillment_policy_id,
                'paymentPolicyId': payment_policy_id,
                'returnPolicyId': return_policy_id,
                'bestOfferTerms': {'bestOfferEnabled': True},
            },
            'merchantLocationKey': 'home',
            'categoryId': category_id,
        },
        'userId': user_id,
        'marketplace_id': 'EBAY_US',
        'accountId': account_id,
        'storeCategoryNames': ['Other'],
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            cookies=_cookies(session_jwt),
            headers=_headers(session_jwt),
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"Listing created! Response: {json.dumps(data)[:300]}")
            return data
        else:
            log.error(f"Create listing failed: {resp.status_code} {resp.text[:500]}")
            return {'error': f"Snap2List returned {resp.status_code}: {resp.text[:300]}"}
    except Exception as e:
        log.error(f"Create listing error: {e}")
        return {'error': str(e)}


def full_listing_flow(session_jwt, ebay_token, image_bytes_list, account_id, user_id,
                      fulfillment_policy_id, payment_policy_id, return_policy_id,
                      price_override=None, grok_price=None, fast_mode=False):
    """
    Complete one-tap listing flow:
    1. Upload images → CDN URLs
    2. Gemini AI → title + description + aspects + category
    3. Create listing on eBay

    Returns dict with success status, listing URL, or error.
    """
    result = {'success': False, 'step': 'init'}

    # Step 1: Upload images
    result['step'] = 'upload_images'
    image_urls = []
    for i, (img_bytes, filename) in enumerate(image_bytes_list):
        cdn_url = upload_image(session_jwt, img_bytes, filename=filename)
        if cdn_url and isinstance(cdn_url, str):
            image_urls.append(cdn_url)
            log.info(f"Uploaded image {i+1}/{len(image_bytes_list)}")
        else:
            return {**result, 'error': f'Failed to upload image {i+1}'}

    if not image_urls:
        return {**result, 'error': 'No images uploaded'}

    # Step 2: Generate listing via Gemini AI
    result['step'] = 'generate_listing'
    ai_data = generate_listing(session_jwt, image_urls, fast_mode=fast_mode)
    if not ai_data:
        return {**result, 'error': 'AI listing generation failed'}

    title = ai_data.get('title', '')
    description = ai_data.get('description', '')
    aspects = ai_data.get('aspects', {})
    category_id = ai_data.get('categoryId') or ai_data.get('category_id', '')

    if not title:
        return {**result, 'error': 'AI did not generate a title'}

    # Step 3: Determine price
    result['step'] = 'pricing'
    price = price_override or grok_price or '15'  # Default fallback

    # Step 4: Create listing
    result['step'] = 'create_listing'
    listing_data = {
        'title': title,
        'description': description,
        'price': price,
        'sku': f'S2L-{int(time.time())}',
        'category_id': str(category_id),
        'condition': 'NEW',
        'image_urls': image_urls,
        'aspects': aspects,
    }

    create_result = create_listing(
        session_jwt, ebay_token, listing_data,
        account_id, user_id,
        fulfillment_policy_id, payment_policy_id, return_policy_id,
    )

    if create_result and not create_result.get('error'):
        result['success'] = True
        result['step'] = 'complete'
        result['listing'] = create_result
        result['title'] = title
        result['price'] = price
        result['images_uploaded'] = len(image_urls)
        return result
    else:
        result['error'] = create_result.get('error', 'Unknown error creating listing')
        return result
