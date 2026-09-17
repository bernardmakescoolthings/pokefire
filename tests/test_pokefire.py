import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from pokefire.monitor import ApiError, EbayClient, State, build_query, load_config, matches, poll


def item(identifier, title="Charizard Base Set", date=None):
    result = {"itemId": identifier, "title": title}
    if date:
        result["itemCreationDate"] = date
    return result


class FakeClient:
    def __init__(self, *pages):
        self.pages = iter(pages)
        self.offsets = []

    def search(self, offset):
        self.offsets.append(offset)
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return result


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = load_config("fixtures/watchlist.example.json")
        self.config["state_file"] = str(Path(self.temp.name) / "state.sqlite3")

    def state(self):
        state = State(self.config)
        self.addCleanup(state.db.close)
        return state

    def test_matching_aliases_accents_boundaries_and_exclusions(self):
        rule = {"terms": [["Pokémon"], ["Base Set", "BS"]], "exclude": ["proxy"]}
        self.assertTrue(matches(item("1", "POKEMON BS holo"), rule))
        self.assertFalse(matches(item("1", "Pokemon base set proxy"), rule))
        self.assertFalse(matches(item("1", "Pokemon ABS holo"), rule))
        self.assertFalse(matches(item("1", "Pokemon"), rule))

    def test_query_encompasses_first_group_aliases(self):
        self.assertEqual(build_query(self.config), "(charizard,evolving,evs,umbreon)")
        self.config["query"] = "pokemon"
        self.assertEqual(build_query(self.config), "pokemon")

    def test_invalid_config_and_long_query(self):
        for update in [{"poll_seconds": 0}, {"query": "x" * 101},
                       {"watchlist": [{"name": "x", "terms": [[]]}]}]:
            path = Path(self.temp.name) / "bad.json"
            path.write_text(json.dumps(self.config | update))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_baseline_new_listing_and_restart_deduplication(self):
        state = self.state()
        sent = []
        send = lambda listing, names: sent.append(listing["itemId"])
        poll(FakeClient({"itemSummaries": [item("old")]}), self.config, state, send)
        self.assertEqual(sent, [])
        poll(FakeClient({"itemSummaries": [item("new"), item("old")]}), self.config, state, send)
        restarted = self.state()
        poll(FakeClient({"itemSummaries": [item("new")]}), self.config, restarted, send)
        self.assertEqual(sent, ["new"])

    def test_historical_listing_not_alerted_when_it_enters_window(self):
        state = self.state()
        state.initialize()
        self.assertEqual(poll(FakeClient({"itemSummaries": [
            item("old", date="2000-01-01T00:00:00Z")]}), self.config, state), 0)

    def test_new_listing_during_initial_poll_and_multiple_matches(self):
        state = self.state()
        sent = []
        listing = item("new", "Umbreon VMAX 215/203 Evolving Skies", "2099-01-01T00:00:00Z")
        poll(FakeClient({"itemSummaries": [listing]}), self.config, state,
             lambda listing, names: sent.append(names))
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]), 2)

    def test_notify_existing_opt_in(self):
        self.config["notify_existing"] = True
        sent = []
        poll(FakeClient({"itemSummaries": [item("old", date="2000-01-01T00:00:00Z")]}),
             self.config, self.state(), lambda *args: sent.append(args))
        self.assertEqual(len(sent), 1)

    def test_pagination_deduplication_and_failed_page_preserves_baseline(self):
        self.config["max_pages"] = 2
        state = self.state()
        client = FakeClient({"itemSummaries": [item("1")], "next": "unused"}, ApiError(503))
        with self.assertRaises(ApiError):
            poll(client, self.config, state)
        self.assertFalse(state.initialized)
        self.assertFalse(state.seen("1"))
        self.assertEqual(client.offsets, [0, 200])
        self.config["notify_existing"] = True
        client = FakeClient({"itemSummaries": [item("1")], "next": "unused"},
                            {"itemSummaries": [item("1"), item("2")]})
        self.assertEqual(poll(client, self.config, state, lambda *args: None), 2)

    def test_delivery_failure_is_retried(self):
        state = self.state()
        state.initialize()
        def fail(*args):
            raise OSError("delivery failed")
        with self.assertRaises(OSError):
            poll(FakeClient({"itemSummaries": [item("1")]}), self.config, state, fail)
        self.assertFalse(state.seen("1"))
        self.assertEqual(poll(FakeClient({"itemSummaries": [item("1")]}),
                              self.config, state, lambda *args: None), 1)

    def test_page_cap_warns_when_no_overlap(self):
        state = self.state()
        state.initialize()
        with self.assertLogs("pokefire", level="WARNING"):
            poll(FakeClient({"itemSummaries": [item("1")], "next": "unused"}),
                 self.config, state, lambda *args: None)

    def test_watchlist_changes_get_a_new_baseline(self):
        state = self.state()
        state.initialize()
        self.config["watchlist"][0]["terms"] = [["Pikachu"]]
        self.assertFalse(self.state().initialized)



if __name__ == "__main__":
    unittest.main()
