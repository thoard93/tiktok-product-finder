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
        latest = Product.query.filter_by(scan_type='apify_shop').order_by(Product.first_seen.desc()).limit(5).all()
        for p in latest:
            print(f"- {p.product_name} | ID: {p.product_id} | Videos: {p.video_count} | Hidden: {p.is_hidden}")
