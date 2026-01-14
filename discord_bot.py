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
PRODUCT_LOOKUP_CHANNEL_ID = 1461053839800139959
BLACKLIST_CHANNEL_ID = 1440369747467174019

# Hot Product Criteria - Free Shipping Deals
MIN_SALES_7D = 50  # Lower threshold since we're filtering by free shipping
MAX_VIDEO_COUNT = 30  # Low competition
MAX_DAILY_POSTS = 10  # Top 10 daily
DAYS_BEFORE_REPEAT = 3  # Don't show same product for 3 days

# Discord Config

# Model imported from app.py


# Discord Bot Setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

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
    """Resolve TikTok share link to get Product ID by following redirects"""
    print(f"🔍 [Bot] Resolving redirect for: {url}")
    
    try:
        # Use a mobile user agent as TikTok often redirects differently for desktop vs mobile
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        
        # Use GET with stream=True to follow redirects but stop before downloading large content
        with requests.get(url, allow_redirects=True, headers=headers, timeout=10, stream=True) as res:
            final_url = res.url
            print(f"✅ [Bot] Resolved URL: {final_url}")
            
            # Use extract_product_id on the final URL
            pid = extract_product_id(final_url)
            if pid:
                return pid, 'US'
            
            # Final attempts with extra regex if extract_product_id missed something
            for pattern in [r'/(\d{15,25})', r'prod_id=(\d+)', r'product_id=(\d+)']:
                match = re.search(pattern, final_url)
                if match: return match.group(1), 'US'
        
    except Exception as e:
        print(f"❌ [Bot] Manual Resolution Error: {e}")

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

from app import enrich_product_data

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
    """Get product - check database first. If simple prefetch, upgrade it."""
    
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
    
    # Not found OR needs upgrade -> Call Scanner
    # Not found OR needs upgrade -> Call Scanner
    print(f"🔍 Product {product_id} needs scan/upgrade, calling Copilot...")
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
    shop_ads_commission = float(get_val_multi(['shop_ads_commission'], 0) or 0)  # GMV Max Ads commission
    price = float(get_val_multi(['price', 'spu_avg_price'], 0) or 0)
    original_price = float(get_val_multi(['original_price'], 0) or 0) # New field
    stock = int(get_val_multi(['live_count', 'stock'], 0) or 0) # live_count is proxy for stock
    has_free_shipping = get_val('has_free_shipping', False)
    has_gmv_max = shop_ads_commission > 0  # GMV Max Ads indicator
    
    # Format commission (handle 0.15 vs 15.0)
    if commission > 0 and commission < 1:
        commission = commission * 100
    
    # Format shop ads commission (handle 0.10 vs 10.0)
    if shop_ads_commission > 0 and shop_ads_commission < 1:
        shop_ads_commission = shop_ads_commission * 100
    
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
    if has_gmv_max:
        title = "🚀 " + title  # GMV Max Ads badge
    elif has_free_shipping:
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
    
    # Commission display - show both regular and shop ads commission
    if shop_ads_commission > 0:
        commission_display = f"{commission:.1f}% + **{shop_ads_commission:.1f}% 🚀**"
    else:
        commission_display = f"{commission:.1f}%"
    embed.add_field(name="💵 Commission", value=commission_display, inline=True)
    
    embed.add_field(name="✨ Total Sales", value=f"{total_sales:,}", inline=True)
    embed.add_field(name="🎬 Total Videos", value=f"**{video_count:,}**", inline=True)
    embed.add_field(name="👥 Creators", value=f"{influencer_count:,}", inline=True)
    
    # Opportunity field with GMV Max indicator
    if has_gmv_max:
        embed.add_field(name="🎯 Opportunity", value=f"**{opportunity}** | 🚀 GMV Max Ads", inline=False)
    else:
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
    print(f'   Product Lookup Channel: {PRODUCT_LOOKUP_CHANNEL_ID}')
    
    # Start the daily hot products task
    if not daily_hot_products.is_running():
        daily_hot_products.start()

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
        await channel.send("📭 No GMV Max products matching criteria today (10%+ shop ads, <40 videos, 100+ 7D sales, $500+ ad spend). Try syncing more products from Copilot!")
        return
    
    # Send header message
    await channel.send(f"# 🚀 Daily GMV Max Picks - {datetime.now(timezone.utc).strftime('%B %d, %Y')}\n"
                       f"**Criteria:** 10%+ Shop Ads Commission, <40 videos (low competition)\n"
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
            
            # Try to resolve share links
            url_pattern = r'https?://[^\s]+'
            urls = re.findall(url_pattern, content)
            
            resolved_url = content
            region = 'US'
            for url in urls:
                if 'vm.tiktok.com' in url or '/t/' in url:
                    resolved, reg = resolve_tiktok_share_link(url)
                    if resolved:
                        resolved_url = resolved
                        region = reg
                        break
            
            # Extract product ID
            product_id = extract_product_id(resolved_url)
            
            if not product_id:
                await message.add_reaction('❌')
                await message.reply("❌ Could not find a valid TikTok product ID in your message.", mention_author=False)
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

@bot.group(name='blacklist', invoke_without_command=True)
async def blacklist_group(ctx):
    """Blacklist management: !blacklist <add|remove|list|scan>"""
    await ctx.reply("Usage: `!blacklist <add|remove|list|scan>`", mention_author=False)

@blacklist_group.command(name='add')
@commands.has_permissions(administrator=True)
async def blacklist_add(ctx, brand_name: str, *, reason: str = "No reason provided"):
    """Add a brand to the blacklist: !blacklist add \"Brand Name\" \"Reason\""""
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

@blacklist_group.command(name='remove')
@commands.has_permissions(administrator=True)
async def blacklist_remove(ctx, *, brand_name: str):
    """Remove a brand from the blacklist: !blacklist remove Brand Name"""
    from app import BlacklistedBrand
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

@blacklist_group.command(name='scan')
@commands.has_permissions(administrator=True)
async def blacklist_scan(ctx, limit: int = 500):
    """Scan historical messages in the blacklist channel to auto-populate"""
    if not BLACKLIST_CHANNEL_ID:
        await ctx.reply("❌ BLACKLIST_CHANNEL_ID not configured.", mention_author=False)
        return
    
    channel = bot.get_channel(BLACKLIST_CHANNEL_ID)
    if not channel:
        # Try fetching if not in cache
        try:
            channel = await bot.fetch_channel(BLACKLIST_CHANNEL_ID)
        except:
            await ctx.reply(f"❌ Could not access channel {BLACKLIST_CHANNEL_ID}.", mention_author=False)
            return
    
    status_msg = await ctx.reply(f"🔍 Scanning last {limit} messages in {channel.name}... (This may take a minute)", mention_author=False)
    
    from app import BlacklistedBrand
    found_brands = []
    processed_count = 0
    
    # 1. Pre-fetch existing brands to avoid redundant queries
    with app.app_context():
        existing_brands = {b.seller_name.lower() for b in BlacklistedBrand.query.all()}
    
    try:
        async for message in channel.history(limit=limit):
            processed_count += 1
            if message.author.bot: continue
            
            # Update status for large scans
            if processed_count % 50 == 0:
                await status_msg.edit(content=f"🔍 Scanning... ({processed_count}/{limit} messages checked, found {len(found_brands)} brands)")
            
            # Heuristic extraction logic
            content = message.content.strip() if message.content else ""
            
            # Check for text in embeds (sometimes images have captions in embeds)
            if not content and message.embeds:
                embed = message.embeds[0]
                content = (embed.title or "") + "\n" + (embed.description or "")
                content = content.strip()

            if not content: continue
            
            # Extraction logic
            brand = None
            # A. Check for bolded text **Brand Name**
            bold_match = re.search(r'\*\*(.*?)\*\*', content)
            if bold_match:
                brand = bold_match.group(1).strip()
            else:
                # B. First line (usually the brand name in reports)
                brand = content.split('\n')[0].strip()
                # Clean up punctuation
                brand = re.sub(r'[:\-!]$', '', brand).strip()
            
            # Filter out obviously non-brand text
            if brand and 2 < len(brand) < 60:
                # Basic blacklist of common words to ignore as "brands"
                ignore_list = ['reasons', 'scam', 'proof', 'attached', 'screenshot', 'info', 'update']
                if any(word == brand.lower() for word in ignore_list):
                    continue

                if brand.lower() not in existing_brands and brand not in found_brands:
                    found_brands.append(brand)
        
        # 2. Batch save found brands
        if found_brands:
            with app.app_context():
                for b_name in found_brands:
                    new_entry = BlacklistedBrand(
                        seller_name=b_name, 
                        reason=f"Auto-imported from historical scan of #{channel.name}"
                    )
                    db.session.add(new_entry)
                db.session.commit()
        
        await status_msg.edit(content=f"✅ Scan complete! Checked {processed_count} messages.\nAdded **{len(found_brands)}** new brands to the blacklist.\n{('New additions: ' + ', '.join(found_brands[:15])) if found_brands else 'No new brands found.'}{'...' if len(found_brands) > 15 else ''}")

    except Exception as e:
        print(f"Error during blacklist scan: {e}")
        await status_msg.edit(content=f"❌ Error during scan: {str(e)}")

def get_hot_products():
    """Get Top Products - Sorted by Ad Spend (high first), then Video Count"""
    from datetime import timedelta
    
    with app.app_context():
        # Calculate cutoff date for repeat prevention
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=DAYS_BEFORE_REPEAT)
        
        # Query: Products with Shop Ads Commission >= 10%, high 7D sales, high ad spend, low competition (<40 videos)
        # Sort by: Shop Ads Commission (highest first), then Ad Spend, then 7D Sales
        products = Product.query.filter(
            Product.video_count >= 5,  # Filter out placeholders
            Product.video_count < 40,  # Low competition filter (<40 videos)
            Product.sales_7d >= 100,  # High 7D sales
            Product.ad_spend >= 500,  # High ad spend ($500+)
            Product.commission_rate > 0,  # Must have regular commission
            Product.shop_ads_commission >= 0.10,  # 10%+ shop ads commission (GMV Max)
            db.or_(
                Product.last_shown_hot == None,
                Product.last_shown_hot < cutoff_date
            )
        ).order_by(
            db.func.coalesce(Product.ad_spend, 0).desc(),  # Priority 1: High Ad Spend
            db.func.coalesce(Product.sales_7d, 0).desc(),  # Priority 2: High 7D Sales
            db.func.coalesce(Product.video_count, 0).asc(),  # Priority 3: Lower videos = better opportunity
            db.func.coalesce(Product.shop_ads_commission, 0).desc()  # Priority 4: Shop Ads Commission
        ).limit(MAX_DAILY_POSTS).all()
        
        # Convert to dicts BEFORE commit to avoid DetachedInstanceError
        product_dicts = []
        for p in products:
            p_dict = {
                'product_id': p.product_id,
                'product_name': p.product_name,
                'seller_name': p.seller_name,
                'sales': p.sales,
                'sales_7d': p.sales_7d,
                'sales_30d': p.sales_30d,
                'influencer_count': p.influencer_count,
                'video_count': p.video_count,
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

if __name__ == '__main__':
    if not DISCORD_BOT_TOKEN:
        print("❌ DISCORD_BOT_TOKEN not set!")
        exit(1)
    
    print("🚀 Starting Brand Hunter Discord Bot...")
    bot.run(DISCORD_BOT_TOKEN)
