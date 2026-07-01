# vinyl-chart-links

Auto-generated data cache for the [Vinyl Solution](https://apps.apple.com) iOS app.

A daily GitHub Action ([.github/workflows/sync.yml](.github/workflows/sync.yml)) fetches Apple Music's
"most played albums" chart, filters it to full albums, and searches Discogs for each one's master
release + best physical pressing. The result is committed to [`chart-links.json`](chart-links.json).

The app fetches this file directly (unauthenticated `raw.githubusercontent.com` read) instead of every
device independently querying Discogs — Discogs auth is a single app-wide credential shared by every
install, so centralizing the search here avoids each device re-discovering the same matches.

No app source code lives in this repo.
