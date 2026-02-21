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

# ─── Discount Ladder (aggressive to undercut TikTok Shop) ────────────────────
# Lower-priced items ($0-30) get steeper discounts since TikTok prices them low
DISCOUNT_LADDER = {
    'conservative': {15: 0.25, 30: 0.30, 60: 0.28, 100: 0.32, 9999: 0.35},
    'balanced':     {15: 0.32, 30: 0.35, 60: 0.33, 100: 0.37, 9999: 0.40},
    'aggressive':   {15: 0.40, 30: 0.42, 60: 0.40, 100: 0.44, 9999: 0.48},
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
    product_name = db.Column(db.String(200), default='')
    product_details = db.Column(db.String(500), default='')
    status = db.Column(db.String(20), default='listed')  # listed, sold, shipped
    list_price = db.Column(db.Float, default=0)
    sold_price = db.Column(db.Float, default=0)
    shipping_cost = db.Column(db.Float, default=0)
    ebay_fees = db.Column(db.Float, default=0)
    profit = db.Column(db.Float, default=0)
    team = db.Column(db.String(20), default='thoard')
    notes = db.Column(db.Text, default='')
    research_id = db.Column(db.Integer, nullable=True)  # Link to PriceResearch
    ebay_item_id = db.Column(db.String(50), default='')
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
            'listed_at': self.listed_at.isoformat() if self.listed_at else None,
            'sold_at': self.sold_at.isoformat() if self.sold_at else None,
        }


# ─── Ensure tables exist ─────────────────────────────────────────────────────
with app.app_context():
    # Create tables if they don't exist
    for model in [PriceResearch, PriceSettings, EbayListing]:
        table_name = model.__tablename__
        if not db.inspect(db.engine).has_table(table_name):
            model.__table__.create(db.engine)
            log.info(f"Created table: {table_name}")

    # Add new columns if missing (safe migrations)
    try:
        inspector = db.inspect(db.engine)
        existing_cols = [c['name'] for c in inspector.get_columns('price_research')]
        from sqlalchemy import text
        if 'team' not in existing_cols:
            db.session.execute(text("ALTER TABLE price_research ADD COLUMN team VARCHAR(20) DEFAULT 'thoard'"))
            db.session.commit()
            log.info("Migration: added 'team' column to price_research")
        if 'notes' not in existing_cols:
            db.session.execute(text("ALTER TABLE price_research ADD COLUMN notes TEXT DEFAULT ''"))
            db.session.commit()
            log.info("Migration: added 'notes' column to price_research")
    except Exception as e:
        log.warning(f"Migration note: {e}")

    # Ensure default settings exist
    if not PriceSettings.query.first():
        db.session.add(PriceSettings(aggressiveness='conservative'))
        db.session.commit()


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

CRITICAL PRICING RULE:
- The recommended_price MUST be LOWER than the TikTok Shop price (since buyers can find it there)
- If TikTok Shop sells it for $18, recommend $14-16 range (not $19!)
- Use the LOWEST price across ALL sources as the baseline, then apply the {aggressiveness} discount
- These are zero-cost products, so any price is pure profit minus eBay fees and shipping

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
      "discount_applied": "16% below TikTok ($18.99)",
      "estimated_profit": 12.51,
      "profit_note": "Cost $0 (free sample) + ~$3.48 fees/shipping"
    }}
  ],
  "bundle_recommendation": null,
  "notes": "Any relevant notes about pricing, competition, rarity, etc."
}}

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
                lowest = price_entry.get('lowest_price', 0)
                tiktok_price = None
                sources = price_entry.get('sources', {})
                if sources.get('tiktok_shop') and sources['tiktok_shop'].get('price'):
                    tiktok_price = float(sources['tiktok_shop']['price'])

                if lowest and lowest > 0:
                    discount = get_discount(lowest, tier_name)
                    recommended = round(lowest * (1 - discount), 2)

                    # CRITICAL: Never price above TikTok Shop
                    if tiktok_price and recommended >= tiktok_price:
                        recommended = round(tiktok_price * (1 - discount), 2)

                    # Get shipping cost from AI estimate or default
                    shipping = price_entry.get('shipping_estimate', {})
                    weight_oz = shipping.get('weight_oz', 12)  # default 12oz
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
    db.session.commit()
    return jsonify(listing.to_dict())


@app.route('/price/api/listings/<int:listing_id>', methods=['DELETE'])
def delete_listing(listing_id):
    """Delete a listing."""
    listing = EbayListing.query.get_or_404(listing_id)
    db.session.delete(listing)
    db.session.commit()
    return jsonify({'deleted': True})


# ─── Gmail eBay Scanning ──────────────────────────────────────────────────────
@app.route('/price/api/gmail/scan', methods=['POST'])
def gmail_scan():
    """Scan Gmail for eBay listing and sold notification emails.
    Per-team env vars: GMAIL_USER_THOARD, GMAIL_APP_PASSWORD_THOARD,
                       GMAIL_USER_REOL, GMAIL_APP_PASSWORD_REOL
    Falls back to shared: GMAIL_USER, GMAIL_APP_PASSWORD
    """
    team = request.get_json().get('team', 'thoard') if request.is_json else 'thoard'
    team_upper = team.upper()

    # Try team-specific credentials first, then shared
    gmail_user = os.environ.get(f'GMAIL_USER_{team_upper}', '') or os.environ.get('GMAIL_USER', '')
    gmail_pass = os.environ.get(f'GMAIL_APP_PASSWORD_{team_upper}', '') or os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_pass:
        return jsonify({'error': f'Gmail not configured for {team}. Set GMAIL_USER_{team_upper} and GMAIL_APP_PASSWORD_{team_upper} env vars on Render.'}), 400

    new_sales = []
    new_listings = []

    try:
        import imaplib
        import email
        from email.header import decode_header

        # Connect to Gmail IMAP
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(gmail_user, gmail_pass)
        mail.select('inbox')

        # Search for eBay emails from the last 7 days
        from datetime import timedelta
        since_date = (datetime.utcnow() - timedelta(days=7)).strftime('%d-%b-%Y')

        # ── Scan for "sold" notifications ──
        _, sold_msgs = mail.search(None, f'(FROM "ebay@ebay.com" SUBJECT "sold" SINCE {since_date})')
        for num in sold_msgs[0].split():
            _, msg_data = mail.fetch(num, '(RFC822)')
            msg = email.message_from_bytes(msg_data[0][1])
            subject = ''
            for part, enc in decode_header(msg['Subject'] or ''):
                subject += part.decode(enc or 'utf-8') if isinstance(part, bytes) else part

            # Extract product name from subject
            # Typical: "You sold ProductName!"
            product_name = subject.replace('You sold ', '').replace('!', '').strip()
            if not product_name:
                continue

            # Check if already tracked
            existing = EbayListing.query.filter_by(
                product_name=product_name, status='sold'
            ).first()
            if existing:
                continue

            # Check if there's an active listing to update
            active = EbayListing.query.filter(
                EbayListing.product_name.ilike(f'%{product_name[:30]}%'),
                EbayListing.status == 'listed'
            ).first()

            if active:
                active.status = 'sold'
                active.sold_at = datetime.utcnow()
                if not active.sold_price:
                    active.sold_price = active.list_price
                active.ebay_fees = round(active.sold_price * 0.13, 2)
                active.profit = round(active.sold_price - active.ebay_fees - active.shipping_cost, 2)
                new_sales.append(active.to_dict())
            else:
                # Create new sold listing
                listing = EbayListing(
                    product_name=product_name,
                    status='sold',
                    team=team,
                    sold_at=datetime.utcnow(),
                )
                db.session.add(listing)
                new_sales.append({'product_name': product_name, 'status': 'sold'})

        # ── Scan for "listed" notifications ──
        _, list_msgs = mail.search(None, f'(FROM "ebay@ebay.com" SUBJECT "listed" SINCE {since_date})')
        for num in list_msgs[0].split():
            _, msg_data = mail.fetch(num, '(RFC822)')
            msg = email.message_from_bytes(msg_data[0][1])
            subject = ''
            for part, enc in decode_header(msg['Subject'] or ''):
                subject += part.decode(enc or 'utf-8') if isinstance(part, bytes) else part

            product_name = subject.replace('Your item is listed: ', '').replace('Your item is listed on eBay: ', '').strip()
            if not product_name:
                continue

            existing = EbayListing.query.filter_by(product_name=product_name).first()
            if existing:
                continue

            listing = EbayListing(
                product_name=product_name,
                status='listed',
                team=team,
            )
            db.session.add(listing)
            new_listings.append({'product_name': product_name, 'status': 'listed'})

        db.session.commit()
        mail.logout()

        return jsonify({
            'new_sales': new_sales,
            'new_listings': new_listings,
            'total_scanned': len(sold_msgs[0].split()) + len(list_msgs[0].split()),
        })

    except Exception as e:
        log.error(f"Gmail scan error: {e}")
        return jsonify({'error': str(e)}), 500


# ─── Dashboard Stats ──────────────────────────────────────────────────────────
@app.route('/price/api/dashboard', methods=['GET'])
def dashboard_stats():
    """Get dashboard stats (per team and combined)."""
    team = request.args.get('team', '')

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

        return {
            'active_listings': listed,
            'items_sold': len(sold_items),
            'total_revenue': round(total_revenue, 2),
            'total_shipping': round(total_shipping, 2),
            'total_fees': round(total_fees, 2),
            'total_profit': round(total_profit, 2),
            'avg_profit': round(total_profit / len(sold_items), 2) if sold_items else 0,
            'research_count': research_count,
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
