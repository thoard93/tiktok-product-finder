from flask import Flask, render_template, request
from models import db, ProductScan, Product
import os
from sqlalchemy import desc, asc

app = Flask(__name__)

# Database configuration
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

@app.route('/')
def index():
    try:
        # Get filter parameters
        tab = request.args.get('tab', 'all')  # 'all' or 'gems'
        brand_filter = request.args.get('brand', '').strip()
        sort_by = request.args.get('sort', '')  # 'low' or 'high' for competition
        
        # Get the most recent scan based on tab selection (that has products!)
        if tab == 'gems':
            # Brand Hunter gems only - find most recent with products
            latest_scan = ProductScan.query.filter(
                ProductScan.scan_type == 'brand_hunter',
                ProductScan.total_qualified > 0
            ).order_by(desc(ProductScan.scan_date)).first()
            
            # If no brand hunter scans yet, show message
            if not latest_scan:
                return render_template('dashboard.html', 
                                     error="No Brand Hunter gems yet! Run brand_hunter_smart.py to find hidden gems.",
                                     products=[],
                                     scan_date=None,
                                     tab=tab)
        else:
            # All products (general scans) - find most recent with products
            latest_scan = ProductScan.query.filter(
                ProductScan.scan_type == 'general',
                ProductScan.total_qualified > 0
            ).order_by(desc(ProductScan.scan_date)).first()
        
        if not latest_scan:
            return render_template('dashboard.html', 
                                 error="No scans found in database",
                                 products=[],
                                 scan_date=None,
                                 tab=tab)
        
        # Build query for products
        query = Product.query.filter_by(scan_id=latest_scan.id)
        
        # Apply brand filter if provided
        if brand_filter:
            query = query.filter(Product.seller_name.ilike(f'%{brand_filter}%'))
        
        # Apply competition sorting
        if sort_by == 'low':
            query = query.order_by(asc(Product.total_influencers))
        elif sort_by == 'high':
            query = query.order_by(desc(Product.total_influencers))
        else:
            # Default: sort by potential earnings (best opportunities)
            query = query.order_by(desc(Product.potential_earnings))
        
        products = query.all()
        
        return render_template('dashboard.html',
                             products=products,
                             scan_date=latest_scan.scan_date,
                             brand_filter=brand_filter,
                             sort_by=sort_by,
                             tab=tab)
    
    except Exception as e:
        return render_template('dashboard.html',
                             error=f"An error occurred: {str(e)}",
                             products=[],
                             scan_date=None,
                             tab='all')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

# Add to app.py
@app.route('/proxy-image')
def proxy_image():
    image_url = request.args.get('url')
    if not image_url:
        return '', 404
    
    try:
        response = requests.get(image_url, timeout=5)
        return Response(response.content, mimetype=response.headers['Content-Type'])
    except:
        return '', 404
