#!/usr/bin/env python3
"""Backfill Back/Disc/Label URLs + crops from vinyl-chart-links image-manifest.json
into the Vinyl Catalog Airtable Albums table (matched by Discogs Release ID).

Also clears Front URL when Apple Music ID is present (fronts come from AM).

Usage:
  python3 backfill_images_from_manifest.py [--manifest path] [--publish]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_BASE = "appBBdksyoPp31wPr"
ALBUMS_TABLE = "tblEdPeuuzVHUXj8G"
DEFAULT_MANIFEST = Path("/Users/aaron/Documents/GitHub/vinyl-chart-links/image-manifest.json")


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


def api(tok: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://api.airtable.com/v0/{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def list_all(tok: str, base: str) -> list[dict]:
    records: list[dict] = []
    offset = None
    while True:
        path = f"{base}/{ALBUMS_TABLE}?pageSize=100"
        if offset:
            path += f"&offset={offset}"
        page = api(tok, "GET", path)
        records.extend(page.get("records", []))
        offset = page.get("offset")
        if not offset:
            break
    return records


def crop_json(crop: dict | None) -> str | None:
    if not crop:
        return None
    return json.dumps(
        {
            "x": crop["x"],
            "y": crop["y"],
            "width": crop["width"],
            "height": crop["height"],
        },
        separators=(",", ":"),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("AIRTABLE_BASE_ID", DEFAULT_BASE))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    print(f"manifest entries: {len(manifest)}")

    tok = token()
    albums = list_all(tok, args.base)
    updates: list[dict] = []
    matched = 0
    cleared_front = 0

    for rec in albums:
        f = rec["fields"]
        fields_out: dict = {}

        # Prefer Apple Music for fronts — clear redundant Front URL.
        if f.get("Apple Music ID") and f.get("Front URL"):
            fields_out["Front URL"] = None
            cleared_front += 1

        rid = f.get("Discogs Release ID")
        if rid is not None:
            key = str(int(rid))
            entry = manifest.get(key)
            if entry:
                matched += 1
                back = entry.get("back")
                label = entry.get("label")
                record = entry.get("record")
                # Disc ↔ Label mutex — prefer Disc (record).
                if record:
                    label = None
                if back:
                    fields_out["Back URL"] = back
                if record:
                    fields_out["Disc URL"] = record
                    fields_out["Label URL"] = None
                elif label:
                    fields_out["Label URL"] = label
                    fields_out["Disc URL"] = None
                bc = crop_json(entry.get("backCrop"))
                dc = crop_json(entry.get("recordCrop")) if record else None
                lc = crop_json(entry.get("labelCrop")) if (label and not record) else None
                if bc:
                    fields_out["Back Crop"] = bc
                if dc:
                    fields_out["Disc Crop"] = dc
                if lc:
                    fields_out["Label Crop"] = lc
                # Clear opposite crop when mutex applies
                if record:
                    fields_out["Label Crop"] = None
                elif label:
                    fields_out["Disc Crop"] = None

        if fields_out:
            updates.append({"id": rec["id"], "fields": fields_out})

    print(f"albums with release ID match in manifest: {matched}")
    print(f"updates to write: {len(updates)} (cleared Front URL on {cleared_front})")
    if args.dry_run:
        return

    for i in range(0, len(updates), 10):
        batch = updates[i : i + 10]
        api(tok, "PATCH", f"{args.base}/{ALBUMS_TABLE}", {"records": batch, "typecast": True})
        if i % 50 == 0:
            print(f"  patched {min(i+10, len(updates))}/{len(updates)}")
        time.sleep(0.22)
    print("backfill done")

    if args.publish:
        import subprocess

        subprocess.check_call([sys.executable, str(Path(__file__).with_name("publish_crates.py")), "--base", args.base])


if __name__ == "__main__":
    main()
