"""Read-only eBay Search adapter. No eBay OAuth or purchase operations."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import json
from modules.listing_store import ListingStore
import re
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request


def normalize_results(payload, mode):
    if not isinstance(payload, dict) or not isinstance(payload.get('search_results'), list):
        raise ValueError('Scrapingdog returned no valid search_results array')
    items = []
    for row in payload['search_results']:
        if not isinstance(row, dict):
            raise ValueError('Invalid Scrapingdog listing')
        identifier, title = str(row.get('itemId', '')), row.get('title')
        # Search pages can contain promoted links to other marketplaces.
        # They are archived, but are not eBay listings and have no item ID.
        if not identifier.isdigit() and isinstance(row.get('link'), str):
            try:
                linked = urlsplit(row['link'])
                if linked.scheme in ('http', 'https') and linked.hostname and not (
                        linked.hostname == 'ebay.com' or linked.hostname.endswith('.ebay.com')):
                    continue
            except ValueError:
                pass
        if not identifier.isdigit() or not isinstance(title, str) or not title.strip():
            raise ValueError('Scrapingdog listing missing item ID or title')
        title = title.removesuffix('Opens in a new window or tab').strip()
        item = {'itemId': identifier, 'title': title, 'source': 'ebay',
                'itemWebUrl': f'https://www.ebay.com/itm/{identifier}',
                'buyingOptions': [mode], 'provider': 'scrapingdog',
                'listingStatus': 'active', 'rawListing': row}
        thumbnail = row.get('thumbnail')
        if isinstance(thumbnail, str):
            try:
                parsed_url = urlsplit(thumbnail)
                if parsed_url.scheme == 'https' and (parsed_url.hostname == 'ebayimg.com' or (parsed_url.hostname or '').endswith('.ebayimg.com')):
                    item['imageUrl'] = thumbnail
            except ValueError:
                pass
        # A filtered BIN search establishes buying mode even when the parser's
        # buying_format is blank. Do not use its unreliable is_sponsored field.
        # Only a single explicit USD amount can qualify; ranges/foreign prices
        # remain unknown rather than turning an extracted minimum into a deal.
        price_text = row.get('price', '')
        match = re.fullmatch(r'(?:US\s*)?\$\s*([\d,]+(?:\.\d{1,2})?)', str(price_text).strip())
        if match:
            try:
                amount = Decimal(match.group(1).replace(',', ''))
                if amount.is_finite() and amount > 0:
                    item['price' if mode == 'FIXED_PRICE' else 'currentBidPrice'] = {
                        'value': str(amount), 'currency': 'USD'}
            except InvalidOperation:
                pass
        # Search usually omits end times. Never invent them from listing age,
        # or allow an auction bid to masquerade as a fixed purchase price.
        for field in ('itemEndDate', 'itemCreationDate'):
            value = row.get(field)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    if parsed.tzinfo is not None:
                        item[field] = parsed.astimezone(timezone.utc).isoformat()
                except ValueError:
                    pass
        # Explicit provider status only. items_sold can refer to some units of
        # an active multi-quantity listing and is never evidence it ended.
        if row.get('status') in ('sold', 'ended'):
            item['listingStatus'] = row['status']
        sold_text = row.get('sold_date')
        if sold_text:
            item['soldDateText'] = str(sold_text)
            # Preserve date-only precision; never substitute polling time.
            for fmt in ('%b %d, %Y', '%B %d, %Y', '%Y-%m-%d'):
                try:
                    item['soldDate'] = datetime.strptime(str(sold_text).removeprefix('Sold '), fmt).date().isoformat()
                    item['soldDatePrecision'] = 'day'
                    break
                except ValueError:
                    pass
        items.append(item)
    return items


class ScrapingdogClient:
    def __init__(self, config, transport=None, clock=time.monotonic, store=None):
        from pokefire import build_query, request_json
        self.config = config
        self.transport = transport or request_json
        self.clock = clock
        self.key = os.environ.get('SCRAPINGDOG_KEY')
        if not self.key:
            raise ValueError('Set SCRAPINGDOG_KEY in .env')
        if config.get('environment', 'production') != 'production' or config.get('marketplace', 'EBAY_US') != 'EBAY_US':
            raise ValueError('Scrapingdog supports production EBAY_US searches only')
        self.query = build_query(config)
        self.page_size = next(size for size in (60, 120, 240) if size >= config['page_size'])
        self.auction_due = {}
        self.store = store or ListingStore(config["state_file"])

    def search_page(self, offset, mode):
        page = offset // self.config['page_size'] + 1
        target = 'https://www.ebay.com/sch/i.html?' + urlencode({
            '_nkw': self.query, '_sacat': self.config.get('category_id', '183454'),
            '_sop': '10' if mode == 'FIXED_PRICE' else '1',
            '_ipg': self.page_size, '_pgn': page,
            'LH_BIN' if mode == 'FIXED_PRICE' else 'LH_Auction': '1'})
        url = 'https://api.scrapingdog.com/ebay/search?' + urlencode({'api_key': self.key, 'url': target})
        try:
            payload = self.transport(Request(url))
        except Exception as exc:
            # Provider URLs contain the key. Do not propagate URL-bearing errors.
            from pokefire import ApiError
            if isinstance(exc, ApiError):
                raise
            if isinstance(exc, (OSError, ValueError)):
                raise ValueError('Scrapingdog search request failed') from None
            raise
        # Archive every received JSON body before interpreting it. A malformed
        # provider response remains available for diagnosis and offline replay.
        clean_payload = json.loads(json.dumps(payload).replace(self.key, '[REDACTED]'))
        poll_id, observed_at = self.store.archive(target, mode, clean_payload)
        try:
            items = normalize_results(clean_payload, mode)
        except ValueError:
            self.store.parse_failed(poll_id)
            raise
        from pokefire import matching_names
        for item in items:
            item['watchlistMatches'] = matching_names(item, self.config)
        self.store.record(poll_id, observed_at, items)
        return {'itemSummaries': items, 'next': len(payload['search_results']) >= self.page_size}

    def search(self, offset):
        fixed = self.search_page(offset, 'FIXED_PRICE')
        # Keep the slower auction scan within the shared credit budget. Expired
        # cached bids are never returned on the intervening fixed-price polls.
        if self.clock() >= self.auction_due.get(offset, 0):
            auctions = self.search_page(offset, 'AUCTION')
            self.auction_due[offset] = self.clock() + self.config.get('auction_poll_seconds', 900)
            fixed['itemSummaries'].extend(auctions['itemSummaries'])
            fixed['next'] = fixed['next'] or auctions['next']
        return fixed
