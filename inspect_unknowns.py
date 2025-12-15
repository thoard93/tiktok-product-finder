from app import app, db, Product

print("--- Inspecting 'Unknown' Products ---")
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instance/products.db'

with app.app_context():
    products = Product.query.filter(Product.product_name == 'Unknown').all()
    print(f"Found {len(products)} 'Unknown' products.")
    
    for p in products[:5]:
        print(f"ID: {p.product_id}")
        print(f"URL: {p.product_url}")
        print(f"Stats: Sales={p.sales}, Vids={p.video_count}")
        print("-" * 30)
