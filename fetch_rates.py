from playwright.sync_api import sync_playwright
from datetime import datetime, timezone
import json
import re
import requests
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

results = {}


def make_browser(p):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 720},
        locale="en-SG",
        timezone_id="Asia/Singapore",
    )
    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return browser, page


def fetch_wise():
    try:
        with sync_playwright() as p:
            browser, page = make_browser(p)
            page.goto(
                "https://wise.com/us/currency-converter/sgd-to-myr-rate", timeout=20000)
            page.wait_for_timeout(4000)
            content = page.content()
            browser.close()

        matches = re.findall(r'(\d+\.\d{3,5})\s*MYR', content)
        rate = float(matches[0]) if matches else None
        results["wise"] = {"rate": rate, "fee": "Small fee", "status": "ok"}
        print(f"✓ Wise: {rate}")
    except Exception as e:
        results["wise"] = {"rate": None, "status": "error", "error": str(e)}
        print(f"✗ Wise: {e}")


def fetch_cimb():
    try:
        with sync_playwright() as p:
            browser, page = make_browser(p)
            page.goto("https://www.cimbclicks.com.sg/sgd-to-myr", timeout=30000)
            page.wait_for_timeout(6000)
            body = page.inner_text("body")
            browser.close()

        match = re.search(r'SGD\s*1\.00\s*=\s*MYR\s*(\d+\.\d{4})', body)
        rate = float(match.group(1)) if match else None
        results["cimb"] = {"rate": rate, "fee": "None", "status": "ok"}
        print(f"✓ CIMB: {rate}")
    except Exception as e:
        results["cimb"] = {"rate": None, "status": "error", "error": str(e)}
        print(f"✗ CIMB: {e}")


def fetch_western_union():
    try:
        with sync_playwright() as p:
            browser, page = make_browser(p)
            page.goto(
                "https://www.westernunion.com/sg/en/currency-converter/sgd-to-myr-rate.html", timeout=30000)
            page.wait_for_timeout(6000)
            body = page.inner_text("body")
            browser.close()

        rate = None
        for line in body.split("\n"):
            line = line.strip()
            if line and re.search(r'3\.\d{2,4}', line) and len(line) < 100:
                match = re.search(r'(\d+\.\d{4})', line)
                if match:
                    val = float(match.group(1))
                    if 3.0 < val < 4.0:
                        rate = val
                        break

        results["western_union"] = {
            "rate": rate, "fee": "~SGD 5", "status": "ok"}
        print(f"✓ Western Union: {rate}")
    except Exception as e:
        results["western_union"] = {"rate": None,
                                    "status": "error", "error": str(e)}
        print(f"✗ Western Union: {e}")


def save_to_supabase():
    try:
        url = f"{SUPABASE_URL}/rest/v1/exchange_rates"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        row = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "wise": results.get("wise", {}).get("rate"),
            "cimb": results.get("cimb", {}).get("rate"),
            "western_union": results.get("western_union", {}).get("rate"),
        }
        res = requests.post(url, json=row, headers=headers)
        if res.status_code in [200, 201]:
            print(f"\n✓ Saved to Supabase: {row}")
        else:
            print(f"\n✗ Supabase error: {res.status_code} {res.text}")
    except Exception as e:
        print(f"\n✗ Supabase error: {e}")


if __name__ == "__main__":
    print(
        f"\nFetching rates — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n")
    fetch_wise()
    fetch_cimb()
    fetch_western_union()
    print("\n── Results ──")
    print(json.dumps(results, indent=2))
    save_to_supabase()
