
import requests
import re
import time

def resolve_tiktok_share_link(url):
    print(f"Resolving: {url}")
    try:
        # User-Agent is sometimes key for TikTok
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.head(url, allow_redirects=True, timeout=10, headers=headers)
        print(f"HEAD Final URL: {response.url}")
        
        # If HEAD doesn't change it, try GET
        if response.url == url or 'tiktok.com/t/' in response.url:
            response = requests.get(url, allow_redirects=True, timeout=10, headers=headers)
            print(f"GET Final URL: {response.url}")
        
        return response.url
    except Exception as e:
        print(f"Error: {e}")
        return None

def extract_product_id(text):
    print(f"Extracting ID from: {text}")
    patterns = [
        r'shop/pdp/(\d+)',
        r'product/(\d+)',
        r'product_id=(\d+)',
        r'/(\d{15,25})(?:[/?]|$)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
            
    return None

target_url = "https://www.tiktok.com/t/ZTHwTDUd3VMBW-NnivT/"
resolved = resolve_tiktok_share_link(target_url)

if resolved:
    pid = extract_product_id(resolved)
    print(f"Extracted ID: {pid}")
    
    # Check if this ID matches the one in the user screenshot: 1729387845486022706
    user_reported_id = "1729387845486022706"
    if pid == user_reported_id:
        print("MATCHES user reported ID. The extraction logic works as expected (mechanically).")
        print("Now the question is: Is this the WRONG product?")
    else:
        print(f"MISMATCH! User saw {user_reported_id}, but we extracted {pid}")
else:
    print("Failed to resolve URL")
