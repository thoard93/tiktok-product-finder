from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class ProductScan(db.Model):
    __tablename__ = 'product_scan'
    
    id = db.Column(db.Integer, primary_key=True)
    scan_date = db.Column(db.DateTime, nullable=False)
    total_products_scanned = db.Column(db.Integer, default=0)
    total_qualified = db.Column(db.Integer, default=0)
    region = db.Column(db.String(10))
    scan_type = db.Column(db.String(50), default='general')  # NEW: 'general' or 'brand_hunter'
    
    # Relationship to products
    products = db.relationship('Product', backref='scan', lazy=True, cascade='all, delete-orphan')

class Product(db.Model):
    __tablename__ = 'product'
    
    id = db.Column(db.Integer, primary_key=True)
    scan_id = db.Column(db.Integer, db.ForeignKey('product_scan.id'), nullable=False)
    product_id = db.Column(db.String(255), nullable=False)
    product_name = db.Column(db.Text)
    seller_name = db.Column(db.String(500))  # NEW: Seller/Brand name
    avg_price = db.Column(db.Float)
    sales = db.Column(db.Integer)
    gmv = db.Column(db.Float)
    commission_rate = db.Column(db.Float)
    total_influencers = db.Column(db.Integer)
    potential_earnings = db.Column(db.Float)
    image_url = db.Column(db.String(2000))  # NEW: Product image URL (increased to 2000 chars)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        db.UniqueConstraint('product_id', 'scan_id', name='unique_product_scan'),
    )
    
    @property
    def tiktok_url(self):
        """Generate TikTok Shop search URL for this product"""
        return f"https://shop.tiktok.com/search/product?q={self.product_name.replace(' ', '+')}"
