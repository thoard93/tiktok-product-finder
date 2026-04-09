"""
AI Routes Blueprint - PRISM AI Chat, Image Generation (Gemini), Video Generation (Kling)
Extracted from monolithic app.py
"""

import os
import time
import json
import base64
import random
import requests
import traceback

from flask import Blueprint, jsonify, request, session, send_from_directory
from app import db
from app.models import Product, User, ApiKey, ScanJob
from app.routes.auth import login_required, admin_required, get_current_user, log_activity

ai_bp = Blueprint('ai_bp', __name__)

# =============================================================================
# AI CONSTANTS
# =============================================================================

KLING_ACCESS_KEY = os.environ.get('KLING_ACCESS_KEY', '')
KLING_SECRET_KEY = os.environ.get('KLING_SECRET_KEY', '')
KLING_API_BASE_URL = "https://api-singapore.klingai.com"
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
KLING_DEFAULT_PROMPT = "cinematic push towards the product, no hands, product stays still"

# Developer passkey (needed for video generation auth)
DEV_PASSKEY = os.environ.get('DEV_PASSKEY', 'change-this-passkey-123')


# =============================================================================
# AI HELPER: Anthropic Key
# =============================================================================

def get_anthropic_key():
    from app import get_config_value
    return get_config_value('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY')


# =============================================================================
# AI CHAT ROUTE
# =============================================================================

@ai_bp.route('/api/ai/chat', methods=['POST'])
def ai_chat():
    """PRISM AI Chatbot - Grok 4.1 powered TikTok Shop Expert"""
    try:
        # Use Grok 4.1 via XAI_API_KEY (falls back to Anthropic if XAI not set)
        xai_key = os.environ.get('XAI_API_KEY', '')
        anthropic_key = get_anthropic_key()

        if not xai_key and not anthropic_key:
            return jsonify({"success": False, "error": "No AI API key configured. Set XAI_API_KEY or ANTHROPIC_API_KEY."}), 500

        data = request.json
        message = data.get('message', '')
        if not message:
            return jsonify({"success": False, "error": "No message provided"}), 400

        # Build diverse product context based on what the user might ask about
        product_context = _build_product_context(message)

        system_prompt = f"""You are 'PRISM AI', the expert intelligence engine of the PRISM platform — a TikTok Shop product research tool.

YOUR CAPABILITIES:
- Analyze TikTok Shop product data (sales, ad spend, video counts, pricing, commission rates, GMV)
- Find "Gems" — products with high ad spend but few affiliate videos (opportunity for content creators)
- Find "Caked Picks" — products with $50K-$200K GMV and ≤50 influencers
- Identify trending products, scaling brands, and best affiliate opportunities
- Provide market insights and product recommendations based on real data

CURRENT DATABASE SNAPSHOT:
{json.dumps(product_context, default=str)}

RESPONSE RULES:
1. Be concise, professional, and data-driven. Always reference specific numbers.
2. When recommending products, explain WHY (e.g., "$22K ad spend with only 40 videos = massive untapped opportunity").
3. Never make up data — only reference products from the context above.
4. If the user asks about something not in your data, say so honestly.
5. Format important numbers clearly (e.g., "$12.5K" not "12500").
6. If asked for "gems" or opportunities, sort by efficiency ratio (ad_spend / videos).
7. Refer to the platform as 'PRISM'. You are PRISM AI.
8. ALWAYS include these when listing products:
   - **Product name** (bold)
   - Seller/Shop name
   - TikTok link from the tiktok_link field
   - Key stats (ad spend, videos, sales, commission)
   Example: **Product Name** by Seller — $X ad spend, Y videos, Z% commission | [View on TikTok](link)"""

        if xai_key:
            # Use Grok 4.1 Fast-Reasoning (preferred)
            ai_res = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {xai_key}",
                },
                json={
                    "model": "grok-4-1-fast-reasoning",
                    "max_tokens": 1500,
                    "temperature": 0.4,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message}
                    ]
                },
                timeout=60
            )

            if ai_res.status_code != 200:
                return jsonify({"success": False, "error": f"AI Error ({ai_res.status_code}): {ai_res.text[:200]}"}), 500

            ai_data = ai_res.json()
            ai_response = ai_data['choices'][0]['message']['content']
        else:
            # Fallback to Anthropic/Claude
            ai_res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-3-haiku-20240307",
                    "max_tokens": 1500,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": message}]
                },
                timeout=30
            )

            if ai_res.status_code != 200:
                return jsonify({"success": False, "error": f"AI Error: {ai_res.text[:200]}"}), 500

            ai_data = ai_res.json()
            ai_response = ai_data['content'][0]['text']

        return jsonify({"success": True, "response": ai_response})

    except Exception as e:
        print(f"[AI] Exception: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _build_product_context(user_message):
    """Build diverse product context segments so the AI can answer any question."""
    from sqlalchemy import func, desc

    msg_lower = user_message.lower()

    def _product_summary(p):
        efficiency = round(p.ad_spend / max(p.video_count or 1, 1), 1) if p.ad_spend else 0
        pid = p.product_id or ''
        return {
            "name": p.product_name[:80] if p.product_name else "Unknown",
            "seller": p.seller_name or "Unknown",
            "tiktok_link": p.product_url or (f"https://shop.tiktok.com/view/product/{pid}?region=US&locale=en-US" if pid else ""),
            "price": round(p.price or 0, 2),
            "ad_spend_7d": round(p.ad_spend or 0, 2),
            "videos_alltime": p.video_count_alltime or p.video_count or 0,
            "videos_7d": p.video_7d or 0,
            "sales_7d": p.sales_7d or 0,
            "sales_30d": p.sales_30d or 0,
            "gmv_30d": round(p.gmv_30d or 0, 2),
            "commission": round((p.commission_rate or 0) * 100, 1),
            "influencers": p.influencer_count or 0,
            "efficiency": efficiency,
            "rating": p.product_rating or 0,
        }

    context = {}

    # Always include: summary stats
    total = Product.query.filter(Product.product_status == 'active').count()
    high_spend = Product.query.filter(Product.ad_spend > 5000, Product.product_status == 'active').count()
    gems = Product.query.filter(
        Product.ad_spend > 2000,
        Product.video_count < 50,
        Product.product_status == 'active'
    ).count()

    context["database_summary"] = {
        "total_active_products": total,
        "high_ad_spend_products": high_spend,
        "gem_opportunities": gems,
    }

    # Segment 1: Top products by ad spend (always useful)
    top_ad = Product.query.filter(
        Product.ad_spend > 0,
        Product.product_status == 'active'
    ).order_by(desc(Product.ad_spend)).limit(15).all()
    context["top_by_ad_spend"] = [_product_summary(p) for p in top_ad]

    # Segment 2: Gems — high ad spend, low videos
    gem_products = Product.query.filter(
        Product.ad_spend > 2000,
        Product.video_count < 50,
        Product.product_status == 'active'
    ).order_by(desc(Product.ad_spend)).limit(15).all()
    context["gems_high_spend_low_videos"] = [_product_summary(p) for p in gem_products]

    # Segment 3: Top sellers by 7D sales volume
    top_sales = Product.query.filter(
        Product.sales_7d > 0,
        Product.product_status == 'active'
    ).order_by(desc(Product.sales_7d)).limit(10).all()
    context["top_by_sales_volume"] = [_product_summary(p) for p in top_sales]

    # Segment 4: Best commission rates
    if any(w in msg_lower for w in ['commission', 'affiliate', 'earn', 'money', 'profit', 'margin']):
        high_comm = Product.query.filter(
            Product.commission_rate > 0.10,
            Product.sales_7d > 10,
            Product.product_status == 'active'
        ).order_by(desc(Product.commission_rate)).limit(10).all()
        context["best_commission_rates"] = [_product_summary(p) for p in high_comm]

    # Segment 5: Newest products (recently discovered)
    if any(w in msg_lower for w in ['new', 'recent', 'latest', 'discover', 'fresh', 'trending']):
        newest = Product.query.filter(
            Product.product_status == 'active'
        ).order_by(desc(Product.first_seen)).limit(10).all()
        context["recently_discovered"] = [_product_summary(p) for p in newest]

    # Segment 6: Caked Picks ($50K-$200K GMV, ≤50 influencers)
    if any(w in msg_lower for w in ['caked', 'cake', 'pick', 'sweet', 'niche']):
        caked = Product.query.filter(
            Product.gmv_30d >= 50000,
            Product.gmv_30d <= 200000,
            Product.influencer_count <= 50,
            Product.product_status == 'active'
        ).order_by(desc(Product.gmv_30d)).limit(10).all()
        context["caked_picks"] = [_product_summary(p) for p in caked]

    # Segment 7: Cheapest products (budget-friendly)
    if any(w in msg_lower for w in ['cheap', 'budget', 'affordable', 'low price', 'under']):
        cheap = Product.query.filter(
            Product.price > 0,
            Product.sales_7d > 5,
            Product.product_status == 'active'
        ).order_by(Product.price.asc()).limit(10).all()
        context["budget_friendly"] = [_product_summary(p) for p in cheap]

    return context


# =============================================================================
# AI IMAGE GENERATION HELPERS
# =============================================================================

def get_product_category(product_name):
    """Determine product category from name for better prompts"""
    name_lower = product_name.lower()

    # IMPORTANT: Order matters! More specific categories should be checked first
    # Outdoor first to catch firewood carts, garden items before tools catches them
    # Tools second to catch Vevor hydraulic lifts, etc.
    categories = {
        'outdoor': ['firewood', 'log cart', 'garden', 'patio', 'lawn', 'grill', 'bbq', 'camping', 'tent', 'backpack', 'hiking', 'fishing', 'cooler', 'umbrella', 'outdoor furniture', 'fire pit', 'fireplace carrier'],
        'tools': ['tool', 'drill', 'hammer', 'screwdriver', 'wrench', 'tape measure', 'level', 'lift', 'hydraulic', 'jack', 'compressor', 'welder', 'saw', 'sander', 'grinder', 'workbench', 'vevor', 'scaffold', 'ladder', 'dolly', 'hoist', 'clamp', 'vise', 'industrial', 'mechanic', 'garage', 'workshop', 'hauler', 'mover', 'rack storage', 'steel', 'heavy duty', 'capacity', 'scissor', 'table cart', 'pallet'],
        'beauty': ['serum', 'cream', 'lotion', 'skincare', 'makeup', 'mascara', 'lipstick', 'foundation', 'moisturizer', 'cleanser', 'toner', 'sunscreen', 'face', 'skin', 'eye cream', 'anti-aging', 'melaxin', 'cemenrete'],
        'hair': ['shampoo', 'conditioner', 'hair oil', 'hair mask', 'brush', 'comb', 'dryer', 'straightener', 'curler', 'hair growth'],
        'fashion': ['dress', 'shirt', 'pants', 'jeans', 'jacket', 'coat', 'sweater', 'hoodie', 'shoes', 'sneakers', 'boots', 'heels', 'bag', 'purse', 'handbag', 'wallet', 'belt', 'scarf', 'hat', 'sunglasses', 'jewelry', 'necklace', 'bracelet', 'earring', 'watch', 'clothing', 'apparel', 'blouse', 'skirt', 'shorts', 'girlfriend jeans', 'boyfriend jeans'],
        'kitchen': ['pan', 'pot', 'knife', 'cutting board', 'blender', 'mixer', 'cooker', 'fryer', 'toaster', 'kettle', 'coffee', 'mug', 'plate', 'bowl', 'utensil', 'spatula', 'container', 'drink mix', 'sodastream', 'kitchen'],
        'home': ['pillow', 'pillowcase', 'blanket', 'curtain', 'rug', 'lamp', 'candle', 'vase', 'frame', 'mirror', 'clock', 'organizer', 'basket', 'shelf', 'holder', 'bedding', 'sheets', 'duvet', 'decor'],
        'tech': ['phone', 'charger', 'cable', 'earbuds', 'headphones', 'speaker', 'mouse', 'keyboard', 'stand', 'mount', 'tripod', 'camera', 'ring light', 'laptop', 'tablet', 'wireless', 'bluetooth'],
        'fitness': ['yoga', 'dumbbell', 'weight', 'resistance band', 'gym', 'workout', 'protein', 'shaker', 'fitness', 'exercise', 'vibration plate', 'treadmill', 'kettlebell'],
        'car': ['car', 'auto', 'vehicle', 'seat cover', 'steering', 'dash', 'freshener', 'automotive'],
        'health': ['vitamin', 'supplement', 'medicine', 'thermometer', 'massager', 'heating pad', 'ice pack'],
        'cleaning': ['cleaner', 'mop', 'broom', 'vacuum', 'sponge', 'detergent', 'spray'],
        'pet': ['dog', 'cat', 'pet', 'collar', 'leash', 'pet toy', 'pet bed', 'treat'],
        'baby': ['baby', 'infant', 'toddler', 'diaper', 'pacifier', 'stroller', 'carrier', 'nursery'],
    }

    for category, keywords in categories.items():
        if any(keyword in name_lower for keyword in keywords):
            return category
    return 'general'


def get_scene_prompt(product_name, category):
    """Generate a RANDOMIZED lifestyle scene prompt based on product category

    IMPORTANT: Small products (beauty, hair) should be CLOSER to camera with readable text
    Large products (tools, fitness) can be farther back
    All images need room above product for video push effect
    """

    # Background items by category - realistic and subtle, blurred/out of focus
    background_items = {
        'beauty': [
            "folded towels, a candle, and a small plant placed around but out of focus",
            "a soap dispenser, rolled face towel, and a small succulent",
            "cotton pads in a jar, a small mirror, and a ceramic dish",
            "a ceramic tray, small vase with dried flowers, and folded washcloths"
        ],
        'hair': [
            "a hairbrush, folded towel, and small potted plant blurred in the background",
            "a round mirror, hair clips in a dish, and a ceramic container"
        ],
        'fashion': [
            "a ceramic vase, stack of magazines, and a coffee cup in the corner blurred",
            "a small plant, decorative tray, and sunglasses placed nearby out of focus"
        ],
        'kitchen': [
            "a fruit bowl, cookbook stand, and ceramic utensil holder blurred in the background",
            "fresh herbs in a pot, wooden cutting board, and linen napkin",
            "a coffee mug, small plant, and woven placemat out of focus"
        ],
        'tools': [
            "a toolbox, work gloves, and safety glasses in the background blurred",
            "pegboard with tools, a shop rag, and small parts organizer out of focus",
            "concrete floor texture, storage shelves blurred in background"
        ],
        'outdoor': [
            "green grass, a patio chair, and potted plants blurred in background",
            "wooden fence, garden tools leaning nearby, natural foliage out of focus",
            "stacked firewood, outdoor decor, and greenery in the distance"
        ],
        'tech': [
            "a coffee mug, small plant, and notebook blurred slightly",
            "a pen holder, coaster, and desk organizer out of focus"
        ],
        'fitness': [
            "a water bottle, folded towel on a shelf, and yoga block",
            "resistance bands placed naturally, a plant, and woven basket"
        ],
        'home': [
            "a small plant, candle, and stack of books out of focus",
            "a decorative tray, vase, and cozy throw blanket edge"
        ],
        'general': [
            "a small plant, folded cloth, and decorative items blurred in the background",
            "a candle, ceramic dish, and natural texture elements out of focus"
        ]
    }

    # SMALL PRODUCTS - beauty, hair - moderate distance, readable text, NO floating banners
    # CRITICAL: Tell AI to NOT add any text/titles/labels to the image
    small_product_templates = [
        "a realistic product photo of the {product} on a clean bathroom counter, shot from a few feet back where the product fills about 40 percent of the frame width, soft natural lighting from a window, subtle background items like {bg_items}, good amount of empty space ABOVE the product, no people, do NOT add any text titles labels or captions to this image, clean modern setting, overall bright and realistic",
        "a bright bathroom scene with the {product} displayed on a marble counter, shot from a comfortable distance with the product as the clear hero, soft daylight from the side, subtle background items like {bg_items}, plenty of breathing room above the product, no people, do NOT overlay any text or titles or product names on the image, neutral aesthetic, overall bright and professional"
    ]

    # FASHION - flat lay overhead, product centered with room around it
    fashion_templates = [
        "a realistic flat lay photo of the {product} laid neatly on a clean beige or cream colored surface, shot from above, the clothing is centered and fills about 50 percent of the frame, soft natural lighting from a window, subtle background items like {bg_items}, good amount of empty space above and around the product, no people, do NOT add any text titles labels or captions to this image, clean minimal aesthetic, overall bright and lifestyle",
        "a wide overhead flat lay shot of the {product} laid flat on a light wooden floor or neutral surface, natural soft daylight, the product is well-lit and centered with breathing room around it, subtle background items like {bg_items}, plenty of space above the product, no people, do NOT overlay any text or product names, clean modern aesthetic"
    ]

    # MEDIUM PRODUCTS - kitchen, home, tech
    medium_product_templates = [
        "a realistic product photo of the {product} on a modern kitchen counter a few feet back, soft daylight from a window, the product is clearly visible and centered, product details are sharp, subtle background items like {bg_items}, plenty of empty space above the product, no people, do NOT add any text titles labels or captions to this image, clean and inviting setting, overall bright and realistic",
        "a bright lifestyle scene with the {product} displayed on a clean surface, shot at a natural distance, soft natural lighting, the product is the clear focus with readable details, subtle background items like {bg_items}, good amount of space above, no people, do NOT overlay any text or product names on the image, modern aesthetic, overall bright and professional"
    ]

    # LARGE PRODUCTS - tools, fitness equipment - show UPRIGHT and ASSEMBLED
    large_product_templates = [
        "a realistic photo of the {product} standing upright in its normal position in a clean garage or workshop, the product is fully assembled and ready to use, natural daylight from a window or open garage door, shot from a few feet back showing the full product, subtle background items like {bg_items}, clean concrete floor, plenty of room above the product, no people, do NOT add any text titles labels or captions to this image, professional atmosphere",
        "a bright outdoor or garage scene with the {product} standing upright on concrete or pavement, the product is fully assembled in its normal upright position, natural daylight, shot from a comfortable distance to show the whole product, subtle background elements, space above, no people, do NOT overlay any text or product names on the image, realistic and practical setting",
        "a realistic lifestyle photo showing the {product} fully assembled and standing upright in a backyard or garage setting, natural lighting, the product is shown in its normal use position as if ready to be used, room around the product for context, plenty of open space above, no people, do NOT add any text or titles to this image, clean and functional environment"
    ]

    # OUTDOOR PRODUCTS - firewood carts, garden equipment, patio items
    outdoor_templates = [
        "a realistic outdoor photo of the {product} standing upright on a patio or backyard, the product is fully assembled in its normal position, natural daylight, green grass or wooden deck visible, shot from a few feet back to show the full product, space above, no people, do NOT add any text titles labels or captions to this image, inviting outdoor setting",
        "a bright backyard scene with the {product} fully assembled and standing upright near a house or garage, natural sunlight, the product looks ready to use in its normal position, subtle outdoor elements in background, plenty of room above the product, no people, do NOT overlay any text or product names, realistic lifestyle photo"
    ]

    # FITNESS - moderate distance for equipment
    fitness_templates = [
        "a realistic home wellness scene with soft natural lighting, the {product} centered on a clean floor, shot at a natural distance where the product is clearly visible, subtle background items like {bg_items}, plenty of open space above the product, no people, do NOT add any text titles labels or captions to this image, calm and minimal decor, neutral tones, overall bright and motivating",
        "a bright fitness space with the {product} placed naturally, the product is well-lit and the focus of the shot, soft daylight, subtle background items like {bg_items}, lots of open space above, no people, do NOT overlay any text or product names on the image, clean and energetic setting"
    ]

    # Get appropriate templates based on product category
    bg_items = random.choice(background_items.get(category, background_items['general']))

    if category in ['beauty', 'hair']:
        template = random.choice(small_product_templates)
    elif category == 'fashion':
        template = random.choice(fashion_templates)
    elif category == 'tools':
        template = random.choice(large_product_templates)
    elif category == 'outdoor':
        template = random.choice(outdoor_templates)
    elif category == 'fitness':
        template = random.choice(fitness_templates)
    else:
        template = random.choice(medium_product_templates)

    # Build prompt by filling in the template
    prompt = template.format(product=product_name, bg_items=bg_items)

    # Add vertical format at the end
    prompt += ", vertical 9:16 portrait format"

    return prompt


# =============================================================================
# AI IMAGE GENERATION ROUTE
# =============================================================================

@ai_bp.route('/api/generate-image/<product_id>', methods=['POST'])
@login_required
def generate_ai_image(product_id):
    """
    Generate an AI lifestyle image for a product using Gemini API (Nano Banana Pro)

    The generated image will:
    - Use the product's existing image as reference (or cropped version if provided)
    - Place it in a natural lifestyle setting
    - Camera a few feet back with open background
    - Add complementary items for realism
    """
    if not GEMINI_API_KEY:
        return jsonify({
            'success': False,
            'error': 'Gemini API key not configured. Please add GEMINI_API_KEY to environment variables.'
        }), 500

    try:
        # Get product info
        product = Product.query.get(product_id)
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        # Check if a cropped image was provided in the request
        request_data = request.get_json() or {}
        cropped_image_data = request_data.get('cropped_image')

        if cropped_image_data:
            # Use the cropped image provided by the frontend
            # Remove data URL prefix if present (e.g., "data:image/png;base64,")
            if ',' in cropped_image_data:
                header, image_data = cropped_image_data.split(',', 1)
                if 'png' in header:
                    mime_type = 'image/png'
                elif 'webp' in header:
                    mime_type = 'image/webp'
                else:
                    mime_type = 'image/jpeg'
            else:
                image_data = cropped_image_data
                mime_type = 'image/jpeg'
        else:
            # Fall back to fetching the original product image
            image_url = product.cached_image_url or product.image_url
            if not image_url:
                return jsonify({'success': False, 'error': 'No product image available'}), 400

            # Download the product image and convert to base64
            try:
                # If it's a proxy URL, fetch through our proxy
                if image_url.startswith('/api/image-proxy'):
                    # Extract the actual URL from the proxy
                    from urllib.parse import parse_qs, urlparse
                    parsed = urlparse(image_url)
                    actual_url = parse_qs(parsed.query).get('url', [None])[0]
                    if actual_url:
                        image_url = actual_url

                img_response = requests.get(image_url, timeout=30, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                if img_response.status_code != 200:
                    return jsonify({'success': False, 'error': f'Failed to download product image: {img_response.status_code}'}), 400

                image_data = base64.b64encode(img_response.content).decode('utf-8')

                # Determine image mime type
                content_type = img_response.headers.get('Content-Type', 'image/jpeg')
                if 'png' in content_type:
                    mime_type = 'image/png'
                elif 'webp' in content_type:
                    mime_type = 'image/webp'
                else:
                    mime_type = 'image/jpeg'

            except Exception as e:
                return jsonify({'success': False, 'error': f'Failed to fetch product image: {str(e)}'}), 400

        # Determine product category and generate prompt
        category = get_product_category(product.product_name or '')
        prompt = get_scene_prompt(product.product_name or 'product', category)

        # Use the REAL Nano Banana Pro models:
        # - gemini-3-pro-image-preview = Nano Banana Pro (BEST quality, 2K/4K, sharp text) - PRIMARY
        # - gemini-2.5-flash-image = Nano Banana (fast fallback)
        models_to_try = [
            "gemini-3-pro-image-preview",   # Nano Banana Pro - BEST QUALITY, try first!
            "gemini-2.5-flash-image",       # Nano Banana - fallback if Pro fails
        ]

        # Basic payload without resolution config (for fallback model)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": image_data
                            }
                        },
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"]
            }
        }

        # Payload with aspect ratio AND 2K resolution for Nano Banana Pro
        # Supports: "1K", "2K", "4K" - using 2K for sharp text while keeping reasonable speed
        payload_with_config = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": image_data
                            }
                        },
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": "9:16",
                    "imageSize": "2K"
                }
            }
        }

        response = None
        last_error = None

        for model_name in models_to_try:
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"

            # Use full config (aspect ratio + 2K resolution) for Nano Banana Pro
            # Use basic payload for fallback model
            current_payload = payload_with_config if "3-pro" in model_name else payload

            try:
                response = requests.post(
                    gemini_url,
                    json=current_payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=120  # Longer timeout for high-quality generation
                )

                if response.status_code == 200:
                    result_check = response.json()
                    # Verify we got an image back
                    if 'candidates' in result_check and len(result_check['candidates']) > 0:
                        candidate = result_check['candidates'][0]
                        if 'content' in candidate and 'parts' in candidate['content']:
                            has_image = any('inlineData' in part for part in candidate['content']['parts'])
                            if has_image:
                                print(f"AI Image: Success with model {model_name}")
                                break
                    last_error = f"{model_name}: No image in response"
                    print(f"AI Image: {model_name} returned no image, trying next...")
                else:
                    last_error = f"{model_name}: {response.status_code} - {response.text[:300]}"
                    print(f"AI Image: Failed with {model_name}, trying next...")
            except Exception as e:
                last_error = f"{model_name}: {str(e)}"
                print(f"AI Image: Exception with {model_name}: {e}")
                continue

        if not response or response.status_code != 200:
            error_detail = last_error or 'All models failed'
            return jsonify({
                'success': False,
                'error': f'Gemini API error',
                'detail': error_detail
            }), 500

        result = response.json()

        # Extract the generated image from response
        generated_image = None
        generated_mime = 'image/png'  # Default
        if 'candidates' in result and len(result['candidates']) > 0:
            candidate = result['candidates'][0]
            if 'content' in candidate and 'parts' in candidate['content']:
                for part in candidate['content']['parts']:
                    if 'inlineData' in part:
                        generated_image = part['inlineData']['data']
                        generated_mime = part['inlineData'].get('mimeType', 'image/png')
                        break

        if not generated_image:
            # Try alternative response structure
            if 'candidates' in result:
                return jsonify({
                    'success': False,
                    'error': 'No image generated - model may not support image output',
                    'debug': str(result)[:500]
                }), 500
            return jsonify({
                'success': False,
                'error': 'Failed to generate image - unexpected response format',
                'debug': str(result)[:500]
            }), 500

        # Log the generation
        user = get_current_user()
        if user:
            log_activity(user.id, 'ai_image_generated', {
                'product_id': product_id,
                'product_name': product.product_name[:50] if product.product_name else '',
                'category': category
            })

        return jsonify({
            'success': True,
            'image': f"data:{generated_mime};base64,{generated_image}",
            'product_name': product.product_name,
            'category': category,
            'prompt_used': prompt[:200] + '...'
        })

    except requests.Timeout:
        return jsonify({'success': False, 'error': 'Gemini API timeout - please try again'}), 504
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500


# =============================================================================
# KLING AI VIDEO GENERATION HELPERS
# =============================================================================

def generate_kling_jwt_token():
    """Generate JWT token for Kling AI API authentication"""
    import jwt # Lazy import to prevent crash if library missing elsewhere
    if not KLING_ACCESS_KEY or not KLING_SECRET_KEY:
        return None

    headers = {
        "alg": "HS256",
        "typ": "JWT"
    }

    payload = {
        "iss": KLING_ACCESS_KEY,
        "exp": int(time.time()) + 1800,  # 30 minutes
        "nbf": int(time.time()) - 5
    }

    token = jwt.encode(payload, KLING_SECRET_KEY, algorithm="HS256", headers=headers)
    return token


def create_kling_video_task(image_url, prompt=None, duration="5"):
    """
    Create an image-to-video task on Kling AI
    Uses Kling 2.5 Turbo (kling-v2-master) in Professional mode
    """
    token = generate_kling_jwt_token()
    if not token:
        return {"error": "Kling AI not configured. Add KLING_ACCESS_KEY and KLING_SECRET_KEY."}

    url = f"{KLING_API_BASE_URL}/v1/videos/image2video"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    if not prompt:
        prompt = KLING_DEFAULT_PROMPT

    payload = {
        "model_name": "kling-v2-master",  # Kling 2.5 Turbo
        "mode": "pro",                     # Professional mode
        "duration": duration,              # "5" or "10"
        "image": image_url,
        "prompt": prompt,
        "negative_prompt": "blurry, distorted, low quality, watermark, text, hands touching product, shaky camera",
        "cfg_scale": 0.5
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        data = response.json()

        if data.get("code") == 0:
            task_id = data.get("data", {}).get("task_id")
            return {
                "success": True,
                "task_id": task_id,
                "message": "Video generation started"
            }
        else:
            return {"error": data.get("message", f"Kling API error: {data}")}

    except Exception as e:
        return {"error": str(e)}


def get_kling_video_result(task_id):
    """Poll Kling AI for video generation result"""
    token = generate_kling_jwt_token()
    if not token:
        return {"error": "Kling AI not configured"}

    url = f"{KLING_API_BASE_URL}/v1/videos/image2video/{task_id}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        data = response.json()

        if data.get("code") == 0:
            task_data = data.get("data", {})
            status = task_data.get("task_status", "unknown")

            status_map = {
                "submitted": "pending",
                "processing": "processing",
                "succeed": "completed",
                "failed": "failed"
            }

            result = {
                "status": status_map.get(status, status),
                "task_id": task_id,
                "raw_status": status
            }

            if status == "succeed":
                videos = task_data.get("task_result", {}).get("videos", [])
                if videos:
                    result["video_url"] = videos[0].get("url")
                    result["duration"] = videos[0].get("duration")
            elif status == "failed":
                result["error"] = task_data.get("task_status_msg", "Video generation failed")

            return result
        else:
            return {"error": data.get("message", "Unknown error")}

    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# AI VIDEO GENERATION ENDPOINTS
# =============================================================================

@ai_bp.route('/api/generate-video', methods=['POST'])
def api_generate_video():
    """Generate AI video for a product using Kling AI"""
    passkey = request.args.get('passkey')
    if passkey != DEV_PASSKEY:
        user = get_current_user()
        if not user:
            return jsonify({'error': 'Unauthorized'}), 401
        if not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json() or {}
    product_id = data.get('product_id')

    if not product_id:
        return jsonify({'error': 'product_id required'}), 400

    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    image_url = data.get('image_url') or product.ai_image_url or product.cached_image_url or product.image_url

    if not image_url:
        return jsonify({'error': 'No image available for this product'}), 400

    # Handle base64 images - Kling API accepts raw base64 (without data: prefix)
    if image_url.startswith('data:'):
        # Extract just the base64 part after the comma
        if ',' in image_url:
            image_url = image_url.split(',')[1]
        else:
            return jsonify({'error': 'Invalid base64 image format'}), 400

    duration = data.get('duration', '5')

    result = create_kling_video_task(image_url, duration=duration)

    if result.get('success'):
        product.ai_video_task_id = result['task_id']
        product.ai_video_status = 'pending'
        db.session.commit()

        return jsonify({
            'success': True,
            'task_id': result['task_id'],
            'product_id': product_id,
            'message': 'Video generation started. Poll /api/video-status for updates.'
        })
    else:
        return jsonify({'error': result.get('error', 'Unknown error')}), 500


@ai_bp.route('/api/video-status/<task_id>', methods=['GET'])
def api_video_status(task_id):
    """Check status of Kling AI video generation task"""
    result = get_kling_video_result(task_id)

    if result.get('status') == 'completed' and result.get('video_url'):
        product = Product.query.filter_by(ai_video_task_id=task_id).first()
        if product:
            product.ai_video_url = result['video_url']
            product.ai_video_status = 'completed'
            db.session.commit()
            result['product_id'] = product.product_id

    elif result.get('status') == 'failed':
        product = Product.query.filter_by(ai_video_task_id=task_id).first()
        if product:
            product.ai_video_status = 'failed'
            db.session.commit()

    return jsonify(result)


@ai_bp.route('/api/one-click-video', methods=['POST'])
def api_one_click_video():
    """
    One-click: Generate AI Image (Gemini) -> Generate AI Video (Kling)

    POST body:
    {
        "product_id": "xxx",
        "category": "beauty"  # Optional: beauty, home, fitness, tech, fashion, default
    }
    """
    passkey = request.args.get('passkey')
    data = request.get_json() or {}

    # Check auth - accept passkey from query OR body
    if passkey != DEV_PASSKEY and data.get('passkey') != DEV_PASSKEY:
        user = get_current_user()
        if not user:
            return jsonify({'error': 'Unauthorized'}), 401
        if not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403

    product_id = data.get('product_id')
    category = data.get('category', 'default')
    skip_image = data.get('skip_image', False)

    if not product_id:
        return jsonify({'error': 'product_id required'}), 400

    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    image_base64 = None

    # Step 1: Generate AI Image (unless we're skipping or already have one)
    if not skip_image or not product.ai_image_url:
        if not GEMINI_API_KEY:
            return jsonify({'error': 'Gemini API not configured for image generation'}), 500

        category_prompts = {
            "beauty": f"Professional product photography of {product.product_name[:100]}, elegant beauty product shot, soft lighting, luxury aesthetic, clean background, 9:16 vertical format",
            "home": f"Lifestyle home product photo of {product.product_name[:100]}, cozy modern home setting, warm natural lighting, 9:16 vertical format",
            "fitness": f"Dynamic fitness product shot of {product.product_name[:100]}, gym or outdoor setting, energetic lighting, 9:16 vertical format",
            "tech": f"Sleek technology product photo of {product.product_name[:100]}, modern minimalist setup, cool lighting, 9:16 vertical format",
            "fashion": f"Fashion product photography of {product.product_name[:100]}, stylish lifestyle shot, natural lighting, 9:16 vertical format",
            "default": f"Professional product lifestyle photography of {product.product_name[:100]}, clean modern aesthetic, soft studio lighting, 9:16 vertical TikTok format"
        }

        prompt = category_prompts.get(category, category_prompts["default"])

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-preview-image-generation:generateContent?key={GEMINI_API_KEY}"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["image", "text"],
                "imageDimension": "PORTRAIT_9_16"
            }
        }

        try:
            response = requests.post(url, json=payload, timeout=90)
            response.raise_for_status()
            gemini_data = response.json()

            candidates = gemini_data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "inlineData" in part:
                        mime_type = part["inlineData"].get("mimeType", "image/png")
                        image_base64 = part["inlineData"].get("data", "")

                        # Save to product with data: prefix for display
                        product.ai_image_url = f"data:{mime_type};base64,{image_base64}"
                        db.session.commit()
                        break

            if not image_base64:
                return jsonify({'error': 'Failed to generate AI image'}), 500

        except Exception as e:
            return jsonify({'error': f'Image generation failed: {str(e)}'}), 500
    else:
        # Use existing AI image
        if product.ai_image_url and product.ai_image_url.startswith('data:'):
            if ',' in product.ai_image_url:
                image_base64 = product.ai_image_url.split(',')[1]

    # Step 2: Generate Video with Kling AI
    if not KLING_ACCESS_KEY or not KLING_SECRET_KEY:
        return jsonify({
            'success': True,
            'image_generated': True,
            'image_url': product.ai_image_url,
            'video_started': False,
            'message': 'Image generated but Kling AI not configured for video'
        })

    if not image_base64:
        # Fallback to cached image URL if no base64 available
        fallback_url = product.cached_image_url or product.image_url
        if not fallback_url:
            return jsonify({'error': 'No image available for video generation'}), 400
        video_result = create_kling_video_task(fallback_url)
    else:
        # Use raw base64 (Kling accepts this per API docs)
        video_result = create_kling_video_task(image_base64)

    if video_result.get('success'):
        product.ai_video_task_id = video_result['task_id']
        product.ai_video_status = 'processing'
        db.session.commit()

        return jsonify({
            'success': True,
            'image_generated': True,
            'image_url': product.ai_image_url,
            'video_started': True,
            'video_task_id': video_result['task_id'],
            'video_status': 'processing',
            'message': 'Image generated, video processing. Poll /api/video-status for updates.',
            'product_id': product_id
        })
    else:
        return jsonify({
            'success': True,
            'image_generated': True,
            'image_url': product.ai_image_url,
            'video_started': False,
            'video_error': video_result.get('error', 'Unknown error'),
            'message': 'Image generated but video generation failed'
        })
