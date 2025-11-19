from flask import Flask, render_template, request, jsonify, Response
from models import db, ProductScan, Product
from sqlalchemy import desc
import os
import requests

app = Flask(__name__)

# Database configuration
database_url = os.environ.get('DATABASE_URL')
if database_url and database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

@app.route('/')
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/products')
def get_products():
    # Get tab parameter (default to 'all')
    tab = request.args.get('tab', 'all')
    
    # Determine scan type based on tab
    scan_type = 'general' if tab == 'all' else 'brand_hunter'
    
    # Get latest scan of this type with products
    latest_scan = ProductScan.query.filter(
        ProductScan.scan_type == scan_type,
        ProductScan.total_qualified > 0
    ).order_by(desc(ProductScan.total_qualified)).first()
    
    if not latest_scan:
        return jsonify({
            'scan_date': None,
            'total_products': 0,
            'products': []
        })
    
    # Get filter and sort parameters
    brand_filter = request.args.get('brand', '').lower()
    sort_by = request.args.get('sort', 'best')
    
    # Query products for this scan
    query = Product.query.filter_by(scan_id=latest_scan.id)
    
    # Apply brand filter if provided
    if brand_filter:
        query = query.filter(Product.seller_name.ilike(f'%{brand_filter}%'))
    
    # Apply sorting
    if sort_by == 'low':
        query = query.order_by(Product.total_influencers.asc())
    elif sort_by == 'high':
        query = query.order_by(Product.total_influencers.desc())
    else:  # best
        # Best opportunities: high potential earnings, low competition
        query = query.order_by(
            (Product.potential_earnings / (Product.total_influencers + 1)).desc()
        )
    
    products = query.all()
    
    # Format products for JSON
    products_data = []
    for p in products:
        products_data.append({
            'id': p.id,
            'product_name': p.product_name,
            'seller_name': p.seller_name,
            'sales': p.sales,
            'gmv': float(p.gmv) if p.gmv else 0,
            'total_influencers': p.total_influencers,
            'commission_rate': float(p.commission_rate) if p.commission_rate else 0,
            'potential_earnings': float(p.potential_earnings) if p.potential_earnings else 0,
            'image_url': p.image_url,
            'tiktok_url': p.tiktok_url
        })
    
    return jsonify({
        'scan_date': latest_scan.scan_date.strftime('%Y-%m-%d %H:%M'),
        'total_products': len(products_data),
        'products': products_data
    })

@app.route('/proxy-image')
def proxy_image():
    """Proxy route to fetch images from EchoSell CDN and serve them to frontend"""
    image_url = request.args.get('url')
    
    if not image_url:
        print("❌ No image URL provided")
        return '', 404
    
    print(f"🖼️  Fetching image: {image_url[:100]}...")
    
    try:
        # Fetch the image from the CDN with longer timeout
        response = requests.get(image_url, timeout=10, allow_redirects=True)
        
        if response.status_code == 200:
            print(f"✅ Image fetched successfully ({len(response.content)} bytes)")
            # Return the image with appropriate content type
            return Response(
                response.content,
                mimetype=response.headers.get('Content-Type', 'image/jpeg'),
                headers={
                    'Cache-Control': 'public, max-age=86400',  # Cache for 24 hours
                    'Access-Control-Allow-Origin': '*'
                }
            )
        else:
            print(f"❌ Failed to fetch image: HTTP {response.status_code}")
            return '', 404
            
    except requests.exceptions.Timeout:
        print(f"❌ Timeout fetching image")
        return '', 404
    except Exception as e:
        # If fetch fails, return 404
        print(f"❌ Error fetching image: {str(e)}")
        return '', 404

@app.route('/test-proxy')
def test_proxy():
    """Debug route to test proxy functionality"""
    test_url = "https://echosell-images.tos-ap-southeast-1.volces.com/product-cover/704/1729383890~tplv-6sxg-9z883.jpeg"
    return f"""
    <h1>Proxy Test</h1>
    <p>Testing with URL: {test_url}</p>
    <p><a href="/proxy-image?url={test_url}">Test Proxy</a></p>
    <p>Direct image test:</p>
    <img src="/proxy-image?url={test_url}" />
    """

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    """Show detailed view of a single product"""
    product = Product.query.get_or_404(product_id)
    scan = ProductScan.query.get(product.scan_id)
    
    # Determine if this is a gem (from brand_hunter scan)
    is_gem = scan.scan_type == 'brand_hunter'
    
    return render_template('product_detail.html', product=product, scan=scan, is_gem=is_gem)

if __name__ == '__main__':
    app.run(debug=True)
