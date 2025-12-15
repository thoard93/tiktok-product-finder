from apify_service import ApifyService
import json

ID = "1729500353778978879"
URL = f"https://shop.tiktok.com/view/product/{ID}?region=US&locale=en"

print(f"--- Testing Excavator for {ID} ---")
print(f"URL: {URL}")

# Run Actor Direclty
print(">> Running Excavator...")
res = ApifyService.run_actor(ApifyService.ACTOR_DETAIL, {
    "urls": [{"url": URL}],
    "maxItems": 1
})

print(f">> Result type: {type(res)}")
if isinstance(res, list) and len(res) > 0:
    item = res[0]
    print(f">> Keys: {list(item.keys())}")
    print(f">> ID field: {item.get('id') or item.get('product_id')}")
    
    # Test Normalize
    print(">> Testing normalize_item...")
    norm = ApifyService.normalize_item(item)
    if norm:
        print("SUCCESS! Normalized Data:")
        print(json.dumps(norm, indent=2))
    else:
        print("FAILED: normalize_item returned None.")
else:
    print(">> Excavator returned empty list or error.")
    print(res)
