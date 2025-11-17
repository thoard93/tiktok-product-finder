from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from flask_login import UserMixin

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    min_sales = db.Column(db.Integer, default=100)
    max_competition = db.Column(db.Integer, default=500)
    min_commission = db.Column(db.Float, default=5.0)
    categories = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
