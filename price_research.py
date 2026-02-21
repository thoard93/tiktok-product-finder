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

# ─── Discount Ladder ─────────────────────────────────────────────────────────
DISCOUNT_LADDER = {
    'conservative': {15: 0.10, 30: 0.15, 60: 0.20, 100: 0.25, 9999: 0.30},
    'balanced':     {15: 0.14, 30: 0.20, 60: 0.25, 100: 0.30, 9999: 0.35},
    'aggressive':   {15: 0.18, 30: 0.25, 60: 0.30, 100: 0.35, 9999: 0.40},
}

def get_discount(price, aggressiveness='conservative'):
    """Get discount percentage based on price tier and aggressiveness."""
    ladder = DISCOUNT_LADDER.get(aggressiveness, DISCOUNT_LADDER['conservative'])
    for threshold, discount in sorted(ladder.items()):
        if price <= threshold:
            return discount
    return 0.30  # fallback


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
    raw_response = db.Column(db.Text, default='')     # Full Grok response for debugging
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


# ─── Ensure tables exist ─────────────────────────────────────────────────────
with app.app_context():
    # Create tables if they don't exist
    for model in [PriceResearch, PriceSettings]:
        table_name = model.__tablename__
        if not db.inspect(db.engine).has_table(table_name):
            model.__table__.create(db.engine)
            log.info(f"Created table: {table_name}")

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

INSTRUCTIONS:
1. IDENTIFY each product: brand, exact product name, size/count/flavor, condition
2. DETECT if this is a BUNDLE (multiple different products) or single product
3. RESEARCH current market prices from these sources (use your knowledge of typical retail pricing):
   - Amazon (current price)
   - Walmart (current price)
   - Google Shopping (lowest price across sellers)
   - TikTok Shop (if available)
   - eBay sold listings (recent average)
4. For each source, give the ACTUAL current retail price. If you're not sure, give your best estimate and mark it.
5. Calculate a recommended selling price using a {aggressiveness} discount strategy.

RESPOND IN THIS EXACT JSON FORMAT (no markdown, no code blocks, ONLY raw JSON):
{{
  "is_bundle": false,
  "products": [
    {{
      "name": "Full product name",
      "brand": "Brand name",
      "details": "Size, count, flavor, etc.",
      "condition": "New/Used/Open Box",
      "confidence": "high/medium/low"
    }}
  ],
  "prices": [
    {{
      "product_index": 0,
      "sources": {{
        "amazon": {{"price": 24.99, "url_hint": "search term", "estimated": false}},
        "walmart": {{"price": 22.49, "url_hint": "search term", "estimated": false}},
        "google_shopping": {{"price": 21.99, "estimated": true}},
        "tiktok_shop": {{"price": 18.99, "estimated": true}},
        "ebay_sold": {{"price": 19.99, "avg_of": 5, "estimated": false}}
      }},
      "lowest_price": 18.99,
      "average_price": 21.69,
      "recommended_price": 16.99,
      "discount_applied": "15% off lowest",
      "estimated_profit": 13.59,
      "profit_note": "Cost $0 (TikTok sample) + ~$3.40 shipping/fees"
    }}
  ],
  "bundle_recommendation": null,
  "notes": "Any relevant notes about the product, deals, rarity, etc."
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

        # Apply discount ladder to recommended prices
        for price_entry in result.get('prices', []):
            lowest = price_entry.get('lowest_price', 0)
            if lowest and lowest > 0:
                discount = get_discount(lowest, aggressiveness)
                recommended = round(lowest * (1 - discount), 2)
                # eBay fees ~13% + shipping ~$3.40
                est_fees = round(recommended * 0.13 + 3.40, 2)
                est_profit = round(recommended - est_fees, 2)

                price_entry['recommended_price'] = recommended
                price_entry['discount_applied'] = f"{int(discount * 100)}% off lowest (${lowest})"
                price_entry['estimated_profit'] = est_profit
                price_entry['profit_note'] = f"Cost $0 + ~${est_fees} fees/shipping"

        # Handle bundle pricing
        if result.get('is_bundle') and len(result.get('prices', [])) > 1:
            individual_total = sum(p.get('recommended_price', 0) for p in result['prices'])
            bundle_discount = 0.15  # 15% off individual total for bundles
            bundle_price = round(individual_total * (1 - bundle_discount), 2)
            bundle_fees = round(bundle_price * 0.13 + 3.40, 2)
            result['bundle_recommendation'] = {
                'individual_total': round(individual_total, 2),
                'bundle_price': bundle_price,
                'bundle_discount': f"{int(bundle_discount * 100)}% off individual total",
                'bundle_profit': round(bundle_price - bundle_fees, 2),
            }

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
    """Get research history (last 30 days)."""
    limit = request.args.get('limit', 50, type=int)
    researches = PriceResearch.query.order_by(
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


log.info("✅ PriceBlade module loaded")
