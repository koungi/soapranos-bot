# laundrlab_potts.py
# Scrapes washer/dryer status and appends rows to a Google Sheet via Apps Script
# Works on GitHub Actions (headless Chromium via Playwright)
#
# Sheet columns written (in order):
# [timestamp_iso, machine_name, machine_type, status, detail]

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from datetime import datetime, timezone
from tenacity import retry, wait_fixed, stop_after_attempt
from typing import List, Dict
from pathlib import Path
import urllib.request
import json
import os
import re

TARGET_URL = os.getenv("TARGET_URL", "https://laundrlab.com.au/live-status-potts-point/")
SHEET_WEBAPP_URL = os.getenv(
    "SHEET_WEBAPP_URL",
    # You can keep this env-based, but since you asked for the fixed URL:
    "https://script.google.com/macros/s/AKfycbxSQkRvbFuaiWtxhvgg81S5AzAbCaIlJWRN-XDHT87SC_gH2fGyex1GOZ7pS540hN0W/exec"
)

# Optional local CSV for debugging (kept in repo /data)
REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DEBUG_CSV = DATA_DIR / "laundrlab_potts_status.csv"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _guess_type(name: str) -> str:
    n = name.lower()
    if "dryer" in n or "dry" in n:
        return "Dryer"
    if "washer" in n or "wash" in n or "laundry" in n:
        return "Washer"
    return ""


def _parse_status_cell(text: str) -> (str, str):
    """
    Returns (status, detail). Tries to extract things like "In Use (12m left)".
    """
    t = text.lower()
    status = "Unknown"
    if "available" in t or "vacant" in t or "free" in t:
        status = "Available"
    elif "in use" in t or "running" in t or "busy" in t or "occupied" in t:
        status = "In Use"
    elif "out of order" in t or "fault" in t or "error" in t or "down" in t:
        status = "Out of Order"

    # detail = anything that looks like time left
    m = re.search(r"(\d+\s*(?:min|mins|minutes|m))", t)
    detail = m.group(1) if m else ""
    return status, detail


def _rows_for_sheet(items: List[Dict]) -> List[List[str]]:
    ts = _now_iso()
    rows = []
    for it in items:
        rows.append([
            ts,
            it.get("name", ""),
            it.get("type", ""),
            it.get("status", ""),
            it.get("detail", "")
        ])
    return rows


def _post_to_sheet(rows: List[List[str]]) -> None:
    if not SHEET_WEBAPP_URL:
        print("SHEET_WEBAPP_URL not set; skipping Google Sheets upload.")
        return
    payload = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        SHEET_WEBAPP_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("Google Sheet response:", resp.read().decode("utf-8", "ignore"))


def _write_debug_csv(rows: List[List[str]]) -> None:
    """Optional: keep a simple CSV copy for debugging (not required for Sheets)."""
    try:
        if not DEBUG_CSV.exists():
            DEBUG_CSV.write_text("timestamp,machine_name,machine_type,status,detail\n", encoding="utf-8")
        with DEBUG_CSV.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(",".join(x.replace(",", " ") for x in r) + "\n")
    except Exception as e:
        print("Debug CSV write failed:", e)


def _extract_table_like(frame) -> List[Dict]:
    """
    Generic extractor for common 'table' layouts.
    Looks for <table><tbody><tr><td>… patterns first.
    Falls back to role=grid/row patterns if needed.
    """
    items: List[Dict] = []

    # Try standard table rows
    trs = frame.locator("table tbody tr")
    count = trs.count()
    if count == 0:
        # Try any tr
        trs = frame.locator("tr")
        count = trs.count()

    if count > 0:
        for i in range(count):
            tds = trs.nth(i).locator("th, td")
            c = tds.count()
            if c == 0:
                continue
            cols = []
            for j in range(c):
                cols.append(_normalise_text(tds.nth(j).inner_text()))
            # Heuristic mapping:
            #  - col0: machine name
            #  - col1 or col2: status
            name = cols[0] if cols else f"Machine {i+1}"
            status_cell = cols[1] if len(cols) > 1 else (cols[0] if cols else "")
            status, detail = _parse_status_cell(status_cell)
            items.append({
                "name": name,
                "type": _guess_type(name),
                "status": status,
                "detail": detail
            })
        return items

    # ARIA grid fallback
    rows = frame.locator('[role="row"]')
    rcount = rows.count()
    for i in range(rcount):
        cells = rows.nth(i).locator('[role="gridcell"], [role="cell"]')
        c = cells.count()
        if c == 0:
            continue
        cols = []
        for j in range(c):
            cols.append(_normalise_text(cells.nth(j).inner_text()))
        name = cols[0] if cols else f"Machine {i+1}"
        status_cell = cols[1] if len(cols) > 1 else (cols[0] if cols else "")
        status, detail = _parse_status_cell(status_cell)
        items.append({
            "name": name,
            "type": _guess_type(name),
            "status": status,
            "detail": detail
        })
    return items


def _extract_cards_like(frame) -> List[Dict]:
    """
    Fallback for 'card' UIs (div-based tiles):
    Grab visible tiles and try to split name/status.
    """
    items: List[Dict] = []
    tiles = frame.locator("div,li,section,article").filter(has_text=re.compile(r"washer|dryer|wash|dry|available|in use|out of order", re.I))
    tcount = min(100, tiles.count())
    for i in range(tcount):
        text = _normalise_text(tiles.nth(i).inner_text())
        if not text:
            continue
        # naive split: first token with '#' or namey bit
        name_match = re.search(r"(?:washer|dryer)\s*#?\s*\d+|(?:washer|dryer)[^\s,;:]+", text, re.I)
        name = name_match.group(0) if name_match else f"Machine {i+1}"
        status, detail = _parse_status_cell(text)
        items.append({
            "name": name,
            "type": _guess_type(name),
            "status": status,
            "detail": detail
        })
    return items


@retry(wait=wait_fixed(5), stop=stop_after_attempt(3))
def scrape() -> List[Dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        print("Navigating to:", TARGET_URL)
        page.goto(TARGET_URL, wait_until="networkidle", timeout=90_000)

        # Try to get the actual content (some sites nest inside an iframe)
        frame = None
        try:
            # If there’s a single significant iframe, use that
            iframes = page.locator("iframe")
            if iframes.count() > 0:
                frame = iframes.nth(0).content_frame()
        except PWTimeout:
            frame = None

        ctx = frame if frame is not None else page

        # Wait for something table-ish to appear (don’t fail the run if it doesn’t)
        try:
            ctx.wait_for_selector("table, [role='grid']", timeout=15_000)
        except PWTimeout:
            pass

        items = _extract_table_like(ctx)
        if not items:
            items = _extract_cards_like(ctx)

        browser.close()

    # If we truly found nothing, emit a heartbeat row so you can see the run happened
    if not items:
        items = [{
            "name": "N/A",
            "type": "",
            "status": "NO_ROWS_FOUND",
            "detail": ""
        }]

    return items


def main():
    try:
        items = scrape()
        rows = _rows_for_sheet(items)
        _post_to_sheet(rows)
        # Optional: keep a simple CSV copy in the repo for quick inspection
        _write_debug_csv(rows)
        print(f"Wrote {len(rows)} rows to Google Sheets.")
    except Exception as e:
        # Also write a heartbeat row on failure
        fail_rows = _rows_for_sheet([{
            "name": "N/A",
            "type": "",
            "status": "SCRAPE_ERROR",
            "detail": _normalise_text(str(e))[:200]
        }])
        try:
            _post_to_sheet(fail_rows)
            _write_debug_csv(fail_rows)
        finally:
            raise


if __name__ == "__main__":
    main()
