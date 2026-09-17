"""Dashboard routes and monitor controls for the Uvicorn application."""

import csv
import json
import hashlib
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

from pokefire.listing_store import ListingStore
from pokefire.monitor_lock import running
from pokefire.monitor import load_config, State
from pokefire.config_json import loads as load_config_json, comment_lines

ROOT = Path.cwd()
ASSETS = Path(__file__).resolve().parent / "static"


class Controller:
    def __init__(self, config_path=ROOT / "watchlist.json"):
        self.config_path = Path(config_path).resolve()
        self.lock = threading.RLock()
        self.children = {}
        self.log_dir = ROOT / "data" / "logs"

    def config(self):
        raw = self.config_path.read_bytes()
        return {"config": load_config_json(raw.decode()), "revision": hashlib.sha256(raw).hexdigest()}

    def state_path(self):
        return ROOT / load_config(self.config_path)["state_file"]

    def status(self):
        with self.lock:
            result = {}
            for source in ("ebay",):
                process = self.children.get(source)
                managed = process is not None and process.poll() is None
                active = managed or running(self.state_path(), source)
                logfile = self.log_dir / f"{source}.log"
                log = ""
                if logfile.exists():
                    with logfile.open("rb") as file:
                        file.seek(max(0, logfile.stat().st_size - 16000))
                        log = file.read().decode("utf-8", errors="replace")
                code = process.poll() if process is not None else None
                result[source] = {"running": active, "managed": managed, "exit_code": code,
                                  "state": "running" if active else "error" if code else "stopped",
                                  "log": log, "completed_poll": ListingStore(self.state_path()).completed_poll()}
            return result

    def start(self, source):
        if source not in ("ebay",):
            raise ValueError("Unknown marketplace")
        with self.lock:
            if self.status()[source]["running"]:
                raise ValueError("This marketplace is already running")
            if not any(r.get("enabled", True) for r in load_config(self.config_path)["watchlist"]):
                raise ValueError("Select at least one watchlist entry before starting")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / f"{source}.log").open("w") as log:
                self.children[source] = subprocess.Popen(
                    [sys.executable, "-u", "-m", "pokefire.monitor", "--config", str(self.config_path),
                     "--source", source], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)

    def stop(self, source):
        if source not in ("ebay",):
            raise ValueError("Unknown marketplace")
        with self.lock:
            process = self.children.get(source)
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            elif running(self.state_path(), source):
                raise ValueError("Started in a terminal: stop it there with Ctrl-C")

    def save(self, payload):
        with self.lock:
            if any(row["running"] for row in self.status().values()):
                raise ValueError("Stop the monitor before saving the watchlist")
            if payload.get("revision") != self.config()["revision"]:
                raise ValueError("Config changed elsewhere. Reload the page before saving")
            config = payload.get("config")
            if not isinstance(config, dict) or set(config) - {
                    "poll_seconds", "marketplace", "category_id", "page_size", "max_pages",
                    "state_file", "notify_existing", "environment", "query", "watchlist",
                    "discord", "ebay_poll_seconds", "auction_poll_seconds"}:
                raise ValueError("Invalid configuration fields")
            # Keep file paths under server control; the editor does not change them.
            if config.get("state_file") != self.config()["config"].get("state_file"):
                raise ValueError("Change the state file from the CLI configuration only")
            name = None
            try:
                with tempfile.NamedTemporaryFile("w", dir=self.config_path.parent, delete=False) as file:
                    name = file.name
                    file.writelines(line.rstrip('\r\n') + '\n' for line in
                                    comment_lines(self.config_path.read_text()))
                    json.dump(config, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                load_config(name)
                self.config_path.with_suffix(".json.bak").write_bytes(self.config_path.read_bytes())
                os.replace(name, self.config_path)
            finally:
                if name and Path(name).exists():
                    Path(name).unlink()





    def observations(self, source):
        if source not in ("ebay",):
            raise ValueError("Unknown marketplace")
        config = load_config(self.config_path, source)
        config["source"] = source
        config["state_file"] = str(ROOT / config["state_file"])
        if not Path(config["state_file"]).exists():
            return []
        state = State(config)
        try:
            rows = state.history()
            # Apply the newest locally stored availability evidence to older
            # price observations, without fetching listings again.
            tables = {r[0] for r in state.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'listings' in tables:
                latest = {r[0]: r[1:] for r in state.db.execute('SELECT item_id,status,last_seen_at,payload FROM listings')}
                for row in rows:
                    if row['item_id'] in latest:
                        status, observed_at, payload = latest[row['item_id']]
                        row['listingStatus'] = status
                        row['statusObservedAt'] = observed_at
                        current = json.loads(payload)
                        if current.get('itemEndDate'):
                            row['itemEndDate'] = current['itemEndDate']
            return rows
        finally:
            state.db.close()

    def close(self):
        for source, child in tuple(self.children.items()):
            if child.poll() is None:
                self.stop(source)


def read_export(path):
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("This export exceeds the viewer's 50 MB limit.")
    with path.open(encoding="utf-8-sig", newline="") as file:
        if path.suffix.lower() == ".csv":
            reader = csv.DictReader(file)
            if not {"item_id", "title", "price", "observed_at"}.issubset(reader.fieldnames or []):
                raise ValueError("Not a Pokefire export.")
            rows = list(reader)
        else:
            rows = json.load(file)
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not
            {"item_id", "title", "price", "observed_at"}.issubset(row) for row in rows):
        raise ValueError("Not a Pokefire export.")
    for row in rows:
        if isinstance(row["price"], dict):
            row["currency"] = row["price"].get("currency")
            row["price"] = row["price"].get("value")
        if isinstance(row.get("seller"), dict):
            row["seller"] = row["seller"].get("username")
        if isinstance(row.get("matches"), list):
            row["matches"] = "; ".join(row["matches"])
        row["grading_text"] = row.get("grading_text") or row.get("gradingText") or row["title"]
    return rows


def request_handler_for(data_dir, controller=None, allowed_hosts=("localhost", "127.0.0.1")):
    data_dir = Path(data_dir).resolve()
    controller = controller or Controller()

    class Handler:
        def reply(self, status, body, content_type="application/json"):
            if content_type == "application/json":
                body = json.dumps(body).encode()
            headers = {
                "content-type": content_type,
                "content-length": str(len(body)),
                "cache-control": "no-store",
                "x-content-type-options": "nosniff",
                "content-security-policy": "default-src 'self'; script-src 'self'; "
                    "style-src 'self'; img-src 'self' https://*.ebayimg.com https://ebayimg.com https://assets.tcgdex.net; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'",
            }
            self.response = (status, [(k.encode("ascii"), v.encode("latin-1"))
                                      for k, v in headers.items()], body)

        def do_GET(self):
            # Reject Host headers outside the configured server addresses.
            host = self.headers.get("Host", "").split(":")[0]
            if host not in allowed_hosts:
                return self.reply(403, {"error": "Host not allowed."})
            url = urlsplit(self.path)
            try:
                if url.path == "/api/status":
                    return self.reply(200, controller.status())
                if url.path == "/api/config":
                    return self.reply(200, controller.config())
                if url.path == "/api/listings":
                    from pokefire.listing_store import ListingStore
                    store = ListingStore(controller.state_path())
                    params = {k:v[0] for k,v in parse_qs(url.query).items()}
                    return self.reply(200, store.history_page(
                        item_id=params.get('item_id'), query=params.get('q',''),
                        card=params.get('card',''), page=int(params.get('page',1)),
                        minimum=params.get('min',''), maximum=params.get('max',''),
                        mode=params.get('type',''), status=params.get('status','')))
                if url.path == "/api/listing-history":
                    from pokefire.listing_store import ListingStore
                    params={k:v[0] for k,v in parse_qs(url.query).items()}
                    return self.reply(200, ListingStore(controller.state_path()).observations(params.get('item_id',''),page=int(params.get('page',1))))
                if url.path == "/api/observations":
                    source = parse_qs(url.query).get("source", ["ebay"])[0]
                    return self.reply(200, {"rows": controller.observations(source)})
            except (ValueError, OSError, sqlite3.Error):
                return self.reply(400, {"error": "Cannot read local configuration or observations."})
            if url.path == "/api/exports":
                name = parse_qs(url.query).get("file", [None])[0]
                if name is None:
                    files = []
                    for path in data_dir.iterdir() if data_dir.exists() else []:
                        if path.is_file() and not path.is_symlink() and path.suffix.lower() in (".csv", ".json"):
                            files.append({"name": path.name, "size": path.stat().st_size,
                                          "modified": path.stat().st_mtime})
                    return self.reply(200, sorted(files, key=lambda f: f["modified"], reverse=True))
                if Path(name).name != name or Path(name).suffix.lower() not in (".csv", ".json"):
                    return self.reply(400, {"error": "Choose a CSV or JSON export in the data directory."})
                path = data_dir / name
                if path.is_symlink() or path.resolve().parent != data_dir:
                    return self.reply(403, {"error": "File is outside the export directory."})
                try:
                    return self.reply(200, {"name": name, "rows": read_export(path)})
                except FileNotFoundError:
                    return self.reply(404, {"error": "Export not found."})
                except (ValueError, OSError, UnicodeError, csv.Error):
                    return self.reply(400, {"error": "Cannot read this export. Choose a valid Pokefire CSV or JSON file (up to 50 MB)."})
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/pokefire.png": ("pokefire.png", "image/png"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if url.path in assets:
                name, mime = assets[url.path]
                return self.reply(200, (ASSETS / name).read_bytes(), mime)
            self.reply(404, {"error": "Not found."})

        def do_POST(self):
            host = self.headers.get("Host", "")
            if host.split(":")[0] not in allowed_hosts or self.headers.get("Origin") != "http://" + host:
                return self.reply(403, {"error": "Use the control panel from the same origin for this action."})
            if self.headers.get("Content-Type") != "application/json":
                return self.reply(415, {"error": "JSON required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256000:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid request")
                if self.path == "/api/start":
                    controller.start(payload.get("source"))
                elif self.path == "/api/stop":
                    controller.stop(payload.get("source"))
                elif self.path == "/api/config":
                    controller.save(payload)
                    return self.reply(200, controller.config())
                else:
                    return self.reply(404, {"error": "Not found"})
                return self.reply(200, controller.status())
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.reply(400, {"error": str(exc)})

    return Handler
