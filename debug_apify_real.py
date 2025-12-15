from apify_service import ApifyService
import json

TEST_ID = "1729500353778978879"
TEST_URL = f"https://shop.tiktok.com/view/product/{TEST_ID}?region=US&locale=en"
ACTOR_PRATIK = "pratikdani~tiktok-shop-search-scraper"

input_data = {
    "startUrls": [{"url": TEST_URL}],
    "country_code": "US",
    "maxItems": 1
}

print("Fetching keys...")
try:
    res = ApifyService.run_actor(ACTOR_PRATIK, input_data, wait_sec=30)
    if isinstance(res, list) and len(res) > 0:
        item = res[0]
        print("\n--- ITEM KEYS ---")
        print(json.dumps(list(item.keys()), indent=2))
        
        print("\n--- SAMPLE METRICS ---")
        print(f"Sales: {item.get('sales')}, Sold: {item.get('sold')}, Total Sales: {item.get('total_sales')}")
        print(f"Videos: {item.get('videos_count')}, Total Videos: {item.get('total_video_count')}")
    else:
        print("Empty or error.")
except Exception as e:
    print(e)
