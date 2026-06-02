#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "geopy"]
# ///
"""
Geocode incident_address -> lat/lon using Nominatim.
Appends columns: lat, lon to daily_logs.csv
Saves result to daily_logs_geocoded.csv
"""

import time

import pandas as pd
from geopy.exc import GeocoderTimedOut
from geopy.geocoders import Nominatim

CITY_SUFFIX = ", La Verne, CA"
geolocator = Nominatim(user_agent="police-logs-geocoder")


def clean_address(addr: str) -> str:
    """Strip business names (after semicolon) and append city."""
    addr = addr.split(";")[0].strip()
    return addr + CITY_SUFFIX


def geocode(addr: str) -> dict:
    try:
        loc = geolocator.geocode(addr, timeout=10)
        if not loc:
            return {}
        return {"lat": loc.latitude, "lon": loc.longitude}
    except GeocoderTimedOut:
        return {}


def main():
    df = pd.read_csv("daily_logs.csv")

    # Deduplicate addresses to minimize API calls
    unique_addrs = df["incident_address"].dropna().unique()
    cache = {}

    print(f"Geocoding {len(unique_addrs)} unique addresses...")
    for i, addr in enumerate(unique_addrs):
        cache[addr] = geocode(clean_address(addr))
        time.sleep(1.1)  # Nominatim rate limit: 1 req/sec
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(unique_addrs)}")

    for col in ("lat", "lon"):
        df[col] = df["incident_address"].map(lambda a: cache.get(a, {}).get(col))

    df.to_csv("daily_logs_geocoded.csv", index=False)
    print(f"Saved daily_logs_geocoded.csv ({len(df)} rows)")


if __name__ == "__main__":
    main()
