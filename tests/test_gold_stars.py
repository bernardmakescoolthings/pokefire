import json
from pathlib import Path
import tempfile
import unittest

from pokefire.monitor import build_query, load_config, matches


class GoldStarTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("watchlist.json")
        self.rules = {r["card"]["pokemon"]: r for r in self.config["watchlist"] if "card" in r}

    def test_complete_catalog_and_all_grades(self):
        expected = {"Mudkip", "Torchic", "Treecko", "Latias", "Latios", "Rayquaza",
                    "Entei", "Raikou", "Suicune", "Groudon", "Kyogre", "Metagross",
                    "Regice", "Regirock", "Registeel", "Gyarados", "Mewtwo", "Pikachu",
                    "Alakazam", "Celebi", "Charizard", "Mew", "Flareon", "Jolteon",
                    "Vaporeon"}
        self.assertEqual(set(self.rules), expected)
        self.assertEqual(len(self.config["watchlist"]), 25)
        self.assertEqual(sum(r.get('kind') == 'set' for r in self.config['watchlist']), 0)
        self.assertTrue(build_query(self.config).startswith('PSA 10 (star,goldstar,'))
        self.assertLessEqual(len(build_query(self.config)),100)
        for rule in self.rules.values():
            self.assertIn(rule['card']['number'].split('/')[0],build_query(self.config))
        for name, rule in self.rules.items():
            for grade in (8, 8.5, 9, 10):
                title = f'{name} Gold Star {rule["card"]["set"]} PSA {grade}'
                with self.subTest(title=title):
                    self.assertEqual(matches({"title": title}, rule), grade == 10)

    def test_title_variations(self):
        for text in ("Rayquaza Goldstar PSA10", "Rayquaza ★ PSA 10",
                     "Rayquaza ☆ PSA GEM MINT 10", "Rayquaza 107/107 PSA GEM MINT 10",
                     "Rayquaza Gold Star PSA-10", "Rayquaza Gold Star PSA graded 10"):
            with self.subTest(text=text):
                self.assertTrue(matches({"title": text}, self.rules["Rayquaza"]))

    def test_reject_wrong_ambiguous_or_missing_grade(self):
        for grade in ("PSA 7", "PSA 7.5", "PSA 9.5", "PSA 80", "PSA 100", "PSA 10?",
                      "PSA 8-10", "PSA 8/10", "PSA 12345678", "PSA Authentic", "BGS 9",
                      "CGC 10", "raw PSA 10 contender", "not PSA 9", "PSA 10 potential",
                      "PSA 7 possible PSA 9", "BGS 9.5 PSA 10", "ungraded", ""):
            with self.subTest(grade=grade):
                self.assertFalse(matches({"title": "Rayquaza Gold Star " + grade}, self.rules["Rayquaza"]))

    def test_identity_and_reprints(self):
        self.assertFalse(matches({"title": "Mewtwo Gold Star PSA 9"}, self.rules["Mew"]))
        self.assertFalse(matches({"title": "Rayquaza VMAX PSA 10"}, self.rules["Rayquaza"]))
        self.assertFalse(matches({"title": "Rayquaza Gold Star proxy PSA 10"}, self.rules["Rayquaza"]))

    def test_invalid_grading_config(self):
        for grading in ({}, {"company": "CGC", "min": 8, "max": 10},
                        {"company": "PSA", "min": 10, "max": 8},
                        {"company": "PSA", "min": "8", "max": 10}):
            self.config["watchlist"][0]["grading"] = grading
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(self.config))
                with self.assertRaises(ValueError):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
