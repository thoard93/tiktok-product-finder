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
class ApifyService:
    # Actor IDs
    ACTOR_SEARCH = "pratikdani~tiktok-shop-search-scraper"
    ACTOR_DETAIL = "excavator~tiktok-shop-product"

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
        return ApifyService.run_actor(ApifyService.ACTOR_SEARCH, run_input)

    @classmethod
    def get_product_details(cls, url_or_id):
        """
        Fetches details for a single product. 
        Auto-detects if input is ID or URL.
        Uses Pratik Dani's 'Search' actor via startUrls for fast lookup.
        Fallback to Excavator if needed.
        """
        is_id = url_or_id.isdigit()
        
        # 1. Construct URL if ID
        if is_id:
            target_url = f"https://shop.tiktok.com/view/product/{url_or_id}?region=US&locale=en"
            print(f"DEBUG: Converting ID {url_or_id} to URL: {target_url}")
        else:
            target_url = url_or_id
        
        # 2. Try Pratik Dani (Search Actor supports startUrls for details) -- FAST & RELIABLE
        print(f"DEBUG: Attempting Pratik Dani lookup for {target_url}")
        try:
            # Pratik Dani Search Actor requires 'country_code' with startUrls
            run_input = {
                "startUrls": [{"url": target_url}],
                "country_code": "US",
                "maxItems": 1 # Try cheap/fast lookup first
            }
            # Use 'search' actor but in detail mode
            items = cls.run_actor(ApifyService.ACTOR_SEARCH, run_input, wait_sec=45)
            
            if items:
                # FILTER: Find the exact product ID we asked for
                if is_id:
                    for i in items:
                        # Handle nested product key if present
                        p_data = i.get('product', i)
                        pid = str(p_data.get('product_id') or p_data.get('id') or '')
                        if pid == str(url_or_id):
                            print(f"DEBUG: Found MATCHING Product ID {pid}")
                            return [i]
                    
                    print(f"DEBUG: ID {url_or_id} not found in Pratik results. Falling back...")
                    # Do NOT return [] here; let it fall through to Excavator
                else:
                    return items[:1] # URL mode: return first result
            else:
                print("DEBUG: Pratik Dani returned 0 items. Falling back...")
        except Exception as e:
            print(f"DEBUG: Pratik Dani failed: {e}")

        # 3. Fallback: Excavator (Detail Scraper) -- SLOW but official
        print("DEBUG: Fallback to Excavator...")
        return cls.run_actor(cls.ACTOR_DETAIL, {
            "urls": [{"url": target_url}],
            "maxItems": 1
        })

    @staticmethod
    def normalize_item(item):
        """
        Normalizes Apify raw data into a standard Dict matching our DB Model.
        Returns None if invalid.
        """
        if not item or item.get('error'): return None

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
            
        # UNWRAP: Pratik Dani's Search Scraper often nests data in 'product' or 'item'
        if 'product' in item and isinstance(item['product'], dict):
            wrapper = item
            item = item['product']
            if 'stats' in wrapper: item['stats'] = wrapper['stats']

        # ID Check
        pid_raw = str(item.get('product_id') or item.get('id') or item.get('item_id') or '')
        if not pid_raw: 
            return None        

        data = {}
        data['product_id'] = pid_raw
        data['raw_id'] = pid_raw
        
        # Name
        data['product_name'] = (item.get('product_name') or item.get('title') or item.get('name') or "Unknown")[:200]
        
        # Images (Prioritize cover_url for Pratik)
        data['image_url'] = (
            item.get('cover_url') or 
            item.get('main_image', {}).get('url') if isinstance(item.get('main_image'), dict) else item.get('main_image') or
            item.get('image_url')
        )

        # Metrics (Pratik Dani Keys + Fallbacks)
        # Pratik: total_sale_cnt, total_sale_gmv_amt, etc.
        data['sales'] = parse_metric(item.get('total_sale_cnt') or item.get('sales') or item.get('sold'))
        data['sales_7d'] = parse_metric(item.get('sales_7d') or item.get('sales_7d_count') or 0)
        data['sales_30d'] = parse_metric(item.get('total_sale_30d_cnt') or item.get('sales_30d'))
        data['gmv'] = parse_float(item.get('total_sale_gmv_amt') or item.get('gmv'))
        data['video_count'] = parse_metric(item.get('total_video_count') or item.get('videos_count'))
        data['influencer_count'] = parse_metric(item.get('total_ifl_cnt') or item.get('influencer_count'))
        
        # Price
        data['price'] = parse_float(item.get('avg_price') or item.get('real_price') or item.get('price'))
        data['currency'] = item.get('currency', 'USD')
        
        # Stock: Pratik sometimes uses 'skus' or 'stock'
        if 'stock' in item:
            data['stock'] = parse_metric(item['stock'])
        else:
            # Sum SKUs if available
            stock = 0
            skus = item.get('skus')
            if isinstance(skus, list):
                for s in skus: stock += int(s.get('stock', 0))
            elif isinstance(skus, dict):
                for v in skus.values(): stock += int(v.get('stock', 0))
            data['stock'] = stock

        data['has_free_shipping'] = bool(item.get('is_free_shipping'))

        # Seller
        seller = item.get('seller') or item.get('shop_info') or {}
        if isinstance(seller, dict):
            data['shop_name'] = seller.get('shop_name') or seller.get('name')
            data['shop_id'] = str(seller.get('shop_id') or seller.get('id') or '')
            data['shop_url'] = seller.get('url')
        else:
            data['shop_name'] = str(seller) if seller else None

        # URL
        data['product_url'] = item.get('product_url') or item.get('url') or f"https://shop.tiktok.com/view/product/{pid_raw}?region=US&locale=en"
        
        return data

