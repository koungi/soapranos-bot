# laundrlab_potts.py
# Scrapes ALL machines (washers + dryers) from the sqinsights iframe table
# and appends rows to a Google Sheet via your Apps Script web app.
# Columns written: [timestamp_iso, machine_name, size, status]

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from datetime import datetime, timezone
from tenacity import retry, wait_fixed, stop_after_attempt
from pathlib import Path
from typing import List, Dict
import urllib.request, json, os, re

TARGET_URL = os.getenv("TARGET_URL", "https://wa.sqinsights.com/182471?room=2778")
SHEET_WEBAPP_URL = os.getenv(
    "SHEET_WEBAPP_URL",
    "https://script.google.com/macros/s/AKfycbxSQkRvbFuaiWtxhvgg81S5AzAbCaIlJWRN-XDHT87SC_gH2fGyex1GOZ7pS540hN0W/exec"
)

# optional: keep a small CSV locally during the run for log tailing
REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DEBUG_CSV = DATA_DIR / "laundrlab_potts_status.csv"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _post_to_sheet(rows: List[List[str]]) -> None:
    if not SHEET_WEBAPP_URL:
        print("SHEET_WEBAPP_URL not set; skipping Google Sheets upload.")
        return
    req = urllib.request.Request(
        SHEET_WEBAPP_URL,
        data=json.dumps(rows).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("Google Sheet response:", resp.read().decode("utf-8", "ignore"))


def _write_debug_csv(rows: List[List[str]]) -> None:
    try:
        if not DEBUG_CSV.exists():
            DEBUG_CSV.write_text("timestamp,machine_name,size,status\n", encoding="utf-8")
        with DEBUG_CSV.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(",".join(x.replace(",", " ") for x in r) + "\n")
    except Exception as e:
        print("Debug CSV write failed:", e)


def _extract_sqinsights_table(ctx) -> List[Dict]:
    """
    Expect a table with columns: Machine | Size | Status
    Returns a list of dicts for ALL rows (no filtering on status).
    """
    items: List[Dict] = []

    # wait briefly for the table to appear
    try:
        ctx.wait_for_selector("table tbody tr", timeout=15000)
    except PWTimeout:
        pass

    rows = ctx.locator("table tbody tr")
    count = rows.count()
    if count == 0:
        # fallback to any TRs if tbody isn’t present
        rows = ctx.locator("tr")
        count = rows.count()

    for i in range(count):
        tr = rows.nth(i)
        tds = tr.locator("td")
        c = tds.count()
        if c == 0:
            # header or non-data row
            continue

        # map first three cells to Machine/Size/Status when present
        name  = _clean(tds.nth(0).inner_text()) if c >= 1 else ""
        size  = _clean(tds.nth(1).inner_text()) if c >= 2 else ""
        # status text may be inside a badge/span—grab td text anyway
        status = _clean(tds.nth(2).inner_text()) if c >= 3 else ""

        # skip completely empty lines
        if not (name or size or status):
            continue

        # defensive: ignore a header row if it slipped through
        if name.lower() in {"machine", "washers", "dryers"}:
            continue

        items.append({"name": name, "size": size, "status": status})

    return items


@retry(wait=wait_fixed(5), stop=stop_after_attempt(3))
def scrape_all() -> List[Dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        print("Navigating to:", TARGET_URL)
        page.goto(TARGET_URL, wait_until="networkidle", timeout=90_000)

        items = _extract_sqinsights_table(page)

        # if nothing parsed, dump a heartbeat row so you can see the run happened
        if not items:
            items = [{"name": "N/A", "size": "", "status": "NO_ROWS_FOUND"}]

        browser.close()
        return items


def main():
    try:
        items = scrape_all()
        ts = _now_iso()
        rows = [[ts, it["name"], it["size"], it["status"]] for it in items]
        _post_to_sheet(rows)
        _write_debug_csv(rows)
        print(f"Wrote {len(rows)} row(s) to Google Sheets.")
    except Exception as e:
        ts = _now_iso()
        rows = [[ts, "N/A", "", f"SCRAPE_ERROR: {_clean(str(e))[:180]}"]]
        try:
            _post_to_sheet(rows)
            _write_debug_csv(rows)
        finally:
            raise


if __name__ == "__main__":
    main()
