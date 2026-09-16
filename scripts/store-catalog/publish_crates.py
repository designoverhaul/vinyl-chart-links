#!/usr/bin/env python3
"""Publish Airtable Vinyl Catalog → crates-v1.json (for vinyl-chart-links CDN).

Usage:
  AIRTABLE_TOKEN=pat_xxx python3 publish_crates.py [--out path] [--push]

Env:
  AIRTABLE_TOKEN   required (or AIRTABLE_API_KEY)
  AIRTABLE_BASE_ID optional (default: Vinyl Catalog appBBdksyoPp31wPr)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = "appBBdksyoPp31wPr"
ARTISTS_TABLE = "tblJt37pdF1GzBSnb"
ALBUMS_TABLE = "tblEdPeuuzVHUXj8G"
CRATE_KEYS = ["recent", "classic_rock", "80s", "90s"]
CRATE_NAMES = {
    "recent": "Recent",
    "classic_rock": "Classic Rock",
    "80s": "80s",
    "90s": "90s",
}


def token() -> str:
    t = os.environ.get("AIRTABLE_TOKEN") or os.environ.get("AIRTABLE_API_KEY")
    if not t:
        # Fall back to Cursor mcp.json for local dev (never print the value).
        mcp = Path.home() / ".cursor" / "mcp.json"
        if mcp.exists():
            data = json.loads(mcp.read_text())
            t = data.get("mcpServers", {}).get("airtable", {}).get("env", {}).get("AIRTABLE_API_KEY")
    if not t:
        sys.exit("Set AIRTABLE_TOKEN (or AIRTABLE_API_KEY)")
    return t


def api_get(tok: str, path: str, params: dict | None = None) -> dict:
    q = f"?{urllib.parse.urlencode(params)}" if params else ""
    req = urllib.request.Request(
        f"https://api.airtable.com/v0/{path}{q}",
        headers={"Authorization": f"Bearer {tok}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def list_all(tok: str, base: str, table: str) -> list[dict]:
    records: list[dict] = []
    offset = None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        page = api_get(tok, f"{base}/{table}", params)
        records.extend(page.get("records", []))
        offset = page.get("offset")
        if not offset:
            break
    return records


def parse_rgba(s: str | None) -> list[float]:
    if not s:
        return [0.2, 0.2, 0.2, 1.0]
    parts = [p.strip() for p in s.split(",")]
    try:
        vals = [float(p) for p in parts[:4]]
        while len(vals) < 4:
            vals.append(1.0)
        return vals
    except ValueError:
        return [0.2, 0.2, 0.2, 1.0]


def parse_crop(raw) -> dict | None:
    """Accept JSON string or already-parsed dict with x,y,width,height."""
    if not raw:
        return None
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
    try:
        return {
            "x": float(data["x"]),
            "y": float(data["y"]),
            "width": float(data["width"]),
            "height": float(data["height"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def album_dto(fields: dict) -> dict:
    disc = fields.get("Disc URL")
    label = fields.get("Label URL")
    disc_crop = parse_crop(fields.get("Disc Crop"))
    label_crop = parse_crop(fields.get("Label Crop"))
    # Disc ↔ Label mutex — prefer Disc.
    if disc:
        label = None
        label_crop = None
    # Front URL is optional; apps prefer Apple Music ID for covers.
    front = fields.get("Front URL") or None
    if fields.get("Apple Music ID"):
        front = None
    return {
        "title": fields.get("Title") or "",
        "artistName": fields.get("Artist Name") or "",
        "year": int(fields.get("Year") or 0),
        "genre": fields.get("Genre") or "",
        "spineColor": parse_rgba(fields.get("Spine Color")),
        "spineAccentColor": parse_rgba(fields.get("Spine Accent")),
        "coverColor": parse_rgba(fields.get("Cover Color")),
        "appleMusicID": fields.get("Apple Music ID") or None,
        "musicBrainzID": fields.get("MusicBrainz ID") or None,
        "discogsReleaseID": int(fields["Discogs Release ID"]) if fields.get("Discogs Release ID") else None,
        "discogsMasterID": int(fields["Discogs Master ID"]) if fields.get("Discogs Master ID") else None,
        "frontURL": front,
        "backURL": fields.get("Back URL") or None,
        "discURL": disc or None,
        "labelURL": label or None,
        "backCrop": parse_crop(fields.get("Back Crop")),
        "discCrop": disc_crop,
        "labelCrop": label_crop,
    }


def build_snapshot(albums: list[dict]) -> dict:
    crates: dict = {}
    for key in CRATE_KEYS:
        rows = [
            r
            for r in albums
            if r["fields"].get("Crate") == key and r["fields"].get("Status") == "in_crate"
        ]
        rows.sort(
            key=lambda r: (
                int(r["fields"].get("Sort Order") or 0),
                r["fields"].get("Artist Name") or "",
                int(r["fields"].get("Year") or 0),
                r["fields"].get("Title") or "",
            )
        )
        by_artist: OrderedDict[str, list] = OrderedDict()
        for r in rows:
            name = r["fields"].get("Artist Name") or "Unknown"
            by_artist.setdefault(name, []).append(r)
        items = []
        for artist_name, arts in by_artist.items():
            for r in arts:
                items.append({"kind": "record", "album": album_dto(r["fields"])})
            items.append(
                {
                    "kind": "divider",
                    "dividerName": artist_name,
                    "dividerAlignment": "center",
                    "dividerCompact": False,
                }
            )
        crates[key] = {"id": key, "name": CRATE_NAMES[key], "items": items}

    return {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "crateOrder": CRATE_KEYS,
        "crates": crates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        action="append",
        default=[],
        help="Output path (repeatable). Defaults to ./crates-v1.json and known local paths.",
    )
    parser.add_argument("--base", default=os.environ.get("AIRTABLE_BASE_ID", DEFAULT_BASE))
    args = parser.parse_args()

    tok = token()
    albums = list_all(tok, args.base, ALBUMS_TABLE)
    snap = build_snapshot(albums)
    text = json.dumps(snap, indent=2) + "\n"

    defaults = [
        str(Path.cwd() / "crates-v1.json"),
        str(Path(__file__).with_name("crates-v1.json")),
    ]
    # Local app + chart-links when present
    app_res = Path(__file__).resolve().parents[2] / "Vinyl Soltion" / "Resources" / "crates-v1.json"
    chart = Path("/Users/aaron/Documents/GitHub/vinyl-chart-links/crates-v1.json")
    if app_res.parent.is_dir():
        defaults.append(str(app_res))
    if chart.parent.is_dir():
        defaults.append(str(chart))

    outs = args.out or defaults
    seen = set()
    for dest in outs:
        if dest in seen:
            continue
        seen.add(dest)
        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        print(f"wrote {p} ({p.stat().st_size} bytes)")

    for key in CRATE_KEYS:
        n = sum(1 for i in snap["crates"][key]["items"] if i["kind"] == "record")
        print(f"  {key}: {n} albums")


if __name__ == "__main__":
    main()
