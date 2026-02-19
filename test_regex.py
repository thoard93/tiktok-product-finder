import re

html = """
<div class="s-item__info clearfix">
    <div class="s-item__title"><span role="heading">Built Peanut Butter Puff Cups 4ct</span></div>
    <span class="s-item__price">$4.99</span>
    <span class="s-item__shipping s-item__logisticsCost">+$5.00 shipping</span>
</div>
<div class="s-item__info clearfix">
    <div class="s-item__title"><span role="heading">Built Peanut Butter Puff Cups 12ct</span></div>
    <span class="s-item__price">$15.99</span>
    <span class="s-item__shipping s-item__logisticsCost">Free shipping</span>
</div>
<div class="s-item__info clearfix">
    <div class="s-item__title"><span>Another Built Bar 12 Pack</span></div>
    <span class="s-item__price">$19.99<span class="ITALIC"></span></span>
    <span class="s-item__shipping s-item__logisticsCost">+$3.50 shipping</span>
</div>
<div class="s-item__info clearfix">
    <div class="s-item__title"><span>Built Bar 12</span></div>
    <span class="s-item__price">$18.00<span class="ITALIC"></span></span>
    <span class="s-item__shipping s-item__logisticsCost">Free</span>
</div>
"""

def extract_prices(html, search_title):
    blocks = html.split('class="s-item__info clearfix"')[1:]
    
    # Identify target quantities in the search title
    target_quantities = re.findall(r'\b(\d+)\s*(?:ct|pack|pk|piece|pcs|oz|lbs|grams|g|count)\b', search_title, re.IGNORECASE)
    # Also just find standalone numbers like "12" if they are part of the title
    if not target_quantities:
        target_quantities = re.findall(r'\b(\d{1,3})\b', search_title)
        
    valid_prices = []
    
    for block in blocks:
        # Title
        title_match = re.search(r'class="s-item__title"[^>]*>(?:<span[^>]*>)?(.*?)(?:</span>|</div)', block)
        title = title_match.group(1).strip() if title_match else ""
        title = re.sub(r'<[^>]+>', '', title) # strip any nested tags
        
        # Base price
        price_match = re.search(r'class="s-item__price"[^>]*>.*?\$(\d{1,4}\.\d{2})', block)
        if not price_match:
            continue
        base_price = float(price_match.group(1))
        
        # Shipping
        shipping_match = re.search(r'class="s-item__shipping[^>]*>.*?\$(\d{1,4}\.\d{2})', block)
        shipping = float(shipping_match.group(1)) if shipping_match else 0.0
        
        total_price = base_price + shipping
        
        # Filter quantities
        # We look for explicit numbers in the item title
        item_quantities = re.findall(r'\b(\d+)\s*(?:ct|pack|pk|piece|pcs|oz|lbs|grams|g|count)?\b', title, re.IGNORECASE)
        skip = False
        
        if target_quantities and item_quantities:
            target_q_set = set([int(q) for q in target_quantities])
            item_q_set = set([int(q) for q in item_quantities])
            
            # If the search wanted specific quantities, and the item has NONE of those quantities, skip it.
            # E.g. search wanted 12. item only has 4. item_q_set intersection target_q_set is empty.
            if not target_q_set.intersection(item_q_set):
                print(f"Skipping '{title}' due to quantity mismatch: wanted {target_q_set}, got {item_q_set}")
                skip = True
                
        if not skip and 3.0 < total_price < 5000.0:
            print(f"Keeping '{title}': ${total_price:.2f}")
            valid_prices.append(total_price)
            
    return valid_prices

print("Prices:", extract_prices(html, "Built Peanut Butter Puff Cups 12ct 17g Protein"))
