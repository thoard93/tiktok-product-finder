
import requests

URL = "https://www.tiktok.com/t/ZTHwgwbUL5uL7-oXGV7/"

def test_extract(input_str):
    print(f"Testing URL: {input_str}")
    
    try:
        print("\nAttempting GET request with Headers...")
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        # replicate app.py logic
        response = requests.get(input_str, allow_redirects=True, timeout=15, headers=headers, stream=True)
        print(f"GET Status: {response.status_code}")
        print(f"Resolved URL: {response.url}")
        response.close()
    except Exception as e:
        print(f"GET Failed: {e}")

if __name__ == "__main__":
    test_extract(URL)
