from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from pokefire.listing_store import ListingStore
from pokefire.scrapingdog import ScrapingdogClient, normalize_results
from pokefire.monitor import ApiError, State, load_config, poll


def row(identifier='123', **kwargs):
    return {'itemId': identifier, 'title': 'Charizard Base Set PSA 10Opens in a new window or tab',
            'price': '$100.00', 'buying_format': '', **kwargs}


class ScrapingdogTests(unittest.TestCase):
    def test_external_promotion_does_not_discard_valid_ebay_results(self):
        promotion = {'title': 'Promoted card', 'link': 'https://goldin.co/sn/123'}
        items = normalize_results({'search_results': [row(), promotion]}, 'FIXED_PRICE')
        self.assertEqual([item['itemId'] for item in items], ['123'])
        with self.assertRaises(ValueError):
            normalize_results({'search_results': [{'title': 'Missing ID',
                'link': 'https://www.ebay.com/itm/invalid'}]}, 'FIXED_PRICE')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = load_config('fixtures/watchlist.example.json')
        self.config.update(state_file=str(Path(self.temp.name)/'state.sqlite3'),page_size=240)
        self.store = ListingStore(self.config['state_file'])
        self.key = patch.dict(os.environ, {'SCRAPINGDOG_KEY': 'secret-test-key'})
        self.key.start(); self.addCleanup(self.key.stop)

    def test_poll_cadence_parameters_and_no_extra_lifecycle_requests(self):
        requests = []
        now = [1000]
        def transport(request):
            requests.append(request)
            return {'search_results':[row()]}
        client = ScrapingdogClient(self.config, transport, clock=lambda:now[0])
        client.search(0)
        self.assertEqual(len(requests),2)
        now[0] += 120
        client.search(0)
        self.assertEqual(len(requests),3)
        now[0] += 900
        client.search(0)
        self.assertEqual(len(requests),5)
        targets = [parse_qs(parse_qs(urlparse(r.full_url).query)['url'][0].split('?',1)[1]) for r in requests]
        self.assertEqual(targets[0]['_sop'],['10'])
        self.assertEqual(targets[0]['LH_BIN'],['1'])
        self.assertEqual(targets[1]['_sop'],['1'])
        self.assertEqual(targets[1]['LH_Auction'],['1'])
        self.assertTrue(all(t['_ipg']==['240'] for t in targets))
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM scraper_polls').fetchone()[0],5)
            raw=db.execute('SELECT raw_json_gzip,target_url FROM scraper_polls LIMIT 1').fetchone()
            self.assertEqual(json.loads(gzip.decompress(raw[0]))['search_results'][0]['itemId'],'123')
            self.assertNotIn('secret-test-key',raw[1])
            self.assertEqual(db.execute('SELECT count(*) FROM listing_snapshots').fetchone()[0],5)
        self.store.history('123')
        self.assertEqual(len(requests),5)

    def test_failed_parse_is_archived_but_never_changes_listing_state(self):
        client=ScrapingdogClient(self.config,lambda _: {'error':'provider error secret-test-key'})
        with self.assertRaises(ValueError):client.search(0)
        self.assertEqual(self.store.history(),[])
        with closing(self.store.connect()) as db:
            data,error=db.execute('SELECT raw_json_gzip,parse_error FROM scraper_polls').fetchone()
            self.assertIsNotNone(error)
            self.assertNotIn(b'secret-test-key',gzip.decompress(data))

    def test_missing_listing_is_not_sold_and_explicit_status_preserves_dates(self):
        def save(rows):
            payload={'search_results':rows}
            pid,at=self.store.archive('https://www.ebay.com/sch/i.html','FIXED_PRICE',payload)
            self.store.record(pid,at,normalize_results(payload,'FIXED_PRICE'))
        save([row(itemCreationDate='2026-09-01T12:00:00Z')])
        save([])
        active=self.store.history('123')[0]
        self.assertEqual(active['status'],'active')
        self.assertIsNone(active['sold_at'])
        self.assertEqual(len(active['events']),1)
        save([row(status='sold',sold_date='Sep 16, 2026')])
        sold=self.store.history('123')[0]
        self.assertEqual(sold['status'],'sold')
        self.assertEqual(sold['sold_at'],'2026-09-16')
        self.assertEqual(sold['sold_at_precision'],'day')
        self.assertEqual(sold['listed_at'],'2026-09-01T12:00:00+00:00')
        self.assertEqual(sold['first_seen_at'],active['first_seen_at'])
        self.assertEqual(sold['events'][-1]['previous_status'],'active')
        save([row(status='sold',sold_date='Sep 16, 2026')])
        self.assertEqual(len(self.store.history('123')[0]['events']),2)

    def test_multi_quantity_and_unknown_dates(self):
        item=normalize_results({'search_results':[row(items_sold='12 sold',sold_date=None)]},'FIXED_PRICE')[0]
        self.assertEqual(item['listingStatus'],'active')
        self.assertNotIn('itemCreationDate',item)
        self.assertNotIn('soldDate',item)
        self.assertEqual(item['rawListing']['items_sold'],'12 sold')

    def test_no_auction_bid_as_fixed_price_or_guessed_currency(self):
        auction=normalize_results({'search_results':[row()]},'AUCTION')[0]
        self.assertNotIn('price',auction)
        self.assertNotIn('itemEndDate',auction)
        self.assertEqual(auction['currentBidPrice']['currency'],'USD')
        for price in ('AU $100.00','C $100.00','£100.00','$10.00 to $100.00','',None,'NaN'):
            self.assertNotIn('price',normalize_results({'search_results':[row(price=price)]},'FIXED_PRICE')[0])
        self.assertEqual(normalize_results({'search_results':[row(price='US $1,200.99')]},'FIXED_PRICE')[0]['price']['value'],'1200.99')

    def test_pagination_and_error_redaction(self):
        urls=[]
        def transport(req):
            urls.append(req.full_url)
            return {'search_results':[row(str(n)) for n in range(240)]}
        client=ScrapingdogClient(self.config,transport)
        self.assertTrue(client.search_page(240,'FIXED_PRICE')['next'])
        target=parse_qs(urlparse(urls[0]).query)['url'][0]
        self.assertEqual(parse_qs(urlparse(target).query)['_pgn'],['2'])
        def failure(req):raise ValueError(req.full_url)
        client.transport=failure
        with self.assertRaises(ValueError) as caught:client.search(0)
        self.assertNotIn('secret-test-key',str(caught.exception))

    def test_sold_listing_cannot_trigger_new_listing_alert(self):
        client=ScrapingdogClient(self.config,lambda _: {'search_results':[row(status='sold')]})
        state=State(self.config);self.addCleanup(state.db.close)
        self.config['notify_existing']=True
        sent=[]
        poll(client,self.config,state,lambda *args:sent.append(args))
        self.assertEqual(sent,[])
        self.assertEqual(self.store.history('123')[0]['status'],'sold')

    def test_listing_with_both_buying_modes_keeps_fixed_price(self):
        from pokefire.monitor import fetch_items
        client=ScrapingdogClient(self.config,lambda _: {'search_results':[row()]})
        items,_=fetch_items(client,self.config)
        self.assertEqual(set(items['123']['buyingOptions']),{'FIXED_PRICE','AUCTION'})
        self.assertEqual(items['123']['price']['value'],'100.00')
        self.assertEqual(items['123']['currentBidPrice']['value'],'100.00')

    def test_thumbnail_is_saved_with_matching_listing_and_alert(self):
        from pokefire.monitor import alert_payload
        url='https://i.ebayimg.com/images/g/example/s-l500.webp'
        client=ScrapingdogClient(self.config,lambda _: {'search_results':[row(thumbnail=url)]})
        result=client.search_page(0,'FIXED_PRICE')['itemSummaries'][0]
        self.assertEqual(result['imageUrl'],url)
        self.assertEqual(self.store.history('123')[0]['payload']['imageUrl'],url)
        self.assertEqual(alert_payload(result,['Charizard'])['imageUrl'],url)
        for bad in ['javascript:alert(1)','https://ebayimg.com.evil.test/img.png','http://i.ebayimg.com/x']:
            self.assertNotIn('imageUrl',normalize_results({'search_results':[row(thumbnail=bad)]},'FIXED_PRICE')[0])

    def test_unmatched_rows_archived_but_excluded_from_history(self):
        client=ScrapingdogClient(self.config,lambda _: {'search_results':[row(title='Unrelated modern card PSA 10')]})
        client.search_page(0,'FIXED_PRICE')
        self.assertEqual(self.store.history(),[])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM listing_snapshots').fetchone()[0],1)

    def test_retired_source_rejected(self):
        with self.assertRaisesRegex(ValueError,'Only eBay'):
            load_config('fixtures/watchlist.example.json','cardtrader')

if __name__=='__main__':unittest.main()

class ListingHistoryTests(unittest.TestCase):
    def test_card_filters_pagination_and_links_cover_all_saved_listings(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ListingStore(Path(directory)/'history.sqlite3')
            rows=[row(str(i),title='Rayquaza Gold Star PSA 10') for i in range(121)]
            items=normalize_results({'search_results':rows},'FIXED_PRICE')
            for item in items:item['watchlistMatches']=['Rayquaza Gold Star']
            pid,at=store.archive('https://www.ebay.com/sch/i.html','FIXED_PRICE',{'search_results':rows})
            store.record(pid,at,items)
            pages=[store.history_page(query='rayquaza gold',card='Rayquaza Gold Star',page=n) for n in (1,2,3)]
            self.assertEqual([len(p['rows']) for p in pages],[50,50,21])
            all_rows=[r for p in pages for r in p['rows']]
            self.assertEqual(len({r['item_id'] for r in all_rows}),121)
            self.assertTrue(all(r['url']==f"https://www.ebay.com/itm/{r['item_id']}" for r in all_rows))
            self.assertEqual(store.history_page(query='Charizard')['total'],0)
            self.assertEqual(store.history_page(card='Different card')['total'],0)
            self.assertEqual(store.history_page(item_id='3')['rows'][0]['payload']['price']['value'],'100.00')
