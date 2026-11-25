#!/usr/bin/env python3
"""Publish Airtable Vinyl Catalog -> catalog-v1.json for TV search.

Writes every album in the Vinyl Catalog base (crate shelves and the pool)
as a slim search index. Apps never call Airtable; they fetch this file
from the same CDN as crates-v1.json, then fall back to a cached / bundled
copy.

This script is run by .github/workflows/rotate-crates.yml after the
daily crate rotation (07:00 UTC), and can be run locally:

  python3 scripts/publish_catalog.py

Default output is catalog-v1.json at the repo root (the CDN file).
Adding a pool album does not change crates-v1.json, so this file is
rebuilt on every Action run, not only when a crate rotates.

Env:
  AIRTABLE_TOKEN   required (or AIRTABLE_API_KEY)
  AIRTABLE_BASE_ID optional (default: Vinyl Catalog)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = "appBBdksyoPp31wPr"
ALBUMS_TABLE = "tblEdPeuuzVHUXj8G"

# Airtable Crate single-select typos / migrate-window names -> canonical keys.
CRATE_ALIASES = {
    "modern-rock": "modern_rock",
    "classic_rock": "classics",
    "recent": "new_release",
}


def canonical_crate(raw: str | None) -> str | None:
    if not raw:
        return None
    return CRATE_ALIASES.get(raw, raw)


def token() -> str:
    t = os.environ.get("AIRTABLE_TOKEN") or os.environ.get("AIRTABLE_API_KEY")
    if not t:
        mcp = Path.home() / ".cursor" / "mcp.json"
        if mcp.exists():
            data = json.loads(mcp.read_text())
            servers = data.get("mcpServers") or {}
            if isinstance(servers, dict):
                for server in servers.values():
                    env = (server or {}).get("env") or {}
                    t = env.get("AIRTABLE_TOKEN") or env.get("AIRTABLE_API_KEY")
                    if t:
                        break
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


def parse_crop(raw) -> dict | None:
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


def hidden_hashes(raw) -> list[str] | None:
    if not raw:
        return None
    if isinstance(raw, list):
        lines = [str(x).strip() for x in raw]
    else:
        lines = [ln.strip() for ln in str(raw).splitlines()]
    lines = [ln for ln in lines if ln]
    return lines or None


def album_dto(fields: dict) -> dict:
    disc = fields.get("Disc URL") or None
    label = fields.get("Label URL") or None
    disc_crop = parse_crop(fields.get("Disc Crop"))
    label_crop = parse_crop(fields.get("Label Crop"))
    if disc:
        label = None
        label_crop = None
    front = fields.get("Front URL") or None
    if fields.get("Apple Music ID"):
        front = None
    am = str(fields.get("Apple Music ID") or "").strip() or None
    crate = canonical_crate(fields.get("Crate"))
    hashes = hidden_hashes(fields.get("Hidden Image Hashes"))
    year = fields.get("Year")
    try:
        year_i = int(year) if year else None
    except (TypeError, ValueError):
        year_i = None
    row = {
        "title": fields.get("Title") or "",
        "artistName": fields.get("Artist Name") or "",
        "year": year_i or None,
        "genre": fields.get("Genre") or "",
        "appleMusicID": am,
        "discogsReleaseID": int(fields["Discogs Release ID"]) if fields.get("Discogs Release ID") else None,
        "discogsMasterID": int(fields["Discogs Master ID"]) if fields.get("Discogs Master ID") else None,
        "frontURL": front,
        "backURL": fields.get("Back URL") or None,
        "discURL": disc,
        "labelURL": label,
        "backCrop": parse_crop(fields.get("Back Crop")),
        "discCrop": disc_crop,
        "labelCrop": label_crop,
        "crate": crate,
        "status": fields.get("Status") or None,
        "newRelease": bool(fields.get("New Release")),
    }
    if hashes:
        row["hiddenImageHashes"] = hashes
    return row


def prefer_shelved(existing: dict, new: dict) -> dict:
    if existing.get("status") == "in_crate" and new.get("status") != "in_crate":
        return existing
    if new.get("status") == "in_crate" and existing.get("status") != "in_crate":
        return new
    return existing


def build_snapshot(rows: list[dict]) -> dict:
    albums: list[dict] = []
    by_am: dict[str, int] = {}
    for row in rows:
        dto = album_dto(row.get("fields") or {})
        if not dto["title"] and not dto["artistName"] and not dto["appleMusicID"]:
            continue
        am = dto["appleMusicID"]
        if am and am in by_am:
            albums[by_am[am]] = prefer_shelved(albums[by_am[am]], dto)
            continue
        if am:
            by_am[am] = len(albums)
        albums.append(dto)
    albums.sort(key=lambda a: ((a.get("artistName") or "").lower(), (a.get("title") or "").lower()))
    return {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "albums": albums,
    }


def default_outs() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    return [root / "catalog-v1.json"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.environ.get("AIRTABLE_BASE_ID") or DEFAULT_BASE)
    parser.add_argument(
        "--out",
        action="append",
        type=Path,
        help="Output path (repeatable). Defaults to catalog-v1.json at the repo root.",
    )
    args = parser.parse_args()
    tok = token()
    rows = list_all(tok, args.base, ALBUMS_TABLE)
    snap = build_snapshot(rows)
    text = json.dumps(snap, ensure_ascii=False, indent=2) + "\n"
    outs = args.out or default_outs()
    seen: set[Path] = set()
    for dest in outs:
        dest = dest.expanduser().resolve()
        if dest in seen:
            continue
        seen.add(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        print(f"wrote {dest} ({len(snap['albums'])} albums, {dest.stat().st_size} bytes, generatedAt {snap['generatedAt']})")


if __name__ == "__main__":
    main()
