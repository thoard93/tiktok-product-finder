import requests
import json

# TikTokCopilot API Endpoint
API_URL = "https://www.tiktokcopilot.com/api/trending"

# Session cookie from the user
COOKIE_STR = "__clerk_db_jwt=dvb_37qMxQlwWSisbxXvehFgH9uKiOD; _ga=GA1.1.203608145.1767626398; __clerk_db_jwt_oNnsQakD=dvb_37qMxQlwWSisbxXvehFgH9uKiOD; __client_uat=1767750231; __refresh_oNnsQakD=2VoZQ8xr0QVrrHsMwddY; clerk_active_context=sess_37uPxo7PFXYLD7kFP5sXZVEsB8i:; __client_uat_oNnsQakD=1767750231; __session=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zNkozejRqN1BNMDRnbEk1MVFDUHBIekhIZTEiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2NzgwOTgxNSwiZnZhIjpbOTkyLC0xXSwiaWF0IjoxNzY3ODA5NzU1LCJpc3MiOiJodHRwczovL2V2b2x2aW5nLW1hbGxhcmQtODUuY2xlcmsuYWNjb3VudHMuZGV2IiwibmJmIjoxNzY3ODA5NzQ1LCJyb2xlIjoiYXV0aGVudGljYXRlZCIsInNpZCI6InNlc3NfMzd1UHhvN1BGWFlMRDdrRlA1c1haVkVzQjhpIiwic3RzIjoiYWN0aXZlIiwic3ViIjoidXNlcl8zN3VQeG5tcGJuUUtVRzgxWWt0ZnFkVmJORDMiLCJ2IjoyfQ.rzRZdG7tU02ZrkxpWMfdwmGk8UJIlE7J2vvT6GjgvRBsQWqQ6Lwfq5HSGUCniXLh2w0W6jlf_RHMKsCV3zm629IfVNKUc6Xbtmf-3kpc8CL_LuNmf4MYtFVgnhcUVpIlawD2Wsohq9yJyo55H4sVRYHFCJ-zzMdQclfpgSAcTlMKoA3l7JkgVGUzUZZtlS3UXgqaVeZrDGdCwdNX_V7JLZonPdFbJRa6tnEXdXmgaQtMhfZvfqr4zelqCxZAtZBJK4kV-YsOCxAUU_7RpJOEsnlIQq5i3dEjpS7PedkJte44Wo9IbDuq4FPiMZ406GR-Tmi9VpEgeXRAr5day6e_pA; __session_oNnsQakD=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zNkozejRqN1BNMDRnbEk1MVFDUHBIekhIZTEiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2NzgwOTgxNSwiZnZhIjpbOTkyLC0xXSwiaWF0IjoxNzY3ODA5NzU1LCJpc3MiOiJodHRwczovL2V2b2x2aW5nLW1hbGxhcmQtODUuY2xlcmsuYWNjb3VudHMuZGV2IiwibmJmIjoxNzY3ODA5NzQ1LCJyb2xlIjoiYXV0aGVudGljYXRlZCIsInNpZCI6InNlc3NfMzd1UHhvN1BGWFlMRDdrRlA1c1haVkVzQjhpIiwic3RzIjoiYWN0aXZlIiwic3ViIjoidXNlcl8zN3VQeG5tcGJuUUtVRzgxWWt0ZnFkVmJORDMiLCJ2IjoyfQ.rzRZdG7tU02ZrkxpWMfdwmGk8UJIlE7J2vvT6GjgvRBsQWqQ6Lwfq5HSGUCniXLh2w0W6jlf_RHMKsCV3zm629IfVNKUc6Xbtmf-3kpc8CL_LuNmf4MYtFVgnhcUVpIlawD2Wsohq9yJyo55H4sVRYHFCJ-zzMdQclfpgSAcTlMKoA3l7JkgVGUzUZZtlS3UXgqaVeZrDGdCwdNX_V7JLZonPdFbJRa6tnEXdXmgaQtMhfZvfqr4zelqCxZAtZBJK4kV-YsOCxAUU_7RpJOEsnlIQq5i3dEjpS7PedkJte44Wo9IbDuq4FPiMZ406GR-Tmi9VpEgeXRAr5day6e_pA; _ga_QQHYFR2Z45=GS2.1.s1767809356$o7$g1$t1767809754$j60$l0$h0"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": COOKIE_STR,
    "Referer": "https://www.tiktokcopilot.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin"
}


def fetch_trending_products(timeframe="7d", sort_by="revenue", feed_type="for-you", limit=24, page=0, region="US"):
    """
    Fetch trending products from TikTokCopilot API.
    
    Args:
        timeframe: 1d, 3d, 7d, 14d, 30d
        sort_by: revenue, views, etc
        feed_type: for-you, etc
        limit: products per page (max ~24)
        page: page number for pagination
        region: US, etc
    
    Returns:
        dict with product data or None on error
    """
    params = {
        "timeframe": timeframe,
        "sortBy": sort_by,
        "feedType": feed_type,
        "limit": limit,
        "page": page,
        "region": region,
        "sAggMode": "net"
    }
    
    try:
        res = requests.get(API_URL, headers=HEADERS, params=params, timeout=30)
        print(f"Status: {res.status_code}")
        
        if res.status_code == 200:
            data = res.json()
            # Save for inspection
            with open('copilot_trending.json', 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"Saved {len(str(data))} bytes to copilot_trending.json")
            return data
        else:
            print(f"Error: {res.status_code}")
            print(res.text[:500])
            return None
    except Exception as e:
        print(f"Exception: {e}")
        return None


if __name__ == "__main__":
    print("Fetching TikTokCopilot Trending Products...")
    data = fetch_trending_products(timeframe="7d", limit=50)
    
    if data:
        # Try to print first product summary
        if isinstance(data, list) and len(data) > 0:
            print(f"\n--- First Product ---")
            print(json.dumps(data[0], indent=2)[:1000])
        elif isinstance(data, dict):
            print(f"\n--- Response Keys ---")
            print(data.keys())
            if 'data' in data:
                print(f"Data items: {len(data.get('data', []))}")
