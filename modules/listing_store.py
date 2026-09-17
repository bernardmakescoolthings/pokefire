"""Durable raw polls, parsed observations, and evidence-based listing history."""
from contextlib import closing
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3


class ListingStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS scraper_polls (
                    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, target_url TEXT NOT NULL,
                    mode TEXT NOT NULL, raw_json_gzip BLOB NOT NULL, parse_error TEXT);
                CREATE TABLE IF NOT EXISTS completed_scraper_polls (
                    poll_id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS listing_snapshots (
                    poll_id INTEGER NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(poll_id,item_id));
                CREATE TABLE IF NOT EXISTS listings (
                    item_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
                    status TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    listed_at TEXT, listed_at_precision TEXT, sold_at TEXT, sold_at_precision TEXT,
                    sold_date_text TEXT, sold_observed_at TEXT, last_poll_id INTEGER NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS listing_card_matches (
                    item_id TEXT NOT NULL, card_name TEXT NOT NULL,
                    PRIMARY KEY(item_id,card_name));
                CREATE INDEX IF NOT EXISTS listing_card_matches_name ON listing_card_matches(card_name,item_id);
                CREATE TABLE IF NOT EXISTS listing_events (
                    id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, poll_id INTEGER NOT NULL,
                    observed_at TEXT NOT NULL, previous_status TEXT, status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS listing_events_item ON listing_events(item_id,id);
                CREATE INDEX IF NOT EXISTS listing_snapshots_item ON listing_snapshots(item_id,poll_id);
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def archive(self, target, mode, payload):
        now = datetime.now(timezone.utc).isoformat()
        compressed = gzip.compress(json.dumps(payload, ensure_ascii=False).encode())
        with closing(self.connect()) as db, db:
            poll_id = db.execute('INSERT INTO scraper_polls(observed_at,target_url,mode,raw_json_gzip) VALUES (?,?,?,?)',
                                 (now, target, mode, compressed)).lastrowid
        return poll_id, now

    def record(self, poll_id, observed_at, items):
        with closing(self.connect()) as db, db:
            for item in items:
                identifier = item['itemId']
                encoded = json.dumps(item, ensure_ascii=False)
                db.execute('INSERT OR REPLACE INTO listing_snapshots VALUES (?,?,?)', (poll_id, identifier, encoded))
                # Keep every raw/parsed poll for reuse, but only watchlist
                # matches become visible card-history entries.
                if 'watchlistMatches' in item and not item['watchlistMatches']:
                    continue
                db.executemany('INSERT OR IGNORE INTO listing_card_matches VALUES (?,?)',
                               [(identifier, name) for name in item.get('watchlistMatches', [])])
                status = item.get('listingStatus', 'active')
                previous = db.execute('SELECT status FROM listings WHERE item_id=?', (identifier,)).fetchone()
                if previous is None or previous[0] != status:
                    db.execute('INSERT INTO listing_events(item_id,poll_id,observed_at,previous_status,status) VALUES (?,?,?,?,?)',
                               (identifier, poll_id, observed_at, previous[0] if previous else None, status))
                db.execute('''INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(item_id) DO UPDATE SET title=excluded.title,url=excluded.url,
                    status=excluded.status,last_seen_at=excluded.last_seen_at,
                    listed_at=COALESCE(excluded.listed_at,listings.listed_at),
                    listed_at_precision=COALESCE(excluded.listed_at_precision,listings.listed_at_precision),
                    sold_at=COALESCE(excluded.sold_at,listings.sold_at),
                    sold_at_precision=COALESCE(excluded.sold_at_precision,listings.sold_at_precision),
                    sold_date_text=COALESCE(excluded.sold_date_text,listings.sold_date_text),
                    sold_observed_at=COALESCE(listings.sold_observed_at,excluded.sold_observed_at),
                    last_poll_id=excluded.last_poll_id,payload=excluded.payload''',
                    (identifier, item['title'], item['itemWebUrl'], status, observed_at, observed_at,
                     item.get('itemCreationDate'), 'timestamp' if item.get('itemCreationDate') else None,
                     item.get('soldDate'), item.get('soldDatePrecision'), item.get('soldDateText'),
                     observed_at if status == 'sold' else None, poll_id, encoded))

            db.execute('INSERT OR IGNORE INTO completed_scraper_polls VALUES (?)', (poll_id,))

    def completed_poll(self):
        with closing(self.connect()) as db:
            return db.execute('SELECT COALESCE(MAX(poll_id), 0) FROM completed_scraper_polls').fetchone()[0]

    def parse_failed(self, poll_id):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE scraper_polls SET parse_error=? WHERE id=?', ('Invalid search response', poll_id))

    def history_page(self, item_id=None, query='', card='', page=1, limit=50, minimum='', maximum='', mode='', status=''):
        page = max(1, int(page))
        limit = min(250, max(1, int(limit)))
        clauses, args = [], []
        if item_id:
            clauses.append('l.item_id=?')
            args.append(item_id)
        for term in query.casefold().split():
            clauses.append('instr(lower(l.title),?)>0')
            args.append(term)
        if card:
            clauses.append('EXISTS (SELECT 1 FROM listing_card_matches m WHERE m.item_id=l.item_id AND m.card_name=?)')
            args.append(card)
        if mode:
            if mode not in ('FIXED_PRICE', 'AUCTION'):
                raise ValueError('Invalid listing type')
            clauses.append("EXISTS (SELECT 1 FROM json_each(l.payload,'$.buyingOptions') WHERE value=?)")
            args.append(mode)
        if status:
            if status not in ('active','sold','ended'):
                raise ValueError('Invalid listing status')
            clauses.append('l.status=?')
            args.append(status)
            if status == 'active':
                clauses.append("(julianday(json_extract(l.payload,'$.itemEndDate')) IS NULL OR julianday(json_extract(l.payload,'$.itemEndDate')) > julianday('now'))")
        price = "json_extract(l.payload,'$.currentBidPrice.value')" if mode == 'AUCTION' else "coalesce(json_extract(l.payload,'$.price.value'),json_extract(l.payload,'$.currentBidPrice.value'))"
        currency = "json_extract(l.payload,'$.currentBidPrice.currency')" if mode == 'AUCTION' else "coalesce(json_extract(l.payload,'$.price.currency'),json_extract(l.payload,'$.currentBidPrice.currency'))"
        import math
        bounds = []
        for value, op in ((minimum, '>='), (maximum, '<=')):
            if value not in ('', None):
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    raise ValueError('Price bounds must be nonnegative numbers')
                clauses.append(f"({currency}='USD' AND {price} IS NOT NULL AND CAST({price} AS REAL) {op} ?)")
                args.append(number)
                bounds.append(number)
        if len(bounds) == 2 and bounds[0] > bounds[1]:
            raise ValueError('Minimum price exceeds maximum')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            total = db.execute('SELECT count(*) FROM listings l'+where,args).fetchone()[0]
            rows = [dict(r) for r in db.execute('SELECT l.* FROM listings l'+where+
                     ' ORDER BY l.first_seen_at DESC,l.item_id LIMIT ? OFFSET ?', args+[limit,(page-1)*limit])]
            for row in rows:
                row['payload'] = json.loads(row['payload'])
                row['events'] = [dict(e) for e in db.execute('SELECT * FROM listing_events WHERE item_id=? ORDER BY id', (row['item_id'],))]
                row['cards'] = [r[0] for r in db.execute('SELECT card_name FROM listing_card_matches WHERE item_id=? ORDER BY card_name',(row['item_id'],))]
            cards = [r[0] for r in db.execute('SELECT DISTINCT card_name FROM listing_card_matches ORDER BY card_name')]
            return {'rows':rows,'total':total,'page':page,'page_size':limit,'cards':cards}

    def history(self, item_id=None, limit=250):
        return self.history_page(item_id=item_id,limit=limit)['rows']

    def observations(self, item_id, page=1, limit=50):
        page=max(1,int(page));limit=min(100,max(1,int(limit)))
        with closing(self.connect()) as db:
            total=db.execute('SELECT count(*) FROM listing_snapshots WHERE item_id=?',(item_id,)).fetchone()[0]
            rows=[{'observed_at':r[0],'payload':json.loads(r[1])} for r in db.execute(
                'SELECT p.observed_at,s.payload FROM listing_snapshots s JOIN scraper_polls p ON p.id=s.poll_id WHERE s.item_id=? ORDER BY p.id DESC LIMIT ? OFFSET ?',
                (item_id,limit,(page-1)*limit))]
            return {'rows':rows,'total':total,'page':page,'page_size':limit}
