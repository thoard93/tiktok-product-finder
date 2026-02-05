"""Test BrightData proxy credentials locally"""
import requests

proxy_url = "http://brd-customer-hl_ccfe19a-zone-residential-country-us:cgg791a7Ax68@brd.superproxy.io:22225"

proxies = {
    "http": proxy_url,
    "https": proxy_url
}

print("Testing BrightData proxy...")
print(f"Proxy: {proxy_url[:50]}...")

try:
    response = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=30)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
    print("✅ Proxy works!" if response.status_code == 200 else "❌ Proxy failed")
except Exception as e:
    print(f"❌ Error: {e}")
