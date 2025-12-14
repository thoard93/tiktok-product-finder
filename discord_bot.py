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
from datetime import datetime, time
import requests
from requests.auth import HTTPBasicAuth
import asyncio

# Database setup - Import from main application to ensure model consistency
from app import app, db, Product

# Discord Config
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
HOT_PRODUCTS_CHANNEL_ID = int(os.environ.get('HOT_PRODUCTS_CHANNEL_ID', 0))
PRODUCT_LOOKUP_CHANNEL_ID = int(os.environ.get('PRODUCT_LOOKUP_CHANNEL_ID', 0))

# Hot Product Criteria - Free Shipping Deals
MIN_SALES_7D = 50  # Lower threshold since we're filtering by free shipping
MAX_VIDEO_COUNT = 30  # Low competition
MAX_DAILY_POSTS = 5  # Top 5 daily
DAYS_BEFORE_REPEAT = 3  # Don't show same product for 3 days

def get_auth():
    # EchoTik is deprecated
    return None

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
        r'product_id=(\d+)',
        r'/(\d{15,25})(?:[/?]|$)',  # Direct product ID (15-25 digits)
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
    """Resolve TikTok share link to get the real URL"""
    try:
        response = requests.head(url, allow_redirects=True, timeout=10)
        return response.url
    except:
        try:
            response = requests.get(url, allow_redirects=True, timeout=10)
            return response.url
        except:
            return None

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
                'price': product.price,
                'image_url': product.cached_image_url or product.image_url,
                'from_database': True
            }
        return None

# Import the hybrid scan logic
from app import start_hybrid_scan

def get_product_from_api(product_id):
    """
    Trigger Apify Hybrid Scan and poll database for results.
    Replaces EchoTik API call.
    """
    try:
        print(f"🚀 Triggering Hybrid Scan for {product_id}...")
        
        # 1. Start the Scan
        with app.app_context():
            # This does Prefetch (Instant) + Launches Apify (Async)
            # Returns dict with 'success'
            res = start_hybrid_scan(product_id)
            if not res.get('success'):
                print(f"❌ Scan trigger failed: {res.get('error')}")
                return None
        
        # 2. Poll Database for completion (Wait for 'apify_shop' scan type)
        # Apify usually takes 20-40s for a single item search.
        print(f"⏳ Polling DB for completion (Max 45s)...")
        import time
        for i in range(9): # 9 * 5s = 45s
            time.sleep(5)
            with app.app_context():
                # We need to construct the specific shop_ID that app.py uses
                # app.py uses f"shop_{product_id}" for new items
                pid = f"shop_{product_id}"
                p = Product.query.get(pid)
                
                if p:
                    # Check if Apify has finished (scan_type changes)
                    # Or if we have stats (video_count > 0)
                    if p.scan_type == 'apify_shop':
                        print(f"✅ Scan Complete! Found stats for {pid}")
                        return p
                    
                    # If we have name but still prefetch, keep waiting...
                    print(f"   ... Waiting for Apify (Current: {p.scan_type}, Videos: {p.video_count})")
        
        # If timeout, return whatever partial data we have (Prefetch data)
        with app.app_context():
            p = Product.query.get(f"shop_{product_id}")
            if p:
                print("⚠️ Timeout waiting for stats. Returning basic data.")
                return p
                
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
        db_product = Product.query.get(product_id) or Product.query.get(f"shop_{product_id}")
        
        # If found AND has stats (apify_shop), return it immediately
        if db_product and db_product.scan_type == 'apify_shop':
            print(f"✅ Product {product_id} found in database (Cached)")
            return db_product
    
    # Not found OR needs upgrade -> Call Scan
    print(f"🔍 Product {product_id} needs scan/upgrade, calling Scanner...")
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
    sales_7d = int(get_val_multi(['sales_7d', 'total_sale_7d_cnt'], 0) or 0)
    sales_30d = int(get_val_multi(['sales_30d', 'total_sale_30d_cnt'], 0) or 0)
    influencer_count = int(get_val_multi(['influencer_count', 'total_ifl_cnt'], 0) or 0)
    video_count = int(get_val_multi(['video_count', 'total_video_cnt'], 0) or 0)
    commission = float(get_val_multi(['commission_rate', 'product_commission_rate'], 0) or 0)
    price = float(get_val_multi(['price', 'spu_avg_price'], 0) or 0)
    stock = int(get_val_multi(['live_count', 'stock'], 0) or 0) # live_count is proxy for stock
    has_free_shipping = get_val('has_free_shipping', False)
    
    # Format commission (handle 0.15 vs 15.0)
    if commission > 0 and commission < 1:
        commission = commission * 100
    
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
    
    embed = Embed(
        title=title,
        url=f"https://www.tiktok.com/shop/pdp/{product_id}",
        color=color
    )
    
    # Add stats fields
    embed.add_field(name="📦 Stock", value=f"{stock:,}", inline=True)
    embed.add_field(name="📉 7-Day Sales", value=f"{sales_7d:,}", inline=True)
    embed.add_field(name="💰 Price", value=f"${price:.2f}", inline=True)
    embed.add_field(name="💵 Commission", value=f"{commission:.1f}%", inline=True)
    embed.add_field(name="🎬 Total Videos", value=f"**{video_count:,}**", inline=True)
    embed.add_field(name="👥 Creators", value=f"{influencer_count:,}", inline=True)
    embed.add_field(name="🎯 Opportunity", value=f"**{opportunity}**", inline=False)
    
    # Add image if available
    if image_url and str(image_url).startswith('http'):
        embed.set_thumbnail(url=image_url)
    
    embed.set_footer(text=f"Product ID: {product_id}")
    embed.timestamp = datetime.utcnow()
    
    return embed

def get_hot_products():
    """Get Top Products from the New Scraper Tab (Apify Shop)"""
    from datetime import timedelta
    
    with app.app_context():
        # Calculate cutoff date for repeat prevention
        cutoff_date = datetime.utcnow() - timedelta(days=DAYS_BEFORE_REPEAT)
        
        # Query: 
        # 1. New Scraper Data (scan_type='apify_shop')
        # 2. Has Stock (live_count > 0)
        # 3. Has Videos (video_count > 0)
        # 4. Not shown recently
        products = Product.query.filter(
            Product.scan_type == 'apify_shop', # ONLY new scraper data
            Product.live_count > 0,            # Stock > 0
            Product.video_count > 0,           # Videos > 0
            db.or_(
                Product.last_shown_hot == None,  # Never shown
                Product.last_shown_hot < cutoff_date  # Or shown more than 3 days ago
            )
        ).order_by(
            Product.sales_7d.desc() # Top Sales first
        ).limit(MAX_DAILY_POSTS).all()
        
        # Mark products as shown today
        for p in products:
            p.last_shown_hot = datetime.utcnow()
        
        try:
            db.session.commit()
        except Exception as e:
            print(f"Error updating last_shown_hot: {e}")
            db.session.rollback()
            
        return products

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
    
    print(f"🎁 Posting daily free shipping deals at {datetime.utcnow().isoformat()}")
    
    products = get_hot_products()
    
    if not products:
        await channel.send("📭 No free shipping deals matching criteria today. Run a Deal Hunter scan!")
        return
    
    # Send header message
    await channel.send(f"# 🎁 Daily Free Shipping Deals - {datetime.utcnow().strftime('%B %d, %Y')}\n"
                       f"**Criteria:** Free shipping, 50+ weekly sales, <30 videos (low competition)\n"
                       f"**Today's Picks:** {len(products)} products\n"
                       f"─────────────────────────────")
    
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
            for url in urls:
                if 'vm.tiktok.com' in url or '/t/' in url:
                    resolved = resolve_tiktok_share_link(url)
                    if resolved:
                        resolved_url = resolved
                        break
            
            # Extract product ID
            product_id = extract_product_id(resolved_url)
            
            if not product_id:
                await message.add_reaction('❌')
                await message.reply("❌ Could not find a valid TikTok product ID in your message.", mention_author=False)
                return
            
            # Fetch product (database first, then API)
            product = get_product_data(product_id)
            
            if not product:
                await message.add_reaction('❌')
                await message.reply(f"❌ Could not find product `{product_id}` in the database.", mention_author=False)
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
    
    await ctx.message.add_reaction('🔍')
    
    # Try to resolve if it's a share link
    if 'vm.tiktok.com' in query or '/t/' in query:
        resolved = resolve_tiktok_share_link(query)
        if resolved:
            query = resolved
    
    product_id = extract_product_id(query)
    
    if not product_id:
        await ctx.message.add_reaction('❌')
        await ctx.reply("❌ Could not extract product ID from your input.", mention_author=False)
        return
    
    product = get_product_data(product_id)
    
    if not product:
        await ctx.message.add_reaction('❌')
        await ctx.reply(f"❌ Product `{product_id}` not found.", mention_author=False)
        return
    
    embed = create_product_embed(product)
    await ctx.reply(embed=embed, mention_author=False)
    await ctx.message.remove_reaction('🔍', bot.user)
    await ctx.message.add_reaction('✅')

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
