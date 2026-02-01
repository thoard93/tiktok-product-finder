"""Test V2 endpoint with fresh cookies"""
import requests

url = "https://www.tiktokcopilot.com/api/trending/products"
params = {
    "timeframe": "all",
    "sortBy": "revenue",
    "limit": 2,
    "page": 0,
    "region": "US"
}

headers = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Cookie": "__session=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zN3gwZTJ0SXEweFhGRHpabVUzYUY2YzlmREsiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2OTk4NTkyNCwiZnZhIjpbMTIyNSwtMV0sImlhdCI6MTc2OTk4NTg2NCwiaXNzIjoiaHR0cHM6Ly9jbGVyay50aWt0b2tjb3BpbG90LmNvbSIsIm5iZiI6MTc2OTk4NTg1NCwic2lkIjoic2Vzc18zOTM2SEVJT0FTdUhwUzBtSE1vd1lOOVpVYWoiLCJzdHMiOiJhY3RpdmUiLCJzdWIiOiJ1c2VyXzM3eDRMNDNWQnpSS0lBbmwzT3ZZY0ZiTVVTbSIsInYiOjJ9.NOUxbFhmtlS2K__qGJmRRRC0_-Rc_dnvRoovnf99fCfJy5IAKOXKvRC-NnZu_5CS3PKJeoWViuQPvpsKt7ndHsS1A5iYGOj6C_D1XUa5inK3Rkskth4n_bDjz-p7Fr-r4vdG56VWYaL0XZR52jXezOo9L6l2F5Yxv1kcrzhHb_cyiwETP8eeAgOG1bD70QiX1OXD0UAX05YeSetrd5Vsgpz6Els-eun475SMplSIMgCM62KT3LUmF89dqEaYOVH9ZVp26z4bEyeVqQL9_3wQME9D98TKfvYp2nM9NLDemUtEKvkSjm0ju2YjhaWG1ur8sJVp5wrCYBX6_7C37afJ1Q"
}

print("Testing V2 endpoint with fresh session cookie...")
try:
    res = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"Status: {res.status_code}")
    
    if res.status_code == 200:
        try:
            data = res.json()
            products = data.get('products', [])
            print(f"Got {len(products)} products")
            
            if products:
                p = products[0]
                print("\n=== FIRST PRODUCT FIELDS ===")
                for key, value in p.items():
                    if 'video' in key.lower() or 'count' in key.lower() or 'creator' in key.lower():
                        print(f"  {key}: {value}")
                
                print("\n=== ALL KEYS ===")
                print(list(p.keys()))
        except Exception as e:
            print(f"JSON Error: {e}")
            print(f"Response: {res.text[:500]}")
    else:
        print(f"Response: {res.text[:500]}")
except Exception as e:
    print(f"Request Error: {e}")
