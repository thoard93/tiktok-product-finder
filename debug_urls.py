from app import app, db, Product

def debug_urls():
    with app.app_context():
        # Get a working product (not apify_shop, assuming 'viral_trends' or similar is working)
        working_p = Product.query.filter(Product.scan_type != 'apify_shop').first()
        
        # Get a broken product (apify_shop)
        broken_p = Product.query.filter_by(scan_type='apify_shop').first()
        
        print("--- WORKING PRODUCT ---")
        if working_p:
            print(f"ID: {working_p.product_id}")
            print(f"URL: {working_p.product_url}")
            print(f"Scan Type: {working_p.scan_type}")
        else:
            print("No 'working' product found to compare.")

        print("\n--- BROKEN PRODUCT (New Scan) ---")
        if broken_p:
            print(f"ID: {broken_p.product_id}")
            print(f"URL: {broken_p.product_url}")
            print(f"Scan Type: {broken_p.scan_type}")
            print(f"Name: {broken_p.product_name}")
        else:
            print("No 'broken' product found.")

if __name__ == '__main__':
    debug_urls()
