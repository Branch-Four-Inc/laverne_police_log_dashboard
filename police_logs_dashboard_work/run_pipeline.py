#!/usr/bin/env python3
"""
One command to run the full pipeline: scrape → transform → geocode → OpenAI backfill


Usage:
    # Initial full pull (Jan 3 – May 31 2026):
    python run_pipeline.py

    # Monthly refresh (run mid-month; grabs whatever new PDFs the site has):
    python run_pipeline.py --monthly-refresh

    # Skip geocoding if you just want to check the transform output quickly:
    python run_pipeline.py --skip-geocode

    # Skip the OpenAI backfill (e.g. no API key, or to avoid OpenAI charges):
    python run_pipeline.py --no-backfill

    # Change when the backfill triggers (defaults: 0.07 share of rows, 50 rows):
    python run_pipeline.py --backfill-threshold 0.05 --backfill-min-count 100

    # Re-download the OpenStreetMap street data used to place intersections
    # (otherwise it's reused from output/osm_streets.json and refreshed after 30 days):
    python run_pipeline.py --refresh-osm

Outputs go to ./output/ — copy daily_logs.csv and daily_logs_geocoded.csv
to the dashboard repo when done.

NOTE: Keep geocode_cache.json! It saves all the addresses we've already looked up
so monthly refreshes only geocode new addresses instead of re-running everything.

The OpenAI backfill (Step 3) only runs when >=7% of rows, or >=50 rows, still lack
lat/lon after Census geocoding. It needs an OpenAI API key in the OPENAI_API_KEY
environment variable. If the key is not set, the backfill is skipped with a warning
and the rest of the pipeline still completes. Addresses that already have coordinates
in geocode_cache.json are never sent to OpenAI.

Setting OPENAI_API_KEY (never hardcode the key or commit it to git):

    # PowerShell (saved for your Windows user; open a NEW terminal afterwards):
    setx OPENAI_API_KEY "sk-..."

"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd

from utils import get_logger, install_exception_logger

HERE = Path(__file__).resolve().parent
SCRAPER = HERE / "lvpd_scrape_daily_logs.py"
TRANSFORMER = HERE / "transform_and_geocode.py"
BACKFILLER = HERE / "openai_geocode_backfill.py"
OUTPUT_DIR = HERE / "output"
GEOCODED_CSV = OUTPUT_DIR / "daily_logs_geocoded.csv"
RAW_CSV = OUTPUT_DIR / "raw_scraped.csv"
GEOCODE_CACHE = OUTPUT_DIR / "geocode_cache.json"
OSM_STREETS = OUTPUT_DIR / "osm_streets.json"
LOGGER = get_logger()
install_exception_logger(LOGGER)


def run(cmd: list[str]) -> None:
    """
    Start logger for file. If program dails, return return code and closes program
    """
    
    print(f"\n$ {' '.join(str(c) for c in cmd)}\n{'─' * 60}")
    LOGGER.info("Running command: %s", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"\n[error] Command exited with code {result.returncode}. Stopping pipeline.")
        LOGGER.error("Command exited with code %s: %s", result.returncode, " ".join(str(c) for c in cmd))
        sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full LVPD scrape → transform → geocode pipeline.")
    parser.add_argument("--monthly-refresh", action="store_true",
                        help="Monthly mode: scrape latest PDFs and merge new rows into existing output CSVs.")
    parser.add_argument("--skip-geocode", action="store_true", help="Skip geocoding step.")
    parser.add_argument("--backfill-threshold", type=float, default=0.07,
                        help="Run the OpenAI backfill if this share of rows lack lat/lon (default 0.07).")
    parser.add_argument("--backfill-min-count", type=int, default=50,
                        help="...or if at least this many rows lack lat/lon (default 50).")
    parser.add_argument("--refresh-osm", action="store_true",
                        help="Re-download the OpenStreetMap street data used for intersections (otherwise refreshed after 30 days).")
    parser.add_argument("--no-backfill", action="store_true", help="Skip the OpenAI geocode backfill step.")
    parser.add_argument("--no-zip", action="store_true", help="Skip saving the PDF ZIP archive (saves disk space).")
    parser.add_argument("--allow-validation-failures", action="store_true",
                        help="Keep going even if the data validation step fails (check the log first!).")
    args = parser.parse_args()
    
    start_time = datetime.now()
    LOGGER.info("Pipeline started at %s: skip_geocode=%s, no_zip=%s", start_time, args.skip_geocode, args.no_zip)

    OUTPUT_DIR.mkdir(exist_ok=True)

    # Step 1: Scrape — pulls the PDF index from lvpd.org and parses every daily log
    print("=== Step 1: Scrape LVPD PDFs ===")
    scrape_cmd = [
        sys.executable, str(SCRAPER),
        "--output-csv", str(RAW_CSV),
    ]
    if args.no_zip:
        scrape_cmd += ["--output-zip", ""]
    else:
        scrape_cmd += ["--output-zip", str(OUTPUT_DIR / "lvpd_daily_logs.zip")]
    run(scrape_cmd)

    # Step 2: Transform + geocode — cleans the raw CSV and adds all the columns the app needs
    print("\n=== Step 2: Transform + Geocode ===")
    transform_cmd = [
        sys.executable, str(TRANSFORMER),
        "--input", str(RAW_CSV),
        "--output-dir", str(OUTPUT_DIR),
        "--geocode-cache", str(GEOCODE_CACHE),
        "--osm-streets", str(OSM_STREETS),
    ]
    if args.skip_geocode:
        transform_cmd.append("--skip-geocode")
    if args.allow_validation_failures:
        transform_cmd.append("--allow-validation-failures")
    # Only the first transform run re-downloads the OSM street data; the re-run after the backfill reuses the saved file
    run(transform_cmd + (["--refresh-osm"] if args.refresh_osm else []))

    # Step 3: OpenAI backfill — only when many rows are still missing lat/lon after Census
    if not args.skip_geocode and not args.no_backfill and GEOCODED_CSV.exists():
        
        print("\n=== Step 3: OpenAI geocode backfill (if needed) ===")
        
        geo = pd.read_csv(GEOCODED_CSV, usecols=["lat", "lon", "nature"])
        # ASSIST AGENCY calls can be in other cities, so they don't count toward missing coordinates
        geo = geo[geo["nature"].fillna("").str.strip().str.upper() != "ASSIST AGENCY"]
        missing = int((geo["lat"].isna() | geo["lon"].isna()).sum())
        share = missing / len(geo) if len(geo) else 0.0
        print(f"Rows missing lat/lon (excluding ASSIST AGENCY) AFTER census geocode: {missing} of {len(geo)} ({share:.1%})")
        LOGGER.info("Rows missing lat/lon after Census geocoding (excluding ASSIST AGENCY) AFTER census geocode: %d of %d (%.1f%%)",
                    missing, len(geo), share * 100)

        if missing and (share >= args.backfill_threshold or missing >= args.backfill_min_count):
            if not os.environ.get("OPENAI_API_KEY"):
                print("[warn] OPENAI_API_KEY not set; skipping OpenAI backfill.")
                LOGGER.warning("OPENAI_API_KEY not set; skipping OpenAI backfill.")
            else:
                run([
                    sys.executable, str(BACKFILLER),
                    "--geocoded-csv", str(GEOCODED_CSV),
                    "--cache", str(GEOCODE_CACHE),
                    "--progress", str(OUTPUT_DIR / "openai_backfill_progress.json"),
                ])
                # Re-run the transform: every address is cached now, so Census isn't called
                # again; this just re-maps the updated cache into the output CSVs.
                print("\n=== Re-applying updated geocode cache ===")
                run(transform_cmd)
        else:
            print("Below backfill threshold; skipping.")
            LOGGER.info("Missing lat/lon below backfill threshold; skipping OpenAI backfill.")

    # Print a summary of what was produced
    print(f"\n{'=' * 60}")
    print("Pipeline complete. Output files:")
    
    for f in sorted(OUTPUT_DIR.iterdir()):
        size_kb = f.stat().st_size / 1024
        print(f"  {f.name:<35} {size_kb:>8.1f} KB")
        
    print(f"\nNext: copy these two files to the dashboard repo root:")
    print(f"  {OUTPUT_DIR}/daily_logs.csv")
    print(f"  {OUTPUT_DIR}/daily_logs_geocoded.csv")
    
    LOGGER.info(f"Pipeline completed successfully. Time to completion: {datetime.now() - start_time}")


if __name__ == "__main__":
    main()
