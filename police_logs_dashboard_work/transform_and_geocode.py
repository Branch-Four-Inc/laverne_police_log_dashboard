#!/usr/bin/env python3
"""
Takes the raw CSV from the scraper and gets it ready for the Streamlit dashboard.
Outputs two files:
  - daily_logs.csv             (base data the app needs)
  - daily_logs_geocoded.csv    (same + lat/lon for the map)

Usage:
    python transform_and_geocode.py --input raw_scraped.csv
    python transform_and_geocode.py --input raw_scraped.csv --skip-geocode
    python transform_and_geocode.py --input raw_scraped.csv --geocode-cache geocode_cache.json

Outputs go to the current directory unless you pass --output-dir.
"""

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests


# These are the human-readable descriptions for the raw nature codes LVPD uses.
# Pulled from the nature key we built during the EDA phase — natures_found_in_key.csv.
# If new codes show up in future scrapes, add them here.
NATURE_DESCRIPTIONS = {
    "PDOBS": "Police Department Observed Suspicious Circumstances",
    "911 INVEST": "911 Investigation",
    "SUSP SUBJ": "Suspicious Subjects",
    "SUSP VEHICLE": "Suspicious Vehicle",
    "SUSP CIRCS": "Suspicious Circumstances",
    "TC NON INJURY": "Traffic Collision - Non Injury",
    "TC HR NON INJ": "Traffic Collision Hit and Run - Non Injuries",
    "470 PC FRAUD": "Fraud Report",
    "PPI": "Impounded Vehicle Private Party",
    "TRAFFIC STOP": "Traffic Stop",
    "WELFARE CHECK": "Welfare Check",
    "CITIZEN ASST": "Citizen Assist",
    "DISTURB PERSON": "Disturbance - Person",
    "DISTURB SUBJS": "Disturbance - Subjects",
    "DISTURB NOISE": "Disturbance - Noise",
    "DISTURB MUSIC": "Disturbance - Music",
    "DISTURB PARTY": "Disturbance - Party",
    "DISTURB ROAD": "Disturbance - Road",
    "DISTURB UNK": "Disturbance - Unknown",
    "DISTURB SHOTS": "Disturbance - Shots",
    "DISTURB FIREWKS": "Disturbance - Fireworks",
    "459PC BURGLARY": "Burglary",
    "459PC VEH/BURG": "Vehicle Burglary",
    "BURGLARY IP/JO": "Burglary In Progress / Just Occurred",
    "487 PC": "Grand Theft",
    "GTARPT": "Grand Theft Auto Report",
    "GRAND THEFT VEH": "Grand Theft Vehicle",
    "GTA IP/JO": "Grand Theft Auto In Progress / Just Occurred",
    "GTA LOCATED": "Stolen Vehicle Located",
    "THEFT VEHICLE": "Vehicle Theft",
    "PETTY THEFT": "Petty Theft",
    "PETTY THEFT RPT": "Petty Theft Report",
    "THEFT MAIL": "Mail Theft",
    "IDENTITY THEFT": "Identity Theft",
    "SHOPLIFTER IC": "Shoplifter In Custody",
    "594 PC GRAFFITI": "Vandalism - Graffiti",
    "594PC VANDALISM": "Vandalism",
    "VANDALISM IP/JO": "Vandalism In Progress / Just Occurred",
    "211PC ROBBERY": "Robbery",
    "ROBBERY IP/JO": "Robbery In Progress / Just Occurred",
    "245PC ADW IP/JO": "Assault with Deadly Weapon In Progress / Just Occurred",
    "245PC ADW RPT": "Assault with Deadly Weapon Report",
    "BATTERY": "Battery",
    "BATTERY IP/JO": "Battery In Progress / Just Occurred",
    "FIGHT IP/JO": "Fight In Progress / Just Occurred",
    "DUI": "Driving Under the Influence",
    "PUBLIC INTOX": "Public Intoxication",
    "DRUG ACTIVITY": "Drug Activity",
    "TRAFFIC ENFORCE": "Traffic Enforcement",
    "TRAFFIC HAZARD": "Traffic Hazard",
    "BIKE TRAFFIC": "Bicycle Traffic Stop",
    "SPEEDING VEH": "Speeding Vehicle",
    "PEDESTRIAN TRAF": "Pedestrian Traffic Stop",
    "TC INJURY": "Traffic Collision - Injury",
    "TC HIT RUN INJ": "Traffic Collision Hit and Run - Injury",
    "TC VEH VS PED": "Traffic Collision - Vehicle vs Pedestrian",
    "TC UNK": "Traffic Collision - Unknown",
    "ILLEGAL PARKER": "Illegal Parking",
    "ABAND VEH": "Abandoned Vehicle",
    "VEH TAMPERING": "Vehicle Tampering",
    "FOUND PROP": "Found Property",
    "LOST PROPERTY": "Lost Property",
    "PROP FOR DESTR": "Property for Destruction",
    "MISSING ADULT": "Missing Adult",
    "MISSING FOUND": "Missing Person Located",
    "CODE H SUBJ": "5150 Welfare and Institutions Code Subject",
    "ASSIST AGENCY": "Assist Other Agency",
    "APS REF": "Adult Protective Services Referral",
    "KEEP THE PEACE": "Keep the Peace",
    "HAIL BY CITIZEN": "Hailed by Citizen",
    "TRESPASS": "Trespassing",
    "SOLICITOR": "Solicitor",
    "ANIMAL BITE RPT": "Animal Bite Report",
    "ANIMAL CRUELTY": "Animal Cruelty",
    "ANIMAL INJURED": "Injured Animal",
    "ANIMAL VICIOUS": "Vicious Animal",
    "BARKING DOG": "Barking Dog",
    "HUMANE PROBLEM": "Humane Problem",
    "BEAR PROB": "Bear Problem",
    "AREA CHECK": "Area Check",
    "FOOT PATROL": "Foot Patrol",
    "CITE SIGN OFF": "Citation Sign Off",
    "LA VERNE MUNI": "La Verne Municipal Code Violation",
    "COMM BUILD CHK": "Commercial Building Check",
    "653M PC": "Threatening / Annoying Phone Calls",
    "INDECENT EXPOS": "Indecent Exposure",
    "BRANDISH WEAPON": "Brandishing a Weapon",
    "BOMB THREAT": "Bomb Threat",
    "PROWLER": "Prowler",
    "CRIM THRT RPT": "Criminal Threat Report",
    "ILLEGAL BURN": "Illegal Burning",
    "SUSP TRANS": "Suspicious Transaction",
    "SUSP NOISE": "Suspicious Noise",
}

# Broader category groupings — mirrors scripts/add_grouped_nature.py in the dashboard repo.
# Keep these in sync if the dashboard team updates their groupings.
GROUPS = {
    "Fraud": ["470 PC FRAUD", "537E PC DEFRAUD"],
    "Grand Theft": ["487 PC", "GTARPT", "GRAND THEFT VEH", "GTA IP/JO", "GTA LOCATED"],
    "Vehicle Theft": ["THEFT VEHICLE"],
    "Vandalism": ["594 PC GRAFFITI", "594PC VANDALISM", "VANDALISM IP/JO"],
    "Burglary": ["459PC BURGLARY", "BURGLARY IP/JO", "459PC VEH/BURG"],
    "Robbery": ["211PC ROBBERY", "ROBBERY IP/JO"],
    "ADW": ["245PC ADW IP/JO", "245PC ADW RPT"],
    "Battery": ["BATTERY", "BATTERY IP/JO"],
    "Suspicious Circumstances": ["SUSP CIRCS", "PDOBS"],
    "Suspicious Subjects": ["SUSP SUBJ"],
    "Suspicious Vehicle": ["SUSP VEHICLE"],
    "Traffic Collision": ["TC NON INJURY", "TC HR NON INJ", "TC INJURY", "TC HIT RUN INJ", "TC VEH VS PED", "TC UNK"],
    "Petty Theft": ["PETTY THEFT", "PETTY THEFT RPT"],
    "Theft": ["THEFT MAIL", "IDENTITY THEFT"],
    "Disturbance": [
        "DISTURB PERSON", "DISTURB SUBJS", "DISTURB NOISE", "DISTURB MUSIC",
        "DISTURB PARTY", "DISTURB ROAD", "DISTURB UNK", "DISTURB SHOTS", "DISTURB FIREWKS",
    ],
    "Threatening Calls": ["653M PC"],
    "Fight": ["FIGHT IP/JO"],
    "Impound": ["PPI"],
    "Traffic Stop": ["TRAFFIC STOP"],
    "Traffic Enforcement": ["TRAFFIC ENFORCE", "TRAFFIC HAZARD", "BIKE TRAFFIC", "SPEEDING VEH", "PEDESTRIAN TRAF"],
    "Parking": ["ILLEGAL PARKER", "ABAND VEH"],
    "Suspicious Activity": ["SUSP TRANS", "SUSP NOISE"],
    "Drug/Alcohol": ["DRUG ACTIVITY", "DUI", "PUBLIC INTOX"],
    "Animal": ["ANIMAL BITE RPT", "ANIMAL CRUELTY", "ANIMAL INJURED", "ANIMAL VICIOUS", "BARKING DOG", "HUMANE PROBLEM", "BEAR PROB"],
    "Property": ["FOUND PROP", "LOST PROPERTY", "PROP FOR DESTR", "VEH TAMPERING"],
    "Welfare/Assist": ["WELFARE CHECK", "CITIZEN ASST", "CODE H SUBJ", "ASSIST AGENCY", "APS REF", "KEEP THE PEACE", "HAIL BY CITIZEN"],
    "Trespass/Solicitor": ["TRESPASS", "SOLICITOR"],
    "Shoplifting": ["SHOPLIFTER IC"],
    "Other": [
        "911 INVEST", "AREA CHECK", "FOOT PATROL", "CITE SIGN OFF", "LA VERNE MUNI",
        "COMM BUILD CHK", "MISSING ADULT", "MISSING FOUND", "INDECENT EXPOS",
        "BRANDISH WEAPON", "BOMB THREAT", "PROWLER", "CRIM THRT RPT", "ILLEGAL BURN",
    ],
}

# Flatten to a simple nature -> group lookup
NATURE_TO_GROUP = {nature: group for group, natures in GROUPS.items() for nature in natures}

# Jan 1-2 PDFs had weird formatting that broke the parser, so we skip them
SKIP_DATES = {"01.01.2026", "01.02.2026"}

# Automatically set the cutoff to the last day of the previous month so we
# don't have to remember to update this manually on every monthly refresh
CUTOFF_DATE = pd.Timestamp.now().replace(day=1) - pd.Timedelta(days=1)

CITY = "La Verne, CA"


def transform(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df = df[~df["log_date"].isin(SKIP_DATES)].copy()

    # Raw scraper gives us "HH:MM:SS MM/DD/YY" — convert to a proper datetime
    df["reported"] = pd.to_datetime(df["reported"], format="%H:%M:%S %m/%d/%y", errors="coerce")
    bad = df["reported"].isna().sum()
    if bad:
        print(f"[warn] Dropping {bad} rows with unparseable 'reported' timestamps.")
    df = df.dropna(subset=["reported"]).copy()

    df = df[df["reported"] <= CUTOFF_DATE].copy()

    # App expects "YYYY-MM-DD" for the date column and "YYYY-MM-DD HH:MM:SS" for reported
    df["date"] = df["reported"].dt.date.astype(str)
    df["reported"] = df["reported"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # Falls back to the raw nature code if we don't have a description for it yet
    df["nature_description"] = df["nature"].map(NATURE_DESCRIPTIONS).fillna(df["nature"])

    df["grouped_nature"] = df["nature"].map(NATURE_TO_GROUP).fillna("Other")

    # Match the column order the app expects
    df = df[["log_date", "incident", "reported", "nature", "incident_address", "nature_description", "date", "grouped_nature"]]

    print(f"Transformed: {len(df):,} rows | {df['date'].min()} → {df['date'].max()}")

    # Helpful to know what's falling through to "Other" in case we want to add more groupings
    unmapped = df[df["grouped_nature"] == "Other"]["nature"].value_counts()
    if not unmapped.empty:
        print(f"[info] Natures mapped to 'Other' (top 10):\n{unmapped.head(10).to_string()}")

    return df.reset_index(drop=True)


# Reuse one session for all geocoding requests
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "LVPD-Dashboard-Pipeline/1.0 (non-profit newsroom research)"})


def _clean_address(addr: str) -> str:
    # Some addresses have a secondary description after a semicolon (e.g. "2269 FOOTHILL BLVD; MCDONALDS")
    # The geocoder only wants the street part
    addr = addr.split(";")[0].strip()
    return f"{addr}, {CITY}"


def _is_intersection(addr: str) -> bool:
    # Catches formats like "FOOTHILL BLVD & WHITE AVE" and "FOOTHILL BLVD / WHITE AVE"
    return " & " in addr or " / " in addr or (addr.count("/") == 1 and not addr[0].isdigit())


def _census_geocode_single(addr: str) -> tuple[float | None, float | None]:
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


def geocode_address(raw_addr: str) -> tuple[float | None, float | None]:
    addr = raw_addr.split(";")[0].strip()

    if not _is_intersection(addr):
        return _census_geocode_single(_clean_address(addr))

    # For intersections the Census geocoder doesn't handle "STREET A & STREET B" well,
    # so we geocode each street individually and average the coords as an approximation.
    # Not perfectly accurate but puts the pin close enough to the right corner.
    for sep in [" & ", " / ", "/"]:
        if sep in addr:
            parts = addr.split(sep, 1)
            break
    else:
        parts = [addr]

    coords = []
    for part in parts:
        part = part.strip()
        if part:
            lat, lon = _census_geocode_single(f"{part}, {CITY}")
            if lat is not None:
                coords.append((lat, lon))
        time.sleep(0.2)

    if len(coords) == 2:
        return (coords[0][0] + coords[1][0]) / 2, (coords[0][1] + coords[1][1]) / 2
    if len(coords) == 1:
        return coords[0]
    return None, None


def geocode_dataframe(df: pd.DataFrame, cache_path: Path) -> pd.DataFrame:
    # Load whatever we've already geocoded so we don't re-hit the API every run
    cache: dict[str, list] = {}
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)
        print(f"Loaded geocode cache: {len(cache)} addresses")

    unique_addrs = df["incident_address"].dropna().unique()
    to_geocode = [a for a in unique_addrs if a not in cache]
    print(f"Addresses to geocode: {len(to_geocode)} new / {len(unique_addrs)} total")

    for i, addr in enumerate(to_geocode, 1):
        lat, lon = geocode_address(addr)
        cache[addr] = [lat, lon]
        time.sleep(0.2)
        if i % 50 == 0:
            print(f"  Geocoded {i}/{len(to_geocode)}...")
            # Write incrementally so we don't lose everything if it gets interrupted
            with open(cache_path, "w") as f:
                json.dump(cache, f)

    with open(cache_path, "w") as f:
        json.dump(cache, f)
    print(f"Geocode cache saved: {len(cache)} addresses → {cache_path}")

    df = df.copy()
    df["lat"] = df["incident_address"].map(lambda a: cache.get(a, [None, None])[0])
    df["lon"] = df["incident_address"].map(lambda a: cache.get(a, [None, None])[1])

    geocoded = df["lat"].notna().sum()
    print(f"Geocoded: {geocoded:,}/{len(df):,} rows ({geocoded/len(df)*100:.1f}%)")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Transform raw LVPD scraper CSV for the Streamlit dashboard.")
    parser.add_argument("--input", required=True, help="Path to raw scraped CSV from lvpd_scrape_daily_logs.py")
    parser.add_argument("--output-dir", default=".", help="Directory to write output CSVs (default: current dir)")
    parser.add_argument("--geocode-cache", default="geocode_cache.json", help="Path to geocoding cache JSON (created if missing)")
    parser.add_argument("--skip-geocode", action="store_true", help="Skip geocoding and only output daily_logs.csv")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Step 1: Load raw scraper output ===")
    raw = pd.read_csv(args.input, dtype=str).fillna("")
    print(f"Loaded {len(raw):,} raw rows from {args.input}")

    print(f"\n=== Step 2: Transform ===")
    df = transform(raw)

    logs_path = out_dir / "daily_logs.csv"
    df.to_csv(logs_path, index=False)
    print(f"Saved: {logs_path}")

    if args.skip_geocode:
        print("\nSkipping geocoding (--skip-geocode flag set).")
        return

    print(f"\n=== Step 3: Geocode ===")
    cache_path = Path(args.geocode_cache)
    df_geo = geocode_dataframe(df, cache_path)

    geo_path = out_dir / "daily_logs_geocoded.csv"
    df_geo.to_csv(geo_path, index=False)
    print(f"Saved: {geo_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
