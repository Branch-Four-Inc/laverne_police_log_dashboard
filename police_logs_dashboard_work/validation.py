"""
Sanity checks on the scraped LVPD data. Runs on the raw scraper CSV BEFORE anything gets
written -- no geocoding (so no cache writes, no API spend) and no daily_logs*.csv.

One thing to keep in mind: the scraper re-pulls every PDF on the site each run, so the file
always holds the whole history plus whatever is new. A problem in just the new month (say LVPD
changes the PDF layout) would get watered down by ~17k good rows from earlier months, so the
fail thresholds get checked against the target month's rows AND the whole file.

Target month = the month this run is meant to add = the month of the cutoff date
(see previous_month_end()).

Checks, in this order (the order matters -- garbled rows have to go before we look for dupes):
  1. schema           raw columns match exactly what the scraper should emit  (hard stop)
  2. required fields  date / address / incident type / incident id on every row
  3. date range       nothing in the future, nothing before the archive starts, nothing after the target month
  4. duplicates       same incident ID more than once
  5. row count        target month vs the trailing 3-month average

Failures get collected and raised together as one DataValidationError, so a single run tells
you everything that's wrong instead of one problem at a time.
"""

from __future__ import annotations

import logging

import pandas as pd

from utils import LOGGER_NAME

# Same logger the scraper/transform use (utils.get_logger sets it up). Nothing is attached at
# import time, so tests and anything else importing this stay quiet unless they ask for logs.
LOGGER = logging.getLogger(LOGGER_NAME)


# ── What the scraper is supposed to hand us ─────────────────────────────────────
EXPECTED_RAW_COLUMNS = ["log_date", "incident", "reported", "nature", "incident_address"]

# Every row needs these filled in (label -> raw column). "incident id" wasn't in the original
# ask, but the duplicate check keys on it and blank IDs would all get lumped together as
# "duplicates" of each other, so it's easier to just require it.
REQUIRED_FIELDS = {
    "date": "reported",
    "address": "incident_address",
    "incident type": "nature",
    "incident id": "incident",
}

REPORTED_FORMAT = "%H:%M:%S %m/%d/%y"  # e.g. "00:17:53 01/01/26"
LOG_DATE_FORMAT = "%m.%d.%Y"           # PDF titles, e.g. "01.01.2026"

# The archive starts in early Jan 2025. A date older than this is a bad parse, not real history.
EARLIEST_VALID_DATE = pd.Timestamp("2025-01-01")


# ── Thresholds (picked off the real history, see comments) ──────────────────────
# Fail if more than this share of rows is missing a required field. The whole history sits
# around 0.5% -- all of it from two Feb 2025 PDFs that had a shifted column layout.
MAX_MISSING_REQUIRED_RATE = 0.25

# Fail if more than this share of rows repeat an earlier incident ID. LVPD repeating the tail of
# one day in the next day's PDF is normal (worst month so far: ~4.75%, Dec 2025), but scraping
# a month twice would land us near 50%.
MAX_DUPLICATE_RATE = 0.10

# New month's row count compared with the trailing 3-month average.
ROW_COUNT_MIN_RATIO = 0.25 # lowered to .25, some months may be much slower than others like holidays, summer vacation
ROW_COUNT_MAX_RATIO = 2.50
BASELINE_MONTHS = 3


class DataValidationError(Exception):
    """The scraped data didn't pass validation -- nothing downstream should run."""


def _say(level: int, msg: str, *args) -> None:
    """Log it to the file and echo it to the terminal, like the rest of the pipeline does."""
    LOGGER.log(level, msg, *args)
    tag = {logging.INFO: "info", logging.WARNING: "warn", logging.ERROR: "error"}[level]
    print(f"[{tag}] " + (msg % args if args else msg))


def previous_month_end(now: pd.Timestamp | None = None) -> pd.Timestamp:
    """Last second of the previous calendar month.

    The old cutoff was `now().replace(day=1) - 1 day`, which keeps the current time of day -- run
    it at 4:40 PM and everything reported after 4:40 PM on the last day of the month got cut.
    """
    now = pd.Timestamp.now() if now is None else pd.Timestamp(now)
    return now.normalize().replace(day=1) - pd.Timedelta(seconds=1)


def _is_blank(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip() == ""


def _from_target_month(df: pd.DataFrame, 
                       target: pd.Period) -> pd.Series:
    """Rows that came from PDFs titled with a date in the target month.

    Goes by log_date rather than `reported` because `reported` is one of the fields that can be
    garbage, and we still want those rows counted against the month they came from.
    """
    log_dt = pd.to_datetime(df["log_date"], format=LOG_DATE_FORMAT, errors="coerce")
    
    return log_dt.dt.to_period("M") == target


# ── 1. schema ───────────────────────────────────────────────────────────────────
def check_schema(df: pd.DataFrame) -> None:
    
    """Check that the expected columns are correct"""
    
    expected, actual = set(EXPECTED_RAW_COLUMNS), set(df.columns)
    missing, unexpected = sorted(expected - actual), sorted(actual - expected)
    if missing or unexpected:
        msg = (
            f"Schema mismatch -- missing columns: {missing or 'none'}; unexpected columns: "
            f"{unexpected or 'none'}. The scraper output format probably changed."
        )
        _say(logging.ERROR, msg)
        raise DataValidationError(msg)


# ── 2. required fields ──────────────────────────────────────────────────────────
def check_required_fields(df: pd.DataFrame, 
                          target: pd.Period, 
                          failures: list[str]) -> pd.DataFrame:
    
    """Checks all rows for any missing required fields. Also checks if the overall missings 
    rate is above the acceptable threshold"""
    
    
    # builds a dataframe identifying missing required columns for all dataframe rows
    missing = pd.DataFrame({label: _is_blank(df[col]) for label, col in REQUIRED_FIELDS.items()})
    
    
    # a timestamp that's there but doesn't parse (garbled PDF text) is as good as missing
    missing["date"] = missing["date"] | df["_dt"].isna()
    any_missing = missing.any(axis=1)

    # check if all rows are in target month
    in_target = _from_target_month(df, target)
    
    # calculate overall missings rate
    overall_rate = any_missing.mean() if len(df) else 0.0
    
    # calculates target rate for rows that are in the target month
    target_rate = any_missing[in_target].mean() if in_target.any() else 0.0
    by_field = {k: int(v) for k, v in missing.sum().items() if v}

    for scope, rate in (("all", overall_rate), (f"{target} PDF", target_rate)):
        if rate > MAX_MISSING_REQUIRED_RATE:
            msg = (
                f"{rate:.1%} of {scope} rows are missing a required field {by_field} "
                f"(limit {MAX_MISSING_REQUIRED_RATE:.0%})"
            )
            _say(logging.ERROR, msg)
            failures.append(msg)

    if any_missing.any():
        ids = df.loc[any_missing, "incident"].head(10).tolist()
        _say(
            logging.WARNING,
            "Dropped %s rows missing a required field %s -- first incident IDs: %s",
            int(any_missing.sum()), by_field, ids,
        )
    else:
        _say(logging.INFO, "Required fields: all %s rows complete", f"{len(df):,}")
        
    # returns df dropping rows with any missing required fields    
    return df[~any_missing]


# ── 3. date range ───────────────────────────────────────────────────────────────
def drop_out_of_range_dates(df: pd.DataFrame, 
                            cutoff: pd.Timestamp, 
                            now: pd.Timestamp) -> pd.DataFrame:
    """Remove rows with log dates that are too old, in the future, or in progress """
    
    dt = df["_dt"]
    future = dt > now
    too_old = dt < EARLIEST_VALID_DATE
    # The current month, still being posted. Expected on every run, so it's not an anomaly --
    # next month's run will pick these up.
    in_progress = (dt > cutoff) & ~future

    if future.any():
        _say(logging.WARNING, "Dropped %s rows dated in the future (after %s) -- e.g. %s",
             int(future.sum()), now.date(), df.loc[future, "reported"].head(3).tolist())
    if too_old.any():
        _say(logging.WARNING, "Dropped %s rows dated before %s -- e.g. %s",
             int(too_old.sum()), EARLIEST_VALID_DATE.date(), df.loc[too_old, "reported"].head(3).tolist())
    if in_progress.any():
        _say(logging.INFO, "Held back %s rows after %s (month still in progress)",
             int(in_progress.sum()), cutoff.date())
    if not (future | too_old | in_progress).any():
        _say(logging.INFO, "Date range: all dates between %s and %s", EARLIEST_VALID_DATE.date(), cutoff.date())
    return df[~(future | too_old | in_progress)]


# ── 4. duplicates ───────────────────────────────────────────────────────────────
def drop_duplicate_incidents(df: pd.DataFrame, 
                             target: pd.Period, 
                             failures: list[str]) -> pd.DataFrame:
    
    """Drop rows with duplicate incident ids, conflicting incidents, and calculate if duplicate rate is within acceptable
    threshold"""
    
    dup = df.duplicated("incident", keep="first")  # same keep-first as before, so output doesn't shift
    in_target = _from_target_month(df, target)
    overall_rate = dup.mean() if len(df) else 0.0
    
    # get rate of duplicates in target month
    target_rate = dup[in_target].mean() if in_target.any() else 0.0

    for scope, rate in (("all", overall_rate), (f"{target} PDF", target_rate)):
        if rate > MAX_DUPLICATE_RATE:
            msg = f"{rate:.1%} of {scope} rows are duplicate incident IDs (limit {MAX_DUPLICATE_RATE:.0%})"
            _say(logging.ERROR, msg)
            failures.append(msg)

    # The usual carryover repeats the whole row. Same ID with different content is something
    # else (ID collision / bad parse), and keep-first would quietly throw one version away.
    repeated = df[df.duplicated("incident", keep=False)]
    versions = repeated[["incident", "reported", "nature", "incident_address"]].drop_duplicates()["incident"].value_counts()
    conflicting = versions[versions > 1].index.tolist()
    if conflicting:
        _say(logging.WARNING, "%s duplicated incident IDs have conflicting content (kept the first) -- first IDs: %s",
             len(conflicting), conflicting[:10])

    if dup.any():
        _say(logging.INFO, "Dropped %s duplicate rows (%.2f%% of rows, same incident ID logged more than once)",
             int(dup.sum()), overall_rate * 100)
    else:
        _say(logging.INFO, "Duplicates: none found")
    return df[~dup]


# ── 5. row count ────────────────────────────────────────────────────────────────
def check_row_count(df: pd.DataFrame, 
                    target: pd.Period, 
                    failures: list[str]) -> None:
    
    
    # get number of rows for each month
    counts = df.groupby(df["_dt"].dt.to_period("M")).size()
    
    # get number of rows for target month
    target_count = int(counts.get(target, 0))

    # No rows for the month at all always fails, even with nothing to compare against --
    # otherwise an empty scrape (blocked page, etc.) would sail through and replace good data.
    if target_count == 0:
        msg = f"Row count: no rows at all for {target} -- LVPD may not have posted that month yet, or the scrape came back empty."
        _say(logging.ERROR, msg)
        failures.append(msg)
        return

    baseline_months = [target - i for i in range(1, BASELINE_MONTHS + 1)]
    baseline = {str(p): int(counts[p]) for p in baseline_months if p in counts.index}
    if not baseline:
        _say(logging.WARNING, "Row count: no earlier months to compare %s against, skipping this check", target)
        return

    # average # rows per month
    avg = sum(baseline.values()) / len(baseline)
    ratio = target_count / avg
    detail = f"{target_count:,} rows in {target} vs {avg:,.0f} avg of {list(baseline)} ({ratio:.0%})"

    if ratio < ROW_COUNT_MIN_RATIO:
        msg = f"Row count too low: {detail} -- under {ROW_COUNT_MIN_RATIO:.0%}. LVPD may not have posted the whole month yet."
    elif ratio > ROW_COUNT_MAX_RATIO:
        msg = f"Row count too high: {detail} -- over {ROW_COUNT_MAX_RATIO:.0%}. Possible double scrape."
    else:
        _say(logging.INFO, "Row count OK: %s", detail)
        return
    _say(logging.ERROR, msg)
    failures.append(msg)


# ── run everything ──────────────────────────────────────────────────────────────
def validate_raw_data(
    raw: pd.DataFrame,
    cutoff: pd.Timestamp,
    now: pd.Timestamp | None = None,
    allow_failures: bool = False,
) -> pd.DataFrame:
    
    """Run all checks on the raw scraper output and return the cleaned rows.

    Raises DataValidationError if anything fails, unless allow_failures is True (then the failures
    are still logged as errors, but we carry on with the cleaned data). A schema mismatch always
    raises -- there's nothing sensible to carry on with.
    """
    now = pd.Timestamp.now() if now is None else pd.Timestamp(now)
    target = pd.Period(cutoff, "M")
    _say(logging.INFO, "Validating %s rows -- target month %s (cutoff %s)", f"{len(raw):,}", target, cutoff)

    check_schema(raw)

    df = raw.copy()
    df["_dt"] = pd.to_datetime(df["reported"], format=REPORTED_FORMAT, errors="coerce")

    failures: list[str] = []
    df = check_required_fields(df, target, failures)
    df = drop_out_of_range_dates(df, cutoff, now)
    df = drop_duplicate_incidents(df, target, failures)
    check_row_count(df, target, failures)

    if failures:
        if not allow_failures:
            raise DataValidationError("; ".join(failures))
        
        _say(logging.WARNING, "%s validation failure(s) ignored (--allow-validation-failures)", len(failures))
    else:
        _say(logging.INFO, "Validation passed: %s rows in, %s rows out", f"{len(raw):,}", f"{len(df):,}")

    return df.drop(columns="_dt").reset_index(drop=True)
