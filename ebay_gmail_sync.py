"""
eBay Gmail Sales Sync — Auto-detect eBay sales from Gmail notifications.
Searches for "Your item has sold" emails from eBay, parses sale details,
and marks matching listings as sold with auto-calculated fees/profit.

Requires: google-api-python-client google-auth-oauthlib
Env vars: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timedelta
from html.parser import HTMLParser

log = logging.getLogger('ebay_gmail')

# Gmail API scopes — read-only access to emails
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


class EbayEmailParser(HTMLParser):
    """Parse eBay 'Your item has sold!' email HTML to extract sale data."""

    def __init__(self):
        super().__init__()
        self._data = []
        self._in_td = False
        self.results = {
            'item_title': '',
            'sale_price': 0,
            'shipping': 0,
            'order_number': '',
            'date_sold': '',
            'buyer': '',
        }

    def handle_starttag(self, tag, attrs):
        if tag == 'td':
            self._in_td = True

    def handle_endtag(self, tag):
        if tag == 'td':
            self._in_td = False

    def handle_data(self, data):
        if self._in_td:
            self._data.append(data.strip())


def parse_ebay_sale_email(html_body):
    """
    Parse an eBay "Your item has sold!" email body.
    Returns dict with: item_title, sale_price, shipping, order_number, date_sold, buyer
    """
    result = {
        'item_title': '',
        'sale_price': 0.0,
        'shipping_charged': 0.0,
        'order_number': '',
        'date_sold': '',
        'buyer': '',
    }

    if not html_body:
        return result

    # Extract text chunks from HTML
    text = re.sub(r'<[^>]+>', ' ', html_body)
    text = re.sub(r'\s+', ' ', text)

    # Item title — from email subject or the linked text before "Sold:"
    title_match = re.search(r'You made the sale for (.+?)(?:\s+Inbox|\s*$)', text)
    if not title_match:
        # Try to find the title near "Sold:" pattern
        title_match = re.search(r'([A-Z][^$]+?)\s+Sold:\s+\$', text)
    if title_match:
        result['item_title'] = title_match.group(1).strip()

    # Sale price — "Sold: $XX.XX" pattern
    price_match = re.search(r'Sold:\s*\$?([\d,]+\.?\d*)', text)
    if price_match:
        result['sale_price'] = float(price_match.group(1).replace(',', ''))

    # Shipping — "Shipping: $XX.XX" pattern
    ship_match = re.search(r'Shipping:\s*\$?([\d,]+\.?\d*)', text)
    if ship_match:
        result['shipping_charged'] = float(ship_match.group(1).replace(',', ''))

    # Order number — "Order: XX-XXXXX-XXXXX" pattern
    order_match = re.search(r'Order:\s*([\d\-]+)', text)
    if order_match:
        result['order_number'] = order_match.group(1)

    # Date sold — "Date sold: MMM DD, YYYY HH:MM" pattern
    date_match = re.search(r'Date sold:\s*(.+?)(?:\s+Buyer:|$)', text)
    if date_match:
        result['date_sold'] = date_match.group(1).strip()

    # Buyer — "Buyer: username" pattern
    buyer_match = re.search(r'Buyer:\s*(\S+)', text)
    if buyer_match:
        result['buyer'] = buyer_match.group(1)

    return result


def fuzzy_title_match(email_title, listing_title, threshold=0.6):
    """Simple fuzzy matching between email title and listing title."""
    if not email_title or not listing_title:
        return 0

    # Normalize both
    a = set(re.sub(r'[^\w\s]', '', email_title.lower()).split())
    b = set(re.sub(r'[^\w\s]', '', listing_title.lower()).split())

    if not a or not b:
        return 0

    # Jaccard similarity
    intersection = a & b
    union = a | b
    return len(intersection) / len(union)


def get_gmail_service(team_id, db_session=None):
    """
    Get authenticated Gmail API service for a team.
    Tokens are stored in the database (EbayTeam.gmail_tokens_json).
    """
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        log.error("google-api-python-client not installed. Run: pip install google-api-python-client google-auth-oauthlib")
        return None

    # Import here to avoid circular imports
    from ebay_lister import EbayTeam, db as ebay_db
    session = db_session or ebay_db.session

    team = session.query(EbayTeam).get(team_id) if hasattr(session, 'query') else EbayTeam.query.get(team_id)
    if not team or not team.gmail_tokens_json:
        return None

    try:
        token_data = json.loads(team.gmail_tokens_json)
        creds = Credentials(
            token=token_data.get('access_token'),
            refresh_token=token_data.get('refresh_token'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=os.environ.get('GMAIL_CLIENT_ID', ''),
            client_secret=os.environ.get('GMAIL_CLIENT_SECRET', ''),
            scopes=GMAIL_SCOPES,
        )

        # Check if token expired and refresh
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            # Save refreshed token
            token_data['access_token'] = creds.token
            team.gmail_tokens_json = json.dumps(token_data)
            session.commit()

        return build('gmail', 'v1', credentials=creds)
    except Exception as e:
        log.error(f"Gmail auth error for team {team_id}: {e}")
        return None


def sync_ebay_sales(team_id, days_back=7):
    """
    Scan Gmail for eBay sale notifications and auto-mark listings as sold.
    Returns dict with: synced (count), skipped, errors, details.
    """
    from ebay_lister import EbayListing, EbayTeam, db

    service = get_gmail_service(team_id)
    if not service:
        return {'error': 'Gmail not connected', 'synced': 0}

    # Search for eBay sale emails
    after_date = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y/%m/%d')
    query = f'from:ebay@ebay.com subject:"You made the sale" after:{after_date}'

    try:
        results = service.users().messages().list(userId='me', q=query, maxResults=50).execute()
        messages = results.get('messages', [])
    except Exception as e:
        log.error(f"Gmail search error: {e}")
        return {'error': str(e), 'synced': 0}

    if not messages:
        return {'synced': 0, 'message': 'No sale emails found', 'searched': query}

    # Get team's active listings for matching
    team = EbayTeam.query.get(team_id)
    active_listings = EbayListing.query.filter(
        EbayListing.team_id == team_id,
        EbayListing.status.in_(['active', 'draft'])
    ).all()

    synced = []
    skipped = []
    errors = []

    for msg_ref in messages:
        try:
            msg = service.users().messages().get(userId='me', id=msg_ref['id'], format='full').execute()

            # Get email body (HTML)
            html_body = ''
            payload = msg.get('payload', {})

            # Try multipart first
            parts = payload.get('parts', [])
            for part in parts:
                if part.get('mimeType') == 'text/html':
                    data = part.get('body', {}).get('data', '')
                    if data:
                        html_body = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
                        break

            # Fallback to direct body
            if not html_body:
                data = payload.get('body', {}).get('data', '')
                if data:
                    html_body = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')

            # Also check subject line for title
            subject = ''
            for header in payload.get('headers', []):
                if header['name'].lower() == 'subject':
                    subject = header['value']
                    break

            # Parse the email
            sale_data = parse_ebay_sale_email(html_body)

            # Use subject to extract title if parser didn't find it
            if not sale_data['item_title'] and subject:
                title_from_subject = re.sub(r'^You made the sale for\s+', '', subject)
                sale_data['item_title'] = title_from_subject.strip()

            if not sale_data['sale_price']:
                skipped.append({
                    'subject': subject,
                    'reason': 'Could not parse sale price'
                })
                continue

            # Check if this order was already processed
            existing = EbayListing.query.filter_by(
                team_id=team_id,
                ebay_listing_id=sale_data['order_number']
            ).first() if sale_data['order_number'] else None

            if existing and existing.status == 'sold':
                skipped.append({
                    'title': sale_data['item_title'],
                    'reason': 'Already processed',
                    'order': sale_data['order_number']
                })
                continue

            # Find matching listing by title
            best_match = None
            best_score = 0

            for listing in active_listings:
                score = fuzzy_title_match(sale_data['item_title'], listing.title)
                if score > best_score and score >= 0.5:
                    best_score = score
                    best_match = listing

            if not best_match:
                skipped.append({
                    'title': sale_data['item_title'],
                    'price': sale_data['sale_price'],
                    'reason': 'No matching listing found',
                    'order': sale_data['order_number']
                })
                continue

            # Calculate fees and profit
            sale_price = sale_data['sale_price']
            shipping_charged = sale_data['shipping_charged']
            ebay_fees = round((sale_price + shipping_charged) * 0.1325 + 0.30, 2)
            cost = best_match.cost_price or 0
            ad_spend = best_match.ad_spend or 0
            # Estimate actual shipping same as charged (user can adjust later)
            shipping_actual = shipping_charged
            net_profit = round(sale_price + shipping_charged - ebay_fees - shipping_actual - cost - ad_spend, 2)

            # Mark as sold
            best_match.status = 'sold'
            best_match.sold_at = datetime.utcnow()
            best_match.sale_price = sale_price
            best_match.shipping_actual = shipping_actual
            best_match.ebay_fees = ebay_fees
            best_match.net_profit = net_profit
            if sale_data['order_number']:
                best_match.ebay_listing_id = sale_data['order_number']

            synced.append({
                'listing_id': best_match.id,
                'title': best_match.title,
                'matched_to': sale_data['item_title'],
                'match_score': round(best_score, 2),
                'sale_price': sale_price,
                'shipping': shipping_charged,
                'ebay_fees': ebay_fees,
                'net_profit': net_profit,
                'order': sale_data['order_number'],
            })

            # Remove from active list so it's not matched again
            active_listings = [l for l in active_listings if l.id != best_match.id]

        except Exception as e:
            errors.append({'message_id': msg_ref['id'], 'error': str(e)})

    db.session.commit()

    log.info(f"Gmail sync for team {team_id}: {len(synced)} synced, {len(skipped)} skipped, {len(errors)} errors")

    return {
        'synced': len(synced),
        'skipped': len(skipped),
        'errors': len(errors),
        'details': synced,
        'skipped_details': skipped,
        'error_details': errors,
    }
