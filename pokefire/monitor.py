"""An eBay watchlist notifier powered by Scrapingdog (Python 3.11+, standard library)."""

import argparse
import csv
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pokefire.env_config import load_env
from pokefire.config_json import loads as load_config_json
from pokefire.monitor_lock import MonitorLock

LOG = logging.getLogger("pokefire")


def normalize(value):
    value = value.replace("★", " star ").replace("☆", " star ")
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", value))


def contains(title, phrase):
    return f" {normalize(phrase)} " in f" {normalize(title)} "


PSA_GRADE = re.compile(
    r"\bpsa\s*[-:]?\s*(?:(?:graded?|gem\s*mint|mint|nm\s*[-/]?\s*mt\+?|near\s*mint)\s*)*"
    r"(\d+(?:\.\d+)?)(?![\w.]|\s*\?|\s*[-/]\s*\d)", re.IGNORECASE)


def matches_grade(item, grading):
    # Preserve decimal points: token normalization would turn PSA 8.5 into PSA 8.
    text = item.get("gradingText", item.get("title", ""))
    if re.search(r"\b(?:BGS|CGC|SGC|raw|ungraded|potential|candidate|contender)\b|\bnot\s+psa\b",
                 text, re.IGNORECASE):
        return False
    grades = [float(m.group(1)) for m in PSA_GRADE.finditer(text)]
    return bool(grades) and all(grade in (8, 8.5, 9, 10) and
                               grading["min"] <= grade <= grading["max"] for grade in grades)


def matches(item, rule):
    """AND between groups; OR between aliases in each group. Extend criteria here."""
    if rule.get("enabled", True) is False:
        return False
    if rule.get("grading") and not matches_grade(item, rule["grading"]):
        return False
    title = item.get("title", "")
    return (all(any(contains(title, alias) for alias in group)
               for group in rule["terms"]) and not any(
        contains(title, term) for term in rule.get("exclude", [])) and
        (not rule.get("grading") or matches_grade(item, rule["grading"])))


def matching_names(item, config):
    return [rule['name'] for rule in config['watchlist'] if matches(item, rule)]


def build_query(config):
    if config.get("query"):
        query = config["query"]
    else:
        # Each rule requires its first group, so OR one broad anchor per alias.
        # Other groups (set/card number/etc.) are checked locally.
        anchors = sorted({normalize(alias).split()[0]
                          for rule in config["watchlist"]
                          for alias in rule["terms"][0]})
        query = anchors[0] if len(anchors) == 1 else "(" + ",".join(anchors) + ")"
    if not isinstance(query, str) or not query.strip() or len(query) > 100:
        raise ValueError("Search query must be 1–100 characters; set a shorter broad 'query'.")
    return query


def load_config(path, source="ebay"):
    if source != "ebay":
        raise ValueError("Only eBay is supported")
    config = load_config_json(Path(path).read_text())
    if not isinstance(config, dict):
        raise ValueError("Config must be an object")
    defaults = {"poll_seconds": 120, "marketplace": "EBAY_US", "category_id": "183454",
                "page_size": 240, "max_pages": 1, "state_file": "data/state.sqlite3",
                "notify_existing": False, "environment": "production"}
    config = defaults | config
    for key, low, high in [("poll_seconds", 1, 86400), ("page_size", 1, 240),
                           ("max_pages", 1, 50)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if config["environment"] != "production" or config["marketplace"] != "EBAY_US":
        raise ValueError("Scrapingdog supports production EBAY_US searches only")
    if type(config["notify_existing"]) is not bool:
        raise ValueError("notify_existing must be a boolean")
    if not isinstance(config.get("watchlist"), list) or not config["watchlist"]:
        raise ValueError("watchlist must be a nonempty list")
    labels = set()
    for rule in config["watchlist"]:
        if not isinstance(rule, dict) or not isinstance(rule.get("name"), str) or not rule["name"].strip():
            raise ValueError("Each rule needs a name")
        if rule["name"] in labels:
            raise ValueError("Watchlist names must be unique")
        labels.add(rule["name"])
        if type(rule.get("enabled", True)) is not bool:
            raise ValueError("Watchlist enabled must be a boolean")
        groups = rule.get("terms")
        if not isinstance(groups, list) or not groups:
            raise ValueError("Each rule needs a nonempty terms list")
        for group in groups + [rule.get("exclude", [])]:
            if not isinstance(group, list) or any(
                    not isinstance(s, str) or not normalize(s) for s in group):
                raise ValueError("Term groups and exclude must be lists of nonempty strings")
        if any(not group for group in groups):
            raise ValueError("Term groups cannot be empty")
        if "grading" in rule:
            grading = rule["grading"]
            if (not isinstance(grading, dict) or grading.get("company") != "PSA" or
                    any(type(grading.get(k)) not in (int, float) for k in ("min", "max")) or
                    not 8 <= grading["min"] <= grading["max"] <= 10):
                raise ValueError("grading requires company PSA and numeric min/max between 8 and 10")
    if source == "ebay":
        build_query(config)
    from pokefire.discord_bot import validate as validate_discord
    validate_discord(config)
    for key in ('ebay_poll_seconds', 'auction_poll_seconds'):
        if key in config and (type(config[key]) is not int or config[key] < 1):
            raise ValueError(f'{key} must be a positive integer')
    return config


class ApiError(Exception):
    def __init__(self, status, retry_after=0):
        super().__init__(f"API HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def request_json(request):
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        retry = exc.headers.get("Retry-After", "0")
        try:
            retry = max(0, float(retry))
        except ValueError:
            try:
                retry = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
            except (ValueError, TypeError):
                retry = 0
        # Do not log request headers, credentials, or raw response bodies.
        raise ApiError(exc.code, retry) from None


from pokefire.scrapingdog import ScrapingdogClient as EbayClient


class State:
    def __init__(self, config):
        path = Path(config["state_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS seen (scope TEXT, item_id TEXT,
                PRIMARY KEY (scope, item_id));
            CREATE TABLE IF NOT EXISTS metadata (scope TEXT PRIMARY KEY, started REAL);
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY, scope TEXT, source TEXT, observed_at TEXT);
            CREATE TABLE IF NOT EXISTS observations (
                scan_id INTEGER, item_id TEXT, payload TEXT,
                PRIMARY KEY (scan_id, item_id));
            CREATE INDEX IF NOT EXISTS scans_scope ON scans(scope, id);
            CREATE TABLE IF NOT EXISTS qualified_alerts (
                scope TEXT, item_id TEXT, PRIMARY KEY(scope, item_id));
        """)
        identity = {key: config.get(key) for key in
                    ("watchlist", "query", "marketplace", "category_id", "environment", "notify_existing")}
        identity["watchlist"] = [{k: v for k, v in rule.items() if k != "image_url"}
                                 for rule in config["watchlist"]]
        identity["provider"] = "scrapingdog"
        self.scope = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        row = self.db.execute("SELECT started FROM metadata WHERE scope=?", (self.scope,)).fetchone()
        self.initialized = row is not None
        self.started = row[0] if row else time.time()
        self.source = config.get("source", "ebay")

    def record(self, items, config):
        """Store one price observation per matching offer per successful scan."""
        observed_at = datetime.now(timezone.utc).isoformat()
        rows = []
        for item in items:
            names = matching_names(item, config)
            if names:
                rows.append(alert_payload(item, names) |
                            {"observed_at": observed_at, "event": "price_observation"})
        with self.db:
            scan_id = self.db.execute(
                "INSERT INTO scans(scope, source, observed_at) VALUES (?, ?, ?)",
                (self.scope, self.source, observed_at)).lastrowid
            self.db.executemany("INSERT INTO observations VALUES (?, ?, ?)",
                                [(scan_id, row["item_id"], json.dumps(row)) for row in rows])
        return rows

    def alerted(self, item_id):
        return self.db.execute('SELECT 1 FROM qualified_alerts WHERE scope=? AND item_id=?',
                               (self.scope, item_id)).fetchone() is not None

    def mark_alerted(self, item_id):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO qualified_alerts VALUES (?, ?)', (self.scope, item_id))

    def history(self):
        rows = self.db.execute(
            "SELECT o.payload FROM observations o JOIN scans s ON s.id=o.scan_id "
            "WHERE s.scope=? ORDER BY s.id, o.item_id", (self.scope,))
        return [json.loads(row[0]) for row in rows]

    def seen(self, item_id):
        return self.db.execute("SELECT 1 FROM seen WHERE scope=? AND item_id=?",
                               (self.scope, item_id)).fetchone() is not None

    def mark(self, item_id):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (self.scope, item_id))

    def initialize(self):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (self.scope, self.started))
        self.initialized = True


def alert_payload(item, names):
    price = item.get("price") or item.get("currentBidPrice") or {}
    payload = {"event": item.get("event", "new_listing"), "source": item.get("source", "ebay"),
                      "matches": names, "item_id": item["itemId"],
                      "title": item["title"], "price": price,
                      "url": item.get("itemWebUrl"), "created_at": item.get("itemCreationDate")}
    payload["url_type"] = item.get("url_type", "listing" if item.get("itemWebUrl") else None)
    for key in ("product_id", "blueprint_id", "seller", "quantity", "properties", "gradingText", "buyingOptions", "itemEndDate", "imageUrl", "listingStatus", "soldDate", "soldDatePrecision", "soldDateText"):
        if key in item:
            payload[key] = item[key]
    return payload


def notify(item, names):
    print(json.dumps(alert_payload(item, names), ensure_ascii=False), flush=True)


def export_rows(path, rows):
    path = Path(path)
    if path.suffix.lower() not in (".csv", ".json"):
        raise ValueError("Export filename must end in .csv or .json")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents an export from overwriting credentials or history.
    with path.open("x", newline="", encoding="utf-8") as output:
        if path.suffix.lower() == ".json":
            json.dump(rows, output, ensure_ascii=False, indent=2)
            output.write("\n")
        else:
            fields = ["observed_at", "source", "item_id", "title", "matches", "price",
                      "currency", "url", "url_type", "seller", "grading_text", "created_at",
                      "imageUrl", "listingStatus", "soldDate", "soldDatePrecision"]
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                flattened = {key: row.get(key) for key in fields}
                flattened.update(matches="; ".join(row["matches"]),
                                 price=row["price"].get("value"), currency=row["price"].get("currency"),
                                 grading_text=row.get("gradingText", row["title"]))
                if isinstance(flattened.get("seller"), dict):
                    flattened["seller"] = flattened["seller"].get("username")
                # Keep seller-controlled text from becoming spreadsheet formulas.
                for key, value in flattened.items():
                    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                        flattened[key] = "'" + value
                writer.writerow(flattened)
    LOG.info("Exported %d rows to %s", len(rows), path)


def fetch_items(client, config):
    items = {}
    for page in range(config["max_pages"]):
        result = client.search(page * config["page_size"])
        for item in result.get("itemSummaries", []):
            if not isinstance(item, dict) or not item.get("itemId") or not item.get("title"):
                raise ValueError("Source returned a listing without itemId/title")
            if item.get("listingStatus") in ("sold", "ended"):
                continue
            previous = items.get(item["itemId"], {})
            combined = previous | item
            if previous.get("buyingOptions") or item.get("buyingOptions"):
                combined["buyingOptions"] = list(dict.fromkeys(
                    previous.get("buyingOptions", []) + item.get("buyingOptions", [])))
            items[item["itemId"]] = combined
        if not result.get("next"):
            break
    return items, bool(result.get("next"))


def poll(client, config, state, send=notify):
    items, has_more = fetch_items(client, config)
    # Fetch the entire configured window before changing state, so failed pages
    # cannot turn an incomplete initial scan into a successful baseline.
    if state.initialized and has_more and not any(state.seen(i) for i in items):
        LOG.warning("No overlap with previous results at page cap; listings may be missed. "
                    "Narrow the search or increase max_pages (uses more API calls).")
    alerts = 0
    observations = state.record(items.values(), config)
    matching_names = {row["item_id"]: row["matches"] for row in observations}
    for item in items.values():
        if state.seen(item["itemId"]):
            continue
        is_new = state.initialized or config["notify_existing"]
        if not config["notify_existing"] and item.get("itemCreationDate"):
            created = datetime.fromisoformat(item["itemCreationDate"].replace("Z", "+00:00"))
            is_new = created.timestamp() >= state.started
        names = matching_names.get(item["itemId"], [])
        if is_new and names:
            send(item, names)
            alerts += 1
        state.mark(item["itemId"])
    if not state.initialized:
        LOG.info("Initial baseline recorded (%d listings)", len(items))
        state.initialize()
    LOG.info("Checked %d listings; %d matching price observations; %d alerts",
             len(items), len(observations), alerts)
    return alerts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="watchlist.json")
    parser.add_argument("--env-file", help="Credentials file (default: .env in the working directory)")
    parser.add_argument("--source", choices=("ebay",), default="ebay")
    parser.add_argument("--once", action="store_true", help="One poll, using persistent state")
    exports = parser.add_mutually_exclusive_group()
    exports.add_argument("--export", metavar="PATH", help="Fetch current matches into a new .csv/.json file and log prices")
    exports.add_argument("--export-history", metavar="PATH", help="Export recorded prices offline for this source/watchlist")
    parser.add_argument("--print-query", action="store_true", help="Preview search; no credentials needed")
    parser.add_argument("--fixture", help="Match a saved source response; no API calls or state writes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = None
    monitor_lock = None
    try:
        load_env(args.env_file or Path(".env"), required=args.env_file is not None)
        config = load_config(args.config, args.source)
        config["source"] = args.source
        config['poll_seconds'] = config.get(args.source + '_poll_seconds', config['poll_seconds'])
        export_path = args.export or args.export_history
        if export_path:
            if args.fixture or args.print_query:
                raise ValueError("Export cannot be combined with fixture or query modes")
            if Path(export_path).suffix.lower() not in (".csv", ".json"):
                raise ValueError("Export filename must end in .csv or .json")
            if Path(export_path).exists():
                raise ValueError("Export path already exists; choose a new filename")
            state = State(config)
            if args.export_history:
                export_rows(args.export_history, state.history())
                return 0
        if args.print_query:
            print(build_query(config))
            return 0
        if args.fixture:
            response = json.loads(Path(args.fixture).read_text())
            for item in response.get("itemSummaries", []):
                names = matching_names(item, config)
                if names:
                    notify(item, names)
            return 0
        client = EbayClient(config, request_json)
        if args.export:
            items, has_more = fetch_items(client, config)
            if has_more:
                LOG.warning("Export contains only the configured result window")
            export_rows(args.export, state.record(items.values(), config))
            return 0
        from pokefire.discord_bot import notifier
        send = notifier(config, notify, alert_payload)
        monitor_lock = MonitorLock(config["state_file"], args.source)
        state = State(config)
        LOG.info("Source: %s; polling every %ds", args.source, config["poll_seconds"])
        if args.source == "ebay":
            LOG.info("Scrapingdog search: %s; 5 credits per successful page; BIN every %ds, auctions every %ds",
                     build_query(config), config["poll_seconds"], config.get("auction_poll_seconds", 900))
        failures = 0
        while True:
            start = time.monotonic()
            delay = config["poll_seconds"]
            try:
                poll(client, config, state, send)
                failures = 0
            except (ApiError, URLError, TimeoutError, OSError, ValueError) as exc:
                LOG.error("Poll failed: %s", exc)
                if args.once or isinstance(exc, ApiError) and exc.status in (401, 403):
                    return 1
                failures += 1
                delay = max(delay, min(900, 30 * 2 ** min(failures - 1, 5)),
                            getattr(exc, "retry_after", 0))
                start = time.monotonic()  # Retry-After starts at the failed response.
            if args.once:
                return 0
            time.sleep(max(0, delay - (time.monotonic() - start)))
    except KeyboardInterrupt:
        LOG.info("Stopped")
        return 0
    except (ApiError, ValueError, OSError, KeyError, sqlite3.Error) as exc:
        LOG.error("%s", exc)
        return 1
    finally:
        if state:
            state.db.close()
        if monitor_lock:
            monitor_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
