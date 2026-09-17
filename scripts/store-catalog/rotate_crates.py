#!/usr/bin/env python3
"""Rotate ~1/8 of each in-crate album set every run (intended every 2 days).

For each crate:
  1. Select ~1/8 of currently shelved albums (prefer whole artist blocks).
  2. Move them to Status=pool and clear Crate.
  3. Pull replacement albums from Status=pool (same era when possible) into the crate.

Then call publish_crates.py (or --publish) to write crates-v1.json.

Env: AIRTABLE_TOKEN / AIRTABLE_API_KEY, optional AIRTABLE_BASE_ID
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = "appBBdksyoPp31wPr"
ALBUMS_TABLE = "tblEdPeuuzVHUXj8G"
CRATE_KEYS = ["recent", "classic_rock", "80s", "90s"]
FRACTION = 1 / 8


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


def patch_batch(tok: str, base: str, updates: list[dict]) -> None:
    for i in range(0, len(updates), 10):
        api(tok, "PATCH", f"{base}/{ALBUMS_TABLE}", {"records": updates[i : i + 10], "typecast": True})
        time.sleep(0.22)


def artist_blocks(records: list[dict]) -> list[list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for r in records:
        name = r["fields"].get("Artist Name") or "Unknown"
        if name not in by:
            order.append(name)
        by[name].append(r)
    return [by[n] for n in order]


def rotate_crate(tok: str, base: str, crate: str, all_albums: list[dict], rng: random.Random) -> tuple[int, int]:
    shelved = [
        r
        for r in all_albums
        if r["fields"].get("Crate") == crate and r["fields"].get("Status") == "in_crate"
    ]
    pool = [r for r in all_albums if r["fields"].get("Status") == "pool"]
    # Prefer pool albums that previously lived in this crate or have no crate history.
    pool_pref = [
        r
        for r in pool
        if not r["fields"].get("Crate") or r["fields"].get("Notes", "").find(crate) >= 0
    ] or pool

    if not shelved:
        print(f"  {crate}: empty — skip")
        return 0, 0
    if not pool:
        print(f"  {crate}: no pool inventory — skip (run collect_albums.py first)")
        return 0, 0

    target = max(1, int(round(len(shelved) * FRACTION)))
    blocks = artist_blocks(shelved)
    rng.shuffle(blocks)

    remove: list[dict] = []
    for block in blocks:
        if len(remove) >= target:
            break
        remove.extend(block)

    remove_ids = {r["id"] for r in remove}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # How many artists / albums to pull in — match album count removed.
    need = len(remove)
    pool_blocks = artist_blocks(pool_pref)
    rng.shuffle(pool_blocks)
    add: list[dict] = []
    for block in pool_blocks:
        if len(add) >= need:
            break
        # Don't re-add albums we're simultaneously removing.
        clean = [r for r in block if r["id"] not in remove_ids]
        add.extend(clean)

    updates: list[dict] = []
    for r in remove:
        updates.append(
            {
                "id": r["id"],
                "fields": {
                    "Status": "pool",
                    "Crate": None,
                    "Last Shelved At": now,
                },
            }
        )
    for r in add[:need]:
        times = int(r["fields"].get("Times Shelved") or 0) + 1
        updates.append(
            {
                "id": r["id"],
                "fields": {
                    "Status": "in_crate",
                    "Crate": crate,
                    "Last Shelved At": now,
                    "Times Shelved": times,
                },
            }
        )

    if updates:
        patch_batch(tok, base, updates)
    print(f"  {crate}: removed {len(remove)} albums, added {min(len(add), need)}")
    return len(remove), min(len(add), need)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("AIRTABLE_BASE_ID", DEFAULT_BASE))
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--publish", action="store_true", help="Run publish_crates.py after rotation")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tok = token()
    rng = random.Random(args.seed)
    albums = list_all(tok, args.base)
    print(f"loaded {len(albums)} albums")

    if args.dry_run:
        for crate in CRATE_KEYS:
            shelved = [
                r
                for r in albums
                if r["fields"].get("Crate") == crate and r["fields"].get("Status") == "in_crate"
            ]
            print(f"  {crate}: {len(shelved)} in crate → would rotate ~{max(1, int(round(len(shelved)*FRACTION)))}")
        return

    for crate in CRATE_KEYS:
        rotate_crate(tok, args.base, crate, albums, rng)
        # Refresh local list so later crates see updated pool membership.
        albums = list_all(tok, args.base)

    if args.publish:
        script = Path(__file__).with_name("publish_crates.py")
        subprocess.check_call([sys.executable, str(script), "--base", args.base])


if __name__ == "__main__":
    main()
