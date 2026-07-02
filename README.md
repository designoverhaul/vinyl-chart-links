# vinyl-chart-links

Auto-generated data cache for the [Vinyl Solution](https://apps.apple.com) iOS app.

A daily GitHub Action ([.github/workflows/sync.yml](.github/workflows/sync.yml)) fetches Apple Music's
"most played albums" chart, filters it to full albums, and searches Discogs for each one's master
release + best physical pressing. The result is committed to [`chart-links.json`](chart-links.json).

The app fetches this file directly (unauthenticated `raw.githubusercontent.com` read) instead of every
device independently querying Discogs — Discogs auth is a single app-wide credential shared by every
install, so centralizing the search here avoids each device re-discovering the same matches.

## image-manifest.json

Manually curated (not auto-generated). The app's DEBUG-only Discogs Image Curator tool lets the
developer crop/position back-cover, label, and record images per release, plus override which
pressing an album uses. Tapping "Publish to Users" in that tool pushes the current selections
straight to this file via GitHub's Contents API. Every install's `DiscogsImageCache.prewarmAll()`
fetches it (24h client-side cache) and applies the same crop locally — no image files live here,
just `{releaseID: {back: url, backCrop: rect, label: url, labelCrop: rect, record: url, recordCrop: rect}}`.

This is the *only* path curated Trending/Chart art reaches real users. An iCloud copy also exists,
but that's a private per-Apple-ID container — it only syncs across the developer's own devices.

No app source code lives in this repo.
