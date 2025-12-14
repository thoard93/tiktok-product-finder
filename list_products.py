from app import app, db, Product

def list_all():
    with app.app_context():
        total = Product.query.count()
        print(f"Total Products in DB: {total}")
        
        products = Product.query.limit(10).all()
        for p in products:
            print(f"ID: {p.product_id} | Type: {p.scan_type} | URL: {p.product_url}")

if __name__ == '__main__':
    list_all()
