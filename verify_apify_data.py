from app import app, db, Product

def verify():
    with app.app_context():
        count = Product.query.filter_by(scan_type='apify_shop').count()
        print(f"Products with scan_type='apify_shop': {count}")
        
        if count > 0:
            p = Product.query.filter_by(scan_type='apify_shop').first()
            print(f"Sample Product: {p.product_id} - {p.product_name}")
            print(f"Scan Type: {p.scan_type}")
            print(f"Is Ad Driven: {p.is_ad_driven}")
            print(f"Product URL: {p.product_url}")

if __name__ == '__main__':
    verify()
