from app import app, db, Product
from datetime import datetime, timedelta

def verify_recent():
    with app.app_context():
        # Get products updated in last hour
        since = datetime.utcnow() - timedelta(hours=1)
        products = Product.query.filter(Product.last_updated >= since).all()
        print(f"Checking {len(products)} RECENT products:")
        for p in products:
            print(f"Product: {p.product_name[:30]}")
            print(f"  Scan Type: {p.scan_type}")
            print(f"  Live Count: {p.live_count}")
            print(f"  ID: {p.product_id}")
            print("-" * 20)

if __name__ == '__main__':
    verify_recent()
