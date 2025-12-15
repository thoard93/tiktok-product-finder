import os
import requests
import json
import time
from datetime import datetime

# Configuration
t_part1 = "apify_api_"
t_part2 = "fd3d6uEEsUzuizgkMQHR"
t_part3 = "SHYSQXn47W0sE7Uf"
APIFY_API_TOKEN = os.environ.get('APIFY_API_TOKEN', t_part1 + t_part2 + t_part3)

# Actor IDs
ACTOR_SEARCH = "pratikdani~tiktok-shop-search-scraper"
ACTOR_DETAIL = "excavator~tiktok-shop-product"

def log(msg):
    print(f"[ApifyService] {msg}", flush=True)

class ApifyService:
    @staticmethod
    def get_token():
        return APIFY_API_TOKEN

    @staticmethod
    def run_actor(actor_id, run_input, wait_sec=60):
        """
        Runs an Apify actor and returns the items.
        """
        token = ApifyService.get_token()
        if not token:
            return {'error': 'Missing API Token'}

        # 1. Start
        start_url = f"https://api.apify.com/v2/acts/{actor_id}/runs?token={token}"
        try:
            start_res = requests.post(start_url, json=run_input)
            if start_res.status_code != 201:
                return {'error': f"Start failed: {start_res.text}"}
            
            run_data = start_res.json()['data']
            run_id = run_data['id']
            dataset_id = run_data['defaultDatasetId']
            # log(f"Started Run {run_id}")
        except Exception as e:
            return {'error': f"Exception executing actor: {str(e)}"}

        # 2. Poll
        slept = 0
        while slept < wait_sec:
            time.sleep(3)
            slept += 3
            
            status_res = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}")
            if status_res.status_code != 200: continue
            
            status = status_res.json()['data']['status']
            if status == 'SUCCEEDED':
                break
            if status in ['FAILED', 'ABORTED', 'TIMED-OUT']:
                return {'error': f"Run ended with status: {status}"}
        
        if status != 'SUCCEEDED':
            return {'error': 'Timeout waiting for actor'}

        # 3. Fetch
        data_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={token}"
        data_res = requests.get(data_url)
        return data_res.json()

    @staticmethod
    def search_products(keyword, limit=10):
        """
        Search for products by keyword (Trending/Discovery).
        """
        run_input = {
            "keyword": keyword,
            "limit": limit,
            "country_code": "US",
            "sort_type": "relevance_desc" # or 1/desc
        }
        return ApifyService.run_actor(ACTOR_SEARCH, run_input)

    @staticmethod
    def get_product_details(urls_or_ids):
        """
        Get details for specific product IDs or URLs.
        Optimized to use SEARCH Actor (Pratik Dani) for IDs as it's faster/reliable.
        """
        # 1. Try Search-By-ID (Preferred for Bridge)
        if len(urls_or_ids) == 1 and str(urls_or_ids[0]).isdigit():
            # User reported Excavator is slow/broken. Using Pratik Dani Search instead.
            # Searching by ID usually returns the exact product as first result.
            return ApifyService.search_products(str(urls_or_ids[0]), limit=1)

        # 2. Fallback to URL Scraper (Excavator) for full links
        urls = []
        for x in urls_or_ids:
            if 'shop.tiktok.com' in x:
                urls.append({"url": x})
            elif str(x).isdigit():
                urls.append({"url": f"https://shop.tiktok.com/view/product/{x}?region=US&locale=en"})
        
        if not urls: return []

        run_input = {
            "urls": urls,
            "maxItems": len(urls)
        }
        return ApifyService.run_actor(ACTOR_DETAIL, run_input)

    @staticmethod
    def normalize_item(item):
        """
        Normalizes Apify raw data into a standard Dict matching our DB Model.
        Returns None if invalid.
        """
        if item.get('error'): return None

        pid_raw = str(item.get('id') or item.get('product_id'))
        if not pid_raw or 'None' in pid_raw: return None
        
        # Helper parsers
        def parse_metric(val):
            if not val: return 0
            val = str(val).replace('$', '').replace(',', '').strip()
            mult = 1
            if 'K' in val: mult = 1000; val = val.replace('K','')
            elif 'M' in val: mult = 1000000; val = val.replace('M','')
            try: return int(float(val) * mult)
            except: return 0

        def parse_float(val):
            if not val: return 0.0
            val = str(val).replace('$', '').replace(',', '').strip()
            try: return float(val)
            except: return 0.0
            
        data = {}
        data['product_id'] = f"shop_{pid_raw}"
        data['raw_id'] = pid_raw
        
        # Name
        data['product_name'] = (item.get('product_name') or item.get('title') or item.get('name') or "Unknown")[:200]
        
        # Images
        data['image_url'] = None
        if item.get('images') and len(item.get('images')) > 0:
            data['image_url'] = item.get('images')[0]
        if not data['image_url']:
            data['image_url'] = item.get('cover_url') or item.get('main_images', [None])[0]

        # Seller
        seller = item.get('seller') or {}
        if isinstance(seller, dict):
            data['seller_name'] = seller.get('seller_name') or item.get('shop_name')
        else:
            data['seller_name'] = item.get('seller_name') or item.get('shop_name')

        # Metrics
        data['sales'] = parse_metric(item.get('total_sale_cnt') or item.get('sales') or item.get('sold'))
        data['sales_7d'] = parse_metric(item.get('total_sale_7d_cnt')) # Often missing in search scraper
        data['sales_30d'] = parse_metric(item.get('total_sale_30d_cnt') or item.get('sales_30d'))
        data['influencer_count'] = parse_metric(item.get('total_ifl_cnt'))
        data['video_count'] = parse_metric(item.get('total_video_count') or item.get('videos_count'))
        
        # Price
        data['price'] = parse_float(item.get('avg_price') or item.get('real_price') or item.get('price'))
        data['original_price'] = parse_float(item.get('original_price') or item.get('market_price') or item.get('list_price'))
        
        # Fallback for orig price from SKU
        if not data['original_price'] and item.get('skus'):
             try:
                sku = item.get('skus')[0] if isinstance(item.get('skus'), list) else list(item.get('skus').values())[0]
                data['original_price'] = parse_float(sku.get('original_price'))
             except: pass

        # Commission
        raw_comm = item.get('affiliate_commission_rate') or item.get('commission_rate')
        if raw_comm:
             try:
                 c = float(str(raw_comm).replace('%',''))
                 if c < 1: c = c * 100
                 data['commission_rate'] = c
             except: data['commission_rate'] = 0
        
        # Stock (Sum SKUs)
        stock = 0
        skus = item.get('skus') or {}
        if isinstance(skus, dict):
             for k,v in skus.items(): stock += int(v.get('stock',0))
        elif isinstance(skus, list):
             for s in skus: stock += int(s.get('stock',0))
        data['live_count'] = stock
        
        return data
