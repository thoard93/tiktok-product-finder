"""
Price Blade — eBay Market Research Service
Scrapes eBay sold/completed listings to analyze pricing and competition.
Falls back to HTML scraping if no EBAY_APP_ID is set.
"""

import os
import re
import json
import logging
import statistics
from datetime import datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

EBAY_APP_ID = os.environ.get('EBAY_APP_ID', '')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}


def _parse_price(text):
    """Extract a float price from text like '$24.99' or '$12.50 to $18.00'."""
    if not text:
        return None
    # Take first price in range
    match = re.search(r'\$?([\d,]+\.?\d*)', text.replace(',', ''))
    if match:
        try:
            return float(match.group(1))
        except (ValueError, TypeError):
            pass
    return None


def _scrape_ebay_listings(keywords, sold=True, limit=50):
    """Scrape eBay search results. If sold=True, scrapes completed/sold items."""
    encoded = quote_plus(keywords)
    params = f"_nkw={encoded}&_ipg={min(limit, 100)}"
    if sold:
        params += "&LH_Sold=1&LH_Complete=1"

    url = f"https://www.ebay.com/sch/i.html?{params}"
    log.info("[PriceBlade] Scraping: %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            log.warning("[PriceBlade] eBay returned %d", resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        items = []

        for el in soup.select('.s-item'):
            title_el = el.select_one('.s-item__title')
            price_el = el.select_one('.s-item__price')
            link_el = el.select_one('.s-item__link')
            date_el = el.select_one('.s-item__ended-date, .s-item__endedDate, .POSITIVE')

            title = title_el.get_text(strip=True) if title_el else ''
            if not title or title.lower() == 'shop on ebay':
                continue

            price = _parse_price(price_el.get_text(strip=True) if price_el else '')
            if price is None or price <= 0:
                continue

            items.append({
                'title': title[:200],
                'price': price,
                'url': link_el['href'] if link_el and link_el.get('href') else '',
                'date': date_el.get_text(strip=True) if date_el else '',
                'sold': sold,
            })

        log.info("[PriceBlade] Found %d %s items for '%s'",
                 len(items), 'sold' if sold else 'active', keywords)
        return items

    except Exception as exc:
        log.warning("[PriceBlade] Scrape failed: %s", exc)
        return []


def analyze_ebay_product(keywords):
    """
    Full eBay market analysis for a product.

    Returns dict with pricing data, competition, and recommendations.
    """
    # Get sold listings
    sold_items = _scrape_ebay_listings(keywords, sold=True, limit=60)
    # Get active listings
    active_items = _scrape_ebay_listings(keywords, sold=False, limit=60)

    sold_prices = [i['price'] for i in sold_items if i['price'] > 0]
    active_prices = [i['price'] for i in active_items if i['price'] > 0]

    sold_count = len(sold_prices)
    active_count = len(active_prices)

    # Calculate metrics
    avg_sold = statistics.mean(sold_prices) if sold_prices else 0
    median_sold = statistics.median(sold_prices) if sold_prices else 0
    highest_sold = max(sold_prices) if sold_prices else 0
    lowest_sold = min(sold_prices) if sold_prices else 0
    lowest_bin = min(active_prices) if active_prices else 0

    # Sell-through rate
    total = sold_count + active_count
    sell_through = (sold_count / total * 100) if total > 0 else 0

    # Recommended price: median * 0.95 as starting point
    recommended = round(median_sold * 0.95, 2) if median_sold > 0 else 0

    # Competition level
    if active_count > 200:
        competition = 'High'
    elif active_count > 50:
        competition = 'Medium'
    else:
        competition = 'Low'

    # Price distribution (5 buckets)
    distribution = []
    if sold_prices:
        price_min = min(sold_prices)
        price_max = max(sold_prices)
        bucket_size = (price_max - price_min) / 5 if price_max > price_min else 1
        for i in range(5):
            low = price_min + i * bucket_size
            high = low + bucket_size
            count = len([p for p in sold_prices if low <= p < high])
            label = f"${low:.0f}-${high:.0f}"
            distribution.append({'label': label, 'count': count})

    return {
        'keywords': keywords,
        'avg_sold_price': round(avg_sold, 2),
        'median_sold_price': round(median_sold, 2),
        'lowest_bin': round(lowest_bin, 2),
        'highest_sold': round(highest_sold, 2),
        'lowest_sold': round(lowest_sold, 2),
        'sell_through_rate': round(sell_through, 1),
        'active_listings': active_count,
        'sold_last_30': sold_count,
        'recommended_price': recommended,
        'price_distribution': distribution,
        'recent_sold': sold_items[:10],
        'competition_level': competition,
    }
