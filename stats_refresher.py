#!/usr/bin/env python3
"""
Stats Refresher Cron Job
Runs at 2:00 AM UTC daily to update ALL existing product stats

This script:
1. Gets all active products from database
2. Fetches latest stats from EchoTik API (in batches)
3. Updates sales, influencer counts, etc.
4. Calculates sales velocity for trending detection
5. Detects out-of-stock products
6. Sends Telegram alerts for hidden gems and back-in-stock
"""

import os
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
import time

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

# EchoTik API Config
BASE_URL = "https://open.echotik.live/api/v3/echotik"
ECHOTIK_USERNAME = os.environ.get('ECHOTIK_USERNAME', '')
ECHOTIK_PASSWORD = os.environ.get('ECHOTIK_PASSWORD', '')

# Telegram Config (optional)
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# Settings
BATCH_SIZE = 20  # Products per API call
BATCH_DELAY = 1  # Seconds between batches

def get_auth():
    return HTTPBasicAuth(ECHOTIK_USERNAME, ECHOTIK_PASSWORD)

# Product model (must match app.py)
class Product(db.Model):
    __tablename__ = 'products'
    product_id = db.Column(db.String(50), primary_key=True)
    product_name = db.Column(db.String(500))
    seller_id = db.Column(db.String(50))
    seller_name = db.Column(db.String(200))
    category = db.Column(db.String(100))
    price = db.Column(db.Float)
    commission_rate = db.Column(db.Float)
    sales = db.Column(db.Integer)
    sales_7d = db.Column(db.Integer)
    sales_30d = db.Column(db.Integer)
    prev_sales_7d = db.Column(db.Integer)
    prev_sales_30d = db.Column(db.Integer)
    sales_velocity = db.Column(db.Float)
    gmv = db.Column(db.Float)
    gmv_30d = db.Column(db.Float)
    influencer_count = db.Column(db.Integer)
    video_count = db.Column(db.Integer)
    video_7d = db.Column(db.Integer)
    video_30d = db.Column(db.Integer)
    live_count = db.Column(db.Integer)
    views_count = db.Column(db.Integer)
    product_rating = db.Column(db.Float)
    review_count = db.Column(db.Integer)
    image_url = db.Column(db.Text)
    cached_image_url = db.Column(db.Text)
    product_status = db.Column(db.String(20), default='active')
    status_changed_at = db.Column(db.DateTime)
    status_note = db.Column(db.Text)
    is_hidden_gem = db.Column(db.Boolean, default=False)
    gem_alert_sent = db.Column(db.Boolean, default=False)
    stock_alert_sent = db.Column(db.Boolean, default=False)
    last_alert_sent = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

def send_telegram_alert(message):
    """Send alert to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, timeout=10)
        return response.status_code == 200
    except:
        return False

def send_hidden_gem_alert(product):
    """Send Telegram alert for hidden gem"""
    message = f"""💎 <b>HIDDEN GEM FOUND!</b>

<b>{product.product_name[:100]}</b>

🔥 7-day sales: <b>{product.sales_7d:,}</b>
👥 Only <b>{product.influencer_count}</b> influencers!
💵 Commission: {product.commission_rate:.1f}%
🏷️ Price: ${product.price:.2f}
🏪 Brand: {product.seller_name}

🔗 <a href="https://shop.tiktok.com/view/product/{product.product_id}">View on TikTok Shop</a>
"""
    return send_telegram_alert(message)

def send_back_in_stock_alert(product):
    """Send Telegram alert for back in stock"""
    message = f"""🔙 <b>BACK IN STOCK!</b>

<b>{product.product_name[:100]}</b>

📈 Now selling: <b>{product.sales_7d:,}</b>/week
👥 Influencers: {product.influencer_count}
💵 Commission: {product.commission_rate:.1f}%

🔗 <a href="https://shop.tiktok.com/view/product/{product.product_id}">View on TikTok Shop</a>
"""
    return send_telegram_alert(message)

def get_product_details(product_ids):
    """Get product details from EchoTik API"""
    try:
        response = requests.get(
            f"{BASE_URL}/product/detail",
            params={'product_ids': ','.join(product_ids)},
            auth=get_auth(),
            timeout=60
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 0:
                return {str(p.get('product_id')): p for p in data.get('data', [])}
        return {}
    except Exception as e:
        print(f"Error fetching product details: {e}")
        return {}

def calculate_velocity(current, previous):
    """Calculate sales velocity percentage"""
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return ((current - previous) / previous) * 100

def run_stats_refresh():
    """Main stats refresh function"""
    print(f"🔄 Starting stats refresh at {datetime.utcnow().isoformat()}")
    
    stats = {
        'total_products': 0,
        'updated': 0,
        'failed': 0,
        'trending_up': 0,
        'trending_down': 0,
        'new_gems': 0,
        'new_oos': 0,
        'back_in_stock': 0,
        'alerts_sent': 0
    }
    
    with app.app_context():
        # Get all active products
        products = Product.query.filter(
            db.or_(Product.product_status == 'active', Product.product_status == None)
        ).all()
        
        stats['total_products'] = len(products)
        print(f"   Found {stats['total_products']} products to refresh")
        
        # Process in batches
        for i in range(0, len(products), BATCH_SIZE):
            batch = products[i:i + BATCH_SIZE]
            product_ids = [p.product_id for p in batch]
            
            print(f"   Processing batch {i//BATCH_SIZE + 1}/{(len(products) + BATCH_SIZE - 1)//BATCH_SIZE}")
            
            # Fetch latest data from API
            api_data = get_product_details(product_ids)
            
            for product in batch:
                p = api_data.get(str(product.product_id))
                
                if not p:
                    stats['failed'] += 1
                    continue
                
                # Store previous values for velocity calculation
                old_sales_7d = product.sales_7d or 0
                old_status = product.product_status
                
                # Update all stats
                new_sales_7d = int(p.get('total_sale_7d_cnt', 0) or 0)
                new_sales_30d = int(p.get('total_sale_30d_cnt', 0) or 0)
                
                product.prev_sales_7d = old_sales_7d
                product.sales = int(p.get('total_sale_cnt', 0) or 0)
                product.sales_7d = new_sales_7d
                product.sales_30d = new_sales_30d
                product.gmv = float(p.get('total_sale_gmv_amt', 0) or 0)
                product.gmv_30d = float(p.get('total_sale_gmv_30d_amt', 0) or 0)
                product.influencer_count = int(p.get('total_ifl_cnt', 0) or 0)
                product.commission_rate = float(p.get('product_commission_rate', 0) or 0)
                product.price = float(p.get('spu_avg_price', 0) or 0)
                product.video_count = int(p.get('total_video_cnt', 0) or 0)
                product.video_7d = int(p.get('total_video_7d_cnt', 0) or 0)
                product.video_30d = int(p.get('total_video_30d_cnt', 0) or 0)
                product.live_count = int(p.get('total_live_cnt', 0) or 0)
                product.views_count = int(p.get('total_views_cnt', 0) or 0)
                product.product_rating = float(p.get('product_rating', 0) or 0)
                product.review_count = int(p.get('review_count', 0) or 0)
                product.last_updated = datetime.utcnow()
                
                # Calculate sales velocity
                if old_sales_7d > 0:
                    velocity = calculate_velocity(new_sales_7d, old_sales_7d)
                    product.sales_velocity = velocity
                    
                    if velocity >= 20:
                        stats['trending_up'] += 1
                    elif velocity <= -20:
                        stats['trending_down'] += 1
                
                # Out-of-stock detection
                if new_sales_7d == 0 and (old_sales_7d > 20 or new_sales_30d > 50):
                    if product.product_status in ['active', None]:
                        product.product_status = 'likely_oos'
                        product.status_changed_at = datetime.utcnow()
                        product.status_note = f'Auto-detected: was selling {old_sales_7d}/7d, now 0'
                        stats['new_oos'] += 1
                
                # Back in stock detection
                elif new_sales_7d > 0 and old_status == 'likely_oos':
                    product.product_status = 'active'
                    product.status_changed_at = datetime.utcnow()
                    product.status_note = f'Back in stock: now selling {new_sales_7d}/7d'
                    stats['back_in_stock'] += 1
                    
                    # Send alert
                    if not product.stock_alert_sent:
                        if send_back_in_stock_alert(product):
                            product.stock_alert_sent = True
                            product.last_alert_sent = datetime.utcnow()
                            stats['alerts_sent'] += 1
                
                # Hidden gem detection
                is_gem = (
                    new_sales_7d >= 50 and
                    product.influencer_count <= 50 and
                    product.commission_rate >= 10
                )
                
                was_gem = product.is_hidden_gem
                product.is_hidden_gem = is_gem
                
                if is_gem and not was_gem and not product.gem_alert_sent:
                    stats['new_gems'] += 1
                    if send_hidden_gem_alert(product):
                        product.gem_alert_sent = True
                        product.last_alert_sent = datetime.utcnow()
                        stats['alerts_sent'] += 1
                
                stats['updated'] += 1
            
            db.session.commit()
            time.sleep(BATCH_DELAY)
        
        db.session.commit()
    
    print(f"✅ Stats refresh complete!")
    print(f"   Total products: {stats['total_products']}")
    print(f"   Updated: {stats['updated']}")
    print(f"   Failed: {stats['failed']}")
    print(f"   Trending up (≥20%): {stats['trending_up']}")
    print(f"   Trending down (≤-20%): {stats['trending_down']}")
    print(f"   New hidden gems: {stats['new_gems']}")
    print(f"   New OOS detected: {stats['new_oos']}")
    print(f"   Back in stock: {stats['back_in_stock']}")
    print(f"   Alerts sent: {stats['alerts_sent']}")
    
    return stats

if __name__ == '__main__':
    if not ECHOTIK_USERNAME or not ECHOTIK_PASSWORD:
        print("❌ Error: ECHOTIK_USERNAME and ECHOTIK_PASSWORD must be set")
        exit(1)
    
    run_stats_refresh()
