import csv
import json
from pathlib import Path
import tempfile
import unittest

from pokefire import State, export_rows, load_config, poll
from test_pokefire import FakeClient, item


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = load_config("fixtures/watchlist.example.json")
        self.config["state_file"] = str(Path(self.temp.name) / "state.sqlite3")
        self.state = State(self.config)
        self.addCleanup(self.state.db.close)

    def test_baseline_seen_prices_changes_and_no_duplicate_notifications(self):
        listing = item("1") | {"price": {"value": "100.00", "currency": "USD"},
                               "itemWebUrl": "https://www.ebay.com/itm/123"}
        sent = []
        for value in ("100.00", "100.00", "90.00"):
            listing["price"]["value"] = value
            poll(FakeClient({"itemSummaries": [listing]}), self.config, self.state,
                 lambda *args: sent.append(args))
        history = self.state.history()
        self.assertEqual([r["price"]["value"] for r in history], ["100.00", "100.00", "90.00"])
        self.assertEqual(sent, [])
        self.assertTrue(all(r["observed_at"] and r["url"] for r in history))
        self.assertEqual(history[0]["url_type"], "listing")

    def test_snapshot_includes_existing_without_changing_notification_baseline(self):
        self.state.mark("1")
        rows = self.state.record([item("1"), item("2", "Unrelated card")], self.config)
        self.assertEqual(len(rows), 1)
        self.assertFalse(self.state.initialized)
        self.assertEqual(rows[0]["event"], "price_observation")
        other = State(self.config | {"query": "different scope"})
        try:
            self.assertEqual(other.history(), [])
        finally:
            other.db.close()

    def test_failed_scan_does_not_record_partial_prices(self):
        self.config["max_pages"] = 2
        client = FakeClient({"itemSummaries": [item("1")], "next": "yes"}, OSError("timeout"))
        with self.assertRaises(OSError):
            poll(client, self.config, self.state)
        self.assertEqual(self.state.history(), [])

    def test_csv_json_links_and_safe_text(self):
        listing = item("1", '=HYPERLINK("bad") Charizard Base Set') | {
            "price": {"value": "12.34", "currency": "EUR"},
            "itemWebUrl": "https://www.ebay.com/itm/123"}
        rows = self.state.record([listing], self.config)
        path = Path(self.temp.name) / "offers.csv"
        export_rows(path, rows)
        with path.open() as file:
            exported = list(csv.DictReader(file))
        self.assertEqual(exported[0]["price"], "12.34")
        self.assertEqual(exported[0]["currency"], "EUR")
        self.assertTrue(exported[0]["title"].startswith("'="))
        self.assertEqual(exported[0]["url"], listing["itemWebUrl"])
        with self.assertRaises(FileExistsError):
            export_rows(path, [])
        path = path.with_suffix(".json")
        export_rows(path, rows)
        self.assertEqual(json.loads(path.read_text()), rows)

    def test_empty_export_has_headers(self):
        path = Path(self.temp.name) / "empty.csv"
        export_rows(path, [])
        self.assertIn("observed_at", path.read_text())

