"""Test LEGACY endpoint with fresh cookies - dump ALL fields"""
import requests
import json

url = "https://www.tiktokcopilot.com/api/trending"
params = {
    "timeframe": "all",  # ALL-TIME
    "sortBy": "revenue",
    "limit": 2,
    "page": 0,
    "region": "US",
    "feedType": "for-you",
    "sAggMode": "net"
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

print("Testing LEGACY endpoint /api/trending with timeframe=all...")
try:
    res = requests.get(url, headers=headers, params=params, timeout=30)
    print(f"Status: {res.status_code}")
    
    if res.status_code == 200:
        try:
            data = res.json()
            # Legacy uses 'videos' not 'products'
            products = data.get('videos', []) or data.get('products', [])
            print(f"Got {len(products)} products/videos")
            
            if products:
                p = products[0]
                print("\n=== FULL FIRST PRODUCT ===")
                print(json.dumps(p, indent=2))
                
                print("\n=== VIDEO/COUNT RELATED FIELDS ===")
                for key, value in p.items():
                    k_lower = key.lower()
                    if 'video' in k_lower or 'count' in k_lower or 'creator' in k_lower or 'total' in k_lower or 'all' in k_lower:
                        print(f"  {key}: {value}")
        except Exception as e:
            print(f"JSON Error: {e}")
            print(f"Response: {res.text[:500]}")
    else:
        print(f"Error Response: {res.text[:500]}")
except Exception as e:
    print(f"Request Error: {e}")
