import requests
from datetime import datetime, timedelta
import json
import os
import sys
from app import app, db, ProductScan, Product

# EchoTik API Configuration
API_BASE_URL = "https://open.echotik.live/api/v2"
API_USERNAME = os.environ.get('ECHOTIK_USERNAME')
API_PASSWORD = os.environ.get('ECHOTIK_PASSWORD')

# Scanner Settings (from environment or defaults)
REGION = os.environ.get('SCAN_REGION', 'US')
PAGES_TO_SCAN = int(os.environ.get('PAGES_TO_SCAN', '20'))
MIN_SALES = int(os.environ.get('MIN_SALES', '10'))
MAX_INFLUENCERS = int(os.environ.get('MAX_INFLUENCERS', '500'))
MIN_GMV = int(os.environ.get('MIN_GMV', '100'))
MIN_COMMISSION = float(os.environ.get('MIN_COMMISSION', '0.10'))

# Scoring weights
SALES_WEIGHT = 0.3
GMV_WEIGHT = 0.2
COMPETITION_WEIGHT = 0.4
COMMISSION_WEIGHT = 0.1

def get_product_rankings(date_str, rank_type=1, sort_by=1, page_num=1):
    """Get product rankings from a specific page"""
    url = f"{API_BASE_URL}/product/ranklist"
    
    params = {
        'date': date_str,
        'region': REGION,
        'rank_type': rank_type,
        'product_rank_field': sort_by,
        'page_num': page_num,
        'page_size': 10
    }
    
    try:
        response = requests.get(url, params=params, auth=(API_USERNAME, API_PASSWORD))
        
        if response.status_code == 200:
            data = response.json()
            if 'code' in data and data['code'] != 0:
                return None
            return data
        else:
            return None
    except Exception as e:
        print(f"Error fetching page {page_num}: {e}")
        return None

def get_product_details(product_ids):
    """Get detailed product information"""
    url = f"{API_BASE_URL}/product/detail"
    params_str = ','.join(product_ids)
    
    try:
        full_url = f"{url}?product_ids={params_str}"
        response = requests.get(full_url, auth=(API_USERNAME, API_PASSWORD))
        
        if response.status_code == 200:
            data = response.json()
            if 'code' in data and data['code'] != 0:
                return None
            return data
        else:
            return None
    except Exception as e:
        print(f"Error fetching details: {e}")
        return None

def calculate_opportunity_score(product, detail):
    """Calculate opportunity score (0-100)"""
    sales = product.get('total_sale_cnt', 0)
    gmv = product.get('total_sale_gmv_amt', 0)
    total_influencers = detail.get('total_ifl_cnt', 1)
    commission = detail.get('product_commission_rate', 0)
    
    sales_score = min(sales / 10, 100)
    gmv_score = min(gmv / 1000, 100)
    competition_score = max(0, 100 - (total_influencers / MAX_INFLUENCERS * 100))
    commission_score = commission * 100
    
    final_score = (
        sales_score * SALES_WEIGHT +
        gmv_score * GMV_WEIGHT +
        competition_score * COMPETITION_WEIGHT +
        commission_score * COMMISSION_WEIGHT
    )
    
    return round(final_score, 2)

def get_cover_url(detail):
    """Extract first cover image URL"""
    cover_url_list = detail.get('cover_url', [])
    if cover_url_list and isinstance(cover_url_list, list) and len(cover_url_list) > 0:
        return cover_url_list[0].get('url', '')
    return ''

def get_brand_name(detail):
    """Extract brand/shop name from seller_id or product name"""
    # Try to get shop name - this might need seller detail API call
    # For now, extract from product name or return placeholder
    product_name = detail.get('product_name', '')
    
    # Simple heuristic: first word is often the brand
    words = product_name.split()
    if words:
        return words[0]
    return 'Unknown'

def run_daily_scan():
    """Main scanning function"""
    print(f"\n{'='*80}")
    print(f"Starting daily scan at {datetime.utcnow()}")
    print(f"Region: {REGION}, Pages: {PAGES_TO_SCAN}")
    print(f"{'='*80}\n")
    
    if not API_USERNAME or not API_PASSWORD:
        print("ERROR: ECHOTIK_USERNAME and ECHOTIK_PASSWORD environment variables must be set!")
        return
    
    yesterday = (datetime.utcnow() - timedelta(days=3)).strftime('%Y-%m-%d')
    
    # Create scan record
    with app.app_context():
        scan = ProductScan(
            scan_date=datetime.utcnow().date(),
            region=REGION,
            total_products_scanned=0,
            total_qualified=0
        )
        db.session.add(scan)
        db.session.commit()
        scan_id = scan.id
    
    # Step 1: Collect products from multiple pages
    all_products = []
    product_ids_to_detail = []
    
    print("Scanning pages...")
    for page in range(1, PAGES_TO_SCAN + 1):
        print(f"  Page {page}/{PAGES_TO_SCAN}...", end=" ")
        
        data = get_product_rankings(target_date, rank_type=1, sort_by=1, page_num=page)
        
        if not data or 'data' not in data or not data['data']:
            print("❌ No more data")
            break
        
        page_products = data['data']
        print(f"✅ Got {len(page_products)} products")
        
        for product in page_products:
            sales = product.get('total_sale_cnt', 0)
            gmv = product.get('total_sale_gmv_amt', 0)
            
            if sales >= MIN_SALES and gmv >= MIN_GMV:
                all_products.append(product)
                product_ids_to_detail.append(product.get('product_id'))
    
    total_scanned = len(all_products)
    print(f"\nCollected {total_scanned} products that meet basic criteria")
    
    if not product_ids_to_detail:
        print("No products found!")
        with app.app_context():
            scan = ProductScan.query.get(scan_id)
            scan.total_products_scanned = 0
            scan.total_qualified = 0
            db.session.commit()
        return
    
    # Step 2: Get detailed info in batches
    print("\nFetching detailed product info...")
    all_details = []
    batch_size = 10
    
    for i in range(0, len(product_ids_to_detail), batch_size):
        batch = product_ids_to_detail[i:i+batch_size]
        print(f"  Batch {i//batch_size + 1}: {len(batch)} products...", end=" ")
        
        details = get_product_details(batch)
        if details and 'data' in details:
            all_details.extend(details['data'])
            print("✅")
        else:
            print("❌")
    
    print(f"Got details for {len(all_details)} products")
    
    # Create product map
    details_map = {d.get('product_id'): d for d in all_details}
    products_map = {p.get('product_id'): p for p in all_products}
    
    # Step 3: Score and filter products
    opportunities = []
    
    print("\nScoring and filtering products...")
    for product_id, detail in details_map.items():
        product = products_map.get(product_id)
        if not product:
            continue
        
        total_influencers = detail.get('total_ifl_cnt', 0)
        commission = detail.get('product_commission_rate', 0)
        off_mark = detail.get('off_mark', 0)
        
        # Filter by influencers
        if total_influencers > MAX_INFLUENCERS:
            continue
        
        # Filter by availability
        if off_mark == 1:
            continue
        
        # Filter by commission
        if commission < MIN_COMMISSION:
            continue
        
        # Calculate score
        score = calculate_opportunity_score(product, detail)
        
        sales = product.get('total_sale_cnt', 0)
        gmv = product.get('total_sale_gmv_amt', 0)
        potential_earnings = gmv * commission
        
        opportunities.append({
            'score': score,
            'product_id': product_id,
            'product_name': detail.get('product_name'),
            'brand_name': get_brand_name(detail),
            'cover_url': get_cover_url(detail),
            'sales': sales,
            'gmv': gmv,
            'total_influencers': total_influencers,
            'commission_rate': commission,
            'avg_price': detail.get('spu_avg_price', 0),
            'potential_earnings': potential_earnings,
            'competition_level': 'Low' if total_influencers < 100 else 'Medium' if total_influencers < 300 else 'High'
        })
    
    # Sort by score
    opportunities.sort(key=lambda x: x['score'], reverse=True)
    
    print(f"Found {len(opportunities)} qualified products")
    
    # Step 4: Save top 5 to database
    with app.app_context():
        scan = ProductScan.query.get(scan_id)
        scan.total_products_scanned = total_scanned
        scan.total_qualified = len(opportunities)
        
        for rank, opp in enumerate(opportunities[:5], 1):
            product = Product(
                scan_id=scan_id,
                product_id=opp['product_id'],
                product_name=opp['product_name'],
                brand_name=opp['brand_name'],
                cover_url=opp['cover_url'],
                sales=opp['sales'],
                gmv=opp['gmv'],
                total_influencers=opp['total_influencers'],
                commission_rate=opp['commission_rate'],
                avg_price=opp['avg_price'],
                potential_earnings=opp['potential_earnings'],
                opportunity_score=opp['score'],
                competition_level=opp['competition_level'],
                rank=rank
            )
            db.session.add(product)
        
        db.session.commit()
        print(f"\n✅ Saved top 5 products to database")
    
    print(f"\n{'='*80}")
    print(f"Scan completed at {datetime.utcnow()}")
    print(f"{'='*80}\n")

if __name__ == '__main__':
    run_daily_scan()
