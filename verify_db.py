from app import app, db, Product
from sqlalchemy import func

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instance/products.db'
print("--- Product Scan Type Breakdown ---")
print(f"DB URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
with app.app_context():
    # Group by scan_type and count
    results = db.session.query(Product.scan_type, func.count(Product.product_id)).group_by(Product.scan_type).all()
    
    print(f"{'scan_type':<20} | {'count':<5}")
    print("-" * 30)
    total = 0
    for scan_type, count in results:
        print(f"{str(scan_type):<20} | {count:<5}")
        total += count
    print("-" * 30)
    print(f"{'Total':<20} | {total:<5}")

    # Also check specific ID user mentioned: 1729500353778978879
    pid = "1729500353778978879"
    p = Product.query.get(pid)
    if p:
        print(f"\nTarget Product {pid}: Found!")
        print(f"Scan Type: {p.scan_type}")
        print(f"Seller: {p.seller_name}")
        print(f"Videos: {p.video_count}")
        print(f"Status: {p.product_status}")
    else:
        print(f"\nTarget Product {pid}: NOT FOUND")
