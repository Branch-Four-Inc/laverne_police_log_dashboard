#!/usr/bin/env python3
"""
Real run (not a test): geocode every address Census couldn't resolve, using OpenAI +
web search, EXCLUDING addresses that are only ever tied to an "ASSIST AGENCY" call
(those might be in a different city entirely -- see assist_agency_excluded_from_geocoding.csv).

Results get written into the same geocode_cache.json the regular pipeline already
reads from, so this is a one-time cost -- future monthly refreshes just reuse the cache.

Queue comes from openai_geocode_queue.csv (built by a one-off script, sitting one
level up from this folder).

Run:
    OPENAI_API_KEY="sk-..." python3 openai_geocode_backfill.py
"""

import argparse
import os
import re
import sys
import time
import json
from pathlib import Path

import pandas as pd

from utils import get_logger

LOGGER = get_logger()
client = None  # built lazily in main() so importing this file never needs the API key
MODEL = "gpt-4.1-mini"

HERE = Path(__file__).resolve().parent
QUEUE_CSV = HERE.parent / "openai_geocode_queue.csv"
CACHE_PATH = HERE / "output" / "geocode_cache.json"
# Separate from geocode_cache.json on purpose -- that file can't tell "never tried" apart
# from "tried and failed" (both look like [None, None]), which matters a lot once a run
# gets interrupted partway. This tracks exactly what THIS backfill has already attempted,
# success or not, so a restart never re-pays for an address twice.
PROGRESS_PATH = HERE / "output" / "openai_backfill_progress.json"

# Rough box around La Verne + the neighboring cities that actually show up in these logs
# (Claremont, San Dimas, Glendora, Pomona). Anything outside this is almost certainly a
# wrong-city guess, not a real result -- we already caught two Census-geocoded corners
# hundreds of miles off using this same check, so OpenAI gets held to the same standard.
LAT_RANGE = (33.95, 34.30)
LON_RANGE = (-117.95, -117.65)

COORD_RE = re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")


def geocode_via_openai(raw_address: str):
    # Just business-name suffixes left at this point (we already pulled out the
    # Assist-Agency-only addresses, which were the ones actually needing a city hint)
    address = raw_address.split(";")[0].strip()

    prompt = (
        f"Find the latitude and longitude of this address: \"{address}, La Verne, CA\". "
        f"The address may have a typo, missing digits, or odd punctuation from a scanned "
        f"police log -- use your best judgment for what real address it's most likely "
        f"referring to in or near La Verne. Respond with ONLY the coordinates as decimal "
        f"degrees in the format: lat, lon -- nothing else. If you truly cannot find it, "
        f"respond with: NONE"
    )

    response = client.responses.create(
        model=MODEL,
        input=[
            {
                "role": "developer",
                "content": "You are a geocoding assistant. Reply with only the requested coordinates or NONE. No bullet points, no explanation, no extra text, no markdown or links.",
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        ],
        tools=[
            {
                "type": "web_search_preview",
                "user_location": {"type": "approximate", "country": "US"},
                "search_context_size": "low",
            }
        ],
        temperature=0.15,
        max_output_tokens=50,
        top_p=1,
        store=False,
    )

    text = response.output_text.strip()
    tokens = response.usage.total_tokens

    match = COORD_RE.search(text)
    if not match:
        return None, None, "unparseable", tokens

    lat, lon = float(match.group(1)), float(match.group(2))
    if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
        return None, None, f"out_of_range ({lat}, {lon})", tokens

    return lat, lon, "ok", tokens


def _norm_addr(addr: str) -> str:
    # Same normalization as transform_and_geocode.py -- cache keys are stripped + lowercase
    return str(addr).strip().lower()


def _has_coords(entry) -> bool:
    return bool(entry) and len(entry) == 2 and entry[0] is not None and entry[1] is not None


def normalize_cache(raw_cache: dict) -> dict:
    """Re-key the cache by normalized address. If two keys collide, keep the one with coordinates."""
    out = {}
    for addr, val in raw_cache.items():
        key = _norm_addr(addr)
        if key not in out or (_has_coords(val) and not _has_coords(out[key])):
            out[key] = val
    return out


def main():
    global client, CACHE_PATH, PROGRESS_PATH

    parser = argparse.ArgumentParser(description="Backfill missing geocodes using OpenAI + web search.")
    parser.add_argument("--queue-csv", default=str(QUEUE_CSV),
                        help="CSV with an incident_address column (default: openai_geocode_queue.csv).")
    parser.add_argument("--geocoded-csv", default=None,
                        help="Build the queue from addresses with null lat/lon in this geocoded CSV "
                             "(overrides --queue-csv).")
    parser.add_argument("--cache", default=str(CACHE_PATH), help="Path to geocode_cache.json.")
    parser.add_argument("--progress", default=str(PROGRESS_PATH), help="Path to the backfill progress JSON.")
    args = parser.parse_args()

    CACHE_PATH = Path(args.cache)
    PROGRESS_PATH = Path(args.progress)

    if not os.environ.get("OPENAI_API_KEY"):
        msg = "OPENAI_API_KEY is not set; cannot run the OpenAI backfill."
        LOGGER.error(msg)
        sys.exit(msg)
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    if args.geocoded_csv:
        geo = pd.read_csv(args.geocoded_csv)
        missing = geo[geo["lat"].isna() | geo["lon"].isna()]
        full_queue = missing["incident_address"].dropna().drop_duplicates().tolist()
    else:
        full_queue = pd.read_csv(args.queue_csv)["incident_address"].dropna().tolist()

    cache = {}
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            cache = normalize_cache(json.load(f))

    # Use coordinates already saved in the cache first -- only call OpenAI for addresses
    # with no usable cached lat/lon. Cache keys are normalized (stripped, lowercase).
    already_cached = [a for a in full_queue if _has_coords(cache.get(_norm_addr(a)))]
    full_queue = [a for a in full_queue if not _has_coords(cache.get(_norm_addr(a)))]
    print(f"{len(already_cached)} addresses already have cached coordinates; skipping those")
    LOGGER.info("OpenAI backfill: %d addresses satisfied from cache, %d need lookup",
                len(already_cached), len(full_queue))

    progress = {}
    if PROGRESS_PATH.exists():
        with open(PROGRESS_PATH) as f:
            progress = json.load(f)

    # Progress keys were saved in whatever case the address had at the time; compare normalized
    attempted = {_norm_addr(a) for a in progress}
    queue = [a for a in full_queue if _norm_addr(a) not in attempted]
    print(f"Queue: {len(full_queue)} total, {len(progress)} already attempted, {len(queue)} remaining")
    LOGGER.info("OpenAI backfill queue: %d total, %d remaining", len(full_queue), len(queue))

    resolved = out_of_range = unparseable = 0
    total_tokens = 0
    consecutive_errors = 0

    for i, addr in enumerate(queue, 1):
        try:
            lat, lon, status, tokens = geocode_via_openai(addr)
            total_tokens += tokens
            consecutive_errors = 0

            cache[_norm_addr(addr)] = [lat, lon]
            progress[addr] = status
            if status == "ok":
                resolved += 1
            elif status.startswith("out_of_range"):
                out_of_range += 1
            else:
                unparseable += 1

        except Exception as exc:
            print(f"  [error] {addr}: {exc}")
            consecutive_errors += 1
            if consecutive_errors >= 3:
                print("\n3 errors in a row -- stopping early instead of burning through the rest of the queue.")
                break
            time.sleep(2)
            continue

        # Save every request, not just every N -- this run has already been interrupted
        # once, cheap enough to just always persist rather than risk losing a batch.
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
        with open(PROGRESS_PATH, "w") as f:
            json.dump(progress, f)

        if i % 25 == 0 or i == len(queue):
            print(f"  {i}/{len(queue)} — resolved={resolved} out_of_range={out_of_range} unparseable={unparseable} (~{total_tokens:,} tokens so far)")

        time.sleep(0.5)

    print(f"\nDone this run. resolved={resolved} out_of_range={out_of_range} unparseable={unparseable} total_tokens={total_tokens:,}")
    LOGGER.info("OpenAI backfill done: resolved=%d out_of_range=%d unparseable=%d total_tokens=%d",
                resolved, out_of_range, unparseable, total_tokens)
    print(f"Cache saved: {CACHE_PATH}")
    print(f"Progress saved: {PROGRESS_PATH} ({len(progress)}/{len(full_queue)} of full queue attempted)")
    print("Check platform.openai.com/usage for the actual dollar amount billed for this run.")


if __name__ == "__main__":
    main()
