import re
import requests
from urllib.parse import quote_plus

query = "Built Peanut Butter Puff Cups 12ct 17g Protein"
search_title = re.sub(r'\b(NEW|SEALED|NIB|NWT|AUTHENTIC|GENUINE|LOT OF|SET OF|BUNDLE|FREE SHIPPING)\b', '', query, flags=re.IGNORECASE).strip()
search_title = ' '.join(search_title.split()[:8])
encoded_query = quote_plus(search_title)

url = f'https://www.ebay.com/sch/i.html?_nkw={encoded_query}&LH_Complete=1&LH_Sold=1&_sop=13'
print("Fetching:", url)
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

resp = requests.get(url, headers=headers, timeout=12)
html = resp.text

# Try to extract <div class="s-item__info clearfix"> blocks
items = re.findall(r'<div class="s-item__info clearfix">.*?</div></div></div>', html, re.DOTALL)
print(f"Found {len(items)} item blocks")

if not items:
    with open('ebay_debug.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print("Saved HTML to ebay_debug.html")

for item in items[:20]:
    title_match = re.search(r'<div class="s-item__title"><span role="heading".*?>([^<]+)</span>', item)
    title = title_match.group(1) if title_match else "No title"
    if title == "No title":
        title_match = re.search(r'<div class="s-item__title"><span>([^<]+)</span>', item)
        title = title_match.group(1) if title_match else "No title"
    
    price_match = re.search(r'<span class="s-item__price">.*?\$(\d{1,4}\.\d{2})', item)
    price = float(price_match.group(1)) if price_match else 0.0
    
    shipping_text_match = re.search(r'<span class="s-item__shipping s-item__logisticsCost">([^<]+)</span>', item)
    shipping_text = shipping_text_match.group(1) if shipping_text_match else ""
    
    shipping_match = re.search(r'\$(\d{1,4}\.\d{2})', shipping_text)
    shipping = 0.0
    if shipping_match:
        shipping = float(shipping_match.group(1))
        
    print(f"Title: {title.strip()}\nPrice: ${price} + ${shipping} shipping (Total: ${price + shipping})\n")
