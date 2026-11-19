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
    python transform_and_geocode.py --input raw_scraped.csv --refresh-osm   # re-download the street data
    python transform_and_geocode.py --input raw_scraped.csv --allow-validation-failures

Outputs go to the current directory unless you pass --output-dir.

The raw data gets validated (validation.py) before anything is written. If it fails, the script
exits with code 2 and the CSVs and the geocode cache are left exactly as they were.
"""

import argparse
import json
import math
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests
from utils import get_logger, install_exception_logger
from validation import DataValidationError, previous_month_end, validate_raw_data

# distinct from a crash (1) so whatever runs this later can tell "bad data" from "broken script"
VALIDATION_FAILED_EXIT_CODE = 2


LOGGER = get_logger()
install_exception_logger(LOGGER)


# read nature descriptions from file 
# (located relative to this file, not the current working directory, so it works from any folder;
# the file lives in the repo root, one level above this script)
NATURE_DESCRIPTIONS_PATH = Path(__file__).resolve().parent.parent / "nature_descriptions.xlsx"
desc = pd.read_excel(NATURE_DESCRIPTIONS_PATH, index_col = 0)
desc = desc.rename(columns = {"Code": "nature", 
                       "Incident Type": "grouped_nature"})

# The scraped nature values are lowercase ("disturb noise") but the codes in the spreadsheet are
# uppercase ("DISTURB NOISE"), so normalize both sides to lowercase/stripped before matching.
desc["nature"] = desc["nature"].astype(str).str.strip().str.lower()

# Build a dict mapping nature -> grouped_nature
NATURE_TO_GROUP = dict(zip(desc["nature"], desc["grouped_nature"]))

NATURE_DESCRIPTIONS = dict(zip(desc["nature"], desc['Description'] ))

# Jan 1-2 PDFs had weird formatting that broke the parser, so we skip them
SKIP_DATES = {"01.01.2026", "01.02.2026"}

# Automatically set the cutoff to the end of the previous month so we don't have to remember to
# update this manually on every monthly refresh. Last SECOND of the month on purpose -- the old
# version kept the current time of day, so a 4:40 PM run cut off the rest of the last day.
CUTOFF_DATE = previous_month_end()

CITY = "La Verne, CA"


def transform(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df = df[~df["log_date"].isin(SKIP_DATES)].copy()

    # Raw scraper gives us "HH:MM:SS MM/DD/YY" — convert to a proper datetime
    df["reported"] = pd.to_datetime(df["reported"], format="%H:%M:%S %m/%d/%y", errors="coerce")
    bad = df["reported"].isna().sum()
    if bad:
        print(f"[warn] Dropping {bad} rows with unparseable 'reported' timestamps.")
        LOGGER.warning("Dropping %s rows with unparseable reported timestamps", bad)
    df = df.dropna(subset=["reported"]).copy()

    df = df[df["reported"] <= CUTOFF_DATE].copy()

    # Duplicate incident IDs (LVPD repeats the tail of one day in the next day's PDF) are
    # dropped and counted in validation.py now, so by the time we get here there are none.

    # App expects "YYYY-MM-DD" for the date column and "YYYY-MM-DD HH:MM:SS" for reported
    df["date"] = df["reported"].dt.date.astype(str)
    df["reported"] = df["reported"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # Falls back to the raw nature code if we don't have a description for it yet
    nature_key = df["nature"].astype(str).str.strip().str.lower()
    df["nature_description"] = nature_key.map(NATURE_DESCRIPTIONS).fillna(df["nature"])

    df["grouped_nature"] = nature_key.map(NATURE_TO_GROUP).fillna("Other")

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
    time.sleep(0.2)  # be polite to the Census API
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


# ── Intersections (OpenStreetMap / Overpass) ──────────────────────────────────
# The Census geocoder can't do "STREET A & STREET B", and geocoding each street separately gives an
# arbitrary point somewhere along a long street. OpenStreetMap stores streets as ways that share a
# node wherever they cross, so we ask Overpass for the node shared by the two streets.

# Search area for the Overpass query only (Overpass needs some area to search in). Results are not
# checked against it afterwards.
OVERPASS_SEARCH_BBOX = {"south": 34.06, "north": 34.16, "west": -117.83, "east": -117.69}
LA_VERNE_CENTER = (34.1008, -117.7678)

OVERPASS_URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

# Police logs abbreviate; OSM spells street types out ("Foothill Boulevard")
STREET_SUFFIXES = {
    "BLVD": "Boulevard", "HWY": "Highway", "AVE": "Avenue", "ST": "Street", "DR": "Drive", "RD": "Road",
    "PKWY": "Parkway", "LN": "Lane", "CT": "Court", "PL": "Place", "CIR": "Circle", "TER": "Terrace",
    "TRL": "Trail", "FWY": "Freeway", "WAY": "Way", "AV": "Avenue", "WY": "Way",
}
# the suffix words themselves, so "E ST" (E Street) isn't mistaken for "East" + a suffix
SUFFIX_WORDS = set(STREET_SUFFIXES) | {v.upper() for v in STREET_SUFFIXES.values()}
DIRECTIONS = {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST"}

# Whole-name spellings the log uses that don't follow the pattern above. Keys are upper case with
# punctuation removed; values are the OSM name. Only streets where the match is unambiguous.
STREET_ALIASES = {
    # bare or truncated names of the main streets
    "WHITE": "White Avenue", "ARROW": "Arrow Highway", "WHEELER": "Wheeler Avenue",
    "FOOTHILL": "Foothill Boulevard", "FOOTHIL": "Foothill Boulevard", "GAREY": "Garey Avenue",
    "BASELINE": "Baseline Road", "BASELINE AVE": "Baseline Road", "BASE LINE": "Baseline Road",
    "BONITA": "Bonita Avenue", "DAMIEN": "Damien Avenue", "PUENTE": "Puente Street",
    "TOWNE CENTER": "Towne Center Drive", "ORANGE GROVE": "Orange Grove Avenue",
    "SAN DIMAS CYN": "San Dimas Canyon Road", "SAN DIMAS CNYN": "San Dimas Canyon Road",
    # the 210 and 10 freeways are named, not numbered, in OSM
    "IS210": "Foothill Freeway", "IS 210": "Foothill Freeway", "IS10": "San Bernardino Freeway",
    "IS 10": "San Bernardino Freeway",
}

# The logs spell numbered streets out ("EIGHTH ST"); OSM mostly uses digits ("8th Street"). We use the
# word form as the canonical name and match either one in OSM.
ORDINALS = {"first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th", "sixth": "6th",
            "seventh": "7th", "eighth": "8th", "ninth": "9th", "tenth": "10th", "eleventh": "11th",
            "twelfth": "12th"}
ORDINALS_REVERSE = {v: k for k, v in ORDINALS.items()}

def _normalize_street(street: str) -> str | None:
    """'FOOTHILL BLVD' -> 'Foothill Boulevard'; 'EIGHTH ST' -> 'Eighth Street'; 'IS210' -> 'Foothill Freeway'.

    Drops a leading N/S/E/W (OSM has 'East Bonita Avenue') and anything after a comma ('WHITE AVE, LAV, 91750').
    """
    street = street.split(",")[0]
    words = re.sub(r"[^A-Za-z0-9 '\-]", " ", street).split()
    if not words:
        return None
    if " ".join(words).upper() in STREET_ALIASES:
        return STREET_ALIASES[" ".join(words).upper()]
    # Drop a leading direction, unless what's left is only a street type ("E ST" is E Street)
    if len(words) > 1 and words[0].upper() in DIRECTIONS and not (len(words) == 2 and words[1].upper() in SUFFIX_WORDS):
        words = words[1:]
        if " ".join(words).upper() in STREET_ALIASES:
            return STREET_ALIASES[" ".join(words).upper()]
    if len(words) > 1 and words[-1].upper() in STREET_SUFFIXES:
        words[-1] = STREET_SUFFIXES[words[-1].upper()]
    if words[0].lower() in ORDINALS_REVERSE:  # "4TH AVENUE" -> "Fourth Avenue"
        words[0] = ORDINALS_REVERSE[words[0].lower()]
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in words)


def _street_variants(name: str) -> list[str]:
    """Other spellings of the same street that OSM might use."""
    variants = [name]
    first, _, rest = name.partition(" ")
    if first.lower() in ORDINALS:
        variants.append(f"{ORDINALS[first.lower()]} {rest}".strip())
    if name == "Baseline Road":
        variants.append("Base Line Road")
    # OSM may name the freeways by name or by route number
    if name == "Foothill Freeway":
        variants += ["Interstate 210", "I-210"]
    if name == "San Bernardino Freeway":
        variants += ["Interstate 10", "I-10"]
    # Some OSM streets are named without their type ("Cobblestone" for "Cobblestone Lane")
    for v in list(variants):
        base, _, last = v.rpartition(" ")
        if base and last.upper() in SUFFIX_WORDS:
            variants.append(base)
    return variants


def _split_intersection(addr: str) -> list[str]:
    return [p.strip() for p in re.split(r"\s*(?:&|/|\band\b)\s*", addr, flags=re.IGNORECASE) if p.strip()]


# ── OpenStreetMap street data: one bulk download, then local lookups ──────────
# Instead of one Overpass request per intersection, download every named street in the search area
# once (ways and their nodes), keep the nodes where two differently named streets meet, and look
# intersections up locally. The result is saved to a file and refreshed after OSM_MAX_AGE_DAYS.
OSM_MAX_AGE_DAYS = 30
# Streets nobody drives on aren't useful for incident intersections
OSM_SKIP_HIGHWAYS = "footway|path|cycleway|steps|pedestrian|track|bridleway|corridor|proposed|construction|elevator"


def _osm_key(name: str) -> str:
    """Lower case, without a leading direction: 'East Bonita Avenue' -> 'bonita avenue'."""
    return re.sub(r"^(north|south|east|west) (?=\S)", "", name.strip().lower())


def _download_osm_streets() -> list[dict] | None:
    b = OVERPASS_SEARCH_BBOX
    box = f"{b['south']},{b['west']},{b['north']},{b['east']}"
    query = (
        "[out:json][timeout:240];"
        f'way["highway"]["name"]["highway"!~"^({OSM_SKIP_HIGHWAYS})$"]({box});'
        "out body;>;out skel qt;"
    )
    for attempt in range(2):
        for url in OVERPASS_URLS:
            try:
                print(f"Downloading street data from {url} (one request; this can take a minute)...")
                r = SESSION.post(url, data={"data": query}, timeout=(10, 300))
                if r.status_code == 200:
                    return r.json()["elements"]
                print(f"  [warn] {url} returned HTTP {r.status_code}")
            except (requests.RequestException, ValueError, KeyError) as exc:
                print(f"  [warn] {url} failed: {type(exc).__name__}")
        time.sleep(10 * (attempt + 1))
    return None


def _build_osm_intersections(elements: list[dict]) -> list[list]:
    """Keep only nodes shared by two or more differently named streets: [[lat, lon, [street keys]], ...]."""
    xy = {e["id"]: (e["lat"], e["lon"]) for e in elements if e["type"] == "node"}
    streets_at: dict[int, set] = defaultdict(set)
    for e in elements:
        if e["type"] == "way" and e.get("tags", {}).get("name"):
            key = _osm_key(e["tags"]["name"])
            for nid in e.get("nodes", []):
                streets_at[nid].add(key)
    return [[xy[nid][0], xy[nid][1], sorted(keys)]
            for nid, keys in streets_at.items() if len(keys) >= 2 and nid in xy]


class OsmStreetIndex:
    def __init__(self, intersections: list[list]):
        self.intersections = intersections
        self.nodes_on: dict[str, set[int]] = defaultdict(set)  # street key -> intersection ids
        for i, (_, _, keys) in enumerate(intersections):
            for k in keys:
                self.nodes_on[k].add(i)

    def _ids(self, name: str) -> set[int]:
        ids: set[int] = set()
        for v in _street_variants(name):
            ids |= self.nodes_on.get(v.lower(), set())
        return ids

    def knows(self, name: str) -> bool:
        return bool(self._ids(name))

    def find(self, name_a: str, name_b: str) -> list[tuple[float, float]]:
        return [(self.intersections[i][0], self.intersections[i][1]) for i in self._ids(name_a) & self._ids(name_b)]


def load_osm_index(path: Path, refresh: bool = False) -> OsmStreetIndex | None:
    """Load the saved street data, downloading it first if missing, stale or --refresh-osm. None if unavailable."""
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < OSM_MAX_AGE_DAYS * 86400
    if fresh and not refresh:
        with open(path) as f:
            data = json.load(f)
        print(f"Loaded OSM street data: {len(data['intersections']):,} intersections ({path})")
        return OsmStreetIndex(data["intersections"])

    elements = _download_osm_streets()
    if elements is None:
        if path.exists():
            print(f"[warn] Could not refresh OSM street data; using the older copy at {path}")
            with open(path) as f:
                return OsmStreetIndex(json.load(f)["intersections"])
        print("[warn] Could not download OSM street data. Intersections will not be geocoded this run.")
        return None
    intersections = _build_osm_intersections(elements)
    with open(path, "w") as f:
        json.dump({"fetched": time.strftime("%Y-%m-%d %H:%M:%S"), "bbox": OVERPASS_SEARCH_BBOX,
                   "intersections": intersections}, f)
    print(f"Saved OSM street data: {len(intersections):,} intersections from {len(elements):,} elements -> {path}")
    return OsmStreetIndex(intersections)


def _meters_apart(p: tuple[float, float], q: tuple[float, float]) -> float:
    dlat = (p[0] - q[0]) * 111_000
    dlon = (p[1] - q[1]) * 111_000 * math.cos(math.radians(p[0]))
    return math.hypot(dlat, dlon)


def _pick_intersection(nodes: list[tuple[float, float]]) -> tuple[float, float] | None:
    # A divided road or a long street that meets another in several places gives several nodes.
    # Take the one nearest La Verne's center, and average any that sit within ~150 m of it
    # (the two sides of a divided road).
    if not nodes:
        return None
    anchor = min(nodes, key=lambda n: _meters_apart(n, LA_VERNE_CENTER))
    near = [n for n in nodes if _meters_apart(anchor, n) <= 150]
    return sum(n[0] for n in near) / len(near), sum(n[1] for n in near) / len(near)


# Why each intersection couldn't be placed (address -> reason), for the unmatched report
UNRESOLVED_INTERSECTIONS: dict[str, str] = {}


def geocode_intersection(addr: str, index: OsmStreetIndex) -> tuple[float | None, float | None]:
    parts = [_normalize_street(p) for p in _split_intersection(addr)]
    parts = [p for p in parts if p]
    if len(parts) != 2:
        UNRESOLVED_INTERSECTIONS[addr] = "could not split into two streets"
        return None, None
    missing = [p for p in parts if not index.knows(p)]
    if missing:
        UNRESOLVED_INTERSECTIONS[addr] = "street not found in OSM: " + ", ".join(missing)
        return None, None
    picked = _pick_intersection(index.find(parts[0], parts[1]))
    if picked is None:
        UNRESOLVED_INTERSECTIONS[addr] = "streets exist in OSM but share no intersection"
        return None, None
    UNRESOLVED_INTERSECTIONS.pop(addr, None)
    return picked


def geocode_address(raw_addr: str, osm_index: OsmStreetIndex | None) -> tuple[float | None, float | None]:
    addr = raw_addr.split(";")[0].strip()

    if _is_intersection(addr):
        # No fallback to geocoding one street: that gives a pin far from the real corner.
        # Better to leave it unmapped (the dashboard counts unmapped incidents).
        if osm_index is None:
            return None, None
        return geocode_intersection(addr, osm_index)

    return _census_geocode_single(_clean_address(addr))


def _norm_addr(addr: str) -> str:
    # Older cache entries are uppercase, newer scrapes are lowercase -- compare on one form
    return str(addr).strip().lower()


def _has_coords(entry) -> bool:
    return bool(entry) and len(entry) == 2 and entry[0] is not None and entry[1] is not None


def normalize_cache(raw_cache: dict) -> dict:
    """Re-key the cache by normalized address. If two keys collide, keep the one with coordinates."""
    out: dict[str, list] = {}
    for addr, val in raw_cache.items():
        key = _norm_addr(addr)
        if key not in out or (_has_coords(val) and not _has_coords(out[key])):
            out[key] = val
    return out


def geocode_dataframe(df: pd.DataFrame, cache_path: Path, osm_streets_path: Path, refresh_osm: bool = False) -> pd.DataFrame:
    # Load whatever we've already geocoded so we don't re-hit the API every run
    cache: dict[str, list] = {}
    if cache_path.exists():
        with open(cache_path) as f:
            raw_cache = json.load(f)
        cache = normalize_cache(raw_cache)
        print(f"Loaded geocode cache: {len(raw_cache)} entries -> {len(cache)} unique addresses")

    osm_index = load_osm_index(osm_streets_path, refresh=refresh_osm)

    # One lookup per normalized address; keep the first original spelling for the Census query
    unique_addrs = {}
    for a in df["incident_address"].dropna().unique():
        unique_addrs.setdefault(_norm_addr(a), a)

    def is_inter(a: str) -> bool:
        return _is_intersection(a.split(";")[0].strip())

    # Intersections are re-resolved every run: it's a local lookup (no network), and older cache entries for
    # them are averaged coordinates that can be far off. If the street data couldn't be loaded, leave them as is.
    to_geocode = [orig for key, orig in unique_addrs.items()
                  if (is_inter(orig) and osm_index is not None) or (not is_inter(orig) and key not in cache)]
    print(f"Addresses to geocode (Census for street addresses, OpenStreetMap for intersections): "
          f"{len(to_geocode)} to look up / {len(unique_addrs)} total")

    for i, addr in enumerate(to_geocode, 1):
        lat, lon = geocode_address(addr, osm_index)
        cache[_norm_addr(addr)] = [lat, lon]
        if i % 50 == 0:
            print(f"  Geocoded {i}/{len(to_geocode)}...")
            # Write incrementally so we don't lose everything if it gets interrupted
            with open(cache_path, "w") as f:
                json.dump(cache, f)

    with open(cache_path, "w") as f:
        json.dump(cache, f)
    print(f"Geocode cache saved: {len(cache)} addresses → {cache_path}")

    if UNRESOLVED_INTERSECTIONS:
        counts = df["incident_address"].map(_norm_addr).value_counts()
        report = pd.DataFrame(
            [(a, int(counts.get(_norm_addr(a), 0)), why) for a, why in UNRESOLVED_INTERSECTIONS.items()],
            columns=["address", "rows", "reason"],
        ).sort_values("rows", ascending=False)
        report_path = cache_path.with_name("unmatched_intersections.csv")
        report.to_csv(report_path, index=False)
        print(f"{len(report):,} intersections ({report['rows'].sum():,} rows) could not be placed -> {report_path}")
        print(report.head(10).to_string(index=False))

    df = df.copy()
    df["lat"] = df["incident_address"].map(lambda a: cache.get(_norm_addr(a), [None, None])[0])
    df["lon"] = df["incident_address"].map(lambda a: cache.get(_norm_addr(a), [None, None])[1])

    geocoded = df["lat"].notna().sum()
    print(f"Geocoded: {geocoded:,}/{len(df):,} rows ({geocoded/len(df)*100:.1f}%)")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Transform raw LVPD scraper CSV for the Streamlit dashboard.")
    parser.add_argument("--input", required=True, help="Path to raw scraped CSV from lvpd_scrape_daily_logs.py")
    parser.add_argument("--output-dir", default=".", help="Directory to write output CSVs (default: current dir)")
    parser.add_argument("--geocode-cache", default="geocode_cache.json", help="Path to geocoding cache JSON (created if missing)")
    parser.add_argument("--osm-streets", default=None,
                        help="Path to the saved OpenStreetMap street data (default: osm_streets.json next to the geocode cache)")
    parser.add_argument("--refresh-osm", action="store_true",
                        help=f"Re-download the OpenStreetMap street data (otherwise it is refreshed after {OSM_MAX_AGE_DAYS} days)")
    parser.add_argument("--skip-geocode", action="store_true", help="Skip geocoding and only output daily_logs.csv")
    parser.add_argument("--allow-validation-failures", action="store_true",
                        help="Log validation failures but keep going anyway (for when you've checked the data and know it's fine)")
    args = parser.parse_args()
    LOGGER.info("Transformer started: input=%s, output_dir=%s, skip_geocode=%s, allow_validation_failures=%s",
                args.input, args.output_dir, args.skip_geocode, args.allow_validation_failures)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Step 1: Load raw scraper output ===")
    raw = pd.read_csv(args.input, dtype=str).fillna("")
    print(f"Loaded {len(raw):,} raw rows from {args.input}")

    # Nothing gets written (CSVs or geocode cache) until this passes
    print(f"\n=== Step 2: Validate ===")
    try:
        raw = validate_raw_data(raw, cutoff=CUTOFF_DATE, allow_failures=args.allow_validation_failures)
    except DataValidationError as exc:
        LOGGER.error("Validation failed, stopping before anything is written: %s", exc)
        print(f"\n[validation failed] {exc}")
        print("Nothing was written: the output CSVs and the geocode cache are untouched.")
        print("If you've looked at the data and it's actually fine, re-run with --allow-validation-failures.")
        sys.exit(VALIDATION_FAILED_EXIT_CODE)

    print(f"\n=== Step 3: Transform ===")
    df = transform(raw)

    logs_path = out_dir / "daily_logs.csv"
    df.to_csv(logs_path, index=False)
    print(f"Saved: {logs_path}")
    LOGGER.info("Saved transformed CSV: %s (%s rows)", logs_path, len(df))

    if args.skip_geocode:
        print("\nSkipping geocoding (--skip-geocode flag set).")
        return

    print(f"\n=== Step 4: Geocode ===")
    cache_path = Path(args.geocode_cache)
    osm_path = Path(args.osm_streets) if args.osm_streets else cache_path.with_name("osm_streets.json")
    df_geo = geocode_dataframe(df, cache_path, osm_path, refresh_osm=args.refresh_osm)

    geo_path = out_dir / "daily_logs_geocoded.csv"
    df_geo.to_csv(geo_path, index=False)
    print(f"Saved: {geo_path}")
    LOGGER.info("Saved geocoded CSV: %s (%s rows)", geo_path, len(df_geo))

    print("\nDone.")


if __name__ == "__main__":
    main()
