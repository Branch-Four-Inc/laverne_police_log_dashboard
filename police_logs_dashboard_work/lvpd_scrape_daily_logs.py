#!/usr/bin/env python3
"""
Standalone LVPD daily logs scraper.

Install dependencies:
    pip install requests beautifulsoup4 lxml pandas pdfplumber

Run:
    python lvpd_scrape_daily_logs.py --output-csv daily_logs.csv --output-zip lvpd_daily_logs.zip
"""

import argparse
import os
import re
import time
import zipfile
from io import BytesIO

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup
from pipeline_logging import get_logger, install_exception_logger


NEWS_URL = "https://lvpd.org/news-statistics"
BASE_API = "https://lvpd.org/wp-json/wp/v2"
DEFAULT_DAILY_LOG_CATEGORY_ID = 39
MAX_HTTP_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 1
MIN_PDF_BYTES = 5 * 1024
DEFAULT_REQUEST_DELAY_SECONDS = 0.5
LOGGER = get_logger()
install_exception_logger(LOGGER)


MONTH_NAMES = {
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
}


def structural_error(message: str) -> None:
    """Log a source-format change before stopping the affected scrape step."""
    LOGGER.error("Structural validation failed: %s", message)
    raise RuntimeError(f"Structural validation failed: {message}")


def make_session(request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS) -> requests.Session:
    if request_delay_seconds < 0:
        raise ValueError("request_delay_seconds cannot be negative")

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        )
    })
    session.lvpd_request_delay_seconds = request_delay_seconds
    session.lvpd_last_request_at = None
    return session


def set_request_delay(session: requests.Session, request_delay_seconds: float) -> None:
    """Configure the minimum spacing between all requests made by this session."""
    if request_delay_seconds < 0:
        raise ValueError("request_delay_seconds cannot be negative")
    session.lvpd_request_delay_seconds = request_delay_seconds


def wait_for_request_slot(session: requests.Session) -> None:
    """Wait until this session has respected its minimum request interval."""
    delay = getattr(session, "lvpd_request_delay_seconds", DEFAULT_REQUEST_DELAY_SECONDS)
    last_request_at = getattr(session, "lvpd_last_request_at", None)
    if last_request_at is not None:
        remaining = delay - (time.monotonic() - last_request_at)
        if remaining > 0:
            time.sleep(remaining)
    session.lvpd_last_request_at = time.monotonic()


def get_with_retries(
    session: requests.Session,
    url: str,
    *,
    min_bytes: int | None = None,
    **kwargs,
) -> requests.Response:
    """Require HTTP 200 and retry temporary or suspicious responses.

    The minimum-size check is used for daily-log PDFs, where a tiny response
    commonly means an error or block page was returned instead of a real PDF.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            wait_for_request_slot(session)
            response = session.get(url, **kwargs)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            if min_bytes is not None and len(response.content) < min_bytes:
                raise RuntimeError(
                    f"suspiciously small response: {len(response.content):,} bytes (minimum {min_bytes:,})"
                )
            return response
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt == MAX_HTTP_ATTEMPTS:
                break
            delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            print(f"[warn] Request failed ({exc}); retrying in {delay}s ({attempt}/{MAX_HTTP_ATTEMPTS})")
            LOGGER.warning(
                "Request to %s failed on attempt %s/%s: %s; retrying in %ss",
                url,
                attempt,
                MAX_HTTP_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)

    print(f"[error] Could not access {url} after {MAX_HTTP_ATTEMPTS} attempts: {last_error}")
    LOGGER.error("Could not access %s after %s attempts: %s", url, MAX_HTTP_ATTEMPTS, last_error)
    raise RuntimeError(f"Could not access {url}") from last_error


def fetch_news_page_context(session: requests.Session) -> dict:
    """Fetch the LVPD news/statistics page and extract helpful context from embedded scripts/HTML."""
    response = get_with_retries(session, NEWS_URL, timeout=30)
    soup = BeautifulSoup(response.content, "lxml")

    context = {
        "ajax_nonce": None,
        "month_folders": {},
        "dlp_folders_params": None,
        "posts_table_params_snippet": None,
    }

    for script in soup.find_all("script"):
        text = script.string or ""

        if "posts_table_params" in text:
            nonce_match = re.search(r'"ajax_nonce"\s*:\s*"([^"]+)"', text)
            if nonce_match:
                context["ajax_nonce"] = nonce_match.group(1)
            context["posts_table_params_snippet"] = text[:2000]

        if "dlp_folders_params" in text:
            params_match = re.search(
                r"dlp_folders_params\s*=\s*({.*?})\s*;\s*$",
                text,
                re.DOTALL | re.MULTILINE,
            )
            if params_match:
                raw = params_match.group(1).replace("\\/", "/")
                raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
                raw = re.sub(r"//[^\n]*", "", raw)
                context["dlp_folders_params"] = raw

    for li in soup.find_all("li", class_="dlp-folder"):
        label = li.find("span", class_="dlp-folder-label")
        category_id = li.get("data-category-id")
        if not label or not category_id:
            continue
        label_text = label.get_text(strip=True)
        if any(month in label_text for month in MONTH_NAMES):
            context["month_folders"][label_text] = category_id

    if not context["month_folders"]:
        structural_error(
            "expected one or more 'li.dlp-folder > span.dlp-folder-label' month folders on the news page"
        )

    return context


def fetch_daily_log_pdf_index(
    session: requests.Session,
    category_id: int = DEFAULT_DAILY_LOG_CATEGORY_ID,
    sleep_seconds: float = 0.2,
) -> pd.DataFrame:
    """Fetch all Document Library Pro PDF records from the LVPD daily-logs category."""
    set_request_delay(session, sleep_seconds)
    all_docs = []
    page = 1

    while True:
        response = get_with_retries(
            session,
            f"{BASE_API}/dlp_document",
            params={
                "doc_categories": category_id,
                "per_page": 100,
                "page": page,
                "_fields": "id,title,link,download_url,file_size,filename,date",
            },
            timeout=30,
        )

        try:
            docs = response.json()
        except ValueError as exc:
            structural_error(f"PDF index page {page} was not valid JSON: {exc}")
        if not isinstance(docs, list):
            structural_error(f"PDF index page {page} returned {type(docs).__name__}, not a document list")
        if not docs:
            if page == 1:
                structural_error("PDF index returned zero documents on its first page")
            break

        required_fields = {"title", "download_url"}
        for index, doc in enumerate(docs, start=1):
            missing_fields = required_fields - set(doc)
            title = doc.get("title")
            if missing_fields or not isinstance(title, dict) or not title.get("rendered") or not doc.get("download_url"):
                structural_error(
                    f"PDF index page {page}, document {index} is missing expected title.rendered or download_url fields"
                )

        all_docs.extend(docs)
        total_pages = int(response.headers.get("X-WP-TotalPages", 1))
        print(f"Fetched PDF index page {page}/{total_pages}: {len(docs)} docs")

        if page >= total_pages:
            break

        page += 1

    pdf_df = pd.DataFrame([
        {
            "title": doc["title"]["rendered"],
            "pdf_url": doc.get("download_url", ""),
            "filename": doc.get("filename", ""),
            "file_size": doc.get("file_size", ""),
            "date_posted": doc.get("date", ""),
            "page_url": doc.get("link", ""),
        }
        for doc in all_docs
    ])

    if pdf_df.empty:
        return pd.DataFrame(columns=["title", "pdf_url", "filename", "file_size", "date_posted", "page_url"])

    return pdf_df.sort_values("title").reset_index(drop=True)


def parse_daily_log(pdf_bytes: bytes) -> list[dict]:
    """Parse one daily log PDF using character x-positions to extract table columns."""
    col_bounds = [0, 95, 190, 280, 9999]
    col_names = ["incident", "reported", "nature", "incident_address"]

    rows = []
    table_header_found = False
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            chars = page.chars
            if not chars:
                continue

            row_groups = {}
            for char in chars:
                row_key = round(char["top"], 0)
                row_groups.setdefault(row_key, []).append(char)

            for top in sorted(row_groups):
                row_chars = sorted(row_groups[top], key=lambda char: char["x0"])
                col_texts = {name: "" for name in col_names}

                for char in row_chars:
                    x = char["x0"]
                    for idx in range(len(col_bounds) - 1):
                        if col_bounds[idx] <= x < col_bounds[idx + 1]:
                            col_texts[col_names[idx]] += char["text"]
                            break

                col_texts = {key: value.strip() for key, value in col_texts.items()}

                if col_texts["incident"] == "INCIDENT":
                    populated_columns = sum(bool(value) for value in col_texts.values())
                    if populated_columns < len(col_names):
                        structural_error(
                            f"daily-log table header had {populated_columns}/{len(col_names)} expected columns"
                        )
                    table_header_found = True
                    continue
                if not col_texts["incident"]:
                    continue
                if col_texts["incident"].isdigit():
                    rows.append(col_texts)

    if not table_header_found:
        structural_error("daily-log PDF did not contain the expected four-column table header")

    return rows


def download_and_parse_daily_logs(
    session: requests.Session,
    pdf_df: pd.DataFrame,
    output_zip: str | None = "lvpd_daily_logs.zip",
    sleep_seconds: float = 0.15,
) -> tuple[pd.DataFrame, list[dict]]:
    """Download all daily log PDFs, optionally zip them, and parse incident rows."""
    set_request_delay(session, sleep_seconds)
    log_pdfs = pdf_df[pdf_df["title"] != "Crime Reports Legend"].copy()
    print(f"Daily log PDFs to process: {len(log_pdfs)}")

    all_rows = []
    failed = []
    zip_buffer = BytesIO() if output_zip else None
    zip_handle = zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) if zip_buffer else None

    try:
        for idx, (_, row) in enumerate(log_pdfs.iterrows(), start=1):
            title = row["title"]
            pdf_url = row["pdf_url"]

            try:
                response = get_with_retries(session, pdf_url, timeout=30, min_bytes=MIN_PDF_BYTES)
                pdf_bytes = response.content

                if zip_handle is not None:
                    zip_handle.writestr(f"{title}.pdf", pdf_bytes)

                parsed_rows = parse_daily_log(pdf_bytes)
                for parsed_row in parsed_rows:
                    parsed_row["log_date"] = title
                all_rows.extend(parsed_rows)

                if idx % 50 == 0:
                    print(f"Processed {idx}/{len(log_pdfs)} PDFs — {len(all_rows)} incident rows so far")

            except Exception as exc:
                failed.append({"title": title, "url": pdf_url, "error": str(exc)})
                LOGGER.error("Could not download or parse %s (%s): %s", title, pdf_url, exc)

    finally:
        if zip_handle is not None:
            zip_handle.close()

    if output_zip and zip_buffer is not None:
        with open(output_zip, "wb") as file:
            file.write(zip_buffer.getvalue())
        zip_size_mb = os.path.getsize(output_zip) / (1024 * 1024)
        print(f"Saved PDF ZIP: {output_zip} ({zip_size_mb:.1f} MB)")

    daily_logs_df = pd.DataFrame(all_rows)
    if not daily_logs_df.empty:
        daily_logs_df = daily_logs_df[["log_date", "incident", "reported", "nature", "incident_address"]]
    else:
        daily_logs_df = pd.DataFrame(columns=["log_date", "incident", "reported", "nature", "incident_address"])

    return daily_logs_df, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape and parse LVPD daily police log PDFs.")
    parser.add_argument("--output-csv", default="daily_logs.csv", help="Path for parsed incident CSV output.")
    parser.add_argument("--output-zip", default="lvpd_daily_logs.zip", help="Path for downloaded PDF ZIP output. Use empty string to skip.")
    parser.add_argument("--category-id", type=int, default=DEFAULT_DAILY_LOG_CATEGORY_ID, help="LVPD daily-log document category ID.")
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help="Minimum delay between every HTTP request in seconds (default: 0.5).",
    )
    args = parser.parse_args()
    LOGGER.info(
        "Scraper started: output_csv=%s, output_zip=%s, request_delay=%ss",
        args.output_csv,
        args.output_zip or "disabled",
        args.sleep,
    )

    session = make_session(args.sleep)
    context = fetch_news_page_context(session)
    print(f"Fresh nonce found: {context['ajax_nonce']}")
    print(f"Month folders found: {len(context['month_folders'])}")

    pdf_df = fetch_daily_log_pdf_index(session, category_id=args.category_id, sleep_seconds=args.sleep)
    print(f"Total daily log PDF records found: {len(pdf_df)}")
    if not pdf_df.empty:
        print(f"Title range: {pdf_df['title'].min()} to {pdf_df['title'].max()}")

    output_zip = args.output_zip or None
    daily_logs_df, failed = download_and_parse_daily_logs(
        session=session,
        pdf_df=pdf_df,
        output_zip=output_zip,
        sleep_seconds=args.sleep,
    )

    daily_logs_df.to_csv(args.output_csv, index=False)
    print(f"Saved CSV: {args.output_csv} ({len(daily_logs_df):,} rows)")

    if failed:
        failed_path = "failed_daily_log_downloads.csv"
        pd.DataFrame(failed).to_csv(failed_path, index=False)
        print(f"Failures: {len(failed)} — saved details to {failed_path}")
        LOGGER.warning("Scraper finished with %s failed PDF(s); details saved to %s", len(failed), failed_path)
    else:
        print("Failures: 0")
        LOGGER.info("Scraper finished successfully: %s rows from %s PDFs", len(daily_logs_df), len(pdf_df))


if __name__ == "__main__":
    main()
