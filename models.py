from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class ProductScan(db.Model):
    __tablename__ = 'product_scan'
    
    id = db.Column(db.Integer, primary_key=True)
    scan_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    total_products_scanned = db.Column(db.Integer, default=0)
    total_qualified = db.Column(db.Integer, default=0)
    region = db.Column(db.String(10))
    
    # Relationship to products
    products = db.relationship('Product', backref='scan', lazy=True)

class Product(db.Model):
    __tablename__ = 'product'
    
    id = db.Column(db.Integer, primary_key=True)
    scan_id = db.Column(db.Integer, db.ForeignKey('product_scan.id'), nullable=False)
    product_id = db.Column(db.String(100))
    product_name = db.Column(db.String(500))
    avg_price = db.Column(db.Float)
    sales = db.Column(db.Integer)
    gmv = db.Column(db.Float)
    commission_rate = db.Column(db.Float)
    total_influencers = db.Column(db.Integer)
    potential_earnings = db.Column(db.Float)
    rank = db.Column(db.Float)  # For ratings
    image_url = db.Column(db.String(500))  # Product image URL
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    @property
    def tiktok_url(self):
        """Generate TikTok Shop URL for the product"""
        if self.product_id:
            # Correct TikTok Shop product URL format
            return f"https://shop.tiktok.com/view/product/{self.product_id}"
        return "#"
    
    def __repr__(self):
        return f'<Product {self.product_name}>'
