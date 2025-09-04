#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Laundrlab Potts Point live machine status into CSV.
- Handles the embedded iframe (wa.sqinsights.com).
- Appends one row per machine per run with a timestamp.
"""

import asyncio
import csv
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

URL = "https://laundrlab.com.au/live-status-potts-point/"

# Write the CSV in your Documents folder so it’s easy to find in Finder/Excel
OUT_CSV = Path.home() / "Documents" / "laundrlab-bot" / "laundrlab_potts_status.csv"

# Snapshots for troubleshooting (optional to view in a browser)
PARENT_HTML_SNAPSHOT = Path.home() / "Documents" / "laundrlab-bot" / "laundrlab_parent_last.html"
IFRAME_HTML_SNAPSHOT = Path.home() / "Documents" / "laundrlab-bot" / "laundrlab_iframe_last.html"

TIMEOUT_MS = 60_000

# Flip this to True ONCE if you want to watch the browser load the page
DEBUG_HEADLESS = False  # True = hidden (default), False = visible for a test run


def ensure_csv_header() -> None:
    if not OUT_CSV.exists():
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["timestamp_local", "machine", "size", "status", "extra"])


def clean_text(s: Optional[str]) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = s.replace("Create Alert", "").replace("•", "").strip()
    return s


async def grab_rows() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=DEBUG_HEADLESS)
        page = await browser.new_page()
        await page.goto(URL, timeout=TIMEOUT_MS, wait_until="networkidle")
        PARENT_HTML_SNAPSHOT.write_text(await page.content(), encoding="utf-8")

        # Find the wa.sqinsights.com iframe
        target_frame = None
        # Wait for *any* iframe first (desktop and mobile layouts differ)
        await page.wait_for_selector("iframe", timeout=TIMEOUT_MS)
        for fr in page.frames:
            if "wa.sqinsights.com" in (fr.url or ""):
                target_frame = fr
                break

        if target_frame is None:
            await browser.close()
            return rows  # No iframe visible yet

        # Wait for the table body rows inside the iframe
        await target_frame.wait_for_selector("table tbody tr", timeout=TIMEOUT_MS)

        # Optional snapshot if same-origin allows it
        try:
            IFRAME_HTML_SNAPSHOT.write_text(await target_frame.content(), encoding="utf-8")
        except Exception:
            pass  # Cross-origin .content() may be blocked — locators still work

        # Pick the table that has a "Machine" header
        tables = target_frame.locator("table")
        tcount = await tables.count()
        table_locator = None
        for i in range(tcount):
            tloc = tables.nth(i)
            head_txt = clean_text(await tloc.inner_text())
            if re.search(r"\bMachine\b", head_txt, flags=re.I):
                table_locator = tloc
                break
        if table_locator is None and tcount > 0:
            table_locator = tables.nth(0)

        if table_locator is None:
            await browser.close()
            return rows

        body_rows = table_locator.locator("tbody tr")
        n = await body_rows.count()

        for i in range(n):
            tds = body_rows.nth(i).locator("td")
            td_count = await tds.count()
            if td_count == 0:
                continue

            machine = clean_text(await tds.nth(0).inner_text()) if td_count >= 1 else ""
            size    = clean_text(await tds.nth(1).inner_text()) if td_count >= 2 else ""
            status  = clean_text(await tds.nth(2).inner_text()) if td_count >= 3 else ""
            extra   = clean_text(await tds.nth(3).inner_text()) if td_count >= 4 else ""

            # Normalise “10 min left” into the extra column
            m = re.search(r"(\d+)\s*min\.?\s*left", status, flags=re.I)
            if m:
                extra = f"{m.group(1)} min left"
                status = clean_text(re.sub(r"\d+\s*min\.?\s*left", "", status, flags=re.I))

            rows.append({"machine": machine, "size": size, "status": status or "Unknown", "extra": extra})

        await browser.close()
    return rows


def append_csv(items: List[Dict[str, str]]) -> None:
    ensure_csv_header()
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    with OUT_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not items:
            w.writerow([ts, "", "", "NO_ROWS_FOUND", ""])
            return
        for r in items:
            w.writerow([ts, r["machine"], r["size"], r["status"], r["extra"]])


async def run_once():
    try:
        items = await grab_rows()
        append_csv(items)
        # Print how many we captured (helps you see success in Terminal)
        print(f"Wrote {len(items)} machine rows to {OUT_CSV}")
    except PWTimeout as e:
        append_csv([])  # heartbeat row
        print(f"Timeout: {e}")
    except Exception as e:
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        with OUT_CSV.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([ts, "", "", f"ERROR: {type(e).__name__}", str(e)])
        print(f"Error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(run_once())
