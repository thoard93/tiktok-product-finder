from flask import Flask, render_template, request
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///tiktok_products.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize database
db = SQLAlchemy(app)

# Define models directly in app.py to ensure they match database
class ProductScan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    scan_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    scan_time = db.Column(db.DateTime)
    total_products_scanned = db.Column(db.Integer, default=0)
    total_qualified = db.Column(db.Integer, default=0)
    region = db.Column(db.String(10))
    status = db.Column(db.String(20), default='pending')
    completed_at = db.Column(db.DateTime)
    products = db.relationship('Product', backref='scan', lazy=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    scan_id = db.Column(db.Integer, db.ForeignKey('product_scan.id'), nullable=False)
    product_id = db.Column(db.String(100))
    product_name = db.Column(db.String(500))
    brand_name = db.Column(db.String(200))
    avg_price = db.Column(db.Float)
    sales = db.Column(db.Integer)
    gmv = db.Column(db.Float)
    commission_rate = db.Column(db.Float)
    rank = db.Column(db.Float)
    total_influencers = db.Column(db.Integer)
    opportunity_score = db.Column(db.Float)
    cover_url = db.Column(db.String(500))
    tiktok_url = db.Column(db.String(500))
    competition_level = db.Column(db.String(20))
    potential_earnings = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@app.route('/')
@app.route('/dashboard')
def dashboard():
    # Get filter parameters
    min_sales = request.args.get('min_sales', 0, type=int)
    max_competition = request.args.get('max_competition', 10000, type=int)
    min_commission = request.args.get('min_commission', 0, type=float)
    time_range = request.args.get('time_range', 'week')
    
    # Calculate date range
    if time_range == 'today':
        start_date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_range == 'yesterday':
        start_date = (datetime.utcnow() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_range == 'week':
        start_date = datetime.utcnow() - timedelta(days=7)
    else:  # month
        start_date = datetime.utcnow() - timedelta(days=30)
    
    # Get latest scan - removing status filter since your DB might not have completed status
    latest_scan = ProductScan.query.filter(
        ProductScan.scan_date >= start_date
    ).order_by(ProductScan.scan_date.desc()).first()
    
    products = []
    summary_stats = None
    
    if latest_scan:
        # Get products with filters
        query = Product.query.filter_by(scan_id=latest_scan.id)
        
        # Apply filters
        if min_sales > 0:
            query = query.filter(Product.sales >= min_sales)
        if max_competition < 10000:
            query = query.filter(Product.total_influencers <= max_competition)
        if min_commission > 0:
            query = query.filter(Product.commission_rate >= min_commission)
        
        # Order by opportunity score or sales
        if Product.query.first() and hasattr(Product.query.first(), 'opportunity_score'):
            products = query.order_by(Product.opportunity_score.desc()).limit(20).all()
        else:
            products = query.order_by(Product.sales.desc()).limit(20).all()
        
        # Calculate summary statistics
        if products:
            summary_stats = {
                'max_earnings': max([p.potential_earnings for p in products if p.potential_earnings] + [0]),
                'avg_commission': sum([p.commission_rate for p in products if p.commission_rate]) / len(products) if products else 0,
                'low_competition_count': len([p for p in products if p.total_influencers and p.total_influencers < 100]),
                'total_gmv': sum([p.gmv for p in products if p.gmv])
            }
    
    # Get total products count
    total_products = Product.query.count()
    
    # Format last scan date
    last_scan_date = latest_scan.scan_date.strftime('%B %d, %Y at %I:%M %p') if latest_scan else 'No scans yet'
    
    # Get top product for header
    top_product = products[0] if products else None
    
    return render_template('dashboard.html',
                         products=products,
                         summary_stats=summary_stats,
                         total_products=total_products,
                         last_scan_date=last_scan_date,
                         top_product=top_product,
                         filters={
                             'min_sales': min_sales,
                             'max_competition': max_competition,
                             'min_commission': min_commission,
                             'time_range': time_range
                         })

if __name__ == '__main__':
    app.run(debug=True)
