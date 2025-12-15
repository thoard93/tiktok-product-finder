from app import app, db, Product
import sys

PID = "1729500353778978879"

print(f"--- Verifying Product {PID} ---")
with app.app_context():
    p = Product.query.get(PID)
    if not p:
        print("Product NOT FOUND in DB.")
    else:
        print(f"Product Found: {p.product_name}")
        print(f"Sales: {p.sales}")
        print(f"Sales 7d: {p.sales_7d}")
        print(f"Sales 30d: {p.sales_30d}")
        print(f"Videos: {p.video_count}")
        print(f"Stock: {p.live_count}") # mapped to stock
        print(f"Shop: {p.seller_name}")
        print(f"Last Updated: {p.last_updated}")
