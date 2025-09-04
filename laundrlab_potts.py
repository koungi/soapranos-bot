# laundrlab_potts.py
# Scrape washer/dryer status from the sqinsights iframe and append rows to Google Sheets.
# Falls back to scraping via the WordPress page if TARGET_URL is that page.

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from datetime import datetime, timezone
from tenacity import retry, wait_fixed, stop_after_attempt
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import urllib.request
import json
import os
import re

# --- Config -------------------------------------------------------------

# Best: point straight at the iframe URL (bypasses lazyload on WP page)
DEFAULT_TARGET = "https://wa.sqinsights.com/182471?room=2778"

TARGET_URL = os.getenv("TARGET_URL", DEFAULT_TARGET)

SHEET_WEBAPP_URL = os.getenv(
    "SHEET_WEBAPP_URL",
    "https://script.google.com/macros/s/AKfycbxSQkRvbFuaiWtxhvgg81S5AzAbCaIlJWRN-XDHT87SC_gH2fGyex1GOZ7pS540hN0W/exec"
)

# Optional debug CSV (written to ./data during the run so you can tail in logs)
REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DEBUG_CSV = DATA_DIR / "laundrlab_potts_status.csv"

# -----------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _status_from_text(text: str) -> Tuple[str, str]:
    t = (text or "").lower()
    if any(k in t for k in ["out of order", "fault", "error", "down"]):
        st = "Out of Order"
    elif any(k in t for k in ["available", "vacant", "free"]):
        st = "Available"
    elif any(k in t for k in ["in use", "running", "busy", "occupied"]):
        st = "In Use"
    else:
        st = "Unknown"
    # try to capture time remaining like “12m”, “12 min”, “3 minutes”
    m = re.search(r"(\d+\s*(?:m|min|mins|minutes))", t)
    detail = m.group(1) if m else ""
    return st, detail


def _guess_type(name: str) -> str:
    n = name.lower()
    if "dryer" in n or re.search(r"\bdry\b", n):
        return "Dryer"
    if "washer" in n or "wash" in n or "laundry" in n:
        return "Washer"
    return ""


def _rows_for_sheet(items: List[Dict]) -> List[List[str]]:
    ts = _now_iso()
    return [[ts, i.get("name",""), i.get("type",""), i.get("status",""), i.get("detail","")] for i in items]


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
    try:
        if not DEBUG_CSV.exists():
            DEBUG_CSV.write_text("timestamp,machine_name,machine_type,status,detail\n", encoding="utf-8")
        with DEBUG_CSV.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(",".join(x.replace(",", " ") for x in r) + "\n")
    except Exception as e:
        print("Debug CSV write failed:", e)


def _extract_table(ctx) -> List[Dict]:
    """Parse generic HTML table/grid into items."""
    items: List[Dict] = []

    # Prefer explicit <table><tbody><tr>
    trs = ctx.locator("table tbody tr")
    if trs.count() == 0:
        trs = ctx.locator("tr")

    row_count = min(200, trs.count())
    for i in range(row_count):
        tds = trs.nth(i).locator("th, td")
        if tds.count() == 0:
            continue
        cells = [_normalise(tds.nth(j).inner_text()) for j in range(tds.count())]
        # Skip header-like rows
        if any(c.lower() in {"node","name","machine"} for c in cells) and not any(re.search(r"\d", c) for c in cells):
            continue

        # Heuristics: name is first non-empty that doesn't look like a status keyword
        name = ""
        status_text = ""
        for c in cells:
            if not name and not re.search(r"(available|in use|out of order|fault|error)", c, re.I):
                name = c
            if not status_text and re.search(r"(available|in use|out of order|fault|error|\d+\s*(?:m|min|mins|minutes))", c, re.I):
                status_text = c

        if not name:
            name = cells[0] if cells else f"Machine {i+1}"

        status, detail = _status_from_text(status_text or " ".join(cells))
        items.append({
            "name": name,
            "type": _guess_type(name),
            "status": status,
            "detail": detail
        })

    return items


def _extract_cards(ctx) -> List[Dict]:
    """Fallback for card/tile layouts."""
    items: List[Dict] = []
    tiles = ctx.locator("div,li,section,article").filter(
        has_text=re.compile(r"washer|dryer|available|in use|out of order|fault|error", re.I)
    )
    tcount = min(200, tiles.count())
    for i in range(tcount):
        text = _normalise(tiles.nth(i).inner_text())
        if not text:
            continue
        # Try to pick a name-ish bit
        name_match = re.search(r"(?:washer|dryer)\s*#?\s*\d+|(?:washer|dryer)[^\s,;:]+", text, re.I)
        name = name_match.group(0) if name_match else f"Machine {i+1}"
        status, detail = _status_from_text(text)
        items.append({"name": name, "type": _guess_type(name), "status": status, "detail": detail})
    return items


def _force_lazy_iframe(page) -> Optional[object]:
    """
    On the WordPress page, if the iframe uses data-lazy-src, set it to src and wait for load.
    Returns a Frame to scrape, or None.
    """
    try:
        page.wait_for_selector("iframe", timeout=5000)
    except PWTimeout:
        return None

    # find any iframe that has data-lazy-src (WP Rocket lazyload)
    frame_el = page.locator("iframe[data-lazy-src]").first
    if frame_el.count() == 0:
        # maybe already loaded normally
        handle = page.locator("iframe").first.element_handle()
        return handle.content_frame() if handle else None

    # set src from data-lazy-src
    lazy_url = page.evaluate("(el) => el.getAttribute('data-lazy-src')", frame_el.element_handle())
    if not lazy_url:
        return None

    page.evaluate("(el, url) => { el.setAttribute('src', url); }", frame_el.element_handle(), lazy_url)
    # wait for it to become non-empty
    try:
        page.wait_for_timeout(300)  # give the browser a tick
        handle = frame_el.element_handle()
        fr = handle.content_frame() if handle else None
        if fr:
            fr.wait_for_load_state("networkidle", timeout=15000)
        return fr
    except Exception:
        return None


def _context_for_url(page, url: str):
    """
    If url is the sqinsights URL, we scrape the page directly.
    If it’s the WP page, we try to enter the lazy iframe.
    """
    if "wa.sqinsights.com" in url:
        return page
    # WordPress page with lazy iframe
    fr = _force_lazy_iframe(page)
    print("Lazy iframe forced:", bool(fr))
    return fr or page


@retry(wait=wait_fixed(5), stop=stop_after_attempt(3))
def scrape() -> List[Dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        print("Navigating to:", TARGET_URL)
        page.goto(TARGET_URL, wait_until="networkidle", timeout=90_000)

        ctx = _context_for_url(page, TARGET_URL)

        # Try table-ish first
        try:
            ctx.wait_for_selector("table, [role='grid']", timeout=10_000)
        except Exception:
            pass

        items = _extract_table(ctx)
        if not items:
            items = _extract_cards(ctx)

        browser.close()

    if not items:
        items = [{"name": "N/A", "type": "", "status": "NO_ROWS_FOUND", "detail": ""}]
    return items


def main():
    try:
        items = scrape()
        rows = _rows_for_sheet(items)
        _post_to_sheet(rows)
        _write_debug_csv(rows)  # optional
        print(f"Wrote {len(rows)} row(s) to Google Sheets.")
    except Exception as e:
        fail_rows = _rows_for_sheet([{
            "name": "N/A",
            "type": "",
            "status": "SCRAPE_ERROR",
            "detail": _normalise(str(e))[:200]
        }])
        try:
            _post_to_sheet(fail_rows)
            _write_debug_csv(fail_rows)
        finally:
            raise


if __name__ == "__main__":
    main()
