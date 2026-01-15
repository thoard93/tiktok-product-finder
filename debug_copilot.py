import requests
import json

# Updated cookie from user's browser session
COOKIE_STR = r'''_ga=GA1.1.203608145.1767626398; __clerk_db_jwt_oNnsQakD=dvb_37qMxQlwWSisbxXvehFgH9uKiOD; __refresh_oNnsQakD=KCSvzhzk0RSn1EtQpmeT; __client_uat_oNnsQakD=0; __refresh_pOM46XQh=7Vf54AUqKWbjr4pvngRv; __client_uat=1768448365; __client_uat_pOM46XQh=1768448365; clerk_active_context=sess_38HF0PrPmYKyDSbkkWmXd8SZ3gO:; _ga_QQHYFR2Z45=GS2.1.s1768448322$o19$g1$t1768451366$j60$l0$h0; __session=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zN3gwZTJ0SXEweFhGRHpabVUzYUY2YzlmREsiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2ODQ1MTQyOSwiZnZhIjpbNTAsLTFdLCJpYXQiOjE3Njg0NTEzNjksImlzcyI6Imh0dHBzOi8vY2xlcmsudGlrdG9rY29waWxvdC5jb20iLCJuYmYiOjE3Njg0NTEzNTksInNpZCI6InNlc3NfMzhIRjBQclBtWUt5RFNia2tXbVhkOFNaM2dPIiwic3RzIjoiYWN0aXZlIiwic3ViIjoidXNlcl8zN3g0TDQzVkJ6UktJQW5sM092WWNGYk1VU20iLCJ2IjoyfQ.UUTnKoF6M2xcAAQZyEHcAq3JGHvQYM-9alWeB8YUNdu_jq2ayy8cA-O2Y_KySQ8WGE08F7K1nqumXN6gpgIbiybQ04bItRAW0XUFZqtu1Gs0tMFhuO_FIhhoTLXkKCsLMTrHX2CmiRybtGZ7MTyTuEabdPHNK07FOvyBhHrP_JGPNLU1OIHoTrE5c0FJZIqyMdliXEtrEm1CMs80-MuEAs3Gebddlxzp97SJ5bcOdCIGO6m8sa8XKwX8akO2PFj0xF0XtbaWhW6rQoZKpBy6f6UaFW3HYCUZTlPJHYvI1oBJ41-OW-yNxkFSlrAZNXq-d3jaX4D6QxeYM01dBehfrw; __session_pOM46XQh=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zN3gwZTJ0SXEweFhGRHpabVUzYUY2YzlmREsiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2ODQ1MTQyOSwiZnZhIjpbNTAsLTFdLCJpYXQiOjE3Njg0NTEzNjksImlzcyI6Imh0dHBzOi8vY2xlcmsudGlrdG9rY29waWxvdC5jb20iLCJuYmYiOjE3Njg0NTEzNTksInNpZCI6InNlc3NfMzhIRjBQclBtWUt5RFNia2tXbVhkOFNaM2dPIiwic3RzIjoiYWN0aXZlIiwic3ViIjoidXNlcl8zN3g0TDQzVkJ6UktJQW5sM092WWNGYk1VU20iLCJ2IjoyfQ.UUTnKoF6M2xcAAQZyEHcAq3JGHvQYM-9alWeB8YUNdu_jq2ayy8cA-O2Y_KySQ8WGE08F7K1nqumXN6gpgIbiybQ04bItRAW0XUFZqtu1Gs0tMFhuO_FIhhoTLXkKCsLMTrHX2CmiRybtGZ7MTyTuEabdPHNK07FOvyBhHrP_JGPNLU1OIHoTrE5c0FJZIqyMdliXEtrEm1CMs80-MuEAs3Gebddlxzp97SJ5bcOdCIGO6m8sa8XKwX8akO2PFj0xF0XtbaWhW6rQoZKpBy6f6UaFW3HYCUZTlPJHYvI1oBJ41-OW-yNxkFSlrAZNXq-d3jaX4D6QxeYM01dBehfrw; ph_phc_RA3Nibqho9D4F0xdDth2UnvdOUkcX3oenoWWDkeVnow_posthog=%7B%22distinct_id%22%3A%22user_37x4L43VBzRKIAnl3OvYcFbMUSm%22%2C%22%24sesid%22%3A%5B1768451370655%2C%22019bbfbb-eb90-78d5-94d1-fb9903b2eb8f%22%2C1768448322438%5D%2C%22%24epp%22%3Atrue%2C%22%24initial_person_info%22%3A%7B%22r%22%3A%22%24direct%22%2C%22u%22%3A%22https%3A%2F%2Fwww.tiktokcopilot.com%2F%22%7D%7D'''

def test_new_products_endpoint():
    """Test the NEW /api/trending/products endpoint discovered on the Products page"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": COOKIE_STR,
        "Referer": "https://www.tiktokcopilot.com/products",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }
    
    # NEW endpoint with ALL TIME data
    params = {
        "timeframe": "all",  # ALL TIME - This is the key!
        "sortBy": "ad_spend",
        "limit": 24,
        "page": 0,
        "region": "US"
    }
    
    print("=" * 60)
    print("TESTING NEW /api/trending/products ENDPOINT")
    print("=" * 60)
    
    res = requests.get("https://www.tiktokcopilot.com/api/trending/products", headers=headers, params=params, timeout=30)
    print(f"Status: {res.status_code}")
    
    if res.status_code == 200:
        data = res.json()
        products = data.get('products', [])
        print(f"Found {len(products)} products")
        
        if products:
            first = products[0]
            
            # Print ALL keys to discover new fields
            print("\n" + "=" * 60)
            print("FIRST PRODUCT - ALL KEYS:")
            print("=" * 60)
            for key in sorted(first.keys()):
                print(f"  {key}: {first.get(key)}")
            
            # Highlight key stats we care about
            print("\n" + "=" * 60)
            print("KEY STATS COMPARISON:")
            print("=" * 60)
            print(f"Product: {first.get('productTitle', 'N/A')[:50]}...")
            print(f"  videoCount      : {first.get('videoCount', 'MISSING')}")
            print(f"  productVideoCount: {first.get('productVideoCount', 'MISSING')}")
            print(f"  allTimeVideos   : {first.get('allTimeVideos', 'MISSING')}")
            print(f"  creatorCount    : {first.get('creatorCount', 'MISSING')}")
            print(f"  productCreatorCount: {first.get('productCreatorCount', 'MISSING')}")
            print(f"  allTimeCreators : {first.get('allTimeCreators', 'MISSING')}")
            print(f"  allTimeRevenue  : {first.get('allTimeRevenue', 'MISSING')}")
            print(f"  allTimeAdSpend  : {first.get('allTimeAdSpend', 'MISSING')}")
            print(f"  periodRevenue   : {first.get('periodRevenue', 'MISSING')}")
            print(f"  periodAdSpend   : {first.get('periodAdSpend', 'MISSING')}")
            
            # Save full response to file for deeper analysis
            with open('copilot_products_response.json', 'w') as f:
                json.dump(data, f, indent=2, default=str)
            print("\n[Saved full response to copilot_products_response.json]")
    else:
        print(f"Error {res.status_code}: {res.text[:500]}")

if __name__ == "__main__":
    test_new_products_endpoint()
