
def save_product_to_db(product_data):
    """
    Save enriched product data to database.
    Expects a dictionary with at least 'product_id'.
    """
    with app.app_context():
        raw_pid = str(product_data.get('product_id')).replace('shop_', '')
        shop_pid = f"shop_{raw_pid}"
        
        # Check if exists
        p = Product.query.get(shop_pid)
        if not p:
            p = Product(product_id=shop_pid)
            p.first_seen = datetime.now(datetime.timezone.utc)
            db.session.add(p)
            
        # Update fields
        p.scan_type = 'bot_lookup'
        p.product_name = product_data.get('product_name') or product_data.get('title') or p.product_name or "Unknown"
        p.image_url = product_data.get('image_url') or product_data.get('cover') or p.image_url
        p.sales = product_data.get('sales', 0)
        p.sales_7d = product_data.get('sales_7d', 0)
        p.sales_30d = product_data.get('sales_30d', 0)
        p.influencer_count = product_data.get('influencer_count', 0)
        p.video_count = product_data.get('video_count', 0)
        p.commission_rate = product_data.get('commission_rate', 0)
        p.price = product_data.get('price', 0)
        p.last_updated = datetime.now(datetime.timezone.utc)
        p.live_count = 999 
        p.is_enriched = True
        
        return p
