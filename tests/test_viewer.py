import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from pokefire.monitor_lock import MonitorLock, running
from pokefire.monitor import matches
from pokefire.viewer import Controller, request_handler_for, read_export


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'watchlist.json'
        config = json.loads(Path('fixtures/watchlist.example.json').read_text())
        config['state_file'] = str(self.root / 'state.sqlite3')
        self.path.write_text(json.dumps(config))
        self.controller = Controller(self.path)
        self.controller.log_dir = self.root / 'logs'

    def test_lock_detects_terminal_process_and_blocks_duplicates(self):
        state = self.controller.state_path()
        self.assertFalse(running(state, 'ebay'))
        lock = MonitorLock(state, 'ebay')
        try:
            self.assertTrue(running(state, 'ebay'))
            with self.assertRaisesRegex(ValueError, 'already running'):
                MonitorLock(state, 'ebay')
            status = self.controller.status()['ebay']
            self.assertTrue(status['running'])
            self.assertFalse(status['managed'])
            with self.assertRaisesRegex(ValueError, 'terminal'):
                self.controller.stop('ebay')
        finally:
            lock.close()
        self.assertFalse(running(state, 'ebay'))

    def test_config_save_backup_revision_and_validation(self):
        payload = self.controller.config()
        payload['config']['poll_seconds'] = 45
        self.controller.save(payload)
        self.assertEqual(json.loads(self.path.read_text())['poll_seconds'], 45)
        self.assertEqual(json.loads(self.path.with_suffix('.json.bak').read_text())['poll_seconds'], 30)
        with self.assertRaisesRegex(ValueError, 'changed elsewhere'):
            self.controller.save(payload)
        payload = self.controller.config()
        payload['config']['watchlist'][0]['terms'] = []
        with self.assertRaises(ValueError):
            self.controller.save(payload)
        self.assertEqual(json.loads(self.path.read_text())['poll_seconds'], 45)

    def test_save_refuses_active_monitors(self):
        lock = MonitorLock(self.controller.state_path(), 'ebay')
        try:
            with self.assertRaisesRegex(ValueError, 'Stop the'):
                self.controller.save(self.controller.config())
        finally:
            lock.close()

    def test_literal_comments_survive_dashboard_save(self):
        from pokefire.monitor import load_config
        comments = '// Original query: PSA 10 (star,goldstar)\n// https://www.ebay.com/sch/i.html?_nkw=star\n'
        self.path.write_text(comments + self.path.read_text())
        payload = self.controller.config()
        payload['config']['watchlist'][0]['image_url'] = 'https://example.com/card.png'
        payload['config']['poll_seconds'] = 45
        self.controller.save(payload)
        self.assertTrue(self.path.read_text().startswith(comments))
        loaded = load_config(self.path)
        self.assertEqual(loaded['poll_seconds'], 45)
        self.assertEqual(loaded['watchlist'][0]['image_url'], 'https://example.com/card.png')

    @patch('pokefire.viewer.subprocess.Popen')
    def test_managed_process_start_stop_and_error(self, popen):
        child = Mock()
        child.poll.return_value = None
        popen.return_value = child
        self.controller.start('ebay')
        command = popen.call_args.args[0]
        self.assertEqual(command[1:4], ['-u', '-m', 'pokefire.monitor'])
        self.assertEqual(command[-2:], ['--source', 'ebay'])
        self.assertTrue(self.controller.status()['ebay']['managed'])
        with self.assertRaises(ValueError):
            self.controller.start('ebay')
        self.controller.stop('ebay')
        child.send_signal.assert_called_once()
        child.poll.return_value = 1
        self.assertEqual(self.controller.status()['ebay']['state'], 'error')

    def test_status_changes_only_after_poll_processing_commits(self):
        from pokefire.listing_store import ListingStore
        store = ListingStore(self.controller.state_path())
        self.assertEqual(self.controller.status()['ebay']['completed_poll'], 0)
        poll, observed = store.archive('https://www.ebay.com/sch/i.html', 'FIXED_PRICE', {})
        self.assertEqual(self.controller.status()['ebay']['completed_poll'], 0)
        # Even an empty successful poll is a completed poll.
        store.record(poll, observed, [])
        self.assertEqual(self.controller.status()['ebay']['completed_poll'], poll)
        failed, observed = store.archive('https://www.ebay.com/sch/i.html', 'AUCTION', {})
        store.parse_failed(failed)
        self.assertEqual(self.controller.status()['ebay']['completed_poll'], poll)
        interrupted, observed = store.archive('https://www.ebay.com/sch/i.html', 'FIXED_PRICE', {})
        with self.assertRaises(KeyError):
            store.record(interrupted, observed, [{'itemId': '123'}])
        self.assertEqual(self.controller.status()['ebay']['completed_poll'], poll)
        store.record(interrupted, observed, [])
        self.assertEqual(self.controller.status()['ebay']['completed_poll'], interrupted)

    def test_disabled_cards_do_not_match(self):
        rule = {'terms': [['Pikachu']], 'enabled': False}
        self.assertFalse(matches({'title': 'Pikachu'}, rule))

    def test_json_and_csv_export_loading_and_invalid_files(self):
        path = self.root / 'test.json'
        path.write_text(json.dumps([{'item_id': '1', 'title': '<script>bad</script>',
                                    'observed_at': '2026-01-01T00:00:00Z',
                                    'price': {'value': '2.00', 'currency': 'EUR'},
                                    'matches': ['Pikachu']}]))
        row = read_export(path)[0]
        self.assertEqual(row['price'], '2.00')
        self.assertEqual(row['currency'], 'EUR')
        self.assertEqual(row['matches'], 'Pikachu')
        path.write_text('{}')
        with self.assertRaises(ValueError):
            read_export(path)
        csv_path = self.root / 'test.csv'
        csv_path.write_text('item_id,title,price,observed_at\n1,Pikachu,5,today\n')
        self.assertEqual(read_export(csv_path)[0]['title'], 'Pikachu')

    def request_handler(self, path, headers=None):
        cls = request_handler_for(self.root, self.controller)
        handler = object.__new__(cls)
        handler.path = path
        handler.headers = headers or {'Host': '127.0.0.1:8765'}
        handler.reply = Mock()
        return handler

    def test_http_rejects_foreign_hosts_path_traversal_and_secret_files(self):
        for path, expected in [('/.env', 404), ('/api/exports?file=../.env', 400),
                               ('/api/exports?file=state.sqlite3', 400)]:
            h = self.request_handler(path)
            h.do_GET()
            self.assertEqual(h.reply.call_args.args[0], expected)
        h = self.request_handler('/api/config', {'Host': 'attacker.example'})
        h.do_GET()
        self.assertEqual(h.reply.call_args.args[0], 403)

    def test_history_api_filters_locally_and_rejects_retired_source(self):
        from pokefire.listing_store import ListingStore
        from pokefire.scrapingdog import normalize_results
        store=ListingStore(self.controller.state_path())
        payload={'search_results':[{'itemId':'123','title':'Rayquaza Gold Star PSA 10','price':'$400'}]}
        pid,at=store.archive('https://www.ebay.com/sch/i.html','FIXED_PRICE',payload)
        store.record(pid,at,normalize_results(payload,'FIXED_PRICE'))
        h=self.request_handler('/api/listings?q=Rayquaza&page=1')
        h.do_GET()
        self.assertEqual(h.reply.call_args.args[0],200)
        self.assertEqual(h.reply.call_args.args[1]['total'],1)
        self.assertEqual(h.reply.call_args.args[1]['rows'][0]['url'],'https://www.ebay.com/itm/123')
        with self.assertRaises(ValueError):self.controller.start('cardtrader')

    def test_http_writes_require_same_origin(self):
        h = self.request_handler('/api/start', {'Host': '127.0.0.1:8765',
                                               'Origin': 'https://attacker.example'})
        h.do_POST()
        self.assertEqual(h.reply.call_args.args[0], 403)


if __name__ == '__main__':
    unittest.main()
