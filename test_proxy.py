"""Test BrightData proxy credentials locally"""
import requests

proxy_url = "http://brd-customer-hl_ccfbe19a-zone-testkey:cgg791a7ax68@brd.superproxy.io:33335"

proxies = {
    "http": proxy_url,
    "https": proxy_url
}

print("Testing BrightData proxy...")
print(f"Proxy: {proxy_url[:50]}...")

try:
    response = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=30, verify=False)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
    print("✅ Proxy works!" if response.status_code == 200 else "❌ Proxy failed")
except Exception as e:
    print(f"❌ Error: {e}")
