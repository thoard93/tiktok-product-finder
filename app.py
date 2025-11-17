from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os
from functools import wraps
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-in-production-123456789')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///tiktok_products.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Fix for Render.com postgres URLs
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

db = SQLAlchemy(app)

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ProductScan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    scan_date = db.Column(db.Date, nullable=False, index=True)
    scan_time = db.Column(db.DateTime, default=datetime.utcnow)
    region = db.Column(db.String(10), nullable=False)
    total_products_scanned = db.Column(db.Integer)
    total_qualified = db.Column(db.Integer)
    
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    scan_id = db.Column(db.Integer, db.ForeignKey('product_scan.id'), nullable=False)
    product_id = db.Column(db.String(100), nullable=False, index=True)
    product_name = db.Column(db.String(500), nullable=False)
    brand_name = db.Column(db.String(200))
    cover_url = db.Column(db.Text)
    sales = db.Column(db.Integer)
    gmv = db.Column(db.Float)
    total_influencers = db.Column(db.Integer)
    commission_rate = db.Column(db.Float)
    avg_price = db.Column(db.Float)
    potential_earnings = db.Column(db.Float)
    opportunity_score = db.Column(db.Float)
    competition_level = db.Column(db.String(20))
    rank = db.Column(db.Integer)  # 1-5 for top 5
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    scan = db.relationship('ProductScan', backref='products')

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Routes
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/products')
@login_required
def get_products():
    """API endpoint to get products with filters"""
    days_ago = request.args.get('days', 0, type=int)
    min_sales = request.args.get('min_sales', 0, type=int)
    max_influencers = request.args.get('max_influencers', 1000, type=int)
    min_commission = request.args.get('min_commission', 0, type=float)
    region = request.args.get('region', 'US')
    
    target_date = datetime.utcnow().date() - timedelta(days=days_ago)
    
    # Get the scan for that date
    scan = ProductScan.query.filter_by(
        scan_date=target_date,
        region=region
    ).order_by(ProductScan.scan_time.desc()).first()
    
    if not scan:
        return jsonify({
            'success': False,
            'message': f'No data available for {target_date}',
            'products': []
        })
    
    # Get products for that scan with filters
    products = Product.query.filter_by(scan_id=scan.id)\
        .filter(Product.sales >= min_sales)\
        .filter(Product.total_influencers <= max_influencers)\
        .filter(Product.commission_rate >= min_commission)\
        .order_by(Product.rank)\
        .all()
    
    products_data = [{
        'id': p.id,
        'product_id': p.product_id,
        'product_name': p.product_name,
        'brand_name': p.brand_name,
        'cover_url': p.cover_url,
        'sales': p.sales,
        'gmv': round(p.gmv, 2),
        'total_influencers': p.total_influencers,
        'commission_rate': round(p.commission_rate * 100, 1),
        'avg_price': round(p.avg_price, 2),
        'potential_earnings': round(p.potential_earnings, 2),
        'opportunity_score': round(p.opportunity_score, 1),
        'competition_level': p.competition_level,
        'rank': p.rank
    } for p in products]
    
    return jsonify({
        'success': True,
        'scan_date': target_date.isoformat(),
        'scan_time': scan.scan_time.isoformat(),
        'total_scanned': scan.total_products_scanned,
        'total_qualified': scan.total_qualified,
        'products': products_data
    })

@app.route('/api/available-dates')
@login_required
def get_available_dates():
    """Get all dates that have scan data"""
    scans = ProductScan.query.with_entities(
        ProductScan.scan_date, 
        ProductScan.region
    ).distinct().order_by(ProductScan.scan_date.desc()).all()
    
    dates_by_region = {}
    for scan_date, region in scans:
        if region not in dates_by_region:
            dates_by_region[region] = []
        dates_by_region[region].append(scan_date.isoformat())
    
    return jsonify(dates_by_region)

@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html')

@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    """Manage scan settings"""
    if request.method == 'POST':
        # Update settings (stored in environment or database)
        data = request.json
        # For now, just return success
        # In production, save to database or update environment
        return jsonify({'success': True, 'message': 'Settings updated'})
    
    # Return current settings
    return jsonify({
        'region': os.environ.get('SCAN_REGION', 'US'),
        'pages_to_scan': int(os.environ.get('PAGES_TO_SCAN', '20')),
        'min_sales': int(os.environ.get('MIN_SALES', '10')),
        'max_influencers': int(os.environ.get('MAX_INFLUENCERS', '500')),
        'min_commission': float(os.environ.get('MIN_COMMISSION', '0.10')),
        'scan_time': os.environ.get('SCAN_TIME', '09:00')
    })

# Initialize database and create admin user
@app.before_request
def init_db():
    """Initialize database on first request"""
    if not hasattr(app, 'db_initialized'):
        db.create_all()
        
        # Create default admin user if none exists
        if User.query.count() == 0:
            admin = User(
                username='admin',
                password_hash=generate_password_hash('changeme123')
            )
            db.session.add(admin)
            db.session.commit()
            print("Created default admin user: admin/changeme123")
        
        app.db_initialized = True

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
