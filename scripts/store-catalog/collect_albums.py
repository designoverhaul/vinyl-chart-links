#!/usr/bin/env python3
"""Grokbot collect: add albums to the Vinyl Catalog Airtable pool.

Modes:
  1. --from-json candidates.json   Load pre-chosen albums (title/artist/year/crate).
  2. --prompt "..."                Call xAI Grok to propose candidates (needs XAI_API_KEY).

For each candidate the script:
  • Looks up Apple Music / iTunes collection ID
  • Optionally searches Discogs for master/release + image URLs
  • Enforces Disc XOR Label when both image types are found (prefers Disc)
  • Creates Airtable rows with Status=pool (or in_crate if --shelve CRATE)

Env:
  AIRTABLE_TOKEN / AIRTABLE_API_KEY
  XAI_API_KEY          (for --prompt)
  DISCOGS_CONSUMER_KEY / DISCOGS_CONSUMER_SECRET (optional image + ID resolve)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "appBBdksyoPp31wPr"
ALBUMS_TABLE = "tblEdPeuuzVHUXj8G"
ARTISTS_TABLE = "tblJt37pdF1GzBSnb"


def token() -> str:
    t = os.environ.get("AIRTABLE_TOKEN") or os.environ.get("AIRTABLE_API_KEY")
    if not t:
        mcp = Path.home() / ".cursor" / "mcp.json"
        if mcp.exists():
            data = json.loads(mcp.read_text())
            t = data.get("mcpServers", {}).get("airtable", {}).get("env", {}).get("AIRTABLE_API_KEY")
    if not t:
        sys.exit("Set AIRTABLE_TOKEN")
    return t


def http_json(url: str, headers: dict | None = None, body: dict | None = None, method: str = "GET") -> dict | list:
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def itunes_lookup(artist: str, title: str) -> dict | None:
    term = urllib.parse.quote(f"{artist} {title}")
    url = f"https://itunes.apple.com/search?term={term}&entity=album&limit=5"
    try:
        data = http_json(url)
    except Exception as e:
        print(f"  iTunes error: {e}")
        return None
    for r in data.get("results", []):
        if r.get("collectionType") != "Album":
            continue
        if (r.get("trackCount") or 0) < 5:
            continue
        return {
            "appleMusicID": str(r.get("collectionId")),
            "year": int(str(r.get("releaseDate", "0"))[:4] or 0),
            "genre": r.get("primaryGenreName") or "",
            "frontURL": (r.get("artworkUrl100") or "").replace("100x100bb", "600x600bb") or None,
            "title": r.get("collectionName") or title,
            "artistName": r.get("artistName") or artist,
        }
    return None


def discogs_search(artist: str, title: str) -> dict:
    """Best-effort Discogs master/release + image URLs. Returns empty dict on miss."""
    key = os.environ.get("DISCOGS_CONSUMER_KEY")
    secret = os.environ.get("DISCOGS_CONSUMER_SECRET")
    if not key or not secret:
        return {}
    q = urllib.parse.quote(f"{artist} {title}")
    url = (
        f"https://api.discogs.com/database/search?q={q}&type=master&per_page=5"
        f"&key={key}&secret={secret}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VinylSolutionGrokbot/1.0"})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  Discogs search error: {e}")
        return {}

    results = data.get("results") or []
    if not results:
        return {}
    top = results[0]
    master_id = top.get("id")
    cover = top.get("cover_image") or top.get("thumb")
    out: dict = {
        "discogsMasterID": master_id,
        "frontURL": cover,
    }

    # Fetch master versions for a vinyl pressing + secondary images.
    try:
        murl = f"https://api.discogs.com/masters/{master_id}?key={key}&secret={secret}"
        req = urllib.request.Request(murl, headers={"User-Agent": "VinylSolutionGrokbot/1.0"})
        with urllib.request.urlopen(req) as resp:
            master = json.loads(resp.read().decode())
        images = master.get("images") or []
        # primary = front, secondary candidates for back / disc / label heuristics
        secondaries = [img for img in images if img.get("type") == "secondary"]
        if secondaries:
            out["backURL"] = secondaries[0].get("uri")
        # Prefer a full-bleed secondary as Disc if aspect is roughly square;
        # otherwise leave Label empty for Grokbot human review in Airtable.
        if len(secondaries) > 1:
            out["discURL"] = secondaries[1].get("uri")
            out["labelURL"] = None
        main = master.get("main_release")
        if main:
            out["discogsReleaseID"] = main
    except Exception as e:
        print(f"  Discogs master error: {e}")
    time.sleep(1.05)  # Discogs rate limit
    return out


def grok_propose(prompt: str, count: int, crate: str) -> list[dict]:
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        sys.exit("XAI_API_KEY required for --prompt")
    system = (
        "You are Grokbot for Vinyl Solution. Propose full studio albums (not singles/EPs) "
        f"suitable for the '{crate}' record-store crate. Return ONLY JSON array of objects: "
        '[{"title":"...","artist":"...","year":1977,"genre":"Rock"}]. '
        f"Propose exactly {count} albums. No markdown."
    )
    body = {
        "model": "grok-2-latest",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
    }
    data = http_json(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
        method="POST",
    )
    content = data["choices"][0]["message"]["content"]
    # Strip fences if present
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
    return json.loads(content)


def ensure_artist(tok: str, base: str, name: str, crate: str, cache: dict[str, str]) -> str | None:
    if name in cache:
        return cache[name]
    # Search
    formula = urllib.parse.quote(f"{{Name}}='{name.replace(chr(39), chr(92)+chr(39))}'")
    try:
        page = http_json(
            f"https://api.airtable.com/v0/{base}/{ARTISTS_TABLE}?filterByFormula={formula}&maxRecords=1",
            headers={"Authorization": f"Bearer {tok}"},
        )
        recs = page.get("records") or []
        if recs:
            cache[name] = recs[0]["id"]
            return cache[name]
    except Exception:
        pass
    created = http_json(
        f"https://api.airtable.com/v0/{base}/{ARTISTS_TABLE}",
        headers={"Authorization": f"Bearer {tok}"},
        body={"records": [{"fields": {"Name": name, "Eras": [crate if crate != "pool" else "pool"]}}], "typecast": True},
        method="POST",
    )
    rid = created["records"][0]["id"]
    cache[name] = rid
    time.sleep(0.22)
    return rid


def create_album(tok: str, base: str, fields: dict) -> None:
    http_json(
        f"https://api.airtable.com/v0/{base}/{ALBUMS_TABLE}",
        headers={"Authorization": f"Bearer {tok}"},
        body={"records": [{"fields": fields}], "typecast": True},
        method="POST",
    )
    time.sleep(0.22)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("AIRTABLE_BASE_ID", DEFAULT_BASE))
    parser.add_argument("--from-json", type=Path, help="JSON array of {title,artist,year?,genre?,crate?}")
    parser.add_argument("--prompt", type=str, help="Natural language ask for Grok")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--crate", default="pool", choices=["pool", "recent", "classic_rock", "80s", "90s"])
    parser.add_argument("--shelve", action="store_true", help="Put into --crate as in_crate instead of pool")
    parser.add_argument("--skip-discogs", action="store_true")
    args = parser.parse_args()

    if args.from_json:
        candidates = json.loads(args.from_json.read_text())
    elif args.prompt:
        candidates = grok_propose(args.prompt, args.count, args.crate)
    else:
        sys.exit("Provide --from-json or --prompt")

    tok = token()
    artist_cache: dict[str, str] = {}
    created = 0
    for raw in candidates:
        title = raw.get("title") or raw.get("Title")
        artist = raw.get("artist") or raw.get("artistName") or raw.get("Artist")
        if not title or not artist:
            print(f"skip incomplete: {raw}")
            continue
        crate = raw.get("crate") or args.crate
        print(f"+ {artist} — {title}")
        am = itunes_lookup(artist, title) or {}
        dg = {} if args.skip_discogs else discogs_search(artist, title)

        disc_url = dg.get("discURL")
        label_url = None if disc_url else dg.get("labelURL")

        fields = {
            "Title": am.get("title") or title,
            "Artist Name": am.get("artistName") or artist,
            "Year": am.get("year") or raw.get("year") or 0,
            "Genre": am.get("genre") or raw.get("genre") or "",
            "Status": "in_crate" if args.shelve and crate != "pool" else "pool",
            "Times Shelved": 1 if args.shelve else 0,
            "Spine Color": "0.2000,0.2000,0.2000,1.0000",
            "Spine Accent": "1.0000,1.0000,1.0000,1.0000",
            "Cover Color": "0.2000,0.2000,0.2000,1.0000",
        }
        if args.shelve and crate != "pool":
            fields["Crate"] = crate
        if am.get("appleMusicID"):
            fields["Apple Music ID"] = am["appleMusicID"]
        if am.get("frontURL") or dg.get("frontURL"):
            # Only store Front URL when there is no Apple Music ID.
            if not (am.get("appleMusicID") or fields.get("Apple Music ID")):
                fields["Front URL"] = am.get("frontURL") or dg.get("frontURL")
        if dg.get("backURL"):
            fields["Back URL"] = dg["backURL"]
        if disc_url:
            fields["Disc URL"] = disc_url
            fields.pop("Label URL", None)
        if label_url and not disc_url:
            fields["Label URL"] = label_url
            fields.pop("Disc URL", None)
        if dg.get("discogsReleaseID"):
            fields["Discogs Release ID"] = dg["discogsReleaseID"]
        if dg.get("discogsMasterID"):
            fields["Discogs Master ID"] = dg["discogsMasterID"]

        aid = ensure_artist(tok, args.base, fields["Artist Name"], crate if crate != "pool" else "pool", artist_cache)
        if aid:
            fields["Artist"] = [aid]

        create_album(tok, args.base, fields)
        created += 1

    print(f"created {created} albums")


if __name__ == "__main__":
    main()
