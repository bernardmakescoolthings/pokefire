# Pokefire

An eBay watchlist monitor using Scrapingdog, with locally saved listings and price history. Python 3.11+; no third-party Python dependencies.

Application code lives in `pokefire/`: `__main__.py` starts the app, `monitor.py` runs polling, `viewer.py` serves the dashboard, and `static/` contains dashboard assets. Runtime configuration (`.env`, `watchlist.json`) and saved `data/` live in the working directory, separate from the installed package.

## Run

Put `SCRAPINGDOG_KEY` and `PORT=8767` in `.env`, then start the dashboard and scraper together:

```sh
uv run pokefire
```

Run this from the project directory. `uv run` creates the local virtual environment automatically. Without uv, use `python3 -m pokefire` with Python 3.11+; no package installation is needed. `--port` overrides `PORT`; existing environment variables override `.env`. If no port is configured, the default is 8765. Restart the app after changing the port.

Open http://127.0.0.1:8767 to view results and control the monitor. Polling begins automatically. Ctrl+C or SIGTERM stops both processes. If the scraper fails, the application exits with an error so a service manager can restart it. Stopping the monitor in the dashboard pauses polling until you start it again or restart the application.

For the dashboard alone, run `uv run python -m pokefire.viewer`. For the scraper alone, run `uv run python -m pokefire.monitor`.

### Remote Linux host

Place the checkout at `/opt/pokefire`, owned by a dedicated `pokefire` user, and set `SCRAPINGDOG_KEY` and `PORT=8767` in `/opt/pokefire/.env`. Create the environment and install the supplied systemd unit:

```sh
cd /opt/pokefire
uv sync
sudo cp deploy/pokefire.service /etc/systemd/system/pokefire.service
sudo systemctl daemon-reload
sudo systemctl enable --now pokefire
```

The service starts on boot and restarts after failures. Adjust `User`, `WorkingDirectory`, and `ExecStart` in the unit if using another user or checkout location. Check service logs with `journalctl -u pokefire -f`; scraper logs are in `data/logs/ebay.log`. Keep the checkout and `data/` on persistent storage.

The dashboard binds to localhost. Access it from your computer through SSH:

```sh
ssh -N -L 8767:127.0.0.1:8767 your-user@your-host
```

Then open http://127.0.0.1:8767 on your computer.

The configured watchlist contains 25 PSA 10 Gold Star cards. A shared, newly listed Buy It Now search runs every 120 seconds; an ending-soon auction search runs every 900 seconds. Local matching checks each result against enabled watchlist rules. Each search retrieves one page of up to 240 listings. This is a bounded search window, so listings beyond that window can be missed. Intervals and the shared query can be changed while the monitor is stopped.

## Offers and history

Observed offers and All history use the same saved listing database. Matches load when the page opens and refresh after a new poll finishes processing, keeping the current filters and page. The existing status check detects completed polls within five seconds; it does not reload listings when no poll has completed. Observed offers defaults to last-observed-active listings, excluding listings with an explicit end time in the past. Filter by card, title, minimum/maximum USD price, auction/Buy It Now, or last known status.

Expand **View history for this card** on an offer to see all saved listings matching that watchlist card, including known sold or ended listings. Expand **Price observations for this listing** to see its recorded prices and statuses over time. Both views are paginated. Images and eBay links come from the saved search response.

Raw responses are compressed and archived before parsing; parsed snapshots and listing status changes are stored in SQLite at `data/state.sqlite3`. Unmatched results remain in raw/parsed poll archives but do not appear in the listing history. Listing dates and sold dates are retained when supplied. Missing dates remain unknown. Disappearing from a search does not mean sold, and last observed active is not a live availability guarantee.

Viewing, filtering, expanding, and refreshing saved history makes **no Scrapingdog requests**. There are no separate listing-detail, sold-status, reference-price, population, or browser-collection requests.

## Optional Discord notifications

Set `DISCORD_BOT_TOKEN` in `.env`, then configure the channel ID and enable alerts in watchlist settings. Existing results form the initial baseline unless `notify_existing` is enabled. New matching listings can then generate alerts.

## Checks

```sh
python -m unittest discover -s tests
node --check pokefire/static/app.js
```

Watchlist `image_url` fields contain card artwork from TCGdex, matched by set and collector number. The page loads those images directly; listing photos still come from eBay. Artwork requires no Scrapingdog credits.
