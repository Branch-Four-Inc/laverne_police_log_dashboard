#!/usr/bin/env python3
"""
Visit the Streamlit app in a headless browser so Streamlit Community Cloud sees a real viewer
and doesn't hibernate it. Wakes the app first if it is already asleep, but always finishes by
confirming the app itself loaded (clicking the wake-up button alone doesn't count as success).

A plain HTTP request isn't enough: activity means a browser session connected over a websocket.

Usage:
    python scripts/keep_alive.py [URL]               # one visit (what the GitHub Action runs every 4 hours)
    python scripts/keep_alive.py --every-hours 4     # visit now, then every 4 hours until stopped (local use)

URL defaults to $STREAMLIT_APP_URL, then the live app.
Exits 0 once the app has rendered, 1 if it never loads (so the GitHub Action run fails and notifies).
"""

import argparse
import os
import sys
import time
from datetime import datetime

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://lavernepolicelogdashboard.streamlit.app/"
WAKE_BUTTON_TEXT = "get this app back up"   # "Yes, get this app back up!"
APP_READY_TEXT = "Disclaimer"               # heading at the bottom of app.py, so the whole script ran
LOAD_TIMEOUT_MS = 120_000                   # a cold start can take a minute or two
STAY_CONNECTED_MS = 30_000                  # stay on the app this long once it has loaded, then exit
ATTEMPTS = 3


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def wake_if_asleep(page: Page) -> bool:
    """Click the wake-up button if the app is hibernated. Returns True if it was clicked."""
    wake = page.get_by_role("button", name=WAKE_BUTTON_TEXT, exact=False)
    try:
        wake.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeout:
        return False
    wake.click()
    return True


def app_loaded(page: Page) -> bool:
    """Wait for the app's own content (not the wake-up page or a loading shell)."""
    try:
        page.wait_for_function(
            "text => document.body && document.body.innerText.includes(text)",
            arg=APP_READY_TEXT,
            timeout=LOAD_TIMEOUT_MS,
        )
        return True
    except PlaywrightTimeout:
        return False


def visit(url: str) -> bool:
    """Open the app, wake it if needed, and confirm it rendered. Retries with a fresh page load."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for attempt in range(1, ATTEMPTS + 1):
                log(f"Visiting {url} (attempt {attempt}/{ATTEMPTS})")
                page = browser.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    if wake_if_asleep(page):
                        log("App was asleep; clicked the wake-up button.")
                    else:
                        log("No wake-up button; app is already awake (or still loading).")

                    if app_loaded(page):
                        log(f"App loaded. Staying connected for {STAY_CONNECTED_MS // 1000}s.")
                        page.wait_for_timeout(STAY_CONNECTED_MS)  # keep the websocket session open
                        log("Done; closing the browser.")
                        return True
                    log(f"App did not finish loading within {LOAD_TIMEOUT_MS // 1000}s.")
                except PlaywrightTimeout:
                    log("Timed out opening the page.")
                finally:
                    page.close()
            return False
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep the Streamlit app awake by visiting it.")
    parser.add_argument("url", nargs="?", default=None, help="App URL (default: $STREAMLIT_APP_URL, then the live app)")
    parser.add_argument("--every-hours", type=float, default=None,
                        help="Keep running and visit again this often (for running on your own machine; "
                             "the GitHub Action uses its own schedule instead)")
    args = parser.parse_args()
    url = args.url or os.environ.get("STREAMLIT_APP_URL") or DEFAULT_URL

    if args.every_hours is None:
        ok = visit(url)
        if not ok:
            log("[error] Could not load the app.")
        return 0 if ok else 1

    while True:
        if not visit(url):
            log("[error] Could not load the app; will try again at the next interval.")
        log(f"Next visit in {args.every_hours:g} hours.")
        time.sleep(args.every_hours * 3600)


if __name__ == "__main__":
    sys.exit(main())
