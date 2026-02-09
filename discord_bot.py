#!/usr/bin/env python3
"""
Brand Hunter Discord Bot
- Posts daily hot products to #hot-product-tracker channel
- Responds to TikTok Shop links in #product-lookup channel with product info
"""

import os
import re
import discord
from discord.ext import commands, tasks
from discord import Embed
from datetime import datetime, time, timezone
import requests
import asyncio

# Database setup - Import from main application to ensure model consistency
# Database setup - Import from main application to ensure model consistency
from app import app, db, Product, User, ApiKey

# Discord Config
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
HOT_PRODUCTS_CHANNEL_ID = int(os.environ.get('HOT_PRODUCTS_CHANNEL_ID', 0))
BRAND_HUNTER_CHANNEL_ID = int(os.environ.get('BRAND_HUNTER_CHANNEL_ID', 0))  # For daily brand hunter posts
PRODUCT_LOOKUP_CHANNEL_ID = 1461053839800139959
BLACKLIST_CHANNEL_ID = 1440369747467174019

# Hot Product Criteria - Free Shipping Deals
MIN_SALES_7D = 50  # Lower threshold since we're filtering by free shipping
MAX_VIDEO_COUNT = 30  # Low competition
MAX_DAILY_POSTS = 15  # Top 15 daily
DAYS_BEFORE_REPEAT = 3  # Don't show same product for 3 days

# Discord Config

# Model imported from app.py


# Discord Bot Setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)  # Disable default help

def extract_product_id(text):
    """Extract TikTok product ID from URL or text"""
    # Pattern for product ID in URLs
    patterns = [
        r'shop/pdp/(\d+)',
        r'product/(\d+)',
        r'view/product/(\d+)',
        r'product_id=(\d+)',
        r'/(\d{15,25})(?:[/?]|$)',  # Direct product ID (15-25 digits)
        r'productId=(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    
    # Check if it's just a raw product ID
    if re.match(r'^\d{15,25}$', text.strip()):
        return text.strip()
    
    return None

def resolve_tiktok_share_link(url):
    """Resolve TikTok share link to get Product ID by following redirects and scraping page"""
    print(f"🔍 [Bot] Resolving redirect for: {url}")
    
    try:
        # Use a mobile user agent as TikTok often redirects differently for desktop vs mobile
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        
        # Follow redirects and get the page content
        res = requests.get(url, allow_redirects=True, headers=headers, timeout=15)
        final_url = res.url
        print(f"✅ [Bot] Resolved URL: {final_url}")
        
        # Try to extract product ID from the final URL first
        pid = extract_product_id(final_url)
        if pid:
            print(f"✅ [Bot] Found product ID in URL: {pid}")
            return pid, 'US'
        
        # If URL doesn't have product ID, try to scrape it from page content
        # TikTok video pages with products often have product links in the HTML
        html_content = res.text
        
        # Look for product IDs in the HTML (common patterns in TikTok's JSON data)
        product_patterns = [
            r'"productId"\s*:\s*"(\d{15,25})"',
            r'"product_id"\s*:\s*"(\d{15,25})"',
            r'shop\.tiktok\.com/view/product/(\d{15,25})',
            r'/product/(\d{15,25})',
            r'"itemId"\s*:\s*"(\d{15,25})"',
            r'data-product-id="(\d{15,25})"',
        ]
        
        for pattern in product_patterns:
            match = re.search(pattern, html_content)
            if match:
                pid = match.group(1)
                print(f"✅ [Bot] Found product ID in page HTML: {pid}")
                return pid, 'US'
        
        # Final fallback: look for any 17-20 digit number that might be a product ID
        # Be more conservative here to avoid false positives
        potential_ids = re.findall(r'(\d{17,20})', final_url + html_content[:5000])
        if potential_ids:
            # Return the first unique one that looks like a product ID
            for pid in potential_ids:
                if len(pid) >= 17:
                    print(f"⚠️ [Bot] Using potential product ID from content: {pid}")
                    return pid, 'US'
        
        print(f"❌ [Bot] Could not extract product ID from {final_url}")
        
    except Exception as e:
        print(f"❌ [Bot] Resolution Error: {e}")

    return None, 'US'


def get_product_from_db(product_id):
    """Check if product exists in database first"""
    with app.app_context():
        product = Product.query.get(product_id)
        if product:
            return {
                'product_id': product.product_id,
                'product_name': product.product_name,
                'sales_7d': product.sales_7d,
                'sales_30d': product.sales_30d,
                'influencer_count': product.influencer_count,
                'video_count': product.video_count,
                'commission_rate': product.commission_rate,
                'shop_ads_commission': product.shop_ads_commission,  # GMV Max Ads
                'price': product.price,
                'image_url': product.cached_image_url or product.image_url,
                'from_database': True
            }
        return None

from app import enrich_product_data, fetch_copilot_products

def get_product_from_v2_api(product_id):
    """
    V2 API: Search Copilot /api/trending/products for accurate product stats.
    This provides much more accurate video_count, creator_count, and sales data.
    """
    try:
        print(f"🚀 [V2] Searching Copilot Products API for {product_id}...")
        raw_pid = str(product_id).replace('shop_', '')
        
        # Try multiple timeframes to find the product
        for timeframe in ['7d', 'all']:
            with app.app_context():
                res = fetch_copilot_products(timeframe=timeframe, limit=50)
            
            if not res or not res.get('products'):
                continue
            
            # Search for this specific product
            for p in res.get('products', []):
                if str(p.get('productId', '')) == raw_pid:
                    print(f"✅ [V2] Found product {product_id} in {timeframe} data!")
                    
                    # Extract V2 accurate stats
                    shop_pid = f"shop_{raw_pid}"
                    video_count = int(p.get('periodVideoCount') or p.get('adVideoCount') or 0)
                    creator_count = int(p.get('periodCreatorCount') or 0)
                    total_ad_spend = float(p.get('totalAdCost') or 0)
                    total_sales = int(p.get('unitsSold') or 0)
                    sales_7d = int(p.get('periodUnits') or 0)
                    gmv = float(p.get('periodRevenue') or 0)
                    
                    # Save to database
                    with app.app_context():
                        db_product = Product.query.get(shop_pid)
                        if not db_product:
                            db_product = Product(product_id=shop_pid)
                            db_product.first_seen = datetime.now(timezone.utc)
                            db.session.add(db_product)
                        
                        db_product.product_name = p.get('productTitle') or db_product.product_name
                        db_product.seller_name = p.get('sellerName') or db_product.seller_name
                        db_product.image_url = p.get('productCoverUrl') or db_product.image_url
                        db_product.video_count = video_count
                        db_product.influencer_count = creator_count
                        db_product.ad_spend_total = total_ad_spend
                        db_product.ad_spend = total_ad_spend * 0.15  # Estimate 7d as 15%
                        db_product.sales = total_sales
                        db_product.sales_7d = sales_7d
                        db_product.gmv = gmv
                        db_product.commission_rate = float(p.get('tapCommissionRate') or 0) / 10000.0
                        db_product.shop_ads_commission = float(p.get('tapShopAdsRate') or 0) / 10000.0
                        db_product.scan_type = 'bot_lookup_v2'
                        db_product.last_updated = datetime.now(timezone.utc)
                        db.session.commit()
                        
                        return {
                            'product_id': db_product.product_id,
                            'product_name': db_product.product_name,
                            'seller_name': db_product.seller_name,
                            'image_url': db_product.cached_image_url or db_product.image_url,
                            'video_count': video_count,
                            'influencer_count': creator_count,
                            'sales': total_sales,
                            'sales_7d': sales_7d,
                            'gmv': gmv,
                            'ad_spend': db_product.ad_spend,
                            'ad_spend_total': total_ad_spend,
                            'commission_rate': db_product.commission_rate,
                            'shop_ads_commission': db_product.shop_ads_commission,
                            'from_v2_api': True
                        }
        
        print(f"⚠️ [V2] Product {product_id} not found in trending data")
        return None
        
    except Exception as e:
        print(f"❌ [V2] Error: {e}")
        return None

def save_product_to_db(product_data):
    """
    Save enriched product data to database.
    Expects a dictionary with at least 'product_id'.
    """
    with app.app_context():
        raw_pid = str(product_data.get('product_id')).replace('shop_', '')
        shop_pid = f"shop_{raw_pid}"
        
        # Check if exists
        p = db.session.get(Product, shop_pid)
        if not p:
            p = Product(product_id=shop_pid)
            p.first_seen = datetime.now(timezone.utc)
            db.session.add(p)
            
        # Update fields
        p.scan_type = 'bot_lookup'
        p.product_name = product_data.get('product_name') or product_data.get('title') or p.product_name or "Unknown"
        p.image_url = product_data.get('image_url') or product_data.get('cover') or p.image_url
        p.sales = product_data.get('sales', 0)
        p.sales_7d = product_data.get('sales_7d', 0)
        p.sales_30d = product_data.get('sales_30d', 0)
        p.influencer_count = product_data.get('influencer_count', 0)
        p.video_count = product_data.get('video_count', 0)
        p.commission_rate = product_data.get('commission_rate', 0)
        p.shop_ads_commission = product_data.get('shop_ads_commission', 0)
        p.price = product_data.get('price', 0)
        p.gmv = product_data.get('gmv', 0)
        p.ad_spend = product_data.get('ad_spend', 0)
        p.ad_spend_total = product_data.get('ad_spend_total', 0)
        p.gmv_growth = product_data.get('gmv_growth', 0)
        p.last_updated = datetime.now(timezone.utc)
        p.live_count = product_data.get('live_count', 0)
        p.is_enriched = True
        
        try:
            db.session.commit()
        except Exception as e:
            print(f"DB Error in save_product_to_db: {e}")
            db.session.rollback()

        # Return DICT to avoid DetachedInstanceError
        return {
            'product_id': p.product_id,
            'product_name': p.product_name,
            'image_url': p.image_url,
            'sales': p.sales,
            'sales_7d': p.sales_7d,
            'sales_30d': p.sales_30d,
            'influencer_count': p.influencer_count,
            'video_count': p.video_count,
            'commission_rate': p.commission_rate,
            'shop_ads_commission': p.shop_ads_commission,
            'price': p.price,
            'gmv': p.gmv,
            'ad_spend': p.ad_spend,
            'gmv_growth': p.gmv_growth,
            'live_count': p.live_count,
            'has_free_shipping': p.has_free_shipping,
            'cached_image_url': p.cached_image_url
        }

def get_product_from_api(product_id):
    """
    Search Copilot for product stats.
    """
    try:
        print(f"🚀 Triggering Copilot Search for {product_id}...")
        
        # Create a temp product dict to pass to enricher
        # We need to create a skeletal DB object or just a dict?
        # enrich_product_data expects a dict-like object (p) that supports .get() and .update() / [] setting
        
        with app.app_context():
            # Check if exists in DB to update it
            # The bot uses numeric ID, app uses shop_ID
            numeric_id = str(product_id).replace('shop_', '')
            shop_pid = f"shop_{numeric_id}"
            
            p = Product.query.get(shop_pid)
            if not p:
                # Create a temporary/new product
                p = Product(product_id=shop_pid)
                p.first_seen = datetime.now(datetime.timezone.utc)
                db.session.add(p)
            
            # Convert to dict-like logic for the function?
            # actually enrich_product_data works on the SQLAlchemy object too because it supports __setitem__? 
            # No, standard SQLAlchemy objects don't support p['key'] assignment unless defined.
            # But the 'Product' model in this app might... let's check app.py later.
            # Assuming it does, or enrich_product_data handles it?
            # Re-reading app.py enrich_product_data:
            # p['sales'] = ... 
            # Yes, standard SQLAlchemy objects DO NOT support this.
            # So `enrich_product_data` in app.py probably expects a Dictionary OR the Product class has __setitem__.
            
            # Only way to be sure: `enrich_product_data` in app.py uses `p.get()` and `p['key'] = val`.
            # This implies `p` is a Dictionary.
            # But `app.py` passes the `Product` row object... 
            # Wait, `scan_target` in apify_service passed a dictionary.
            # Let's fix `enrich_product_data` compatibility or wrap the product.
            
            # Simple fix: Pass a dictionary, then update the DB object.
            
            temp_p = {
                'product_id': shop_pid,
                'is_enriched': False
            }
            
            # Call Echotik Logic
            success, msg = enrich_product_data(temp_p, force=True)
            
            if success:
                print(f"✅ Copilot found stats for {product_id}")
                
                # Update Real DB Object
                p.scan_type = 'bot_lookup'
                p.product_name = temp_p.get('product_name') or p.product_name or "Unknown Product"
                p.image_url = temp_p.get('image_url') or p.image_url
                p.sales = temp_p.get('sales', 0)
                p.sales_7d = temp_p.get('sales_7d', 0)
                p.sales_30d = temp_p.get('sales_30d', 0)
                p.influencer_count = temp_p.get('influencer_count', 0)
                p.video_count = temp_p.get('video_count', 0)
                p.commission_rate = temp_p.get('commission_rate', 0)
                p.shop_ads_commission = temp_p.get('shop_ads_commission', 0)
                p.price = temp_p.get('price', 0)
                p.gmv = temp_p.get('gmv', 0)
                p.ad_spend = temp_p.get('ad_spend', 0)
                p.ad_spend_total = temp_p.get('ad_spend_total', 0)
                p.gmv_growth = temp_p.get('gmv_growth', 0)
                p.last_updated = datetime.now(timezone.utc)
                p.live_count = temp_p.get('live_count', 0)
                
                db.session.commit()
                
                # Convert to dict before returning!
                return {
                    'product_id': p.product_id,
                    'product_name': p.product_name,
                    'sales': p.sales,
                    'sales_7d': p.sales_7d,
                    'sales_30d': p.sales_30d,
                    'influencer_count': p.influencer_count,
                    'video_count': p.video_count,
                    'commission_rate': p.commission_rate,
                    'shop_ads_commission': p.shop_ads_commission,
                    'price': p.price,
                    'gmv': p.gmv,
                    'ad_spend': p.ad_spend,
                    'gmv_growth': p.gmv_growth,
                    'image_url': p.cached_image_url or p.image_url,
                    'live_count': p.live_count,
                    'from_api': True
                }
            else:
                print(f"❌ Copilot search failed: {msg}")
                return None

    except Exception as e:
        print(f"Error fetching product {product_id}: {e}")
        return None

def get_product_data(product_id):
    """Get product - check database first. If not found, try V2 API then legacy."""
    
    with app.app_context():
        # Check DB
        # Note: The bot logic uses numeric ID, but app uses 'shop_' prefix.
        # We need to handle both lookups.
        db_product = db.session.get(Product, product_id) or db.session.get(Product, f"shop_{product_id}")
        
        # If found AND has valid stats (any source), return it
        # We trust 'sales_7d' > 0 as a sign of having data
        if db_product and (db_product.sales_7d > 0 or db_product.video_count > 0):
            print(f"✅ Product {product_id} found in database (Cached)")
            # Return as DICT to survive session close
            return {
                'product_id': db_product.product_id,
                'product_name': db_product.product_name,
                'sales': db_product.sales,
                'sales_7d': db_product.sales_7d,
                'sales_30d': db_product.sales_30d,
                'influencer_count': db_product.influencer_count,
                'video_count': db_product.video_count,
                'commission_rate': db_product.commission_rate,
                'shop_ads_commission': db_product.shop_ads_commission,  # GMV Max Ads
                'price': db_product.price,
                'image_url': db_product.cached_image_url or db_product.image_url,
                'has_free_shipping': db_product.has_free_shipping,
                'live_count': db_product.live_count
            }
    
    # Not found OR needs upgrade -> Try V2 API first (more accurate data)
    print(f"🔍 Product {product_id} needs scan/upgrade, trying V2 API...")
    v2_result = get_product_from_v2_api(product_id)
    if v2_result:
        return v2_result
    
    # V2 didn't find it -> Fallback to legacy Copilot search
    print(f"⚠️ V2 API didn't find {product_id}, falling back to legacy...")
    return get_product_from_api(product_id)


def create_product_embed(p, title_prefix=""):
    """Create a Discord embed for a product"""
    # Helper to safe get values whether p is dict or object
    def get_val(key, default=None):
        if isinstance(p, dict):
            return p.get(key, default)
        return getattr(p, key, default)

    product_name = get_val('product_name', 'Unknown Product')[:100]
    product_id = get_val('product_id', '')
    
    
    # Expanded helper to check multiple keys (DB vs API)
    def get_val_multi(keys, default=0):
        val = default
        for key in keys:
            if isinstance(p, dict):
                v = p.get(key)
            else:
                v = getattr(p, key, None)
            
            if v is not None:
                val = v
                break
        return val

    # Get stats with fallback to API keys
    total_sales = int(get_val_multi(['sales', 'total_sale_cnt'], 0) or 0) # Use TOTAL sales
    sales_7d = int(get_val_multi(['sales_7d', 'total_sale_7d_cnt'], 0) or 0)
    influencer_count = int(get_val_multi(['influencer_count', 'total_ifl_cnt'], 0) or 0)
    video_count = int(get_val_multi(['video_count', 'total_video_cnt'], 0) or 0)
    commission = float(get_val_multi(['commission_rate', 'product_commission_rate'], 0) or 0)
    shop_ads_commission = float(get_val_multi(['shop_ads_commission'], 0) or 0)
    price = float(get_val_multi(['price', 'spu_avg_price'], 0) or 0)
    original_price = float(get_val_multi(['original_price'], 0) or 0) # New field
    stock = int(get_val_multi(['live_count', 'stock'], 0) or 0) # live_count is proxy for stock
    has_free_shipping = get_val('has_free_shipping', False)
    
    # Format commission (handle 0.15 vs 15.0)
    if commission > 0 and commission < 1:
        commission = commission * 100
    
    # Calculate Display Price (Handle Sale)
    price_display = f"${price:.2f}"
    if original_price > price:
        discount_pct = int(((original_price - price) / original_price) * 100)
        if discount_pct > 0:
            price_display = f"~~${original_price:.2f}~~ **${price:.2f}** ({discount_pct}% OFF)"
    
    # Get image URL
    image_url = get_val('cached_image_url') or get_val('image_url') or get_val('cover') or get_val('cover_url', '')
    if isinstance(image_url, list) and len(image_url) > 0:
        image_url = image_url[0]
    
    # Determine embed color and opportunity based on VIDEO COUNT
    if video_count <= 10:
        color = 0xFF4500  # Orange-red (Hot/Untapped)
        opportunity = "🔥 UNTAPPED (1-10 vids)"
    elif video_count <= 30:
        color = 0x00FF00  # Green (Low Comp)
        opportunity = "💎 LOW COMPETITION (11-30 vids)"
    elif video_count <= 60:
        color = 0xFFD700  # Gold (Medium)
        opportunity = "📊 MEDIUM (31-60 vids)"
    else:
        color = 0x808080  # Gray (High)
        opportunity = "⚠️ HIGH COMPETITION (61+ vids)"
    
    title = f"{title_prefix}{product_name}"
    if has_free_shipping:
        title = "🎁 " + title
    
    # Strip 'shop_' prefix for the clean TikTok View URL
    view_id = product_id.replace('shop_', '') if product_id else ''

    embed = Embed(
        title=title,
        url=f"https://www.tiktok.com/t/{view_id}" if len(view_id) < 15 else f"https://shop.tiktok.com/view/product/{view_id}?region=US&locale=en-US",
        color=color
    )
    
    # Add stats fields (Stock Removed per user request)
    # embed.add_field(name="📦 Stock", value=f"{stock:,}", inline=True) <-- REMOVED
    
    embed.add_field(name="📉 7 Day Sales", value=f"{sales_7d:,}", inline=True)
    embed.add_field(name="💸 Ad Spend", value=f"${float(get_val('ad_spend', 0)):,.2f}", inline=True)
    embed.add_field(name="🏷️ Price", value=f"${price:,.2f}", inline=True)
    
    # Commission display
    commission_display = f"{commission:.1f}%"
    embed.add_field(name="💵 Commission", value=commission_display, inline=True)
    
    embed.add_field(name="✨ Total Sales", value=f"{total_sales:,}", inline=True)
    embed.add_field(name="🎬 Total Videos", value=f"**{video_count:,}**", inline=True)
    embed.add_field(name="👥 Creators", value=f"{influencer_count:,}", inline=True)
    
    # Brand field
    seller_name = get_val('seller_name')
    if seller_name and seller_name not in ['Unknown', 'Unknown Seller', '']:
        embed.add_field(name="🏷️ Brand", value=f"{seller_name}", inline=True)
    
    # Opportunity field
    embed.add_field(name="🎯 Opportunity", value=f"**{opportunity}**", inline=False)
    
    # Add image if available
    if image_url and str(image_url).startswith('http'):
        embed.set_thumbnail(url=image_url)
    
    # BLACKLIST WARNING
    from app import is_brand_blacklisted
    seller_name = get_val('seller_name')
    seller_id = get_val('seller_id')
    
    with app.app_context():
        if is_brand_blacklisted(seller_name=seller_name, seller_id=seller_id):
            embed.description = "⚠️ **WARNING: BLACKLISTED BRAND/SELLER**\nThis seller has been reported for removing commission rates or other scams."
            embed.color = 0xFF0000 # Red for alert
    
    embed.set_footer(text=f"Product ID: {product_id}")
    embed.timestamp = datetime.now(timezone.utc)
    
    return embed


@bot.event
async def on_ready():
    print(f'🤖 Bot logged in as {bot.user}')
    print(f'   Hot Products Channel: {HOT_PRODUCTS_CHANNEL_ID}')
    print(f'   Brand Hunter Channel: {BRAND_HUNTER_CHANNEL_ID}')
    print(f'   Product Lookup Channel: {PRODUCT_LOOKUP_CHANNEL_ID}')
    
    # Start the daily hot products task
    if not daily_hot_products.is_running():
        daily_hot_products.start()
    
    # Start the daily brand hunter task
    if BRAND_HUNTER_CHANNEL_ID and not daily_brand_hunter.is_running():
        daily_brand_hunter.start()

@tasks.loop(time=time(hour=17, minute=0))  # 12:00 PM EST = 17:00 UTC
async def daily_hot_products():
    """Post daily hot products at noon EST"""
    if not HOT_PRODUCTS_CHANNEL_ID:
        print("No hot products channel configured")
        return
    
    channel = bot.get_channel(HOT_PRODUCTS_CHANNEL_ID)
    if not channel:
        print(f"Could not find channel {HOT_PRODUCTS_CHANNEL_ID}")
        return
    
    print(f"🎁 Posting daily free shipping deals at {datetime.now(timezone.utc).isoformat()}")
    
    products = get_hot_products()
    
    if not products:
        await channel.send("📭 No products matching criteria today (40-120 videos, 100+ 7D sales, $100+ ad spend, commission > 0). Try syncing more products from Copilot!")
        return
    
    # Send header message
    await channel.send(f"# 🔥 Daily Hot Picks - {datetime.now(timezone.utc).strftime('%B %d, %Y')}\n"
                       f"**Criteria:** 40-120 all-time videos, $100+ ad spend, 100+ 7D sales\n"
                       f"**Today's Picks:** {len(products)} products\n"
                       f"──────────────────────────────")
    
    # Send each product as an embed
    for i, p in enumerate(products, 1):
        try:
            embed = create_product_embed(p, title_prefix=f"#{i} ")
            await channel.send(embed=embed)
            await asyncio.sleep(1)  # Rate limiting
        except Exception as e:
            print(f"❌ Error sending product #{i} ({getattr(p, 'product_id', 'unknown')}): {e}")
            # Try sending a basic error message to the channel so we know it failed here
            try:
                await channel.send(f"⚠️ Failed to display product #{i}. Check logs.")
            except:
                pass
    
    print(f"   Finished hot products loop.")


def get_top_brand_opportunities(limit=10):
    """Get top opportunity products from top revenue brands (50-300 all-time videos)."""
    with app.app_context():
        from sqlalchemy import func
        
        # Get top brands by revenue
        top_brands = db.session.query(
            Product.seller_name,
            func.sum(Product.gmv).label('total_revenue')
        ).filter(
            Product.seller_name != None,
            Product.seller_name != '',
            Product.seller_name != 'Unknown',
            ~Product.seller_name.ilike('unknown%'),
            ~Product.seller_name.ilike('classified%')
        ).group_by(Product.seller_name).order_by(func.sum(Product.gmv).desc()).limit(50).all()
        
        brand_names = [b[0] for b in top_brands]
        print(f"[Brand Hunter Daily] Top {len(brand_names)} brands: {brand_names[:5]}...")
        
        # Get opportunity products from these brands (40-500 all-time videos for broader selection)
        # Use video_count_alltime for saturation metric
        video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)
        
        products = Product.query.filter(
            Product.seller_name.in_(brand_names),
            video_count_field >= 50,
            video_count_field <= 300  # Max 300 all-time videos
        ).order_by(
            video_count_field.asc(),  # Priority: Lower videos = better opportunity
            Product.sales_7d.desc().nullslast()
        ).limit(limit).all()
        
        print(f"[Brand Hunter Daily] Found {len(products)} opportunity products from top {len(brand_names)} brands")
        
        # Convert to dicts - use video_count_alltime
        product_dicts = []
        for p in products:
            video_count = p.video_count_alltime or p.video_count or 0
            product_dicts.append({
                'product_id': p.product_id,
                'product_name': p.product_name,
                'seller_name': p.seller_name,
                'sales': p.sales,  # Total sales
                'sales_7d': p.sales_7d,
                'video_count': video_count,  # All-time video count
                'influencer_count': p.influencer_count,
                'ad_spend': p.ad_spend,
                'commission_rate': p.commission_rate,
                'shop_ads_commission': p.shop_ads_commission,
                'price': p.price,
                'image_url': p.cached_image_url or p.image_url,
            })
        
        return product_dicts


@tasks.loop(time=time(hour=17, minute=5))  # 12:05 PM EST = 17:05 UTC (5 min after hot products)
async def daily_brand_hunter():
    """Post daily brand hunter opportunities at noon EST"""
    if not BRAND_HUNTER_CHANNEL_ID:
        print("No brand hunter channel configured")
        return
    
    channel = bot.get_channel(BRAND_HUNTER_CHANNEL_ID)
    if not channel:
        print(f"Could not find brand hunter channel {BRAND_HUNTER_CHANNEL_ID}")
        return
    
    print(f"🎯 Posting daily brand hunter at {datetime.now(timezone.utc).isoformat()}")
    
    try:
        products = get_top_brand_opportunities(limit=10)
        
        if not products:
            await channel.send("📭 No brand opportunity products found today (40-300 all-time videos from top brands). Check back tomorrow!")
            return
        
        # Send header message
        await channel.send(f"# 🎯 Daily Brand Opportunities - {datetime.now(timezone.utc).strftime('%B %d, %Y')}\n"
                           f"**Criteria:** Top 50 Revenue Brands, 40-300 all-time videos (sorted by lowest)\n"
                           f"**Today's Picks:** {len(products)} products from proven brands\n"
                           f"──────────────────────────────")
        
        # Send each product as an embed
        for i, p in enumerate(products, 1):
            try:
                embed = create_product_embed(p, title_prefix=f"#{i} ")
                await channel.send(embed=embed)
                await asyncio.sleep(1)  # Rate limiting
            except Exception as e:
                print(f"❌ Error sending brand product #{i}: {e}")
        
        print(f"   Finished brand hunter loop.")
    except Exception as e:
        print(f"❌ Error in daily_brand_hunter: {e}")
        import traceback
        traceback.print_exc()
        await channel.send(f"❌ Error fetching brand opportunities: {str(e)[:200]}")

@bot.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        return
    
    # Check if message is in the product lookup channel
    if message.channel.id == PRODUCT_LOOKUP_CHANNEL_ID:
        # Look for TikTok product links
        tiktok_patterns = [
            r'tiktok\.com.*product',
            r'shop\.tiktok\.com',
            r'vm\.tiktok\.com',
            r'/t/\w+',
        ]
        
        content = message.content
        has_tiktok_link = any(re.search(pattern, content, re.IGNORECASE) for pattern in tiktok_patterns)
        
        if has_tiktok_link or re.search(r'\d{15,25}', content):
            # React to show we're processing
            await message.add_reaction('🔍')
            
            # Try to resolve share links first
            url_pattern = r'https?://[^\s]+'
            urls = re.findall(url_pattern, content)
            
            product_id = None
            region = 'US'
            
            # Check if any URL is a share link that needs resolving
            for url in urls:
                if 'vm.tiktok.com' in url or '/t/' in url or 'tiktok.com' in url:
                    resolved_pid, reg = resolve_tiktok_share_link(url)
                    if resolved_pid:
                        product_id = resolved_pid
                        region = reg
                        print(f"🎯 [Bot] Got product ID from link resolution: {product_id}")
                        break
            
            # If no product ID from link resolution, try extracting from the raw message
            if not product_id:
                product_id = extract_product_id(content)
            
            if not product_id:
                await message.add_reaction('❌')
                await message.reply("❌ Could not find a valid TikTok product ID. This might be a regular video link without a tagged product.", mention_author=False)
                return

            
            # Fetch product (database first, then API)
            product = get_product_data(product_id)
            
            # If not in DB, try to fetch from API
            if not product:
                status_msg = await message.reply(f"🔎 Fetching fresh data from Copilot for `{product_id}`...", mention_author=False)
                
                # We need a dummy dict to pass to enrich_product_data since it expects a dict
                dummy_p = {'product_id': product_id, 'region': region}
                success, msg = enrich_product_data(dummy_p, i_log_prefix="[BotAutoLookup]", force=True)
                
                await status_msg.delete() # Remove searching message
                
                if success:
                    # Save it to DB so get_product_data works
                    new_prod = save_product_to_db(dummy_p) 
                    if new_prod:
                         product = new_prod
                else:
                    await message.reply(f"❌ Copilot search failed: {msg}", mention_author=False)
                    return

            if not product:
                await message.add_reaction('❌')
                await message.reply(f"❌ Lookup Failed: {msg or 'Unknown Error'} (ID: {product_id})", mention_author=False)
                return
            
            # Create and send embed
            embed = create_product_embed(product)
            await message.reply(embed=embed, mention_author=False)
            
            # Update reaction
            await message.remove_reaction('🔍', bot.user)
            await message.add_reaction('✅')
    
    # Process other commands
    await bot.process_commands(message)

@bot.command(name='lookup')
async def lookup_command(ctx, *, query: str = None):
    """Manual lookup command: !lookup <product_id or URL>"""
    if not query:
        await ctx.reply("Usage: `!lookup <product_id or TikTok URL>`", mention_author=False)
        return

    # =========================================================================
    # CREDIT SYSTEM CHECK (DISABLED - FREE MODE)
    # =========================================================================
    ADMIN_IDS = [274339622119669760]
    is_admin = ctx.author.id in ADMIN_IDS
    user_credits = "Unlimited" # Free mode
    
    # Logic bypassed - Bot is now free for everyone
    # if not is_admin: ... (removed)
        
    # =========================================================================
    # EXECUTE SCAN
    # =========================================================================
    
    await ctx.message.add_reaction('🔍')
    if is_admin:
        status_msg = await ctx.reply(f"👑 **Admin Bypass** | Scanning... ", mention_author=False)
    else:
        status_msg = await ctx.reply(f"🎁 **Free Product Lookup** | Scanning...", mention_author=False)
    
    # Try to resolve if it's a share link
    region = 'US'
    if 'vm.tiktok.com' in query or '/t/' in query or 'tiktok.com' in query:
        # Use API to get ID directly
        extracted_id, reg = resolve_tiktok_share_link(query)
        if extracted_id:
            query = extracted_id # Query is now the ID
            region = reg
    
    product_id = extract_product_id(query)
    
    if not product_id:
        await ctx.message.add_reaction('❌')
        await status_msg.edit(content="❌ Could not extract product ID from your input.")
        return
    
    
    product = get_product_data(product_id)
    
    # If not in DB, try to fetch from API
    if not product:
        await status_msg.edit(content=f"🔎 Fetching fresh data from Copilot for `{product_id}`...")
        
        # We need a dummy dict to pass to enrich_product_data since it expects a dict
        dummy_p = {'product_id': product_id, 'region': region}
        success, msg = enrich_product_data(dummy_p, i_log_prefix="[BotLookup]", force=True)
        
        if success:
            # Save it to DB so get_product_data works
            new_prod = save_product_to_db(dummy_p) 
            if new_prod:
                product = new_prod
        else:
             await status_msg.edit(content=f"❌ Copilot search failed: {msg}")
             return
    
    if not product:
        await ctx.message.add_reaction('❌')
        # Use simple variable 'msg' which might capture the error from the enrich call above
        # Note: 'msg' scope needs care. Initializing it safely.
        await status_msg.edit(content=f"❌ Lookup Failed: {locals().get('msg', 'Product not found in DB/API')} (ID: {product_id})")
        return
    
    await status_msg.delete()
    embed = create_product_embed(product)
    
    # Add Credit Footer
    if not is_admin:
        embed.set_footer(text=f"Credits Remaining: {user_credits} | ID: {product_id}")
    else:
        embed.set_footer(text=f"👑 Admin Mode | ID: {product_id}")
        
    await ctx.reply(embed=embed, mention_author=False)
    await ctx.message.remove_reaction('🔍', bot.user)
    await ctx.message.add_reaction('✅')

# =============================================================================
# BLACKLIST COMMANDS
# =============================================================================

# Roles that can manage the blacklist (in addition to admins)
BLACKLIST_ALLOWED_ROLES = ['Team', 'Brand Manager', 'Moderator', 'Mod', 'Staff', 'Admin']

def can_manage_blacklist(ctx):
    """Check if user can manage blacklist: admin OR has an allowed role"""
    # Admins always have access
    if ctx.author.guild_permissions.administrator:
        return True
    # Check if user has any of the allowed roles
    user_role_names = [role.name for role in ctx.author.roles]
    for allowed_role in BLACKLIST_ALLOWED_ROLES:
        if any(allowed_role.lower() in role.lower() for role in user_role_names):
            return True
    return False

@bot.group(name='blacklist', invoke_without_command=True)
async def blacklist_group(ctx):
    """Blacklist management: !blacklist <add|remove|list|scan>"""
    await ctx.reply("Usage: `!blacklist <add|remove|list|scan>`", mention_author=False)

@blacklist_group.command(name='add')
async def blacklist_add(ctx, *, input_text: str = None):
    """Add a brand to the blacklist: !blacklist add Brand Name [| reason]"""
    if not input_text:
        await ctx.reply("Usage: `!blacklist add Brand Name` or `!blacklist add Brand Name | reason`", mention_author=False)
        return
    
    # Parse: "Brand Name | reason" or just "Brand Name"
    if '|' in input_text:
        parts = input_text.split('|', 1)
        brand_name = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else "No reason provided"
    else:
        brand_name = input_text.strip()
        reason = "No reason provided"
    
    from app import BlacklistedBrand
    with app.app_context():
        existing = BlacklistedBrand.query.filter(BlacklistedBrand.seller_name.ilike(brand_name)).first()
        if existing:
            await ctx.reply(f"⚠️ Brand `{brand_name}` is already on the blacklist.", mention_author=False)
            return
        
        new_entry = BlacklistedBrand(seller_name=brand_name, reason=reason)
        db.session.add(new_entry)
        db.session.commit()
    
    await ctx.reply(f"✅ Added `{brand_name}` to the blacklist.", mention_author=False)


@blacklist_group.command(name='reset')
@commands.has_permissions(administrator=True)
async def blacklist_reset(ctx):
    """Admin only: Totally clear the brand blacklist"""
    from app import BlacklistedBrand
    with app.app_context():
        num_rows = BlacklistedBrand.query.delete()
        db.session.commit()
    await ctx.reply(f"🧹 Blacklist cleared! Removed **{num_rows}** entries.", mention_author=False)

@blacklist_group.command(name='remove')
async def blacklist_remove(ctx, *, brand_name: str):
    """Remove a brand from the blacklist: !blacklist remove Brand Name"""
    from app import BlacklistedBrand
    brand_name = brand_name.strip().replace('"', '').replace("'", "")
    with app.app_context():
        existing = BlacklistedBrand.query.filter(BlacklistedBrand.seller_name.ilike(brand_name)).first()
        if not existing:
            await ctx.reply(f"⚠️ Brand `{brand_name}` not found on the blacklist.", mention_author=False)
            return
        
        db.session.delete(existing)
        db.session.commit()
    
    await ctx.reply(f"✅ Removed `{brand_name}` from the blacklist.", mention_author=False)

@blacklist_group.command(name='list')
async def blacklist_list(ctx):
    """List all blacklisted brands"""
    from app import BlacklistedBrand
    with app.app_context():
        brands = BlacklistedBrand.query.all()
        if not brands:
            await ctx.reply("📭 The blacklist is currently empty.", mention_author=False)
            return
        
        text = "**🚫 Blacklisted Brands/Sellers**\n"
        for i, b in enumerate(brands, 1):
            text += f"{i}. **{b.seller_name}** - {b.reason or 'No reason'} (Added: {b.added_at.strftime('%Y-%m-%d')})\n"
            if len(text) > 1800:
                await ctx.send(text)
                text = ""
        
        if text:
            await ctx.send(text)

def get_hot_products():
    """Get Top Products - Sorted by Ad Spend (high first), then Video Count
    
    V2 Update: Filters adjusted for accurate video counts from /api/trending/products.
    Old counts were ~20-40, new accurate counts are 100-17,000+.
    """
    from datetime import timedelta
    
    with app.app_context():
        # Calculate cutoff date for repeat prevention
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=DAYS_BEFORE_REPEAT)
        
        # Query: Products in opportunity zone (40-120 videos), matching v6.0 filters
        video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)
        products = Product.query.filter(
            video_count_field >= 40,  # Min 40 all-time videos
            video_count_field <= 120,  # Max 120 all-time videos (opportunity zone)
            Product.sales_7d >= 100,  # 100+ 7D sales
            Product.ad_spend >= 100,  # $100+ ad spend
            Product.commission_rate > 0,  # Must have commission
            # Exclude unenriched products (alltime == period means enrichment didn't work)
            Product.video_count_alltime != None,
            Product.video_count_alltime != Product.video_count,
            db.or_(
                Product.last_shown_hot == None,
                Product.last_shown_hot < cutoff_date
            )
        ).order_by(
            db.func.coalesce(Product.ad_spend, 0).desc(),  # Priority 1: High Ad Spend
            db.func.coalesce(Product.sales_7d, 0).desc(),  # Priority 2: High 7D Sales
            video_count_field.asc()  # Priority 3: Lower videos = better opportunity
        ).limit(MAX_DAILY_POSTS * 3).all()  # Fetch extra to allow deduplication
        
        print(f"[Hot Products] Found {len(products)} candidates matching filters (before dedup)")
        
        # Deduplicate by product_name (same product can have multiple IDs)
        seen_names = set()
        unique_products = []
        for p in products:
            # Normalize name for comparison (lowercase, strip whitespace)
            name_key = (p.product_name or '').lower().strip()[:50]  # First 50 chars
            if name_key and name_key not in seen_names:
                seen_names.add(name_key)
                unique_products.append(p)
                if len(unique_products) >= MAX_DAILY_POSTS:
                    break
        
        # Convert to dicts BEFORE commit to avoid DetachedInstanceError
        product_dicts = []
        for p in unique_products:
            video_count = p.video_count_alltime or p.video_count or 0  # All-time video count
            p_dict = {
                'product_id': p.product_id,
                'product_name': p.product_name,
                'seller_name': p.seller_name,
                'sales': p.sales,  # Total sales
                'sales_7d': p.sales_7d,
                'sales_30d': p.sales_30d,
                'influencer_count': p.influencer_count,
                'video_count': video_count,  # All-time video count!
                'commission_rate': p.commission_rate,
                'shop_ads_commission': p.shop_ads_commission,  # GMV Max Ads commission
                'price': p.price,
                'ad_spend': p.ad_spend,
                'image_url': p.cached_image_url or p.image_url,
                'cached_image_url': p.cached_image_url,
                'has_free_shipping': p.has_free_shipping,
                'live_count': p.live_count,
                'stock': p.live_count
            }
            product_dicts.append(p_dict)
            
            # Mark products as shown today
            p.last_shown_hot = datetime.now(timezone.utc)
        
        try:
            db.session.commit()
        except Exception as e:
            print(f"Error updating last_shown_hot: {e}")
            db.session.rollback()
            
        return product_dicts

@bot.command(name='hotproducts')
@commands.has_permissions(administrator=True)
async def force_hot_products(ctx):
    """Admin command to force post hot products"""
    await ctx.reply("🔥 Posting hot products now...", mention_author=False)
    await daily_hot_products()


@bot.command(name='brandhunter')
@commands.has_permissions(administrator=True)
async def force_brand_hunter(ctx):
    """Admin command to force post brand hunter products to current channel"""
    await ctx.reply("🎯 Fetching brand hunter opportunities...", mention_author=False)
    
    try:
        products = get_top_brand_opportunities(limit=10)
        print(f"[!brandhunter] Retrieved {len(products) if products else 0} products")
        
        if not products:
            await ctx.send("📭 No brand opportunity products found (40-300 all-time videos from top brands).")
            return
        
        # Send header message
        await ctx.send(f"# 🎯 Brand Opportunities\n"
                       f"**Criteria:** Top 50 Revenue Brands, 40-300 all-time videos\n"
                       f"**Found:** {len(products)} products from proven brands\n"
                       f"──────────────────────────────")
        
        # Send each product as an embed
        for i, p in enumerate(products, 1):
            try:
                embed = create_product_embed(p, title_prefix=f"#{i} ")
                await ctx.send(embed=embed)
                await asyncio.sleep(1)  # Rate limiting
            except Exception as e:
                print(f"❌ Error sending brand product #{i}: {e}")
                await ctx.send(f"⚠️ Error displaying product #{i}")
        
        await ctx.send("✅ Brand hunter complete!")
    except Exception as e:
        print(f"❌ Error in force_brand_hunter: {e}")
        import traceback
        traceback.print_exc()
        await ctx.send(f"❌ Error: {str(e)[:500]}")

# =============================================================================
# BRAND HUNTER COMMANDS
# =============================================================================

def get_brand_products(brand_name, limit=5):
    """Get top products for a brand with opportunity criteria (50-120 all-time videos)."""
    with app.app_context():
        # Search by seller_name (brand name)
        # Use video_count_alltime (all-time saturation metric) with fallback to video_count
        video_count_field = db.func.coalesce(Product.video_count_alltime, Product.video_count)
        
        products = Product.query.filter(
            Product.seller_name.ilike(f'%{brand_name}%'),
            video_count_field >= 50,  # Min 50 all-time videos
            video_count_field <= 120,  # Max 120 all-time videos (opportunity zone)
        ).order_by(
            video_count_field.asc(),  # Priority 1: Lower videos = better opportunity
            Product.ad_spend.desc().nullslast(),  # Priority 2: High Ad Spend
            Product.sales_7d.desc().nullslast(),  # Priority 3: High 7D Sales
        ).limit(limit).all()
        
        print(f"[Brand Hunt] Found {len(products)} products for '{brand_name}'")
        
        # Convert to dicts to avoid DetachedInstanceError
        # Use video_count_alltime for display (same as filter)
        product_dicts = []
        for p in products:
            video_count = p.video_count_alltime or p.video_count or 0
            product_dicts.append({
                'product_id': p.product_id,
                'product_name': p.product_name,
                'seller_name': p.seller_name,
                'sales': p.sales,  # Total sales
                'sales_7d': p.sales_7d,
                'video_count': video_count,  # All-time video count
                'influencer_count': p.influencer_count,
                'ad_spend': p.ad_spend,
                'commission_rate': p.commission_rate,
                'shop_ads_commission': p.shop_ads_commission,
                'price': p.price,
                'image_url': p.cached_image_url or p.image_url,
            })
        
        return product_dicts

def get_popular_brands(limit=20):
    """Get top brands by product count and ad spend."""
    with app.app_context():
        # Get brands with most products and highest total ad spend
        from sqlalchemy import func
        brands = db.session.query(
            Product.seller_name,
            func.count(Product.product_id).label('product_count'),
            func.sum(Product.ad_spend).label('total_ad_spend'),
            func.avg(Product.sales_7d).label('avg_sales')
        ).filter(
            Product.seller_name != None,
            Product.seller_name != '',
            Product.seller_name != 'Unknown',
            Product.seller_name != 'Unknown Seller',
            Product.ad_spend > 0,
        ).group_by(
            Product.seller_name
        ).order_by(
            func.sum(Product.ad_spend).desc()
        ).limit(limit).all()
        
        return brands

@bot.command(name='brand')
async def brand_command(ctx, *, args: str = None):
    """Brand Hunter command: !brand list OR !brand <brandname>"""
    if not args:
        await ctx.reply("**Usage:**\n`!brand list` - Show popular brands\n`!brand <name>` - Search brand products\n\nOr use `!brandname` directly (e.g., `!qvc`, `!shark`)", mention_author=False)
        return
    
    args_lower = args.lower().strip()
    
    if args_lower == 'list':
        # Show popular brands
        await ctx.message.add_reaction('🔍')
        brands = get_popular_brands(15)
        
        if not brands:
            await ctx.reply("📭 No brands found in database.", mention_author=False)
            return
        
        embed = Embed(
            title="🎯 Popular Brands",
            description="Top brands by ad spend. Use `!brandname` to see their products.",
            color=0x2563eb
        )
        
        brand_list = []
        for i, (name, count, spend, avg_sales) in enumerate(brands, 1):
            spend_display = f"${float(spend or 0):,.0f}"
            brand_list.append(f"**{i}.** {name} ({count} products, {spend_display} spend)")
        
        embed.add_field(name="Top Brands", value="\n".join(brand_list[:10]), inline=False)
        if len(brand_list) > 10:
            embed.add_field(name="More Brands", value="\n".join(brand_list[10:]), inline=False)
        
        embed.set_footer(text="Tip: Type !shark or !qvc to see products")
        await ctx.reply(embed=embed, mention_author=False)
        await ctx.message.remove_reaction('🔍', bot.user)
        await ctx.message.add_reaction('✅')
    else:
        # Search for brand products
        await search_brand(ctx, args)

async def search_brand(ctx, brand_name: str):
    """Search for products by brand name."""
    await ctx.message.add_reaction('🔍')
    
    products = get_brand_products(brand_name, limit=5)
    
    if not products:
        await ctx.reply(f"📭 No products found for **{brand_name}** with 40-120 videos.\n\nTry a different brand or check spelling.", mention_author=False)
        await ctx.message.remove_reaction('🔍', bot.user)
        await ctx.message.add_reaction('❌')
        return
    
    # Send header
    await ctx.reply(f"# 🎯 Brand Hunter: {brand_name.upper()}\n"
                    f"**Found {len(products)} opportunity products** (40-120 videos)\n"
                    f"──────────────────────────────", mention_author=False)
    
    # Send each product as embed
    for i, p in enumerate(products, 1):
        try:
            embed = create_product_embed(p, title_prefix=f"#{i} ")
            await ctx.send(embed=embed)
            await asyncio.sleep(0.5)  # Rate limiting
        except Exception as e:
            print(f"❌ Error sending brand product #{i}: {e}")
    
    await ctx.message.remove_reaction('🔍', bot.user)
    await ctx.message.add_reaction('✅')

@bot.command(name='help')
async def help_command(ctx):
    """Show available commands"""
    embed = Embed(
        title="🤖 Brand Hunter Bot - Commands",
        description="Your TikTok Shop product intelligence assistant.",
        color=0x2563eb
    )
    
    embed.add_field(
        name="🎯 Brand Hunter",
        value="**`!brandname`** - Get top products for any brand\n"
              "Examples: `!qvc`, `!shark`, `!ninja`, `!conair`\n\n"
              "**`!brand list`** - Show popular brands\n"
              "**`!brand <name>`** - Search brand products",
        inline=False
    )
    
    embed.add_field(
        name="🔍 Product Lookup",
        value="**`!lookup <url or ID>`** - Lookup any TikTok product\n"
              "Or just paste a TikTok Shop link in #product-lookup",
        inline=False
    )
    
    embed.add_field(
        name="🚫 Blacklist",
        value="**`!blacklist add <brand>`** - Report a scam brand\n"
              "**`!blacklist list`** - View blacklisted brands",
        inline=False
    )
    
    embed.add_field(
        name="📊 Criteria",
        value="• **Opportunity Zone:** 21-100 total videos\n"
              "• Sorted by: Ad Spend → 7D Sales → Lowest Videos\n"
              "• Only products with active ad spend",
        inline=False
    )
    
    embed.set_footer(text="🔥 Hot products posted daily at 12 PM EST")
    await ctx.reply(embed=embed, mention_author=False)

# Catch-all for brand shortcuts like !qvc, !shark
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # Only respond to brand searches in brand-hunter channel
        channel_name = ctx.channel.name.lower() if hasattr(ctx.channel, 'name') else ''
        if 'brand' not in channel_name and 'hunter' not in channel_name:
            # Silently ignore commands in non-brand-hunter channels
            return
        
        # Treat unknown commands as brand searches
        # Get everything after the ! as potential brand name
        content = ctx.message.content.strip()
        if content.startswith('!'):
            brand_search = content[1:].strip()  # Remove the ! prefix
            
            # Only trigger if it's at least 2 chars
            if len(brand_search) >= 2:
                print(f"[Brand Hunt] Searching for: {brand_search}")
                await search_brand(ctx, brand_search)
            else:
                # Silently ignore very short commands
                pass
    else:
        # Log other errors
        print(f"[Bot Error] {type(error).__name__}: {error}")

if __name__ == '__main__':
    if not DISCORD_BOT_TOKEN:
        print("❌ DISCORD_BOT_TOKEN not set!")
        exit(1)
    
    print("🚀 Starting Brand Hunter Discord Bot...")
    bot.run(DISCORD_BOT_TOKEN)
