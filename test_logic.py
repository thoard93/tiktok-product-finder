from app import app, db, Product, save_or_update_product
import datetime

# Mock data simulating a product from EchoTik or Apify
mock_data = {
    "product_id": "1729384756",
    "product_name": "Test Hot Glue Gun",
    "total_sale_7d_cnt": 1500,
    "total_ifl_cnt": 5,
    "total_video_cnt": 10,
    "seller_id": "74839201",
    "seller_name": "ProCraft Shop"
    # Note: product_url is MISSING, should be auto-generated
}

mock_data_broken_seller = {
    "product_id": "1729384756", # Same product
    "seller_name": "Unknown" # Should NOT overwrite existing "ProCraft Shop"
}

with app.app_context():
    print("--- Test 1: New Product (Auto-URL + ID Prefix) ---")
    # Clear existing if any
    p_id = "shop_1729384756"
    existing = Product.query.get(p_id)
    if existing: db.session.delete(existing); db.session.commit()
    
    save_or_update_product(mock_data)
    db.session.commit()
    
    p = Product.query.get(p_id)
    print(f"Product ID: {p.product_id}")
    print(f"Product Name: {p.product_name}")
    print(f"Seller Name: {p.seller_name}")
    print(f"Product URL: {p.product_url}")
    
    if p.product_url == "https://shop.tiktok.com/view/product/1729384756?region=US":
        print("SUCCESS: URL auto-generated correctly.")
    else:
        print(f"FAILURE: URL mismatch. Got {p.product_url}")

    print("\n--- Test 2: Update Product (Don't Overwrite with Unknown) ---")
    save_or_update_product(mock_data_broken_seller)
    db.session.commit()
    
    p = Product.query.get(p_id)
    print(f"Seller after update with 'Unknown': {p.seller_name}")
    if p.seller_name == "ProCraft Shop":
        print("SUCCESS: Seller name preserved.")
    else:
        print("FAILURE: Seller name overwritten.")

    print("\n--- Test 3: Update with Real URL ---")
    mock_data_with_url = {
        "product_id": "1729384756",
        "product_url": "https://www.tiktok.com/@user/video/123" # Mock specialized URL
    }
    save_or_update_product(mock_data_with_url)
    db.session.commit()
    p = Product.query.get(p_id)
    print(f"Product URL after update: {p.product_url}")
    if "tiktok.com/@user" in p.product_url:
        print("SUCCESS: Custom URL preserved.")
    else:
        print("FAILURE: Custom URL lost.")
