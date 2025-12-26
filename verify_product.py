from app import app, db, Product
import json

with app.app_context():
    pid = "shop_1731997246433955867"
    p = Product.query.get(pid)
    if p:
        print(f"Product Found: {p.product_id}")
        print(f"Name: {p.product_name}")
        print(f"Sales 7D: {p.sales_7d}")
        print(f"Video Count: {p.video_count}")
        print(f"Image URL: {p.image_url}")
        print(f"Scan Type: {p.scan_type}")
        print(f"Last Updated: {p.last_updated}")
    else:
        print(f"Product {pid} NOT found in DB.")
