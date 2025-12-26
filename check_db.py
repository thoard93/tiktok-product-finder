from app import app, Product
import json

with app.app_context():
    # Enriched product confirmed in logs
    p1 = Product.query.get('1731194857673101831')
    p2 = Product.query.get('shop_1731194857673101831')
    p = p1 or p2
    
    if p:
        print(f"Product: {p.product_id}")
        print(f"Name: {p.product_name}")
        print(f"Sales 7D: {p.sales_7d}")
        print(f"Total Sales: {p.sales}")
        print(f"Video Count: {p.video_count}")
        print(f"Image URL: {p.image_url}")
        print(f"Cached Image URL: {p.cached_image_url}")
        print(f"Last Updated: {p.last_updated}")
    else:
        print("Product not found in DB.")
