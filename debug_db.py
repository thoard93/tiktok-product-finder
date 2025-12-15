from app import app, db, Product
import os

with app.app_context():
    print(f"DB URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
    db.create_all()
    print("Tables created (if needed).")
    
    try:
        count = Product.query.count()
        print(f"Product count: {count}")
        
        # Try inserting a dummy product
        p = Product.query.get("test_id")
        if not p:
            p = Product(product_id="test_id", product_name="Test Product")
            db.session.add(p)
            db.session.commit()
            print("Test product inserted.")
        else:
            print("Test product already exists.")
            
    except Exception as e:
        print(f"DB Error: {e}")
