#!/usr/bin/env python3
"""
One command to run the full pipeline: scrape → transform → geocode.

Usage:
    # Initial full pull (Jan 3 – May 31 2026):
    python run_pipeline.py

    # Monthly refresh (run mid-month; grabs whatever new PDFs the site has):
    python run_pipeline.py --monthly-refresh

    # Skip geocoding if you just want to check the transform output quickly:
    python run_pipeline.py --skip-geocode

Outputs go to ./output/ — copy daily_logs.csv and daily_logs_geocoded.csv
to the dashboard repo when done.

NOTE: Keep geocode_cache.json! It saves all the addresses we've already looked up
so monthly refreshes only geocode new addresses instead of re-running everything.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRAPER = HERE / "lvpd_scrape_daily_logs.py"
TRANSFORMER = HERE / "transform_and_geocode.py"
OUTPUT_DIR = HERE / "output"
RAW_CSV = OUTPUT_DIR / "raw_scraped.csv"
GEOCODE_CACHE = OUTPUT_DIR / "geocode_cache.json"


def run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}\n{'─' * 60}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"\n[error] Command exited with code {result.returncode}. Stopping pipeline.")
        sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full LVPD scrape → transform → geocode pipeline.")
    parser.add_argument("--monthly-refresh", action="store_true",
                        help="Monthly mode: scrape latest PDFs and merge new rows into existing output CSVs.")
    parser.add_argument("--skip-geocode", action="store_true", help="Skip geocoding step.")
    parser.add_argument("--no-zip", action="store_true", help="Skip saving the PDF ZIP archive (saves disk space).")
    args = parser.parse_args()

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
    ]
    if args.skip_geocode:
        transform_cmd.append("--skip-geocode")
    run(transform_cmd)

    # Print a summary of what was produced
    print(f"\n{'=' * 60}")
    print("Pipeline complete. Output files:")
    for f in sorted(OUTPUT_DIR.iterdir()):
        size_kb = f.stat().st_size / 1024
        print(f"  {f.name:<35} {size_kb:>8.1f} KB")
    print(f"\nNext: copy these two files to the dashboard repo root:")
    print(f"  {OUTPUT_DIR}/daily_logs.csv")
    print(f"  {OUTPUT_DIR}/daily_logs_geocoded.csv")


if __name__ == "__main__":
    main()
