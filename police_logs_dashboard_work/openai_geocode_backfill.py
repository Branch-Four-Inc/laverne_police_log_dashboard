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

import os
import re
import time
import json
from pathlib import Path

import pandas as pd
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
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


def main():
    full_queue = pd.read_csv(QUEUE_CSV)["incident_address"].dropna().tolist()

    cache = {}
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            cache = json.load(f)

    progress = {}
    if PROGRESS_PATH.exists():
        with open(PROGRESS_PATH) as f:
            progress = json.load(f)

    queue = [a for a in full_queue if a not in progress]
    print(f"Queue: {len(full_queue)} total, {len(progress)} already attempted, {len(queue)} remaining")

    resolved = out_of_range = unparseable = 0
    total_tokens = 0
    consecutive_errors = 0

    for i, addr in enumerate(queue, 1):
        try:
            lat, lon, status, tokens = geocode_via_openai(addr)
            total_tokens += tokens
            consecutive_errors = 0

            cache[addr] = [lat, lon]
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
    print(f"Cache saved: {CACHE_PATH}")
    print(f"Progress saved: {PROGRESS_PATH} ({len(progress)}/{len(full_queue)} of full queue attempted)")
    print("Check platform.openai.com/usage for the actual dollar amount billed for this run.")


if __name__ == "__main__":
    main()
