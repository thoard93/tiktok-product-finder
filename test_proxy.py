"""Test BrightData proxy credentials locally"""
import requests

proxy_url = "http://user-default-network-res-country-us:mWd3MoS4KuNq@proxy.proxiware.com:1337"

proxies = {
    "http": proxy_url,
    "https": proxy_url
}

print("Testing BrightData proxy...")
print(f"Proxy: {proxy_url[:50]}...")

try:
    # Test 1: httpbin (basic connectivity)
    print("Test 1: httpbin.org...")
    response = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=30, verify=False)
    print(f"  Status: {response.status_code} - {response.text.strip()}")
    
    # Test 2: tiktokcopilot.com (the actual target)
    print("\nTest 2: tiktokcopilot.com...")
    response2 = requests.get("https://www.tiktokcopilot.com/api/trending/products?timeframe=7d&limit=1", proxies=proxies, timeout=30, verify=False)
    print(f"  Status: {response2.status_code}")
    print(f"  Response: {response2.text[:300]}")
    
    if response2.status_code == 200:
        print("✅ BrightData works for tiktokcopilot!")
    else:
        print(f"❌ BrightData returned {response2.status_code} for tiktokcopilot")
except Exception as e:
    print(f"❌ Error: {e}")
