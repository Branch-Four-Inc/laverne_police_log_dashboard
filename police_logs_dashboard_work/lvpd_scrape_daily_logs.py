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


NEWS_URL = "https://lvpd.org/news-statistics"
BASE_API = "https://lvpd.org/wp-json/wp/v2"
DEFAULT_DAILY_LOG_CATEGORY_ID = 39


MONTH_NAMES = {
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        )
    })
    return session


def fetch_news_page_context(session: requests.Session) -> dict:
    """Fetch the LVPD news/statistics page and extract helpful context from embedded scripts/HTML."""
    response = session.get(NEWS_URL, timeout=30)
    response.raise_for_status()
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

    return context


def fetch_daily_log_pdf_index(
    session: requests.Session,
    category_id: int = DEFAULT_DAILY_LOG_CATEGORY_ID,
    sleep_seconds: float = 0.2,
) -> pd.DataFrame:
    """Fetch all Document Library Pro PDF records from the LVPD daily-logs category."""
    all_docs = []
    page = 1

    while True:
        response = session.get(
            f"{BASE_API}/dlp_document",
            params={
                "doc_categories": category_id,
                "per_page": 100,
                "page": page,
                "_fields": "id,title,link,download_url,file_size,filename,date",
            },
            timeout=30,
        )

        if response.status_code != 200:
            break

        docs = response.json()
        if not docs:
            break

        all_docs.extend(docs)
        total_pages = int(response.headers.get("X-WP-TotalPages", 1))
        print(f"Fetched PDF index page {page}/{total_pages}: {len(docs)} docs")

        if page >= total_pages:
            break

        page += 1
        time.sleep(sleep_seconds)

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

                if col_texts["incident"] == "INCIDENT" or not col_texts["incident"]:
                    continue
                if col_texts["incident"].isdigit():
                    rows.append(col_texts)

    return rows


def download_and_parse_daily_logs(
    session: requests.Session,
    pdf_df: pd.DataFrame,
    output_zip: str | None = "lvpd_daily_logs.zip",
    sleep_seconds: float = 0.15,
) -> tuple[pd.DataFrame, list[dict]]:
    """Download all daily log PDFs, optionally zip them, and parse incident rows."""
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
                response = session.get(pdf_url, timeout=30)
                response.raise_for_status()
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

            time.sleep(sleep_seconds)

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
    parser.add_argument("--sleep", type=float, default=0.15, help="Delay between HTTP requests in seconds.")
    args = parser.parse_args()

    session = make_session()
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
    else:
        print("Failures: 0")


if __name__ == "__main__":
    main()
