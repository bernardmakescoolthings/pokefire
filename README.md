# Pokefire

An eBay watchlist monitor using Scrapingdog, with locally saved listings and price history. Python 3.11+; no third-party Python dependencies.

## Run

Put `SCRAPINGDOG_KEY` in `.env`, then start the local viewer:

```sh
uv run python -m modules.viewer --port 8767
```

Run this from the project directory. `uv run` creates the local virtual environment automatically. Without uv, use `python3 -m modules.viewer --port 8767` with Python 3.11+; no package installation is needed.

Open http://127.0.0.1:8767 and use **Start monitor** to begin polling. Starting the viewer alone does not poll eBay. Alternatively, run `uv run python pokefire.py` in a terminal.

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
node --check viewer/app.js
```

Watchlist `image_url` fields contain card artwork from TCGdex, matched by set and collector number. The page loads those images directly; listing photos still come from eBay. Artwork requires no Scrapingdog credits.
