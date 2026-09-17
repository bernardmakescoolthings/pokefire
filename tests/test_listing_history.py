import tempfile
import unittest
from pathlib import Path
from modules.listing_store import ListingStore

class ListingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ListingStore(Path(self.tmp.name) / 'history.sqlite3')

    def save(self, identifier, value, status='active', card='Rayquaza', mode='FIXED_PRICE', end=None):
        item={'itemId':identifier,'title':card+' PSA 10','itemWebUrl':'https://www.ebay.com/itm/'+identifier,'watchlistMatches':[card],'listingStatus':status,'buyingOptions':[mode], 'currentBidPrice' if mode=='AUCTION' else 'price':{'value':str(value),'currency':'USD'}}
        if end:item['itemEndDate']=end
        pid,at=self.store.archive('https://www.ebay.com/sch/i.html',mode,{})
        self.store.record(pid,at,[item])

    def test_card_history_includes_previous_listings_and_price_changes(self):
        self.save('1',400)
        self.save('1',350,'sold')
        self.save('2',500)
        self.save('3',100,card='Pikachu')
        history=self.store.history_page(card='Rayquaza')
        self.assertEqual({r['item_id'] for r in history['rows']},{'1','2'})
        self.assertEqual(self.store.history_page(card='Rayquaza',status='active')['total'],1)
        observations=self.store.observations('1',limit=1)
        self.assertEqual(observations['total'],2)
        self.assertEqual(observations['rows'][0]['payload']['price']['value'],'350')
        self.assertEqual(self.store.observations('1',page=2,limit=1)['rows'][0]['payload']['price']['value'],'400')

    def test_current_offer_filters_price_type_and_explicit_end(self):
        self.save('1',400)
        self.save('2',200,mode='AUCTION')
        self.save('3',300,'sold')
        self.save('4',400,end='2020-01-01T00:00:00Z')
        rows=self.store.history_page(status='active',minimum='300',maximum='500')['rows']
        self.assertEqual([r['item_id'] for r in rows],['1'])
        rows=self.store.history_page(mode='AUCTION',minimum='150',maximum='250')['rows']
        self.assertEqual([r['item_id'] for r in rows],['2'])
        for bounds in [{'minimum':'nan'},{'minimum':'-1'},{'minimum':'500','maximum':'100'}]:
            with self.assertRaises(ValueError):self.store.history_page(**bounds)
