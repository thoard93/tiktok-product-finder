from app import app, db, Product

with app.app_context():
    print(f"DB URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
    total = Product.query.count()
    print(f"Total Products in DB: {total}")

    count_shop = Product.query.filter_by(scan_type='apify_shop').count()
    count_viral = Product.query.filter_by(scan_type='apify_viral').count()
    print(f"Products with scan_type='apify_shop': {count_shop}")
    print(f"Products with scan_type='apify_viral': {count_viral}")
    
    if count_shop > 0:
    if count_shop > 0:
        latest = Product.query.filter_by(scan_type='apify_shop').order_by(Product.first_seen.desc()).limit(10).all()
        for p in latest:
            print(f"--- Product: {p.product_id} ---")
            print(f"Name: {p.product_name[:50]}...")
            print(f"Seller: {p.seller_name}")
            print(f"Sales: {p.sales} | Sales7d: {p.sales_7d} | Sales30d: {p.sales_30d}")
            print(f"GMV: {p.gmv} | GMV7d: {p.gmv_7d}")
            print(f"Image: {p.image_url}")
            print(f"First Seen: {p.first_seen}")
            print("-" * 30)
