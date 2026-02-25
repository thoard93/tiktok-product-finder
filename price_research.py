"""
PriceBlade — Premium Price Research Engine
AI-powered multi-source price research for zero-cost TikTok samples.
Uses Grok 4.1 fast reasoning with vision for product identification & pricing.
"""

import os
import json
import io
import logging
import requests
import base64
import re
from datetime import datetime
from flask import request, jsonify, send_from_directory

from app import app, db

log = logging.getLogger('PriceBlade')

# ─── Configuration ───────────────────────────────────────────────────────────
XAI_API_KEY = os.environ.get('XAI_API_KEY', '')
XAI_API_URL = 'https://api.x.ai/v1/chat/completions'
XAI_MODEL = os.environ.get('XAI_MODEL', 'grok-4-1-fast-reasoning')

# ─── Discount Ladder (competitive undercut vs TikTok Shop) ────────────────────
# We want to undercut TikTok Shop but not by so much that we leave money on table.
# These are % below the MEDIAN price across sources (not the lowest).
DISCOUNT_LADDER = {
    'conservative': {15: 0.17, 30: 0.20, 60: 0.20, 100: 0.23, 9999: 0.25},
    'balanced':     {15: 0.23, 30: 0.27, 60: 0.27, 100: 0.30, 9999: 0.33},
    'aggressive':   {15: 0.33, 30: 0.37, 60: 0.35, 100: 0.39, 9999: 0.43},
}

def get_discount(price, aggressiveness='conservative'):
    """Get discount percentage based on price tier and aggressiveness."""
    ladder = DISCOUNT_LADDER.get(aggressiveness, DISCOUNT_LADDER['conservative'])
    for threshold, discount in sorted(ladder.items()):
        if price <= threshold:
            return discount
    return 0.30  # fallback


# ─── USPS Ground Advantage Cost Table ────────────────────────────────────────
# Approximate rates for USPS Ground Advantage (varies by zone, these are averages)
USPS_GROUND_RATES = [
    (4,   4.00),   # ≤ 4oz
    (8,   4.65),   # ≤ 8oz
    (12,  5.15),   # ≤ 12oz
    (16,  5.65),   # ≤ 1lb
    (32,  7.00),   # ≤ 2lb
    (48,  8.50),   # ≤ 3lb
    (80,  10.50),  # ≤ 5lb
    (160, 14.00),  # ≤ 10lb
    (320, 18.00),  # ≤ 20lb
]

def get_usps_shipping_cost(weight_oz):
    """Get estimated USPS Ground Advantage cost based on weight in ounces."""
    for max_oz, cost in USPS_GROUND_RATES:
        if weight_oz <= max_oz:
            return cost
    return 20.00  # fallback for heavy items


# ─── BrightData TikTok Shop Price Lookup ─────────────────────────────────────
BRIGHTDATA_PROXY_HOST = os.environ.get('BRIGHTDATA_PROXY_HOST', 'brd.superproxy.io')
BRIGHTDATA_PROXY_PORT = os.environ.get('BRIGHTDATA_PROXY_PORT', '33335')
BRIGHTDATA_PROXY_USER = os.environ.get('BRIGHTDATA_PROXY_USER', '')
BRIGHTDATA_PROXY_PASS = os.environ.get('BRIGHTDATA_PROXY_PASS', '')


def search_tiktok_shop(product_name, brand=''):
    """
    Search TikTok Shop for a product and return the real price.
    Uses BrightData Web Unlocker to bypass TikTok's JS rendering.
    
    Returns: dict with {price, name, url, verified} or None on failure
    """
    if not BRIGHTDATA_PROXY_USER or not BRIGHTDATA_PROXY_PASS:
        log.warning("TikTok Shop lookup skipped: no BrightData proxy credentials")
        return None
    
    # Build optimized search query (brand + key product words)
    search_query = product_name.strip()
    if brand and brand.lower() not in search_query.lower():
        search_query = f"{brand} {search_query}"
    
    # Clean up search query: remove size/count details that may hurt matching
    # Keep brand + product name, drop "8.4oz", "Full Size", "Pack of 3", etc.
    clean_query = re.sub(r'\b\d+(\.\d+)?\s*(oz|ml|fl|g|mg|count|pack|ct|lb|kg)\b', '', search_query, flags=re.IGNORECASE)
    clean_query = re.sub(r'\b(full size|travel size|trial size|sample)\b', '', clean_query, flags=re.IGNORECASE)
    clean_query = re.sub(r'\s+', ' ', clean_query).strip()
    
    from urllib.parse import quote_plus
    search_url = f"https://www.tiktok.com/shop/search?q={quote_plus(clean_query)}"
    
    proxy_url = f"http://{BRIGHTDATA_PROXY_USER}:{BRIGHTDATA_PROXY_PASS}@{BRIGHTDATA_PROXY_HOST}:{BRIGHTDATA_PROXY_PORT}"
    proxies = {
        'http': proxy_url,
        'https': proxy_url,
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    try:
        import time as _time
        _start = _time.time()
        log.info(f"TikTok Shop search: '{clean_query}' via BrightData")
        
        resp = requests.get(
            search_url,
            headers=headers,
            proxies=proxies,
            timeout=30,
            verify=False,  # BrightData proxy SSL
        )
        
        elapsed = round(_time.time() - _start, 1)
        log.info(f"TikTok Shop response: {resp.status_code} in {elapsed}s, {len(resp.text)} bytes")
        
        if resp.status_code != 200:
            log.warning(f"TikTok Shop search failed: {resp.status_code}")
            return None
        
        html = resp.text
        
        # Parse prices from the rendered HTML
        # TikTok Shop search results typically have price in structured data or product cards
        prices_found = []
        
        # Method 1: Look for JSON-LD structured data
        json_ld_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
        for jld in json_ld_matches:
            try:
                data = json.loads(jld)
                if isinstance(data, dict) and data.get('offers'):
                    offers = data['offers'] if isinstance(data['offers'], list) else [data['offers']]
                    for offer in offers:
                        price = offer.get('price') or offer.get('lowPrice')
                        if price:
                            prices_found.append({
                                'price': float(price),
                                'name': data.get('name', ''),
                                'source': 'json_ld'
                            })
            except:
                pass
        
        # Method 2: Look for price patterns in the HTML (common TikTok Shop patterns)
        # Prices like $18.00, $24.99 etc in product cards
        price_patterns = re.findall(
            r'(?:"price"|"salePrice"|"currentPrice"|price["\s:]+)\s*[":]*\s*\$?(\d+\.?\d{0,2})',
            html, re.IGNORECASE
        )
        for p in price_patterns:
            try:
                pf = float(p)
                if 1.0 < pf < 500:  # Reasonable price range
                    prices_found.append({'price': pf, 'name': '', 'source': 'html_pattern'})
            except:
                pass
        
        # Method 3: Look for __NEXT_DATA__ or similar SSR data blocks
        next_data = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if next_data:
            try:
                nd = json.loads(next_data.group(1))
                # Traverse for product price data
                nd_str = json.dumps(nd)
                nd_prices = re.findall(r'"(?:price|salePrice|originalPrice)":\s*"?(\d+\.?\d{0,2})"?', nd_str)
                for p in nd_prices:
                    pf = float(p)
                    if 1.0 < pf < 500:
                        prices_found.append({'price': pf, 'name': '', 'source': 'next_data'})
            except:
                pass
        
        # Method 4: Look for SIGI_STATE or similar TikTok data blocks
        sigi_match = re.search(r'window\[.SIGI_STATE.\]\s*=\s*({.*?});?\s*</script>', html, re.DOTALL)
        if not sigi_match:
            sigi_match = re.search(r'"ItemModule":\s*({.*?})\s*[,}]', html, re.DOTALL)
        if sigi_match:
            try:
                sigi_str = sigi_match.group(1)[:10000]  # Limit size
                sigi_prices = re.findall(r'"(?:price|salePrice)":\s*"?(\d+\.?\d{0,2})"?', sigi_str)
                for p in sigi_prices:
                    pf = float(p)
                    if 1.0 < pf < 500:
                        prices_found.append({'price': pf, 'name': '', 'source': 'sigi_state'})
            except:
                pass
        
        if not prices_found:
            log.info(f"TikTok Shop: no prices found in HTML ({len(html)} bytes)")
            # Log a snippet for debugging
            log.debug(f"TikTok HTML snippet: {html[:2000]}")
            return None
        
        # Get the most common / median price (filter outliers)
        price_values = [p['price'] for p in prices_found]
        # Use the most common price (mode)
        from collections import Counter
        price_counter = Counter(price_values)
        most_common_price = price_counter.most_common(1)[0][0]
        
        result = {
            'price': most_common_price,
            'name': next((p['name'] for p in prices_found if p['name']), clean_query),
            'url': search_url,
            'verified': True,
            'source_method': prices_found[0]['source'],
            'prices_found': len(prices_found),
        }
        log.info(f"TikTok Shop VERIFIED: ${most_common_price} for '{clean_query}' ({len(prices_found)} price signals)")
        return result
        
    except requests.exceptions.Timeout:
        log.warning(f"TikTok Shop lookup timed out after 30s")
        return None
    except Exception as e:
        log.error(f"TikTok Shop lookup error: {e}")
        return None


# =============================================================================
# DATABASE MODELS
# =============================================================================

class PriceResearch(db.Model):
    """Saved price research result."""
    __tablename__ = 'price_research'
    id = db.Column(db.Integer, primary_key=True)
    images = db.Column(db.Text, default='[]')        # JSON list of image URLs/data
    products = db.Column(db.Text, default='[]')       # JSON: identified products
    prices = db.Column(db.Text, default='{}')         # JSON: price comparison data
    recommended_price = db.Column(db.Float, default=0)
    is_bundle = db.Column(db.Boolean, default=False)
    aggressiveness = db.Column(db.String(20), default='conservative')
    team = db.Column(db.String(20), default='thoard') # Team: thoard or reol
    raw_response = db.Column(db.Text, default='')     # Full Grok response for debugging
    notes = db.Column(db.Text, default='')              # User notes
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        days_ago = (datetime.utcnow() - self.created_at).days if self.created_at else 999
        if days_ago <= 7:
            freshness = 'fresh'
        elif days_ago <= 30:
            freshness = 'stale'
        else:
            freshness = 'old'
        return {
            'id': self.id,
            'products': json.loads(self.products or '[]'),
            'prices': json.loads(self.prices or '{}'),
            'recommended_price': self.recommended_price,
            'is_bundle': self.is_bundle,
            'aggressiveness': self.aggressiveness,
            'team': self.team or 'thoard',
            'notes': self.notes or '',
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'days_ago': days_ago,
            'freshness': freshness,
        }


class PriceSettings(db.Model):
    """App settings."""
    __tablename__ = 'price_settings'
    id = db.Column(db.Integer, primary_key=True)
    aggressiveness = db.Column(db.String(20), default='conservative')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class EbayListing(db.Model):
    """Track eBay listings and sales."""
    __tablename__ = 'ebay_listings'
    id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(300), default='')
    product_details = db.Column(db.String(500), default='')
    status = db.Column(db.String(20), default='listed')  # listed, sold, shipped
    list_price = db.Column(db.Float, default=0)
    sold_price = db.Column(db.Float, default=0)
    shipping_cost = db.Column(db.Float, default=0)
    ebay_fees = db.Column(db.Float, default=0)
    profit = db.Column(db.Float, default=0)
    team = db.Column(db.String(20), default='thoard')
    notes = db.Column(db.Text, default='')
    research_id = db.Column(db.Integer, nullable=True)
    ebay_item_id = db.Column(db.String(50), default='')
    order_number = db.Column(db.String(50), default='')
    buyer_name = db.Column(db.String(100), default='')
    buyer_address = db.Column(db.String(300), default='')
    tracking_number = db.Column(db.String(100), default='')
    shipping_service = db.Column(db.String(50), default='')
    weight_oz = db.Column(db.Float, nullable=True)
    length_in = db.Column(db.Float, nullable=True)
    width_in = db.Column(db.Float, nullable=True)
    height_in = db.Column(db.Float, nullable=True)
    package_type = db.Column(db.String(30), default='')
    listed_at = db.Column(db.DateTime, default=datetime.utcnow)
    sold_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'product_name': self.product_name,
            'product_details': self.product_details,
            'status': self.status,
            'list_price': self.list_price,
            'sold_price': self.sold_price,
            'shipping_cost': self.shipping_cost,
            'ebay_fees': self.ebay_fees,
            'profit': self.profit,
            'team': self.team,
            'notes': self.notes,
            'research_id': self.research_id,
            'ebay_item_id': self.ebay_item_id,
            'order_number': self.order_number or '',
            'buyer_name': self.buyer_name or '',
            'buyer_address': self.buyer_address or '',
            'tracking_number': self.tracking_number or '',
            'shipping_service': self.shipping_service or '',
            'weight_oz': self.weight_oz,
            'length_in': self.length_in,
            'width_in': self.width_in,
            'height_in': self.height_in,
            'package_type': self.package_type or '',
            'listed_at': self.listed_at.isoformat() if self.listed_at else None,
            'sold_at': self.sold_at.isoformat() if self.sold_at else None,
        }


# ─── Ensure tables exist ─────────────────────────────────────────────────────
with app.app_context():
    from sqlalchemy import text

    # Create base tables if they don't exist
    for model in [PriceResearch, PriceSettings]:
        try:
            table_name = model.__tablename__
            if not db.inspect(db.engine).has_table(table_name):
                model.__table__.create(db.engine)
                log.info(f"Created table: {table_name}")
        except Exception as e:
            log.warning(f"Table creation note for {model.__tablename__}: {e}")

    # EbayListing: drop and recreate if columns are wrong or missing new fields
    try:
        inspector = db.inspect(db.engine)
        if inspector.has_table('ebay_listings'):
            ebay_cols = [c['name'] for c in inspector.get_columns('ebay_listings')]
            if 'product_name' not in ebay_cols or 'order_number' not in ebay_cols:
                log.info("Migration: ebay_listings schema outdated, dropping and recreating")
                db.session.execute(text("DROP TABLE ebay_listings"))
                db.session.commit()
                EbayListing.__table__.create(db.engine)
                log.info("Migration: recreated ebay_listings table with new columns")
        else:
            EbayListing.__table__.create(db.engine)
            log.info("Created table: ebay_listings")
    except Exception as e:
        db.session.rollback()
        log.warning(f"EbayListing migration note: {e}")

    # Add new shipping dimension columns if missing (safe migrations)
    try:
        inspector = db.inspect(db.engine)
        ebay_cols = [c['name'] for c in inspector.get_columns('ebay_listings')]
        shipping_cols = {
            'weight_oz': 'FLOAT',
            'length_in': 'FLOAT',
            'width_in': 'FLOAT',
            'height_in': 'FLOAT',
            'package_type': "VARCHAR(30) DEFAULT ''",
        }
        for col_name, col_type in shipping_cols.items():
            if col_name not in ebay_cols:
                try:
                    db.session.execute(text(f"ALTER TABLE ebay_listings ADD COLUMN {col_name} {col_type}"))
                    db.session.commit()
                    log.info(f"Migration: added '{col_name}' column to ebay_listings")
                except Exception as col_err:
                    db.session.rollback()
                    log.warning(f"Migration note for {col_name}: {col_err}")
    except Exception as e:
        db.session.rollback()
        log.warning(f"Shipping column migration note: {e}")

    # Add new columns if missing (safe migrations)
    try:
        inspector = db.inspect(db.engine)
        existing_cols = [c['name'] for c in inspector.get_columns('price_research')]
        if 'team' not in existing_cols:
            db.session.execute(text("ALTER TABLE price_research ADD COLUMN team VARCHAR(20) DEFAULT 'thoard'"))
            db.session.commit()
            log.info("Migration: added 'team' column to price_research")
        if 'notes' not in existing_cols:
            db.session.execute(text("ALTER TABLE price_research ADD COLUMN notes TEXT DEFAULT ''"))
            db.session.commit()
            log.info("Migration: added 'notes' column to price_research")
    except Exception as e:
        db.session.rollback()
        log.warning(f"Migration note: {e}")

    # Ensure default settings exist
    try:
        if not PriceSettings.query.first():
            db.session.add(PriceSettings(aggressiveness='conservative'))
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.warning(f"Default settings note: {e}")


# =============================================================================
# GROK VISION — PRODUCT IDENTIFICATION & PRICE RESEARCH
# =============================================================================

def grok_research(image_data_list, aggressiveness='conservative'):
    """
    Send images to Grok 4.1 vision for product identification and price research.
    Returns structured JSON with products, prices, and recommendations.
    """
    if not XAI_API_KEY:
        return {'error': 'XAI_API_KEY not configured'}

    # Build image content parts for Grok vision
    content_parts = []

    # System instruction as first text part
    content_parts.append({
        'type': 'text',
        'text': f'''You are PriceBlade, a premium price research AI. Analyze these product images and provide a complete price research report.

CONTEXT: The user receives these products as FREE TikTok Shop samples and resells them on eBay/other platforms. The recommended price MUST be competitive — especially LOWER than TikTok Shop's own price, since buyers can find it there.

INSTRUCTIONS:
1. IDENTIFY each product: brand, exact product name, size/count/flavor, condition
2. DETECT if this is a BUNDLE (multiple different products) or single product
3. RESEARCH current market prices from ALL these sources (use your knowledge of typical retail pricing):
   - TikTok Shop (MOST IMPORTANT — this is the competitor! Get the actual TikTok Shop price)
   - Amazon (current price)
   - Walmart (current price)
   - Google Shopping (lowest price across sellers)
   - eBay sold listings (recent average sold price)
4. For each source, give the ACTUAL current retail price. If not sure, give your best estimate and mark estimated=true.
5. ESTIMATE SHIPPING: For each product, estimate the approximate weight in ounces, dimensions in inches (L x W x H), and best package type (padded_envelope, small_box, medium_box, large_box).

CRITICAL PRICING RULES:
- TikTok Shop prices are typically 20-40% BELOW Amazon/retail. They are NOT 50%+ below. If your TikTok estimate is less than half the Amazon price, you are underestimating — revise upward.
- For the "lowest_price" field: use the MEDIAN price across ALL sources, not the actual lowest. We want competitive pricing, not fire-sale pricing.
- The recommended_price should undercut this median by a {aggressiveness} discount (17-25% for conservative)
- Example: If Amazon=$35, Walmart=$30, TikTok=$25, eBay=$22, median is ~$28. Conservative recommendation = $28 * 0.80 = $22.40
- These are zero-cost products, so any price is pure profit minus eBay fees and shipping
- NEVER recommend a price below $10 for ANY product unless it genuinely sells for under $12 everywhere

RESPOND IN THIS EXACT JSON FORMAT (no markdown, no code blocks, ONLY raw JSON):
{{
  "is_bundle": false,
  "products": [
    {{
      "name": "Full product name",
      "brand": "Brand name",
      "details": "Size, count, flavor, etc.",
      "condition": "New",
      "confidence": "high/medium/low"
    }}
  ],
  "prices": [
    {{
      "product_index": 0,
      "sources": {{
        "tiktok_shop": {{"price": 18.99, "url_hint": "search term for TikTok Shop", "estimated": false}},
        "amazon": {{"price": 24.99, "url_hint": "search term", "estimated": false}},
        "walmart": {{"price": 22.49, "url_hint": "search term", "estimated": false}},
        "google_shopping": {{"price": 21.99, "estimated": true}},
        "ebay_sold": {{"price": 19.99, "avg_of": 5, "estimated": false}}
      }},
      "shipping_estimate": {{
        "weight_oz": 12,
        "length_in": 8,
        "width_in": 4,
        "height_in": 3,
        "package_type": "small_box"
      }},
      "lowest_price": 18.99,
      "average_price": 21.69,
      "tiktok_price": 18.99,
      "recommended_price": 15.99,
      "discount_applied": "16% below median ($19.09)",
      "estimated_profit": 12.51,
      "profit_note": "Cost $0 (free sample) + ~$3.48 fees/shipping"
    }}
  ],
  "bundle_recommendation": null,
  "notes": "Any relevant notes about pricing, competition, rarity, etc."
}}

BUNDLE PRICING (CRITICAL — when multiple products are shown):
If the images show MULTIPLE DIFFERENT products that will be sold together as a bundle:
1. Research EACH product individually with full price data in the "prices" array
2. Calculate the sum of ALL individual recommended prices — this is the "individual_total"
3. Apply a small bundle discount (5-10%) to the total for the "bundle_price"
4. The bundle_price represents what the ENTIRE set/lot should sell for on eBay
5. DO NOT just price the bundle at one product's price — it must reflect ALL items combined

If it's a BUNDLE, include each product separately in "products" and "prices", then add:
"bundle_recommendation": {{
  "individual_total": 45.99,
  "bundle_price": 39.99,
  "bundle_discount": "13% off individual total",
  "bundle_profit": 36.59
}}

IMPORTANT: Respond with ONLY the JSON object. No markdown, no explanation, no code blocks.'''
    })

    # Add images
    for i, img_data in enumerate(image_data_list):
        if img_data.startswith('data:'):
            # Already base64
            content_parts.append({
                'type': 'image_url',
                'image_url': {'url': img_data}
            })
        elif img_data.startswith('http'):
            content_parts.append({
                'type': 'image_url',
                'image_url': {'url': img_data}
            })
        else:
            # Raw base64 without header
            content_parts.append({
                'type': 'image_url',
                'image_url': {'url': f'data:image/jpeg;base64,{img_data}'}
            })

    try:
        # Dynamic timeout: base 60s + 20s per image, max 180s
        timeout = min(60 + len(image_data_list) * 20, 180)
        log.info(f"Calling Grok API with {len(image_data_list)} images, timeout={timeout}s")
        import time as _time
        _start = _time.time()

        resp = requests.post(
            XAI_API_URL,
            headers={
                'Authorization': f'Bearer {XAI_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': XAI_MODEL,
                'messages': [
                    {'role': 'user', 'content': content_parts}
                ],
                'temperature': 0.3,
                'max_tokens': 4000,
            },
            timeout=timeout,
        )

        elapsed = round(_time.time() - _start, 1)
        log.info(f"Grok API responded in {elapsed}s, status={resp.status_code}")

        if resp.status_code != 200:
            log.error(f"Grok API error: {resp.status_code} {resp.text[:500]}")
            return {'error': f'Grok API returned {resp.status_code}'}

        data = resp.json()
        raw_text = data['choices'][0]['message']['content']
        log.info(f"Grok response length: {len(raw_text)}")

        # Parse JSON from response (strip any markdown wrapping)
        json_text = raw_text.strip()
        # Remove markdown code blocks (```json ... ``` or ``` ... ```)
        md_match = re.search(r'```(?:json)?\s*\n(.*?)\n```', json_text, re.DOTALL)
        if md_match:
            json_text = md_match.group(1).strip()
        # Remove any <think>...</think> tags (some models add these)
        json_text = re.sub(r'<think>.*?</think>', '', json_text, flags=re.DOTALL).strip()

        result = json.loads(json_text)

        # ── REAL TIKTOK SHOP PRICE LOOKUP ──
        # After Grok identifies products, search TikTok Shop for real prices
        products = result.get('products', [])
        for idx, product in enumerate(products):
            try:
                tiktok_result = search_tiktok_shop(
                    product.get('name', ''),
                    product.get('brand', '')
                )
                if tiktok_result and tiktok_result.get('price'):
                    real_price = tiktok_result['price']
                    log.info(f"Product {idx}: TikTok VERIFIED ${real_price} (was estimated)")
                    
                    # Override TikTok price in the sources
                    if idx < len(result.get('prices', [])):
                        price_entry = result['prices'][idx]
                        if 'sources' not in price_entry:
                            price_entry['sources'] = {}
                        price_entry['sources']['tiktok_shop'] = {
                            'price': real_price,
                            'url_hint': tiktok_result.get('url', ''),
                            'estimated': False,
                            'verified': True,
                            'source_method': tiktok_result.get('source_method', ''),
                        }
                        # Recalculate lowest_price if TikTok is lower
                        current_lowest = price_entry.get('lowest_price', 999)
                        if real_price < current_lowest:
                            price_entry['lowest_price'] = real_price
                            log.info(f"Product {idx}: new lowest price ${real_price} (TikTok)")
            except Exception as e:
                log.warning(f"TikTok lookup failed for product {idx}: {e}")

        # Calculate ALL 3 pricing tiers per product so frontend can tab-switch
        all_tiers = {}
        for tier_name in ['conservative', 'balanced', 'aggressive']:
            tier_prices = []
            for price_entry in result.get('prices', []):
                # Get lowest price — compute from sources if Grok didn't provide it
                lowest = 0
                try:
                    lowest = float(price_entry.get('lowest_price', 0) or 0)
                except (ValueError, TypeError):
                    lowest = 0

                # If no lowest_price, compute from sources
                sources = price_entry.get('sources', {})
                if lowest <= 0:
                    source_prices = []
                    for src_name, src_data in sources.items():
                        if src_data and src_data.get('price'):
                            try:
                                source_prices.append(float(src_data['price']))
                            except (ValueError, TypeError):
                                pass
                    if source_prices:
                        lowest = min(source_prices)
                        log.info(f"Computed lowest_price from sources: ${lowest}")

                # If STILL no lowest, try average_price or recommended_price from Grok
                if lowest <= 0:
                    try:
                        lowest = float(price_entry.get('average_price', 0) or 0)
                    except (ValueError, TypeError):
                        lowest = 0
                if lowest <= 0:
                    try:
                        lowest = float(price_entry.get('recommended_price', 0) or 0)
                    except (ValueError, TypeError):
                        lowest = 0

                tiktok_price = None
                if sources.get('tiktok_shop') and sources['tiktok_shop'].get('price'):
                    try:
                        tiktok_price = float(sources['tiktok_shop']['price'])
                    except (ValueError, TypeError):
                        tiktok_price = None

                if lowest > 0:
                    discount = get_discount(lowest, tier_name)
                    recommended = round(lowest * (1 - discount), 2)

                    # CRITICAL: Never price above TikTok Shop
                    if tiktok_price and recommended >= tiktok_price:
                        recommended = round(tiktok_price * (1 - discount), 2)

                    # Get shipping cost from AI estimate or default
                    shipping = price_entry.get('shipping_estimate', {})
                    weight_oz = shipping.get('weight_oz', 12) if isinstance(shipping, dict) else 12
                    shipping_cost = get_usps_shipping_cost(weight_oz)

                    ebay_fees = round(recommended * 0.13, 2)
                    est_profit = round(recommended - ebay_fees - shipping_cost, 2)

                    if tiktok_price:
                        pct_below = round((1 - recommended / tiktok_price) * 100)
                        discount_label = f"{pct_below}% below TikTok (${tiktok_price})"
                    else:
                        discount_label = f"{int(discount * 100)}% off lowest (${lowest})"

                    tier_prices.append({
                        'recommended_price': recommended,
                        'discount_applied': discount_label,
                        'estimated_profit': est_profit,
                        'profit_note': f"$0 cost + ${ebay_fees} eBay fees + ${shipping_cost} shipping",
                        'shipping_cost': shipping_cost,
                        'shipping_estimate': shipping,
                    })
                else:
                    tier_prices.append({
                        'recommended_price': price_entry.get('recommended_price', 0),
                        'discount_applied': price_entry.get('discount_applied', ''),
                        'estimated_profit': price_entry.get('estimated_profit', 0),
                        'profit_note': price_entry.get('profit_note', ''),
                    })

            # Bundle pricing per tier
            tier_bundle = None
            if result.get('is_bundle') and len(tier_prices) > 1:
                individual_total = sum(p['recommended_price'] for p in tier_prices)
                bundle_disc = 0.15
                bundle_price = round(individual_total * (1 - bundle_disc), 2)
                # Use sum of shipping costs from tier prices, or default
                total_shipping = sum(p.get('shipping_cost', 5.15) for p in tier_prices)
                bundle_fees = round(bundle_price * 0.13, 2)
                tier_bundle = {
                    'individual_total': round(individual_total, 2),
                    'bundle_price': bundle_price,
                    'bundle_discount': f"{int(bundle_disc * 100)}% off individual total",
                    'bundle_profit': round(bundle_price - bundle_fees - total_shipping, 2),
                }

            all_tiers[tier_name] = {
                'prices': tier_prices,
                'bundle_recommendation': tier_bundle,
            }

        result['pricing_tiers'] = all_tiers

        # Apply the selected tier to the main fields (for backward compat + DB storage)
        selected_tier = all_tiers.get(aggressiveness, all_tiers['conservative'])
        for idx, price_entry in enumerate(result.get('prices', [])):
            if idx < len(selected_tier['prices']):
                price_entry.update(selected_tier['prices'][idx])

        # Handle bundle pricing (selected tier)
        if selected_tier.get('bundle_recommendation'):
            result['bundle_recommendation'] = selected_tier['bundle_recommendation']

        result['_raw'] = raw_text
        return result

    except json.JSONDecodeError as e:
        log.error(f"Grok response not valid JSON: {e}\nRaw: {raw_text[:500]}")
        return {'error': 'AI response was not valid JSON', 'raw': raw_text[:300]}
    except Exception as e:
        log.error(f"Grok research error: {e}")
        return {'error': str(e)}


# =============================================================================
# API ROUTES
# =============================================================================

# ─── Serve PriceBlade PWA ─────────────────────────────────────────────────────
@app.route('/price')
@app.route('/price/')
def price_home():
    return send_from_directory('pwa/price', 'index.html')

@app.route('/price/<path:filename>')
def price_static(filename):
    return send_from_directory('pwa/price', filename)


# ─── Main Research Endpoint ───────────────────────────────────────────────────
@app.route('/price/api/research', methods=['POST'])
def price_research():
    """
    Main price research endpoint.
    Accepts: JSON with 'images' (list of base64 or URLs)
    Returns: Full price research with recommendations
    """
    data = request.get_json()
    if not data or not data.get('images'):
        return jsonify({'error': 'No images provided'}), 400

    images = data['images']
    if not isinstance(images, list) or len(images) == 0:
        return jsonify({'error': 'images must be a non-empty list'}), 400

    # Get aggressiveness from request or settings
    aggressiveness = data.get('aggressiveness')
    if not aggressiveness:
        settings = PriceSettings.query.first()
        aggressiveness = settings.aggressiveness if settings else 'conservative'

    log.info(f"Price research: {len(images)} images, aggressiveness={aggressiveness}")

    # Auto-rotate images and compress (EXIF + size limit)
    # Compress harder for bundles (multiple images = larger payload)
    is_multi = len(images) > 1
    max_dim = 1200 if is_multi else 1600
    quality = 70 if is_multi else 80

    processed_images = []
    for img_data in images:
        if img_data.startswith('data:'):
            try:
                from PIL import Image, ImageOps
                header, b64data = img_data.split(',', 1)
                img_bytes = base64.b64decode(b64data)
                img = Image.open(io.BytesIO(img_bytes))
                img = ImageOps.exif_transpose(img)
                # Resize to limit API payload
                if max(img.size) > max_dim:
                    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=quality)
                rotated_b64 = base64.b64encode(buf.getvalue()).decode()
                processed_images.append(f'data:image/jpeg;base64,{rotated_b64}')
                log.info(f"Image processed: {img.size}, {len(rotated_b64)//1024}KB (q={quality})")
            except Exception as e:
                log.warning(f"Image processing failed: {e}")
                processed_images.append(img_data)
        else:
            processed_images.append(img_data)

    # Run Grok research
    result = grok_research(processed_images, aggressiveness)

    if result.get('error'):
        return jsonify(result), 500

    # Save to history
    try:
        raw = result.pop('_raw', '')
        research = PriceResearch(
            images=json.dumps([f'img_{i}' for i in range(len(images))]),  # Don't store full base64
            products=json.dumps(result.get('products', [])),
            prices=json.dumps(result.get('prices', [])),
            recommended_price=result['prices'][0]['recommended_price'] if result.get('prices') else 0,
            is_bundle=result.get('is_bundle', False),
            aggressiveness=aggressiveness,
            team=data.get('team', 'thoard'),
            raw_response=raw[:5000],
        )
        db.session.add(research)
        db.session.commit()
        result['research_id'] = research.id
    except Exception as e:
        log.error(f"Failed to save research: {e}")

    return jsonify(result)


# ─── Research History ─────────────────────────────────────────────────────────
@app.route('/price/api/history', methods=['GET'])
def price_history():
    """Get research history, optionally filtered by team."""
    limit = request.args.get('limit', 50, type=int)
    team = request.args.get('team', '')
    query = PriceResearch.query
    if team:
        query = query.filter_by(team=team)
    researches = query.order_by(
        PriceResearch.created_at.desc()
    ).limit(limit).all()
    return jsonify([r.to_dict() for r in researches])


@app.route('/price/api/history/<int:research_id>', methods=['GET'])
def price_history_detail(research_id):
    """Get a specific research result."""
    research = PriceResearch.query.get_or_404(research_id)
    return jsonify(research.to_dict())


@app.route('/price/api/history/<int:research_id>', methods=['DELETE'])
def price_history_delete(research_id):
    """Delete a research result."""
    research = PriceResearch.query.get_or_404(research_id)
    db.session.delete(research)
    db.session.commit()
    return jsonify({'deleted': True})


# ─── Settings ─────────────────────────────────────────────────────────────────
@app.route('/price/api/settings', methods=['GET'])
def price_get_settings():
    """Get current settings."""
    settings = PriceSettings.query.first()
    return jsonify({
        'aggressiveness': settings.aggressiveness if settings else 'conservative',
    })


@app.route('/price/api/settings', methods=['POST'])
def price_update_settings():
    """Update settings."""
    data = request.get_json()
    settings = PriceSettings.query.first()
    if not settings:
        settings = PriceSettings()
        db.session.add(settings)

    if 'aggressiveness' in data:
        if data['aggressiveness'] in ('conservative', 'balanced', 'aggressive'):
            settings.aggressiveness = data['aggressiveness']
        else:
            return jsonify({'error': 'Invalid aggressiveness level'}), 400

    settings.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'aggressiveness': settings.aggressiveness})


# ─── Notes ────────────────────────────────────────────────────────────────────
@app.route('/price/api/history/<int:research_id>/notes', methods=['PUT'])
def price_update_notes(research_id):
    """Update notes for a research entry."""
    research = PriceResearch.query.get_or_404(research_id)
    data = request.get_json()
    research.notes = data.get('notes', '')
    db.session.commit()
    return jsonify({'id': research.id, 'notes': research.notes})


# ─── Listings CRUD ────────────────────────────────────────────────────────────
@app.route('/price/api/listings', methods=['GET'])
def get_listings():
    """Get all listings, optionally by team and status."""
    team = request.args.get('team', '')
    status = request.args.get('status', '')
    query = EbayListing.query
    if team:
        query = query.filter_by(team=team)
    if status:
        query = query.filter_by(status=status)
    listings = query.order_by(EbayListing.created_at.desc()).limit(100).all()
    return jsonify([l.to_dict() for l in listings])


@app.route('/price/api/listings', methods=['POST'])
def create_listing():
    """Create a new listing manually."""
    data = request.get_json()
    listing = EbayListing(
        product_name=data.get('product_name', 'Untitled'),
        product_details=data.get('product_details', ''),
        status=data.get('status', 'listed'),
        list_price=data.get('list_price', 0),
        shipping_cost=data.get('shipping_cost', 0),
        team=data.get('team', 'thoard'),
        notes=data.get('notes', ''),
        research_id=data.get('research_id'),
        ebay_item_id=data.get('ebay_item_id', ''),
    )
    # Calculate eBay fees
    listing.ebay_fees = round(listing.list_price * 0.13, 2)
    db.session.add(listing)
    db.session.commit()
    return jsonify(listing.to_dict()), 201


@app.route('/price/api/listings/<int:listing_id>', methods=['PUT'])
def update_listing(listing_id):
    """Update a listing (mark sold, update price, add notes)."""
    listing = EbayListing.query.get_or_404(listing_id)
    data = request.get_json()
    if 'status' in data:
        listing.status = data['status']
        if data['status'] == 'sold':
            listing.sold_at = datetime.utcnow()
            # If no sold_price explicitly set, use list_price and recalculate
            if not listing.sold_price and listing.list_price:
                listing.sold_price = listing.list_price
                listing.ebay_fees = round(listing.sold_price * 0.13, 2)
                listing.profit = round(listing.sold_price - listing.ebay_fees - listing.shipping_cost, 2)
    if 'sold_price' in data:
        listing.sold_price = data['sold_price']
        listing.ebay_fees = round(listing.sold_price * 0.13, 2)
        listing.profit = round(listing.sold_price - listing.ebay_fees - listing.shipping_cost, 2)
    if 'shipping_cost' in data:
        listing.shipping_cost = data['shipping_cost']
        # Recalculate profit
        price = listing.sold_price or listing.list_price
        listing.ebay_fees = round(price * 0.13, 2)
        listing.profit = round(price - listing.ebay_fees - listing.shipping_cost, 2)
    if 'notes' in data:
        listing.notes = data['notes']
    if 'list_price' in data:
        listing.list_price = data['list_price']
    if 'product_name' in data:
        listing.product_name = data['product_name']
    # Shipping dimensions
    for field in ['weight_oz', 'length_in', 'width_in', 'height_in', 'package_type']:
        if field in data:
            setattr(listing, field, data[field])
    db.session.commit()
    return jsonify(listing.to_dict())


@app.route('/price/api/listings/<int:listing_id>', methods=['DELETE'])
def delete_listing(listing_id):
    """Delete a listing."""
    listing = EbayListing.query.get_or_404(listing_id)
    db.session.delete(listing)
    db.session.commit()
    return jsonify({'deleted': True})


@app.route('/price/api/shipping-estimate', methods=['POST'])
def shipping_estimate():
    """Estimate shipping dimensions for a product from its image using Grok vision."""
    data = request.get_json()
    image = data.get('image', '')
    if not image:
        return jsonify({'error': 'No image provided'}), 400
    if not XAI_API_KEY:
        return jsonify({'error': 'XAI_API_KEY not configured'}), 400

    content_parts = [{
        'type': 'text',
        'text': '''You are a shipping expert. Look at this product image and estimate the shipping dimensions.

Provide:
1. Product weight in ounces (estimate based on typical product weight for this type of item)
2. Package dimensions in inches (Length × Width × Height) for the smallest box/envelope it would fit in
3. Best package type: padded_envelope, small_box, medium_box, or large_box
4. USPS Ground Advantage shipping cost estimate

RESPOND IN THIS EXACT JSON FORMAT ONLY (no markdown, no code blocks):
{
  "product_name": "What this product appears to be",
  "weight_oz": 12,
  "length_in": 8,
  "width_in": 4,
  "height_in": 3,
  "package_type": "small_box",
  "usps_cost_estimate": 5.15,
  "notes": "Brief note about packaging recommendation"
}'''
    }]

    if image.startswith('data:'):
        content_parts.append({'type': 'image_url', 'image_url': {'url': image}})
    elif image.startswith('http'):
        content_parts.append({'type': 'image_url', 'image_url': {'url': image}})
    else:
        content_parts.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image}'}})

    try:
        resp = requests.post(
            XAI_API_URL,
            headers={
                'Authorization': f'Bearer {XAI_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': XAI_MODEL,
                'messages': [{'role': 'user', 'content': content_parts}],
                'temperature': 0.2,
                'max_tokens': 1000,
            },
            timeout=60,
        )

        if resp.status_code != 200:
            return jsonify({'error': f'Grok API returned {resp.status_code}'}), 500

        raw = resp.json()['choices'][0]['message']['content']
        # Strip markdown code block if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw.strip())
        raw = re.sub(r'\s*```$', '', raw.strip())
        result = json.loads(raw)

        # Calculate USPS cost from our rate table
        weight = result.get('weight_oz', 12)
        result['usps_cost_calculated'] = get_usps_shipping_cost(weight)

        return jsonify(result)
    except json.JSONDecodeError:
        return jsonify({'error': 'Could not parse AI response', 'raw': raw[:500]}), 500
    except Exception as e:
        log.error(f"Shipping estimate error: {e}")
        return jsonify({'error': str(e)}), 500


# ─── Gmail eBay Scanning ──────────────────────────────────────────────────────

def _get_email_body(msg):
    """Extract text/HTML body from email message."""
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/html':
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', errors='replace')
                    break
            elif ct == 'text/plain' and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', errors='replace')
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode('utf-8', errors='replace')
    return body


def _strip_html(body):
    """Strip HTML tags and normalize whitespace for regex parsing."""
    # Remove <style>...</style> blocks (CSS content would leak as text)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', body, flags=re.DOTALL | re.IGNORECASE)
    # Remove <script>...</script> blocks
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove MSO conditional comments
    text = re.sub(r'<!--\[if[^]]*\]>.*?<!\[endif\]-->', ' ', text, flags=re.DOTALL)
    # Strip remaining HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text)


def _parse_ebay_sold_email(body):
    """Parse eBay sold notification for price, order, buyer."""
    info = {}
    text = _strip_html(body)
    m = re.search(r'Sold:\s*\$?([\d,]+\.?\d*)', text)
    if m: info['sold_price'] = float(m.group(1).replace(',', ''))
    m = re.search(r'Order:\s*([\d\-]+)', text)
    if m: info['order_number'] = m.group(1)
    m = re.search(r'Date sold:\s*([A-Za-z]+ \d+,?\s*\d{4}\s*[\d:]*)', text)
    if m: info['date_sold'] = m.group(1).strip()
    m = re.search(r'Buyer:\s*(\S+)', text)
    if m: info['buyer_name'] = m.group(1).strip()
    m = re.search(r"buyer.?s shipping details:\s*(.*?)(?:Ship by|United States)", text, re.IGNORECASE)
    if m: info['buyer_address'] = m.group(1).strip()
    return info


def _parse_ebay_listed_email(body):
    """Parse eBay listing notification for price, product link."""
    info = {}
    text = _strip_html(body)
    m = re.search(r'Item price:\s*\$?([\d,]+\.?\d*)', text)
    if m: info['list_price'] = float(m.group(1).replace(',', ''))

    # Skip words that indicate non-product links
    skip_words = ['View', 'Manage', 'Revise', 'Sell similar', 'eBay', 'Sign in',
                  'Learn', 'See details', 'Terms', 'Privacy', 'Payments', 'Unsubscribe',
                  'User Agreement', 'Copyright', 'Help', 'Contact', 'email preferences']

    product_name = ''

    # Strip <style> and <script> blocks from body before parsing
    clean_body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL | re.IGNORECASE)
    clean_body = re.sub(r'<script[^>]*>.*?</script>', '', clean_body, flags=re.DOTALL | re.IGNORECASE)

    # 1. eBay listed emails put the product name inside <span class="linkStyledText">
    #    wrapped in MSO conditional comments, within an <a href="ebay.com/itm/..."> link.
    #    IMPORTANT: linkStyledText is used in MULTIPLE places (product + footer), so we
    #    must scope the search to inside the ebay.com/itm anchor tag only.
    itm_block = re.search(r'<a[^>]*href="[^"]*ebay\.com/itm[^"]*"[^>]*>(.*?)</a>', clean_body, re.DOTALL | re.IGNORECASE)
    if itm_block:
        block_html = itm_block.group(1)
        # Look for linkStyledText span within this block
        m = re.search(r'<span[^>]*class="linkStyledText"[^>]*>(.*?)</span>', block_html, re.DOTALL | re.IGNORECASE)
        if m:
            raw = m.group(1)
        else:
            # Fallback: strip all HTML from the link block
            raw = block_html
        # Strip MSO conditional comments: <!--[if mso]>...<![endif]-->
        raw = re.sub(r'<!--\[if mso\]>.*?<!\[endif\]-->', '', raw, flags=re.DOTALL)
        # Strip any remaining HTML tags
        raw = re.sub(r'<[^>]+>', '', raw)
        name = raw.strip().rstrip('.')
        # Remove trailing "..." and clean up
        name = re.sub(r'\.{2,}$', '', name).strip()
        if len(name) > 5 and not any(sw.lower() in name.lower() for sw in skip_words):
            product_name = name

    # 2. Fallback: look for text near "Item price" in the stripped body
    if not product_name:
        # The product name appears before "Item price:" in the stripped text
        m = re.search(r'(?:listing is live|your listing)\s+(.*?)\s*Item price', text, re.DOTALL | re.IGNORECASE)
        if m:
            # Extract the last meaningful text block before Item price
            candidate = m.group(1).strip()
            # Clean up whitespace and take the last line (product name is usually last)
            lines = [l.strip() for l in candidate.split('\n') if l.strip()]
            if lines:
                name = lines[-1].rstrip('.').strip()
                name = re.sub(r'\.{2,}$', '', name).strip()
                if len(name) > 5 and not any(sw.lower() in name.lower() for sw in skip_words):
                    product_name = name

    # 3. Alt text on product image
    if not product_name:
        m = re.search(r'<img[^>]*alt="([^"]{10,200})"[^>]*>', body, re.IGNORECASE)
        if m:
            alt = m.group(1).strip()
            if 'eBay' not in alt and 'logo' not in alt.lower():
                product_name = alt

    if product_name:
        info['product_name_from_body'] = product_name
    return info


def _parse_ebay_shipping_email(body):
    """Parse eBay shipping label for cost, tracking, service."""
    info = {}
    text = _strip_html(body)
    m = re.search(r'Item Number\s*:?\s*(\d+)', text)
    if m: info['ebay_item_id'] = m.group(1)
    m = re.search(r'Tracking\s*:?\s*(\d{10,})', text)
    if m: info['tracking_number'] = m.group(1)
    m = re.search(r'Service\s*:?\s*(USPS[A-Za-z\s]+?)(?:\s*Tracking|\s*Package)', text)
    if m: info['shipping_service'] = m.group(1).strip()
    m = re.search(r'(?:USPS[^$]*|Total charged[^$]*|Order total[^$]*)\$([\d,]+\.?\d*)', text)
    if m: info['shipping_cost'] = float(m.group(1).replace(',', ''))
    m = re.search(r'Buyer\s*:?\s*([A-Z][a-z]+\s+[A-Z][a-z]+)', text)
    if m: info['buyer_name'] = m.group(1)
    m = re.search(r'Ship to\s*:?\s*(.*?)(?:\s*Service|\s*Tracking)', text)
    if m: info['buyer_address'] = m.group(1).strip()
    return info


def _decode_subject(msg):
    """Decode email subject."""
    from email.header import decode_header
    subject = ''
    for part, enc in decode_header(msg['Subject'] or ''):
        subject += part.decode(enc or 'utf-8') if isinstance(part, bytes) else part
    return subject


@app.route('/price/api/gmail/scan', methods=['POST'])
def gmail_scan():
    """Scan Gmail for eBay emails: sold, listed, and shipping labels.
    Parses HTML bodies for prices, orders, tracking, and shipping costs.
    """
    team = request.get_json().get('team', 'thoard') if request.is_json else 'thoard'
    team_upper = team.upper()

    gmail_user = os.environ.get(f'GMAIL_USER_{team_upper}', '') or os.environ.get('GMAIL_USER', '')
    gmail_pass = os.environ.get(f'GMAIL_APP_PASSWORD_{team_upper}', '') or os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_pass:
        return jsonify({'error': f'Gmail not configured for {team}. Set GMAIL_USER_{team_upper} and GMAIL_APP_PASSWORD_{team_upper} env vars on Render.'}), 400

    new_sales = []
    new_listings = []
    updated_shipping = []

    try:
        import imaplib
        import email as _email
        from datetime import timedelta

        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(gmail_user, gmail_pass)
        since_date = (datetime.utcnow() - timedelta(days=14)).strftime('%d-%b-%Y')

        # Try inbox first, fall back to All Mail for Workspace accounts
        mail.select('inbox')
        _, test_msgs = mail.search(None, f'(FROM "ebay@ebay.com" SINCE {since_date})')
        from_filter = 'ebay@ebay.com'
        if not test_msgs[0]:
            # Try broader FROM and All Mail folder for custom domain accounts
            _, test_msgs2 = mail.search(None, f'(FROM "ebay" SINCE {since_date})')
            if test_msgs2[0]:
                from_filter = 'ebay'
            else:
                # Try All Mail folder
                status, _ = mail.select('"[Gmail]/All Mail"')
                if status == 'OK':
                    _, test_msgs3 = mail.search(None, f'(FROM "ebay@ebay.com" SINCE {since_date})')
                    if not test_msgs3[0]:
                        _, test_msgs4 = mail.search(None, f'(FROM "ebay" SINCE {since_date})')
                        if test_msgs4[0]:
                            from_filter = 'ebay'

        # ── 1. SOLD EMAILS ──
        for search_q in ['made the sale', 'item has sold', 'You sold']:
            _, msgs = mail.search(None, f'(FROM "{from_filter}" SUBJECT "{search_q}" SINCE {since_date})')
            for num in (msgs[0].split() if msgs[0] else []):
                try:
                    _, msg_data = mail.fetch(num, '(RFC822)')
                    msg = _email.message_from_bytes(msg_data[0][1])
                    subject = _decode_subject(msg)

                    # Extract product name from subject
                    product_name = subject
                    for pfx in ['You made the sale for ', 'You sold ', 'Your item ']:
                        product_name = product_name.replace(pfx, '')
                    for sfx in ['!', ' has sold', ' has been sold']:
                        product_name = product_name.replace(sfx, '')
                    product_name = product_name.strip()
                    if not product_name:
                        continue

                    body = _get_email_body(msg)
                    info = _parse_ebay_sold_email(body) if body else {}

                    # Skip if already tracked by order number + same product
                    if info.get('order_number'):
                        existing = EbayListing.query.filter_by(order_number=info['order_number']).filter(
                            EbayListing.product_name.ilike(f'%{product_name[:40]}%')
                        ).first()
                        if existing:
                            continue

                    # Check if already tracked as sold with same name
                    existing_sold = EbayListing.query.filter(
                        EbayListing.product_name.ilike(f'%{product_name[:40]}%'),
                        EbayListing.status == 'sold'
                    ).first()
                    if existing_sold:
                        # Fill in missing data
                        if info.get('order_number') and not existing_sold.order_number:
                            existing_sold.order_number = info['order_number']
                        if info.get('buyer_name') and not existing_sold.buyer_name:
                            existing_sold.buyer_name = info['buyer_name']
                        if info.get('sold_price') and not existing_sold.sold_price:
                            existing_sold.sold_price = info['sold_price']
                            existing_sold.ebay_fees = round(existing_sold.sold_price * 0.13, 2)
                            existing_sold.profit = round(existing_sold.sold_price - existing_sold.ebay_fees - existing_sold.shipping_cost, 2)
                        continue

                    # Upgrade active listing to sold
                    active = EbayListing.query.filter(
                        EbayListing.product_name.ilike(f'%{product_name[:40]}%'),
                        EbayListing.status == 'listed'
                    ).first()

                    sold_price = info.get('sold_price', 0)
                    if active:
                        active.status = 'sold'
                        active.sold_at = datetime.utcnow()
                        active.sold_price = sold_price or active.list_price
                        active.order_number = info.get('order_number', '')
                        active.buyer_name = info.get('buyer_name', '')
                        active.buyer_address = info.get('buyer_address', '')
                        active.ebay_fees = round(active.sold_price * 0.13, 2)
                        active.profit = round(active.sold_price - active.ebay_fees - (active.shipping_cost or 0), 2)
                        new_sales.append(active.to_dict())
                    else:
                        fees = round(sold_price * 0.13, 2)
                        listing = EbayListing(
                            product_name=product_name, status='sold', team=team,
                            sold_price=sold_price, sold_at=datetime.utcnow(),
                            order_number=info.get('order_number', ''),
                            buyer_name=info.get('buyer_name', ''),
                            buyer_address=info.get('buyer_address', ''),
                            ebay_fees=fees, profit=round(sold_price - fees, 2),
                        )
                        db.session.add(listing)
                        new_sales.append(listing.to_dict())
                except Exception as e:
                    log.warning(f"Error parsing sold email: {e}")

        db.session.flush()  # Make sold items visible to listed email queries

        # ── 2. LISTED EMAILS ──
        # eBay subject format: "👏 Product Name... has been listed"
        seen_names = set()  # Track names within this scan to prevent in-batch duplicates
        for search_q in ['has been listed', 'listing is live', 'item is listed', 'listing started', 'your listing']:
            _, msgs = mail.search(None, f'(FROM "{from_filter}" SUBJECT "{search_q}" SINCE {since_date})')
            for num in (msgs[0].split() if msgs[0] else []):
                try:
                    _, msg_data = mail.fetch(num, '(RFC822)')
                    msg = _email.message_from_bytes(msg_data[0][1])
                    subject = _decode_subject(msg)

                    # Skip if this is a sold or shipping email
                    subj_lower = subject.lower()
                    if 'made the sale' in subj_lower or 'shipping label' in subj_lower or 'has sold' in subj_lower:
                        continue

                    body = _get_email_body(msg)
                    info = _parse_ebay_listed_email(body) if body else {}

                    # Extract product name — prefer body (more complete) over truncated subject
                    import html as _html
                    body_name = _html.unescape(info.get('product_name_from_body', '')).replace('\ufe0f', '').replace('\ufe0e', '').strip().rstrip('.')
                    # Subject fallback: strip emoji, suffixes, prefixes
                    subj_name = subject.replace('\ufe0f', '').replace('\ufe0e', '')
                    subj_name = re.sub(r'^[\U0001F300-\U0001FAFF\u2600-\u27BF\s]+', '', subj_name).strip()
                    for sfx in ['has been listed', 'is now listed', 'is listed', '- Check our selling tips']:
                        idx = subj_name.lower().find(sfx.lower())
                        if idx > 0:
                            subj_name = subj_name[:idx].strip()
                    for pfx in ['Your item is listed: ', 'Your listing started: ']:
                        subj_name = subj_name.replace(pfx, '')
                    subj_name = subj_name.rstrip('.').strip()
                    if any(kw in subj_name.lower() for kw in ['listing is live', 'your listing', 'listing started']):
                        subj_name = ''
                    # Use subject name as primary (reliable), body only if subject was truncated
                    junk_words = ['terms of use', 'user agreement', 'payment', 'policy update',
                                  'selling tips', 'newsletter', 'account', 'verification',
                                  'email preferences', 'privacy', 'copyright', 'unsubscribe']
                    body_is_valid = body_name and len(body_name) > 8 and not any(jw in body_name.lower() for jw in junk_words)
                    # ALWAYS prefer body_name when it's longer — eBay subjects are often truncated
                    if body_is_valid and len(body_name) > len(subj_name) + 3:
                        product_name = body_name
                    elif subj_name:
                        product_name = subj_name
                    elif body_is_valid:
                        product_name = body_name
                    else:
                        product_name = ''
                    if not product_name:
                        continue
                    # Final junk check on selected name
                    if any(jw in product_name.lower() for jw in junk_words):
                        continue

                    # Skip if already seen in this batch
                    name_key = product_name[:30].lower().strip()
                    if name_key in seen_names:
                        continue
                    seen_names.add(name_key)

                    # Check if already tracked in DB (ANY status — including sold)
                    existing = EbayListing.query.filter(
                        EbayListing.product_name.ilike(f'%{product_name[:40]}%')
                    ).first()
                    # Also check reverse: does a sold/listed item's name contain this short name?
                    if not existing and len(product_name) >= 8:
                        all_items = EbayListing.query.filter_by(team=team).all()
                        for item in all_items:
                            db_name = _html.unescape(item.product_name).replace('\ufe0f', '').replace('\ufe0e', '').lower()
                            if product_name.lower() in db_name:
                                existing = item
                                break
                    if existing:
                        if info.get('list_price') and not existing.list_price:
                            existing.list_price = info['list_price']
                        # Upgrade truncated name with fuller body name
                        if len(product_name) > len(existing.product_name) + 3:
                            log.info(f"Upgrading name: '{existing.product_name}' → '{product_name}'")
                            existing.product_name = product_name
                        continue

                    listing = EbayListing(
                        product_name=product_name, status='listed', team=team,
                        list_price=info.get('list_price', 0),
                        ebay_fees=round(info.get('list_price', 0) * 0.13, 2),
                    )
                    db.session.add(listing)
                    db.session.flush()  # Make visible to subsequent queries
                    new_listings.append(listing.to_dict())
                except Exception as e:
                    log.warning(f"Error parsing listed email: {e}")

        # ── 3. SHIPPING LABEL EMAILS ──
        _, ship_msgs = mail.search(None, f'(FROM "{from_filter}" SUBJECT "shipping label" SINCE {since_date})')
        for num in (ship_msgs[0].split() if ship_msgs[0] else []):
            try:
                _, msg_data = mail.fetch(num, '(RFC822)')
                msg = _email.message_from_bytes(msg_data[0][1])
                subject = _decode_subject(msg)

                product_name = subject.replace('eBay shipping label for ', '').replace('Shipping label for ', '').strip()
                body = _get_email_body(msg)
                info = _parse_ebay_shipping_email(body) if body else {}

                if not product_name:
                    continue

                listing = EbayListing.query.filter(
                    EbayListing.product_name.ilike(f'%{product_name[:40]}%')
                ).first()

                if listing:
                    changed = False
                    if info.get('shipping_cost') and not listing.shipping_cost:
                        listing.shipping_cost = info['shipping_cost']; changed = True
                    if info.get('tracking_number') and not listing.tracking_number:
                        listing.tracking_number = info['tracking_number']
                    if info.get('shipping_service') and not listing.shipping_service:
                        listing.shipping_service = info['shipping_service']
                    if info.get('buyer_name') and not listing.buyer_name:
                        listing.buyer_name = info['buyer_name']
                    if info.get('ebay_item_id') and not listing.ebay_item_id:
                        listing.ebay_item_id = info['ebay_item_id']
                    if changed:
                        price = listing.sold_price or listing.list_price
                        if price:
                            listing.ebay_fees = round(price * 0.13, 2)
                            listing.profit = round(price - listing.ebay_fees - listing.shipping_cost, 2)
                    updated_shipping.append(listing.to_dict())
            except Exception as e:
                log.warning(f"Error parsing shipping email: {e}")

        # ── 4. CLEANUP: merge duplicates ──
        # If a listed item's name overlaps a sold item's name, delete the listed duplicate
        db.session.flush()  # Ensure all new items are visible
        listed_items = EbayListing.query.filter_by(team=team, status='listed').all()
        sold_items = EbayListing.query.filter_by(team=team, status='sold').all()
        to_delete = []

        # Delete junk-named items from DB
        junk_words = ['terms of use', 'user agreement', 'payment', 'policy update',
                      'selling tips', 'newsletter', 'account', 'verification',
                      'email preferences', 'privacy', 'copyright', 'unsubscribe']
        for listed in listed_items:
            if any(jw in listed.product_name.lower() for jw in junk_words):
                to_delete.append(listed)
                log.info(f"Removing junk listing: '{listed.product_name}'")

        # Merge listed items that match sold items
        for listed in listed_items:
            if listed in to_delete:
                continue
            ln = listed.product_name.lower()
            for sold in sold_items:
                sn = sold.product_name.lower()
                # Match if either name contains the other, or first 8+ chars match
                if (ln in sn or sn in ln or
                    (len(ln) >= 8 and len(sn) >= 8 and ln[:8] == sn[:8])):
                    # Merge list_price into sold if missing
                    if listed.list_price and not sold.list_price:
                        sold.list_price = listed.list_price
                    to_delete.append(listed)
                    log.info(f"Merged duplicate: '{listed.product_name}' into sold '{sold.product_name}'")
                    break
        for item in to_delete:
            db.session.delete(item)

        db.session.commit()
        mail.logout()

        return jsonify({
            'new_sales': new_sales,
            'new_listings': new_listings,
            'updated_shipping': updated_shipping,
            'summary': f"{len(new_sales)} sales, {len(new_listings)} listings, {len(updated_shipping)} shipping updates",
        })

    except Exception as e:
        log.error(f"Gmail scan error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/price/api/gmail/debug-scan', methods=['POST'])
def gmail_debug_scan():
    """Debug endpoint: show raw email data without saving anything."""
    team = request.get_json().get('team', 'thoard') if request.is_json else 'thoard'
    team_upper = team.upper()
    gmail_user = os.environ.get(f'GMAIL_USER_{team_upper}', '') or os.environ.get('GMAIL_USER', '')
    gmail_pass = os.environ.get(f'GMAIL_APP_PASSWORD_{team_upper}', '') or os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_pass:
        return jsonify({'error': 'Gmail not configured'}), 400

    debug_results = []
    try:
        import imaplib
        import email as _email
        from datetime import timedelta

        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(gmail_user, gmail_pass)
        since_date = (datetime.utcnow() - timedelta(days=14)).strftime('%d-%b-%Y')

        # Try multiple folders (Workspace accounts may filter to different folders)
        folders_to_try = ['inbox', '"[Gmail]/All Mail"']
        for folder in folders_to_try:
            status, _ = mail.select(folder)
            if status != 'OK':
                debug_results.append({'folder': folder, 'status': 'not available'})
                continue

            # First: search ALL eBay emails (no subject filter) to verify access
            _, all_ebay = mail.search(None, f'(FROM "ebay@ebay.com" SINCE {since_date})')
            total_ebay = len(all_ebay[0].split()) if all_ebay[0] else 0

            # Also try broader FROM patterns for custom domain accounts
            _, all_ebay2 = mail.search(None, f'(FROM "ebay" SINCE {since_date})')
            total_ebay_broad = len(all_ebay2[0].split()) if all_ebay2[0] else 0

            debug_results.append({
                'folder': folder,
                'gmail_user': gmail_user,
                'total_ebay_emails_exact': total_ebay,
                'total_ebay_emails_broad': total_ebay_broad,
            })

            # If we found eBay emails, search by subject
            search_source = all_ebay2 if total_ebay_broad > 0 else all_ebay
            from_filter = 'ebay' if total_ebay_broad > total_ebay else 'ebay@ebay.com'

            for search_q in ['has been listed', 'made the sale', 'shipping label', 'listing is live', 'item has sold']:
                _, msgs = mail.search(None, f'(FROM "{from_filter}" SUBJECT "{search_q}" SINCE {since_date})')
                count = len(msgs[0].split()) if msgs[0] else 0
                if count == 0:
                    debug_results.append({'search': search_q, 'folder': folder, 'count': 0})
                    continue

                # Show first 2 from each type
                for num in (msgs[0].split() if msgs[0] else [])[:2]:
                    _, msg_data = mail.fetch(num, '(RFC822)')
                    msg = _email.message_from_bytes(msg_data[0][1])
                    subject = _decode_subject(msg)
                    body = _get_email_body(msg)
                    text = _strip_html(body)[:500] if body else ''
                    sender = msg.get('From', '')

                    # Parse based on type
                    parsed = {}
                    if 'listed' in search_q or 'listing' in search_q:
                        parsed = _parse_ebay_listed_email(body) if body else {}
                    elif 'sale' in search_q or 'sold' in search_q:
                        parsed = _parse_ebay_sold_email(body) if body else {}
                    elif 'shipping' in search_q:
                        parsed = _parse_ebay_shipping_email(body) if body else {}

                    debug_results.append({
                        'search': search_q,
                        'folder': folder,
                        'total_found': count,
                        'subject': subject,
                        'from': sender,
                        'body_preview': text,
                        'parsed': parsed,
                        # Show raw HTML around product link for debugging
                        'raw_html_near_price': '',
                    })
                    # Extract HTML window around "Item price" for listed emails
                    if body and ('listed' in search_q or 'listing' in search_q):
                        import re as _re
                        idx = body.find('Item price')
                        if idx == -1:
                            idx = body.find('listing is live')
                        if idx > 0:
                            start = max(0, idx - 2000)
                            end = min(len(body), idx + 500)
                            debug_results[-1]['raw_html_near_price'] = body[start:end]

            # If we found emails in this folder, no need to check others
            if total_ebay > 0 or total_ebay_broad > 0:
                break

        mail.logout()
        return jsonify({'debug': debug_results})
    except Exception as e:
        return jsonify({'error': str(e), 'gmail_user': gmail_user}), 500


# ─── Dashboard Stats ──────────────────────────────────────────────────────────
@app.route('/price/api/dashboard', methods=['GET'])
def dashboard_stats():
    """Get dashboard stats with time-frame breakdowns."""
    team = request.args.get('team', '')
    from datetime import timedelta

    def get_team_stats(team_filter=None):
        query = EbayListing.query
        if team_filter:
            query = query.filter_by(team=team_filter)

        listed = query.filter_by(status='listed').count()
        sold_items = query.filter_by(status='sold').all()
        total_revenue = sum(s.sold_price or 0 for s in sold_items)
        total_shipping = sum(s.shipping_cost or 0 for s in sold_items)
        total_fees = sum(s.ebay_fees or 0 for s in sold_items)
        total_profit = sum(s.profit or 0 for s in sold_items)

        # Research count
        rq = PriceResearch.query
        if team_filter:
            rq = rq.filter_by(team=team_filter)
        research_count = rq.count()

        # Time-frame breakdowns (EST = UTC-5)
        now = datetime.utcnow()
        est_now = now - timedelta(hours=5)
        today_start = est_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=5)  # Back to UTC
        yesterday_start = today_start - timedelta(days=1)
        week_start = today_start - timedelta(days=7)
        month_start = today_start - timedelta(days=30)

        def period_stats(items, start, end=None):
            filtered = [s for s in items if s.sold_at and s.sold_at >= start and (not end or s.sold_at < end)]
            return {
                'sales': len(filtered),
                'revenue': round(sum(s.sold_price or 0 for s in filtered), 2),
                'profit': round(sum(s.profit or 0 for s in filtered), 2),
            }

        return {
            'active_listings': listed,
            'items_sold': len(sold_items),
            'total_revenue': round(total_revenue, 2),
            'total_shipping': round(total_shipping, 2),
            'total_fees': round(total_fees, 2),
            'total_profit': round(total_profit, 2),
            'avg_profit': round(total_profit / len(sold_items), 2) if sold_items else 0,
            'research_count': research_count,
            'today': period_stats(sold_items, today_start),
            'yesterday': period_stats(sold_items, yesterday_start, today_start),
            'last_7d': period_stats(sold_items, week_start),
            'last_30d': period_stats(sold_items, month_start),
        }

    result = {'team': get_team_stats(team) if team else get_team_stats()}
    if not team:
        result['thoard'] = get_team_stats('thoard')
        result['reol'] = get_team_stats('reol')
    return jsonify(result)


# ─── CSV Export ───────────────────────────────────────────────────────────────
@app.route('/price/api/export/csv', methods=['GET'])
def export_csv():
    """Export research history as CSV."""
    team = request.args.get('team', '')
    query = PriceResearch.query
    if team:
        query = query.filter_by(team=team)
    researches = query.order_by(PriceResearch.created_at.desc()).all()

    import csv
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Product', 'Brand', 'Recommended Price', 'Tier', 'Team', 'Notes'])
    for r in researches:
        products = json.loads(r.products or '[]')
        name = products[0]['name'] if products else 'Unknown'
        brand = products[0].get('brand', '') if products else ''
        writer.writerow([
            r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '',
            name, brand,
            f'${r.recommended_price:.2f}' if r.recommended_price else '',
            r.aggressiveness, r.team or 'thoard', r.notes or '',
        ])

    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=priceblade_export.csv'},
    )


log.info("✅ PriceBlade module loaded")


# ─── Background Gmail Auto-Scanner ───────────────────────────────────────────
# Scans every 5 minutes for both teams, stores new sales for push notifications
import threading
import time as _time

_pending_sales = []  # New sales waiting to be fetched by frontend
_last_scan_time = None

def _auto_scan_gmail():
    """Background thread: scan Gmail for both teams every 5 minutes."""
    global _pending_sales, _last_scan_time
    _time.sleep(30)  # Wait 30s for app to fully start
    while True:
        for team in ['thoard', 'reol']:
            team_upper = team.upper()
            gmail_user = os.environ.get(f'GMAIL_USER_{team_upper}', '') or os.environ.get('GMAIL_USER', '')
            gmail_pass = os.environ.get(f'GMAIL_APP_PASSWORD_{team_upper}', '') or os.environ.get('GMAIL_APP_PASSWORD', '')
            if not gmail_user or not gmail_pass:
                continue
            try:
                import imaplib
                import email as _email
                from datetime import timedelta

                mail = imaplib.IMAP4_SSL('imap.gmail.com')
                mail.login(gmail_user, gmail_pass)
                since_date = (datetime.utcnow() - timedelta(days=14)).strftime('%d-%b-%Y')

                # Auto-detect folder and FROM filter (Workspace support)
                mail.select('inbox')
                _, test_msgs = mail.search(None, f'(FROM "ebay@ebay.com" SINCE {since_date})')
                from_filter = 'ebay@ebay.com'
                if not test_msgs[0]:
                    _, test_msgs2 = mail.search(None, f'(FROM "ebay" SINCE {since_date})')
                    if test_msgs2[0]:
                        from_filter = 'ebay'
                    else:
                        status, _ = mail.select('"[Gmail]/All Mail"')
                        if status == 'OK':
                            _, test_msgs3 = mail.search(None, f'(FROM "ebay" SINCE {since_date})')
                            if test_msgs3[0]:
                                from_filter = 'ebay'

                with app.app_context():
                    # ── SOLD EMAILS ──
                    seen_sold = set()  # Track processed sold items to prevent duplicates
                    for search_q in ['made the sale', 'item has sold', 'You sold']:
                        _, msgs = mail.search(None, f'(FROM "{from_filter}" SUBJECT "{search_q}" SINCE {since_date})')
                        for num in (msgs[0].split() if msgs[0] else []):
                            try:
                                _, msg_data = mail.fetch(num, '(RFC822)')
                                msg = _email.message_from_bytes(msg_data[0][1])
                                subject = _decode_subject(msg)

                                product_name = subject
                                for pfx in ['You made the sale for ', 'You sold ', 'Your item ']:
                                    product_name = product_name.replace(pfx, '')
                                for sfx in ['!', ' has sold', ' has been sold']:
                                    product_name = product_name.replace(sfx, '')
                                product_name = product_name.strip()
                                if not product_name:
                                    continue

                                body = _get_email_body(msg)
                                info = _parse_ebay_sold_email(body) if body else {}

                                # Skip if already seen in this batch (same email matches multiple queries)
                                sold_key = (product_name[:30].lower().strip(), info.get('order_number', ''))
                                if sold_key in seen_sold:
                                    continue
                                seen_sold.add(sold_key)

                                # Skip if already tracked (check order + name for multi-item orders)
                                if info.get('order_number'):
                                    if EbayListing.query.filter_by(order_number=info['order_number']).filter(
                                        EbayListing.product_name.ilike(f'%{product_name[:40]}%')
                                    ).first():
                                        continue

                                existing = EbayListing.query.filter(
                                    EbayListing.product_name.ilike(f'%{product_name[:40]}%'),
                                    EbayListing.status == 'sold'
                                ).first()
                                if existing:
                                    # Fill in missing data on existing sold entry
                                    if info.get('order_number') and not existing.order_number:
                                        existing.order_number = info['order_number']
                                    if info.get('buyer_name') and not existing.buyer_name:
                                        existing.buyer_name = info['buyer_name']
                                    if info.get('sold_price') and not existing.sold_price:
                                        existing.sold_price = info['sold_price']
                                        existing.ebay_fees = round(existing.sold_price * 0.13, 2)
                                        existing.profit = round(existing.sold_price - existing.ebay_fees - (existing.shipping_cost or 0), 2)
                                    continue

                                active = EbayListing.query.filter(
                                    EbayListing.product_name.ilike(f'%{product_name[:40]}%'),
                                    EbayListing.status == 'listed'
                                ).first()

                                sold_price = info.get('sold_price', 0)
                                if active:
                                    active.status = 'sold'
                                    active.sold_at = datetime.utcnow()
                                    active.sold_price = sold_price or active.list_price
                                    active.order_number = info.get('order_number', '')
                                    active.buyer_name = info.get('buyer_name', '')
                                    active.buyer_address = info.get('buyer_address', '')
                                    active.ebay_fees = round(active.sold_price * 0.13, 2)
                                    active.profit = round(active.sold_price - active.ebay_fees - (active.shipping_cost or 0), 2)
                                    _pending_sales.append({'product_name': product_name, 'team': team, 'profit': active.profit})
                                else:
                                    fees = round(sold_price * 0.13, 2)
                                    listing = EbayListing(
                                        product_name=product_name, status='sold', team=team,
                                        sold_price=sold_price, sold_at=datetime.utcnow(),
                                        order_number=info.get('order_number', ''),
                                        buyer_name=info.get('buyer_name', ''),
                                        ebay_fees=fees, profit=round(sold_price - fees, 2),
                                    )
                                    db.session.add(listing)
                                    _pending_sales.append({'product_name': product_name, 'team': team, 'profit': round(sold_price - fees, 2)})
                                    db.session.flush()  # Make visible to subsequent queries
                            except Exception as e:
                                log.warning(f"Auto-scan sold error: {e}")

                    db.session.flush()

                    # ── LISTED EMAILS ──
                    seen_names = set()
                    for search_q in ['has been listed', 'listing is live', 'item is listed', 'listing started', 'your listing']:
                        _, msgs = mail.search(None, f'(FROM "{from_filter}" SUBJECT "{search_q}" SINCE {since_date})')
                        for num in (msgs[0].split() if msgs[0] else []):
                            try:
                                _, msg_data = mail.fetch(num, '(RFC822)')
                                msg = _email.message_from_bytes(msg_data[0][1])
                                subject = _decode_subject(msg)

                                body = _get_email_body(msg)
                                info = _parse_ebay_listed_email(body) if body else {}

                                import html as _html
                                body_name = _html.unescape(info.get('product_name_from_body', '')).replace('\ufe0f', '').replace('\ufe0e', '').strip().rstrip('.')
                                subj_name = subject.replace('\ufe0f', '').replace('\ufe0e', '')
                                subj_name = re.sub(r'^[\U0001F300-\U0001FAFF\u2600-\u27BF\s]+', '', subj_name).strip()
                                for sfx in ['has been listed', 'is now listed', 'is listed', '- Check our selling tips']:
                                    idx = subj_name.lower().find(sfx.lower())
                                    if idx > 0:
                                        subj_name = subj_name[:idx].strip()
                                for pfx in ['Your item is listed: ', 'Your listing started: ']:
                                    subj_name = subj_name.replace(pfx, '')
                                subj_name = subj_name.rstrip('.').strip()
                                if any(kw in subj_name.lower() for kw in ['listing is live', 'your listing', 'listing started']):
                                    subj_name = ''
                                junk_words = ['terms of use', 'user agreement', 'payment', 'policy update',
                                              'selling tips', 'newsletter', 'account', 'verification',
                                              'email preferences', 'privacy', 'copyright', 'unsubscribe']
                                body_is_valid = body_name and len(body_name) > 8 and not any(jw in body_name.lower() for jw in junk_words)
                                # ALWAYS prefer body_name when it's longer — eBay subjects are often truncated
                                if body_is_valid and len(body_name) > len(subj_name) + 3:
                                    product_name = body_name
                                elif subj_name:
                                    product_name = subj_name
                                elif body_is_valid:
                                    product_name = body_name
                                else:
                                    product_name = ''
                                if not product_name:
                                    continue
                                if any(jw in product_name.lower() for jw in junk_words):
                                    continue

                                # Skip if already seen in this batch
                                name_key = product_name[:30].lower().strip()
                                if name_key in seen_names:
                                    continue
                                seen_names.add(name_key)

                                # Skip if already tracked in DB (ANY status including sold)
                                existing = EbayListing.query.filter(
                                    EbayListing.product_name.ilike(f'%{product_name[:40]}%')
                                ).first()
                                if not existing and len(product_name) >= 8:
                                    all_items = EbayListing.query.filter_by(team=team).all()
                                    for item in all_items:
                                        db_name = _html.unescape(item.product_name).replace('\ufe0f', '').replace('\ufe0e', '').lower()
                                        if product_name.lower() in db_name:
                                            existing = item
                                            break
                                if existing:
                                    if info.get('list_price') and not existing.list_price:
                                        existing.list_price = info['list_price']
                                    # Upgrade truncated name with fuller body name
                                    if len(product_name) > len(existing.product_name) + 3:
                                        log.info(f"Upgrading name: '{existing.product_name}' → '{product_name}'")
                                        existing.product_name = product_name
                                    continue

                                listing = EbayListing(
                                    product_name=product_name, status='listed', team=team,
                                    list_price=info.get('list_price'),
                                    listed_at=datetime.utcnow(),
                                )
                                db.session.add(listing)
                                db.session.flush()  # Make visible to subsequent queries
                            except Exception as e:
                                log.warning(f"Auto-scan listed error: {e}")

                    # ── SHIPPING LABEL EMAILS ──
                    _, ship_msgs = mail.search(None, f'(FROM "{from_filter}" SUBJECT "shipping label" SINCE {since_date})')
                    for num in (ship_msgs[0].split() if ship_msgs[0] else []):
                        try:
                            _, msg_data = mail.fetch(num, '(RFC822)')
                            msg = _email.message_from_bytes(msg_data[0][1])
                            subject = _decode_subject(msg)
                            product_name = subject.replace('eBay shipping label for ', '').replace('Shipping label for ', '').strip()
                            body = _get_email_body(msg)
                            info = _parse_ebay_shipping_email(body) if body else {}
                            if not product_name:
                                continue
                            listing = EbayListing.query.filter(
                                EbayListing.product_name.ilike(f'%{product_name[:40]}%')
                            ).first()
                            if listing:
                                changed = False
                                if info.get('shipping_cost') and not listing.shipping_cost:
                                    listing.shipping_cost = info['shipping_cost']; changed = True
                                if info.get('tracking_number') and not listing.tracking_number:
                                    listing.tracking_number = info['tracking_number']
                                if info.get('shipping_service') and not listing.shipping_service:
                                    listing.shipping_service = info['shipping_service']
                                if info.get('buyer_name') and not listing.buyer_name:
                                    listing.buyer_name = info['buyer_name']
                                if info.get('ebay_item_id') and not listing.ebay_item_id:
                                    listing.ebay_item_id = info['ebay_item_id']
                                if changed:
                                    price = listing.sold_price or listing.list_price
                                    if price:
                                        listing.ebay_fees = round(price * 0.13, 2)
                                        listing.profit = round(price - listing.ebay_fees - listing.shipping_cost, 2)
                        except Exception as e:
                            log.warning(f"Auto-scan shipping error: {e}")

                    # ── CLEANUP: merge duplicates ──
                    db.session.flush()
                    listed_items = EbayListing.query.filter_by(team=team, status='listed').all()
                    sold_items = EbayListing.query.filter_by(team=team, status='sold').all()
                    to_delete = []
                    junk_words = ['terms of use', 'user agreement', 'payment', 'policy update',
                                  'selling tips', 'newsletter', 'account', 'verification',
                                  'email preferences', 'privacy', 'copyright', 'unsubscribe']
                    for listed in listed_items:
                        if any(jw in listed.product_name.lower() for jw in junk_words):
                            to_delete.append(listed)
                    # Remove active items that match a sold item
                    for listed in listed_items:
                        if listed in to_delete:
                            continue
                        ln = listed.product_name.lower()
                        for sold in sold_items:
                            sn = sold.product_name.lower()
                            if (ln in sn or sn in ln or
                                (len(ln) >= 8 and len(sn) >= 8 and ln[:8] == sn[:8])):
                                if listed.list_price and not sold.list_price:
                                    sold.list_price = listed.list_price
                                to_delete.append(listed)
                                break
                    # Remove duplicate active-to-active items (keep first)
                    seen_active = set()
                    for listed in listed_items:
                        if listed in to_delete:
                            continue
                        key = listed.product_name[:30].lower().strip()
                        if key in seen_active:
                            to_delete.append(listed)
                        else:
                            seen_active.add(key)
                    for item in to_delete:
                        db.session.delete(item)

                    db.session.commit()

                mail.logout()
                _last_scan_time = datetime.utcnow()
                log.info(f"Auto-scan complete for {team}")
            except Exception as e:
                log.warning(f"Auto-scan error for {team}: {e}")
        _time.sleep(300)  # Wait 5 minutes before next cycle


@app.route('/price/api/gmail/new-sales', methods=['GET'])
def get_new_sales():
    """Get pending new sales for push notifications, then clear them."""
    global _pending_sales
    team = request.args.get('team', '')
    if team:
        sales = [s for s in _pending_sales if s.get('team') == team]
        _pending_sales = [s for s in _pending_sales if s.get('team') != team]
    else:
        sales = list(_pending_sales)
        _pending_sales = []
    return jsonify({
        'new_sales': sales,
        'last_scan': (_last_scan_time.isoformat() + 'Z') if _last_scan_time else None,
    })


# Start auto-scanner thread
_scan_thread = threading.Thread(target=_auto_scan_gmail, daemon=True)
_scan_thread.start()
log.info("📧 Gmail auto-scanner started (every 5 minutes)")
