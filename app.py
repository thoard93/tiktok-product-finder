from flask import Flask, render_template, request, redirect, url_for
from models import db, Product, ProductScan
from datetime import datetime, timedelta
import os

app = Flask(__name__)

# Database configuration
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}

db.init_app(app)

@app.route('/')
def dashboard():
    """Main dashboard showing latest scan results"""
    try:
        # Get latest scan from the last 7 days
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        latest_scan = ProductScan.query.filter(
            ProductScan.scan_date >= seven_days_ago
        ).order_by(ProductScan.scan_date.desc()).first()
        
        if not latest_scan:
            return render_template('dashboard.html', 
                                 scan=None, 
                                 products=[], 
                                 stats={},
                                 filters={})
        
        # Get filter parameters
        min_sales = request.args.get('min_sales', type=int, default=0)
        max_price = request.args.get('max_price', type=float)
        min_earnings = request.args.get('min_earnings', type=float, default=0)
        brand_search = request.args.get('brand_search', '').strip()  # NEW: Brand search
        sort_by = request.args.get('sort_by', default='potential_earnings')
        
        # Build query
        query = Product.query.filter_by(scan_id=latest_scan.id)
        
        # Apply filters
        if min_sales > 0:
            query = query.filter(Product.sales >= min_sales)
        
        if max_price:
            query = query.filter(Product.avg_price <= max_price)
        
        if min_earnings > 0:
            query = query.filter(Product.potential_earnings >= min_earnings)
        
        # NEW: Brand/seller search filter
        if brand_search:
            query = query.filter(Product.seller_name.ilike(f'%{brand_search}%'))
        
        # Apply sorting
        if sort_by == 'sales':
            query = query.order_by(Product.sales.desc())
        elif sort_by == 'gmv':
            query = query.order_by(Product.gmv.desc())
        elif sort_by == 'commission':
            query = query.order_by(Product.commission_rate.desc())
        elif sort_by == 'competition_low':  # NEW: Sort by competition LOW to HIGH
            query = query.order_by(Product.total_influencers.asc())
        elif sort_by == 'competition_high':  # Sort by competition HIGH to LOW
            query = query.order_by(Product.total_influencers.desc())
        else:  # default to potential_earnings
            query = query.order_by(Product.potential_earnings.desc())
        
        # Get paginated results
        page = request.args.get('page', 1, type=int)
        per_page = 20
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        products = pagination.items
        
        # Calculate statistics
        all_products = Product.query.filter_by(scan_id=latest_scan.id).all()
        
        stats = {
            'total_products': len(all_products),
            'total_gmv': sum(p.gmv or 0 for p in all_products),
            'total_earnings': sum(p.potential_earnings or 0 for p in all_products),
            'avg_commission': sum(p.commission_rate or 0 for p in all_products) / len(all_products) if all_products else 0,
            'top_product': max(all_products, key=lambda p: p.potential_earnings or 0) if all_products else None
        }
        
        # Current filters for template
        filters = {
            'min_sales': min_sales,
            'max_price': max_price,
            'min_earnings': min_earnings,
            'brand_search': brand_search,  # NEW
            'sort_by': sort_by
        }
        
        return render_template('dashboard.html',
                             scan=latest_scan,
                             products=products,
                             stats=stats,
                             filters=filters,
                             pagination=pagination)
    
    except Exception as e:
        print(f"Error in dashboard: {e}")
        return f"An error occurred: {str(e)}", 500

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    """Detailed view of a single product"""
    try:
        product = Product.query.get_or_404(product_id)
        return render_template('product_detail.html', product=product)
    except Exception as e:
        print(f"Error loading product: {e}")
        return f"Product not found: {str(e)}", 404

@app.route('/scans')
def scan_history():
    """View all historical scans"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 10
        
        pagination = ProductScan.query.order_by(
            ProductScan.scan_date.desc()
        ).paginate(page=page, per_page=per_page, error_out=False)
        
        scans = pagination.items
        
        return render_template('scan_history.html',
                             scans=scans,
                             pagination=pagination)
    except Exception as e:
        print(f"Error loading scan history: {e}")
        return f"An error occurred: {str(e)}", 500

@app.route('/health')
def health():
    """Health check endpoint for monitoring"""
    try:
        # Test database connection
        ProductScan.query.first()
        return {'status': 'healthy', 'database': 'connected'}, 200
    except Exception as e:
        return {'status': 'unhealthy', 'error': str(e)}, 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
