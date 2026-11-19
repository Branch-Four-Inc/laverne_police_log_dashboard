#!/usr/bin/env python3
"""
Visit the Streamlit app in a headless browser so Streamlit Community Cloud sees a real viewer
and doesn't hibernate it. Wakes the app if it is already asleep.

A plain HTTP request isn't enough: activity means a browser session connected over a websocket.

Usage:
    python scripts/keep_alive.py [URL]      # URL defaults to $STREAMLIT_APP_URL, then the live app

Exits 0 once the app has rendered, 1 if it never loads (so the GitHub Action run fails and notifies).
"""

import os
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://lavernepolicelogdashboard.streamlit.app/"
WAKE_BUTTON_TEXT = "get this app back up"   # "Yes, get this app back up!"
APP_READY_TEXT = "Disclaimer"               # heading at the bottom of app.py, so the whole script ran
LOAD_TIMEOUT_MS = 120_000                   # a cold start can take a minute or two
STAY_CONNECTED_MS = 10_000


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("STREAMLIT_APP_URL")) or DEFAULT_URL
    print(f"Visiting {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            # A hibernated app shows a wake-up button instead of the app
            wake = page.get_by_role("button", name=WAKE_BUTTON_TEXT, exact=False)
            try:
                wake.wait_for(state="visible", timeout=15_000)
                print("App was asleep; clicking the wake-up button.")
                wake.click()
            except PlaywrightTimeout:
                print("No wake-up button; app is already awake (or still loading).")

            # The app can render inside an iframe on some hosts, so look across all frames
            page.wait_for_function(
                "text => document.body && document.body.innerText.includes(text)",
                arg=APP_READY_TEXT,
                timeout=LOAD_TIMEOUT_MS,
            )
            print("App loaded.")
            page.wait_for_timeout(STAY_CONNECTED_MS)  # keep the websocket session open briefly
            return 0
        except PlaywrightTimeout:
            print(f"[error] App did not finish loading within {LOAD_TIMEOUT_MS // 1000}s.", file=sys.stderr)
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
