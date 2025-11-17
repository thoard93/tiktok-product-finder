from flask import Flask, render_template, redirect, url_for, request, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from models import db, User, Product, ProductScan, UserSettings
from datetime import datetime, timedelta
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///tiktok_products.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize database
db.init_app(app)

# Initialize login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
@app.route('/dashboard')
def dashboard():
    # Get filter parameters
    min_sales = request.args.get('min_sales', 100, type=int)
    max_competition = request.args.get('max_competition', 500, type=int)
    min_commission = request.args.get('min_commission', 5, type=float)
    time_range = request.args.get('time_range', 'today')
    
    # Calculate date range
    if time_range == 'today':
        start_date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_range == 'yesterday':
        start_date = (datetime.utcnow() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_range == 'week':
        start_date = datetime.utcnow() - timedelta(days=7)
    else:  # month
        start_date = datetime.utcnow() - timedelta(days=30)
    
    # Get latest scan
    latest_scan = ProductScan.query.filter(
        ProductScan.scan_date >= start_date,
        ProductScan.status == 'completed'
    ).order_by(ProductScan.scan_date.desc()).first()
    
    products = []
    summary_stats = None
    
    if latest_scan:
        # Get products with filters - using CORRECT column names
        query = Product.query.filter_by(scan_id=latest_scan.id)
        
        # Apply filters
        query = query.filter(Product.sales >= min_sales)
        query = query.filter(Product.total_influencers <= max_competition)  # Using total_influencers
        query = query.filter(Product.commission_rate >= min_commission)
        
        # Order by opportunity score and limit to top products
        products = query.order_by(Product.opportunity_score.desc()).limit(20).all()
        
        # Calculate summary statistics with CORRECT column names
        if products:
            summary_stats = {
                'max_earnings': max([p.potential_earnings for p in products] + [0]),
                'avg_commission': sum([p.commission_rate for p in products]) / len(products) if products else 0,
                'low_competition_count': len([p for p in products if p.total_influencers < 100]),  # Using total_influencers
                'total_gmv': sum([p.gmv for p in products])  # Using gmv instead of revenue
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user_settings = UserSettings.query.filter_by(user_id=current_user.id).first()
    
    if request.method == 'POST':
        if not user_settings:
            user_settings = UserSettings(user_id=current_user.id)
        
        user_settings.min_sales = request.form.get('min_sales', 100, type=int)
        user_settings.max_competition = request.form.get('max_competition', 500, type=int)
        user_settings.min_commission = request.form.get('min_commission', 5, type=float)
        
        db.session.add(user_settings)
        db.session.commit()
        
        flash('Settings saved successfully!')
        return redirect(url_for('dashboard'))
    
    return render_template('settings.html', settings=user_settings)

# Create tables and default user
with app.app_context():
    db.create_all()
    
    # Create default user if doesn't exist
    if not User.query.filter_by(username='admin').first():
        default_user = User(
            username='admin',
            password=generate_password_hash('tiktok123')
        )
        db.session.add(default_user)
        db.session.commit()

if __name__ == '__main__':
    app.run(debug=True)
