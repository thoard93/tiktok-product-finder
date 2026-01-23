"""
Debug script to test TikTokCopilot API with new cookie and inspect response fields.
Run this locally to verify the cookie works and see the current API response structure.
"""
import requests
import json

# New cookie from user's cURL (30d timeframe - most recent)
NEW_COOKIE = "_ga=GA1.1.203608145.1767626398; __clerk_db_jwt_oNnsQakD=dvb_37qMxQlwWSisbxXvehFgH9uKiOD; __refresh_oNnsQakD=KCSvzhzk0RSn1EtQpmeT; __client_uat_oNnsQakD=0; __client_uat=1769087884; __client_uat_pOM46XQh=1769087884; clerk_active_context=sess_38c9F2BNijjuXJF4dANtbxnJQo6:; __refresh_pOM46XQh=TylsozbldyyeOys7Tq6s; __session=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zN3gwZTJ0SXEweFhGRHpabVUzYUY2YzlmREsiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2OTIwNTk0MywiZnZhIjpbMTk2NiwtMV0sImlhdCI6MTc2OTIwNTg4MywiaXNzIjoiaHR0cHM6Ly9jbGVyay50aWt0b2tjb3BpbG90LmNvbSIsIm5iZiI6MTc2OTIwNTg3Mywic2lkIjoic2Vzc18zOGM5RjJCTmlqanVYSkY0ZEFOdGJ4bkpRbzYiLCJzdHMiOiJhY3RpdmUiLCJzdWIiOiJ1c2VyXzM3eDRMNDNWQnpSS0lBbmwzT3ZZY0ZiTVVTbSIsInYiOjJ9.tHlYp0dc1MQCcvTH6Rqvy5Hco9YPjBTxqfkTtAFzg0Bj7jLNEY5oob1guGZrQsfrh1IpI_Ho47SwEKeJ5gVWA1GDeuVR6DIor55XfF_VyrcI1GUC9Y72a6odNMWDFOlDjIEh25WxcKpcFo0A5h9u5MAx21cp5t7h8EyzNFz03k2H2H3PPHgj4YRs4FGmpWmGpP1NjHw35HmDmrfxfcJ-VYOPQjZf9Rp-5bcq-9lpQQF3NBDFeJWl6hYLQwk9c0Pgrq_Tl4lz0SW5zkdKFn52w50kYd4K5uqR4Xuii_V3i6T88TdeS4Of3hjFztQev7Fa2BbWm6bb2yPnvm3XcJxZdw; __session_pOM46XQh=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zN3gwZTJ0SXEweFhGRHpabVUzYUY2YzlmREsiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2OTIwNTk0MywiZnZhIjpbMTk2NiwtMV0sImlhdCI6MTc2OTIwNTg4MywiaXNzIjoiaHR0cHM6Ly9jbGVyay50aWt0b2tjb3BpbG90LmNvbSIsIm5iZiI6MTc2OTIwNTg3Mywic2lkIjoic2Vzc18zOGM5RjJCTmlqanVYSkY0ZEFOdGJ4bkpRbzYiLCJzdHMiOiJhY3RpdmUiLCJzdWIiOiJ1c2VyXzM3eDRMNDNWQnpSS0lBbmwzT3ZZY0ZiTVVTbSIsInYiOjJ9.tHlYp0dc1MQCcvTH6Rqvy5Hco9YPjBTxqfkTtAFzg0Bj7jLNEY5oob1guGZrQsfrh1IpI_Ho47SwEKeJ5gVWA1GDeuVR6DIor55XfF_VyrcI1GUC9Y72a6odNMWDFOlDjIEh25WxcKpcFo0A5h9u5MAx21cp5t7h8EyzNFz03k2H2H3PPHgj4YRs4FGmpWmGpP1NjHw35HmDmrfxfcJ-VYOPQjZf9Rp-5bcq-9lpQQF3NBDFeJWl6hYLQwk9c0Pgrq_Tl4lz0SW5zkdKFn52w50kYd4K5uqR4Xuii_V3i6T88TdeS4Of3hjFztQev7Fa2BbWm6bb2yPnvm3XcJxZdw"

API_BASE = "https://www.tiktokcopilot.com/api"

def test_api():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": NEW_COOKIE,
        "Referer": "https://www.tiktokcopilot.com/products",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }
    
    params = {
        "timeframe": "7d",
        "sortBy": "revenue",
        "limit": 5,
        "page": 0,
        "region": "US"
    }
    
    print("[TEST] Testing TikTokCopilot API with new cookie...")
    print(f"   Endpoint: {API_BASE}/trending/products")
    print(f"   Params: {params}")
    print()
    
    try:
        res = requests.get(f"{API_BASE}/trending/products", headers=headers, params=params, timeout=30)
        print(f"[STATUS] {res.status_code}")
        
        if res.status_code == 200:
            data = res.json()
            
            # Check response structure
            print(f"[OK] Success! Got response with keys: {list(data.keys())}")
            
            products = data.get('products', [])
            print(f"[PRODUCTS] Products in response: {len(products)}")
            
            if products:
                # Show first product's fields
                first = products[0]
                print("\n[FIELDS] FIRST PRODUCT FIELD NAMES:")
                print("-" * 50)
                for key in sorted(first.keys()):
                    value = first[key]
                    value_preview = str(value)[:80] + "..." if len(str(value)) > 80 else str(value)
                    print(f"  {key}: {value_preview}")
                
                # Check for sales/video count fields specifically
                print("\n[METRICS] KEY METRICS (checking field names):")
                print("-" * 50)
                sales_fields = ['periodUnits', 'unitsSold', 'salesCount', 'units', 'sales', 'totalUnits']
                video_fields = ['periodVideoCount', 'adVideoCount', 'videoCount', 'videos', 'totalVideos']
                
                for f in sales_fields:
                    if f in first:
                        print(f"  [SALES] {f} = {first[f]}")
                
                for f in video_fields:
                    if f in first:
                        print(f"  [VIDEO] {f} = {first[f]}")
            else:
                print("[WARN] No products in response - check if API structure changed")
                print(f"Full response: {json.dumps(data, indent=2)[:2000]}")
        else:
            print(f"[ERROR] Status: {res.status_code}")
            print(f"   Response: {res.text[:500]}")
            
    except Exception as e:
        print(f"[EXCEPTION] {e}")

if __name__ == "__main__":
    test_api()

