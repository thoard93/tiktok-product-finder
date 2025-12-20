from app import app, db, Product
import json

print("--- Deep Diagnostic: Unknown Products ---")

with app.app_context():
    # Find products with Unknown seller
    prods = Product.query.filter(Product.seller_name == 'Unknown').limit(5).all()
    
    results = []
    for p in prods:
        results.append({
            'id': p.product_id,
            'name': p.product_name,
            'seller': p.seller_name,
            'image': p.image_url,
            'cached_image': p.cached_image_url,
            'sales': p.sales,
            'sales_7d': p.sales_7d,
            'last_updated': p.last_updated.isoformat() if p.last_updated else None
        })

    print(json.dumps(results, indent=2))
