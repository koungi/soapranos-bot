# laundrlab_potts.py
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from datetime import datetime, timezone
from tenacity import retry, wait_fixed, stop_after_attempt
from pathlib import Path
from typing import List, Dict
from zoneinfo import ZoneInfo
import urllib.request, json, os, re

TARGET_URL = os.getenv("TARGET_URL", "https://wa.sqinsights.com/182471?room=2778")
REFERER_URL = os.getenv("REFERER_URL", "https://laundrlab.com.au/live-status-potts-point/")
SHEET_WEBAPP_URL = os.getenv(
    "SHEET_WEBAPP_URL",
    "https://script.google.com/macros/s/AKfycbxSQkRvbFuaiWtxhvgg81S5AzAbCaIlJWRN-XDHT87SC_gH2fGyex1GOZ7pS540hN0W/exec"
)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DEBUG_CSV = DATA_DIR / "laundrlab_potts_status.csv"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def _now_sheets():
    return datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y-%m-%d %H:%M:%S")
    
def _clean(s: str) -> str: return re.sub(r"\s+", " ", (s or "").strip())

def _post_to_sheet(rows: List[List[str]]) -> None:
    req = urllib.request.Request(
        SHEET_WEBAPP_URL,
        data=json.dumps(rows).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("Google Sheet response:", resp.read().decode("utf-8", "ignore"))

def _write_debug_csv(rows: List[List[str]]) -> None:
    if not DEBUG_CSV.exists():
        DEBUG_CSV.write_text("timestamp,machine,size,status\n", encoding="utf-8")
    with DEBUG_CSV.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(",".join(x.replace(",", " ") for x in r) + "\n")

def _extract_status_table(ctx) -> List[Dict]:
    """Reads #statusTable -> td[1]=Machine, td[2]=Size, td[3]=Status."""
    items: List[Dict] = []

    # be patient: Vue/jQuery renders after XHR
    for _ in range(4):
        try:
            ctx.wait_for_selector("#statusTable tbody tr", timeout=10_000)
            break
        except PWTimeout:
            pass

    rows = ctx.locator("#statusTable tbody tr")
    count = rows.count()
    for i in range(count):
        tds = rows.nth(i).locator("td")
        if tds.count() < 4:
            continue
        machine = _clean(tds.nth(1).inner_text())
        size    = _clean(tds.nth(2).inner_text())
        status  = _clean(tds.nth(3).inner_text())  # inside <span.status-pill>
        if not (machine or size or status):  # skip empty
            continue
        items.append({"machine": machine, "size": size, "status": status})
    return items

@retry(wait=wait_fixed(5), stop=stop_after_attempt(3))
def scrape() -> List[Dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=UA,
            extra_http_headers={"Referer": REFERER_URL, "Accept-Language": "en-AU,en;q=0.8"},
            viewport={"width": 1280, "height": 1600},
        )
        page = context.new_page()
        print("Navigating to:", TARGET_URL)
        page.goto(TARGET_URL, wait_until="networkidle", timeout=90_000)

        items = _extract_status_table(page)

        if not items:
            # dump a tiny snapshot for debugging if ever needed
            try:
                html = page.inner_html("#statusTable")[:1500]
                print("DEBUG #statusTable >>>", _clean(html))
            except Exception:
                pass

        if not items:
            items = [{"machine": "N/A", "size": "", "status": "NO_ROWS_FOUND"}]

        browser.close()
        return items

def main():
    items = scrape()
    ts = _now_sheets()   # <--- use the new helper
    rows = [[ts, it["machine"], it["size"], it["status"]] for it in items]
    _post_to_sheet(rows)
    _write_debug_csv(rows)
    print(f"Wrote {len(rows)} row(s) to Google Sheets.")

if __name__ == "__main__":
    main()
