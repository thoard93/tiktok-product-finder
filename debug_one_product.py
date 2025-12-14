from app import app, db, Product

def debug_product():
    with app.app_context():
        # Verify DB File Path FIRST
        print(f"  DB URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
        
        # Search by partial name
        print("Searching for 'Facial Massager'...")
        p = Product.query.filter(Product.product_name.like('%Facial Massager%')).first()
        
        if not p:
            print("Product NOT FOUND by name.")
            # Try searching by ID from log if possible, but let's stick to name first
            return

        print(f"Product Found: {p.product_name[:30]}")
        print(f"  ID: {p.product_id}")
        print(f"  Scan Type: {p.scan_type}")
        print(f"  Live Count (Stock): {p.live_count}")
        print(f"  Favorites (Old): {getattr(p, 'favorites', 'N/A')}")
        print(f"  Commission: {p.commission_rate}")
        print(f"  Sales: {p.sales}")
        print(f"  Video Count: {p.video_count}")
        print(f"  URL: {p.product_url}")
        print(f"  Updated: {p.last_updated}")
        
        # Verify DB File Path
        print(f"  DB URI: {app.config['SQLALCHEMY_DATABASE_URI']}")

if __name__ == '__main__':
    debug_product()
