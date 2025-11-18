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
    image_url = db.Column(db.String(2000))  # Product image URL (EchoTik URLs can be very long)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    @property
    def tiktok_url(self):
        """Generate TikTok Shop search URL for the product"""
        if self.product_name:
            # Use TikTok Shop search - more reliable than direct product links
            import urllib.parse
            search_term = urllib.parse.quote(self.product_name[:50])
            return f"https://shop.tiktok.com/search?q={search_term}"
        return "#"
    
    def __repr__(self):
        return f'<Product {self.product_name}>'
