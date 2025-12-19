from app import app, db, Product, get_top_brands, get_seller_products, save_or_update_product
import os

with app.app_context():
    print("--- STEP 1: Fetching a Brand ---")
    brands = get_top_brands(page=1)
    if not brands:
        print("FAILED: No brands found from EchoTik.")
        exit(1)
    
    target_brand = brands[0]
    seller_id = target_brand.get('seller_id')
    seller_name = target_brand.get('seller_name') or target_brand.get('shop_name')
    print(f"Target Brand: {seller_name} ({seller_id})")

    print("\n--- STEP 2: Scanning Products (Page 1) ---")
    products = get_seller_products(seller_id, page=1, page_size=5)
    if not products:
        print("FAILED: No products found for this brand.")
        exit(1)
    
    print(f"Found {len(products)} products. Saving...")
    saved_count = 0
    for p in products:
        p['seller_id'] = seller_id
        p['seller_name'] = seller_name # Manual pass for this test
        if save_or_update_product(p):
            saved_count += 1
    
    db.session.commit()
    print(f"Saved {saved_count} products.")

    print("\n--- STEP 3: Verifying Data Integrity ---")
    db.session.expire_all()
    recent = Product.query.order_by(Product.last_updated.desc()).limit(5).all()
    
    print(f"{'ID':<25} | {'Seller':<25} | {'URL'}")
    print("-" * 150)
    for p in recent:
        p_id = (p.product_id[:22] + "..") if len(p.product_id) > 22 else p.product_id
        seller = (p.seller_name[:22] + "..") if p.seller_name and len(p.seller_name) > 22 else (p.seller_name or "Unknown")
        p_url = (p.product_url[:100] + "..") if p.product_url and len(p.product_url) > 100 else (p.product_url or "MISSING")
        print(f"{p_id:<25} | {seller:<25} | {p_url}")
