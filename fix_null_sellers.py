from app import app, db, Product
import sys

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///products.db'
print("--- Fixing NULL Seller Names ---")
with app.app_context():
    # Find products where seller_name is NULL or empty
    products = Product.query.filter(db.or_(Product.seller_name == None, Product.seller_name == '')).all()
    count = len(products)
    
    print(f"Found {count} products with NULL seller_name.")
    
    if count > 0:
        for p in products:
            p.seller_name = "Unknown Shop"
            print(f"Fixed: {p.product_id} ({p.product_name})")
        
        db.session.commit()
        print(f"Successfully updated {count} records.")
    else:
        print("No records needed fixing.")
