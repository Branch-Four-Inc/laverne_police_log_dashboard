"""
Tests for validation.py. Run from the repo root (no extra packages needed, it's plain unittest):

    python -m unittest discover -s police_logs_dashboard_work -v
"""

import contextlib
import io
import logging
import unittest

import pandas as pd

import validation as v
from validation import DataValidationError

# The module logs to the pipeline logger. Give it a handler so Python doesn't dump warnings to
# stderr mid-test (assertLogs still sees everything).
logging.getLogger(v.LOGGER_NAME).addHandler(logging.NullHandler())

CUTOFF = pd.Timestamp("2026-05-31 23:59:59")   # target month = May 2026
NOW = pd.Timestamp("2026-06-15 12:00:00")


def make_rows(month: str, 
              n: int, 
              id_start: int = 1, **overrides) -> pd.DataFrame:
    
    """n clean raw-scraper rows spread across `month` (e.g. '2026-03')."""
    
    p = pd.Period(month, "M")
    rows = []
    for i in range(n):
        ts = pd.Timestamp(year=p.year, month=p.month, day=i % p.days_in_month + 1,
                          hour=(i * 7) % 24, minute=i % 60, second=(i * 13) % 60)
        row = {
            "log_date": ts.strftime("%m.%d.%Y"),
            "incident": f"{p.year % 100:02d}{p.month:02d}{id_start + i:05d}",
            "reported": ts.strftime("%H:%M:%S %m/%d/%y"),
            "nature": "PDOBS",
            "incident_address": "123 MAIN ST",
        }
        row.update(overrides)
        rows.append(row)
    return pd.DataFrame(rows)


def one_row(reported: str, 
            incident: str, 
            log_date: str, **overrides) -> pd.DataFrame:
    
    """A single raw row with the `reported` string exactly as given (to hand-craft edge cases)."""
    
    row = {"log_date": log_date, "incident": incident, "reported": reported,
           "nature": "PDOBS", "incident_address": "123 MAIN ST"}
    row.update(overrides)
    return pd.DataFrame([row])


def healthy(per_month: int = 100) -> pd.DataFrame:
    
    """Feb-May 2026, same row count every month, nothing wrong with any of it."""
    
    return pd.concat([make_rows(m, per_month) for m in ("2026-02", "2026-03", "2026-04", "2026-05")],
                     ignore_index=True)


def run(df: pd.DataFrame, **kw) -> pd.DataFrame:
    return v.validate_raw_data(df, cutoff=CUTOFF, now=NOW, **kw)


class Quiet(unittest.TestCase):
    """The checks echo to the terminal like the real pipeline does -- keep that out of test output."""

    def setUp(self):
        stdout = contextlib.redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)


class TestPreviousMonthEnd(Quiet):
    def test_is_last_second_of_previous_month(self):
        self.assertEqual(v.previous_month_end(pd.Timestamp("2026-09-30 16:40:12")), pd.Timestamp("2026-08-31 23:59:59"))

    def test_rolls_back_across_a_year(self):
        self.assertEqual(v.previous_month_end(pd.Timestamp("2026-01-15")), pd.Timestamp("2025-12-31 23:59:59"))

    def test_first_of_the_month_at_midnight(self):
        self.assertEqual(v.previous_month_end(pd.Timestamp("2026-03-01 00:00:00")), pd.Timestamp("2026-02-28 23:59:59"))

    def test_late_on_the_last_day_is_not_cut_off(self):
        # the old cutoff kept the run's time of day: a 4:40 PM run dropped a 8 PM incident on the 31st
        cutoff = v.previous_month_end(pd.Timestamp("2026-09-30 16:40:12"))
        self.assertLessEqual(pd.Timestamp("2026-08-31 20:00:00"), cutoff)


class TestSchema(Quiet):
    def test_passes_in_any_column_order(self):
        df = healthy()[list(reversed(v.EXPECTED_RAW_COLUMNS))]
        self.assertEqual(len(run(df)), 400)

    def test_missing_column_raises_and_names_it(self):
        with self.assertRaisesRegex(DataValidationError, "incident_address"):
            run(healthy().drop(columns="incident_address"))

    def test_unexpected_column_raises_and_names_it(self):
        df = healthy().assign(priority="HIGH")
        with self.assertRaisesRegex(DataValidationError, "priority"):
            run(df)

    def test_schema_error_raises_even_when_failures_are_allowed(self):
        with self.assertRaises(DataValidationError):
            run(healthy().drop(columns="nature"), allow_failures=True)


class TestRequiredFields(Quiet):
    def test_a_few_blank_addresses_are_dropped_not_fatal(self):
        df = healthy()
        df.loc[[310, 320], "incident_address"] = ""
        with self.assertLogs(v.LOGGER_NAME, level="WARNING") as logs:
            out = run(df)
        self.assertEqual(len(out), 398)
        self.assertTrue(any("Dropped 2 rows missing a required field" in m for m in logs.output))

    def test_unparseable_timestamp_counts_as_missing_date(self):
        df = healthy()
        df.loc[350, "reported"] = "3000:26:33 02/18/"   # real example of the garbled PDF rows
        self.assertEqual(len(run(df)), 399)

    def test_whitespace_only_and_nan_count_as_blank(self):
        df = healthy()
        df.loc[310, "nature"] = "   "
        df.loc[320, "incident_address"] = None
        self.assertEqual(len(run(df)), 398)

    def test_limit_is_over_25_percent_not_at_25(self):
        at_limit = healthy()
        at_limit.loc[300:324, "incident_address"] = ""      # 25 of May's 100 rows
        self.assertEqual(len(run(at_limit)), 375)
        over = healthy()
        over.loc[300:325, "incident_address"] = ""          # 26 of May's 100 rows
        with self.assertRaisesRegex(DataValidationError, "missing a required field"):
            run(over)

    def test_broken_new_month_is_caught_even_when_history_is_fine(self):
        # 3 good months x 1000 rows + a May where 30% have no address. Across the whole file
        # that's only 7.5% -- the target-month check is what catches it.
        df = pd.concat([make_rows(m, 1000) for m in ("2026-02", "2026-03", "2026-04", "2026-05")], ignore_index=True)
        df.loc[3000:3299, "incident_address"] = ""
        with self.assertRaises(DataValidationError) as ctx:
            run(df)
        self.assertIn("2026-05 PDF rows are missing a required field", str(ctx.exception))
        self.assertNotIn("of all rows", str(ctx.exception))

    def test_failure_names_the_field_that_is_missing(self):
        df = healthy()
        df.loc[300:340, "nature"] = ""
        with self.assertRaisesRegex(DataValidationError, "incident type"):
            run(df)


class TestDateRange(Quiet):
    def kept(self, extra: pd.DataFrame) -> set:
        return set(run(pd.concat([healthy(), extra], ignore_index=True))["incident"])

    def test_future_dates_are_dropped_and_logged(self):
        extra = one_row("12:00:00 07/01/26", "260799001", "07.01.2026")
        with self.assertLogs(v.LOGGER_NAME, level="WARNING") as logs:
            kept = self.kept(extra)
        self.assertNotIn("260799001", kept)
        self.assertTrue(any("in the future" in m for m in logs.output))

    def test_garbled_two_digit_year_lands_in_the_future_and_is_dropped(self):
        self.assertNotIn("260599002", self.kept(one_row("12:00:00 05/15/62", "260599002", "05.15.2026")))

    def test_dates_before_the_archive_starts_are_dropped(self):
        extra = one_row("12:00:00 12/31/24", "241299001", "12.31.2024")
        with self.assertLogs(v.LOGGER_NAME, level="WARNING") as logs:
            kept = self.kept(extra)
        self.assertNotIn("241299001", kept)
        self.assertTrue(any("before 2025-01-01" in m for m in logs.output))

    def test_earliest_valid_date_itself_is_kept(self):
        self.assertIn("250199001", self.kept(one_row("00:00:00 01/01/25", "250199001", "01.01.2025")))

    def test_month_still_in_progress_is_held_back_without_failing(self):
        extra = one_row("12:00:00 06/10/26", "260699001", "06.10.2026")
        with self.assertLogs(v.LOGGER_NAME, level="INFO") as logs:
            kept = self.kept(extra)
        self.assertNotIn("260699001", kept)
        self.assertTrue(any("Held back 1 rows" in m for m in logs.output))

    def test_last_second_of_the_target_month_is_kept_and_next_second_is_not(self):
        extra = pd.concat([one_row("23:59:59 05/31/26", "260599003", "05.31.2026"),
                           one_row("00:00:00 06/01/26", "260699003", "06.01.2026")])
        kept = self.kept(extra)
        self.assertIn("260599003", kept)
        self.assertNotIn("260699003", kept)


class TestDuplicates(Quiet):
    def test_repeats_are_dropped_and_the_first_copy_is_kept(self):
        base = healthy()
        carryover = base.loc[[300, 301, 302]].copy()
        carryover["log_date"] = "06.01.2026"   # same incident showing up again in the next day's PDF
        out = run(pd.concat([base, carryover], ignore_index=True))
        self.assertEqual(len(out), 400)
        self.assertFalse(out["incident"].duplicated().any())
        self.assertFalse((out["log_date"] == "06.01.2026").any())

    def test_a_normal_amount_of_carryover_passes(self):
        base = healthy()
        out = run(pd.concat([base, base.loc[300:304]], ignore_index=True))   # 5 repeats of May's 100
        self.assertEqual(len(out), 400)

    def test_too_many_repeats_fails(self):
        base = healthy()
        with self.assertRaisesRegex(DataValidationError, "duplicate incident IDs"):
            run(pd.concat([base, base.loc[300:314]], ignore_index=True))      # 15 repeats of May's 100

    def test_double_scraped_new_month_is_caught_even_when_history_is_fine(self):
        # Overall only ~7% repeats, but 23% of May's rows are repeats.
        df = pd.concat([make_rows(m, 1000) for m in ("2026-02", "2026-03", "2026-04", "2026-05")], ignore_index=True)
        df = pd.concat([df, df.loc[3000:3299]], ignore_index=True)
        with self.assertRaises(DataValidationError) as ctx:
            run(df)
        self.assertIn("2026-05 PDF rows are duplicate", str(ctx.exception))
        self.assertNotIn("of all rows", str(ctx.exception))

    def test_same_id_with_different_content_warns_but_does_not_fail(self):
        base = healthy()
        clash = base.loc[[300]].copy()
        clash["incident_address"] = "999 OTHER ST"
        with self.assertLogs(v.LOGGER_NAME, level="WARNING") as logs:
            out = run(pd.concat([base, clash], ignore_index=True))
        self.assertEqual(len(out), 400)
        self.assertTrue(any("conflicting content" in m for m in logs.output))

    def test_garbled_rows_sharing_an_id_are_not_counted_as_duplicates(self):
        # Mirrors the Feb 2025 PDFs: dozens of garbled rows that all ended up with the same
        # truncated incident ID. If duplicates were checked before the bad rows are thrown out,
        # that's 59 "duplicates" out of 460 rows (12.8%) and the run fails for the wrong reason.
        garbled = pd.concat([one_row(f"30{i:02d}:26:33 02/18/", "2502009", "02.18.2026") for i in range(60)],
                            ignore_index=True)
        out = run(pd.concat([healthy(), garbled], ignore_index=True))
        self.assertEqual(len(out), 400)


class TestRowCount(Quiet):
    
    """Set one's row counts and all others to default 100"""
    
    def months(self, 
               may: int, 
               others: int = 100) -> pd.DataFrame:
        
        return pd.concat([make_rows("2026-02", others), make_rows("2026-03", others),
                          make_rows("2026-04", others), make_rows("2026-05", may)], ignore_index=True)

    def test_matching_the_baseline_passes(self):
        self.assertEqual(len(run(self.months(may=100))), 400)

    def test_lower_edge_is_inclusive_at_50_percent(self):
        self.assertEqual(len(run(self.months(may=50))), 350)
        
        with self.assertRaisesRegex(DataValidationError, "Row count too low"):
            run(self.months(may=22))

    def test_upper_edge_is_inclusive_at_250_percent(self):
        self.assertEqual(len(run(self.months(may=250))), 550)
        with self.assertRaisesRegex(DataValidationError, "Row count too high"):
            run(self.months(may=251))

    def test_a_target_month_with_no_rows_fails(self):
        df = pd.concat([make_rows(m, 100) for m in ("2026-02", "2026-03", "2026-04")], ignore_index=True)
        with self.assertRaisesRegex(DataValidationError, "no rows at all for 2026-05"):
            run(df)

    def test_an_empty_scrape_fails_instead_of_slipping_through(self):
        with self.assertRaisesRegex(DataValidationError, "no rows at all"):
            run(healthy().iloc[0:0])

    def test_no_history_to_compare_against_skips_the_check_with_a_warning(self):
        with self.assertLogs(v.LOGGER_NAME, level="WARNING") as logs:
            out = run(make_rows("2026-05", 100))
        self.assertEqual(len(out), 100)
        self.assertTrue(any("no earlier months" in m for m in logs.output))

    def test_works_with_fewer_than_three_baseline_months(self):
        
        df = pd.concat([make_rows("2026-04", 100), make_rows("2026-05", 100)], ignore_index=True)
        self.assertEqual(len(run(df)), 200)
        low = pd.concat([make_rows("2026-04", 100), make_rows("2026-05", 22)], ignore_index=True)
        
        with self.assertRaisesRegex(DataValidationError, "Row count too low"):
            run(low)

    def test_only_the_three_months_before_the_target_count(self):
        # Jan 2026 is four months back -- if it leaked into the average this would fail as "too low"
        df = pd.concat([make_rows("2026-01", 5000), self.months(may=100)], ignore_index=True)
        self.assertEqual(len(run(df)), 5400)


class TestRunEverything(Quiet):
    def test_clean_data_passes_untouched(self):
        df = healthy()
        out = run(df)
        pd.testing.assert_frame_equal(out, df)

    def test_returns_raw_shaped_rows_with_no_helper_columns(self):
        out = run(healthy())
        self.assertEqual(list(out.columns), list(healthy().columns))
        self.assertTrue((out["reported"].str.match(r"\d\d:\d\d:\d\d \d\d/\d\d/\d\d")).all())   # still the scraper's string format

    def test_does_not_modify_the_input(self):
        df = healthy()
        before = df.copy()
        run(df)
        pd.testing.assert_frame_equal(df, before)

    def test_reports_every_failure_at_once(self):
        # May has only 10 rows (too low) AND 5 of them have no address (50% of May's PDFs)
        df = pd.concat([make_rows(m, 100) for m in ("2026-02", "2026-03", "2026-04")] + [make_rows("2026-05", 10)],
                       ignore_index=True)
        df.loc[300:304, "incident_address"] = ""
        with self.assertRaises(DataValidationError) as ctx:
            run(df)
        self.assertIn("missing a required field", str(ctx.exception))
        self.assertIn("Row count too low", str(ctx.exception))

    def test_allow_failures_logs_errors_but_returns_the_cleaned_rows(self):
        df = pd.concat([make_rows(m, 100) for m in ("2026-02", "2026-03", "2026-04")] + [make_rows("2026-05", 10)],
                       ignore_index=True)
        with self.assertLogs(v.LOGGER_NAME, level="ERROR") as logs:
            out = run(df, allow_failures=True)
        self.assertEqual(len(out), 310)
        self.assertTrue(any("Row count too low" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
