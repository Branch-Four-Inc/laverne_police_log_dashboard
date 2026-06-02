#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "requests"]
# ///
"""Fill missing lat/lon in daily_logs_geocoded.csv using the Census Geocoder."""

import time

import pandas as pd
import requests

CITY = "La Verne, CA"
SESSION = requests.Session()


def clean(addr: str) -> str:
    addr = addr.split(";")[0].strip()
    # Convert intersections: & / / → ' and '
    for sep in [" & ", " / ", "/"]:
        if sep in addr:
            addr = addr.replace(sep, " and ")
            break
    return f"{addr}, {CITY}"


def census_geocode(addr: str) -> tuple[float, float] | tuple[None, None]:
    try:
        r = SESSION.get(
            "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
            params={"address": addr, "benchmark": "2020", "format": "json"},
            timeout=10,
        )
        matches = r.json()["result"]["addressMatches"]
        if matches:
            c = matches[0]["coordinates"]
            return c["y"], c["x"]  # lat, lon
    except Exception:
        pass
    return None, None


def main():
    df = pd.read_csv("daily_logs_geocoded.csv")
    missing = df[df["lat"].isna()]["incident_address"].dropna().unique()
    print(f"Filling {len(missing)} unmatched addresses via Census Geocoder...")

    cache: dict[str, tuple] = {}
    for i, addr in enumerate(missing):
        cache[addr] = census_geocode(clean(addr))
        time.sleep(0.2)  # Census has no strict rate limit but be polite
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(missing)}")

    filled = sum(1 for v in cache.values() if v[0] is not None)
    print(f"Resolved {filled}/{len(missing)}")

    for addr, (lat, lon) in cache.items():
        if lat is not None:
            mask = df["incident_address"] == addr
            df.loc[mask, "lat"] = lat
            df.loc[mask, "lon"] = lon

    df.to_csv("daily_logs_geocoded.csv", index=False)
    print(f"Updated daily_logs_geocoded.csv — total with coords: {df['lat'].notna().sum()}/{len(df)}")


if __name__ == "__main__":
    main()
