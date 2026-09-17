# Pokefire

An eBay watchlist monitor using Scrapingdog, with locally saved listings and price history. Python 3.11+ with Uvicorn.

Application code lives in `pokefire/`: `__main__.py` starts the app, `monitor.py` runs polling, `viewer.py` serves the dashboard, and `static/` contains dashboard assets. Runtime configuration (`.env`, `watchlist.json`) and saved `data/` live in the working directory, separate from the installed package.

## Run

Put `SCRAPINGDOG_KEY` in `.env` and set the dashboard address:

```dotenv
HOST=192.168.0.239
PORT=8767
```

From the project directory, start Uvicorn and the monitor:

```sh
uv run pokefire
```

Use the server's own LAN IP for `HOST`. CLI flags `--host` and `--port` override these settings. Open http://192.168.0.239:8767 from your network. Without `--host`, the server binds to `HOST` in `.env`, or localhost by default. This dashboard provides monitor and configuration controls without authentication; bind it only on a trusted network. `--port` overrides `PORT`; existing environment variables override `.env`.

**Stop:** press Ctrl+C in the terminal running Pokefire. Uvicorn shuts down and stops its managed monitor. Start it again with the same command. It runs in the foreground and does not install or enable a system service.

To start with polling paused, add `--no-monitor`. Use the dashboard's Start and Stop buttons to control polling while Uvicorn stays running. A failed scraper appears as an error in the dashboard and can be restarted there.

If you previously installed the systemd unit, turn that instance off once before starting Uvicorn manually:

```sh
sudo systemctl disable --now pokefire
```

The configured watchlist contains 25 PSA 10 Gold Star cards. A shared, newly listed Buy It Now search runs every 120 seconds; an ending-soon auction search runs every 900 seconds. The shared query is `PSA 10 ("gold star",goldstar) -celebrations -25th -2021 -japanese -jpn`. It avoids broad standalone star and card-number matches, but can miss listings titled only with a card number or star symbol. Local matching checks each result against enabled watchlist rules. Each search retrieves one page of up to 240 listings. This is a bounded search window, so listings beyond that window can be missed. Intervals and the shared query can be changed while the monitor is stopped.

## Offers and history

Observed offers and All history use the same saved listing database. Matches load when the page opens and refresh after a new poll finishes processing, keeping the current filters and page. The existing status check detects completed polls within five seconds; it does not reload listings when no poll has completed. Observed offers defaults to last-observed-active listings, excluding listings with an explicit end time in the past. Filter by card, title, minimum/maximum USD price, auction/Buy It Now, or last known status.

Expand **View history for this card** on an offer to see all saved listings matching that watchlist card, including known sold or ended listings. Expand **Price observations for this listing** to see its recorded prices and statuses over time. Both views are paginated. Images and eBay links come from the saved search response.

Raw responses are compressed and archived before parsing; parsed snapshots and listing status changes are stored in SQLite at `data/state.sqlite3`. Unmatched results remain in raw/parsed poll archives but do not appear in the listing history. Listing dates and sold dates are retained when supplied. Missing dates remain unknown. Disappearing from a search does not mean sold, and last observed active is not a live availability guarantee.

Viewing, filtering, expanding, and refreshing saved history makes **no Scrapingdog requests**. There are no separate listing-detail, sold-status, reference-price, population, or browser-collection requests.

## Optional Discord notifications

Set `DISCORD_BOT_TOKEN` in `.env`, then configure the channel ID and enable alerts in watchlist settings. Existing results form the initial baseline unless `notify_existing` is enabled. New matching listings can then generate alerts.

## Checks

```sh
uv run python -m unittest discover -s tests
node --check pokefire/static/app.js
```

Watchlist `image_url` fields contain card artwork from TCGdex, matched by set and collector number. The page loads those images directly; listing photos still come from eBay. Artwork requires no Scrapingdog credits.

`watchlist.json` supports standalone `//` comment lines. Dashboard saves preserve these comments at the top of the file. Inline and block comments are not supported.
