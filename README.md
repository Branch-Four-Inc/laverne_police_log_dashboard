# La Verne Police Log Dashboard

A Streamlit dashboard of incidents reported to the La Verne Police Department (LVPD).

- Live app: https://lavernepolicelogdashboard-qnxjkmecozminidibjlmto.streamlit.app/
- Raw daily logs (PDFs) from LVPD: https://lvpd.org/news-statistics

## Repository layout

| Path | What it is |
|---|---|
| `app.py` | The Streamlit dashboard |
| `daily_logs.csv`, `daily_logs_geocoded.csv` | Data the app reads (copied here from the pipeline output) |
| `nature_descriptions.xlsx` | Lookup of LVPD incident codes → grouped incident type + plain-English description. Used by the pipeline and shown as the app's data dictionary |
| `police_logs_dashboard_work/` | The data pipeline (scraper, transform, geocoding, validation) |
| `police_logs_dashboard_work/output/` | Where the pipeline writes its results, the geocode cache, and logs |
| `scripts/` | Older one-off helper scripts from the original EDA |
| `.streamlit/config.toml` | Streamlit theme |
| `pyproject.toml`, `uv.lock` | Python dependencies |

---

## Setup

Requires Python 3.14+ (see `.python-version`).

```bash
# dashboard dependencies (uv recommended)
uv sync

# the pipeline needs a few extra packages that are not in pyproject.toml
pip install requests beautifulsoup4 lxml pdfplumber openai python-dateutil
```

`openai` is only needed for the optional geocode backfill (Step 3 below).

---

## Running the full pipeline

The pipeline turns LVPD's daily PDFs into the two CSVs the dashboard reads.

```bash
cd police_logs_dashboard_work
python run_pipeline.py
```

Common options:

| Command | Effect |
|---|---|
| `python run_pipeline.py` | Full run: scrape → validate/transform → geocode → backfill if needed |
| `python run_pipeline.py --skip-geocode` | Stop after the transform (quick check; only writes `daily_logs.csv`) |
| `python run_pipeline.py --no-backfill` | Skip the OpenAI backfill (no API key, or avoid charges) |
| `python run_pipeline.py --refresh-osm` | Re-download the OpenStreetMap street data used to place intersections (otherwise reused, and refreshed after 30 days) |
| `python run_pipeline.py --no-zip` | Don't save the downloaded PDFs as a ZIP (saves ~70 MB of disk) |
| `python run_pipeline.py --backfill-threshold 0.05 --backfill-min-count 100` | Change when the backfill triggers (defaults: 7% of rows or 50 rows missing coordinates) |
| `python run_pipeline.py --allow-validation-failures` | Keep going even if validation fails. Check the log first |

Let a run finish without interrupting it. If a run is killed partway, the files in `output/` may be left from an earlier run.

### Publishing new data to the dashboard

When the pipeline finishes, copy these two files from `police_logs_dashboard_work/output/` to the repository root, then commit and push:

- `daily_logs.csv`
- `daily_logs_geocoded.csv`

Streamlit Community Cloud redeploys on push (the app also caches data for up to an hour).

### Keep `geocode_cache.json`

`output/geocode_cache.json` stores every address already geocoded. Don't delete it. It's what lets later runs geocode only new addresses. (`output/osm_streets.json` is a separate download that's safe to delete. It will be fetched again.)

### OpenAI API key (optional)

The backfill needs `OPENAI_API_KEY` in your environment. Never put the key in code or commit it.

```powershell
# PowerShell: saves for your Windows user. Open a NEW terminal afterwards.
setx OPENAI_API_KEY "sk-..."
```

If the key isn't set, the backfill is skipped with a warning and the rest of the pipeline still completes.

---

## What each part of the pipeline does

All pipeline code lives in `police_logs_dashboard_work/`. `run_pipeline.py` runs these in order and stops if any step fails.

### Step 1: Scrape: `lvpd_scrape_daily_logs.py`

1. Loads the LVPD news/statistics page and confirms the expected month folders exist.
2. Pages through LVPD's WordPress API (`/wp-json/wp/v2/dlp_document`, category 39) to list every daily-log PDF.
3. Keeps only PDFs **posted in 2026**. This uses the posting date, not the date in the title. A log titled `12.31.2025` that was posted in January 2026 is therefore included.
4. Downloads each PDF, with retries and a minimum-size check to catch error pages. It optionally saves them to `output/lvpd_daily_logs.zip`.
5. Parses each PDF into rows by reading character positions to split the four columns (incident number, reported time, nature, address).
6. Writes `output/raw_scraped.csv`. PDFs that can't be downloaded or parsed are listed in `failed_daily_log_downloads.csv`.

The scraper treats a changed site or PDF layout as an error and stops, rather than silently producing bad data.

### Step 2: Validate, transform, and geocode: `transform_and_geocode.py`

Runs in this order:

1. **Validate** (`validation.py`) before anything is written. It checks:
   - The columns match what the scraper should produce.
   - Date, address, incident type, and incident ID are filled in on each row.
   - Dates fall in a sane range: not in the future, not before the archive starts, and not after the target month.
   - Duplicate incident IDs. LVPD repeats the end of one day in the next day's PDF, so these are dropped and counted.
   - The target month's row count is in line with the trailing 3-month average.

   If validation fails, the script exits with code 2 and leaves the existing CSVs and cache untouched. After you've checked the data, `--allow-validation-failures` overrides this.
2. **Transform**
   - Skips two dates whose PDFs don't parse (`SKIP_DATES`).
   - Converts timestamps and drops rows that can't be parsed.
   - Cuts off at the end of the previous month, so a partial current month never appears.
   - Adds `nature_description` and `grouped_nature` from `nature_descriptions.xlsx`. Unknown codes fall back to "Other". Writes `output/daily_logs.csv`.
3. **Geocode** each unique address. Writes `output/daily_logs_geocoded.csv` with `lat` and `lon`. Results are cached in `output/geocode_cache.json`.
   - **Street addresses** (`123 FOOTHILL BLVD`) go to the US Census geocoder. Only addresses not already in the cache are looked up.
   - **Intersections** (`A & B`, `A / B`) use OpenStreetMap (OSM) data instead, because the Census geocoder can't place them. See [Intersection geocoding](#intersection-geocoding-openstreetmap) below.

#### Intersection geocoding (OpenStreetMap)

Averaging the coordinates of two separately geocoded streets puts the pin at an arbitrary point along a long street. Instead, the pipeline finds the point where the two streets actually cross in OSM:

1. **One bulk download.** The first run asks the Overpass API for every named, drivable street in a box around La Verne (footways, paths, and similar are skipped). It keeps only the nodes where two differently named streets meet, and saves them to `output/osm_streets.json`.
2. **Local lookups.** Each intersection is then matched against that file with no network calls. The file is re-downloaded when it's older than 30 days, or when you pass `--refresh-osm` to `transform_and_geocode.py`. If a refresh fails, the older copy is used. If there is no copy at all, intersections are left unmapped for that run.
3. **Street names are standardized** to OSM's spelling before matching:
   - Abbreviated types are spelled out (`FOOTHILL BLVD` → Foothill Boulevard).
   - A leading `N/S/E/W` is dropped, and anything after a comma is ignored.
   - Spelled-out numbered streets (`EIGHTH ST`) also match OSM's digit form (`8th Street`).
   - Truncated or bare names are mapped by hand (`FOOTHILL`, `ARROW`, `BASELINE`, and others in `STREET_ALIASES`).
   - The freeways are named in OSM, not numbered. `IS210` becomes Foothill Freeway, and it also tries `Interstate 210` and `I-210`. `IS10` becomes San Bernardino Freeway, and it also tries `Interstate 10` and `I-10`.
4. **Picking the point.** If the streets cross in several places, it takes the crossing nearest the center of La Verne and averages any within about 150 m of it, which covers the two sides of a divided road.

There is deliberately no fallback to geocoding one of the streets. An intersection that can't be placed stays unmapped (the dashboard reports how many), because a wrong pin is worse than none.

Intersections are re-resolved on every run, since it's a quick local lookup, and this replaces any older averaged coordinates in the cache. The ones that couldn't be placed are written to `output/unmatched_intersections.csv`, with the row count and the reason for each (street not found in OSM, no shared intersection, or couldn't be split into two streets). To fix one, add its spelling to `STREET_ALIASES` in `transform_and_geocode.py`.

To force a re-download, run `python run_pipeline.py --refresh-osm` (or pass `--refresh-osm` to `transform_and_geocode.py` directly).

### Step 3: OpenAI backfill: `openai_geocode_backfill.py`

Runs only if, after Census geocoding, at least 7% of rows or at least 50 rows have no coordinates. It doesn't count `ASSIST AGENCY` rows, since those can be in other cities.

- It sends the unresolved addresses to OpenAI (with web search).
- It rejects any result outside a bounding box around La Verne and neighboring cities.
- It saves results into `geocode_cache.json`.
- It records attempts in `output/openai_backfill_progress.json`, so a restart never pays for the same address twice.

After the backfill, `run_pipeline.py` re-runs the transform. Every address is cached by then, so this just rebuilds the output CSVs from the updated cache.

### Supporting files

- `validation.py`: the data checks and their thresholds.
- `utils.py`: logging setup. Logs go to `output/log_files/scraper.log`. Check there first when a run fails.
- `test_validation.py`: tests for the validation checks.

---

## Running the dashboard locally

From the repository root, where `daily_logs_geocoded.csv` is:

```bash
streamlit run app.py
```

Then open http://localhost:8501.

## Hosting the dashboard (Streamlit Community Cloud)

The app is hosted on [Streamlit Community Cloud](https://share.streamlit.io):

1. Push the repository to GitHub, including the current `daily_logs_geocoded.csv`, `daily_logs.csv`, and `nature_descriptions.xlsx` in the repository root.
2. Sign in at share.streamlit.io with GitHub and choose **Create app**.
3. Select the repository and branch, set the main file to `app.py`, and deploy.
4. Dependencies are read from `pyproject.toml`. The pipeline-only packages aren't needed to host the app.

To update the data later, replace the two CSVs and push. The app redeploys automatically.

No secrets are needed to host the dashboard. The OpenAI key is used only when running the pipeline locally, and `.env` and `secrets.toml` are git-ignored.

---

## How the dashboard works

`app.py` is a single-page Streamlit app. It reads `daily_logs_geocoded.csv` and falls back to `daily_logs.csv` if that's missing. It then builds `hour` and day-of-week columns and assigns each incident type a color. The data is cached for one hour (`st.cache_data`).

**Sidebar filters**: date range, incident type (multiselect, plus a "Select All Incidents" button), and a street-address "contains" search. Every section below responds to these filters, except the Assist Agency expander (see below).

**Sections, top to bottom:**

1. **Counts**: the filtered incident count next to the total across all data.
2. **Incident Map** (pydeck): one dot per geocoded incident, colored by incident type, with a hover tooltip showing the date, type, and address. Incidents without coordinates aren't mapped. A note beneath the map gives how many.
3. **Total Incidents by Day of Week & Hour**: a heatmap showing when incidents happen.
4. **Total Incidents by Day**: daily counts with a 7-day average line. The "Incident Type breakdown" toggle switches to stacked bars by type.
5. **All Incidents**: a table of the filtered incidents (report date, incident type, description, address).
6. **View Assist Agency Calls** (expander): `ASSIST AGENCY` calls are kept out of the incident-type filter and map because many happen in other cities. They're listed here for the selected date range and address search, with their coordinates when available.
7. **View Data Dictionary** (expander): incident codes, types, and descriptions from `nature_descriptions.xlsx`.
8. **Important**: disclaimers. Incidents are reports, not proof that a crime occurred. The dashboard is informational only, and anyone with an emergency should call 911.

Incident types come from the `grouped_nature` column, which the pipeline fills from `nature_descriptions.xlsx`. To rename or regroup a type, or to add a new code, edit that spreadsheet and re-run the pipeline.
