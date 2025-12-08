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

# Database setup
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}
db = SQLAlchemy(app)

# Discord Config
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
HOT_PRODUCTS_CHANNEL_ID = int(os.environ.get('HOT_PRODUCTS_CHANNEL_ID', 0))
PRODUCT_LOOKUP_CHANNEL_ID = int(os.environ.get('PRODUCT_LOOKUP_CHANNEL_ID', 0))

# EchoTik API Config
BASE_URL = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')

# Hot Product Criteria - Free Shipping Deals
MIN_SALES_7D = 50  # Lower threshold since we're filtering by free shipping
MAX_VIDEO_COUNT = 30  # Low competition
MAX_DAILY_POSTS = 5  # Top 5 daily
DAYS_BEFORE_REPEAT = 3  # Don't show same product for 3 days

def get_auth():
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)

# Product model (must match app.py)
class Product(db.Model):
    __tablename__ = 'products'
    product_id = db.Column(db.String(50), primary_key=True)
    product_name = db.Column(db.String(500))
    seller_id = db.Column(db.String(50))
    seller_name = db.Column(db.String(255))
    gmv = db.Column(db.Float, default=0)
    gmv_30d = db.Column(db.Float, default=0)
    sales = db.Column(db.Integer, default=0)
    sales_7d = db.Column(db.Integer, default=0)
    sales_30d = db.Column(db.Integer, default=0)
    influencer_count = db.Column(db.Integer, default=0)
    commission_rate = db.Column(db.Float, default=0)
    price = db.Column(db.Float, default=0)
    image_url = db.Column(db.Text)
    cached_image_url = db.Column(db.Text)
    video_count = db.Column(db.Integer, default=0)
    video_7d = db.Column(db.Integer, default=0)
    video_30d = db.Column(db.Integer, default=0)
    live_count = db.Column(db.Integer, default=0)
    views_count = db.Column(db.Integer, default=0)
    product_rating = db.Column(db.Float, default=0)
    product_status = db.Column(db.String(50), default='active')
    has_free_shipping = db.Column(db.Boolean, default=False)
    last_shown_hot = db.Column(db.DateTime)  # Track when product was last shown in hot products
    last_updated = db.Column(db.DateTime)

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

def get_product_from_api(product_id):
    """Fetch product details from EchoTik API"""
    try:
        response = requests.get(
            f"{BASE_URL}/product/detail",
            params={'product_ids': product_id},
            auth=get_auth(),
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 0 and data.get('data'):
                products = data.get('data', [])
                if isinstance(products, list) and len(products) > 0:
                    return products[0]
        return None
    except Exception as e:
        print(f"Error fetching product {product_id}: {e}")
        return None

def get_product_data(product_id):
    """Get product - check database first, then API if not found"""
    # Try database first (no API call!)
    db_product = get_product_from_db(product_id)
    if db_product:
        print(f"✅ Product {product_id} found in database (saved API call)")
        return db_product
    
    # Not in database, call API
    print(f"🔍 Product {product_id} not in database, calling API...")
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
    
    # Get stats
    sales_7d = int(get_val('sales_7d', 0) or 0)
    sales_30d = int(get_val('sales_30d', 0) or 0)
    influencer_count = int(get_val('influencer_count', 0) or 0)
    video_count = int(get_val('video_count', 0) or 0)
    commission = float(get_val('commission_rate', 0) or 0)
    price = float(get_val('price', 0) or 0)
    has_free_shipping = get_val('has_free_shipping', False)
    
    # Format commission (handle 0.15 vs 15.0)
    if commission > 0 and commission < 1:
        commission = commission * 100
    
    # Get image URL
    image_url = get_val('cached_image_url') or get_val('image_url') or get_val('cover_url', '')
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
    embed.add_field(name="📦 7-Day Sales", value=f"{sales_7d:,}", inline=True)
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
    """Get hot FREE SHIPPING products from database with variety (no repeats for 3 days)"""
    from datetime import timedelta
    
    with app.app_context():
        # Calculate cutoff date for repeat prevention
        cutoff_date = datetime.utcnow() - timedelta(days=DAYS_BEFORE_REPEAT)
        
        # Query free shipping products with low video count
        # Exclude products shown in the last 3 days
        products = Product.query.filter(
            Product.has_free_shipping == True,  # Free shipping only!
            Product.sales_7d >= MIN_SALES_7D,
            Product.video_count <= MAX_VIDEO_COUNT,
            db.or_(Product.product_status == 'active', Product.product_status == None),
            db.or_(
                Product.last_shown_hot == None,  # Never shown
                Product.last_shown_hot < cutoff_date  # Or shown more than 3 days ago
            )
        ).order_by(
            Product.sales_7d.desc()
        ).limit(MAX_DAILY_POSTS).all()
        
        # If we don't have enough fresh products, fall back to showing any free shipping product
        if len(products) < MAX_DAILY_POSTS:
            needed = MAX_DAILY_POSTS - len(products)
            existing_ids = [p.product_id for p in products]
            
            fallback_products = Product.query.filter(
                Product.has_free_shipping == True,
                Product.sales_7d >= MIN_SALES_7D,
                Product.video_count <= MAX_VIDEO_COUNT, # Keep low video count constraint if possible
                db.or_(Product.product_status == 'active', Product.product_status == None),
                ~Product.product_id.in_(existing_ids)
            ).order_by(
                Product.sales_7d.desc()
            ).limit(needed).all()
            products.extend(fallback_products)
        
        # Mark products as shown today
        for p in products:
            p.last_shown_hot = datetime.utcnow()
        
        try:
            db.session.commit()
        except Exception as e:
            print(f"Error updating last_shown_hot: {e}")
            db.session.rollback()
        
        # Convert to dicts for consistency
        return products  # Return objects, helper handles them

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
        embed = create_product_embed(p, title_prefix=f"#{i} ")
        await channel.send(embed=embed)
        await asyncio.sleep(1)  # Rate limiting
    
    print(f"   Posted {len(products)} free shipping deals")

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
