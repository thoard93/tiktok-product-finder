import os
import requests
import json
from urllib.parse import urlencode

# CONFIG
PROXY_STR = os.environ.get('DAILYVIRALS_PROXY', '')
TOKEN = os.environ.get('DAILYVIRALS_TOKEN', '')
TARGET_URL = "https://backend.thedailyvirals.com/api/videos/stats/top-growth-by-date-range"

def test_request(name, url, proxies=None, headers=None):
    print(f"\n--- Testing {name} ---")
    try:
        res = requests.get(url, proxies=proxies, headers=headers, timeout=30)
        print(f"Status: {res.status_code}")
        print(f"Server: {res.headers.get('Server', 'Unknown')}")
        if res.status_code != 200:
            print(f"Body snippet: {res.text[:300]}")
        return res
    except Exception as e:
        print(f"Error: {e}")
        return None

if __name__ == "__main__":
    proxies = None
    if PROXY_STR and len(PROXY_STR.split(':')) == 4:
        host, port, user, pw = PROXY_STR.split(':')
        proxy_url = f"http://{user}:{pw}@{host}:{port}"
        proxies = {"http": proxy_url, "https": proxy_url}
        print(f"Loaded Proxy: {host}:{port}")
    else:
        print("No proxy loaded via DAILYVIRALS_PROXY")

    # 1. Test basic connectivity (Google)
    test_request("Google (Direct)", "https://www.google.com")
    if proxies:
        test_request("Google (via Proxy)", "https://www.google.com", proxies=proxies)

    # 2. Test DailyVirals
    if TOKEN:
        headers = {
            'accept': 'application/json, text/plain, */*',
            'authorization': f'Bearer {TOKEN}',
            'origin': 'https://www.thedailyvirals.com',
            'referer': 'https://www.thedailyvirals.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        params = {
            'startDate': '2025-12-23T00:00:00.000Z',
            'endDate': '2025-12-24T00:00:00.000Z',
            'page': '1',
            'limit': '1',
            'sortBy': 'growth'
        }
        full_url = f"{TARGET_URL}?{urlencode(params)}"
        
        test_request("DailyVirals (Direct)", full_url, headers=headers)
        if proxies:
            test_request("DailyVirals (via Proxy)", full_url, proxies=proxies, headers=headers)
    else:
        print("No DailyVirals token found.")
