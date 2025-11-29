"""
Brand Hunter - Automated Daily Scanner
Runs as a Render Cron Job

Schedule: Daily at 1:00 AM UTC (after EchoTik updates at UTC 0)
         = 8:00 PM EST / 7:00 PM CST

What it does:
1. Deep scans top 10-20 brands
2. Scans pages 150-200 (deep into catalog for hidden gems)
3. Filters for products with ≤100 influencers
4. Sends Telegram notification with results
"""

import requests
import os
import time
from datetime import datetime

# =============================================================================
# CONFIGURATION
# =============================================================================

# Your Brand Hunter app URL
APP_URL = os.environ.get('APP_URL', 'https://tiktok-product-finder.onrender.com')

# Telegram notifications
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8265804607:AAEnAPrz_KfTKH2xWp4AK-qA4pvdkUjEmo8')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '7379075068')

# Scan parameters
START_BRAND_RANK = 1      # Start from top brand
NUM_BRANDS = 15           # Scan top 15 brands (middle ground between 10-20)
START_PAGE = 150          # Start from page 150
END_PAGE = 200            # End at page 200
MAX_INFLUENCERS = 100     # Only products with ≤100 influencers

# Dev passkey for authentication (set in Render environment)
DEV_PASSKEY = os.environ.get('DEV_PASSKEY', '')

# =============================================================================
# TELEGRAM NOTIFICATION
# =============================================================================

def send_telegram(message):
    """Send notification to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, skipping notification")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }, timeout=10)
        
        if response.status_code == 200:
            print("Telegram notification sent!")
            return True
        else:
            print(f"Telegram error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"Telegram exception: {e}")
        return False

# =============================================================================
# AUTHENTICATION
# =============================================================================

def get_session():
    """Create authenticated session"""
    session = requests.Session()
    
    if DEV_PASSKEY:
        # Login with dev passkey
        try:
            response = session.post(
                f"{APP_URL}/auth/passkey",
                json={'passkey': DEV_PASSKEY},
                timeout=30
            )
            if response.status_code == 200:
                print("✅ Authenticated with dev passkey")
            else:
                print(f"⚠️ Auth failed: {response.status_code}")
        except Exception as e:
            print(f"⚠️ Auth exception: {e}")
    
    return session

# =============================================================================
# SCANNING
# =============================================================================

def run_deep_scan(session):
    """
    Run deep scan across multiple brands
    Uses the scan-pages endpoint for each brand
    """
    results = {
        'brands_scanned': 0,
        'products_found': 0,
        'products_saved': 0,
        'errors': 0,
        'brand_details': []
    }
    
    print(f"\n🔍 Starting Deep Scan")
    print(f"   Brands: {START_BRAND_RANK} to {START_BRAND_RANK + NUM_BRANDS - 1}")
    print(f"   Pages: {START_PAGE} to {END_PAGE}")
    print(f"   Max Influencers: {MAX_INFLUENCERS}")
    print("-" * 50)
    
    # First, get the list of top brands
    try:
        brands_response = session.get(
            f"{APP_URL}/api/top-brands",
            params={'start_rank': START_BRAND_RANK, 'count': NUM_BRANDS},
            timeout=60
        )
        
        if brands_response.status_code != 200:
            print(f"❌ Failed to get brands: {brands_response.status_code}")
            results['errors'] += 1
            return results
        
        brands_data = brands_response.json()
        brands = brands_data.get('brands', [])
        
        if not brands:
            print("❌ No brands returned")
            results['errors'] += 1
            return results
        
        print(f"📊 Got {len(brands)} brands to scan\n")
        
    except Exception as e:
        print(f"❌ Exception getting brands: {e}")
        results['errors'] += 1
        return results
    
    # Scan each brand
    for i, brand in enumerate(brands):
        seller_id = brand.get('seller_id', '')
        seller_name = brand.get('seller_name', 'Unknown')
        
        print(f"[{i+1}/{len(brands)}] Scanning: {seller_name[:30]}...")
        
        brand_found = 0
        brand_saved = 0
        
        # Scan in chunks of 10 pages to avoid timeout
        for page_start in range(START_PAGE, END_PAGE + 1, 10):
            page_end = min(page_start + 9, END_PAGE)
            
            try:
                scan_url = f"{APP_URL}/api/scan-pages/{seller_id}"
                params = {
                    'start': page_start,
                    'end': page_end,
                    'max_influencers': MAX_INFLUENCERS,
                    'min_sales': 0,
                    'seller_name': seller_name
                }
                
                response = session.get(scan_url, params=params, timeout=120)
                
                if response.status_code == 200:
                    data = response.json()
                    found = data.get('products_found', 0)
                    saved = data.get('products_saved', 0)
                    brand_found += found
                    brand_saved += saved
                    
                    if found > 0:
                        print(f"   Pages {page_start}-{page_end}: Found {found}, Saved {saved}")
                elif response.status_code == 423:
                    print(f"   ⚠️ Scan locked - someone else is scanning")
                    results['errors'] += 1
                    break
                else:
                    print(f"   ⚠️ Pages {page_start}-{page_end}: Error {response.status_code}")
                    
                # Small delay between chunks
                time.sleep(0.5)
                
            except Exception as e:
                print(f"   ❌ Pages {page_start}-{page_end}: {str(e)[:50]}")
                results['errors'] += 1
        
        results['brands_scanned'] += 1
        results['products_found'] += brand_found
        results['products_saved'] += brand_saved
        results['brand_details'].append({
            'name': seller_name,
            'found': brand_found,
            'saved': brand_saved
        })
        
        print(f"   ✅ {seller_name[:20]}: {brand_found} found, {brand_saved} saved\n")
        
        # Delay between brands
        time.sleep(1)
    
    return results

# =============================================================================
# MAIN
# =============================================================================

def main():
    start_time = datetime.utcnow()
    print("=" * 60)
    print(f"🚀 Brand Hunter Daily Scanner")
    print(f"   Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 60)
    
    # Send start notification
    send_telegram(f"🔍 <b>Daily Scan Started</b>\n\n"
                  f"Scanning top {NUM_BRANDS} brands\n"
                  f"Pages {START_PAGE}-{END_PAGE}\n"
                  f"Max {MAX_INFLUENCERS} influencers")
    
    # Get authenticated session
    session = get_session()
    
    # Run the deep scan
    results = run_deep_scan(session)
    
    # Calculate duration
    end_time = datetime.utcnow()
    duration = (end_time - start_time).total_seconds() / 60
    
    # Print summary
    print("=" * 60)
    print("📊 SCAN COMPLETE")
    print(f"   Duration: {duration:.1f} minutes")
    print(f"   Brands Scanned: {results['brands_scanned']}")
    print(f"   Products Found: {results['products_found']}")
    print(f"   Products Saved: {results['products_saved']}")
    print(f"   Errors: {results['errors']}")
    print("=" * 60)
    
    # Build Telegram summary
    top_brands = sorted(results['brand_details'], key=lambda x: x['saved'], reverse=True)[:5]
    top_brands_text = "\n".join([
        f"  • {b['name'][:25]}: {b['saved']} new" 
        for b in top_brands if b['saved'] > 0
    ]) or "  No new products found"
    
    telegram_msg = (
        f"✅ <b>Daily Scan Complete</b>\n\n"
        f"⏱ Duration: {duration:.1f} min\n"
        f"🏪 Brands: {results['brands_scanned']}\n"
        f"📦 Found: {results['products_found']}\n"
        f"💾 Saved: {results['products_saved']}\n"
        f"⚠️ Errors: {results['errors']}\n\n"
        f"<b>Top Brands:</b>\n{top_brands_text}\n\n"
        f"🔗 <a href='{APP_URL}'>View Products</a>"
    )
    
    send_telegram(telegram_msg)
    
    print("\n✅ Daily scan finished!")

if __name__ == '__main__':
    main()
